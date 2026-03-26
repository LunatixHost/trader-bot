"""Discord Bot — setup, panel management, and trade notifications.

Runs as an asyncio task within the main bot process.
Reads directly from BotState — no network calls between trading bot and Discord bot.
"""

import asyncio
import hashlib
import logging
import time
import discord
from discord.ext import tasks
from config import (
    DISCORD_BOT_TOKEN, DISCORD_PANEL_CHANNEL_NAME,
    INTERVAL_DISCORD_PANEL,
)
from discord_bot.panel import build_main_panel, build_trade_notification
from discord_bot.views import MainView
from discord_bot.chart import generate_chart
from logger import get_trades_for_pair

# Force a full refresh at least this often so the "Updated <t:...>" timestamp
# in the panel header stays reasonably current even in a flat market.
# Raised from 60s to 120s: at INTERVAL_DISCORD_PANEL=30s, this means at most
# one forced edit every 4 cycles — well within Discord's rate limits.
_PANEL_FORCE_REFRESH_SECS = 120

# Minimum gap between trade notification messages (channel.send) to prevent
# burst-sending during rapid trade sequences from triggering per-channel 429s.
_NOTIFICATION_MIN_GAP_SECS = 4.0


def _panel_fingerprint(state) -> str:
    """Cheap hash of the values that represent a panel-worthy state change.

    Deliberately excludes live price ticks: prices update every WebSocket tick
    (~1 s) but don't warrant a Discord edit — edits carry a 429 risk and show
    no visible benefit when nothing structural has changed.

    What triggers a redraw:
      - Position opened / closed (asset_balance, buy_price)
      - Decision changed (BUY / SELL / HOLD)
      - Effective score moved by ≥ 0.5 (coarse bucket)
      - Regime or trend label changed
      - Portfolio-level risk flags (crash, drawdown, paused, balance)
      - Long-term layer state (holding status)
    """
    parts = [
        f"{state.usdt_balance:.0f}",          # bucket by $1 — ignores tiny float drift
        f"{state.portfolio_current_value:.0f}",
        str(state.is_paused),
        str(state.btc_crash_active),
        str(state.in_drawdown),
        str(state.open_positions_count),
    ]
    for ps in state.pairs.values():
        # Position state — changes matter
        parts.append(f"{ps.asset_balance:.4f}")
        parts.append(f"{ps.buy_price:.2f}")
        # Decision / score — whole-number bucket (1.0 steps) prevents minor drift edits.
        # Raised from 0.5 to 1.0: at 30s interval this is sufficient; score rarely
        # changes by a full point between cycles without a regime/decision change.
        parts.append(str(getattr(ps, "decision", "HOLD")))
        parts.append(str(round(getattr(ps, "effective_score", 0.0))))
        # Regime / trend label changes are visually significant
        parts.append(str(getattr(ps, "regime", "")))
        parts.append(str(getattr(ps, "trend", "")))
        # Failed-exit suppression block visible in panel
        parts.append(str(getattr(ps, "last_failed_exit_reason", "")))

    # Long-term layer holding status
    for sym, lt_s in getattr(state, "long_term_states", {}).items():
        parts.append(f"lt:{sym}:{lt_s.holding}:{lt_s.quantity:.4f}")

    # Futures paper layer — open positions trigger a redraw
    for sym, fp_s in getattr(state, "futures_states", {}).items():
        parts.append(f"fp:{sym}:{fp_s.is_open}:{fp_s.side}:{fp_s.entry_price:.2f}")

    # Futures paper cumulative PnL (bucketed to $0.10 to avoid micro-float noise)
    fp_pnl = getattr(state, "futures_paper_realized_pnl", 0.0) or 0.0
    parts.append(f"fppnl:{round(fp_pnl * 10)}")

    return hashlib.md5("|".join(parts).encode()).hexdigest()

logger = logging.getLogger("trading_bot.discord")

# Module-level references set during startup
_state = None
_panel_message: discord.Message | None = None
_panel_channel: discord.TextChannel | None = None
_discord_client: discord.Client | None = None


class TradingDiscordBot(discord.Client):
    """Discord client for the trading bot panel."""

    def __init__(self, state):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        super().__init__(intents=intents)
        self.state = state
        self.panel_message: discord.Message | None = None
        self.panel_channel: discord.TextChannel | None = None
        self._last_fingerprint: str = ""
        self._last_refresh_ts: float = 0.0
        self._rate_limit_until: float = 0.0   # monotonic timestamp; skip edits before this
        self._last_notification_ts: float = 0.0  # throttle trade notification channel.send

    async def setup_hook(self):
        """Called when the bot is ready to set up background tasks."""
        self.refresh_panel.start()

    async def on_ready(self):
        logger.info(f"Discord bot logged in as {self.user}")
        await self._setup_panel()

    async def _setup_panel(self):
        """Find or create the trading-bot channel, clear old messages, post panel."""
        global _panel_message, _panel_channel

        for guild in self.guilds:
            # Find existing channel
            channel = discord.utils.get(
                guild.text_channels, name=DISCORD_PANEL_CHANNEL_NAME
            )

            # Create if not found
            if channel is None:
                try:
                    channel = await guild.create_text_channel(DISCORD_PANEL_CHANNEL_NAME)
                    logger.info(f"Created #{DISCORD_PANEL_CHANNEL_NAME} in {guild.name}")
                except discord.Forbidden:
                    logger.error(f"No permission to create channel in {guild.name}")
                    continue

            self.panel_channel = channel
            _panel_channel = channel

            # Clear old messages
            try:
                await channel.purge(limit=50)
            except discord.Forbidden:
                logger.warning("No permission to purge messages")

            # Post the main panel
            embed = build_main_panel(self.state)
            view = MainView(self.state, chart_callback=self._chart_callback, execute_sell_callback=self._execute_sell_callback)
            msg = await channel.send(embed=embed, view=view)
            self.panel_message = msg
            _panel_message = msg

            # Pin it
            try:
                await msg.pin()
            except discord.Forbidden:
                pass

            logger.info(f"Panel posted in #{DISCORD_PANEL_CHANNEL_NAME}")
            break  # Only use first guild

    async def _execute_sell_callback(self, pair: str, note: str = "FORCE SELL"):
        """Force-sell a position from Discord (called by AbandonConfirmView)."""
        from bot import execute_sell
        await execute_sell(pair, note=note)

    async def _chart_callback(self, interaction: discord.Interaction, pair: str):
        """Generate and send a chart for a pair (called from PairSelectView)."""
        from binance_client import binance

        base = pair.replace("USDT", "")
        logger.info(f"Chart requested for {pair}")

        try:
            trades = get_trades_for_pair(pair, limit=50)
            buf = await generate_chart(binance, pair, trades)

            if buf:
                await interaction.followup.send(
                    content=f"\U0001f4c8  **{base}/USDT** \u2014 Live Chart",
                    file=discord.File(buf, filename=f"{pair}_chart.png"),
                )
            else:
                await interaction.followup.send(
                    f"No chart data available for {base}/USDT (testnet may have limited candle history).",
                )
        except Exception as e:
            logger.error(f"Chart callback error for {pair}: {e}")
            await interaction.followup.send(
                f"Chart error for {base}/USDT: {e}",
            )

    @tasks.loop(seconds=INTERVAL_DISCORD_PANEL)
    async def refresh_panel(self):
        """Refresh the panel embed when state changes or every 30 seconds.

        Checks state fingerprint every INTERVAL_DISCORD_PANEL seconds.
        Only calls message.edit() when something visible has actually changed,
        or when _PANEL_FORCE_REFRESH_SECS have elapsed (to keep the timestamp
        in the header current). This lets the interval stay low without burning
        Discord's rate limit on duplicate edits.
        """
        if self.panel_message is None:
            return

        now = time.monotonic()

        # Honour any active rate-limit backoff before doing anything else
        if now < self._rate_limit_until:
            return

        fingerprint = _panel_fingerprint(self.state)
        state_changed = fingerprint != self._last_fingerprint
        force_due = (now - self._last_refresh_ts) >= _PANEL_FORCE_REFRESH_SECS

        if not state_changed and not force_due:
            return  # Nothing worth updating yet

        try:
            embed = build_main_panel(self.state)
            view = MainView(self.state, chart_callback=self._chart_callback, execute_sell_callback=self._execute_sell_callback)
            await self.panel_message.edit(embed=embed, view=view)
            self._last_fingerprint = fingerprint
            self._last_refresh_ts = now
        except discord.NotFound:
            # Message was deleted — repost
            logger.warning("Panel message not found, reposting...")
            await self._setup_panel()
        except discord.HTTPException as e:
            if e.status == 429:
                # Use the retry_after Discord sends us; fall back to 60 s if absent
                retry_after = float(getattr(e, "retry_after", 60))
                self._rate_limit_until = now + retry_after
                logger.warning(
                    f"Panel rate limited — backing off {retry_after:.1f}s "
                    f"(resumes in ~{retry_after:.0f}s)"
                )
            else:
                logger.error(f"Panel refresh HTTP error: {e}")
        except Exception as e:
            logger.error(f"Panel refresh error: {e}")

    @refresh_panel.before_loop
    async def before_refresh(self):
        await self.wait_until_ready()


async def send_trade_notification(action: str, pair: str, pair_state, bot_state, pl: float = 0.0):
    """Send a trade notification to the trading-bot channel.

    Called from bot.py after each trade execution.
    Throttled to _NOTIFICATION_MIN_GAP_SECS between sends to prevent burst-sends
    during rapid multi-pair activity from triggering per-channel 429 rate limits.
    """
    if _panel_channel is None:
        return

    now = time.monotonic()
    if _discord_client is not None:
        last_ts = getattr(_discord_client, "_last_notification_ts", 0.0)
        elapsed = now - last_ts
        if elapsed < _NOTIFICATION_MIN_GAP_SECS:
            # Brief async sleep to spread notifications across time.
            # This avoids dropping the notification entirely while staying below rate limit.
            await asyncio.sleep(_NOTIFICATION_MIN_GAP_SECS - elapsed)
        if _discord_client is not None:
            _discord_client._last_notification_ts = time.monotonic()

    try:
        embed = build_trade_notification(action, pair, pair_state, bot_state, pl)
        await _panel_channel.send(embed=embed)
    except Exception as e:
        logger.error(f"Failed to send trade notification: {e}")


async def start_discord_bot(state):
    """Start the Discord bot. Called as an asyncio task from bot.py."""
    global _state, _discord_client

    _state = state

    # Wire up the notification callback in bot.py
    import bot as main_bot
    main_bot.discord_notify_callback = send_trade_notification

    client = TradingDiscordBot(state)
    _discord_client = client

    try:
        await client.start(DISCORD_BOT_TOKEN)
    except discord.LoginFailure:
        logger.error("Invalid Discord bot token!")
    except Exception as e:
        logger.error(f"Discord bot error: {e}")
