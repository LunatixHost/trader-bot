"""Main entry point — orchestrates trading loop, signals, and Discord bot.

Usage:
    python bot.py

Startup sequence:
1. Load .env, validate API keys
2. Connect to Binance (testnet or live)
3. Load last known state from trades.db (or create fresh)
4. Open WebSocket price streams per pair
5. Start signal polling tasks per pair
6. Launch Discord bot
7. Begin trading loop
"""

import os
import sys

# ── Fix working directory when launched via double-click ──
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta

from config import (
    USE_TESTNET, TRADING_PAIRS,
    BINANCE_API_KEY, BINANCE_SECRET_KEY, DISCORD_BOT_TOKEN,
    INTERVAL_RSI, INTERVAL_ETHERSCAN, INTERVAL_ORDERBOOK,
    INTERVAL_DISCORD_PANEL, INTERVAL_PORTFOLIO_SNAPSHOT,
    INTERVAL_FVG,
    PORTFOLIO_REBALANCE_INTERVAL, PORTFOLIO_RESERVE_PCT, STOP_LOSS_PCT,
    TRADE_SIZE_PCT, MAX_POSITION_PCT, PROFIT_TARGET_PCT,
    ETHERSCAN_COINS,
    USE_ATR_EXITS, ATR_TRAILING_MULTIPLIER, ATR_TP_MULTIPLIER,
    ATR_MAX_TRAIL_PCT, ATR_TRAIL_ACTIVATION_PCT, ATR_SL_MULTIPLIER, ATR_SL_MAX_PCT,
    DRAWDOWN_TRIGGER_PCT, TRADE_COOLDOWN_SECONDS, TRADE_COOLDOWN_TRENDING,
    COOLDOWN_BYPASS_CONFIDENCE, COOLDOWN_BYPASS_REGIMES,
    FAILED_EXIT_COOLDOWN_STAGNATION, FAILED_EXIT_COOLDOWN_TRAIL_STOP, FAILED_EXIT_COOLDOWN_HARD_STOP,
    BTC_CRASH_PCT, BTC_CRASH_THRESHOLD_BOOST, BTC_CRASH_SIZE_MULTIPLIER,
    DOWNTREND_SIZE_MULTIPLIER,
    BTC_CRASH_COOLDOWN, WICK_RATIO_THRESHOLD, MIN_EDGE_PCT, MIN_TRADE_NOTIONAL,
    SPREAD_MAX_PCT, SPIKE_FILTER_PCT, UNSTABLE_CANDLE_ATR_MULT,
    FVG_MIN_GAP_PCT, FVG_MAX_AGE_SECONDS, FVG_FAST_SCORE_MIN,
    FVG_OB_MIN, FVG_COOLDOWN_SECONDS, FVG_ORDER_TIMEOUT,
    BASE_TP1_PCT, TP1_SELL_RATIO, BREAKEVEN_BUFFER_PCT,
    TRAILING_ACTIVATION_PCT, TRAILING_DISTANCE_PCT,
    HARD_EXIT_MINUTES_BASE,
    ROUND_TRIP_COST_PCT, ECONOMIC_MIN_REWARD_PCT,
    SCALP_EARLY_STAG_MINUTES_T1, SCALP_EARLY_STAG_MIN_PCT_T1,
    SCALP_EARLY_STAG_MINUTES, SCALP_EARLY_STAG_MIN_PCT,
    CONFIDENCE_BUY_THRESHOLD, RSI_BUY_THRESHOLD,
    DYNAMIC_VOL_FACTOR_MIN, DYNAMIC_VOL_FACTOR_MAX, DYNAMIC_VOL_SMOOTH,
    LONG_TERM_ENABLED, LONG_TERM_ALLOCATION, LONG_TERM_ASSETS,
    LONG_TERM_ENTRY_THRESHOLD, LONG_TERM_EXIT_THRESHOLD,
    LONG_TERM_CHECK_INTERVAL, LONG_TERM_DOWNTREND_EXIT_CYCLES,
    FUTURES_PAPER_ENABLED,
    FUTURES_LEVERAGE, FUTURES_ACCOUNT_RISK_PCT,
    FUTURES_MAKER_FEE, FUTURES_TAKER_FEE,
    AUDIT_MODE, AUDIT_MICRO_NOTIONAL,
    get_base_asset,
)
import futures_execution
from binance_client import binance, ExecutionError
from signals.fvg import detect_fvg
from state import BotState, LongTermState, FuturesPaperState, create_bot_state, load_state_from_db, save_state_to_db
from strategy import get_decision, get_trade_size_modifier, get_correlation_modifier, get_btc_cluster_size_modifier
from portfolio import (
    rebalance, update_portfolio_tracking, update_volatility,
    calculate_portfolio_value,
)
from logger import init_db, log_trade, save_portfolio_snapshot
from signal_weights import compute_signal_weights
from signals.rsi import fetch_technical_indicators
from signals.etherscan import fetch_etherscan
from signals.orderbook import fetch_orderbook_signals
from market_data import init_market_data, data_provider, market_dynamics_loop

# ─── ANSI Color Codes ────────────────────────────────────────────────

class C:
    """ANSI escape codes for terminal colors."""
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    # Foreground
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    WHITE   = "\033[97m"
    GRAY    = "\033[90m"
    # Backgrounds
    BG_RED    = "\033[41m"
    BG_GREEN  = "\033[42m"
    BG_YELLOW = "\033[43m"


class ColorFormatter(logging.Formatter):
    """Color-coded log formatter: green timestamp, level-colored messages."""

    LEVEL_COLORS = {
        logging.DEBUG:    C.GRAY,
        logging.INFO:     C.WHITE,
        logging.WARNING:  C.YELLOW,
        logging.ERROR:    C.RED,
        logging.CRITICAL: C.BG_RED + C.WHITE,
    }

    def format(self, record):
        color = self.LEVEL_COLORS.get(record.levelno, C.WHITE)
        ts = self.formatTime(record, self.datefmt)
        return f"{C.GRAY}[{ts}]{C.RESET} {color}{record.getMessage()}{C.RESET}"


# ─── Logging Setup ────────────────────────────────────────────────────

import io
_stream = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
_handler = logging.StreamHandler(_stream)
_handler.setFormatter(ColorFormatter(datefmt="%H:%M:%S"))
logging.basicConfig(
    level=logging.INFO,
    handlers=[_handler],
)
log = logging.getLogger("trading_bot")

# Global state
state: BotState = None  # type: ignore
discord_notify_callback = None  # Set by discord_bot on startup


# ─── Signal Polling Tasks ─────────────────────────────────────────────

async def poll_rsi(pair: str):
    """Poll RSI + MACD + Bollinger Bands + ATR + regime for a pair."""
    while True:
        try:
            result = await fetch_technical_indicators(binance, pair, data_provider)
            async with state.lock:
                ps = state.pairs[pair]
                ps.rsi = result["rsi"]
                ps.macd = result["macd"]
                ps.macd_signal = result["macd_signal"]
                ps.macd_histogram = result["macd_histogram"]
                ps.macd_crossover = result["macd_crossover"]
                ps.bb_upper = result["bb_upper"]
                ps.bb_middle = result["bb_middle"]
                ps.bb_lower = result["bb_lower"]
                ps.bb_position = result["bb_position"]
                ps.bb_squeeze = result["bb_squeeze"]
                # ATR & regime (computed from same candles, no extra API call)
                ps.atr = result.get("atr", 0.0)
                ps.atr_pct = result.get("atr_pct", 0.0)
                ps.avg_atr_20 = result.get("avg_atr_20", 0.0)
                ps.trend_strength = result.get("trend_strength", 0.0)
                ps.trend_direction = result.get("trend_direction", "flat")
                ps.regime = result.get("regime", "ranging")
                # Derive trend label from trend_direction (used by chop gate)
                td = ps.trend_direction
                ps.trend = "uptrend" if td == "up" else ("downtrend" if td == "down" else "chop")
                ps.wick_ratio = result.get("wick_ratio", 0.0)
                ps.candle_range_pct = result.get("candle_range_pct", 0.0)
                ps.liquidity_sweep_score = result.get("liquidity_sweep_score", 0.0)
                ps.structure_score = result.get("structure_score", 0.0)
                ps.volatility_score = result.get("volatility_score", 0.0)
                # Update ATR trailing stop if holding a position
                if ps.asset_balance > 0 and ps.atr > 0 and USE_ATR_EXITS and ps.buy_price > 0:
                    gain_pct = (ps.current_price - ps.buy_price) / ps.buy_price * 100

                    if ps.tp1_hit and gain_pct >= TRAILING_ACTIVATION_PCT:
                        # Post-TP1: tight fixed-distance trailing (0.2%)
                        new_stop = ps.current_price * (1 - TRAILING_DISTANCE_PCT / 100)
                        if new_stop > ps.trailing_stop:
                            ps.trailing_stop = new_stop
                    elif not ps.tp1_hit and gain_pct >= ATR_TRAIL_ACTIVATION_PCT:
                        # Pre-TP1: ATR-based trailing
                        new_stop = ps.current_price - ps.atr * ATR_TRAILING_MULTIPLIER
                        # Cap: never let stop be >2% below price once activated
                        max_trail = ps.current_price * (1 - ATR_MAX_TRAIL_PCT)
                        new_stop = max(new_stop, max_trail)
                        # Trailing stop only ratchets UP, never down
                        if new_stop > ps.trailing_stop:
                            ps.trailing_stop = new_stop

                # Track peak unrealised gain for profit-protection logic
                if ps.asset_balance > 0 and ps.buy_price > 0 and ps.current_price > 0:
                    gain_pct = (ps.current_price - ps.buy_price) / ps.buy_price * 100
                    if gain_pct > ps.peak_gain_pct:
                        ps.peak_gain_pct = gain_pct

                # Recalculate per-pair dynamic parameters each cycle
                _update_dynamic_params(ps)
        except Exception as e:
            log.error(f"RSI poll error for {pair}: {e}")
        await asyncio.sleep(INTERVAL_RSI)


async def poll_rsi_on_close():
    """Phase 5: Kline-close triggered indicator recalculation.

    Drains binance.kline_close_queue and fires a full indicator recalc for the
    pair whose candle just closed.  Fires at candle-close T+0 (WebSocket push)
    instead of waiting up to INTERVAL_RSI seconds for the REST poll to wake up.

    Coexists with poll_rsi — if the kline stream misses a close or reconnects,
    poll_rsi still recalculates on its 10-second timer as a fallback.
    """
    log.info("Kline-close indicator worker started (Phase 5)")
    while True:
        try:
            pair = await binance.kline_close_queue.get()
            try:
                if pair not in state.pairs:
                    continue
                result = await fetch_technical_indicators(
                    binance, pair, data_provider=data_provider
                )
                async with state.lock:
                    ps = state.pairs[pair]
                    ps.rsi              = result["rsi"]
                    ps.macd             = result["macd"]
                    ps.macd_signal      = result["macd_signal"]
                    ps.macd_histogram   = result["macd_histogram"]
                    ps.macd_crossover   = result["macd_crossover"]
                    ps.bb_upper         = result["bb_upper"]
                    ps.bb_middle        = result["bb_middle"]
                    ps.bb_lower         = result["bb_lower"]
                    ps.bb_position      = result["bb_position"]
                    ps.bb_squeeze       = result["bb_squeeze"]
                    ps.atr              = result.get("atr", 0.0)
                    ps.atr_pct          = result.get("atr_pct", 0.0)
                    ps.avg_atr_20       = result.get("avg_atr_20", 0.0)
                    ps.trend_strength   = result.get("trend_strength", 0.0)
                    ps.trend_direction  = result.get("trend_direction", "flat")
                    ps.regime           = result.get("regime", "ranging")
                    td = ps.trend_direction
                    ps.trend = "uptrend" if td == "up" else ("downtrend" if td == "down" else "chop")
                    ps.wick_ratio       = result.get("wick_ratio", 0.0)
                    ps.candle_range_pct = result.get("candle_range_pct", 0.0)
                    ps.liquidity_sweep_score = result.get("liquidity_sweep_score", 0.0)
                    ps.structure_score  = result.get("structure_score", 0.0)
                    ps.volatility_score = result.get("volatility_score", 0.0)
                    _update_dynamic_params(ps)
                log.debug(f"Kline-close recalc {pair}: RSI={result['rsi']:.1f}")
            except Exception as e:
                log.error(f"Kline-close recalc error for {pair}: {e}")
            finally:
                binance.kline_close_queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"Kline-close worker error: {e}")


def _update_dynamic_params(ps):
    """Recalculate per-pair dynamic parameters from current market state.

    Called inside poll_rsi (under state.lock) every RSI cycle.
    Updates ps.dynamic_params in-place.

    volatility_factor: ATR / avg_atr_20 → high vol raises threshold, low vol lowers it.
        - Exponential smoothing (alpha=DYNAMIC_VOL_SMOOTH) damps oscillation.
        - Hard capped to [DYNAMIC_VOL_FACTOR_MIN, DYNAMIC_VOL_FACTOR_MAX].
    momentum_factor: scales TP1 by structure_score strength.
    trend_factor: allows longer holds in trending regimes.
    """
    prev_params = ps.dynamic_params or {}
    prev_vf = prev_params.get("_volatility_factor", 1.0)

    # ── volatility_factor ──
    if ps.avg_atr_20 > 0 and ps.atr > 0:
        raw_vf = ps.atr / ps.avg_atr_20
    else:
        raw_vf = 1.0
    # Exponential smoothing to prevent per-cycle oscillation
    vf = prev_vf + DYNAMIC_VOL_SMOOTH * (raw_vf - prev_vf)
    vf = max(DYNAMIC_VOL_FACTOR_MIN, min(DYNAMIC_VOL_FACTOR_MAX, vf))

    # ── momentum_factor: structure_score → TP1 scaling ──
    # structure_score typically -2 to +2; map to 0.75–1.25 range
    momentum_factor = 1.0 + max(-0.25, min(0.25, ps.structure_score * 0.125))

    # ── trend_factor: longer hold in trending regimes ──
    if ps.regime in {"trending", "trending_volatile"}:
        trend_factor = 1.33
    else:
        trend_factor = 1.0  # chop / ranging → use base exit window

    # ── Regime/trend behavior mode ──
    # Three modes derived purely from ps.trend (set by poll_rsi every 10 s).
    # Stored in dynamic_params so strategy.py and _get_forced_exit_reason can
    # read them without needing ps directly.
    #
    # UPTREND  — dips statistically bounce → ease penalties, lower threshold, allow more time
    # RANGING  — baseline environment the scalper is designed for → unchanged
    # DOWNTREND — continuation risk real → slightly heavier penalties, higher threshold
    trend_label = getattr(ps, "trend", "chop")

    if trend_label == "uptrend":
        trade_mode    = "UPTREND"
        penalty_scale = 0.8    # general penalties 20% lighter
        threshold_bias = -0.2  # entry threshold eased
        stag_t1 = SCALP_EARLY_STAG_MINUTES_T1 + 2.0   # +2 min for trades to develop
        stag_t2 = SCALP_EARLY_STAG_MINUTES + 2.0
    elif trend_label == "downtrend":
        trade_mode    = "DOWNTREND"
        penalty_scale = 1.1    # general penalties 10% heavier
        threshold_bias = 0.2   # entry threshold tightened
        stag_t1 = SCALP_EARLY_STAG_MINUTES_T1     # no grace: cut weak downtrend trades faster
        stag_t2 = SCALP_EARLY_STAG_MINUTES
    else:
        trade_mode    = "RANGING"
        penalty_scale = 1.0    # baseline
        threshold_bias = 0.0
        stag_t1 = SCALP_EARLY_STAG_MINUTES_T1
        stag_t2 = SCALP_EARLY_STAG_MINUTES

    # Floor TP1 at ECONOMIC_MIN_REWARD_PCT so it never drops below fee break-even,
    # even when momentum_factor is at its minimum (0.75).
    # Without this floor: 0.35 × 0.75 = 0.2625%, barely above break-even but close.
    # With floor: guaranteed minimum target that justifies execution costs.
    tp1_raw = BASE_TP1_PCT * momentum_factor
    tp1_floored = max(tp1_raw, ECONOMIC_MIN_REWARD_PCT)

    ps.dynamic_params = {
        "buy_threshold":      CONFIDENCE_BUY_THRESHOLD * vf,
        "tp1":                tp1_floored,
        "hard_exit_minutes":  HARD_EXIT_MINUTES_BASE / trend_factor,
        "_volatility_factor": vf,       # carry forward for next cycle's EMA
        "trade_mode":         trade_mode,
        "penalty_scale":      penalty_scale,
        "threshold_bias":     threshold_bias,
        "stag_t1_minutes":    stag_t1,
        "stag_t2_minutes":    stag_t2,
    }


async def poll_fvg(pair: str, initial_delay: float = 0):
    """Poll Fair Value Gap detection on 5m candles for a pair.

    Updates ps.fvg_detected, ps.fvg_buy_price, ps.fvg_top, ps.fvg_bottom.
    Does NOT place orders — order placement happens in execute_fvg_entry().
    """
    if initial_delay > 0:
        await asyncio.sleep(initial_delay)
    while True:
        try:
            result = await detect_fvg(binance, pair, data_provider)
            async with state.lock:
                ps = state.pairs[pair]
                # Only update FVG state if no limit order is pending
                # (don't overwrite an active order with a new gap)
                if not ps.fvg_limit_active:
                    ps.fvg_detected      = result["fvg_detected"]
                    ps.fvg_buy_price     = result["fvg_buy_price"]
                    ps.fvg_top           = result["fvg_top"]
                    ps.fvg_bottom        = result["fvg_bottom"]
                    ps.fvg_direction     = result.get("fvg_direction", "bullish")
                    ps.fvg_detected_time = result.get("fvg_detected_time", "")
        except Exception as e:
            log.error(f"FVG poll error for {pair}: {e}")
        await asyncio.sleep(INTERVAL_FVG)




async def poll_etherscan(pair: str):
    """Poll Etherscan for ETH only."""
    base = get_base_asset(pair)
    if base not in ETHERSCAN_COINS:
        return
    while True:
        try:
            result = await fetch_etherscan(base)
            async with state.lock:
                ps = state.pairs[pair]
                ps.etherscan_gas = result["gas_price"]
                ps.etherscan_activity = result["activity"]
        except Exception as e:
            log.error(f"Etherscan poll error: {e}")
        await asyncio.sleep(INTERVAL_ETHERSCAN)


async def poll_orderbook(pair: str):
    """Poll orderbook imbalance and trade flow for a pair.

    Updates ps.bid_ask_spread_pct, ps.orderbook_imbalance, ps.flow_ratio.
    Runs every INTERVAL_ORDERBOOK seconds (same cadence as RSI, no extra cost).
    """
    while True:
        try:
            result = await fetch_orderbook_signals(binance, pair)
            async with state.lock:
                ps = state.pairs[pair]
                ps.bid_ask_spread_pct = result["spread_pct"]
                ps.orderbook_imbalance = result["imbalance"]
                ps.flow_ratio = result["flow_ratio"]
        except Exception as e:
            log.error(f"Orderbook poll error for {pair}: {e}")
        await asyncio.sleep(INTERVAL_ORDERBOOK)


# ─── Portfolio Tasks ──────────────────────────────────────────────────

async def portfolio_rebalance_loop():
    """Periodically rebalance and sync balances with Binance.

    Keeps portfolio_total_usdt, usdt_balance, usdt_reserved, and
    portfolio_current_value in sync with reality every rebalance cycle.
    """
    while True:
        try:
            # Re-sync USDT balance from the USDⓈ-M futures wallet.
            # Futures positions are USDT-settled — we never query base-asset balances.
            usdt = await futures_execution.get_futures_usdt_balance()
            async with state.lock:
                # Guard: never overwrite a valid balance with zero (API error / network blip)
                if usdt > 0:
                    state.usdt_balance = usdt
                else:
                    log.warning(
                        "portfolio_rebalance_loop: futures USDT balance returned 0 — "
                        "keeping previous usdt_balance to avoid false drawdown"
                    )

                # Recompute portfolio_total_usdt from live data every cycle.
                # Must include ALL three layers:
                #   usdt_balance + spot positions + long-term positions + futures PnL
                # Using calculate_portfolio_value() as the single source of truth.
                # (Prior inline formula only counted spot, causing LT buys to appear
                #  as permanent losses and understating trade-size calculations.)
                fresh_total = calculate_portfolio_value(state)
                if fresh_total > 0:
                    state.portfolio_total_usdt = fresh_total

                # Refresh portfolio_current_value and P/L from the same snapshot
                update_portfolio_tracking(state)

            await update_volatility(state, binance)
            await rebalance(state, binance)
        except Exception as e:
            log.error(f"Portfolio rebalance error: {e}")
        await asyncio.sleep(PORTFOLIO_REBALANCE_INTERVAL)


async def portfolio_snapshot_loop():
    """Save portfolio snapshots periodically."""
    while True:
        await asyncio.sleep(INTERVAL_PORTFOLIO_SNAPSHOT)
        try:
            save_portfolio_snapshot(
                state.portfolio_current_value,
                state.portfolio_pl_usdt,
                state.portfolio_pl_pct,
            )
        except Exception as e:
            log.error(f"Portfolio snapshot error: {e}")


async def state_persistence_loop():
    """Save state to DB periodically for restart recovery."""
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.get_running_loop().run_in_executor(None, save_state_to_db, state)
        except Exception as e:
            log.error(f"State persistence error: {e}")


async def signal_weight_learning_loop():
    """Periodically recompute signal weights from trade history.

    Runs every 5 minutes. Reads completed trades, computes per-signal
    reliability scores, and updates BotState.signal_weights.

    Frozen during drawdown to prevent learning from bad conditions.
    Uses inertia smoothing to prevent sudden weight shifts.
    """
    await asyncio.sleep(30)  # Wait for initial trades to accumulate
    while True:
        try:
            loop = asyncio.get_running_loop()
            current = state.signal_weights.copy()
            drawdown = state.in_drawdown
            from functools import partial
            weights = await loop.run_in_executor(
                None, partial(
                    compute_signal_weights,
                    current_weights=current,
                    in_drawdown=drawdown,
                )
            )
            async with state.lock:
                state.signal_weights = weights
        except Exception as e:
            log.error(f"Signal weight learning error: {e}")
        await asyncio.sleep(300)  # Every 5 minutes


async def btc_crash_detection_loop():
    """Monitor BTC for sharp drops and activate market-wide risk-off.

    Checks BTC's recent 15m candle change. If BTC drops more than
    BTC_CRASH_PCT in 15 min, activates crash mode for all pairs.
    Crash mode: higher buy threshold, smaller position sizes.
    """
    btc_prev = 0.0
    while True:
        try:
            btc_price = 0.0
            if "BTCUSDT" in state.pairs:
                btc_price = state.pairs["BTCUSDT"].current_price

            if btc_price > 0 and btc_prev > 0:
                change_pct = (btc_price - btc_prev) / btc_prev * 100
                async with state.lock:
                    state.btc_15m_change_pct = round(change_pct, 2)

                if change_pct <= BTC_CRASH_PCT and not state.btc_crash_active:
                    crash_until = datetime.now(timezone.utc).timestamp() + BTC_CRASH_COOLDOWN
                    async with state.lock:
                        state.btc_crash_active = True
                        state.btc_crash_until = datetime.fromtimestamp(
                            crash_until, tz=timezone.utc
                        ).isoformat()
                    log.warning(
                        f"{C.BG_RED}{C.WHITE}{C.BOLD} BTC CRASH {C.RESET} "
                        f"{C.RED}BTC {change_pct:+.1f}% in 15m{C.RESET} "
                        f"— risk-off mode for {BTC_CRASH_COOLDOWN}s"
                    )

            # Check if crash mode should expire
            if state.btc_crash_active and state.btc_crash_until:
                try:
                    expire = datetime.fromisoformat(state.btc_crash_until)
                    if datetime.now(timezone.utc) >= expire:
                        async with state.lock:
                            state.btc_crash_active = False
                            state.btc_crash_until = ""
                        log.info(
                            f"{C.BG_GREEN}{C.WHITE}{C.BOLD} CLEAR {C.RESET} "
                            f"{C.GREEN}BTC crash filter expired{C.RESET} — resuming normal trading"
                        )
                except (ValueError, TypeError):
                    pass

            if btc_price > 0:
                btc_prev = btc_price

        except Exception as e:
            log.error(f"BTC crash detection error: {e}")

        # Check every 15 seconds (sub-candle resolution for fast detection)
        await asyncio.sleep(15)


async def drawdown_tracking_loop():
    """Track portfolio drawdown and toggle drawdown mode.

    Runs every 30 seconds. When drawdown exceeds threshold,
    sets state.in_drawdown = True which triggers:
    - Higher buy threshold (+1)
    - Halved position sizes
    """
    while True:
        try:
            async with state.lock:
                if state.portfolio_peak_value > 0:
                    dd = (state.portfolio_peak_value - state.portfolio_current_value) / state.portfolio_peak_value * 100
                    state.drawdown_pct = max(0.0, dd)
                    was_in_drawdown = state.in_drawdown
                    state.in_drawdown = dd >= DRAWDOWN_TRIGGER_PCT
                    if state.in_drawdown and not was_in_drawdown:
                        log.warning(
                            f"{C.BG_YELLOW}{C.WHITE}{C.BOLD} DRAWDOWN {C.RESET} "
                            f"{C.YELLOW}Portfolio is {dd:.1f}% below peak{C.RESET} "
                            f"(${state.portfolio_current_value:.0f} vs peak ${state.portfolio_peak_value:.0f}) "
                            f"— raising threshold, halving sizes"
                        )
                    elif not state.in_drawdown and was_in_drawdown:
                        log.info(
                            f"{C.BG_GREEN}{C.WHITE}{C.BOLD} RECOVERED {C.RESET} "
                            f"{C.GREEN}Drawdown {dd:.1f}%{C.RESET} "
                            f"below threshold {DRAWDOWN_TRIGGER_PCT}% — normal trading"
                        )
        except Exception as e:
            log.error(f"Drawdown tracking error: {e}")
        await asyncio.sleep(30)


# ─── Trading Logic ────────────────────────────────────────────────────

def _position_metrics(ps) -> dict:
    """Compute live position metrics used by exit/risk logic."""
    pnl_pct = 0.0
    if ps.asset_balance > 0 and ps.buy_price > 0 and ps.current_price > 0:
        pnl_pct = (ps.current_price - ps.buy_price) / ps.buy_price * 100

    hold_minutes = 0.0
    if ps.position_open_time:
        try:
            opened = datetime.fromisoformat(ps.position_open_time)
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            hold_minutes = (datetime.now(timezone.utc) - opened).total_seconds() / 60
        except Exception:
            pass

    hard_exit_minutes = ps.dynamic_params.get("hard_exit_minutes", HARD_EXIT_MINUTES_BASE)

    hard_stop_price = 0.0
    if ps.buy_price > 0:
        # Dynamic (Triple Barrier) stop-loss: widen with ATR in volatile markets
        # to prevent whipsaw stop-outs; floor is always STOP_LOSS_PCT.
        # e.g. ATR=0.4% → SL=0.6%; ATR=0.8% → SL=1.2%; quiet ATR=0.2% → floor 0.5%
        atr_sl_pct = ps.atr_pct * ATR_SL_MULTIPLIER * 100 if ps.atr_pct > 0 else 0.0
        effective_sl_pct = min(ATR_SL_MAX_PCT, max(STOP_LOSS_PCT, atr_sl_pct))
        hard_stop_price = ps.buy_price * (1 - effective_sl_pct / 100)

    active_stop = hard_stop_price
    if ps.trailing_stop > 0:
        active_stop = max(active_stop, ps.trailing_stop)

    return {
        "pnl_pct": pnl_pct,
        "hold_minutes": hold_minutes,
        "hard_exit_minutes": hard_exit_minutes,
        "hard_stop_price": hard_stop_price,
        "active_stop": active_stop,
    }


def _get_forced_exit_reason(ps) -> str | None:
    """
    Exit priority:
    1. Hard stop loss
    2. Trailing stop
    3. Time stop for stale/non-performing trades
    4. Maximum hold cap
    """
    if ps.asset_balance <= 0 or ps.buy_price <= 0 or ps.current_price <= 0:
        return None

    m = _position_metrics(ps)
    pnl_pct = m["pnl_pct"]
    hold_minutes = m["hold_minutes"]
    hard_exit_minutes = m["hard_exit_minutes"]
    hard_stop_price = m["hard_stop_price"]

    if hard_stop_price > 0 and ps.current_price <= hard_stop_price:
        return "HARD_STOP"

    if ps.trailing_stop > 0 and ps.current_price <= ps.trailing_stop:
        return "TRAIL_STOP"

    # Early stagnation: exit non-moving trades well before the full time window.
    # Tier 1 / Tier 2 timing is regime-adjusted:
    #   UPTREND  → +2 min grace (dips develop slower against momentum)
    #   RANGING  → baseline (SCALP_EARLY_STAG_MINUTES_T1 / SCALP_EARLY_STAG_MINUTES)
    #   DOWNTREND → baseline (no grace: weak downtrend trades should die quickly)
    stag_t1 = ps.dynamic_params.get("stag_t1_minutes", SCALP_EARLY_STAG_MINUTES_T1)
    stag_t2 = ps.dynamic_params.get("stag_t2_minutes", SCALP_EARLY_STAG_MINUTES)
    if hold_minutes >= stag_t1 and pnl_pct < SCALP_EARLY_STAG_MIN_PCT_T1:
        return "EARLY_STAGNATION"
    if hold_minutes >= stag_t2 and pnl_pct < SCALP_EARLY_STAG_MIN_PCT:
        return "EARLY_STAGNATION_2"

    if hold_minutes >= hard_exit_minutes and pnl_pct <= 0.15:
        return "TIME_STOP"

    if hold_minutes >= hard_exit_minutes * 1.5:
        return "MAX_HOLD_EXIT"

    return None


def _should_allow_signal_sell(ps) -> bool:
    """
    Prevent weak SELL signals from clipping winners too early.
    Strategy can still suggest SELL, but bot.py only executes it
    when reward is meaningful, the trade has peaked and retraced, or has gone stale.

    Two escape routes calibrated to actual captured bounce sizes (0.01–0.25%):
    - pnl_pct >= 0.08%  : lower green threshold matches realistic small-bounce wins
    - peak_gain_pct >= 0.15% : allow exit even if current PnL has partially retraced,
      so a trade that peaked at +0.18% and slipped to +0.04% can still be closed
    """
    if ps.asset_balance <= 0 or ps.buy_price <= 0 or ps.current_price <= 0:
        return False

    m = _position_metrics(ps)
    pnl_pct = m["pnl_pct"]
    hold_minutes = m["hold_minutes"]
    hard_exit_minutes = m["hard_exit_minutes"]

    # Current profit clears fees and leaves net gain — allow the signal sell.
    # Raised from 0.25% to 0.40%: matches new SCALP_SMALL_PROFIT_LOW.
    # 0.40% gross - 0.25% round-trip = 0.15% net minimum.
    if pnl_pct >= 0.40:
        return True

    # Trade peaked meaningfully and has retraced — protect what remains.
    # Peak must reach 0.50% (aligned with new TP1) before this gate fires.
    peak_gain = getattr(ps, "peak_gain_pct", 0.0)
    if peak_gain >= 0.50:
        return True

    # Time-based escape: stale trade with any green past the hold window
    if hold_minutes >= hard_exit_minutes and pnl_pct > 0:
        return True

    # Score-collapse escape: trade has meaningful gain but setup has deteriorated sharply.
    # Raised from 0.22% to 0.40% to keep above the new fee break-even level.
    eff = getattr(ps, "effective_score", 0.0)
    thr = ps.dynamic_params.get("effective_threshold", CONFIDENCE_BUY_THRESHOLD)
    if pnl_pct > 0.40 and eff < thr - 1.0:
        return True

    return False


def get_entry_block_reason(ps, state) -> str | None:
    """Return the first real execution blocker for a BUY, or None if tradable.

    Mirrors the pre-check order in execute_buy() so panel.py can display the
    same reason that execution would enforce, keeping dashboard truthful.
    """
    # 1) Never average down when price is below avg entry
    if ps.asset_balance > 0 and ps.buy_price > 0 and ps.current_price < ps.buy_price:
        return "add_blocked"

    # 2) Cooldown
    if ps.last_trade_time:
        try:
            last_time = datetime.fromisoformat(ps.last_trade_time)
            elapsed = (datetime.now(timezone.utc) - last_time).total_seconds()
            cooldown = (
                TRADE_COOLDOWN_TRENDING
                if ps.regime in {"trending", "trending_volatile"}
                else TRADE_COOLDOWN_SECONDS
            )
            if elapsed < cooldown:
                dynamic_threshold = ps.dynamic_params.get(
                    "effective_threshold", CONFIDENCE_BUY_THRESHOLD
                )
                can_bypass = (
                    ps.effective_score >= dynamic_threshold + COOLDOWN_BYPASS_CONFIDENCE
                    and (not COOLDOWN_BYPASS_REGIMES or ps.regime in COOLDOWN_BYPASS_REGIMES)
                )
                if not can_bypass:
                    return "cooldown"
        except (ValueError, TypeError):
            pass

    # 2b) Failed-exit re-entry suppression — pair-specific cooldown after weak/bad exits.
    # Prevents immediate churn (e.g. BUY → EARLY_STAGNATION → BUY → repeat).
    # This runs AFTER the normal cooldown so it can add extra time beyond the standard gate.
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
        if suppress_s > 0:
            try:
                ft = datetime.fromisoformat(failed_time)
                if ft.tzinfo is None:
                    ft = ft.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - ft).total_seconds() < suppress_s:
                    return "failed_exit_cooldown"
            except (ValueError, TypeError):
                pass

    # 3) Spike filter — single-tick explosive move
    if ps.prev_price > 0:
        tick_move = abs(ps.current_price - ps.prev_price) / ps.prev_price
        if tick_move > SPIKE_FILTER_PCT:
            return "spike_filter"

    # 3b) Unstable candle — last candle range is abnormally large vs ATR.
    # Catches multi-tick violent candles that each individually pass the spike filter.
    # ATR is the 14-period average range, so 2× means a genuine outlier candle.
    if ps.atr_pct > 0:
        candle_range_pct = getattr(ps, "candle_range_pct", 0.0)
        if candle_range_pct > ps.atr_pct * 100 * UNSTABLE_CANDLE_ATR_MULT:
            return "unstable_candle"

    # 3c) Economic viability hard block.
    # If the ATR-based expected reward (ATR × TP_multiplier) can't even cover the
    # round-trip execution cost (fees + slippage ≈ 0.25%), the trade is mathematically
    # negative in expectation regardless of signal quality. Hard block — no bypass.
    # ATR_TP_MULTIPLIER × atr_pct × 100 < ROUND_TRIP_COST_PCT → block.
    if ps.atr_pct > 0:
        atr_reward_pct = ps.atr_pct * ATR_TP_MULTIPLIER * 100
        if atr_reward_pct < ROUND_TRIP_COST_PCT:
            return "uneconomic_move"

    # 4) Spread filter (live only)
    if not USE_TESTNET and ps.bid_ask_spread_pct > SPREAD_MAX_PCT:
        return "spread_filter"

    # 5) Spendable capital
    spendable = state.usdt_balance - state.usdt_reserved
    if spendable <= 1.0:
        return "insufficient_spendable"

    # 6) Position room
    max_position = state.portfolio_total_usdt * MAX_POSITION_PCT
    current_position = ps.asset_balance * ps.current_price if ps.current_price > 0 else 0.0
    room = max_position - current_position
    if room <= 1.0:
        return "position_maxed"

    # 7) Min notional (accounting for all size modifiers)
    trade_size = state.portfolio_total_usdt * TRADE_SIZE_PCT
    size_modifier = get_trade_size_modifier(state)
    corr_modifier = get_correlation_modifier(ps, state)
    btc_cluster_modifier = get_btc_cluster_size_modifier(ps, state)
    crash_modifier = BTC_CRASH_SIZE_MULTIPLIER if state.btc_crash_active else 1.0
    downtrend_modifier = DOWNTREND_SIZE_MULTIPLIER if getattr(ps, "trend", "") == "downtrend" else 1.0
    trade_size *= size_modifier * corr_modifier * btc_cluster_modifier * crash_modifier * downtrend_modifier
    # Phase 4: Inverse volatility sizing — keep dollar risk flat across SL widths.
    # When ATR widens the SL (volatile market), shrink the position proportionally so
    # the max dollar loss stays constant.  vol_adj = 1.0 in quiet markets.
    if ps.atr_pct > 0:
        _atr_sl = min(ATR_SL_MAX_PCT, max(STOP_LOSS_PCT, ps.atr_pct * ATR_SL_MULTIPLIER * 100))
        trade_size *= STOP_LOSS_PCT / _atr_sl
    if min(trade_size, spendable, room) < MIN_TRADE_NOTIONAL:
        return "min_notional"

    # 8) FVG cooldown
    if ps.fvg_detected and ps.last_fvg_trade_time:
        try:
            last_fvg = datetime.fromisoformat(ps.last_fvg_trade_time)
            if last_fvg.tzinfo is None:
                last_fvg = last_fvg.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_fvg).total_seconds() < FVG_COOLDOWN_SECONDS:
                return "fvg_cooldown"
        except (ValueError, TypeError):
            pass

    return None




async def evaluate_and_trade(pair: str):
    """Evaluate signals and execute trade decision for a pair.

    Called on every RSI cycle (every 10s) after signals are updated.
    Uses adaptive weighted confidence engine.
    """
    if state.is_paused:
        return

    async with state.lock:
        ps = state.pairs[pair]

        if ps.asset_balance > 0 and not ps.position_open_time:
            ps.position_open_time = (
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat()
            log.warning(
                f"  {ps.base_asset} position_open_time backfilled "
                f"(stale position, synthetic age 2h — time exits will fire)"
            )

        decision, signal_snapshot = get_decision(ps, state)
        ps.decision = decision

        base = ps.base_asset
        price = ps.current_price
        rsi = ps.rsi
        regime = ps.regime
        conf_w = ps.confidence_weighted
        atr_pct = ps.atr_pct
        eff_score = ps.effective_score
        eff_threshold = ps.dynamic_params.get("effective_threshold", CONFIDENCE_BUY_THRESHOLD)
        forced_exit_reason = _get_forced_exit_reason(ps)

    if decision == "BUY":
        dec_color = f"{C.BOLD}{C.GREEN}{decision}{C.RESET}"
    elif decision == "SELL":
        dec_color = f"{C.BOLD}{C.RED}{decision}{C.RESET}"
    else:
        dec_color = f"{C.DIM}{decision}{C.RESET}"

    if rsi < 35:
        rsi_str = f"{C.GREEN}{rsi:<5.1f}{C.RESET}"
    elif rsi > 65:
        rsi_str = f"{C.RED}{rsi:<5.1f}{C.RESET}"
    else:
        rsi_str = f"{rsi:<5.1f}"

    regime_colors = {
        "trend": C.MAGENTA, "rangi": C.BLUE,
        "chopp": C.YELLOW,
    }
    r_color = regime_colors.get(regime[:5], C.WHITE)

    log.info(
        f"{C.BOLD}{C.WHITE}{base:<5}{C.RESET} ${price:<12,.2f} | "
        f"RSI {rsi_str} | "
        f"raw={C.CYAN}{conf_w:.1f}{C.RESET} eff={C.CYAN}{eff_score:.1f}{C.RESET}/{eff_threshold:.1f} | "
        f"{r_color}{regime[:5]}{C.RESET} ATR:{atr_pct:.3%} | -> {dec_color}"
    )

    if forced_exit_reason:
        await execute_sell(pair, note=forced_exit_reason, signal_snapshot=signal_snapshot)
        return

    async with state.lock:
        ps_check = state.pairs[pair]
        tp1_should_fire = (
            ps_check.asset_balance > 0
            and ps_check.buy_price > 0
            and not ps_check.tp1_hit
        )
        if tp1_should_fire:
            tp1_pct = ps_check.dynamic_params.get("tp1", BASE_TP1_PCT)
            gain_for_tp1 = (
                (ps_check.current_price - ps_check.buy_price)
                / ps_check.buy_price * 100
            )
        else:
            gain_for_tp1 = 0.0
            tp1_pct = BASE_TP1_PCT

    if tp1_should_fire and gain_for_tp1 >= tp1_pct:
        await execute_partial_sell(pair, TP1_SELL_RATIO)
        return

    async with state.lock:
        fvg_active = state.pairs[pair].fvg_limit_active
    if fvg_active:
        await check_fvg_order_fills(pair)
        return

    if decision == "BUY":
        await execute_buy(pair, signal_snapshot)
    elif decision == "SELL":
        async with state.lock:
            ps_sell = state.pairs[pair]
            sell_reason = ps_sell.dynamic_params.get("sell_reason", "signal")

        if sell_reason in ("early_failure", "risk_exit", "early_stagnation", "early_stagnation_2"):
            # Protective exits bypass the profit gate — cutting deteriorating/stagnant positions.
            # "risk_exit" reserved for future protective exits beyond early_failure.
            await execute_sell(pair, note=sell_reason.upper(), signal_snapshot=signal_snapshot)
        else:
            # Signal sells still gated by profit threshold to avoid clipping winners
            async with state.lock:
                ps_sell = state.pairs[pair]
                allow_sell = _should_allow_signal_sell(ps_sell)
            if allow_sell:
                await execute_sell(pair, note="SIGNAL_SELL", signal_snapshot=signal_snapshot)


def _clear_position_state(ps) -> None:
    """Reset all position fields to flat state.

    Called after a full position close (execute_sell) or dust guard.
    Centralised here so the same cleanup runs from both execute_sell and
    execute_partial_sell (when the last contracts are cleared).
    """
    ps.asset_balance    = 0.0
    ps.buy_price        = 0.0
    ps.trailing_stop    = 0.0
    ps.atr_take_profit  = 0.0
    ps.position_open_time = ""
    ps.tp1_hit          = False
    ps.fvg_entry        = False
    ps.fvg_detected     = False
    ps.fvg_limit_active = False
    ps.fvg_order_id     = 0
    ps.peak_gain_pct    = 0.0
    ps.position_side    = "none"   # mark flat — no reduceOnly exits until next entry
    ps.leverage         = 0


async def execute_buy(pair: str, signal_snapshot: dict | None = None):
    """Open a LONG USDⓈ-M futures position (scalping style)."""
    ps = state.pairs[pair]

    # Single canonical gate — all pre-buy blockers checked here.
    # get_entry_block_reason() is the shared source of truth; panel.py mirrors it.
    block_reason = get_entry_block_reason(ps, state)
    if block_reason is not None:
        log.debug(f"  {ps.base_asset} buy blocked: {block_reason}")
        return

    if state.btc_crash_active:
        log.info(
            f"  {C.YELLOW}{ps.base_asset} BTC crash mode{C.RESET} "
            f"— size reduced to {BTC_CRASH_SIZE_MULTIPLIER*100:.0f}%"
        )

    # Wick, min-edge, and R/R are score penalties in strategy.py — not hard gates here.

    spendable = state.usdt_balance - state.usdt_reserved
    if spendable <= 1.0:
        return

    trade_size = state.portfolio_total_usdt * TRADE_SIZE_PCT
    size_modifier = get_trade_size_modifier(state)
    corr_modifier = get_correlation_modifier(ps, state)
    btc_cluster_modifier = get_btc_cluster_size_modifier(ps, state)
    crash_modifier = BTC_CRASH_SIZE_MULTIPLIER if state.btc_crash_active else 1.0
    downtrend_modifier = DOWNTREND_SIZE_MULTIPLIER if getattr(ps, "trend", "") == "downtrend" else 1.0
    trade_size *= size_modifier * corr_modifier * btc_cluster_modifier * crash_modifier * downtrend_modifier
    # Phase 4: Inverse volatility sizing — constant dollar risk across all regimes.
    if ps.atr_pct > 0:
        _atr_sl = min(ATR_SL_MAX_PCT, max(STOP_LOSS_PCT, ps.atr_pct * ATR_SL_MULTIPLIER * 100))
        trade_size *= STOP_LOSS_PCT / _atr_sl

    max_position = state.portfolio_total_usdt * MAX_POSITION_PCT
    current_position = ps.asset_balance * ps.current_price if ps.current_price > 0 else 0
    room = max_position - current_position
    if room <= 1.0:
        log.info(f"  {ps.base_asset} position maxed out (${current_position:.0f}/${max_position:.0f})")
        return

    usdt_to_spend = min(trade_size, spendable, room)

    if usdt_to_spend < MIN_TRADE_NOTIONAL:
        log.debug(
            f"  {ps.base_asset} min notional: ${usdt_to_spend:.2f} "
            f"< ${MIN_TRADE_NOTIONAL:.0f} — skipping"
        )
        return

    is_fvg_early_entry = False
    if ps.fvg_detected and ps.fvg_buy_price > 0 and not ps.fvg_limit_active:
        gap_size_ok = (
            ps.current_price > 0
            and (ps.fvg_top - ps.fvg_bottom) / ps.current_price >= FVG_MIN_GAP_PCT
        )
        age_ok = True
        if ps.fvg_detected_time:
            try:
                det = datetime.fromisoformat(ps.fvg_detected_time)
                if det.tzinfo is None:
                    det = det.replace(tzinfo=timezone.utc)
                age_ok = (datetime.now(timezone.utc) - det).total_seconds() <= FVG_MAX_AGE_SECONDS
            except (ValueError, TypeError):
                pass

        if gap_size_ok and age_ok:
            fvg_in_cooldown = False
            if ps.last_fvg_trade_time:
                try:
                    last_fvg = datetime.fromisoformat(ps.last_fvg_trade_time)
                    if last_fvg.tzinfo is None:
                        last_fvg = last_fvg.replace(tzinfo=timezone.utc)
                    fvg_in_cooldown = (
                        datetime.now(timezone.utc) - last_fvg
                    ).total_seconds() < FVG_COOLDOWN_SECONDS
                except (ValueError, TypeError):
                    pass

            if not fvg_in_cooldown:
                ob_ok = ps.orderbook_imbalance >= FVG_OB_MIN
                flow_ok = ps.flow_ratio >= FVG_OB_MIN

                if ob_ok and flow_ok:
                    rsi_sig_fvg = 1.0 if ps.rsi < RSI_BUY_THRESHOLD else 0.0
                    bb_sig_fvg = 1.0 if ps.bb_position == "below_lower" else 0.0
                    fast_score_fvg = (
                        rsi_sig_fvg + bb_sig_fvg
                        + getattr(ps, "liquidity_sweep_score", 0.0)
                    )

                    if fast_score_fvg >= FVG_FAST_SCORE_MIN:
                        if ps.current_price > ps.fvg_buy_price:
                            await execute_fvg_entry(pair, usdt_to_spend)
                            return
                        elif ps.current_price > ps.fvg_bottom:
                            log.info(
                                f"{C.CYAN}{C.BOLD} FVG EARLY {C.RESET} "
                                f"{C.CYAN}{ps.base_asset}{C.RESET} "
                                f"price ${ps.current_price:.4f} ≤ mid ${ps.fvg_buy_price:.4f} "
                                f"(gap ${ps.fvg_bottom:.4f}–${ps.fvg_top:.4f}) | "
                                f"fast={fast_score_fvg:.1f} ob={ps.orderbook_imbalance:.2f} "
                                f"flow={ps.flow_ratio:.2f}"
                            )
                            is_fvg_early_entry = True

    # ── Futures execution: calculate contract size from ATR-based SL ──────
    # Dynamic SL price mirrors the Triple Barrier lower barrier (Phase 2).
    if ps.atr_pct > 0:
        _atr_sl_pct = min(ATR_SL_MAX_PCT, max(STOP_LOSS_PCT, ps.atr_pct * ATR_SL_MULTIPLIER * 100))
    else:
        _atr_sl_pct = STOP_LOSS_PCT

    entry_price_est = ps.current_price
    sl_price = entry_price_est * (1 - _atr_sl_pct / 100)

    # Re-query live futures USDT balance for accurate sizing
    live_usdt = await futures_execution.get_futures_usdt_balance()
    if live_usdt <= 0:
        live_usdt = state.usdt_balance  # fallback to last known

    contracts = futures_execution.calculate_futures_position_size(
        live_usdt, entry_price_est, sl_price
    )

    if contracts <= 0:
        log.debug(f"  {ps.base_asset} futures contract size = 0 — skipping")
        return

    order = await futures_execution.execute_futures_entry(pair, "buy", contracts, FUTURES_LEVERAGE)
    if order is None:
        return

    filled_qty  = float(order.get("filled") or order.get("amount") or contracts)
    avg_price   = float(order.get("average") or order.get("price") or entry_price_est)
    if avg_price <= 0:
        avg_price = entry_price_est
    filled_usdt = filled_qty * avg_price

    # Re-sync USDT balance after order (margin has been locked)
    new_usdt = await futures_execution.get_futures_usdt_balance()

    async with state.lock:
        had_position = ps.asset_balance > 0

        old_cost = ps.asset_balance * ps.buy_price if ps.buy_price > 0 else 0
        ps.asset_balance += filled_qty
        total_cost = old_cost + filled_usdt
        ps.buy_price = total_cost / ps.asset_balance if ps.asset_balance > 0 else avg_price
        ps.position_value_usdt = ps.asset_balance * avg_price
        ps.position_side = "long"          # track direction for reduceOnly exits
        ps.leverage = FUTURES_LEVERAGE

        if USE_ATR_EXITS and ps.atr > 0:
            initial_trail = avg_price - ps.atr * ATR_TRAILING_MULTIPLIER
            hard_stop = avg_price * (1 - STOP_LOSS_PCT / 100)
            ps.trailing_stop = max(initial_trail, hard_stop)
            ps.atr_take_profit = avg_price + ps.atr * ATR_TP_MULTIPLIER

        now_iso = datetime.now(timezone.utc).isoformat()
        ps.last_trade = {
            "action": "BUY", "price": avg_price,
            "amount": filled_qty, "usdt": filled_usdt,
            "time": now_iso,
        }
        ps.last_trade_time = now_iso

        if not had_position:
            ps.position_open_time = now_iso
            ps.tp1_hit = False

        ps.fvg_entry = is_fvg_early_entry
        if is_fvg_early_entry:
            ps.last_fvg_trade_time = now_iso

        ps.fvg_detected = False
        ps.fvg_limit_active = False
        ps.fvg_order_id = 0

        # Update USDT balance — prefer live re-query, fallback to margin estimate
        if new_usdt > 0:
            state.usdt_balance = new_usdt
        else:
            margin_used = filled_usdt / FUTURES_LEVERAGE
            state.usdt_balance = max(0.0, state.usdt_balance - margin_used)
        state.total_trades += 1
        state.total_buys += 1

    fee_est = filled_usdt * FUTURES_TAKER_FEE
    exit_info = ""
    if USE_ATR_EXITS and ps.atr > 0:
        exit_info = f" | Trail: ${ps.trailing_stop:.2f}  TP: ${ps.atr_take_profit:.2f}"
    log.info(
        f"{C.BG_GREEN}{C.WHITE}{C.BOLD} BUY {C.RESET} "
        f"{C.GREEN}{ps.base_asset:<5}{C.RESET} {filled_qty:.6f} {ps.base_asset}  "
        f"for {C.GREEN}{filled_usdt:.2f} USDT{C.RESET} | "
        f"{C.DIM}Fee: ~{fee_est:.2f}{C.RESET} | Eff: {ps.effective_score:.2f}{exit_info}"
    )

    log_trade(
        pair=pair, action="BUY", coin_price=avg_price,
        amount_coin=filled_qty, amount_usdt=filled_usdt,
        rsi=ps.rsi, confidence=ps.confidence_score,
        signal_snapshot=signal_snapshot,
        regime_at_entry=ps.regime,
        atr_value=ps.atr,
        confidence_weighted=ps.confidence_weighted,
    )

    if discord_notify_callback:
        discord_notify_callback("BUY", pair, ps, state)

    update_portfolio_tracking(state)
    await asyncio.get_running_loop().run_in_executor(None, save_state_to_db, state)


async def execute_sell(pair: str, note: str = "", signal_snapshot: dict | None = None):
    """Close the open LONG futures position for a pair (reduceOnly market order)."""
    ps = state.pairs[pair]
    if ps.asset_balance <= 0 or ps.position_side == "none":
        return

    position_side = ps.position_side    # "long" (or "short" when SHORT signals are added)
    sell_amount   = ps.asset_balance
    current_price = ps.current_price

    # Dust guard: sub-$5 notional positions have no fillable value on futures
    if current_price > 0 and sell_amount * current_price < 5.0:
        async with state.lock:
            log.warning(
                f"  {ps.base_asset} dust pre-cleared "
                f"(${sell_amount * current_price:.4f} < $5 min notional — removing from state)"
            )
            _clear_position_state(ps)
        update_portfolio_tracking(state)
        return

    order = await futures_execution.execute_futures_exit(pair, position_side, sell_amount)

    if order is None:
        # Exit call failed — clear state anyway to prevent zombie positions.
        # Log loudly; operator should verify on exchange.
        log.error(
            f"  {ps.base_asset} futures exit FAILED — clearing local state. "
            f"VERIFY POSITION ON EXCHANGE MANUALLY."
        )
        async with state.lock:
            _clear_position_state(ps)
        return

    filled_qty  = float(order.get("filled") or sell_amount)
    avg_price   = float(order.get("average") or current_price)
    if avg_price <= 0:
        avg_price = current_price
    filled_usdt = filled_qty * avg_price

    # Capture entry_time before the lock block clears ps.position_open_time
    entry_time = ps.position_open_time or ""
    hold_minutes = 0.0
    if entry_time:
        try:
            opened = datetime.fromisoformat(entry_time)
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            hold_minutes = (datetime.now(timezone.utc) - opened).total_seconds() / 60
        except Exception:
            pass

    cost_basis = ps.buy_price * filled_qty if ps.buy_price > 0 else 0
    # P/L for LONG: profit when exit price > entry price.
    # For SHORT exits the sign is reversed — handled when SHORT signals are added.
    pl = filled_usdt - cost_basis
    pl_pct = (pl / cost_basis * 100) if cost_basis > 0 else 0.0

    # Re-sync futures USDT balance after the exit (margin returned + P/L settled)
    new_usdt = await futures_execution.get_futures_usdt_balance()

    async with state.lock:
        ps.asset_balance -= filled_qty
        if ps.asset_balance < 1e-6:
            _clear_position_state(ps)
        ps.position_value_usdt = ps.asset_balance * avg_price
        ps.pl_usdt += pl
        ps.pl_pct = pl_pct
        now_iso = datetime.now(timezone.utc).isoformat()
        ps.last_trade = {
            "action": note or "SELL",
            "price": avg_price,
            "amount": filled_qty,
            "usdt": filled_usdt,
            "pl": pl,
            "pl_pct": pl_pct,
            "hold_minutes": hold_minutes,
            "time": now_iso,
        }
        ps.last_trade_time = now_iso

        # Use live balance; fall back to estimate if re-query failed
        if new_usdt > 0:
            state.usdt_balance = new_usdt
        else:
            state.usdt_balance += filled_usdt
        state.total_trades += 1
        state.total_sells += 1
        if pl > 0:
            state.profitable_trades += 1

        # Failed-exit re-entry suppression: record reason + time so the next
        # BUY attempt on this pair is held off for the configured window.
        _FAILED_EXIT_REASONS = {
            "EARLY_STAGNATION", "EARLY_STAGNATION_2", "TRAIL_STOP", "HARD_STOP"
        }
        _action_label_for_suppress = note or "SELL"
        if _action_label_for_suppress in _FAILED_EXIT_REASONS:
            ps.last_failed_exit_reason = _action_label_for_suppress
            ps.last_failed_exit_time   = now_iso

    action_label = note or "SELL"
    pl_color = C.GREEN if pl >= 0 else C.RED
    bg = C.BG_RED if ("STOP" in action_label or pl < 0) else C.BG_YELLOW
    log.info(
        f"{bg}{C.WHITE}{C.BOLD} {action_label} {C.RESET} "
        f"{C.RED}{ps.base_asset:<5}{C.RESET} {filled_qty:.6f} {ps.base_asset}  "
        f"for {filled_usdt:.2f} USDT | "
        f"P/L: {pl_color}{C.BOLD}{pl:+.4f} USDT ({pl_pct:+.2f}%){C.RESET} | "
        f"Hold: {hold_minutes:.1f}m"
    )

    log_trade(
        pair=pair, action=action_label, coin_price=avg_price,
        amount_coin=filled_qty, amount_usdt=filled_usdt,
        rsi=ps.rsi, confidence=ps.confidence_score,
        pl_usdt=pl, note=note,
        signal_snapshot=signal_snapshot,
        regime_at_entry=ps.regime,
        atr_value=ps.atr,
        confidence_weighted=ps.confidence_weighted,
        hold_minutes=hold_minutes,
        entry_time=entry_time,
    )

    if discord_notify_callback:
        discord_notify_callback(action_label, pair, ps, state, pl)

    update_portfolio_tracking(state)
    await asyncio.get_running_loop().run_in_executor(None, save_state_to_db, state)


async def execute_partial_sell(pair: str, fraction: float):
    """Close `fraction` (0–1) of the open LONG position for TP1 ladder.

    Uses a reduceOnly market order so a retry cannot reverse into a Short.
    After a successful TP1 partial close:
      - Sets ps.tp1_hit = True
      - Moves stop-loss to entry + BREAKEVEN_BUFFER_PCT (breakeven lock)

    Falls back to a full exit if the partial notional is below $5.
    """
    ps = state.pairs[pair]
    if ps.asset_balance <= 0 or ps.buy_price <= 0 or ps.position_side == "none":
        return

    current_price = ps.current_price
    sell_qty_raw  = ps.asset_balance * fraction

    # Format to valid contract precision using CCXT
    sell_qty = await futures_execution.format_futures_amount(pair, sell_qty_raw)

    if sell_qty <= 0:
        log.warning(f"  {ps.base_asset} TP1 partial sell rounds to 0 — skipping")
        return

    notional = sell_qty * current_price
    if notional < 5.0:
        log.warning(
            f"  {ps.base_asset} TP1 below $5 notional "
            f"({sell_qty} × {current_price:.4f} = ${notional:.2f}) — converting to full exit"
        )
        await execute_sell(pair, note="TP1_FULL_EXIT")
        return

    order = await futures_execution.execute_futures_exit(pair, ps.position_side, sell_qty)

    if order is None:
        log.warning(f"  {ps.base_asset} TP1 partial exit order failed — skipping TP1")
        return

    filled_qty  = float(order.get("filled") or sell_qty)
    avg_price   = float(order.get("average") or current_price)
    if avg_price <= 0:
        avg_price = current_price
    filled_usdt = filled_qty * avg_price

    cost_basis = ps.buy_price * filled_qty
    pl = filled_usdt - cost_basis

    # Re-sync futures balance after partial close
    new_usdt = await futures_execution.get_futures_usdt_balance()

    async with state.lock:
        ps.asset_balance -= filled_qty
        if ps.asset_balance < 1e-6:
            _clear_position_state(ps)
        ps.position_value_usdt = ps.asset_balance * avg_price
        ps.pl_usdt += pl
        ps.tp1_hit = True
        # Move stop-loss to break-even + buffer
        be_stop = ps.buy_price * (1 + BREAKEVEN_BUFFER_PCT / 100)
        ps.trailing_stop = max(ps.trailing_stop, be_stop)
        now_iso = datetime.now(timezone.utc).isoformat()
        ps.last_trade = {
            "action": "TP1_SELL", "price": avg_price,
            "amount": filled_qty, "usdt": filled_usdt,
            "pl": pl, "time": now_iso,
        }
        if new_usdt > 0:
            state.usdt_balance = new_usdt
        else:
            state.usdt_balance += filled_usdt
        state.total_trades += 1
        state.total_sells += 1
        if pl > 0:
            state.profitable_trades += 1

    pl_color = C.GREEN if pl >= 0 else C.RED
    log.info(
        f"{C.BG_GREEN}{C.WHITE}{C.BOLD} TP1 {C.RESET} "
        f"{C.GREEN}{ps.base_asset:<5}{C.RESET} sold {fraction*100:.0f}% "
        f"({filled_qty:.6f}) for {C.GREEN}{filled_usdt:.2f} USDT{C.RESET} | "
        f"P/L: {pl_color}{C.BOLD}{pl:+.4f} USDT{C.RESET} | "
        f"SL → entry+{BREAKEVEN_BUFFER_PCT:.1f}%"
    )

    update_portfolio_tracking(state)
    await asyncio.get_running_loop().run_in_executor(None, save_state_to_db, state)


async def execute_fvg_entry(pair: str, usdt_to_spend: float):
    """Place a futures LIMIT LONG at the FVG midpoint.

    Parks a GTC limit order at the 50% gap level on the USDⓈ-M futures market.
    check_fvg_order_fills() monitors fill status and updates position state.

    Contract size is derived from risk-parity sizing at the limit price,
    using the ATR dynamic SL as the risk distance.
    """
    ps = state.pairs[pair]
    if not ps.fvg_detected or ps.fvg_buy_price <= 0:
        return

    limit_price = ps.fvg_buy_price

    # Compute dynamic SL for sizing (same as execute_buy)
    if ps.atr_pct > 0:
        _atr_sl_pct = min(ATR_SL_MAX_PCT, max(STOP_LOSS_PCT, ps.atr_pct * ATR_SL_MULTIPLIER * 100))
    else:
        _atr_sl_pct = STOP_LOSS_PCT
    sl_price = limit_price * (1 - _atr_sl_pct / 100)

    live_usdt = await futures_execution.get_futures_usdt_balance()
    if live_usdt <= 0:
        live_usdt = state.usdt_balance

    contracts = futures_execution.calculate_futures_position_size(
        live_usdt, limit_price, sl_price
    )

    if contracts <= 0 or (contracts * limit_price) < 5.0:
        log.debug(
            f"  {ps.base_asset} FVG limit too small "
            f"({contracts:.6f} × {limit_price:.4f} = ${contracts * limit_price:.2f}) — skipping"
        )
        return

    order = await futures_execution.execute_futures_limit_entry(
        pair, "buy", contracts, limit_price, FUTURES_LEVERAGE
    )

    if order is None:
        return

    order_id = int(order.get("id", 0))
    now_iso = datetime.now(timezone.utc).isoformat()
    async with state.lock:
        ps.fvg_limit_active = True
        ps.fvg_order_id = order_id
        ps.fvg_order_time = now_iso

    log.info(
        f"{C.CYAN}{C.BOLD} FVG LIMIT {C.RESET} "
        f"{C.CYAN}{ps.base_asset}{C.RESET} limit long {contracts:.6f} contracts "
        f"@ ${limit_price:.4f} (gap: ${ps.fvg_bottom:.4f}–${ps.fvg_top:.4f}) "
        f"orderId={order_id}"
    )


async def check_fvg_order_fills(pair: str):
    """Check if a pending FVG futures limit order has been filled, cancelled, or stale.

    Uses CCXT futures order management (futures_execution module).
    CCXT unified order fields: status='open'|'closed'|'canceled', filled, average.
    """
    ps = state.pairs[pair]
    if not ps.fvg_limit_active or ps.fvg_order_id == 0:
        return

    status = await futures_execution.get_futures_order_status(pair, ps.fvg_order_id)
    if status is None:
        return

    order_status = status.get("status", "")    # 'open' | 'closed' | 'canceled'

    # ── Order timeout: cancel if limit has been sitting unfilled too long ──
    if order_status == "open" and ps.fvg_order_time:
        try:
            placed = datetime.fromisoformat(ps.fvg_order_time)
            if placed.tzinfo is None:
                placed = placed.replace(tzinfo=timezone.utc)
            age_s = (datetime.now(timezone.utc) - placed).total_seconds()
            if age_s > FVG_ORDER_TIMEOUT:
                cancelled = await futures_execution.cancel_futures_order(pair, ps.fvg_order_id)
                if cancelled:
                    async with state.lock:
                        ps.fvg_limit_active = False
                        ps.fvg_order_id = 0
                        ps.fvg_order_time = ""
                    log.info(
                        f"  {ps.base_asset} FVG futures limit cancelled — "
                        f"order open {age_s:.0f}s > {FVG_ORDER_TIMEOUT}s timeout"
                    )
                return
        except Exception:
            pass

    if order_status == "closed":    # CCXT 'closed' = fully filled
        filled_qty  = float(status.get("filled", 0))
        avg_price   = float(status.get("average") or ps.fvg_buy_price)
        filled_usdt = filled_qty * avg_price

        # Re-sync futures balance after fill
        new_usdt = await futures_execution.get_futures_usdt_balance()

        async with state.lock:
            old_cost = ps.asset_balance * ps.buy_price if ps.buy_price > 0 else 0
            ps.asset_balance += filled_qty
            total_cost = old_cost + filled_usdt
            ps.buy_price = total_cost / ps.asset_balance if ps.asset_balance > 0 else avg_price
            ps.position_value_usdt = ps.asset_balance * avg_price
            ps.position_side = "long"
            ps.leverage = FUTURES_LEVERAGE

            if USE_ATR_EXITS and ps.atr > 0:
                ps.trailing_stop   = avg_price - ps.atr * ATR_TRAILING_MULTIPLIER
                ps.atr_take_profit = avg_price + ps.atr * ATR_TP_MULTIPLIER

            now_iso = datetime.now(timezone.utc).isoformat()
            ps.position_open_time = now_iso
            ps.tp1_hit = False
            ps.fvg_entry = True
            ps.last_fvg_trade_time = now_iso
            ps.last_trade = {
                "action": "BUY_FVG", "price": avg_price,
                "amount": filled_qty, "usdt": filled_usdt,
                "time": now_iso,
            }
            ps.last_trade_time = now_iso
            ps.fvg_limit_active = False
            ps.fvg_order_id = 0
            ps.fvg_order_time = ""
            ps.fvg_detected = False

            if new_usdt > 0:
                state.usdt_balance = new_usdt
            else:
                margin_used = filled_usdt / FUTURES_LEVERAGE
                state.usdt_balance = max(0.0, state.usdt_balance - margin_used)
            state.total_trades += 1
            state.total_buys += 1

        log.info(
            f"{C.BG_GREEN}{C.WHITE}{C.BOLD} FVG FILLED {C.RESET} "
            f"{C.GREEN}{ps.base_asset}{C.RESET} {filled_qty:.6f} contracts "
            f"@ ${avg_price:.4f} ≈ ${filled_usdt:.2f} USDT notional"
        )
        update_portfolio_tracking(state)
        await asyncio.get_running_loop().run_in_executor(None, save_state_to_db, state)

    elif order_status == "canceled":
        async with state.lock:
            ps.fvg_limit_active = False
            ps.fvg_order_id = 0
            ps.fvg_order_time = ""
        log.info(f"  {ps.base_asset} FVG futures limit order canceled — cleared")

    else:
        # Order still open — cancel if price moved >2% above gap_top (opportunity passed)
        if ps.fvg_top > 0 and ps.current_price > ps.fvg_top * 1.02:
            cancelled = await futures_execution.cancel_futures_order(pair, ps.fvg_order_id)
            if cancelled:
                async with state.lock:
                    ps.fvg_limit_active = False
                    ps.fvg_order_id = 0
                    ps.fvg_order_time = ""
                log.info(
                    f"  {ps.base_asset} FVG futures limit cancelled — price ${ps.current_price:.4f} "
                    f"moved >2% above gap_top ${ps.fvg_top:.4f}"
                )


# ─── Long-Term Portfolio Layer ───────────────────────────────────────────
# Completely separate from the scalping system.
# Uses the same effective_score / trend / regime signals already computed
# each RSI cycle — no new indicators, just stricter thresholds and
# much lower evaluation frequency (LONG_TERM_CHECK_INTERVAL, default 5 min).
#
# Capital separation:
#   lt_capital_budget  = total × LONG_TERM_ALLOCATION   (soft budget, LT respects it)
#   lt_capital_deployed = running cost-basis total of open LT positions
#   The scalping system uses state.usdt_balance - state.usdt_reserved as before;
#   usdt_reserved is set at startup to cover only the TRADING portion's reserve,
#   so LT deployments naturally reduce USDT available for scalping.
#
# Position separation:
#   LongTermState.quantity tracks LT coins independently of ps.asset_balance.
#   At startup, LT quantities are subtracted from ps.asset_balance so the two
#   systems never double-count the same coins.


def _lt_per_symbol_budget() -> float:
    """Max USDT to deploy per LT symbol = budget ÷ number of LT assets."""
    n = max(1, len(LONG_TERM_ASSETS))
    return state.lt_capital_budget / n


def _evaluate_lt_entry(pair: str) -> bool:
    """Return True if conditions allow opening a new long-term position.

    Called inside long_term_loop — state.lock is NOT held when this runs.
    Reads are safe (floats/strings are atomic in CPython) but no writes.

    Design intent (true long-term, not a slow scalp):
    - Entry threshold is LOW enough to allow accumulation in good-not-perfect conditions.
    - Short-term downtrend is NOT a blocker — BTC routinely dips before continuing higher.
      The only downtrend block is when the score is also very weak (genuine bear signal).
    - Choppy regime is not blocked — regime labels fluctuate on 15-min candles and have
      no predictive value over the hours-to-days window this layer targets.
    - Capital gate is lenient: enter if at least 20% of per-symbol budget is available.
    - Hard blocks: BTC crash active (macro risk-off), or near-zero spendable capital.
    """
    if not LONG_TERM_ENABLED:
        return False
    lt = state.long_term_states.get(pair)
    if lt is None or lt.holding:
        return False
    ps = state.pairs.get(pair)
    if ps is None:
        return False

    eff = ps.effective_score

    # Score gate — must meet minimum threshold (accumulation-friendly level)
    if eff < LONG_TERM_ENTRY_THRESHOLD:
        return False

    # Hard block: active BTC crash is a macro risk-off event
    # (distinct from a short-term downtrend — crash means systemic selling)
    if state.btc_crash_active:
        return False

    # Downtrend only blocked when score is ALSO weak — avoids buying into a
    # collapsing setup, but allows accumulation on healthy dips
    if ps.trend == "downtrend" and eff < LONG_TERM_ENTRY_THRESHOLD - 0.3:
        return False

    # Capital gate: at least 20% of per-symbol budget still available
    remaining = state.lt_capital_budget - state.lt_capital_deployed
    if remaining < _lt_per_symbol_budget() * 0.20:
        return False

    # Real wallet gate
    spendable = state.usdt_balance - state.usdt_reserved
    if spendable < MIN_TRADE_NOTIONAL:
        return False

    return True


def _evaluate_lt_exit(pair: str) -> tuple[bool, str]:
    """Return (should_exit, reason) for a held LT position.

    Does NOT use TP1, trailing stop, stagnation, or time exits — those are
    scalping constructs only.  LT exits only when there is a genuine, sustained
    reason to de-risk — not on normal short-term noise.

    Exit conditions (in priority order):
    1. Active BTC crash + score is already weak → macro emergency exit
    2. Sustained downtrend: LONG_TERM_DOWNTREND_EXIT_CYCLES consecutive cycles
       (at 10-min intervals, default 8 × 10 min = 80 min of unbroken downtrend)
    3. Score collapse: effective_score below LONG_TERM_EXIT_THRESHOLD (default 1.2)
       This only fires on genuine weakness, not brief dips.
    """
    lt = state.long_term_states.get(pair)
    if lt is None or not lt.holding or lt.quantity <= 0:
        return False, ""
    ps = state.pairs.get(pair)
    if ps is None:
        return False, ""

    eff = ps.effective_score

    # 1. BTC crash active AND score is already weak — don't hold through a macro dump
    if state.btc_crash_active and eff < LONG_TERM_EXIT_THRESHOLD + 0.3:
        return True, "LT_CRASH_EXIT"

    # 2. Sustained downtrend over many consecutive evaluation cycles
    if lt.consecutive_downtrend_cycles >= LONG_TERM_DOWNTREND_EXIT_CYCLES:
        return True, "LT_DOWNTREND"

    # 3. Score collapse — only genuine collapse fires this (threshold = 1.2)
    if eff < LONG_TERM_EXIT_THRESHOLD:
        return True, "LT_SCORE_EXIT"

    return False, ""


async def execute_long_term_buy(pair: str):
    """Place a market buy for the long-term portfolio layer.

    Completely separate execution path — no trailing stop, no TP1, no
    stagnation logic.  Updates LongTermState and state.lt_capital_deployed.
    """
    lt = state.long_term_states.get(pair)
    if lt is None or lt.holding:
        return
    ps = state.pairs.get(pair)
    if ps is None:
        return

    per_symbol = _lt_per_symbol_budget()
    remaining  = state.lt_capital_budget - state.lt_capital_deployed
    spendable  = state.usdt_balance - state.usdt_reserved
    usdt_to_spend = min(per_symbol, remaining, spendable)

    if usdt_to_spend < MIN_TRADE_NOTIONAL:
        log.debug(f"  LT {pair} skipped — too small (${usdt_to_spend:.2f})")
        return

    try:
        order = await binance.place_market_buy(pair, usdt_to_spend)
    except ExecutionError as e:
        log.warning(f"  LT {pair} buy ExecutionError: {e}")
        return
    if order is None:
        return

    filled_qty   = float(order.get("executedQty", 0))
    filled_usdt  = float(order.get("cummulativeQuoteQty", 0))
    avg_price    = filled_usdt / filled_qty if filled_qty > 0 else ps.current_price

    now = datetime.now(timezone.utc)
    async with state.lock:
        lt.holding      = True
        lt.entry_price  = avg_price
        lt.quantity     = filled_qty
        lt.last_action_ts = now.timestamp()
        lt.entry_time   = now.isoformat()
        lt.consecutive_downtrend_cycles = 0
        state.lt_capital_deployed += filled_usdt
        state.usdt_balance -= filled_usdt

    log.info(
        f"{C.BG_GREEN}{C.WHITE}{C.BOLD} LT BUY {C.RESET} "
        f"{C.GREEN}{pair}{C.RESET}  {filled_qty:.6f} @ ${avg_price:,.4f} "
        f"for ${filled_usdt:.2f} USDT  "
        f"[deployed ${state.lt_capital_deployed:.2f} / ${state.lt_capital_budget:.2f}]"
    )

    log_trade(
        pair=pair, action="LT_BUY", coin_price=avg_price,
        amount_coin=filled_qty, amount_usdt=filled_usdt,
        rsi=ps.rsi, confidence=ps.confidence_score,
        pl_usdt=0.0, note="long_term",
        signal_snapshot=None,
        regime_at_entry=ps.regime, atr_value=ps.atr,
        confidence_weighted=ps.confidence_weighted,
        hold_minutes=0.0, entry_time="",
    )
    await asyncio.get_running_loop().run_in_executor(None, save_state_to_db, state)


async def execute_long_term_sell(pair: str, reason: str = "LT_SELL"):
    """Sell the entire long-term position for a symbol.

    Uses only lt_state.quantity — does not touch ps.asset_balance (scalp layer).
    """
    lt = state.long_term_states.get(pair)
    if lt is None or not lt.holding or lt.quantity <= 0:
        return
    ps = state.pairs.get(pair)
    if ps is None:
        return

    sell_amount = lt.quantity

    try:
        order = await binance.place_market_sell(pair, sell_amount, current_price=ps.current_price)
    except ExecutionError as e:
        log.warning(f"  LT {pair} sell ExecutionError: {e}")
        return
    if order is None:
        return

    filled_qty   = float(order.get("executedQty", 0))
    filled_usdt  = float(order.get("cummulativeQuoteQty", 0))
    avg_price    = filled_usdt / filled_qty if filled_qty > 0 else ps.current_price

    cost_basis = lt.entry_price * filled_qty if lt.entry_price > 0 else 0.0
    pl         = filled_usdt - cost_basis
    pl_pct     = (pl / cost_basis * 100) if cost_basis > 0 else 0.0
    entry_time = lt.entry_time

    # Hold time for logging
    hold_minutes = 0.0
    if entry_time:
        try:
            opened = datetime.fromisoformat(entry_time)
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            hold_minutes = (datetime.now(timezone.utc) - opened).total_seconds() / 60
        except Exception:
            pass

    now = datetime.now(timezone.utc)
    async with state.lock:
        lt.holding      = False
        lt.quantity     = 0.0
        lt.entry_price  = 0.0
        lt.last_action_ts = now.timestamp()
        lt.consecutive_downtrend_cycles = 0
        state.lt_capital_deployed = max(0.0, state.lt_capital_deployed - cost_basis)
        state.usdt_balance += filled_usdt

    pl_color = C.GREEN if pl >= 0 else C.RED
    log.info(
        f"{C.BG_YELLOW}{C.WHITE}{C.BOLD} LT SELL {C.RESET} "
        f"{pair}  {filled_qty:.6f} @ ${avg_price:,.4f}  "
        f"P/L: {pl_color}{pl:+.4f} USDT ({pl_pct:+.2f}%){C.RESET}  [{reason}]"
    )

    log_trade(
        pair=pair, action=reason, coin_price=avg_price,
        amount_coin=filled_qty, amount_usdt=filled_usdt,
        rsi=ps.rsi, confidence=ps.confidence_score,
        pl_usdt=pl, note="long_term",
        signal_snapshot=None,
        regime_at_entry=ps.regime, atr_value=ps.atr,
        confidence_weighted=ps.confidence_weighted,
        hold_minutes=hold_minutes, entry_time=entry_time,
    )
    await asyncio.get_running_loop().run_in_executor(None, save_state_to_db, state)


async def long_term_loop():
    """Long-term portfolio evaluation loop.

    Runs every LONG_TERM_CHECK_INTERVAL seconds (default 5 min).
    Completely separate from trading_loop — different thresholds,
    different exits, different capital pool.
    """
    # Initial delay: let all signal pollers populate ps.effective_score first
    await asyncio.sleep(60)
    while True:
        if LONG_TERM_ENABLED and not state.is_paused:
            for pair in LONG_TERM_ASSETS:
                if pair not in state.pairs or pair not in state.long_term_states:
                    continue
                try:
                    ps = state.pairs[pair]
                    lt = state.long_term_states[pair]

                    # Update downtrend counter under lock (atomic write)
                    async with state.lock:
                        if ps.trend == "downtrend":
                            lt.consecutive_downtrend_cycles += 1
                        else:
                            lt.consecutive_downtrend_cycles = 0

                    if lt.holding:
                        should_exit, reason = _evaluate_lt_exit(pair)
                        if should_exit:
                            await execute_long_term_sell(pair, reason=reason)
                    else:
                        if _evaluate_lt_entry(pair):
                            await execute_long_term_buy(pair)

                except Exception as e:
                    log.error(f"Long-term loop error for {pair}: {e}")

        await asyncio.sleep(LONG_TERM_CHECK_INTERVAL)


# ─── Main Trading Loop ───────────────────────────────────────────────

async def real_time_execution_worker():
    """Event-driven trade evaluator — reacts to WebSocket price ticks instantly.

    Replaces the timer-based trading_loop.  Execution latency is now bounded
    only by asyncio scheduling + lock acquisition, not by a sleep interval.

    Flow:
      1. BinanceClient._price_stream_loop updates ps.current_price on every tick.
      2. If the pair is not already pending, it enqueues the pair symbol.
      3. This worker dequeues and calls evaluate_and_trade() immediately.
      4. The dedup set in BinanceClient ensures each pair appears at most once
         in the queue — fast ticks between evaluations are intentionally dropped
         because evaluate_and_trade always reads the live ps.current_price, so
         it always acts on the freshest price regardless of how many ticks fired.

    update_portfolio_tracking() is called after each evaluation so portfolio
    P/L, drawdown, and position counts stay current at tick resolution.
    """
    log.info("Real-time execution worker started")
    while True:
        try:
            pair = await binance.price_event_queue.get()
            # Clear the dedup entry so the next tick for this pair can be queued
            binance._queued_pairs.discard(pair)
            try:
                await evaluate_and_trade(pair)
                update_portfolio_tracking(state)
            except Exception as e:
                log.error(f"Execution worker error for {pair}: {e}")
            finally:
                binance.price_event_queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"Execution worker fatal error: {e}")


# ─── Startup ──────────────────────────────────────────────────────────

async def start():
    """Main startup sequence."""
    global state, discord_notify_callback

    # Validate API keys
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        log.error("Missing BINANCE_API_KEY or BINANCE_SECRET_KEY in .env")
        return
    if not DISCORD_BOT_TOKEN:
        log.warning("Missing DISCORD_BOT_TOKEN — Discord bot will not start")

    from config import DISCORD_OWNER_ID
    if DISCORD_OWNER_ID == 0:
        log.warning("DISCORD_OWNER_ID not set — all Discord buttons will be locked out!")

    # Initialize database
    init_db()

    # Load state from DB or create fresh
    saved_state = load_state_from_db()
    if saved_state is not None:
        state = saved_state
        log.info("Restored state from trades.db")
        # ── Phase 8: WAL Recovery Audit ───────────────────────────────────────
        # After a crash or kill -9, list every open position recovered from WAL.
        # Proves the SQLite WAL survived the termination and position_side is intact.
        recovered = [
            (pair, ps)
            for pair, ps in state.pairs.items()
            if ps.asset_balance > 0 and ps.position_side != "none"
        ]
        if recovered:
            log.warning(
                f"{'─'*60}\n"
                f"  ⚡ WAL RECOVERY: {len(recovered)} open position(s) restored\n"
                + "".join(
                    f"  → {pair}: {ps.position_side.upper()} "
                    f"{ps.asset_balance:.6f} contracts @ ${ps.buy_price:,.4f}  "
                    f"[SL: ${ps.buy_price * (1 - STOP_LOSS_PCT/100):,.4f}]\n"
                    for pair, ps in recovered
                )
                + f"  Bot will resume exit monitoring immediately.\n"
                f"{'─'*60}"
            )
        else:
            log.info("WAL recovery: no open positions — clean slate")
    else:
        state = create_bot_state()
        log.info("Created fresh bot state")

    # Connect to Binance (WebSocket + market-data REST via python-binance)
    await binance.connect()

    # ── Futures pre-flight: enforce One-Way Mode via CCXT ─────────────────
    # Must run before any order is placed. Idempotent — safe to retry.
    # IMPORTANT: futures testnet keys are separate from spot testnet keys.
    # Generate them at https://testnet.binancefuture.com before live deployment.
    try:
        await futures_execution.enforce_futures_environment()
    except Exception as e:
        log.error(f"Futures environment setup failed: {e} — HALTING startup")
        await futures_execution.close_exchange()
        return

    # Initialise Market Dynamics Engine (DataProvider cache + VolumePairList)
    init_market_data(binance)

    # ── Futures USDT balance ───────────────────────────────────────────────
    # In futures mode we hold ONLY USDT — never query base-asset spot walances.
    # Portfolio value = futures wallet total (free + locked margin).
    usdt = await futures_execution.get_futures_usdt_balance()
    total_value = usdt      # Futures: portfolio IS the USDT wallet
    num_pairs = len(TRADING_PAIRS)

    # Check if we have any trade history — if not, reset start value
    from logger import get_trade_stats
    has_trades = get_trade_stats()["total_trades"] > 0

    trade_size = total_value * TRADE_SIZE_PCT
    max_pos = total_value * MAX_POSITION_PCT

    # ── Capital split: long-term layer vs active trading ──────────────────
    # If long-term is enabled, reserve covers only the TRADING portion so LT
    # deployments don't crowd out the scalping system's spendable balance.
    if LONG_TERM_ENABLED:
        lt_budget     = total_value * LONG_TERM_ALLOCATION
        trading_cap   = total_value * (1.0 - LONG_TERM_ALLOCATION)
        usdt_reserved = trading_cap * PORTFOLIO_RESERVE_PCT
    else:
        lt_budget     = 0.0
        trading_cap   = total_value
        usdt_reserved = total_value * PORTFOLIO_RESERVE_PCT

    async with state.lock:
        state.portfolio_total_usdt = total_value
        state.usdt_balance = usdt
        state.usdt_reserved = usdt_reserved
        # Reset start value if no trades exist (DB was wiped or fresh start)
        if not has_trades or state.portfolio_start_value <= 0:
            state.portfolio_start_value = total_value
        # Set per-pair allocation to trade size (not full deployment)
        for pair_key, ps in state.pairs.items():
            ps.trade_amount_usdt = trade_size
            ps.allocated_usdt = trade_size

    # ── Long-term layer initialisation ────────────────────────────────────
    if LONG_TERM_ENABLED:
        async with state.lock:
            state.lt_capital_budget = lt_budget

            # Ensure a LongTermState exists for every configured LT asset
            for pair in LONG_TERM_ASSETS:
                if pair not in state.long_term_states:
                    state.long_term_states[pair] = LongTermState(symbol=pair)

            # Recompute deployed capital from any LT positions that survived restart.
            # Also subtract LT quantities from ps.asset_balance so the scalping
            # system never counts long-term coins as part of its own position.
            deployed = 0.0
            for sym, lt_s in state.long_term_states.items():
                if lt_s.holding and lt_s.quantity > 0:
                    deployed += lt_s.quantity * lt_s.entry_price
                    scal_ps = state.pairs.get(sym)
                    if scal_ps and scal_ps.asset_balance >= lt_s.quantity:
                        scal_ps.asset_balance -= lt_s.quantity
                        log.info(
                            f"  LT restore: separated {lt_s.quantity:.6f} {sym} "
                            f"from scalp balance"
                        )
            state.lt_capital_deployed = deployed

        log.info(
            f"{C.BOLD}Long-Term Layer:{C.RESET} "
            f"budget=${lt_budget:,.2f} ({LONG_TERM_ALLOCATION*100:.0f}%)  "
            f"deployed=${state.lt_capital_deployed:,.2f}  "
            f"trading_cap=${trading_cap:,.2f} ({(1-LONG_TERM_ALLOCATION)*100:.0f}%)"
        )

    # ── Phase 8: Audit Mode banner ─────────────────────────────────────────────
    if AUDIT_MODE:
        log.warning(
            f"\n{'━'*60}\n"
            f"  ⚠️  AUDIT MODE ACTIVE (Phase 8 Execution Audit)\n"
            f"  Risk Parity DISABLED — all trades forced to ${AUDIT_MICRO_NOTIONAL:.0f} USDT notional\n"
            f"  Set AUDIT_MODE=False in .env to restore full sizing\n"
            f"{'━'*60}"
        )

    exit_mode = "ATR-based" if USE_ATR_EXITS else f"Fixed {PROFIT_TARGET_PCT}%/{STOP_LOSS_PCT}%"
    log.info(
        f"{C.BOLD}Futures Portfolio: {C.GREEN}${total_value:.2f} USDT{C.RESET} "
        f"({'TESTNET' if USE_TESTNET else 'LIVE'} — USDⓈ-M Perpetuals)\n"
        f"  {C.CYAN}Reserve:{C.RESET}      ${state.usdt_reserved:.2f} ({PORTFOLIO_RESERVE_PCT*100:.0f}% of trading cap)\n"
        f"  {C.CYAN}Leverage:{C.RESET}     {FUTURES_LEVERAGE}x isolated margin | "
        f"Risk: {FUTURES_ACCOUNT_RISK_PCT*100:.1f}% per trade\n"
        f"  {C.CYAN}Trade size:{C.RESET}   ${trade_size:.2f} ({TRADE_SIZE_PCT*100:.0f}% base) — "
        f"risk-parity adjusted per ATR\n"
        f"  {C.CYAN}Exit mode:{C.RESET}    {exit_mode} | SL floor: {STOP_LOSS_PCT}% | "
        f"Fees: taker {FUTURES_TAKER_FEE*100:.3f}%\n"
        f"  {C.CYAN}Engine:{C.RESET}       adaptive weights + regime + correlation + BTC crash filter\n"
        f"  {C.CYAN}Safety:{C.RESET}       One-Way Mode + reduceOnly exits + isolated margin"
    )

    # Fetch initial prices for all pairs
    for pair_cfg in TRADING_PAIRS:
        pair = pair_cfg["pair"]
        price = await binance.get_current_price(pair)
        if price > 0:
            async with state.lock:
                state.pairs[pair].current_price = price
            log.info(f"  {pair}: ${price:,.4f}")

    # Start WebSocket price streams (tick-level price updates)
    await binance.start_price_streams(state)
    # Phase 5: Start kline-close streams (fires indicator recalc at candle T+0)
    await binance.start_kline_streams(interval="3m")

    # Start signal polling tasks for each pair — staggered to avoid rate limits
    tasks = []
    for i, pair_cfg in enumerate(TRADING_PAIRS):
        pair = pair_cfg["pair"]
        stagger = i * 5  # 5 seconds between each pair's first poll
        tasks.append(asyncio.create_task(poll_rsi(pair)))
        tasks.append(asyncio.create_task(poll_orderbook(pair)))
        tasks.append(asyncio.create_task(poll_etherscan(pair)))
        tasks.append(asyncio.create_task(poll_fvg(pair, initial_delay=stagger + 15)))

    # Portfolio management tasks
    tasks.append(asyncio.create_task(portfolio_rebalance_loop()))
    tasks.append(asyncio.create_task(portfolio_snapshot_loop()))
    tasks.append(asyncio.create_task(state_persistence_loop()))

    # Adaptive engine tasks
    tasks.append(asyncio.create_task(signal_weight_learning_loop()))
    tasks.append(asyncio.create_task(drawdown_tracking_loop()))
    tasks.append(asyncio.create_task(btc_crash_detection_loop()))

    # Market Dynamics Engine — kline cache warm-up + volume pairlist refresh
    tasks.append(asyncio.create_task(market_dynamics_loop()))

    # Event-driven execution worker (replaces timer-based trading_loop)
    tasks.append(asyncio.create_task(real_time_execution_worker()))
    # Phase 5: Kline-close indicator worker (fires recalc at exact candle close)
    tasks.append(asyncio.create_task(poll_rsi_on_close()))

    # Long-term portfolio layer (separate from scalping, 5-min cycle)
    if LONG_TERM_ENABLED:
        tasks.append(asyncio.create_task(long_term_loop()))

    # Futures paper-trading layer (simulated only, separate from spot)
    if FUTURES_PAPER_ENABLED:
        from futures_paper import setup_futures_paper, futures_paper_loop
        setup_futures_paper(state)
        tasks.append(asyncio.create_task(futures_paper_loop()))

    log.info(f"Started {len(tasks)} background tasks")

    # Start Dashboard API server (FastAPI + WebSocket)
    from api import start_api_server
    api_task = asyncio.create_task(start_api_server(state, binance))
    tasks.append(api_task)
    import os as _os
    _dash_port = _os.environ.get("PORT", "8081")
    log.info(f"Dashboard: {C.CYAN}http://localhost:{_dash_port}{C.RESET}")

    # Start Discord bot (runs in same event loop)
    if DISCORD_BOT_TOKEN:
        from discord_bot.bot import start_discord_bot
        discord_task = asyncio.create_task(start_discord_bot(state))
        tasks.append(discord_task)
        log.info("Discord bot starting...")
    else:
        log.info("Discord bot skipped (no token)")

    log.info("Bot is running! Press Ctrl+C to stop.\n")

    # Wait for all tasks (runs forever until cancelled)
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass


async def shutdown():
    """Clean shutdown."""
    log.info("Shutting down...")
    await asyncio.get_running_loop().run_in_executor(None, save_state_to_db, state)
    await binance.disconnect()
    await futures_execution.close_exchange()    # close CCXT HTTP session
    log.info("Goodbye!")


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(start())
    except KeyboardInterrupt:
        log.info("\nCtrl+C received")
        loop.run_until_complete(shutdown())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
