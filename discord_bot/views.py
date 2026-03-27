"""Views — Discord button views and interactions.

Dashboard-style replacement aligned with the panel exports.
"""

import discord
from discord.ui import View, Button, Select, button
from config import DISCORD_OWNER_ID, TRADING_PAIRS, get_base_asset
from discord_bot.panel import (
    build_signals_embed,
    build_portfolio_embed,
    build_trades_embed,
    build_config_embed,
)
from logger import get_recent_trades, get_filtered_trades


# ─── Trade History Filter ─────────────────────────────────────────────────────
# Each entry: (filter_key, display_label, emoji, short_description)
# filter_key must match the keys accepted by logger.get_filtered_trades().
_TRADE_FILTER_OPTIONS = [
    ("all",              "All Recent Trades",   "📜", "Every trade, newest first"),
    ("buys",             "BUY Entries",         "🔵", "Entry orders only"),
    ("profitable",       "Profitable Exits",    "🟢", "Closed trades with positive P/L"),
    ("losing",           "Losing Exits",        "🔴", "Closed trades with negative P/L"),
    ("signal_sell",      "SIGNAL_SELL",         "📈", "Signal-triggered exits"),
    ("trail_stop",       "TRAIL_STOP",          "📉", "Trailing stop exits"),
    ("hard_stop",        "HARD_STOP",           "🛑", "Hard stop-loss exits"),
    ("early_stagnation", "EARLY_STAGNATION",    "⏱️", "Early stagnation exits (T1 & T2)"),
    ("time_stop",        "TIME_STOP",           "⌛", "Time stop and max-hold exits"),
    ("tp1",              "TP1 / Partial Sells", "🎯", "TP1 and partial-sell exits"),
    ("futures_paper",    "Futures Paper",       "📉", "Futures paper trading entries and exits"),
]

# Quick label lookup used by the Select callback to build the embed title
_FILTER_LABEL: dict[str, str] = {v: l for v, l, _, _ in _TRADE_FILTER_OPTIONS}


class OwnerOnlyView(View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != DISCORD_OWNER_ID:
            await interaction.response.send_message(
                "⛔ You don't have permission to control this bot.",
                ephemeral=True,
            )
            return False
        return True


# ─── Trade Filter Select ──────────────────────────────────────────────────────

class TradeFilterSelect(Select):
    """Dropdown that lets the user choose which category of trades to inspect.

    Selecting an option edits the same message in-place so the chosen filter
    stays "active" — the default option in the rebuilt dropdown reflects the
    currently displayed category.
    """

    def __init__(self, current_filter: str = "all"):
        options = [
            discord.SelectOption(
                label=label,
                value=value,
                emoji=emoji,
                description=desc,
                default=(value == current_filter),
            )
            for value, label, emoji, desc in _TRADE_FILTER_OPTIONS
        ]
        super().__init__(
            placeholder="📋  Filter trade history…",
            options=options,
            custom_id="trade_filter_select",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        # Select callbacks bypass OwnerOnlyView.interaction_check so we check here.
        if interaction.user.id != DISCORD_OWNER_ID:
            await interaction.response.send_message(
                "⛔ You don't have permission.", ephemeral=True
            )
            return

        selected = self.values[0]
        trades = get_filtered_trades(selected, limit=15)
        label = _FILTER_LABEL.get(selected, "All Recent Trades")
        embed = build_trades_embed(trades, title=f"📜 Trades — {label}")

        # Rebuild the view with the new selection marked as default
        new_view = TradeHistoryView(current_filter=selected)
        await interaction.response.edit_message(embed=embed, view=new_view)


class TradeHistoryView(OwnerOnlyView):
    """Persistent view attached to the trade history message.

    Contains only the filter dropdown.  Timeout is generous (5 min) so the
    user can browse filters without the view expiring mid-session.
    """

    def __init__(self, current_filter: str = "all"):
        super().__init__(timeout=300)
        self.current_filter = current_filter
        self.add_item(TradeFilterSelect(current_filter=current_filter))


# ─── Main Control View ────────────────────────────────────────────────────────

class MainView(OwnerOnlyView):
    def __init__(self, state, chart_callback=None, execute_sell_callback=None):
        super().__init__(timeout=None)
        self.state = state
        self.chart_callback = chart_callback
        self.execute_sell_callback = execute_sell_callback
        self._update_pause_button()

    def _update_pause_button(self):
        for child in self.children:
            if getattr(child, "custom_id", "") == "btn_pause":
                if self.state.is_paused:
                    child.label = "▶ Resume"
                    child.style = discord.ButtonStyle.success
                else:
                    child.label = "⏸ Pause"
                    child.style = discord.ButtonStyle.danger
                break

    @button(label="⏸ Pause", style=discord.ButtonStyle.danger, custom_id="btn_pause")
    async def pause_button(self, interaction: discord.Interaction, btn: Button):
        self.state.is_paused = not self.state.is_paused
        self._update_pause_button()
        status = "⏸ PAUSED" if self.state.is_paused else "▶ RESUMED"
        await interaction.response.send_message(f"Bot is now **{status}**", ephemeral=False)

    @button(label="📊 Signals", style=discord.ButtonStyle.primary, custom_id="btn_signals")
    async def signals_button(self, interaction: discord.Interaction, btn: Button):
        view = PairSelectView(self.state, mode="signals")
        await interaction.response.send_message(
            "📊 Select a pair to inspect:",
            view=view,
            ephemeral=False,
        )

    @button(label="📈 Chart", style=discord.ButtonStyle.primary, custom_id="btn_chart")
    async def chart_button(self, interaction: discord.Interaction, btn: Button):
        view = PairSelectView(self.state, mode="chart", chart_callback=self.chart_callback)
        await interaction.response.send_message(
            "📈 Select a pair to chart:",
            view=view,
            ephemeral=False,
        )

    @button(label="💼 Portfolio", style=discord.ButtonStyle.primary, custom_id="btn_portfolio")
    async def portfolio_button(self, interaction: discord.Interaction, btn: Button):
        await interaction.response.send_message(
            embed=build_portfolio_embed(self.state),
            ephemeral=False,
        )

    @button(label="📜 Trades", style=discord.ButtonStyle.primary, custom_id="btn_trades")
    async def trades_button(self, interaction: discord.Interaction, btn: Button):
        """Open the trade history view with the filter dropdown defaulting to All."""
        trades = get_filtered_trades("all", limit=15)
        embed = build_trades_embed(trades, title="📜 Trades — All Recent Trades")
        view = TradeHistoryView(current_filter="all")
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)

    @button(label="⚙️ Config", style=discord.ButtonStyle.secondary, custom_id="btn_config")
    async def config_button(self, interaction: discord.Interaction, btn: Button):
        await interaction.response.send_message(
            embed=build_config_embed(self.state),
            ephemeral=False,
        )

    @button(label="🗑️ Abandon", style=discord.ButtonStyle.danger, custom_id="btn_abandon")
    async def abandon_button(self, interaction: discord.Interaction, btn: Button):
        open_pairs = [pair for pair, ps in self.state.pairs.items() if ps.asset_balance > 0]
        if not open_pairs:
            await interaction.response.send_message("ℹ️ No open positions to abandon.", ephemeral=False)
            return
        view = AbandonSelectView(self.state, open_pairs, execute_sell_callback=self.execute_sell_callback)
        await interaction.response.send_message(
            "🗑️ Select which position to force-close:",
            view=view,
            ephemeral=False,
        )


# ─── Pair Select View ─────────────────────────────────────────────────────────

class PairSelectView(OwnerOnlyView):
    def __init__(self, state, mode: str = "signals", chart_callback=None):
        super().__init__(timeout=120)
        self.state = state
        self.mode = mode
        self.chart_callback = chart_callback

        for pair_cfg in TRADING_PAIRS:
            pair = pair_cfg["pair"]
            base = get_base_asset(pair)
            btn = Button(
                label=f"{base}/USDT",
                style=discord.ButtonStyle.primary,
                custom_id=f"pair_{pair}_{mode}",
            )
            btn.callback = self._make_callback(pair)
            self.add_item(btn)

    def _make_callback(self, pair: str):
        async def callback(interaction: discord.Interaction):
            if self.mode == "signals":
                ps = self.state.pairs.get(pair)
                if ps:
                    await interaction.response.send_message(
                        embed=build_signals_embed(ps, self.state),
                        ephemeral=False,
                    )
                else:
                    await interaction.response.send_message(
                        f"No data for {pair}",
                        ephemeral=False,
                    )
            elif self.mode == "chart":
                await interaction.response.defer(ephemeral=False)
                if self.chart_callback:
                    await self.chart_callback(interaction, pair)
                else:
                    await interaction.followup.send("Chart generation not available.", ephemeral=False)
        return callback


# ─── Abandon Views ────────────────────────────────────────────────────────────

class AbandonSelectView(OwnerOnlyView):
    def __init__(self, state, open_pairs: list[str], execute_sell_callback=None):
        super().__init__(timeout=60)
        self.state = state
        self.execute_sell_callback = execute_sell_callback

        for pair in open_pairs:
            ps = state.pairs[pair]
            base = get_base_asset(pair)
            gain_pct = 0.0
            if ps.buy_price > 0 and ps.current_price > 0:
                gain_pct = (ps.current_price - ps.buy_price) / ps.buy_price * 100
            label = f"{base}  {gain_pct:+.2f}%"
            color = discord.ButtonStyle.danger if gain_pct < 0 else discord.ButtonStyle.secondary
            btn = Button(label=label, style=color, custom_id=f"abandon_select_{pair}")
            btn.callback = self._make_callback(pair)
            self.add_item(btn)

    def _make_callback(self, pair: str):
        async def callback(interaction: discord.Interaction):
            if not await self.interaction_check(interaction):
                return
            ps = self.state.pairs.get(pair)
            if ps is None or ps.asset_balance <= 0:
                await interaction.response.send_message(
                    f"ℹ️ {get_base_asset(pair)} has no open position.",
                    ephemeral=False,
                )
                return

            base = get_base_asset(pair)
            val = ps.asset_balance * ps.current_price
            gain_pct = ((ps.current_price - ps.buy_price) / ps.buy_price * 100) if ps.buy_price > 0 else 0.0
            view = AbandonConfirmView(self.state, pair, execute_sell_callback=self.execute_sell_callback)
            await interaction.response.send_message(
                f"⚠️ Confirm force sell {base}\n"
                f"Qty: {ps.asset_balance:.6f} {base}\n"
                f"Value: ${val:.4f} USDT\n"
                f"P/L: {gain_pct:+.2f}%\n"
                f"Entry: ${ps.buy_price:.4f}",
                view=view,
                ephemeral=False,
            )
        return callback


# ─── Futures Kill Switch ──────────────────────────────────────────────────────

class FuturesKillSwitchView(OwnerOnlyView):
    """Danger button attached to every futures SIGNALS notification.

    Clicking FORCE CLOSE calls execute_futures_exit() with reduceOnly=True —
    the same path used by normal SL/TP exits, so it cannot open a reversal.
    Button is disabled after one press or when the interaction times out.
    """

    def __init__(self, symbol: str, side: str):
        super().__init__(timeout=3600)   # live for 1 hour
        self.symbol = symbol
        self.side   = side               # 'long' | 'short'

    @discord.ui.button(
        label="⛔ FORCE CLOSE",
        style=discord.ButtonStyle.danger,
        custom_id="futures_force_close",
    )
    async def force_close(self, interaction: discord.Interaction, btn: discord.ui.Button):
        btn.disabled = True
        btn.label    = "Closing…"
        await interaction.response.edit_message(view=self)

        try:
            from futures_execution import execute_futures_exit
            await execute_futures_exit(self.symbol, self.side, None)  # None = full close
            await interaction.followup.send(
                f"✅ Emergency close sent for **{self.symbol}** ({self.side.upper()}). "
                f"Check Binance to confirm fill.",
                ephemeral=True,
            )
        except Exception as e:
            btn.label = "⛔ FORCE CLOSE"
            btn.disabled = False
            await interaction.followup.send(
                f"❌ Close failed: `{e}`",
                ephemeral=True,
            )
        finally:
            self.stop()


class AbandonConfirmView(OwnerOnlyView):
    def __init__(self, state, pair: str, execute_sell_callback=None):
        super().__init__(timeout=30)
        self.state = state
        self.pair = pair
        self.execute_sell_callback = execute_sell_callback

    @button(label="✅ Confirm Sell", style=discord.ButtonStyle.danger, custom_id="abandon_confirm")
    async def confirm(self, interaction: discord.Interaction, btn: Button):
        pair = self.pair
        ps = self.state.pairs.get(pair)
        if ps is None or ps.asset_balance <= 0:
            await interaction.response.send_message("ℹ️ Position already cleared.", ephemeral=False)
            return

        base = get_base_asset(pair)
        qty = ps.asset_balance
        entry = ps.buy_price
        val = qty * ps.current_price
        gain_pct = ((ps.current_price - entry) / entry * 100) if entry > 0 else 0.0

        await interaction.response.send_message(
            f"🗑️ Force selling {base}...\n"
            f"Qty: {qty:.6f} {base} (${val:.4f})\n"
            f"Entry: ${entry:.4f}   P/L: {gain_pct:+.2f}%",
            ephemeral=False,
        )
        self.stop()

        if self.execute_sell_callback:
            await self.execute_sell_callback(pair, note="FORCE SELL")

    @button(label="❌ Cancel", style=discord.ButtonStyle.secondary, custom_id="abandon_cancel")
    async def cancel(self, interaction: discord.Interaction, btn: Button):
        await interaction.response.send_message("❌ Abandon cancelled.", ephemeral=False)
        self.stop()
