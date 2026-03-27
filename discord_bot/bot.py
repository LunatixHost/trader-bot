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
    DISCORD_BOT_TOKEN, DISCORD_CHANNELS,
    INTERVAL_DISCORD_PANEL,
)
from discord_bot.panel import (
    build_main_panel, build_audit_panel, build_equity_panel,
    build_admin_panel, build_trade_notification,
)
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
_discord_client: discord.Client | None = None
# SIGNALS channel reference — set during setup; used by send_trade_notification
_signal_channel: discord.TextChannel | None = None


# Map each live-panel key to its builder function and whether it gets buttons.
# SIGNALS has no live panel — it is notification-only.
_PANEL_BUILDERS = {
    "MAIN":   (build_main_panel,   True),   # True  = attach MainView buttons
    "AUDIT":  (build_audit_panel,  False),  # False = read-only, no buttons
    "EQUITY": (build_equity_panel, False),
    "ADMIN":  (build_admin_panel,  False),  # admin status; buttons via MAIN
}


class TradingDiscordBot(discord.Client):
    """Discord client — multi-channel terminal architecture.

    Channels:
      MAIN    — active positions + full button controls
      SIGNALS — trade entry/exit notification ledger (no live panel)
      AUDIT   — entry block diagnostic (read-only live panel)
      EQUITY  — PnL accounting (read-only live panel)
      ADMIN   — system health (read-only live panel)
    """

    def __init__(self, state):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        super().__init__(intents=intents)
        self.state = state

        # Per-channel Discord objects (populated in _setup_channels)
        self._ch:  dict[str, discord.TextChannel | None] = {
            k: None for k in ("MAIN", "SIGNALS", "AUDIT", "EQUITY", "ADMIN")
        }
        self._msg: dict[str, discord.Message | None] = {
            k: None for k in ("MAIN", "AUDIT", "EQUITY", "ADMIN")
        }

        # Refresh state tracking
        self._last_fingerprint:    str   = ""
        self._last_refresh_ts:     float = 0.0
        self._rate_limit_until:    float = 0.0
        self._last_notification_ts: float = 0.0

    async def setup_hook(self):
        self.refresh_panels.start()

    async def on_ready(self):
        logger.info(f"Discord bot logged in as {self.user}")
        await self._setup_channels()

    async def _setup_channels(self):
        """Fetch all 5 channels by ID, rename them to terminal scheme, post panels."""
        global _signal_channel

        # Canonical terminal names for each role
        _TARGET_NAMES = {
            "MAIN":    "terminal-main",
            "SIGNALS": "terminal-signals",
            "AUDIT":   "terminal-audit",
            "EQUITY":  "terminal-equity",
            "ADMIN":   "terminal-admin",
        }

        logger.info("Setting up multi-channel terminal…")
        for key, ch_id in DISCORD_CHANNELS.items():
            try:
                ch = await self.fetch_channel(ch_id)
                self._ch[key] = ch

                # Rename the channel to its terminal role name if needed
                target = _TARGET_NAMES[key]
                if ch.name != target:
                    try:
                        await ch.edit(name=target)
                        logger.info(f"  [{key}] renamed #{ch.name} → #{target}")
                    except discord.Forbidden:
                        logger.warning(f"  [{key}] no permission to rename #{ch.name}")
                    except Exception as e:
                        logger.warning(f"  [{key}] rename failed: {e}")
                else:
                    logger.info(f"  [{key}] bound → #{ch.name} (id={ch_id})")

            except discord.Forbidden:
                logger.error(f"  [{key}] no access to channel {ch_id}")
            except discord.NotFound:
                logger.error(f"  [{key}] channel {ch_id} not found")
            except Exception as e:
                logger.error(f"  [{key}] fetch error: {e}")

        # Wire up the SIGNALS channel for trade notifications
        _signal_channel = self._ch.get("SIGNALS")

        # Post initial live panels
        for key, (builder, has_buttons) in _PANEL_BUILDERS.items():
            ch = self._ch.get(key)
            if ch is None:
                logger.warning(f"  [{key}] skipped (channel not available)")
                continue

            # Purge stale messages
            try:
                await ch.purge(limit=50)
            except discord.Forbidden:
                pass

            # Build embed + optional view
            try:
                embed = builder(self.state)
                view  = (
                    MainView(
                        self.state,
                        chart_callback=self._chart_callback,
                        execute_sell_callback=self._execute_sell_callback,
                    )
                    if has_buttons else None
                )
                msg = await ch.send(embed=embed, **({"view": view} if view else {}))
                self._msg[key] = msg
                try:
                    await msg.pin()
                except discord.Forbidden:
                    pass
                logger.info(f"  [{key}] panel posted in #{ch.name}")
            except Exception as e:
                logger.error(f"  [{key}] failed to post initial panel: {e}")

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

    async def _repost_panel(self, key: str, builder, has_buttons: bool):
        """Re-post a panel message after NotFound (message deleted)."""
        ch = self._ch.get(key)
        if ch is None:
            return
        try:
            embed = builder(self.state)
            view  = (
                MainView(
                    self.state,
                    chart_callback=self._chart_callback,
                    execute_sell_callback=self._execute_sell_callback,
                )
                if has_buttons else None
            )
            msg = await ch.send(embed=embed, **({"view": view} if view else {}))
            self._msg[key] = msg
            try:
                await msg.pin()
            except discord.Forbidden:
                pass
            logger.info(f"[{key}] panel re-posted after NotFound")
        except Exception as e:
            logger.error(f"[{key}] failed to re-post panel: {e}")

    @tasks.loop(seconds=INTERVAL_DISCORD_PANEL)
    async def refresh_panels(self):
        """Refresh all four live panel embeds when state changes.

        Uses a shared fingerprint: when anything meaningful changes (position
        opened/closed, regime shift, score bucket change, etc.) all four panels
        update together.  On quiet cycles only the force-refresh interval
        triggers an edit, keeping the "Updated <t:>" timestamps current.

        Rate-limit strategy: if a 429 is received on any channel we back off
        the entire refresh cycle (break out of the loop) so we don't cascade
        rate limits across the remaining channels.
        """
        now = time.monotonic()

        if now < self._rate_limit_until:
            return

        fingerprint   = _panel_fingerprint(self.state)
        state_changed = fingerprint != self._last_fingerprint
        force_due     = (now - self._last_refresh_ts) >= _PANEL_FORCE_REFRESH_SECS

        if not state_changed and not force_due:
            return

        for key, (builder, has_buttons) in _PANEL_BUILDERS.items():
            msg = self._msg.get(key)
            if msg is None:
                continue

            try:
                embed = builder(self.state)
                if has_buttons:
                    view = MainView(
                        self.state,
                        chart_callback=self._chart_callback,
                        execute_sell_callback=self._execute_sell_callback,
                    )
                    await msg.edit(embed=embed, view=view)
                else:
                    await msg.edit(embed=embed)

            except discord.NotFound:
                logger.warning(f"[{key}] panel message not found — reposting")
                await self._repost_panel(key, builder, has_buttons)

            except discord.HTTPException as e:
                if e.status == 429:
                    retry_after = float(getattr(e, "retry_after", 60))
                    self._rate_limit_until = now + retry_after
                    logger.warning(
                        f"[{key}] rate limited — backing off {retry_after:.1f}s"
                    )
                    break  # abort remaining channels this cycle
                elif e.status in (500, 502, 503, 504):
                    self._rate_limit_until = now + 30.0
                    logger.warning(f"[{key}] Discord {e.status} — backing off 30s")
                    break
                else:
                    logger.error(f"[{key}] panel HTTP error {e.status}: {e}")

            except Exception as e:
                logger.error(f"[{key}] panel refresh error: {e}")

        self._last_fingerprint = fingerprint
        self._last_refresh_ts  = now

    @refresh_panels.before_loop
    async def before_refresh(self):
        await self.wait_until_ready()


async def send_trade_notification(action: str, pair: str, pair_state, bot_state, pl: float = 0.0):
    """Send a trade notification embed to the SIGNALS channel.

    The SIGNALS channel is the read-only entry/exit ledger.  Trade notifications
    land here instead of the MAIN panel so MAIN stays clean (positions only).
    Throttled to _NOTIFICATION_MIN_GAP_SECS between sends to prevent burst-sends
    during rapid multi-pair activity from triggering per-channel 429 rate limits.
    """
    if _signal_channel is None:
        return

    now = time.monotonic()
    if _discord_client is not None:
        last_ts = getattr(_discord_client, "_last_notification_ts", 0.0)
        elapsed = now - last_ts
        if elapsed < _NOTIFICATION_MIN_GAP_SECS:
            await asyncio.sleep(_NOTIFICATION_MIN_GAP_SECS - elapsed)
        if _discord_client is not None:
            _discord_client._last_notification_ts = time.monotonic()

    try:
        embed = build_trade_notification(action, pair, pair_state, bot_state, pl)
        await _signal_channel.send(embed=embed)
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
