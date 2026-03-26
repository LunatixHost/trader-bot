"""Futures Paper-Trading Module — Simulated SHORT (and optional LONG) positions.

This module is ADDITIVE — it does not modify the spot scalping layer or long-term layer.
All positions are entirely simulated:
  - No real Binance futures orders are placed.
  - No margin borrowing occurs.
  - No liquidation engine is needed at this leverage level.

Design principles:
  - Short priority: bearish structure + downtrend confirmation + inverted signal scoring.
  - Reuses existing effective_score / trend / ATR framework from PairState.
  - Separate state (FuturesPaperState), separate PnL accounting.
  - Conservative: 1–2× leverage, max 2 positions, small notional per trade.
  - Economic viability filter preserved: tiny ATR moves rejected.
  - Re-entry suppression: failed exits block same symbol for FUTURES_REENTRY_COOLDOWN_SECS.

Paper PnL formula for SHORT:
    pnl_pct = (entry_price - current_price) / entry_price × 100 × leverage

Paper PnL formula for LONG (optional, disabled in v1):
    pnl_pct = (current_price - entry_price) / entry_price × 100 × leverage
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from config import (
    FUTURES_PAPER_ENABLED,
    FUTURES_SHORT_ENABLED,
    FUTURES_LONG_ENABLED,
    FUTURES_MAX_LEVERAGE,
    FUTURES_POSITION_SIZE_USDT,
    FUTURES_MAX_CONCURRENT_POSITIONS,
    FUTURES_MIN_SCORE,
    FUTURES_MIN_SCORE_RANGING,
    FUTURES_MIN_SCORE_VOLATILE,
    FUTURES_ATR_SIZING_ENABLED,
    FUTURES_RISK_PCT_PER_TRADE,
    FUTURES_ATR_STOP_MULT,
    FUTURES_TP1_PCT,
    FUTURES_TP1_RATIO,
    FUTURES_HARD_STOP_PCT,
    FUTURES_TRAIL_ACTIVATION_PCT,
    FUTURES_TRAIL_DISTANCE_PCT,
    FUTURES_STAGNATION_MINUTES_T1,
    FUTURES_STAGNATION_THRESHOLD_T1,
    FUTURES_STAGNATION_MINUTES_T2,
    FUTURES_STAGNATION_THRESHOLD_T2,
    FUTURES_TIME_STOP_MINUTES,
    FUTURES_REENTRY_COOLDOWN_SECS,
    FUTURES_ROUND_TRIP_COST_PCT,
    FUTURES_MIN_REWARD_PCT,
    FUTURES_ATR_UNECONOMIC_MULT,
    FUTURES_CHECK_INTERVAL,
    FUTURES_FUNDING_RATE_PCT,
    FUTURES_FUNDING_INTERVAL_SECS,
    FUTURES_OB_BEAR_MAX,
    FUTURES_FLOW_BEAR_MAX,
    FUTURES_MACD_HISTOGRAM_MAX,
    FUTURES_OB_BULL_MIN,
    FUTURES_FLOW_BULL_MIN,
    FUTURES_TRIPLE_LONG_BLOCK,
    RSI_SELL_THRESHOLD,
    RSI_BUY_THRESHOLD,
    OB_IMBALANCE_BEAR,
    OB_FLOW_BEAR,
    OB_IMBALANCE_BULL,
    OB_FLOW_BULL,
    TRADING_PAIRS,
)
from state import FuturesPaperState
from logger import log_trade

logger = logging.getLogger("trading_bot.futures_paper")

# Module-level state reference (set by setup_futures_paper)
_state = None


# ─── Actions logged to trades table ────────────────────────────────────────
# These action strings are written verbatim — kept distinct from spot actions.
_ACTION_SHORT_ENTRY       = "FUTURES_SHORT_ENTRY"
_ACTION_LONG_ENTRY        = "FUTURES_LONG_ENTRY"
_ACTION_TP1               = "FUTURES_TP1"
_ACTION_SIGNAL_EXIT       = "FUTURES_SIGNAL_EXIT"
_ACTION_TRAIL_STOP        = "FUTURES_TRAIL_STOP"
_ACTION_HARD_STOP         = "FUTURES_HARD_STOP"
_ACTION_EARLY_STAGNATION  = "FUTURES_EARLY_STAGNATION"
_ACTION_TIME_STOP         = "FUTURES_TIME_STOP"

# Exit actions that trigger re-entry cooldown
_FAILED_EXIT_ACTIONS = {
    _ACTION_HARD_STOP,
    _ACTION_TRAIL_STOP,
    _ACTION_EARLY_STAGNATION,
    _ACTION_TIME_STOP,
}


# ─── Setup ──────────────────────────────────────────────────────────────────

def setup_futures_paper(state):
    """Bind the module to the shared bot state.  Called once at startup."""
    global _state
    _state = state
    logger.info(
        f"Futures paper module ready — "
        f"SHORT={'on' if FUTURES_SHORT_ENABLED else 'off'}  "
        f"LONG={'on' if FUTURES_LONG_ENABLED else 'off'}  "
        f"leverage={FUTURES_MAX_LEVERAGE}×  "
        f"size=${FUTURES_POSITION_SIZE_USDT:.0f}  "
        f"max_pos={FUTURES_MAX_CONCURRENT_POSITIONS}"
    )


# ─── Score helpers ───────────────────────────────────────────────────────────

def _futures_bearish_score(ps) -> float:
    """Compute a bearish signal score for a SHORT entry.

    This is intentionally inverted from spot BUY logic:
      - spot BUY prefers RSI oversold, BB below, MACD bullish cross
      - futures SHORT prefers RSI overbought, bearish MACD, weak OB/flow

    Score components (each ≥ 0, higher = more bearish):
      - structure_score < 0 contributes magnitude (bear PA confirmed)
      - RSI overbought (> RSI_SELL_THRESHOLD)
      - MACD bearish crossover
      - OB imbalance below bear threshold
      - Flow ratio below bear threshold
      - Downtrend confirmed

    Max possible: ~4.0 in extreme bearish setup.
    Minimum required for SHORT entry: FUTURES_MIN_SCORE (1.5).
    """
    score = 0.0

    # Inverted structure score: negative structure_score = bearish PA
    struct = float(getattr(ps, "structure_score", 0.0))
    if struct < 0:
        score += min(abs(struct), 1.5)

    # RSI overbought
    rsi = float(getattr(ps, "rsi", 50.0))
    if rsi > RSI_SELL_THRESHOLD:
        score += 0.8

    # Bearish MACD crossover
    if getattr(ps, "macd_crossover", "none") == "bearish":
        score += 0.6

    # Weak orderbook (sellers stacking)
    ob = float(getattr(ps, "orderbook_imbalance", 0.5))
    if ob < OB_IMBALANCE_BEAR:
        score += 0.5

    # Weak flow (sellers hitting bids)
    flow = float(getattr(ps, "flow_ratio", 0.5))
    if flow < OB_FLOW_BEAR:
        score += 0.5

    return score


def _compute_futures_pnl_pct(fp: FuturesPaperState, current_price: float) -> float:
    """Compute unrealised PnL % for a futures paper position.

    For SHORT: profit when price falls below entry.
    For LONG:  profit when price rises above entry.
    Scaled by leverage.
    """
    if fp.entry_price <= 0 or current_price <= 0:
        return 0.0
    if fp.side == "short":
        return (fp.entry_price - current_price) / fp.entry_price * 100.0 * fp.leverage
    else:  # long
        return (current_price - fp.entry_price) / fp.entry_price * 100.0 * fp.leverage


def _futures_bullish_score(ps) -> float:
    """Compute a bullish signal score for a LONG entry.

    Mirror of _futures_bearish_score() — inverted direction:
      - positive structure_score = bullish PA
      - RSI oversold (bounce setup)
      - bullish MACD crossover
      - OB imbalance above bull threshold (buyers stacking)
      - flow ratio above bull threshold (buyers lifting asks)

    Max possible: ~4.0. Minimum for LONG entry: FUTURES_MIN_SCORE (1.5).
    """
    score = 0.0

    struct = float(getattr(ps, "structure_score", 0.0))
    if struct > 0:
        score += min(struct, 1.5)

    rsi = float(getattr(ps, "rsi", 50.0))
    if rsi < RSI_BUY_THRESHOLD:
        score += 0.8

    if getattr(ps, "macd_crossover", "none") == "bullish":
        score += 0.6

    ob = float(getattr(ps, "orderbook_imbalance", 0.5))
    if ob > OB_IMBALANCE_BULL:
        score += 0.5

    flow = float(getattr(ps, "flow_ratio", 0.5))
    if flow > OB_FLOW_BULL:
        score += 0.5

    return score


def _dynamic_futures_min_score(ps) -> float:
    """Return the regime-adjusted minimum score required for a futures entry.

    Priority 1 — Volatility-Adjusted Gate:
      ranging         → FUTURES_MIN_SCORE_RANGING (1.2) — quiet market, score hard to reach at 1.5
      trending        → FUTURES_MIN_SCORE          (1.5) — baseline, moderate ATR
      choppy_volatile → FUTURES_MIN_SCORE_VOLATILE (1.8) — noisy; require stronger conviction
      trending_volatile→ FUTURES_MIN_SCORE_VOLATILE(1.8) — fast moves; weak signals get stopped out

    Using ATR regime (already computed per pair) avoids any extra indicator fetch.
    """
    regime = getattr(ps, "regime", "trending")
    if regime == "ranging":
        return FUTURES_MIN_SCORE_RANGING
    if regime in {"choppy_volatile", "trending_volatile"}:
        return FUTURES_MIN_SCORE_VOLATILE
    return FUTURES_MIN_SCORE  # trending or unknown → baseline


# ─── Entry gate ─────────────────────────────────────────────────────────────

def get_futures_entry_block_reason(ps, fp: FuturesPaperState, side: str, score: float) -> str | None:
    """Return a block reason string if a futures entry should be rejected, else None.

    Checks (in priority order):
      1.  Module disabled / side disabled
      2.  Position already open on this symbol
      3.  ATR uneconomic
      4.  Re-entry cooldown
      5.  Unstable candle
      6.  Trend alignment (downtrend for SHORT, uptrend for LONG)
      7.  Regime bias filter
      8.  Triple-long safety (LONG only)
      9.  Microstructure confirmation (directional)
      10. Momentum confirmation (MACD histogram direction)
      11. Score gate
      12. Max concurrent positions
    """
    if not FUTURES_PAPER_ENABLED:
        return "futures_disabled"
    if side == "short" and not FUTURES_SHORT_ENABLED:
        return "short_disabled"
    if side == "long" and not FUTURES_LONG_ENABLED:
        return "long_disabled"

    if fp.is_open:
        return "position_open"

    # Economic viability: ATR × multiplier × 100 must clear round-trip cost
    atr_pct = float(getattr(ps, "atr_pct", 0.0))
    if atr_pct > 0:
        expected_reward = atr_pct * FUTURES_ATR_UNECONOMIC_MULT * 100
        if expected_reward < FUTURES_ROUND_TRIP_COST_PCT:
            return "uneconomic_move"
        if expected_reward < FUTURES_MIN_REWARD_PCT:
            return "low_reward"

    # Re-entry cooldown after failed exit
    failed_reason = fp.last_failed_exit_reason
    failed_time   = fp.last_failed_exit_time
    if failed_reason and failed_time:
        try:
            ts = datetime.fromisoformat(failed_time.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
            if elapsed < FUTURES_REENTRY_COOLDOWN_SECS:
                return "reentry_cooldown"
        except Exception:
            pass

    # Unstable candle
    candle_range_pct = float(getattr(ps, "candle_range_pct", 0.0))
    if atr_pct > 0 and candle_range_pct > atr_pct * 2.5:
        return "unstable_candle"

    trend  = getattr(ps, "trend", "chop")
    regime = getattr(ps, "regime", "ranging")
    ob     = float(getattr(ps, "orderbook_imbalance", 0.5))
    flow   = float(getattr(ps, "flow_ratio", 0.5))
    macd_hist = float(getattr(ps, "macd_histogram", 0.0))

    if side == "short":
        # ── SHORT-specific gates ─────────────────────────────────────────────
        if trend != "downtrend":
            return "no_trend_alignment"
        # Block SHORTs during trending_volatile — high-velocity upswings crush shorts
        if regime in {"trending_volatile"}:
            return "unfavorable_regime"
        # Microstructure: both OB and flow must confirm selling pressure
        if not (ob < FUTURES_OB_BEAR_MAX and flow < FUTURES_FLOW_BEAR_MAX):
            return "weak_bearish_microstructure"
        # Momentum: MACD histogram must already be negative
        if macd_hist >= FUTURES_MACD_HISTOGRAM_MAX:
            return "no_downward_momentum"

    else:  # long
        # ── LONG-specific gates ──────────────────────────────────────────────
        if trend != "uptrend":
            return "no_bullish_trend"
        # Block LONGs in choppy_volatile — no clean directional edge
        if regime in {"choppy_volatile"}:
            return "unfavorable_regime"
        # Triple-long safety: block if spot scalp AND long-term both holding same coin
        if FUTURES_TRIPLE_LONG_BLOCK and _state is not None:
            pair_key = getattr(ps, "pair", "")
            spot_holding = float(getattr(ps, "asset_balance", 0.0)) > 0
            lt_holding   = getattr(_state.long_term_states.get(pair_key), "holding", False)
            if spot_holding and lt_holding:
                return "triple_long_risk"
        # Microstructure: both OB and flow must confirm buying pressure
        if not (ob > FUTURES_OB_BULL_MIN and flow > FUTURES_FLOW_BULL_MIN):
            return "weak_bullish_microstructure"
        # Momentum: MACD histogram must be positive (upward momentum confirmed)
        if macd_hist <= FUTURES_MACD_HISTOGRAM_MAX:
            return "no_upward_momentum"

    # Score gate — regime-adjusted minimum (Priority 1: volatility-adjusted gate)
    min_score = _dynamic_futures_min_score(ps)
    if score < min_score:
        return "low_score"

    # Concurrent position cap
    open_count = sum(1 for fp2 in _state.futures_states.values() if fp2.is_open)
    if open_count >= FUTURES_MAX_CONCURRENT_POSITIONS:
        return "max_positions"

    return None


def _all_futures_block_reasons(ps, fp: FuturesPaperState, side: str, score: float) -> list[str]:
    """Diagnostic helper — returns ALL block reasons that would fire, not just the first.

    Does NOT short-circuit.  Used only for debug logging so you can see which
    downstream gates would also block even when an upstream gate fires first.
    Never used in actual entry decisions.
    """
    reasons = []

    atr_pct = float(getattr(ps, "atr_pct", 0.0))
    if atr_pct > 0:
        expected_reward = atr_pct * FUTURES_ATR_UNECONOMIC_MULT * 100
        if expected_reward < FUTURES_ROUND_TRIP_COST_PCT:
            reasons.append("uneconomic_move")
        elif expected_reward < FUTURES_MIN_REWARD_PCT:
            reasons.append("low_reward")

    trend  = getattr(ps, "trend", "chop")
    regime = getattr(ps, "regime", "ranging")
    ob     = float(getattr(ps, "orderbook_imbalance", 0.5))
    flow   = float(getattr(ps, "flow_ratio", 0.5))
    macd_hist = float(getattr(ps, "macd_histogram", 0.0))

    if side == "short":
        if trend != "downtrend":
            reasons.append("no_trend_alignment")
        if regime in {"trending_volatile"}:
            reasons.append("unfavorable_regime")
        if not (ob < FUTURES_OB_BEAR_MAX and flow < FUTURES_FLOW_BEAR_MAX):
            reasons.append("weak_bearish_microstructure")
        if macd_hist >= FUTURES_MACD_HISTOGRAM_MAX:
            reasons.append("no_downward_momentum")
    else:  # long
        if trend != "uptrend":
            reasons.append("no_bullish_trend")
        if regime in {"choppy_volatile"}:
            reasons.append("unfavorable_regime")
        if FUTURES_TRIPLE_LONG_BLOCK and _state is not None:
            pair_key = getattr(ps, "pair", "")
            if float(getattr(ps, "asset_balance", 0.0)) > 0 and \
               getattr(_state.long_term_states.get(pair_key), "holding", False):
                reasons.append("triple_long_risk")
        if not (ob > FUTURES_OB_BULL_MIN and flow > FUTURES_FLOW_BULL_MIN):
            reasons.append("weak_bullish_microstructure")
        if macd_hist <= FUTURES_MACD_HISTOGRAM_MAX:
            reasons.append("no_upward_momentum")

    if score < _dynamic_futures_min_score(ps):
        reasons.append("low_score")

    return reasons


# ─── Funding fee simulation ──────────────────────────────────────────────────

def _apply_simulated_funding(fp: FuturesPaperState) -> float:
    """Apply accrued funding fees to an open paper position.

    Called each evaluation cycle.  Only charges the fee when a full
    FUTURES_FUNDING_INTERVAL_SECS (8 hours) has elapsed since the last charge,
    matching Binance's actual 00:00 / 08:00 / 16:00 UTC settlement schedule.

    Rate: FUTURES_FUNDING_RATE_PCT of notional per interval (both sides pay —
    conservative assumption that keeps paper P&L honest regardless of direction).

    Returns:
        USD amount charged this call (0.0 if interval not yet elapsed).
    """
    if not fp.is_open or fp.notional_usdt <= 0:
        return 0.0

    now = time.monotonic()
    # Initialise on first call after entry
    if fp.last_funding_ts == 0.0:
        fp.last_funding_ts = now
        return 0.0

    elapsed = now - fp.last_funding_ts
    if elapsed < FUTURES_FUNDING_INTERVAL_SECS:
        return 0.0  # Not yet time for next settlement

    fee = fp.notional_usdt * (FUTURES_FUNDING_RATE_PCT / 100.0)
    fp.accrued_funding_usdt += fee
    fp.last_funding_ts = now

    logger.info(
        f"[FUTURES PAPER] FUNDING {fp.symbol} {fp.side.upper()}  "
        f"-${fee:.4f} (total accrued: -${fp.accrued_funding_usdt:.4f}  "
        f"notional=${fp.notional_usdt:.2f})"
    )
    return fee


# ─── Exit logic ─────────────────────────────────────────────────────────────

def _get_futures_forced_exit_reason(fp: FuturesPaperState, pnl_pct: float) -> str | None:
    """Return a forced exit reason if the current state warrants one, else None.

    Priority (checked in order):
      1. HARD_STOP — max loss exceeded
      2. TRAIL_STOP — trailing stop price breached
      3. EARLY_STAGNATION T1 — no movement by T1 minutes
      4. EARLY_STAGNATION T2 — no movement by T2 minutes (time-stop tier)
      5. TIME_STOP — absolute max hold time exceeded
    """
    if not fp.is_open or fp.entry_price <= 0:
        return None

    # Hard stop: absolute max loss
    if pnl_pct < -FUTURES_HARD_STOP_PCT:
        return _ACTION_HARD_STOP

    # Trailing stop: once activated, stop is (for SHORT) entry - trail_distance above current
    if fp.trailing_stop > 0:
        if fp.side == "short":
            # For a short, we're watching price going DOWN.
            # trailing_stop is a PRICE — if price rises back above it, exit.
            pass  # Checked in _evaluate_futures_exits with current_price
        # Note: the actual trailing_stop breach check is done in _evaluate_futures_exits
        # because it needs current_price. Here we return None and let that caller handle it.

    # Time-based stagnation
    if fp.entry_time:
        try:
            entry_dt = datetime.fromisoformat(fp.entry_time.replace("Z", "+00:00"))
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            held_min = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60.0
        except Exception:
            held_min = 0.0

        # T1 stagnation
        if held_min >= FUTURES_STAGNATION_MINUTES_T1 and not fp.tp1_hit:
            if pnl_pct < FUTURES_STAGNATION_THRESHOLD_T1:
                return _ACTION_EARLY_STAGNATION

        # T2 stagnation (even if TP1 hit, check progression)
        if held_min >= FUTURES_STAGNATION_MINUTES_T2:
            if pnl_pct < FUTURES_STAGNATION_THRESHOLD_T2:
                return _ACTION_EARLY_STAGNATION

        # Absolute time stop
        if held_min >= FUTURES_TIME_STOP_MINUTES:
            return _ACTION_TIME_STOP

    return None


def _should_fire_futures_tp1(fp: FuturesPaperState, pnl_pct: float) -> bool:
    """Return True if TP1 partial exit should fire."""
    if fp.tp1_hit:
        return False
    return pnl_pct >= FUTURES_TP1_PCT


def _should_signal_exit_futures(fp: FuturesPaperState, ps, pnl_pct: float) -> bool:
    """Return True if the directional thesis has reversed and we should exit.

    For SHORT: exits when bearish thesis breaks (bullish reversal signals).
    For LONG:  exits when bullish thesis breaks (bearish reversal signals).
    Only fires in profit — hard_stop handles loss exits.
    """
    if not fp.is_open:
        return False

    # Only use signal exit when in profit (avoid signal-selling a loser)
    if pnl_pct < 0.10:
        return False

    macd_cross = getattr(ps, "macd_crossover", "none")
    ob   = float(getattr(ps, "orderbook_imbalance", 0.5))
    flow = float(getattr(ps, "flow_ratio", 0.5))

    if fp.side == "short":
        # Bearish thesis breaks on bullish reversal
        if macd_cross == "bullish":
            return True
        if ob > 0.55 and flow > 0.55:
            return True

    else:  # long
        # Bullish thesis breaks on bearish reversal
        if macd_cross == "bearish":
            return True
        if ob < 0.45 and flow < 0.45:
            return True

    return False


# ─── Execution (paper only) ──────────────────────────────────────────────────

def execute_futures_paper_entry(pair: str, ps, side: str = "short"):
    """Open a simulated futures paper position.  No real order placed.

    Initialises the FuturesPaperState fields and logs the trade.
    Called synchronously — no I/O, just state mutation + DB write.
    """
    fp = _state.futures_states.get(pair)
    if fp is None or fp.is_open:
        return

    current_price = float(getattr(ps, "current_price", 0.0))
    if current_price <= 0:
        logger.warning(f"[FUTURES PAPER] Cannot open {side.upper()} {pair}: no price")
        return

    entry_price = current_price
    leverage    = FUTURES_MAX_LEVERAGE

    # Priority 3 — ATR-Based Position Sizing:
    # Size each position so the hard stop always costs exactly
    # FUTURES_RISK_PCT_PER_TRADE % of total portfolio value.
    # High ATR → wide stop → smaller position. Low ATR → tighter stop → larger position.
    # Capped at FUTURES_POSITION_SIZE_USDT (max notional) and floored at $5 (no dust).
    notional = FUTURES_POSITION_SIZE_USDT  # fallback
    if FUTURES_ATR_SIZING_ENABLED and _state is not None:
        atr_pct = float(getattr(ps, "atr_pct", 0.0))
        portfolio_val = (
            _state.portfolio_current_value
            or _state.portfolio_total_usdt
            or 0.0
        )
        if atr_pct > 0 and portfolio_val > 0:
            dollar_risk  = portfolio_val * FUTURES_RISK_PCT_PER_TRADE
            stop_pct     = atr_pct * FUTURES_ATR_STOP_MULT  # expected stop distance
            atr_notional = dollar_risk / stop_pct
            notional     = max(5.0, min(FUTURES_POSITION_SIZE_USDT, atr_notional))

    quantity    = notional / entry_price

    now_iso = datetime.now(timezone.utc).isoformat()
    fp.symbol       = pair
    fp.side         = side
    fp.is_open      = True
    fp.entry_price  = entry_price
    fp.quantity     = quantity
    fp.notional_usdt = notional
    fp.leverage     = leverage
    fp.entry_time   = now_iso
    fp.peak_pnl_pct = 0.0
    fp.trailing_stop = 0.0
    fp.tp1_hit      = False
    fp.last_action_ts = time.monotonic()

    action = _ACTION_SHORT_ENTRY if side == "short" else _ACTION_LONG_ENTRY
    note = (
        f"PAPER {side.upper()} | lev={leverage:.1f}× | "
        f"notional=${notional:.2f} | qty={quantity:.6f}"
    )

    log_trade(
        pair=pair,
        action=action,
        coin_price=entry_price,
        amount_coin=quantity,
        amount_usdt=notional,
        rsi=float(getattr(ps, "rsi", 0.0)),
        confidence=0,
        pl_usdt=0.0,
        note=note,
        regime_at_entry=getattr(ps, "regime", ""),
        atr_value=float(getattr(ps, "atr", 0.0)),
        confidence_weighted=0.0,
    )

    logger.info(
        f"[FUTURES PAPER] OPEN {side.upper()} {pair}  "
        f"entry=${entry_price:,.4f}  notional=${notional:.2f}  lev={leverage:.1f}×"
    )


def execute_futures_tp1_partial(pair: str, ps):
    """Simulate TP1 partial close for a futures paper position.

    Closes FUTURES_TP1_RATIO of the position.  Remaining portion continues
    with trailing stop activated.
    """
    fp = _state.futures_states.get(pair)
    if fp is None or not fp.is_open:
        return

    current_price = float(getattr(ps, "current_price", 0.0))
    pnl_pct = _compute_futures_pnl_pct(fp, current_price)

    closed_qty  = fp.quantity * FUTURES_TP1_RATIO
    closed_notl = closed_qty * fp.entry_price
    # PnL on the closed portion
    if fp.side == "short":
        pl_usdt = (fp.entry_price - current_price) * closed_qty * fp.leverage
    else:
        pl_usdt = (current_price - fp.entry_price) * closed_qty * fp.leverage

    fp.quantity  -= closed_qty
    fp.notional_usdt -= closed_notl
    fp.tp1_hit   = True
    fp.last_action_ts = time.monotonic()

    # Activate trailing stop after TP1
    _update_futures_trailing_stop(fp, current_price)

    # Accumulate realized PnL on partial close
    _state.futures_paper_realized_pnl += pl_usdt

    log_trade(
        pair=pair,
        action=_ACTION_TP1,
        coin_price=current_price,
        amount_coin=closed_qty,
        amount_usdt=closed_notl,
        pl_usdt=pl_usdt,
        note=f"PAPER TP1 {FUTURES_TP1_RATIO*100:.0f}% closed | pnl={pnl_pct:+.2f}%",
    )

    logger.info(
        f"[FUTURES PAPER] TP1 {pair}  "
        f"closed {FUTURES_TP1_RATIO*100:.0f}% @ ${current_price:,.4f}  "
        f"pnl={pnl_pct:+.2f}%  pl=${pl_usdt:+.2f}"
    )


def execute_futures_paper_exit(pair: str, ps, reason: str):
    """Close a simulated futures paper position entirely.

    Computes final PnL, updates accounting, clears position state, and logs.
    """
    fp = _state.futures_states.get(pair)
    if fp is None or not fp.is_open:
        return

    current_price = float(getattr(ps, "current_price", 0.0))
    pnl_pct = _compute_futures_pnl_pct(fp, current_price)

    if fp.side == "short":
        pl_usdt = (fp.entry_price - current_price) * fp.quantity * fp.leverage
    else:
        pl_usdt = (current_price - fp.entry_price) * fp.quantity * fp.leverage

    # Apply paper round-trip cost (taker fees)
    cost_deduction = fp.notional_usdt * (FUTURES_ROUND_TRIP_COST_PCT / 100.0)
    pl_usdt -= cost_deduction

    # Deduct all accrued funding fees ("rent" paid over the hold period)
    funding_deduction = fp.accrued_funding_usdt
    pl_usdt -= funding_deduction

    # Hold time
    held_min = 0.0
    if fp.entry_time:
        try:
            entry_dt = datetime.fromisoformat(fp.entry_time.replace("Z", "+00:00"))
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            held_min = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60.0
        except Exception:
            pass

    # Update global accounting
    _state.futures_paper_realized_pnl += pl_usdt
    _state.futures_paper_total_trades += 1
    if pl_usdt > 0:
        _state.futures_paper_winning_trades += 1

    # Re-entry suppression for forced/failed exits
    if reason in _FAILED_EXIT_ACTIONS:
        fp.last_failed_exit_reason = reason
        fp.last_failed_exit_time   = datetime.now(timezone.utc).isoformat()

    # Clear position
    entry_price_snapshot = fp.entry_price
    fp.is_open      = False
    fp.entry_price  = 0.0
    fp.quantity     = 0.0
    fp.notional_usdt = 0.0
    fp.leverage     = 1.0
    fp.entry_time   = ""
    fp.peak_pnl_pct = 0.0
    fp.trailing_stop = 0.0
    fp.tp1_hit      = False
    fp.accrued_funding_usdt = 0.0
    fp.last_funding_ts = 0.0
    fp.last_action_ts = time.monotonic()

    note = (
        f"PAPER EXIT {reason} | "
        f"entry=${entry_price_snapshot:,.4f} → exit=${current_price:,.4f} | "
        f"pnl={pnl_pct:+.2f}% | fees=-${cost_deduction:.4f} | "
        f"funding=-${funding_deduction:.4f} | held={held_min:.1f}min"
    )

    log_trade(
        pair=pair,
        action=reason,
        coin_price=current_price,
        amount_coin=0.0,
        amount_usdt=0.0,
        pl_usdt=pl_usdt,
        note=note,
        hold_minutes=held_min,
        entry_time=fp.entry_time if fp.entry_time else "",
    )

    sign = "+" if pl_usdt >= 0 else ""
    logger.info(
        f"[FUTURES PAPER] CLOSE {pair} ({reason})  "
        f"${entry_price_snapshot:,.4f} → ${current_price:,.4f}  "
        f"pnl={pnl_pct:+.2f}%  fees=-${cost_deduction:.4f}  "
        f"funding=-${funding_deduction:.4f}  net={sign}${pl_usdt:.2f}  held={held_min:.1f}min"
    )


# ─── Trailing stop management ────────────────────────────────────────────────

def _update_futures_trailing_stop(fp: FuturesPaperState, current_price: float):
    """Update the trailing stop price for an open futures paper position.

    For SHORT:
      - Trail follows price DOWN.
      - trailing_stop = current_price × (1 + FUTURES_TRAIL_DISTANCE_PCT / 100)
        (price must stay below this level — if it rises above, exit)
      - Only tightens (never moves against position direction).
    """
    if not fp.is_open or fp.entry_price <= 0:
        return

    if fp.side == "short":
        new_trail = current_price * (1.0 + FUTURES_TRAIL_DISTANCE_PCT / 100.0)
        if fp.trailing_stop == 0.0:
            fp.trailing_stop = new_trail
        else:
            # Only move trail DOWN (tighter) — never up (looser)
            fp.trailing_stop = min(fp.trailing_stop, new_trail)
    else:
        # LONG trail: price × (1 - distance)
        new_trail = current_price * (1.0 - FUTURES_TRAIL_DISTANCE_PCT / 100.0)
        if fp.trailing_stop == 0.0:
            fp.trailing_stop = new_trail
        else:
            fp.trailing_stop = max(fp.trailing_stop, new_trail)


# ─── Evaluation ──────────────────────────────────────────────────────────────

def _evaluate_futures_short_entry(pair: str, ps):
    """Evaluate whether to open a paper SHORT on this pair.

    Called each futures_paper_loop cycle for pairs without an open position.
    All entry filtering is centralised in get_futures_entry_block_reason().
    """
    fp = _state.futures_states.get(pair)
    if fp is None:
        return

    # Compute bearish score first (needed by gate)
    bearish_score = _futures_bearish_score(ps)

    # Check entry gate — all hard blocks live here
    block = get_futures_entry_block_reason(ps, fp, "short", bearish_score)
    if block:
        # Shadow-check: collect ALL gates that would block (not just the first)
        # so the log shows the full picture even when an early gate fires first.
        all_blocks = _all_futures_block_reasons(ps, fp, "short", bearish_score)
        all_str = " | ".join(all_blocks) if all_blocks else block
        logger.debug(
            f"[FUTURES PAPER] {pair} SHORT blocked: {block}  "
            f"all_blocks=[{all_str}]  "
            f"score={bearish_score:.2f} "
            f"atr={getattr(ps,'atr_pct',0.0)*100:.3f}% "
            f"ob={getattr(ps,'orderbook_imbalance',0.5):.2f} "
            f"flow={getattr(ps,'flow_ratio',0.5):.2f} "
            f"macd_hist={getattr(ps,'macd_histogram',0.0):.6f} "
            f"trend={getattr(ps,'trend','?')} "
            f"regime={getattr(ps,'regime','?')}"
        )
        return

    # Entry approved — open paper position
    execute_futures_paper_entry(pair, ps, side="short")


def _evaluate_futures_long_entry(pair: str, ps):
    """Evaluate whether to open a paper LONG on this pair.

    Mirror of _evaluate_futures_short_entry() using bullish scoring and
    uptrend-aligned gates.  Called each futures_paper_loop cycle for pairs
    without an open position, when FUTURES_LONG_ENABLED is True.
    """
    fp = _state.futures_states.get(pair)
    if fp is None:
        return

    bullish_score = _futures_bullish_score(ps)

    block = get_futures_entry_block_reason(ps, fp, "long", bullish_score)
    if block:
        all_blocks = _all_futures_block_reasons(ps, fp, "long", bullish_score)
        all_str = " | ".join(all_blocks) if all_blocks else block
        logger.debug(
            f"[FUTURES PAPER] {pair} LONG blocked: {block}  "
            f"all_blocks=[{all_str}]  "
            f"score={bullish_score:.2f} "
            f"atr={getattr(ps,'atr_pct',0.0)*100:.3f}% "
            f"ob={getattr(ps,'orderbook_imbalance',0.5):.2f} "
            f"flow={getattr(ps,'flow_ratio',0.5):.2f} "
            f"macd_hist={getattr(ps,'macd_histogram',0.0):.6f} "
            f"trend={getattr(ps,'trend','?')} "
            f"regime={getattr(ps,'regime','?')}"
        )
        return

    execute_futures_paper_entry(pair, ps, side="long")


def _evaluate_futures_exits(pair: str, ps):
    """Evaluate whether to close or partially close an open futures paper position.

    Called each cycle for pairs with an open position.
    Handles (in priority order):
      1. Hard stop
      2. Trailing stop breach
      3. Signal reversal exit
      4. TP1 partial profit
      5. Stagnation / time stop
    """
    fp = _state.futures_states.get(pair)
    if fp is None or not fp.is_open:
        return

    current_price = float(getattr(ps, "current_price", 0.0))
    if current_price <= 0:
        return

    # Apply funding fee if interval has elapsed (8-hour settlement)
    _apply_simulated_funding(fp)

    pnl_pct = _compute_futures_pnl_pct(fp, current_price)

    # Update peak PnL for trailing reference
    if pnl_pct > fp.peak_pnl_pct:
        fp.peak_pnl_pct = pnl_pct

    # Activate / update trailing stop once position is profitable enough
    if pnl_pct >= FUTURES_TRAIL_ACTIVATION_PCT or fp.tp1_hit:
        _update_futures_trailing_stop(fp, current_price)

    # 1. Hard stop (max loss)
    if pnl_pct < -FUTURES_HARD_STOP_PCT:
        execute_futures_paper_exit(pair, ps, _ACTION_HARD_STOP)
        return

    # 2. Trailing stop breach
    if fp.trailing_stop > 0:
        if fp.side == "short" and current_price >= fp.trailing_stop:
            execute_futures_paper_exit(pair, ps, _ACTION_TRAIL_STOP)
            return
        if fp.side == "long" and current_price <= fp.trailing_stop:
            execute_futures_paper_exit(pair, ps, _ACTION_TRAIL_STOP)
            return

    # 3. Signal reversal exit (thesis invalidated)
    if _should_signal_exit_futures(fp, ps, pnl_pct):
        execute_futures_paper_exit(pair, ps, _ACTION_SIGNAL_EXIT)
        return

    # 4. TP1 partial close
    if _should_fire_futures_tp1(fp, pnl_pct):
        execute_futures_tp1_partial(pair, ps)
        return  # Continue holding remainder

    # 5. Forced exits (stagnation / time)
    forced = _get_futures_forced_exit_reason(fp, pnl_pct)
    if forced:
        execute_futures_paper_exit(pair, ps, forced)
        return


# ─── Main evaluation entry point ─────────────────────────────────────────────

def evaluate_and_trade_futures(pair: str):
    """Main per-pair evaluation function called by futures_paper_loop.

    Dispatches to entry or exit evaluation based on current position state.
    """
    if _state is None:
        return

    ps = _state.pairs.get(pair)
    fp = _state.futures_states.get(pair)
    if ps is None or fp is None:
        return

    if fp.is_open:
        _evaluate_futures_exits(pair, ps)
    else:
        if FUTURES_SHORT_ENABLED:
            _evaluate_futures_short_entry(pair, ps)
        if FUTURES_LONG_ENABLED and not fp.is_open:
            _evaluate_futures_long_entry(pair, ps)


# ─── Main loop ──────────────────────────────────────────────────────────────

async def futures_paper_loop():
    """Background task — evaluates all pairs on a fixed interval.

    Runs separately from the spot trading_loop.
    Completely independent: does not touch PairState.asset_balance or any
    spot execution path.
    """
    if not FUTURES_PAPER_ENABLED:
        logger.info("Futures paper trading disabled — loop not started")
        return

    logger.info(
        f"Futures paper loop started (interval={FUTURES_CHECK_INTERVAL}s)"
    )

    # Brief startup delay so prices and indicators are populated first
    await asyncio.sleep(30)

    while True:
        try:
            if _state is None or _state.is_paused:
                await asyncio.sleep(FUTURES_CHECK_INTERVAL)
                continue

            async with _state.lock:
                pairs = list(_state.futures_states.keys())

            for pair in pairs:
                try:
                    async with _state.lock:
                        evaluate_and_trade_futures(pair)
                except Exception as e:
                    logger.error(f"Futures paper eval error for {pair}: {e}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Futures paper loop error: {e}")

        await asyncio.sleep(FUTURES_CHECK_INTERVAL)


# ─── Utility for panel display ───────────────────────────────────────────────

def get_futures_unrealized_pnl(state) -> float:
    """Sum of unrealised PnL across all open futures paper positions (USDT)."""
    total = 0.0
    for pair, fp in state.futures_states.items():
        if not fp.is_open or fp.entry_price <= 0:
            continue
        ps = state.pairs.get(pair)
        if ps is None:
            continue
        current_price = float(getattr(ps, "current_price", 0.0))
        if current_price <= 0:
            continue
        pnl_pct = _compute_futures_pnl_pct(fp, current_price)
        # PnL in USDT = notional × pnl_pct / 100 (leverage already baked in)
        total += fp.notional_usdt * pnl_pct / 100.0
    return total


def get_futures_open_positions(state) -> list[dict]:
    """Return a list of dicts describing each open futures paper position.

    Used by panel.py for the FUTURES PAPER section display.
    """
    positions = []
    for pair, fp in state.futures_states.items():
        if not fp.is_open:
            continue
        ps = state.pairs.get(pair)
        current_price = float(getattr(ps, "current_price", 0.0)) if ps else 0.0
        pnl_pct = _compute_futures_pnl_pct(fp, current_price)
        pl_usdt = fp.notional_usdt * pnl_pct / 100.0

        held_min = 0.0
        if fp.entry_time:
            try:
                entry_dt = datetime.fromisoformat(fp.entry_time.replace("Z", "+00:00"))
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                held_min = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60.0
            except Exception:
                pass

        positions.append({
            "pair":                 pair,
            "base":                 pair.replace("USDT", ""),
            "side":                 fp.side,
            "entry_price":          fp.entry_price,
            "current_price":        current_price,
            "notional_usdt":        fp.notional_usdt,
            "leverage":             fp.leverage,
            "pnl_pct":              pnl_pct,
            "pl_usdt":              pl_usdt,
            "tp1_hit":              fp.tp1_hit,
            "trailing_stop":        fp.trailing_stop,
            "peak_pnl_pct":         fp.peak_pnl_pct,
            "held_min":             held_min,
            "entry_time":           fp.entry_time,
            "accrued_funding_usdt": fp.accrued_funding_usdt,
        })
    return positions
