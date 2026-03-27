"""Discord Bot — setup, panel management, and trade notifications.

Runs as an asyncio task within the main bot process.
Reads directly from BotState — no network calls between trading bot and Discord bot.
"""

import asyncio
import hashlib
import logging
import time
import discord
from discord import app_commands
from discord.ext import commands, tasks
from config import (
    DISCORD_BOT_TOKEN, DISCORD_CHANNELS,
    INTERVAL_DISCORD_PANEL,
)
from discord_bot.panel import (
    build_main_panel, build_audit_panel, build_equity_panel,
    build_admin_panel, build_trade_notification,
)
from discord_bot.views import MainView, FuturesKillSwitchView
from discord_bot.chart import generate_chart
from logger import get_trades_for_pair
from alert_router import alert_router

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


class TradingDiscordBot(commands.Bot):
    """Discord bot — multi-channel terminal architecture with slash commands.

    Channels:
      MAIN    — active positions + full button controls
      SIGNALS — trade entry/exit notification ledger (no live panel)
      AUDIT   — entry block diagnostic (read-only live panel)
      EQUITY  — PnL accounting (read-only live panel)
      ADMIN   — system health (read-only live panel)

    Slash commands: /status  /portfolio  /pause  /resume  /close
    """

    def __init__(self, state):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        super().__init__(command_prefix="!", intents=intents)
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
        self.consume_alerts.start()
        self._register_slash_commands()

    async def on_ready(self):
        logger.info(f"Discord bot logged in as {self.user}")
        try:
            synced = await self.tree.sync()
            logger.info(f"Slash commands synced: {[c.name for c in synced]}")
        except Exception as e:
            logger.warning(f"Slash command sync failed: {e}")
        await self._setup_channels()

    # ─── Slash Commands ───────────────────────────────────────────────────────

    def _register_slash_commands(self):
        """Register all app_commands (slash commands) on the bot's tree."""

        @self.tree.command(name="status", description="Live bot status summary")
        async def slash_status(interaction: discord.Interaction):
            from discord_bot.panel import build_main_panel
            embed = build_main_panel(self.state)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="portfolio", description="Portfolio exposure and P/L")
        async def slash_portfolio(interaction: discord.Interaction):
            from discord_bot.panel import build_portfolio_embed
            embed = build_portfolio_embed(self.state)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="audit", description="Entry block diagnostics")
        async def slash_audit(interaction: discord.Interaction):
            from discord_bot.panel import build_audit_panel
            embed = build_audit_panel(self.state)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="pause", description="Pause the trading engine")
        async def slash_pause(interaction: discord.Interaction):
            from config import DISCORD_OWNER_ID
            if interaction.user.id != DISCORD_OWNER_ID:
                await interaction.response.send_message("⛔ Unauthorized.", ephemeral=True)
                return
            self.state.is_paused = True
            await interaction.response.send_message("⏸ Bot **PAUSED**.", ephemeral=False)

        @self.tree.command(name="resume", description="Resume the trading engine")
        async def slash_resume(interaction: discord.Interaction):
            from config import DISCORD_OWNER_ID
            if interaction.user.id != DISCORD_OWNER_ID:
                await interaction.response.send_message("⛔ Unauthorized.", ephemeral=True)
                return
            self.state.is_paused = False
            await interaction.response.send_message("▶ Bot **RESUMED**.", ephemeral=False)

    # ─── Alert Queue Consumer ─────────────────────────────────────────────────

    @tasks.loop(seconds=0.5)
    async def consume_alerts(self):
        """Drain the alert_router queue and send embeds to SIGNALS channel."""
        ch = self._ch.get("SIGNALS")
        if ch is None:
            return

        while not alert_router.queue.empty():
            try:
                payload = alert_router.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            try:
                await self._dispatch_payload(ch, payload)
            except discord.HTTPException as e:
                if e.status == 429:
                    retry = float(getattr(e, "retry_after", 5))
                    logger.warning(f"[SIGNALS] rate limited — sleeping {retry:.1f}s")
                    await asyncio.sleep(retry)
                    # Re-queue so it's not lost
                    alert_router.queue.put_nowait(payload)
                    break
                else:
                    logger.error(f"[SIGNALS] HTTP {e.status}: {e}")
            except Exception as e:
                logger.error(f"[SIGNALS] dispatch error: {e}")
            finally:
                alert_router.queue.task_done()

    @consume_alerts.before_loop
    async def before_consume(self):
        await self.wait_until_ready()

    async def _dispatch_payload(self, ch: discord.TextChannel, payload: dict):
        """Route a queue payload to the correct embed builder and send it."""
        ptype = payload.get("type")

        if ptype == "TRADE":
            embed = build_trade_notification(
                payload["action"],
                payload["pair"],
                payload["pair_state"],
                payload["bot_state"],
                payload["pl"],
            )
            await ch.send(embed=embed)

        elif ptype == "FUTURES":
            embed = self._build_futures_embed(payload)
            is_entry = "ENTRY" in payload["action"]
            view = (
                FuturesKillSwitchView(payload["symbol"], payload["side"])
                if is_entry else None
            )
            if view:
                await ch.send(embed=embed, view=view)
            else:
                await ch.send(embed=embed)

        elif ptype == "SYSTEM":
            level = payload.get("level", "warning")
            color = {
                "info":    0x3498db,
                "warning": 0xFFD700,
                "error":   0xFF0000,
            }.get(level, 0xFFD700)
            embed = discord.Embed(
                title="⚙️ System Alert",
                description=payload["message"],
                color=color,
            )
            await ch.send(embed=embed)

    @staticmethod
    def _build_futures_embed(p: dict) -> discord.Embed:
        """Institutional-style embed for futures entries and exits."""
        is_long   = p["side"] == "long"
        is_entry  = "ENTRY" in p["action"]
        pl        = p.get("pl", 0.0)

        if is_entry:
            color = 0x2ecc71 if is_long else 0xe74c3c
        else:
            color = 0x2ecc71 if pl >= 0 else 0xe74c3c

        direction_icon = "📈" if is_long else "📉"
        action_label   = p["action"].replace("_", " ")

        embed = discord.Embed(
            title=f"⚡ {action_label}  ·  {p['symbol']}",
            color=color,
        )
        embed.add_field(name="Direction",    value=f"{direction_icon} **{p['side'].upper()}**", inline=True)
        embed.add_field(name="Entry Price",  value=f"`${p['entry_price']:,.4f}`",               inline=True)
        embed.add_field(name="Size (USDT)",  value=f"`${p['size']:.2f}`",                       inline=True)

        if p.get("sl_price"):
            embed.add_field(name="Stop Loss",  value=f"`${p['sl_price']:,.4f}`", inline=True)
        if p.get("tp_price"):
            embed.add_field(name="Take Profit",value=f"`${p['tp_price']:,.4f}`", inline=True)

        if not is_entry and pl != 0.0:
            sign = "+" if pl >= 0 else ""
            embed.add_field(
                name="Realized PnL",
                value=f"**{sign}${pl:.4f}**",
                inline=False,
            )

        embed.set_footer(text="USDⓈ-M Futures  ·  5× Isolated  ·  reduceOnly exits")
        return embed

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


def send_trade_notification(action: str, pair: str, pair_state, bot_state, pl: float = 0.0):
    """Drop a trade alert onto the queue — non-blocking, returns immediately.

    The queue consumer in TradingDiscordBot.consume_alerts() picks this up
    and handles the actual Discord API send with rate-limit backoff.

    Signature kept synchronous so bot.py callers don't need to await it
    (they already fire-and-forget via discord_notify_callback).
    """
    alert_router.dispatch_trade_alert(action, pair, pair_state, bot_state, pl)


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
