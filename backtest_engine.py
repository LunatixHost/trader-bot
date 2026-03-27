"""
Localized Historical Backtesting Engine
========================================
Feeds historical OHLCV data directly into the live strategy and signal
pipeline without touching Binance or Discord.  All production logic runs
unchanged — only the data-ingest and order-execution layers are replaced
with offline equivalents.

Reuse map
---------
  signals/rsi.py  fetch_technical_indicators()  ← same function, MockDataProvider
  strategy.py     get_decision()                ← called verbatim
  config.py       all thresholds / multipliers  ← imported directly
  state.py        PairState / BotState           ← instantiated fresh per run

Execution model
---------------
  - Candle CLOSE price is used as the fill price (standard backtesting
    convention; add a slippage_pct argument for a more conservative estimate).
  - Fees: ROUND_TRIP_COST_PCT / 2 charged on each side (buy & sell).
  - TP1 ladder: sell TP1_SELL_RATIO of the position at BASE_TP1_PCT gain,
    then set a breakeven stop and start ATR trailing.
  - Forced exits: HARD_STOP, TRAIL_STOP, EARLY_STAGNATION T1/T2, TIME_STOP,
    MAX_HOLD_EXIT — all replicated from bot.py with simulated time.
  - Entry blocking: cooldown, add-blocked (averaging down), capital/room
    guards, economic viability (ATR reward vs round-trip cost).

CSV format
----------
  Required columns: timestamp, open, high, low, close, volume
  timestamp can be any format pandas can parse (ISO 8601, Unix ms, etc.)

  The candle interval in the CSV should match how your live bot operates.
  If your live bot uses 3-minute klines for RSI, supply a 3-minute CSV
  for the most accurate signal reproduction.

Usage
-----
  # Basic run — one pair, default capital
  python backtest_engine.py data/ETHUSDT_3m_2024.csv --pair ETHUSDT

  # Custom capital and slippage
  python backtest_engine.py data/BTCUSDT_1h_2024.csv --pair BTCUSDT \\
      --capital 1000 --slippage 0.05

  # Threshold parameter sweep (tests 9 combinations)
  python backtest_engine.py data/ETHUSDT_3m_2024.csv --pair ETHUSDT --sweep

  # Quiet: suppress trade-by-trade log, show only summary
  python backtest_engine.py data/ETHUSDT_3m_2024.csv --pair ETHUSDT --summary-only

Binance historical data
-----------------------
  Download via:  https://data.binance.vision/
  Filename pattern: ETHUSDT-3m-2024-01.zip
  After unzipping, columns are already in OHLCV order; add a header or
  use the --no-header flag (see argparse help below).
"""

import argparse
import asyncio
import logging
import math
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import pandas as pd

# ─── Project imports (live modules — not mocked) ─────────────────────
from config import (
    TRADING_PAIRS,
    TRADE_SIZE_PCT, MAX_POSITION_PCT, STOP_LOSS_PCT,
    PROFIT_TARGET_PCT, USE_ATR_EXITS,
    ATR_TRAILING_MULTIPLIER, ATR_TP_MULTIPLIER,
    ATR_MAX_TRAIL_PCT, ATR_TRAIL_ACTIVATION_PCT,
    BASE_TP1_PCT, TP1_SELL_RATIO, BREAKEVEN_BUFFER_PCT,
    TRAILING_ACTIVATION_PCT, TRAILING_DISTANCE_PCT,
    HARD_EXIT_MINUTES_BASE,
    SCALP_EARLY_STAG_MINUTES_T1, SCALP_EARLY_STAG_MIN_PCT_T1,
    SCALP_EARLY_STAG_MINUTES, SCALP_EARLY_STAG_MIN_PCT,
    CONFIDENCE_BUY_THRESHOLD, RSI_BUY_THRESHOLD,
    DYNAMIC_VOL_FACTOR_MIN, DYNAMIC_VOL_FACTOR_MAX, DYNAMIC_VOL_SMOOTH,
    ROUND_TRIP_COST_PCT, ECONOMIC_MIN_REWARD_PCT,
    ATR_PERIOD, MIN_TRADE_NOTIONAL,
    TRADE_COOLDOWN_SECONDS, TRADE_COOLDOWN_TRENDING,
    COOLDOWN_BYPASS_CONFIDENCE,
    FAILED_EXIT_COOLDOWN_STAGNATION, FAILED_EXIT_COOLDOWN_TRAIL_STOP,
    FAILED_EXIT_COOLDOWN_HARD_STOP,
    WICK_RATIO_THRESHOLD, MIN_EDGE_PCT, NO_TRADE_ATR_PCT, NO_TRADE_TREND_PCT,
    MAX_CONCURRENT_TRADES,
    get_base_asset,
)
from state import PairState, BotState
from strategy import get_decision, get_trade_size_modifier, get_correlation_modifier
from signals.rsi import fetch_technical_indicators

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backtest")

# ─── Warm-up period ───────────────────────────────────────────────────
# Skip trading during warm-up; indicators need this many candles to stabilise.
# RSI-14 needs ~30, MACD needs ~34, BB-20 needs 20 → 120 candles is safe.
WARMUP_CANDLES = 120

# ─── Binance kline column layout (indices) ────────────────────────────
_KL_OPEN_TIME  = 0
_KL_OPEN       = 1
_KL_HIGH       = 2
_KL_LOW        = 3
_KL_CLOSE      = 4
_KL_VOLUME     = 5


# ═══════════════════════════════════════════════════════════════════════
# MockDataProvider
# ═══════════════════════════════════════════════════════════════════════

class MockDataProvider:
    """Drop-in replacement for market_data.DataProvider.

    Holds a pre-built list of Binance-format kline rows.  get_klines()
    returns the tail of that list (up to `limit`) — interval and pair
    arguments are accepted but ignored (the CSV is a single interval/pair).
    """

    def __init__(self, klines: List[list]):
        self._klines = klines

    async def get_klines(
        self, pair: str, interval: str = "3m", limit: int = 120
    ) -> List[list]:
        return self._klines[-limit:]


# ═══════════════════════════════════════════════════════════════════════
# CSV loader
# ═══════════════════════════════════════════════════════════════════════

# Binance vision CSV column order (no header)
_BINANCE_VISION_COLS = [
    "open_time_ms", "open", "high", "low", "close", "volume",
    "close_time_ms", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]


def load_csv(path: str, no_header: bool = False) -> pd.DataFrame:
    """Load and normalise a historical OHLCV CSV.

    Accepts:
      - Standard user CSV: timestamp, open, high, low, close, volume
      - Binance Vision CSV (no header, 12 columns): auto-detected when
        `no_header=True` or when the file has no recognisable header row.

    Returns a DataFrame with columns: timestamp (datetime), open, high,
    low, close, volume — sorted by timestamp ascending.
    """
    # Auto-detect Binance Vision format: peek at first row
    with open(path, "r") as f:
        first = f.readline().strip()

    first_parts = first.split(",")
    is_binance_vision = (
        no_header
        or (len(first_parts) == 12 and first_parts[0].isdigit())
    )

    if is_binance_vision:
        df = pd.read_csv(path, header=None, names=_BINANCE_VISION_COLS)
        # Binance Vision timestamps: 13-digit = ms, 16-digit = µs — auto-detect
        sample_ts = int(df["open_time_ms"].iloc[0])
        ts_unit = "us" if sample_ts > 1e15 else "ms"
        df["timestamp"] = pd.to_datetime(df["open_time_ms"], unit=ts_unit, utc=True)
    else:
        df = pd.read_csv(path)
        # Accept 'open_time' alias (Binance Vision with header)
        if "open_time" in df.columns and "timestamp" not in df.columns:
            df = df.rename(columns={"open_time": "timestamp"})
        if "timestamp" not in df.columns:
            raise ValueError(
                "CSV must have a 'timestamp' column (or 'open_time').\n"
                "Required columns: timestamp, open, high, low, close, volume"
            )
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, infer_datetime_format=True)

    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    logger.info(
        f"Loaded {len(df):,} candles  "
        f"[{df['timestamp'].iloc[0]}  to  {df['timestamp'].iloc[-1]}]"
    )
    return df


def df_to_klines(df: pd.DataFrame) -> List[list]:
    """Convert a normalised OHLCV DataFrame to Binance kline list format.

    Binance kline format:
      [open_time_ms, open, high, low, close, volume,
       close_time_ms, quote_vol, trades, taker_buy_base, taker_buy_quote, ignore]

    All fields beyond OHLCV are synthetic zeros/ones — the indicators only
    use open/high/low/close/volume so this is safe.
    """
    klines = []
    for row in df.itertuples(index=False):
        ts_ms = int(row.timestamp.timestamp() * 1000)
        klines.append([
            ts_ms,                   # open_time_ms
            str(row.open),
            str(row.high),
            str(row.low),
            str(row.close),
            str(row.volume),
            ts_ms + 179_999,         # close_time_ms (synthetic — 3m - 1ms)
            "0",                     # quote_volume
            "0",                     # trades
            "0",                     # taker_buy_base
            "0",                     # taker_buy_quote
            "0",                     # ignore
        ])
    return klines


# ═══════════════════════════════════════════════════════════════════════
# Replicated bot.py helpers (sim-time aware)
# ═══════════════════════════════════════════════════════════════════════

def _update_dynamic_params_sim(ps: PairState):
    """Exact replica of bot.py _update_dynamic_params() — no changes."""
    prev_params = ps.dynamic_params or {}
    prev_vf = prev_params.get("_volatility_factor", 1.0)

    if ps.avg_atr_20 > 0 and ps.atr > 0:
        raw_vf = ps.atr / ps.avg_atr_20
    else:
        raw_vf = 1.0
    vf = prev_vf + DYNAMIC_VOL_SMOOTH * (raw_vf - prev_vf)
    vf = max(DYNAMIC_VOL_FACTOR_MIN, min(DYNAMIC_VOL_FACTOR_MAX, vf))

    momentum_factor = 1.0 + max(-0.25, min(0.25, ps.structure_score * 0.125))

    if ps.regime in {"trending", "trending_volatile"}:
        trend_factor = 1.33
    else:
        trend_factor = 1.0

    trend_label = getattr(ps, "trend", "chop")
    if trend_label == "uptrend":
        trade_mode    = "UPTREND"
        penalty_scale = 0.8
        threshold_bias = -0.2
        stag_t1 = SCALP_EARLY_STAG_MINUTES_T1 + 2.0
        stag_t2 = SCALP_EARLY_STAG_MINUTES + 2.0
    elif trend_label == "downtrend":
        trade_mode    = "DOWNTREND"
        penalty_scale = 1.1
        threshold_bias = 0.2
        stag_t1 = SCALP_EARLY_STAG_MINUTES_T1
        stag_t2 = SCALP_EARLY_STAG_MINUTES
    else:
        trade_mode    = "RANGING"
        penalty_scale = 1.0
        threshold_bias = 0.0
        stag_t1 = SCALP_EARLY_STAG_MINUTES_T1
        stag_t2 = SCALP_EARLY_STAG_MINUTES

    tp1_raw = BASE_TP1_PCT * momentum_factor
    tp1_floored = max(tp1_raw, ECONOMIC_MIN_REWARD_PCT)

    ps.dynamic_params = {
        "buy_threshold":      CONFIDENCE_BUY_THRESHOLD * vf,
        "tp1":                tp1_floored,
        "hard_exit_minutes":  HARD_EXIT_MINUTES_BASE / trend_factor,
        "_volatility_factor": vf,
        "trade_mode":         trade_mode,
        "penalty_scale":      penalty_scale,
        "threshold_bias":     threshold_bias,
        "stag_t1_minutes":    stag_t1,
        "stag_t2_minutes":    stag_t2,
    }


def _position_metrics_sim(ps: PairState, sim_now: datetime) -> dict:
    """Replica of bot.py _position_metrics() using simulated time."""
    pnl_pct = 0.0
    if ps.asset_balance > 0 and ps.buy_price > 0 and ps.current_price > 0:
        pnl_pct = (ps.current_price - ps.buy_price) / ps.buy_price * 100

    hold_minutes = 0.0
    if ps.position_open_time:
        try:
            opened = datetime.fromisoformat(ps.position_open_time)
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            hold_minutes = (sim_now - opened).total_seconds() / 60
        except Exception:
            pass

    hard_exit_minutes = ps.dynamic_params.get("hard_exit_minutes", HARD_EXIT_MINUTES_BASE)

    hard_stop_price = 0.0
    if ps.buy_price > 0:
        hard_stop_price = ps.buy_price * (1 - STOP_LOSS_PCT / 100)

    active_stop = hard_stop_price
    if ps.trailing_stop > 0:
        active_stop = max(active_stop, ps.trailing_stop)

    return {
        "pnl_pct":          pnl_pct,
        "hold_minutes":     hold_minutes,
        "hard_exit_minutes": hard_exit_minutes,
        "hard_stop_price":  hard_stop_price,
        "active_stop":      active_stop,
    }


def _get_forced_exit_sim(ps: PairState, sim_now: datetime) -> Optional[str]:
    """Replica of bot.py _get_forced_exit_reason() using simulated time."""
    if ps.asset_balance <= 0 or ps.buy_price <= 0 or ps.current_price <= 0:
        return None

    m = _position_metrics_sim(ps, sim_now)
    pnl_pct       = m["pnl_pct"]
    hold_minutes  = m["hold_minutes"]
    hard_exit_min = m["hard_exit_minutes"]
    hard_stop     = m["hard_stop_price"]

    if hard_stop > 0 and ps.current_price <= hard_stop:
        return "HARD_STOP"

    if ps.trailing_stop > 0 and ps.current_price <= ps.trailing_stop:
        return "TRAIL_STOP"

    stag_t1 = ps.dynamic_params.get("stag_t1_minutes", SCALP_EARLY_STAG_MINUTES_T1)
    stag_t2 = ps.dynamic_params.get("stag_t2_minutes", SCALP_EARLY_STAG_MINUTES)
    if hold_minutes >= stag_t1 and pnl_pct < SCALP_EARLY_STAG_MIN_PCT_T1:
        return "EARLY_STAGNATION"
    if hold_minutes >= stag_t2 and pnl_pct < SCALP_EARLY_STAG_MIN_PCT:
        return "EARLY_STAGNATION_2"

    if hold_minutes >= hard_exit_min and pnl_pct <= 0.15:
        return "TIME_STOP"

    if hold_minutes >= hard_exit_min * 1.5:
        return "MAX_HOLD_EXIT"

    return None


def _should_allow_signal_sell_sim(ps: PairState, sim_now: datetime) -> bool:
    """Replica of bot.py _should_allow_signal_sell() using simulated time."""
    if ps.asset_balance <= 0 or ps.buy_price <= 0 or ps.current_price <= 0:
        return False

    m = _position_metrics_sim(ps, sim_now)
    pnl_pct      = m["pnl_pct"]
    hold_minutes = m["hold_minutes"]
    hard_exit_min = m["hard_exit_minutes"]

    if pnl_pct >= 0.25:
        return True

    peak_gain = getattr(ps, "peak_gain_pct", 0.0)
    if peak_gain >= 0.30:
        return True

    if hold_minutes >= hard_exit_min and pnl_pct > 0:
        return True

    eff = getattr(ps, "effective_score", 0.0)
    thr = ps.dynamic_params.get("effective_threshold", CONFIDENCE_BUY_THRESHOLD)
    if pnl_pct > 0.22 and eff < thr - 1.0:
        return True

    return False


def _check_entry_block_sim(ps: PairState, state: BotState, sim_now: datetime) -> Optional[str]:
    """Simplified entry block check (no spread filter, no Binance calls)."""
    # 1) No averaging down
    if ps.asset_balance > 0 and ps.buy_price > 0 and ps.current_price < ps.buy_price:
        return "add_blocked"

    # 2) Cooldown
    if ps.last_trade_time:
        try:
            last_time = datetime.fromisoformat(ps.last_trade_time)
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            elapsed = (sim_now - last_time).total_seconds()
            cooldown = (
                TRADE_COOLDOWN_TRENDING
                if ps.regime in {"trending", "trending_volatile"}
                else TRADE_COOLDOWN_SECONDS
            )
            if elapsed < cooldown:
                bypass_score = ps.dynamic_params.get("effective_threshold", CONFIDENCE_BUY_THRESHOLD)
                if ps.effective_score < bypass_score + COOLDOWN_BYPASS_CONFIDENCE:
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
        if suppress_s > 0:
            try:
                ft = datetime.fromisoformat(failed_time)
                if ft.tzinfo is None:
                    ft = ft.replace(tzinfo=timezone.utc)
                if (sim_now - ft).total_seconds() < suppress_s:
                    return "failed_exit_cooldown"
            except (ValueError, TypeError):
                pass

    # 3) Economic viability hard block
    if ps.atr_pct > 0:
        atr_reward_pct = ps.atr_pct * ATR_TP_MULTIPLIER * 100
        if atr_reward_pct < ROUND_TRIP_COST_PCT:
            return "uneconomic_move"

    # 4) Capital
    spendable = state.usdt_balance - state.usdt_reserved
    if spendable <= 1.0:
        return "insufficient_spendable"

    # 5) Position room
    max_position = state.portfolio_total_usdt * MAX_POSITION_PCT
    current_position = ps.asset_balance * ps.current_price if ps.current_price > 0 else 0.0
    if max_position - current_position <= 1.0:
        return "position_maxed"

    # 6) Min notional
    trade_size = state.portfolio_total_usdt * TRADE_SIZE_PCT
    trade_size *= get_trade_size_modifier(state)
    if trade_size < MIN_TRADE_NOTIONAL:
        return "min_notional"

    # 7) Concurrent cap
    open_count = sum(1 for p in state.pairs.values() if p.asset_balance > 0)
    if open_count >= MAX_CONCURRENT_TRADES:
        return "concurrent_cap"

    return None


# ═══════════════════════════════════════════════════════════════════════
# Trade record
# ═══════════════════════════════════════════════════════════════════════

class TradeRecord:
    __slots__ = (
        "pair", "side", "timestamp", "price", "qty", "usdt_value",
        "fee_usdt", "pnl_usdt", "pnl_pct",
        "exit_reason", "hold_minutes",
    )

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ═══════════════════════════════════════════════════════════════════════
# BacktestEngine
# ═══════════════════════════════════════════════════════════════════════

class BacktestEngine:
    """High-speed offline simulation of the live trading engine.

    Parameters
    ----------
    df : pd.DataFrame
        Normalised OHLCV DataFrame from load_csv().
    pair : str
        Trading pair identifier (e.g. "ETHUSDT").
    starting_capital : float
        Simulated USDT wallet balance at start.
    fee_pct : float
        One-way fee (%). Default = ROUND_TRIP_COST_PCT / 2.
    slippage_pct : float
        One-way slippage (%). Added on top of fee.
    verbose : bool
        Log each trade entry/exit to console.
    """

    def __init__(
        self,
        df: pd.DataFrame,
        pair: str,
        starting_capital: float = 1000.0,
        fee_pct: float = ROUND_TRIP_COST_PCT / 2,
        slippage_pct: float = 0.0,
        verbose: bool = True,
        threshold_scale: float = 0.65,
    ):
        # threshold_scale: multiplied onto CONFIDENCE_BUY_THRESHOLD after
        # _update_dynamic_params_sim(). Compensates for structure_score and
        # volatility_score being unimplemented in the live bot (always 0.0).
        # Those fields were intended to contribute ~1 pt to effective score;
        # without them the max achievable effective = ~1.8 vs threshold 2.7.
        # 0.65 → threshold = 2.7 × 0.65 = ~1.75, just below max signal output.
        # Override with --threshold-scale on the CLI to test other levels.
        self.threshold_scale = threshold_scale
        self.df              = df
        self.pair            = pair
        self.base_asset      = get_base_asset(pair)
        self.starting_capital = starting_capital
        self.fee_pct         = fee_pct
        self.slippage_pct    = slippage_pct
        self.verbose         = verbose

        # All-klines list built from CSV; rolling window slides forward each candle
        self._all_klines: List[list] = []

        # Simulation state
        self._state: BotState = self._make_bot_state(starting_capital)
        self._ps:    PairState = self._state.pairs[pair]

        # Trade ledger
        self.trades: List[TradeRecord] = []

        # Equity curve: (timestamp, portfolio_value) after each candle
        self.equity_curve: List[tuple] = []

        # TP1 partial-sell tracking
        self._entry_qty: float = 0.0   # full quantity bought (for TP1 math)

    # ─── State factories ─────────────────────────────────────────────

    def _make_bot_state(self, capital: float) -> BotState:
        state = BotState()
        state.usdt_balance        = capital
        state.portfolio_total_usdt = capital
        state.portfolio_start_value = capital
        state.portfolio_current_value = capital
        state.portfolio_peak_value  = capital

        # PORTFOLIO_RESERVE_PCT equivalent: 10% of trading cap
        state.usdt_reserved = capital * 0.10

        ps = PairState(pair=self.pair, base_asset=get_base_asset(self.pair))
        ps.trade_amount_usdt = capital * TRADE_SIZE_PCT
        ps.allocated_usdt    = capital * MAX_POSITION_PCT
        state.pairs[self.pair] = ps

        # Neutral signal weights (no learned weights for backtest)
        state.signal_weights = {"rsi": 1.0, "macd": 1.0, "bollinger": 1.0, "gas": 1.0}

        return state

    # ─── Helpers ─────────────────────────────────────────────────────

    def _cost_rate(self) -> float:
        """Combined one-way cost rate (fee + slippage) as a fraction."""
        return (self.fee_pct + self.slippage_pct) / 100.0

    def _portfolio_value(self) -> float:
        ps = self._ps
        coin_val = ps.asset_balance * ps.current_price if ps.current_price > 0 else 0.0
        return self._state.usdt_balance + coin_val

    def _update_trailing_stop(self, sim_now: datetime):
        """Ratchet ATR and post-TP1 trailing stops — mirrors poll_rsi logic."""
        ps = self._ps
        if ps.asset_balance <= 0 or ps.atr <= 0 or ps.buy_price <= 0:
            return

        gain_pct = (ps.current_price - ps.buy_price) / ps.buy_price * 100

        if ps.tp1_hit and gain_pct >= TRAILING_ACTIVATION_PCT:
            new_stop = ps.current_price * (1 - TRAILING_DISTANCE_PCT / 100)
            if new_stop > ps.trailing_stop:
                ps.trailing_stop = new_stop
        elif not ps.tp1_hit and gain_pct >= ATR_TRAIL_ACTIVATION_PCT:
            new_stop = ps.current_price - ps.atr * ATR_TRAILING_MULTIPLIER
            max_trail = ps.current_price * (1 - ATR_MAX_TRAIL_PCT)
            new_stop = max(new_stop, max_trail)
            if new_stop > ps.trailing_stop:
                ps.trailing_stop = new_stop

        # Peak gain tracking
        if gain_pct > ps.peak_gain_pct:
            ps.peak_gain_pct = gain_pct

    # ─── Execution simulation ─────────────────────────────────────────

    def _execute_buy(self, price: float, sim_now: datetime):
        """Simulate a full market buy at `price`."""
        state = self._state
        ps    = self._ps

        trade_size = state.portfolio_total_usdt * TRADE_SIZE_PCT
        trade_size *= get_trade_size_modifier(state)
        trade_size *= get_correlation_modifier(ps, state)

        spendable = state.usdt_balance - state.usdt_reserved
        max_pos   = state.portfolio_total_usdt * MAX_POSITION_PCT
        cur_pos   = ps.asset_balance * price if price > 0 else 0.0
        room      = max_pos - cur_pos

        spend = min(trade_size, spendable, room)
        if spend < MIN_TRADE_NOTIONAL:
            return

        # Apply fee + slippage (buying costs money → effective price higher)
        effective_price = price * (1 + self._cost_rate())
        qty = spend / effective_price
        fee_usdt = spend * self._cost_rate()

        # Update state
        state.usdt_balance -= spend
        ps.asset_balance   += qty
        ps.buy_price        = price           # buy_price = fill, not slipped
        ps.position_open_time = sim_now.isoformat()
        ps.last_trade_time    = sim_now.isoformat()
        ps.trailing_stop      = 0.0
        ps.tp1_hit            = False
        ps.peak_gain_pct      = 0.0
        ps.atr_take_profit    = price + ps.atr * ATR_TP_MULTIPLIER if ps.atr > 0 else 0.0

        self._entry_qty = qty   # store for TP1 partial tracking
        state.total_buys   += 1
        state.total_trades += 1

        rec = TradeRecord(
            pair=self.pair, side="BUY", timestamp=sim_now,
            price=price, qty=qty,
            usdt_value=spend, fee_usdt=fee_usdt,
            pnl_usdt=0.0, pnl_pct=0.0,
            exit_reason="", hold_minutes=0.0,
        )
        self.trades.append(rec)

        if self.verbose:
            logger.info(
                f"  BUY   {self.base_asset:6s}  {qty:.6f} @ ${price:,.4f}"
                f"  (${spend:.2f} spend  fee=${fee_usdt:.2f})"
            )

    def _execute_partial_sell(self, price: float, sim_now: datetime, ratio: float = TP1_SELL_RATIO):
        """TP1 partial exit — sell `ratio` of the position."""
        ps = self._ps

        sell_qty = ps.asset_balance * ratio
        if sell_qty <= 0:
            return

        effective_price = price * (1 - self._cost_rate())
        proceeds = sell_qty * effective_price
        fee_usdt = sell_qty * price * self._cost_rate()
        cost_basis = sell_qty * ps.buy_price
        pnl_usdt = proceeds - cost_basis
        pnl_pct  = (price / ps.buy_price - 1) * 100 if ps.buy_price > 0 else 0.0

        self._state.usdt_balance += proceeds
        ps.asset_balance -= sell_qty
        ps.tp1_hit = True

        # Move stop to breakeven + buffer
        ps.trailing_stop = ps.buy_price * (1 + BREAKEVEN_BUFFER_PCT / 100)
        ps.last_trade_time = sim_now.isoformat()

        self._state.total_sells += 1
        if pnl_usdt > 0:
            self._state.profitable_trades += 1

        hold_m = _position_metrics_sim(ps, sim_now)["hold_minutes"]
        rec = TradeRecord(
            pair=self.pair, side="SELL_TP1", timestamp=sim_now,
            price=price, qty=sell_qty,
            usdt_value=proceeds, fee_usdt=fee_usdt,
            pnl_usdt=pnl_usdt, pnl_pct=pnl_pct,
            exit_reason="TP1", hold_minutes=hold_m,
        )
        self.trades.append(rec)

        if self.verbose:
            logger.info(
                f"  TP1   {self.base_asset:6s}  {sell_qty:.6f} @ ${price:,.4f}"
                f"  PnL ${pnl_usdt:+.2f} ({pnl_pct:+.3f}%)"
            )

    def _execute_sell(self, price: float, sim_now: datetime, reason: str):
        """Close the full remaining position."""
        ps = self._ps

        if ps.asset_balance <= 0:
            return

        qty = ps.asset_balance
        effective_price = price * (1 - self._cost_rate())
        proceeds = qty * effective_price
        fee_usdt = qty * price * self._cost_rate()
        cost_basis = qty * ps.buy_price
        pnl_usdt = proceeds - cost_basis
        pnl_pct  = (price / ps.buy_price - 1) * 100 if ps.buy_price > 0 else 0.0

        self._state.usdt_balance += proceeds
        hold_m = _position_metrics_sim(ps, sim_now)["hold_minutes"]

        # Tag weak exits for re-entry suppression
        _FAILED_EXIT_REASONS = {
            "HARD_STOP", "TRAIL_STOP",
            "EARLY_STAGNATION", "EARLY_STAGNATION_2",
        }
        if reason in _FAILED_EXIT_REASONS:
            ps.last_failed_exit_reason = reason
            ps.last_failed_exit_time   = sim_now.isoformat()
        else:
            ps.last_failed_exit_reason = ""
            ps.last_failed_exit_time   = ""

        ps.asset_balance   = 0.0
        ps.buy_price        = 0.0
        ps.position_open_time = ""
        ps.trailing_stop    = 0.0
        ps.tp1_hit          = False
        ps.peak_gain_pct    = 0.0
        ps.last_trade_time  = sim_now.isoformat()

        self._state.total_sells += 1
        self._state.total_trades += 1
        if pnl_usdt > 0:
            self._state.profitable_trades += 1

        rec = TradeRecord(
            pair=self.pair, side="SELL", timestamp=sim_now,
            price=price, qty=qty,
            usdt_value=proceeds, fee_usdt=fee_usdt,
            pnl_usdt=pnl_usdt, pnl_pct=pnl_pct,
            exit_reason=reason, hold_minutes=hold_m,
        )
        self.trades.append(rec)

        if self.verbose:
            sign = "+" if pnl_usdt >= 0 else ""
            logger.info(
                f"  SELL  {self.base_asset:6s}  {qty:.6f} @ ${price:,.4f}"
                f"  PnL ${sign}{pnl_usdt:.2f} ({sign}{pnl_pct:.3f}%)  [{reason}]"
                f"  held {hold_m:.0f}m"
            )

    # ─── Main run loop ────────────────────────────────────────────────

    async def run(self) -> "BacktestReport":
        """Execute the full backtest.  Returns a BacktestReport."""
        all_klines = df_to_klines(self.df)
        n = len(all_klines)

        if n <= WARMUP_CANDLES:
            raise ValueError(
                f"Dataset too short: {n} candles. Need > {WARMUP_CANDLES} for warm-up."
            )

        logger.info(
            f"Starting backtest: {self.pair}  "
            f"{n - WARMUP_CANDLES:,} tradeable candles  "
            f"capital=${self.starting_capital:,.2f}"
        )

        for i in range(1, n + 1):
            window = all_klines[:i]
            sim_ts = self.df["timestamp"].iloc[i - 1]
            sim_now = sim_ts.to_pydatetime()
            if sim_now.tzinfo is None:
                sim_now = sim_now.replace(tzinfo=timezone.utc)

            # Current candle's OHLC
            row = self.df.iloc[i - 1]
            close_price = float(row["close"])
            high_price  = float(row["high"])
            low_price   = float(row["low"])

            # ── Update price state ──
            ps = self._ps
            ps.prev_price    = ps.current_price if ps.current_price > 0 else close_price
            ps.current_price = close_price

            # ── Skip warm-up ──
            if i <= WARMUP_CANDLES:
                self.equity_curve.append((sim_now, self._portfolio_value()))
                continue

            # ── Compute indicators via the real signal pipeline ──
            mock_dp = MockDataProvider(window)
            indicators = await fetch_technical_indicators(
                None, self.pair, data_provider=mock_dp
            )

            # Populate PairState exactly as poll_rsi() does in bot.py
            ps.rsi               = indicators["rsi"]
            ps.macd              = indicators["macd"]
            ps.macd_signal       = indicators["macd_signal"]
            ps.macd_histogram    = indicators["macd_histogram"]
            ps.macd_crossover    = indicators["macd_crossover"]
            ps.bb_upper          = indicators["bb_upper"]
            ps.bb_middle         = indicators["bb_middle"]
            ps.bb_lower          = indicators["bb_lower"]
            ps.bb_position       = indicators["bb_position"]
            ps.bb_squeeze        = indicators["bb_squeeze"]
            ps.atr               = indicators.get("atr", 0.0)
            ps.atr_pct           = indicators.get("atr_pct", 0.0)
            ps.avg_atr_20        = indicators.get("avg_atr_20", 0.0)
            ps.trend_strength    = indicators.get("trend_strength", 0.0)
            ps.trend_direction   = indicators.get("trend_direction", "flat")
            ps.regime            = indicators.get("regime", "ranging")
            ps.trend             = (
                "uptrend"   if ps.trend_direction == "up"   else
                "downtrend" if ps.trend_direction == "down" else "chop"
            )
            ps.wick_ratio        = indicators.get("wick_ratio", 0.0)
            ps.candle_range_pct  = indicators.get("candle_range_pct", 0.0)
            ps.liquidity_sweep_score = indicators.get("liquidity_sweep_score", 0.0)

            # Neutral microstructure defaults (no live orderbook in backtest)
            ps.orderbook_imbalance = 0.5
            ps.flow_ratio          = 0.5

            # Update dynamic params (volatility_factor, TP1 scaling, regime mode)
            _update_dynamic_params_sim(ps)
            # Apply threshold scale to compensate for unimplemented structure_score
            ps.dynamic_params["buy_threshold"] *= self.threshold_scale
            ps.dynamic_params["effective_threshold"] = ps.dynamic_params["buy_threshold"]

            # Update trailing stop ratchet using sim time
            self._update_trailing_stop(sim_now)

            # ── Strategy decision ──
            decision, signal_snapshot = get_decision(ps, self._state)
            ps.decision = decision

            # ── Check forced exits (always checked before new entries) ──
            forced = _get_forced_exit_sim(ps, sim_now)
            if forced:
                # For HARD_STOP / TRAIL_STOP, use the actual stop price, not close
                if forced == "HARD_STOP":
                    exit_price = ps.buy_price * (1 - STOP_LOSS_PCT / 100)
                    exit_price = max(low_price, exit_price)   # can't fill below the candle low
                elif forced == "TRAIL_STOP":
                    exit_price = max(low_price, ps.trailing_stop)
                else:
                    exit_price = close_price
                self._execute_sell(exit_price, sim_now, reason=forced)

            # ── TP1 check ──
            elif ps.asset_balance > 0 and not ps.tp1_hit and ps.buy_price > 0:
                tp1_pct   = ps.dynamic_params.get("tp1", BASE_TP1_PCT)
                tp1_price = ps.buy_price * (1 + tp1_pct / 100)
                if high_price >= tp1_price:
                    self._execute_partial_sell(tp1_price, sim_now)

            # ── Signal-driven SELL ──
            elif decision == "SELL" and ps.asset_balance > 0:
                sell_reason = ps.dynamic_params.get("sell_reason", "signal")
                if sell_reason in ("early_failure", "risk_exit"):
                    self._execute_sell(close_price, sim_now, reason=sell_reason.upper())
                else:
                    if _should_allow_signal_sell_sim(ps, sim_now):
                        self._execute_sell(close_price, sim_now, reason="SIGNAL_SELL")

            # ── BUY ──
            elif decision == "BUY" and ps.asset_balance == 0:
                block = _check_entry_block_sim(ps, self._state, sim_now)
                if block is None:
                    self._execute_buy(close_price, sim_now)

            # ── Portfolio bookkeeping ──
            pv = self._portfolio_value()
            self._state.portfolio_current_value = pv
            if pv > self._state.portfolio_peak_value:
                self._state.portfolio_peak_value = pv

            # Drawdown flag (mirrors drawdown_tracking_loop)
            if self._state.portfolio_peak_value > 0:
                dd = (self._state.portfolio_peak_value - pv) / self._state.portfolio_peak_value * 100
                self._state.drawdown_pct = max(0.0, dd)
                self._state.in_drawdown  = dd >= 3.0  # DRAWDOWN_TRIGGER_PCT default

            self.equity_curve.append((sim_now, pv))

        # ── Close any open position at last candle close ──
        ps = self._ps
        if ps.asset_balance > 0:
            last_price = float(self.df["close"].iloc[-1])
            last_ts    = self.df["timestamp"].iloc[-1].to_pydatetime()
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            self._execute_sell(last_price, last_ts, reason="END_OF_DATA")

        return self._build_report()

    # ─── Report ──────────────────────────────────────────────────────

    def _build_report(self) -> "BacktestReport":
        return BacktestReport(
            pair             = self.pair,
            starting_capital = self.starting_capital,
            trades           = self.trades,
            equity_curve     = self.equity_curve,
            final_balance    = self._portfolio_value(),
        )


# ═══════════════════════════════════════════════════════════════════════
# BacktestReport
# ═══════════════════════════════════════════════════════════════════════

class BacktestReport:
    """Compiled performance metrics for one backtest run."""

    def __init__(
        self,
        pair: str,
        starting_capital: float,
        trades: List[TradeRecord],
        equity_curve: List[tuple],
        final_balance: float,
    ):
        self.pair             = pair
        self.starting_capital = starting_capital
        self.trades           = trades
        self.equity_curve     = equity_curve
        self.final_balance    = final_balance

    # ─── Metrics ────────────────────────────────────────────────────

    def _sell_trades(self) -> List[TradeRecord]:
        return [t for t in self.trades if t.side in ("SELL", "SELL_TP1")]

    def total_net_pnl(self) -> float:
        return sum(t.pnl_usdt for t in self._sell_trades())

    def total_fees(self) -> float:
        return sum(t.fee_usdt for t in self.trades)

    def total_trades(self) -> int:
        return len([t for t in self.trades if t.side == "SELL"])

    def win_rate(self) -> float:
        sells = self._sell_trades()
        if not sells:
            return 0.0
        wins = sum(1 for t in sells if t.pnl_usdt > 0)
        return wins / len(sells) * 100

    def profit_factor(self) -> float:
        gross_win  = sum(t.pnl_usdt for t in self._sell_trades() if t.pnl_usdt > 0)
        gross_loss = abs(sum(t.pnl_usdt for t in self._sell_trades() if t.pnl_usdt < 0))
        return (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0][1]
        max_dd = 0.0
        for _, val in self.equity_curve:
            if val > peak:
                peak = val
            dd = (peak - val) / peak * 100 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        return max_dd

    def sharpe_ratio(self, risk_free_annual_pct: float = 4.0) -> float:
        """Annualised Sharpe ratio from daily equity returns."""
        if len(self.equity_curve) < 2:
            return 0.0
        df = pd.DataFrame(self.equity_curve, columns=["ts", "equity"])
        df = df.set_index("ts").resample("1D").last().dropna()
        if len(df) < 2:
            return 0.0
        daily_ret = df["equity"].pct_change().dropna()
        if daily_ret.std() == 0:
            return 0.0
        rf_daily = risk_free_annual_pct / 100 / 365
        sharpe = (daily_ret.mean() - rf_daily) / daily_ret.std() * math.sqrt(365)
        return round(sharpe, 3)

    def avg_hold_minutes(self) -> float:
        sells = [t for t in self.trades if t.side == "SELL"]
        if not sells:
            return 0.0
        return sum(t.hold_minutes for t in sells) / len(sells)

    def exit_reason_breakdown(self) -> dict:
        breakdown = defaultdict(int)
        for t in self.trades:
            if t.side == "SELL":
                breakdown[t.exit_reason] += 1
        return dict(sorted(breakdown.items(), key=lambda x: -x[1]))

    # ─── Print ──────────────────────────────────────────────────────

    def print_trade_log(self):
        sells = [t for t in self.trades if t.side in ("SELL", "SELL_TP1")]
        if not sells:
            print("  No completed trades.")
            return
        print(f"\n  {'#':>4}  {'Timestamp':<24}  {'Side':<8}  {'Price':>12}  "
              f"{'Qty':>12}  {'PnL $':>9}  {'PnL %':>7}  {'Reason':<22}  Hold")
        print("  " + "-" * 120)
        for n, t in enumerate(sells, 1):
            sign = "+" if t.pnl_usdt >= 0 else ""
            print(
                f"  {n:>4}  {str(t.timestamp)[:23]:<24}  {t.side:<8}  "
                f"${t.price:>11,.4f}  {t.qty:>12.6f}  "
                f"{sign}${t.pnl_usdt:>8.2f}  {sign}{t.pnl_pct:>6.3f}%  "
                f"{t.exit_reason:<22}  {t.hold_minutes:.0f}m"
            )

    def print_summary(self, label: str = ""):
        net_pnl    = self.total_net_pnl()
        ret_pct    = (self.final_balance - self.starting_capital) / self.starting_capital * 100
        n_trades   = self.total_trades()
        win_r      = self.win_rate()
        pf         = self.profit_factor()
        max_dd     = self.max_drawdown_pct()
        sharpe     = self.sharpe_ratio()
        avg_hold   = self.avg_hold_minutes()
        fees       = self.total_fees()
        exit_br    = self.exit_reason_breakdown()

        lbl = f"  [{label}]" if label else ""
        bar = "=" * 65
        print(f"\n  {bar}")
        print(f"  BACKTEST RESULTS{lbl}  --  {self.pair}")
        print(f"  {bar}")
        print(f"  Capital:         ${self.starting_capital:>10,.2f}  ->  ${self.final_balance:>10,.2f}")
        print(f"  Net P&L:         {'+' if net_pnl >= 0 else ''}${net_pnl:>10.2f}  ({'+' if ret_pct >= 0 else ''}{ret_pct:.2f}%)")
        print(f"  Total fees:      ${fees:>10.2f}")
        print(f"  Trades:          {n_trades:>10,}")
        print(f"  Win rate:        {win_r:>10.1f}%")
        print(f"  Profit factor:   {pf:>10.3f}")
        print(f"  Max drawdown:    {max_dd:>10.2f}%")
        print(f"  Sharpe ratio:    {sharpe:>10.3f}")
        print(f"  Avg hold time:   {avg_hold:>10.1f} min")
        print(f"  Exit breakdown:")
        for reason, count in exit_br.items():
            print(f"    {reason:<22} {count:>5}x")
        print(f"  {bar}\n")

    def print_sweep_row(self, label: str):
        """One-line summary row for sweep table output."""
        ret = (self.final_balance - self.starting_capital) / self.starting_capital * 100
        print(
            f"  {label:<28}  ${self.total_net_pnl():>9.2f}  "
            f"{ret:>7.2f}%  {self.win_rate():>7.1f}%  "
            f"{self.profit_factor():>6.2f}  {self.max_drawdown_pct():>7.2f}%  "
            f"{self.sharpe_ratio():>8.3f}  {self.total_trades():>7,}"
        )


# ═══════════════════════════════════════════════════════════════════════
# Parameter sweep
# ═══════════════════════════════════════════════════════════════════════

async def run_sweep(
    df: pd.DataFrame,
    pair: str,
    capital: float,
    rsi_thresholds: List[float],
    conf_thresholds: List[float],
    slippage_pct: float = 0.0,
    threshold_scale: float = 0.65,
):
    """Grid search over RSI and confidence thresholds.

    Temporarily overrides the config module values for each run.
    Prints a comparison table at the end.
    """
    import config as cfg

    results = []
    total = len(rsi_thresholds) * len(conf_thresholds)
    run_n = 0

    for rsi_t in rsi_thresholds:
        for conf_t in conf_thresholds:
            run_n += 1
            cfg.RSI_BUY_THRESHOLD        = rsi_t
            cfg.CONFIDENCE_BUY_THRESHOLD = conf_t

            # Also patch the imported names in this module
            globals()["RSI_BUY_THRESHOLD"]        = rsi_t
            globals()["CONFIDENCE_BUY_THRESHOLD"]  = conf_t

            label = f"RSI<{rsi_t:.0f} / CONF≥{conf_t:.1f}"
            logger.info(f"Sweep run {run_n}/{total}: {label}")

            engine = BacktestEngine(df, pair, capital, slippage_pct=slippage_pct, verbose=False, threshold_scale=threshold_scale)
            report = await engine.run()
            results.append((label, report))

    # Restore defaults
    cfg.RSI_BUY_THRESHOLD        = 55
    cfg.CONFIDENCE_BUY_THRESHOLD = 2.7

    # Summary table
    print(f"\n  {'-' * 80}")
    print(f"  PARAMETER SWEEP  --  {pair}  ({total} combinations)")
    print(f"  {'-' * 80}")
    print(f"  {'Label':<28}  {'Net PnL':>10}  {'Return':>8}  {'WinRate':>8}  "
          f"{'PF':>6}  {'MaxDD':>8}  {'Sharpe':>8}  {'Trades':>7}")
    print(f"  {'-' * 80}")
    for label, r in sorted(results, key=lambda x: -x[1].total_net_pnl()):
        ret = (r.final_balance - r.starting_capital) / r.starting_capital * 100
        print(
            f"  {label:<28}  ${r.total_net_pnl():>9.2f}  "
            f"{ret:>7.2f}%  {r.win_rate():>7.1f}%  "
            f"{r.profit_factor():>6.2f}  {r.max_drawdown_pct():>7.2f}%  "
            f"{r.sharpe_ratio():>8.3f}  {r.total_trades():>7,}"
        )
    print(f"  {'-' * 80}\n")


# ═══════════════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(
        description="Backtest the trading bot against historical OHLCV CSV data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "csv", help="Path to OHLCV CSV file."
    )
    parser.add_argument(
        "--pair", default="ETHUSDT",
        help="Trading pair (e.g. ETHUSDT, BTCUSDT). Must be in TRADING_PAIRS config "
             "or be added manually. Default: ETHUSDT"
    )
    parser.add_argument(
        "--capital", type=float, default=1000.0,
        help="Starting USDT capital. Default: 1000"
    )
    parser.add_argument(
        "--fee", type=float, default=ROUND_TRIP_COST_PCT / 2,
        help=f"One-way fee %%. Default: {ROUND_TRIP_COST_PCT / 2} (Binance maker/taker)"
    )
    parser.add_argument(
        "--slippage", type=float, default=0.0,
        help="One-way slippage %%. Default: 0 (conservative estimate: try 0.03)"
    )
    parser.add_argument(
        "--threshold-scale", type=float, default=0.65, dest="threshold_scale",
        help="Scale factor applied to CONFIDENCE_BUY_THRESHOLD (default 0.65). "
             "Compensates for structure_score/volatility_score being unimplemented "
             "in the live bot (always 0). 0.65 -> threshold ~1.75 vs max score ~1.8."
    )
    parser.add_argument(
        "--summary-only", action="store_true",
        help="Suppress trade-by-trade log; show only the summary table."
    )
    parser.add_argument(
        "--sweep", action="store_true",
        help="Run a parameter sweep over RSI and confidence thresholds."
    )
    parser.add_argument(
        "--no-header", action="store_true",
        help="CSV has no header row (auto-detected for Binance Vision 12-column format)."
    )

    args = parser.parse_args()

    # Ensure the pair has a PairState entry — if it's not in TRADING_PAIRS,
    # create a minimal config entry so state.py can find it.
    pair = args.pair.upper()
    known_pairs = [p["pair"] for p in TRADING_PAIRS]
    if pair not in known_pairs:
        logger.warning(
            f"  {pair} not in config TRADING_PAIRS — running in standalone mode "
            f"(no max_score cap from config, uses defaults)"
        )

    # Suppress noisy sub-loggers that fire on every candle during simulation
    for _noisy in ("trading_bot.regime", "trading_bot.signals.rsi"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    df = load_csv(args.csv, no_header=args.no_header)

    if args.sweep:
        rsi_thresholds  = [45, 50, 55, 60]
        conf_thresholds = [2.0, 2.5, 2.7, 3.0]
        await run_sweep(
            df, pair, args.capital,
            rsi_thresholds, conf_thresholds,
            slippage_pct=args.slippage,
            threshold_scale=args.threshold_scale,
        )
    else:
        verbose = not args.summary_only
        engine  = BacktestEngine(
            df, pair,
            starting_capital = args.capital,
            fee_pct          = args.fee,
            slippage_pct     = args.slippage,
            verbose          = verbose,
            threshold_scale  = args.threshold_scale,
        )
        report = await engine.run()

        if not args.summary_only:
            report.print_trade_log()

        report.print_summary()


if __name__ == "__main__":
    asyncio.run(main())
