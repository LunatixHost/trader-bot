"""Panel — Discord embed builders for the trading bot.

Trader-dashboard panel with WATCHING grouped into one section and
multiple code blocks inside that section, matching the requested style.
Compatible with current discord bot and views imports.
"""

import discord
import sqlite3
from datetime import datetime, timezone, timedelta, time as dt_time
from config import (
    USE_TESTNET,
    PORTFOLIO_RESERVE_PCT,
    PORTFOLIO_MAX_POSITIONS,
    RSI_BUY_THRESHOLD,
    RSI_SELL_THRESHOLD,
    STOP_LOSS_PCT,
    CONFIDENCE_BUY_THRESHOLD,
    TRADE_SIZE_PCT,
    MAX_POSITION_PCT,
    PROFIT_TARGET_PCT,
    INTERVAL_RSI,
    INTERVAL_ETHERSCAN,
    INTERVAL_DISCORD_PANEL,
    PORTFOLIO_REBALANCE_INTERVAL,
    TRADE_COOLDOWN_SECONDS,
    TRADE_COOLDOWN_TRENDING,
    COOLDOWN_BYPASS_CONFIDENCE,
    COOLDOWN_BYPASS_REGIMES,
    OB_IMBALANCE_BULL,
    OB_IMBALANCE_BEAR,
    OB_FLOW_BULL,
    OB_FLOW_BEAR,
    BASE_TP1_PCT,
    SPIKE_FILTER_PCT,
    SPREAD_MAX_PCT,
    UNSTABLE_CANDLE_ATR_MULT,
    MIN_TRADE_NOTIONAL,
    FVG_COOLDOWN_SECONDS,
    BTC_CRASH_SIZE_MULTIPLIER,
    MAX_POSITION_PCT,
    TRADE_SIZE_PCT,
    FAILED_EXIT_COOLDOWN_STAGNATION,
    FAILED_EXIT_COOLDOWN_TRAIL_STOP,
    FAILED_EXIT_COOLDOWN_HARD_STOP,
    ROUND_TRIP_COST_PCT,
    ATR_TP_MULTIPLIER,
    LONG_TERM_ENABLED,
    LONG_TERM_ALLOCATION,
    LONG_TERM_ASSETS,
    LONG_TERM_ENTRY_THRESHOLD,
    LONG_TERM_EXIT_THRESHOLD,
    FUTURES_PAPER_ENABLED,
    FUTURES_SHORT_ENABLED,
    FUTURES_LONG_ENABLED,
    FUTURES_MAX_LEVERAGE,
    FUTURES_POSITION_SIZE_USDT,
    FUTURES_MAX_CONCURRENT_POSITIONS,
)

# ─── Embed color palette ─────────────────────────────────────────────────────
# All colors in one place so state → color mapping is easy to adjust.
COLOR_GREEN  = 0x2ECC71   # healthy / profitable / BUY
COLOR_YELLOW = 0xF1C40F   # slightly down / caution
COLOR_ORANGE = 0xE67E22   # defensive / drawdown
COLOR_RED    = 0xE74C3C   # crash / significant loss / hard stop
COLOR_DARK_RED = 0xC0392B # hard/trail stop loss
COLOR_BLUE   = 0x3498DB   # informational / neutral
COLOR_GRAY   = 0x95A5A6   # paused / unknown


def _portfolio_color(state) -> int:
    """Choose embed left-stripe color based on portfolio + risk state.

    Priority (highest first):
      paused       → gray
      btc_crash    → red
      in_drawdown  → orange
      pl < -1%     → amber
      pl < 0%      → yellow
      pl >= 0%     → green
    """
    if state.is_paused:
        return COLOR_GRAY
    if getattr(state, "btc_crash_active", False):
        return COLOR_RED
    if getattr(state, "in_drawdown", False):
        return COLOR_ORANGE
    pl_pct = getattr(state, "portfolio_pl_pct", 0.0) or 0.0
    if pl_pct < -1.0:
        return COLOR_ORANGE
    if pl_pct < 0.0:
        return COLOR_YELLOW
    return COLOR_GREEN


def _bot_status_label(state) -> str:
    """Short one-line state badge used in the embed title."""
    if state.is_paused:
        return "⏸ PAUSED"
    if getattr(state, "btc_crash_active", False):
        return "🔴 BTC CRASH"
    if getattr(state, "in_drawdown", False):
        return "🟠 DEFENSIVE"
    pl_pct = getattr(state, "portfolio_pl_pct", 0.0) or 0.0
    if pl_pct < -0.5:
        return "🟡 CAUTION"
    return "🟢 ACTIVE"


def _uptime(start: datetime) -> str:
    delta = datetime.now(timezone.utc) - start
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours}h {minutes}m"


def _relative_time(ts_str: str) -> str:
    """Convert an ISO timestamp to a human-readable relative string.

    Timezone-agnostic — always shows how long ago, not a clock time.
    Safe to display inside code blocks where Discord <t:> tags don't render.
    """
    try:
        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        secs = int((datetime.now(timezone.utc) - ts).total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            h, m = secs // 3600, (secs % 3600) // 60
            return f"{h}h {m:02d}m ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return "?"


def _format_price(price: float) -> str:
    if price >= 1000:
        return f"${price:,.2f}"
    if price >= 1:
        return f"${price:.4f}"
    return f"${price:.6f}"


def _compact_price(price: float) -> str:
    if price >= 10000:
        return f"${price:,.0f}"
    if price >= 1000:
        return f"${price:,.2f}"
    if price >= 1:
        return f"${price:.4f}"
    return f"${price:.5f}"


def _pl_display(value: float, pct: float | None = None) -> str:
    emoji = "🟢" if value > 0 else ("🔴" if value < 0 else "⚪")
    sign = "+" if value >= 0 else ""
    text = f"{sign}${value:.2f} {emoji}"
    if pct is not None:
        text += f" ({sign}{pct:.2f}%)"
    return text


def _minutes_held(position_open_time: str) -> int:
    if not position_open_time:
        return -1
    try:
        ts = datetime.fromisoformat(str(position_open_time).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - ts).total_seconds() / 60))
    except Exception:
        return -1


def _cooldown_remaining(last_trade_time: str, cooldown_s: int = TRADE_COOLDOWN_SECONDS) -> int:
    if not last_trade_time:
        return 0
    try:
        ts = datetime.fromisoformat(str(last_trade_time).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
        return max(0, int(cooldown_s - elapsed))
    except Exception:
        return 0


def _effective_score(ps) -> float:
    # Use the score that strategy.py actually compared to threshold.
    # strategy.py writes this after applying all penalties (wick, edge, RR, trend).
    # Reading it here keeps panel and strategy permanently aligned.
    return float(getattr(ps, "effective_score", 0.0))


def _effective_threshold(ps) -> float:
    # Use the threshold strategy.py computed (includes drawdown/crash boosts).
    # Falls back to base buy_threshold, then to config default.
    dp = getattr(ps, "dynamic_params", {}) or {}
    return float(
        dp.get("effective_threshold", dp.get("buy_threshold", CONFIDENCE_BUY_THRESHOLD))
    )


def _entry_block_reason(ps, state) -> str | None:
    """Mirror of bot.py's get_entry_block_reason — same logic, no imports needed.

    Used by _watch_block so the panel shows the exact same blocker that
    execute_buy() would enforce. Keep in sync with bot.py manually.
    """
    # 1) No averaging down
    if ps.asset_balance > 0 and ps.buy_price > 0 and ps.current_price < ps.buy_price:
        return "add_blocked"

    # 2) Cooldown
    last_trade_time = getattr(ps, "last_trade_time", None)
    if last_trade_time:
        try:
            last_time = datetime.fromisoformat(last_trade_time)
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - last_time).total_seconds()
            regime = getattr(ps, "regime", "ranging")
            cooldown = (
                TRADE_COOLDOWN_TRENDING
                if regime in {"trending", "trending_volatile"}
                else TRADE_COOLDOWN_SECONDS
            )
            if elapsed < cooldown:
                eff = _effective_score(ps)
                thr = _effective_threshold(ps)
                can_bypass = (
                    eff >= thr + COOLDOWN_BYPASS_CONFIDENCE
                    and (not COOLDOWN_BYPASS_REGIMES or regime in COOLDOWN_BYPASS_REGIMES)
                )
                if not can_bypass:
                    return "cooldown"
        except (ValueError, TypeError):
            pass

    # 2b) Failed-exit re-entry suppression
    _FAILED_SUPPRESS = {
        "EARLY_STAGNATION":   FAILED_EXIT_COOLDOWN_STAGNATION,
        "EARLY_STAGNATION_2": FAILED_EXIT_COOLDOWN_STAGNATION,
        "TRAIL_STOP":         FAILED_EXIT_COOLDOWN_TRAIL_STOP,
        "HARD_STOP":          FAILED_EXIT_COOLDOWN_HARD_STOP,
    }
    failed_reason = getattr(ps, "last_failed_exit_reason", "")
    failed_time   = getattr(ps, "last_failed_exit_time", "")
    if failed_reason and failed_time:
        suppress_s = _FAILED_SUPPRESS.get(failed_reason, 0)
        if suppress_s > 0 and _cooldown_remaining(failed_time, suppress_s) > 0:
            return "failed_exit_cooldown"

    # 3) Spike filter — single-tick explosive move
    prev_price = getattr(ps, "prev_price", 0.0)
    if prev_price > 0:
        tick_move = abs(ps.current_price - prev_price) / prev_price
        if tick_move > SPIKE_FILTER_PCT:
            return "spike_filter"

    # 3b) Unstable candle — last candle range abnormally large vs ATR
    atr_pct = getattr(ps, "atr_pct", 0.0)
    if atr_pct > 0:
        candle_range_pct = getattr(ps, "candle_range_pct", 0.0)
        if candle_range_pct > atr_pct * 100 * UNSTABLE_CANDLE_ATR_MULT:
            return "unstable_candle"

    # 3c) Economic viability hard block — mirrors bot.py check 3c
    atr_pct = getattr(ps, "atr_pct", 0.0)
    if atr_pct > 0:
        atr_reward_pct = atr_pct * ATR_TP_MULTIPLIER * 100
        if atr_reward_pct < ROUND_TRIP_COST_PCT:
            return "uneconomic_move"

    # 4) Spread filter (live only)
    spread = getattr(ps, "bid_ask_spread_pct", 0.0)
    if not USE_TESTNET and spread > SPREAD_MAX_PCT:
        return "spread_filter"

    # 5) Spendable capital
    spendable = getattr(state, "usdt_balance", 0.0) - getattr(state, "usdt_reserved", 0.0)
    if spendable <= 1.0:
        return "insufficient_spendable"

    # 6) Position room
    max_position = getattr(state, "portfolio_total_usdt", 0.0) * MAX_POSITION_PCT
    current_position = ps.asset_balance * ps.current_price if ps.current_price > 0 else 0.0
    room = max_position - current_position
    if room <= 1.0:
        return "position_maxed"

    # 7) Min notional
    trade_size = getattr(state, "portfolio_total_usdt", 0.0) * TRADE_SIZE_PCT
    crash_modifier = BTC_CRASH_SIZE_MULTIPLIER if getattr(state, "btc_crash_active", False) else 1.0
    trade_size *= crash_modifier
    if min(trade_size, spendable, room) < MIN_TRADE_NOTIONAL:
        return "min_notional"

    # 8) FVG cooldown
    if getattr(ps, "fvg_detected", False) and getattr(ps, "last_fvg_trade_time", None):
        try:
            last_fvg = datetime.fromisoformat(ps.last_fvg_trade_time)
            if last_fvg.tzinfo is None:
                last_fvg = last_fvg.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_fvg).total_seconds() < FVG_COOLDOWN_SECONDS:
                return "fvg_cooldown"
        except (ValueError, TypeError):
            pass

    return None


def _decision_icon(decision: str) -> str:
    return {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}.get(decision, "⚪")


def _bool_icon(value: bool) -> str:
    return "✓" if value else "✗"


def _tri_icon(value: float, bull: float, bear: float) -> str:
    if value > bull:
        return "✓"
    if value < bear:
        return "✗"
    return "≈"


def _held_str(position_open_time: str) -> str:
    mins = _minutes_held(position_open_time)
    if mins < 0:
        return "?"
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h {mins % 60}m"


def _sell_actions():
    return (
        "SELL", "STOP_LOSS", "SIGNAL_SELL", "EARLY_FAILURE",
        "HARD_STOP", "TRAIL_STOP", "TIME_STOP", "MAX_HOLD_EXIT",
        "TP1_FULL_EXIT", "FORCE SELL",
    )


def _get_24h_stats() -> tuple[int, float]:
    # Hard reset at UTC midnight — all trades clear simultaneously instead of
    # rolling off one-by-one as they age past 24 hours.
    # Returns (closed_trades_today, win_rate) — BUY rows are excluded so the
    # count matches the "All-time closed trades" metric in get_trade_stats().
    today_midnight = datetime.combine(
        datetime.now(timezone.utc).date(), dt_time.min
    ).replace(tzinfo=timezone.utc)
    cutoff = today_midnight.isoformat()
    sell_actions = _sell_actions()
    placeholders = ",".join("?" for _ in sell_actions)
    try:
        conn = sqlite3.connect("trades.db")
        sells = conn.execute(
            f"SELECT COUNT(*) FROM trades WHERE action IN ({placeholders}) AND timestamp >= ?",
            (*sell_actions, cutoff),
        ).fetchone()[0]
        profitable = conn.execute(
            f"SELECT COUNT(*) FROM trades WHERE action IN ({placeholders}) AND pl_usdt > 0 AND timestamp >= ?",
            (*sell_actions, cutoff),
        ).fetchone()[0]
        conn.close()
        win_rate = (profitable / sells * 100) if sells > 0 else 0.0
        return sells, round(win_rate, 1)
    except Exception:
        return 0, 0.0


def _market_state(all_ps) -> tuple[str, float, str]:
    if not all_ps:
        return "Normal", 0.0, "Neutral ↔"

    atr_vals = [float(getattr(ps, "atr_pct", 0.0) or 0.0) for ps in all_ps if getattr(ps, "atr_pct", 0.0) > 0]
    avg_atr_pct = (sum(atr_vals) / len(atr_vals) * 100) if atr_vals else 0.0

    avg_struct = sum(float(getattr(ps, "structure_score", 0.0) or 0.0) for ps in all_ps) / len(all_ps)
    if avg_struct > 1.0:
        trend_bias = "Strong Long ↗↗"
    elif avg_struct > 0.3:
        trend_bias = "Slight Long ↗"
    elif avg_struct < -1.0:
        trend_bias = "Strong Short ↘↘"
    elif avg_struct < -0.3:
        trend_bias = "Slight Short ↘"
    else:
        trend_bias = "Neutral ↔"

    vol_expansion = sum(1 for ps in all_ps if getattr(ps, "volatility_regime", "normal") == "expansion")
    market_vol = "Expansion ⚡" if vol_expansion > len(all_ps) / 2 else "Normal"

    return market_vol, avg_atr_pct, trend_bias


def _watch_block(ps, state) -> str:
    eff = _effective_score(ps)
    threshold = _effective_threshold(ps)
    ob = getattr(ps, "orderbook_imbalance", 0.5)
    flow = getattr(ps, "flow_ratio", 0.5)
    bos_icon = _bool_icon(getattr(ps, "bos", False))
    sweep_icon = "✓" if getattr(ps, "sweep_type", "none") != "none" or getattr(ps, "liquidity_sweep_score", 0.0) > 0 else "✗"
    rsi_icon = _bool_icon(getattr(ps, "rsi", 50.0) < RSI_BUY_THRESHOLD)
    regime_label = str(getattr(ps, "regime", "normal")).replace("_", " ").title()
    decision = getattr(ps, "decision", "HOLD")

    # Use the same execution-layer block check as bot.py
    block_reason = _entry_block_reason(ps, state)

    _BLOCK_LABELS = {
        "add_blocked":           "avg-down blocked",
        "cooldown":              _cooldown_label(ps),
        "failed_exit_cooldown":  _failed_exit_cooldown_label(ps),
        "spike_filter":          "price spike",
        "unstable_candle":       "volatile candle",
        "uneconomic_move":       "low reward (ATR < fee cost)",
        "spread_filter":         "wide spread",
        "insufficient_spendable":"no capital",
        "position_maxed":        "position maxed",
        "min_notional":          "order too small",
        "fvg_cooldown":          "FVG cooldown",
    }

    if block_reason is not None:
        reason_text = _BLOCK_LABELS.get(block_reason, block_reason)
        if decision == "BUY":
            status = f"READY but BLOCKED ({reason_text})"
        else:
            status = f"BLOCKED ({reason_text})"
    elif decision == "BUY":
        status = "READY ✓"
    elif eff < threshold:
        gap = threshold - eff
        status = f"HOLD (need +{gap:.1f})"
    else:
        status = f"Watching — {decision}"

    lines = [
        f"{ps.base_asset}/USDT — {_compact_price(ps.current_price)}",
        f"Regime: {regime_label} | Eff: {eff:.1f} / Th: {threshold:.1f} → {decision} {_decision_icon(decision)}",
        f"Signals: BOS {bos_icon}  Sweep {sweep_icon}  RSI {rsi_icon}",
        f"OB: {ob:.2f} {_tri_icon(ob, OB_IMBALANCE_BULL, OB_IMBALANCE_BEAR)}  Flow: {flow:.2f} {_tri_icon(flow, OB_FLOW_BULL, OB_FLOW_BEAR)}",
        f"Trend: {getattr(ps, 'trend', 'chop').capitalize()} · Mode: {(getattr(ps, 'dynamic_params', None) or {}).get('trade_mode', 'RANGING')}  |  {status}",
    ]
    return "```" + "\n" + "\n".join(lines) + "\n```"


def _cooldown_label(ps) -> str:
    """Return a human-readable cooldown remaining string for the watch block."""
    regime = getattr(ps, "regime", "ranging")
    cooldown_duration = (
        TRADE_COOLDOWN_TRENDING
        if regime in {"trending", "trending_volatile"}
        else TRADE_COOLDOWN_SECONDS
    )
    cd = _cooldown_remaining(getattr(ps, "last_trade_time", ""), cooldown_duration)
    mins, secs = cd // 60, cd % 60
    return f"cooldown {mins}m {secs}s" if mins > 0 else f"cooldown {secs}s"


def _failed_exit_cooldown_label(ps) -> str:
    """Return a human-readable failed-exit suppression label for the watch block."""
    _FAILED_SUPPRESS = {
        "EARLY_STAGNATION":   FAILED_EXIT_COOLDOWN_STAGNATION,
        "EARLY_STAGNATION_2": FAILED_EXIT_COOLDOWN_STAGNATION,
        "TRAIL_STOP":         FAILED_EXIT_COOLDOWN_TRAIL_STOP,
        "HARD_STOP":          FAILED_EXIT_COOLDOWN_HARD_STOP,
    }
    _SHORT_LABELS = {
        "EARLY_STAGNATION":   "stagnation",
        "EARLY_STAGNATION_2": "stagnation",
        "TRAIL_STOP":         "trail stop",
        "HARD_STOP":          "hard stop",
    }
    reason = getattr(ps, "last_failed_exit_reason", "")
    ftime  = getattr(ps, "last_failed_exit_time", "")
    suppress_s = _FAILED_SUPPRESS.get(reason, 0)
    remaining = _cooldown_remaining(ftime, suppress_s)
    label = _SHORT_LABELS.get(reason, "failed exit")
    mins, secs = remaining // 60, remaining % 60
    time_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
    return f"recent {label} · {time_str} left"


def _long_term_section(state) -> str:
    """Build the long-term portfolio section as a code block string.

    Shows capital split summary and per-symbol holding status.
    Called only when LONG_TERM_ENABLED is True.
    """
    total      = getattr(state, "portfolio_total_usdt", 0.0) or 1.0
    lt_budget  = getattr(state, "lt_capital_budget", 0.0) or 0.0
    lt_deployed = getattr(state, "lt_capital_deployed", 0.0) or 0.0
    lt_remaining = max(0.0, lt_budget - lt_deployed)
    trading_cap = total - lt_budget

    lines = [
        f"Budget    ${lt_budget:>10,.2f}  ({LONG_TERM_ALLOCATION*100:.0f}% of portfolio)",
        f"Deployed  ${lt_deployed:>10,.2f}  |  Free: ${lt_remaining:,.2f}",
        f"Trading   ${trading_cap:>10,.2f}  ({(1-LONG_TERM_ALLOCATION)*100:.0f}% of portfolio)",
        "",
    ]

    lt_states = getattr(state, "long_term_states", {})
    pairs_map = getattr(state, "pairs", {})

    for pair in LONG_TERM_ASSETS:
        lt_s = lt_states.get(pair)
        ps   = pairs_map.get(pair)
        base = pair.replace("USDT", "")

        if lt_s is None:
            lines.append(f"{base:<5}  ⚪ NOT CONFIGURED")
            lines.append("")
            continue

        cur_price = float(getattr(ps, "current_price", 0.0)) if ps else 0.0
        eff       = float(getattr(ps, "effective_score", 0.0)) if ps else 0.0
        trend     = getattr(ps, "trend", "chop") if ps else "unknown"
        dt_cycles = lt_s.consecutive_downtrend_cycles

        if lt_s.holding and lt_s.quantity > 0:
            value    = lt_s.quantity * cur_price if cur_price > 0 else 0.0
            cost     = lt_s.quantity * lt_s.entry_price if lt_s.entry_price > 0 else 0.0
            pl       = value - cost
            pl_pct   = (pl / cost * 100) if cost > 0 else 0.0
            pl_sign  = "+" if pl >= 0 else ""
            held     = _held_str(lt_s.entry_time)
            exit_warn = f"  ⚠ downtrend ×{dt_cycles}" if dt_cycles > 0 else ""
            lines += [
                f"{base:<5}  🟢 HOLDING{exit_warn}",
                f"  Entry  ${lt_s.entry_price:>12,.4f}",
                f"  Now    ${cur_price:>12,.4f}",
                f"  P/L    {pl_sign}{pl_pct:.2f}%  ({pl_sign}${pl:.2f})",
                f"  Held   {held}  |  Trend: {trend.capitalize()}",
                "",
            ]
        else:
            thr_gap = LONG_TERM_ENTRY_THRESHOLD - eff
            if thr_gap > 0:
                entry_status = f"need +{thr_gap:.1f}"
            else:
                entry_status = "READY ✓"
            lines += [
                f"{base:<5}  ⚪ WATCHING",
                f"  Score  {eff:.1f} / {LONG_TERM_ENTRY_THRESHOLD:.1f}  ({entry_status})",
                f"  Trend  {trend.capitalize()}",
                "",
            ]

    return "```\n" + "\n".join(lines).rstrip() + "\n```"


def _futures_block_label(block_reason: str) -> str:
    """Human-readable label for a futures entry block reason."""
    _LABELS = {
        "futures_disabled":           "module off",
        "short_disabled":             "SHORT off",
        "long_disabled":              "LONG off",
        "position_open":              "position open",
        "uneconomic_move":            "ATR < fee cost",
        "low_reward":                 "ATR < min reward",
        "reentry_cooldown":           "re-entry cooldown",
        "unstable_candle":            "unstable candle",
        "no_trend_alignment":         "not in downtrend",
        "weak_bearish_microstructure":"OB/flow not bearish",
        "no_downward_momentum":       "MACD not bearish",
        "unfavorable_regime":         "regime blocks entry",
        "no_bullish_trend":           "not in uptrend",
        "weak_bullish_microstructure":"OB/flow not bullish",
        "no_upward_momentum":         "MACD not bullish",
        "triple_long_risk":           "triple-long risk",
        "low_score":                  "score too low",
        "max_positions":              "max positions",
    }
    return _LABELS.get(block_reason, block_reason)


def _futures_paper_section(state) -> str:
    """Build the futures paper-trading section as a code block string.

    Shows session PnL summary, win rate, open positions, and per-pair
    block reasons for pairs currently not in a position.
    Called only when FUTURES_PAPER_ENABLED is True.
    """
    try:
        from futures_paper import (
            get_futures_open_positions, get_futures_unrealized_pnl,
            get_futures_entry_block_reason,
            _futures_bearish_score, _futures_bullish_score,
        )
        positions  = get_futures_open_positions(state)
        unrealized = get_futures_unrealized_pnl(state)
        _gate_available = True
    except Exception:
        positions  = []
        unrealized = 0.0
        _gate_available = False

    realized     = getattr(state, "futures_paper_realized_pnl", 0.0) or 0.0
    total_trades = getattr(state, "futures_paper_total_trades", 0) or 0
    wins         = getattr(state, "futures_paper_winning_trades", 0) or 0
    win_rate     = (wins / total_trades * 100) if total_trades > 0 else 0.0
    total_pnl    = realized + unrealized

    sides_enabled = []
    if FUTURES_SHORT_ENABLED:
        sides_enabled.append("SHORT")
    if FUTURES_LONG_ENABLED:
        sides_enabled.append("LONG")
    sides_str = "/".join(sides_enabled) if sides_enabled else "NONE"

    pnl_sign      = "+" if total_pnl >= 0 else ""
    realized_sign = "+" if realized >= 0 else ""
    unreal_sign   = "+" if unrealized >= 0 else ""

    open_pairs = {pos["pair"] for pos in positions}

    lines = [
        f"Mode       PAPER  |  {sides_str}  lev={FUTURES_MAX_LEVERAGE:.0f}×  sz=${FUTURES_POSITION_SIZE_USDT:.0f}",
        f"Total P/L  {pnl_sign}${total_pnl:.2f}  (realized {realized_sign}${realized:.2f} / unreal {unreal_sign}${unrealized:.2f})",
        f"Trades     {total_trades}  |  Win Rate {win_rate:.0f}%",
        f"Max Pos    {FUTURES_MAX_CONCURRENT_POSITIONS}  |  Open: {len(positions)}",
        "",
    ]

    # ── Open positions ──────────────────────────────────────────────────
    if positions:
        for pos in positions:
            pnl_pct   = pos["pnl_pct"]
            pl_usdt   = pos["pl_usdt"]
            icon      = "🟢" if pnl_pct >= 0 else "🔴"
            tp1_str   = "✅" if pos["tp1_hit"] else "—"
            trail     = pos["trailing_stop"]
            trail_str = f"${trail:,.4f}" if trail > 0 else "—"
            held_str  = f"{int(pos['held_min'])}m"
            funding   = pos.get("accrued_funding_usdt", 0.0)
            fund_str  = f"-${funding:.4f}" if funding > 0 else "none"
            net_usdt  = pl_usdt - funding
            lines += [
                f"{pos['base']:<6} {pos['side'].upper()}  {icon} {pnl_pct:+.2f}%  gross ${pl_usdt:+.2f}  net ${net_usdt:+.2f}",
                f"  Entry   ${pos['entry_price']:>12,.4f}  →  ${pos['current_price']:>12,.4f}",
                f"  Lev     {pos['leverage']:.0f}×  |  Notional ${pos['notional_usdt']:.0f}",
                f"  TP1     {tp1_str}  |  Trail {trail_str}  |  Held {held_str}",
                f"  Funding {fund_str}",
                "",
            ]

    # ── Watching pairs (not in a position) — show current block reason ──
    watching = [
        (pair, fp_s)
        for pair, fp_s in getattr(state, "futures_states", {}).items()
        if pair not in open_pairs
    ]
    if watching:
        lines.append("Watching:")
        for pair, fp_s in watching:
            base = pair.replace("USDT", "")
            if _gate_available:
                ps = state.pairs.get(pair)
                if ps is not None:
                    try:
                        short_score = _futures_bearish_score(ps)
                        short_block = get_futures_entry_block_reason(ps, fp_s, "short", short_score)
                        long_score  = _futures_bullish_score(ps)
                        long_block  = get_futures_entry_block_reason(ps, fp_s, "long",  long_score)
                        if not short_block:
                            block_str = "SHORT READY ✓"
                            score_str = f"{short_score:.1f}"
                        elif not long_block:
                            block_str = "LONG READY ✓"
                            score_str = f"{long_score:.1f}"
                        else:
                            # Show the less-blocked direction (prefer SHORT)
                            block_str = f"S:{_futures_block_label(short_block)}"
                            score_str = f"{short_score:.1f}"
                        lines.append(f"  {base:<6} ⚪  {block_str}  (score {score_str})")
                    except Exception:
                        lines.append(f"  {base:<6} ⚪  —")
                else:
                    lines.append(f"  {base:<6} ⚪  no data")
            else:
                lines.append(f"  {base:<6} ⚪  —")

    if not positions and not watching:
        lines.append("No pairs configured")

    return "```\n" + "\n".join(lines).rstrip() + "\n```"


def build_main_panel(state) -> discord.Embed:
    """Main dashboard embed.

    Embed color = portfolio/risk state (green → yellow → orange → red → gray).
    Title carries the same state badge so it's visible even in notification previews.
    Description adds a one-line summary so the user can scan health without reading
    all sections.
    """
    color  = _portfolio_color(state)
    status = _bot_status_label(state)
    mode   = "TESTNET" if USE_TESTNET else "LIVE"
    mode_badge = "🧪" if USE_TESTNET else "🔴"
    now_unix = int(datetime.now(timezone.utc).timestamp())
    uptime = _uptime(state.bot_uptime_start)

    # One-line health summary appended to description for quick scanning
    pl_sign = "+" if (getattr(state, "portfolio_pl_usdt", 0.0) or 0.0) >= 0 else ""
    pl_pct  = getattr(state, "portfolio_pl_pct", 0.0) or 0.0
    pl_usdt = getattr(state, "portfolio_pl_usdt", 0.0) or 0.0
    n_pos   = getattr(state, "open_positions_count", 0)
    cur_val = getattr(state, "portfolio_current_value", 0.0) or 0.0
    summary = (
        f"**${cur_val:,.0f}**  ·  P/L {pl_sign}${pl_usdt:,.2f} ({pl_sign}{pl_pct:.2f}%)"
        f"  ·  {n_pos} position{'s' if n_pos != 1 else ''} open"
    )

    embed = discord.Embed(
        title=f"🤖 Trading Bot  ·  {status}",
        description=(
            f"{mode_badge} {mode}  ·  Updated <t:{now_unix}:t>  ·  Uptime {uptime}\n"
            f"{summary}"
        ),
        color=color,
    )

    all_ps = list(state.pairs.values())
    trades_24h, win_rate_24h = _get_24h_stats()

    avg_eff = (sum(_effective_score(ps) for ps in all_ps) / len(all_ps)) if all_ps else 0.0
    avg_struct = (sum(float(getattr(ps, "structure_score", 0.0)) for ps in all_ps) / len(all_ps)) if all_ps else 0.0
    bias = "Long ↗" if avg_struct > 0.1 else ("Short ↘" if avg_struct < -0.1 else "Neutral ↔")

    from logger import get_trade_stats
    stats = get_trade_stats()

    # BOT DECISION section title reflects risk mode
    if state.is_paused:
        decision_title = "🧠 BOT DECISION  ·  ⏸ PAUSED"
    elif getattr(state, "btc_crash_active", False):
        decision_title = "🧠 BOT DECISION  ·  🔴 CRASH MODE"
    elif getattr(state, "in_drawdown", False):
        decision_title = "🧠 BOT DECISION  ·  ⚠️ DEFENSIVE"
    else:
        decision_title = "🧠 BOT DECISION"

    risk_mode = "DEFENSIVE ⚠️" if state.in_drawdown else "NORMAL"
    bot_lines = [
        f"Mode       {risk_mode}",
        f"Bias       {bias}",
        f"Avg Eff.   {avg_eff:.1f}",
        f"Trades24h  {trades_24h}",
        f"Win Rate   {win_rate_24h:.0f}%",
        f"All Time   {stats['sells']} trades",
    ]
    embed.add_field(
        name=decision_title,
        value="```" + "\n" + "\n".join(bot_lines) + "\n```",
        inline=False,
    )

    # PORTFOLIO section title carries P/L direction at a glance
    if pl_pct > 0:
        portfolio_title = f"💼 PORTFOLIO  ·  🟢 {pl_sign}{pl_pct:.2f}%"
    elif pl_pct < -1.0:
        portfolio_title = f"💼 PORTFOLIO  ·  🔴 {pl_pct:.2f}%"
    elif pl_pct < 0:
        portfolio_title = f"💼 PORTFOLIO  ·  🟡 {pl_pct:.2f}%"
    else:
        portfolio_title = "💼 PORTFOLIO"

    peak = state.portfolio_peak_value or state.portfolio_start_value or state.portfolio_current_value
    dd_usdt = max(0.0, peak - state.portfolio_current_value)
    dd_pct = (dd_usdt / peak * 100) if peak > 0 else 0.0
    spendable = max(0.0, state.usdt_balance - state.usdt_reserved)

    lt_budget   = getattr(state, "lt_capital_budget", 0.0) or 0.0
    lt_deployed = getattr(state, "lt_capital_deployed", 0.0) or 0.0
    if LONG_TERM_ENABLED and lt_budget > 0:
        cap_split_line = (
            f"Cap Split  LT {LONG_TERM_ALLOCATION*100:.0f}% / "
            f"Trade {(1-LONG_TERM_ALLOCATION)*100:.0f}%"
        )
    else:
        cap_split_line = ""

    perf_lines = [
        f"Value      ${state.portfolio_current_value:,.2f}",
        f"Start      ${state.portfolio_start_value:,.2f}",
        f"P/L        {_pl_display(state.portfolio_pl_usdt, state.portfolio_pl_pct)}",
        f"Drawdown   -${dd_usdt:,.2f} (-{dd_pct:.2f}%)",
        f"USDT Cash  ${state.usdt_balance:,.2f}",
        f"Spendable  ${spendable:,.2f}  (rsv {PORTFOLIO_RESERVE_PCT*100:.0f}%)",
        f"Positions  {state.open_positions_count} open",
    ]
    if cap_split_line:
        perf_lines.append("")
        perf_lines.append(cap_split_line)
    perf_lines += [
        "",
        f"Total Gain  +${stats['total_gain']:,.2f}",
        f"Total Loss   ${stats['total_loss']:,.2f}",
    ]
    embed.add_field(
        name=portfolio_title,
        value="```" + "\n" + "\n".join(perf_lines) + "\n```",
        inline=False,
    )

    btc_assets = {"ETH", "BTC", "BCH"}
    alt_assets = {"BNB", "SUI", "XRP"}
    total = state.portfolio_current_value or 1.0
    btc_val = sum(ps.position_value_usdt for ps in all_ps if ps.base_asset in btc_assets)
    alt_val = sum(ps.position_value_usdt for ps in all_ps if ps.base_asset in alt_assets)
    cash_val = max(0.0, state.usdt_balance)
    exposure_lines = [
        f"BTC Cluster  {btc_val / total * 100:.0f}%",
        f"Alt Cluster  {alt_val / total * 100:.0f}%",
        f"Cash         {cash_val / total * 100:.0f}%",
        "",
        f"Drawdown  {state.portfolio_pl_pct:+.2f}%",
        f"Mode      {risk_mode}",
    ]
    embed.add_field(
        name="⚠️ EXPOSURE",
        value="```" + "\n" + "\n".join(exposure_lines) + "\n```",
        inline=False,
    )

    held_pairs = [ps for ps in all_ps if ps.asset_balance > 0]
    for ps in held_pairs:
        entry = ps.buy_price if ps.buy_price > 0 else 0.0
        value = ps.asset_balance * ps.current_price
        cost  = ps.asset_balance * entry if entry > 0 else 0.0
        live_pl     = value - cost if cost > 0 else 0.0
        live_pl_pct = (live_pl / cost * 100) if cost > 0 else 0.0
        eff       = _effective_score(ps)
        threshold = _effective_threshold(ps)
        held      = _held_str(getattr(ps, "position_open_time", ""))
        tp1_target = ps.dynamic_params.get("tp1", BASE_TP1_PCT)
        tp1    = "✅ hit" if getattr(ps, "tp1_hit", False) else f"{tp1_target:.2f}%"
        hard_sl = entry * (1 - STOP_LOSS_PCT / 100) if entry > 0 else 0.0
        trail   = getattr(ps, "trailing_stop", 0.0)
        atr_tp  = getattr(ps, "atr_take_profit", 0.0)
        ob      = getattr(ps, "orderbook_imbalance", 0.5)
        flow    = getattr(ps, "flow_ratio", 0.5)

        bos_icon   = _bool_icon(getattr(ps, "bos", False))
        sweep_icon = "✓" if getattr(ps, "sweep_type", "none") != "none" or getattr(ps, "liquidity_sweep_score", 0.0) > 0 else "✗"
        rsi_icon   = _bool_icon(getattr(ps, "rsi", 50.0) < RSI_BUY_THRESHOLD)

        reasons = []
        if bos_icon == "✓":   reasons.append("BOS")
        if sweep_icon == "✓": reasons.append("Sweep")
        if rsi_icon == "✓":   reasons.append("RSI")
        if ob > OB_IMBALANCE_BULL:  reasons.append("OB")
        if flow > OB_FLOW_BULL:     reasons.append("Flow")
        reason_str = " + ".join(reasons[:4]) if reasons else "None"

        # Field name includes live P/L so user can scan positions without opening the block
        if live_pl > 0.005:
            pos_title = f"📈 {ps.base_asset}  ·  🟢 +{live_pl_pct:.2f}%  (+${live_pl:.2f})"
        elif live_pl < -0.005:
            pos_title = f"📈 {ps.base_asset}  ·  🔴 {live_pl_pct:.2f}%  (-${abs(live_pl):.2f})"
        else:
            pos_title = f"📈 {ps.base_asset}  ·  ⚪ ±0.00%"

        lines = [
            f"{ps.base_asset}/USDT — {_format_price(ps.current_price)}",
            f"Regime: {str(getattr(ps, 'regime', 'normal')).replace('_', ' ').title()} | Eff: {eff:.1f} / Th: {threshold:.1f} → {ps.decision} {_decision_icon(ps.decision)}",
            f"Signals: BOS {bos_icon}  Sweep {sweep_icon}  RSI {rsi_icon}",
            f"OB: {ob:.2f} {_tri_icon(ob, OB_IMBALANCE_BULL, OB_IMBALANCE_BEAR)}  Flow: {flow:.2f} {_tri_icon(flow, OB_FLOW_BULL, OB_FLOW_BEAR)}",
            f"Trend: {getattr(ps, 'trend', 'chop').capitalize()} · Mode: {(getattr(ps, 'dynamic_params', None) or {}).get('trade_mode', 'RANGING')}  |  Held: {held}",
            "",
            f"Qty:    {ps.asset_balance:.6f} {ps.base_asset}",
            f"Entry:  {_format_price(entry) if entry > 0 else '-'}",
            f"Value:  ${value:,.2f}",
            f"P/L:    {live_pl_pct:+.2f}% ({live_pl:+.2f} USDT)",
            f"TP1:    {tp1}",
            f"SL:     {_format_price(hard_sl) if hard_sl > 0 else '-'}",
            f"Trail:  {_format_price(trail) if trail > 0 else '-'}",
            f"ATR TP: {_format_price(atr_tp) if atr_tp > 0 else '-'}",
            f"Reason: {reason_str}",
        ]
        embed.add_field(
            name=pos_title,
            value="```" + "\n" + "\n".join(lines) + "\n```",
            inline=False,
        )

    watch_pairs = [ps for ps in all_ps if ps.asset_balance <= 0]
    if watch_pairs:
        watch_value = "\n".join(_watch_block(ps, state) for ps in watch_pairs)
        if len(watch_value) <= 1024:
            embed.add_field(name="📊 WATCHING", value=watch_value, inline=False)
        else:
            current = ""
            chunk_idx = 1
            for ps in watch_pairs:
                block = _watch_block(ps, state)
                candidate = current + ("\n" if current else "") + block
                if len(candidate) > 1024 and current:
                    embed.add_field(
                        name=f"📊 WATCHING ({chunk_idx})",
                        value=current,
                        inline=False,
                    )
                    chunk_idx += 1
                    current = block
                else:
                    current = candidate
            if current:
                embed.add_field(
                    name=f"📊 WATCHING ({chunk_idx})" if chunk_idx > 1 else "📊 WATCHING",
                    value=current,
                    inline=False,
                )

    # ── Long-Term Portfolio section ───────────────────────────────────────
    if LONG_TERM_ENABLED:
        lt_value = _long_term_section(state)
        embed.add_field(
            name="📦 LONG-TERM PORTFOLIO",
            value=lt_value,
            inline=False,
        )

    # ── Futures Paper-Trading section ─────────────────────────────────────
    if FUTURES_PAPER_ENABLED:
        fp_value = _futures_paper_section(state)
        embed.add_field(
            name="📉 FUTURES PAPER TRADING",
            value=fp_value,
            inline=False,
        )

    market_vol, avg_atr_pct, trend_bias = _market_state(all_ps)
    market_lines = [
        f"Volatility  {market_vol}",
        f"Avg ATR     {avg_atr_pct:.2f}%",
        f"Trend Bias  {trend_bias}",
    ]
    embed.add_field(
        name="🌍 MARKET STATE",
        value="```" + "\n" + "\n".join(market_lines) + "\n```",
        inline=False,
    )

    return embed


def build_signals_embed(pair_state, bot_state) -> discord.Embed:
    eff = _effective_score(pair_state)
    threshold = _effective_threshold(pair_state)
    ob = getattr(pair_state, "orderbook_imbalance", 0.5)
    flow = getattr(pair_state, "flow_ratio", 0.5)
    embed = discord.Embed(
        title=f"📊 {pair_state.base_asset}/USDT Signals",
        color=COLOR_BLUE,
    )
    embed.description = (
        "```"
        f"\nRSI            {pair_state.rsi:.2f}"
        f"\nMACD           {pair_state.macd_crossover}"
        f"\nBollinger      {pair_state.bb_position}"
        f"\nTrend          {pair_state.trend}"
        f"\nRegime         {pair_state.regime}"
        f"\nATR %          {pair_state.atr_pct * 100:.2f}%"
        f"\nOB             {ob:.2f}"
        f"\nFlow           {flow:.2f}"
        f"\nConfidence     {pair_state.confidence_weighted:.2f}"
        f"\nEffective      {eff:.2f}"
        f"\nThreshold      {threshold:.2f}"
        f"\nDecision       {pair_state.decision}"
        "\n```"
    )
    return embed


def build_portfolio_embed(state) -> discord.Embed:
    color = _portfolio_color(state)
    embed = discord.Embed(title="💼 Portfolio Breakdown", color=color)
    embed.add_field(
        name="Overview",
        value=(
            "```"
            f"\nCurrent     ${state.portfolio_current_value:,.2f}"
            f"\nStart       ${state.portfolio_start_value:,.2f}"
            f"\nP/L         {_pl_display(state.portfolio_pl_usdt, state.portfolio_pl_pct)}"
            f"\nUSDT Cash   ${state.usdt_balance:,.2f}"
            f"\nSpendable   ${max(0.0, state.usdt_balance - state.usdt_reserved):,.2f}"
            f"\nReserve tgt ${state.usdt_reserved:,.2f} ({PORTFOLIO_RESERVE_PCT*100:.0f}%)"
            f"\nOpen Pos    {state.open_positions_count}"
            "\n```"
        ),
        inline=False,
    )
    for ps in state.pairs.values():
        embed.add_field(
            name=ps.base_asset,
            value=(
                "```"
                f"\nQty     {ps.asset_balance:.6f}"
                f"\nEntry   {_format_price(ps.buy_price) if ps.buy_price > 0 else '-'}"
                f"\nNow     {_format_price(ps.current_price)}"
                f"\nValue   ${ps.position_value_usdt:,.2f}"
                "\n```"
            ),
            inline=True,
        )
    return embed


def _ts_to_unix(ts_str: str) -> int | None:
    """Convert an ISO timestamp string to a Unix epoch integer, or None on failure."""
    try:
        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return int(ts.timestamp())
    except Exception:
        return None


def build_trades_embed(trades: list[dict], title: str = "📜 Recent Trades") -> discord.Embed:
    """Recent trade history embed.

    Uses Discord's native <t:unix:t> and <t:unix:R> timestamp tags so every
    timestamp renders in the viewer's local timezone automatically.
    Code blocks are intentionally avoided because they prevent <t:> rendering.
    Embed color reflects net P/L of the last 5 closed trades for quick scanning.

    title: optional override for the embed title (used by filtered views).
    """
    from logger import get_trade_stats

    # Color the trades embed by recent performance
    sell_set = set(_sell_actions())
    recent_pl = sum(
        float(t.get("pl_usdt", 0.0) or 0.0)
        for t in trades[:10]
        if t.get("action", "") in sell_set
    )
    if recent_pl > 0:
        embed_color = COLOR_GREEN
    elif recent_pl < 0:
        embed_color = COLOR_RED
    else:
        embed_color = COLOR_BLUE

    embed = discord.Embed(title=title, color=embed_color)
    if not trades:
        embed.description = "No trades recorded yet."
        return embed

    lines = []

    for t in trades[:15]:
        action     = t.get("action", "")
        pair       = t.get("pair", "").replace("USDT", "")
        usdt       = float(t.get("amount_usdt", 0.0) or 0.0)
        pl         = float(t.get("pl_usdt", 0.0) or 0.0)
        exit_unix  = _ts_to_unix(t.get("timestamp", ""))
        entry_unix = _ts_to_unix(t.get("entry_time", ""))

        is_sell = action in sell_set
        if is_sell:
            if pl > 0:
                icon   = "🟢"
                pl_str = f"  **+${pl:.2f}**"
            elif pl < 0:
                icon   = "🔴"
                pl_str = f"  **-${abs(pl):.2f}**"
            else:
                icon   = "⚪"
                pl_str = "  $0.00"

            # Show entry → exit times for sell rows
            if entry_unix and exit_unix:
                time_str = f"<t:{entry_unix}:t> → <t:{exit_unix}:t> *(<t:{exit_unix}:R>)*"
            elif exit_unix:
                time_str = f"<t:{exit_unix}:t> *(<t:{exit_unix}:R>)*"
            else:
                time_str = "?"
        else:
            icon   = "🔵"
            pl_str = ""
            # Buy rows: just show when the entry happened
            if exit_unix:
                time_str = f"<t:{exit_unix}:t> *(<t:{exit_unix}:R>)*"
            else:
                time_str = "?"

        lines.append(
            f"{icon} `{action}` **{pair}** ${usdt:,.0f}{pl_str} · {time_str}"
        )

    embed.description = "\n".join(lines)

    # Footer: today's count + all-time total
    try:
        stats = get_trade_stats()
        lifetime = stats.get("sells", 0)
        trades_today, _ = _get_24h_stats()
        embed.set_footer(text=f"Today: {trades_today} trades  ·  All-time: {lifetime} closed trades")
    except Exception:
        pass

    return embed


def build_config_embed(state) -> discord.Embed:
    embed = discord.Embed(title="⚙️ Bot Config", color=COLOR_GRAY)
    embed.description = (
        "```"
        f"\nMode                {'TESTNET' if USE_TESTNET else 'LIVE'}"
        f"\nReserve %           {PORTFOLIO_RESERVE_PCT * 100:.0f}%"
        f"\nMax positions       {PORTFOLIO_MAX_POSITIONS}"
        f"\nRSI Buy/Sell        {RSI_BUY_THRESHOLD} / {RSI_SELL_THRESHOLD}"
        f"\nStop Loss           {STOP_LOSS_PCT}%"
        f"\nProfit Target       {PROFIT_TARGET_PCT}%"
        f"\nConfidence Buy      {CONFIDENCE_BUY_THRESHOLD}"
        f"\nTrade Size %        {TRADE_SIZE_PCT * 100:.0f}%"
        f"\nMax Position %      {MAX_POSITION_PCT * 100:.0f}%"
        f"\nRSI Interval        {INTERVAL_RSI}s"
        f"\nEtherscan Interval  {INTERVAL_ETHERSCAN}s"
        f"\nDiscord Panel Int   {INTERVAL_DISCORD_PANEL}s"
        f"\nRebalance Int       {PORTFOLIO_REBALANCE_INTERVAL}s"
        "\n```"
    )
    return embed


def build_exit_analytics_embed() -> discord.Embed:
    """Show per-action exit breakdown: count, win rate, avg P/L, total P/L."""
    from logger import get_exit_analytics
    data = get_exit_analytics()
    embed = discord.Embed(title="📊 Exit Analytics", color=COLOR_BLUE)

    total = data.get("total_exits", 0)
    by_action = data.get("by_action", {})

    if not by_action:
        embed.description = "No exit trades recorded yet."
        return embed

    lines = [f"{'Action':<16} {'N':>4} {'Win%':>6} {'AvgPL':>8} {'TotalPL':>10}"]
    lines.append("-" * 50)
    for action, row in sorted(by_action.items(), key=lambda x: -x[1]["count"]):
        win_rate = (row["wins"] / row["count"] * 100) if row["count"] > 0 else 0.0
        lines.append(
            f"{action:<16} {row['count']:>4} {win_rate:>5.0f}% {row['avg_pl']:>+8.2f} {row['total_pl']:>+10.2f}"
        )
    lines.append("-" * 50)
    lines.append(f"{'TOTAL':<16} {total:>4}")

    embed.description = "```" + "\n" + "\n".join(lines) + "\n```"
    return embed


# ─── Multi-channel panel builders ────────────────────────────────────────────


def build_audit_panel(state) -> discord.Embed:
    """AUDIT channel — per-pair entry diagnostic (why the bot isn't trading).

    Keeps diagnostic noise completely out of the MAIN terminal so the owner
    can open #terminal-audit when the bot is silent and see exactly which gate
    is blocking each pair, without reading through positions or equity data.
    """
    now_unix = int(datetime.now(timezone.utc).timestamp())
    embed = discord.Embed(
        title="🔍 ENTRY AUDIT — Block Reasons",
        description=f"Updated <t:{now_unix}:t>  ·  Why entries are not firing",
        color=COLOR_BLUE,
    )

    all_ps = list(state.pairs.values())

    # ── Spot scalp entry blocks ───────────────────────────────────────────────
    spot_lines = []
    for ps in sorted(all_ps, key=lambda p: p.base_asset):
        block  = _entry_block_reason(ps, state)
        eff    = _effective_score(ps)
        thr    = _effective_threshold(ps)
        trend  = getattr(ps, "trend", "?")
        regime = str(getattr(ps, "regime", "?")).replace("_", " ")

        if block is None and getattr(ps, "decision", "HOLD") == "BUY":
            status = "READY ✓"
        elif block:
            _SPOT_BLOCK_LABELS = {
                "add_blocked":            "avg-down blocked",
                "cooldown":               _cooldown_label(ps),
                "failed_exit_cooldown":   _failed_exit_cooldown_label(ps),
                "spike_filter":           "price spike",
                "unstable_candle":        "volatile candle",
                "uneconomic_move":        "ATR < fee cost",
                "spread_filter":          "wide spread",
                "insufficient_spendable": "no capital",
                "position_maxed":         "position maxed",
                "min_notional":           "order too small",
                "fvg_cooldown":           "FVG cooldown",
            }
            status = _SPOT_BLOCK_LABELS.get(block, block)
        else:
            gap = thr - eff
            status = f"need +{gap:.1f} score"

        spot_lines.append(
            f"{ps.base_asset:<6} {trend:<10} {regime:<18}  "
            f"Eff:{eff:.1f}/{thr:.1f}  {status}"
        )

    embed.add_field(
        name="⚡ SPOT SCALP BLOCKS",
        value="```\n" + "\n".join(spot_lines) + "\n```",
        inline=False,
    )

    # ── Futures entry blocks ──────────────────────────────────────────────────
    if FUTURES_PAPER_ENABLED:
        try:
            from futures_paper import (
                get_futures_entry_block_reason,
                _futures_bearish_score,
                _futures_bullish_score,
            )
            futures_lines = []
            for pair, fp_s in sorted(getattr(state, "futures_states", {}).items()):
                base = pair.replace("USDT", "")
                if fp_s.is_open:
                    pnl_rough = "open"
                    futures_lines.append(f"  {base:<6} ▶ POSITION OPEN ({fp_s.side.upper()})")
                    continue
                ps = state.pairs.get(pair)
                if ps is None:
                    futures_lines.append(f"  {base:<6} ⚪ no pair state")
                    continue
                try:
                    s_score = _futures_bearish_score(ps)
                    s_block = get_futures_entry_block_reason(ps, fp_s, "short", s_score)
                    l_score = _futures_bullish_score(ps)
                    l_block = get_futures_entry_block_reason(ps, fp_s, "long",  l_score)

                    if not s_block:
                        futures_lines.append(
                            f"  {base:<6} 🔴 SHORT READY ✓  score={s_score:.1f}"
                        )
                    elif not l_block:
                        futures_lines.append(
                            f"  {base:<6} 🟢 LONG READY ✓   score={l_score:.1f}"
                        )
                    else:
                        s_lbl = _futures_block_label(s_block)
                        l_lbl = _futures_block_label(l_block)
                        futures_lines.append(
                            f"  {base:<6} ⚪ S:{s_lbl:<24} L:{l_lbl}"
                        )
                except Exception:
                    futures_lines.append(f"  {base:<6} ⚪ —")

            if futures_lines:
                embed.add_field(
                    name="📉 FUTURES PAPER BLOCKS",
                    value="```\n" + "\n".join(futures_lines) + "\n```",
                    inline=False,
                )
        except Exception:
            pass

    return embed


def build_equity_panel(state) -> discord.Embed:
    """EQUITY channel — PnL accounting, funding costs, win-rate breakdown.

    Completely separated from positions / diagnostics so the owner has a
    single channel that answers "how much money did I make / lose today?".
    """
    now_unix = int(datetime.now(timezone.utc).timestamp())

    from logger import get_trade_stats
    stats       = get_trade_stats()
    trades_24h, win_rate_24h = _get_24h_stats()

    pl_pct  = getattr(state, "portfolio_pl_pct",  0.0) or 0.0
    pl_usdt = getattr(state, "portfolio_pl_usdt", 0.0) or 0.0
    color   = COLOR_GREEN if pl_pct >= 0 else (COLOR_ORANGE if pl_pct > -2.0 else COLOR_RED)

    embed = discord.Embed(
        title="💰 EQUITY — PnL Accounting",
        description=f"Updated <t:{now_unix}:t>  ·  Net P/L this session: {'+' if pl_usdt >= 0 else ''}${pl_usdt:.2f}",
        color=color,
    )

    # ── Spot scalp accounting ─────────────────────────────────────────────────
    spot_lines = [
        f"Portfolio    ${getattr(state, 'portfolio_current_value', 0.0):,.2f}",
        f"Start Value  ${getattr(state, 'portfolio_start_value',   0.0):,.2f}",
        f"Session P/L  {_pl_display(pl_usdt, pl_pct)}",
        "",
        f"Trades 24h   {trades_24h}   Win {win_rate_24h:.0f}%",
        f"All Closed   {stats['sells']}   Wins {stats['profitable']}",
        f"Total Gain   +${stats['total_gain']:,.2f}",
        f"Total Loss    ${stats['total_loss']:,.2f}",
    ]
    embed.add_field(
        name="⚡ SPOT SCALP",
        value="```\n" + "\n".join(spot_lines) + "\n```",
        inline=False,
    )

    # ── Futures paper accounting ──────────────────────────────────────────────
    if FUTURES_PAPER_ENABLED:
        realized     = getattr(state, "futures_paper_realized_pnl",    0.0) or 0.0
        total_ftrades = getattr(state, "futures_paper_total_trades",   0)   or 0
        wins_ftrades  = getattr(state, "futures_paper_winning_trades", 0)   or 0
        win_rate_f    = (wins_ftrades / total_ftrades * 100) if total_ftrades > 0 else 0.0

        try:
            from futures_paper import get_futures_open_positions, get_futures_unrealized_pnl
            unrealized = get_futures_unrealized_pnl(state)
            positions  = get_futures_open_positions(state)
        except Exception:
            unrealized = 0.0
            positions  = []

        total_f_pnl = realized + unrealized
        r_sign = "+" if realized    >= 0 else ""
        u_sign = "+" if unrealized  >= 0 else ""
        t_sign = "+" if total_f_pnl >= 0 else ""

        f_lines = [
            f"Total P/L    {t_sign}${total_f_pnl:.2f}",
            f"  Realized   {r_sign}${realized:.2f}",
            f"  Unrealized {u_sign}${unrealized:.2f}",
            f"Trades       {total_ftrades}   Win Rate {win_rate_f:.0f}%",
        ]

        # Per-position funding breakdown
        if positions:
            f_lines.append("")
            f_lines.append("Open positions:")
            for pos in positions:
                funding  = pos.get("accrued_funding_usdt", 0.0)
                pnl_pct  = pos["pnl_pct"]
                icon     = "🟢" if pnl_pct >= 0 else "🔴"
                fund_str = f"-${funding:.4f}" if funding > 0 else "none"
                net_usdt = pos["pl_usdt"] - funding
                f_lines.append(
                    f"  {pos['base']:<6} {pos['side'].upper()} {icon} "
                    f"{pnl_pct:+.2f}%  net ${net_usdt:+.2f}  "
                    f"funding {fund_str}  held {int(pos['held_min'])}m"
                )

        embed.add_field(
            name="📉 FUTURES PAPER",
            value="```\n" + "\n".join(f_lines) + "\n```",
            inline=False,
        )

    # ── Long-term P/L ────────────────────────────────────────────────────────
    if LONG_TERM_ENABLED:
        lt_lines = []
        for pair in LONG_TERM_ASSETS:
            lt_s = getattr(state, "long_term_states", {}).get(pair)
            ps   = state.pairs.get(pair)
            base = pair.replace("USDT", "")
            if lt_s and lt_s.holding and lt_s.quantity > 0 and ps:
                cur     = float(getattr(ps, "current_price", 0.0))
                cost    = lt_s.quantity * lt_s.entry_price
                value   = lt_s.quantity * cur if cur > 0 else 0.0
                lt_pl   = value - cost
                lt_pct  = (lt_pl / cost * 100) if cost > 0 else 0.0
                sign    = "+" if lt_pl >= 0 else ""
                held    = _held_str(lt_s.entry_time)
                lt_lines.append(
                    f"  {base:<6} HOLD  {sign}{lt_pct:.2f}%  ({sign}${lt_pl:.2f})"
                    f"  held {held}"
                )
            else:
                lt_lines.append(f"  {base:<6} WATCHING")

        if lt_lines:
            embed.add_field(
                name="📦 LONG-TERM POSITIONS",
                value="```\n" + "\n".join(lt_lines) + "\n```",
                inline=False,
            )

    return embed


def build_admin_panel(state) -> discord.Embed:
    """ADMIN channel — system health and master override controls.

    Intentionally light — no positions, no block reasons.
    Shows uptime, mode, risk state, and session summary so the admin can
    confirm the bot is alive and healthy without digging into MAIN.
    """
    now_unix   = int(datetime.now(timezone.utc).timestamp())
    mode       = "TESTNET" if USE_TESTNET else "LIVE"
    mode_badge = "🧪" if USE_TESTNET else "🔴"
    uptime     = _uptime(state.bot_uptime_start)
    status     = _bot_status_label(state)

    color = (
        COLOR_GRAY   if state.is_paused else
        COLOR_RED    if getattr(state, "btc_crash_active", False) else
        COLOR_ORANGE if getattr(state, "in_drawdown", False) else
        COLOR_BLUE
    )

    embed = discord.Embed(
        title=f"⚙️ ADMIN — System Health  ·  {status}",
        description=(
            f"{mode_badge} {mode}  ·  Updated <t:{now_unix}:t>  ·  Uptime {uptime}"
        ),
        color=color,
    )

    btc_chg  = getattr(state, "btc_15m_change_pct", 0.0) or 0.0
    risk_mode = "DEFENSIVE ⚠️" if state.in_drawdown else "NORMAL"
    crash_str = "ACTIVE 🔴" if getattr(state, "btc_crash_active", False) else "Clear ✓"

    sys_lines = [
        f"Status     {status}",
        f"Mode       {mode}",
        f"Risk Mode  {risk_mode}",
        f"BTC Crash  {crash_str}",
        f"BTC 15m    {btc_chg:+.2f}%",
        f"Paused     {'YES ⏸' if state.is_paused else 'No'}",
        f"Drawdown   {getattr(state, 'drawdown_pct', 0.0):.2f}%",
        f"Open Pos   {getattr(state, 'open_positions_count', 0)}",
        f"USDT Cash  ${getattr(state, 'usdt_balance', 0.0):,.2f}",
    ]
    embed.add_field(
        name="🔧 SYSTEM STATUS",
        value="```\n" + "\n".join(sys_lines) + "\n```",
        inline=False,
    )

    from logger import get_trade_stats
    stats       = get_trade_stats()
    trades_24h, win_rate_24h = _get_24h_stats()

    pl_pct  = getattr(state, "portfolio_pl_pct",  0.0) or 0.0
    pl_usdt = getattr(state, "portfolio_pl_usdt", 0.0) or 0.0

    perf_lines = [
        f"Portfolio  ${getattr(state, 'portfolio_current_value', 0.0):,.2f}",
        f"P/L        {_pl_display(pl_usdt, pl_pct)}",
        f"24h Trades {trades_24h}  |  Win {win_rate_24h:.0f}%",
        f"All-Time   {stats['sells']} closed  |  "
        f"{stats['profitable']}W / {stats['sells'] - stats['profitable']}L",
    ]
    embed.add_field(
        name="📊 SESSION SUMMARY",
        value="```\n" + "\n".join(perf_lines) + "\n```",
        inline=False,
    )

    return embed


# Exit action → color mapping for trade notifications
_NOTIFICATION_COLORS = {
    "BUY":          COLOR_GREEN,
    "BUY_FVG":      COLOR_GREEN,
    # Profitable exits — colored green at call site when pl > 0
    "SIGNAL_SELL":  COLOR_GREEN,
    "TP1_FULL_EXIT":COLOR_GREEN,
    "PEAK_RETRACE": COLOR_GREEN,
    # Controlled / time-based exits — orange (not panic, not profit)
    "TIME_STOP":       COLOR_ORANGE,
    "MAX_HOLD_EXIT":   COLOR_ORANGE,
    "EARLY_FAILURE":   COLOR_ORANGE,
    "EARLY_STAGNATION":COLOR_ORANGE,
    "EARLY_STAGNATION_2": COLOR_ORANGE,
    "LOW_MOMENTUM_EXIT":  COLOR_ORANGE,
    # Hard risk exits — red
    "HARD_STOP":    COLOR_DARK_RED,
    "TRAIL_STOP":   COLOR_DARK_RED,
    "STOP_LOSS":    COLOR_DARK_RED,
    "FORCE SELL":   COLOR_RED,
}


def build_trade_notification(action, pair, ps, state, pl=0):
    """Trade notification embed.

    Color logic:
    - BUY always green
    - Sells: green if profitable, dark-red for hard/trail stops,
      orange for time/stagnation exits, red otherwise
    - Action-specific defaults from _NOTIFICATION_COLORS (overridden by actual P/L)
    """
    is_sell = action not in ("BUY", "BUY_FVG")

    if is_sell:
        if pl > 0:
            # Actual profit regardless of exit type
            color = COLOR_GREEN
        elif action in ("HARD_STOP", "TRAIL_STOP", "STOP_LOSS"):
            color = COLOR_DARK_RED
        elif action in ("TIME_STOP", "MAX_HOLD_EXIT", "EARLY_FAILURE",
                        "EARLY_STAGNATION", "EARLY_STAGNATION_2", "LOW_MOMENTUM_EXIT"):
            color = COLOR_ORANGE
        elif action == "FORCE SELL":
            color = COLOR_RED
        else:
            color = COLOR_RED
    else:
        color = COLOR_GREEN

    # Action badge: add a quick label so the notification is scannable
    _ACTION_BADGES = {
        "BUY":              "🔵 BUY",
        "BUY_FVG":          "🔵 BUY (FVG)",
        "SIGNAL_SELL":      "🟢 SIGNAL SELL" if pl > 0 else "🔴 SIGNAL SELL",
        "HARD_STOP":        "🛑 HARD STOP",
        "TRAIL_STOP":       "🛑 TRAIL STOP",
        "TIME_STOP":        "⏱ TIME STOP",
        "MAX_HOLD_EXIT":    "⏱ MAX HOLD",
        "EARLY_FAILURE":    "🟠 EARLY FAILURE",
        "EARLY_STAGNATION": "🟠 STAGNATION",
        "EARLY_STAGNATION_2": "🟠 STAGNATION",
        "LOW_MOMENTUM_EXIT":"🟠 LOW MOMENTUM",
        "TP1_FULL_EXIT":    "✅ TP1 HIT",
        "FORCE SELL":       "⚠️ FORCE SELL",
        "PEAK_RETRACE":     "📉 PEAK RETRACE",
    }
    badge = _ACTION_BADGES.get(action, action)

    title = f"{badge}  ·  {ps.base_asset}/USDT"

    # Build body: BUY and SELL show different fields
    entry_price = getattr(ps, "buy_price", 0.0)
    held = _held_str(getattr(ps, "position_open_time", "")) if is_sell else None

    body_lines = [f"Price     {_format_price(ps.current_price)}"]
    if is_sell and entry_price > 0:
        body_lines.append(f"Entry     {_format_price(entry_price)}")
    if is_sell:
        body_lines.append(f"P/L       {_pl_display(pl)}")
    if held and is_sell:
        body_lines.append(f"Held      {held}")
    body_lines.append(f"Decision  {ps.decision}")

    embed = discord.Embed(title=title, color=color)
    embed.description = "```" + "\n" + "\n".join(body_lines) + "\n```"
    return embed
