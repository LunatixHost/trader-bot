import asyncio
import json
import sqlite3
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from config import TRADING_PAIRS, get_base_asset, get_max_score, LONG_TERM_ENABLED, LONG_TERM_ASSETS, FUTURES_PAPER_ENABLED

logger = logging.getLogger("trading_bot.state")

DB_PATH = "trades.db"


@dataclass
class LongTermState:
    """Per-symbol state for the long-term portfolio layer.

    Completely separate from PairState / scalping logic.
    No TP1, no trailing stop, no stagnation exits — this is a macro hold.
    """
    symbol: str = ""
    holding: bool = False               # True when a LT position is open
    entry_price: float = 0.0            # Average entry price of the LT position
    quantity: float = 0.0               # Units held (separate from scalping ps.asset_balance)
    last_action_ts: float = 0.0         # Unix timestamp of last buy/sell
    entry_time: str = ""                # ISO timestamp when position was opened
    consecutive_downtrend_cycles: int = 0  # Counter for sustained downtrend exit gate

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LongTermState":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class FuturesPaperState:
    """Per-symbol state for a simulated futures paper position.

    Completely separate from PairState (spot scalping) and LongTermState.
    No real orders — all execution is simulated internally.
    """
    symbol: str = ""
    side: str = "short"               # "short" | "long" (only "short" in v1)
    is_open: bool = False             # True when a paper position is active
    entry_price: float = 0.0         # Simulated entry price
    quantity: float = 0.0            # Simulated units held (notional / entry_price)
    notional_usdt: float = 0.0       # USDT notional (paper capital deployed)
    leverage: float = 1.0            # Simulated leverage factor
    entry_time: str = ""             # ISO timestamp when position was opened
    peak_pnl_pct: float = 0.0        # Highest unrealised PnL % since entry
    trailing_stop: float = 0.0       # Trailing stop price (0 = not yet activated)
    tp1_hit: bool = False            # True after first partial take-profit
    last_action_ts: float = 0.0      # Monotonic timestamp of last action
    last_failed_exit_reason: str = ""  # Most recent forced-exit reason (for cooldown)
    last_failed_exit_time: str = ""    # ISO timestamp of that exit
    accrued_funding_usdt: float = 0.0  # Cumulative funding fees charged during this position
    last_funding_ts: float = 0.0       # Monotonic timestamp of last funding application

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FuturesPaperState":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class PairState:
    """Per-pair trading state."""

    pair: str = ""                      # e.g. "ETHUSDT"
    base_asset: str = ""                # e.g. "ETH"
    current_price: float = 0.0

    # Technical indicators
    rsi: float = 50.0
    macd: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    macd_crossover: str = "none"        # "bullish" | "bearish" | "none"
    bb_upper: float = 0.0
    bb_middle: float = 0.0
    bb_lower: float = 0.0
    bb_position: str = "inside"         # "above_upper" | "below_lower" | "inside"
    bb_squeeze: bool = False

    # Other signals
    etherscan_gas: int = 0              # ETH only; 0 for others
    etherscan_activity: str = "normal"  # "high" | "normal" | "low"

    # Strategy
    confidence_score: int = 0
    confidence_weighted: float = 0.0    # Weighted confidence from adaptive engine
    effective_score: float = 0.0        # Confidence + PA + volatility + whale + sweep + FVG
    max_score: int = 6
    decision: str = "HOLD"              # "BUY" | "SELL" | "HOLD"

    # Regime & ATR
    regime: str = "ranging"             # "trending" | "ranging" | "choppy_volatile" | "trending_volatile"
    atr: float = 0.0                    # Average True Range (absolute, not %)
    atr_pct: float = 0.0               # ATR as % of price
    trend_strength: float = 0.0         # |EMA20 - EMA50| / price
    trend_direction: str = "flat"       # "up" | "down" | "flat" (EMA20 vs EMA50)
    trailing_stop: float = 0.0         # ATR-based trailing stop price (updated per tick)
    atr_take_profit: float = 0.0       # ATR-based take profit price (set at entry)

    # Portfolio / position
    trade_amount_usdt: float = 0.0      # From config (base allocation)
    allocated_usdt: float = 0.0         # Dynamically assigned by portfolio.py
    asset_balance: float = 0.0          # How much of this coin currently held
    buy_price: float = 0.0              # Price of last BUY (for stop-loss)
    position_value_usdt: float = 0.0    # Current value of held coin in USDT
    last_trade: dict = field(default_factory=dict)
    pl_usdt: float = 0.0               # P/L for this pair since bot started
    pl_pct: float = 0.0                # P/L % for this pair
    pl_today_usdt: float = 0.0         # P/L for today only
    peak_value: float = 0.0            # Highest position value (for per-pair drawdown)
    last_trade_time: str = ""          # ISO timestamp of last trade (for cooldown)
    wick_ratio: float = 0.0            # Upper wick as fraction of candle range (0-1)
    candle_range_pct: float = 0.0      # Last candle (high-low)/close as % — unstable_candle filter
    volatility: float = 0.0            # Calculated by portfolio.py

    # Microstructure signals (orderbook imbalance + trade flow)
    prev_price: float = 0.0             # Previous WebSocket tick price (for spike detection)
    bid_ask_spread_pct: float = 0.0     # (best_ask - best_bid) / mid — used as spread gate
    orderbook_imbalance: float = 0.5    # bid_vol / (bid+ask), EMA-smoothed; 0.5 = neutral
    flow_ratio: float = 0.5             # buyer-aggressor vol / total, EMA-smoothed; 0.5 = neutral

    # Price action / structure signals
    structure_score: float = 0.0
    trend: str = "chop"                # "uptrend" | "downtrend" | "chop"
    bos: bool = False                  # Break of structure flag
    sweep_type: str = "none"           # "bearish" | "bullish" | "none"
    # Volatility signals
    volatility_score: float = 0.0
    volatility_regime: str = "normal" # "expansion" | "normal"

    # Liquidity sweep signal (scaled by wick-depth/ATR, max ~1.5)
    liquidity_sweep_score: float = 0.0

    # 20-period average ATR (for volatility_factor in dynamic params)
    avg_atr_20: float = 0.0

    # FVG (Fair Value Gap) state
    fvg_detected: bool = False         # True when a valid FVG was found on 5m
    fvg_buy_price: float = 0.0         # Limit price = midpoint of the FVG gap
    fvg_top: float = 0.0               # Top of the FVG gap (Candle[i].low)
    fvg_bottom: float = 0.0            # Bottom of the FVG gap (Candle[i-2].high)
    fvg_direction: str = "bullish"     # "bullish" | "bearish" (only bullish detected currently)
    fvg_detected_time: str = ""        # ISO timestamp of detection (for age filter)
    fvg_order_id: int = 0              # Binance order ID of pending limit buy (0 = none)
    fvg_order_time: str = ""           # ISO timestamp when limit order was placed (for timeout)
    fvg_limit_active: bool = False     # True while a limit order is pending for this pair
    fvg_entry: bool = False            # True if the current open position was entered via FVG
    last_fvg_trade_time: str = ""      # ISO timestamp of last completed FVG trade (cooldown)

    # TP1 ladder state
    tp1_hit: bool = False              # True after TP1 sell has been executed
    position_open_time: str = ""       # ISO timestamp when current position was opened
    peak_gain_pct: float = 0.0         # Highest unrealised gain % since entry (profit protection)

    # Failed-exit re-entry suppression (pair-specific)
    last_failed_exit_reason: str = ""  # Most recent failed-exit action (EARLY_STAGNATION, TRAIL_STOP, HARD_STOP)
    last_failed_exit_time: str = ""    # ISO timestamp of that exit — cooldown measured from here

    # Per-pair dynamic parameters (recalculated every RSI cycle)
    dynamic_params: dict = field(default_factory=lambda: {
        "buy_threshold": 2.5,
        "tp1": 0.5,
        "hard_exit_minutes": 20.0,
        "_volatility_factor": 1.0,     # smoothed carry-forward for EMA
    })

    def to_dict(self) -> dict:
        """Serialize to dict for DB persistence."""
        d = asdict(self)
        d["last_trade"] = json.dumps(d["last_trade"])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PairState":
        """Deserialize from dict loaded from DB."""
        if isinstance(d.get("last_trade"), str):
            try:
                d["last_trade"] = json.loads(d["last_trade"])
            except (json.JSONDecodeError, TypeError):
                d["last_trade"] = {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class BotState:
    """Global bot state shared across all modules."""

    is_paused: bool = False
    bot_uptime_start: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Portfolio-level tracking
    portfolio_total_usdt: float = 0.0  # Set from wallet balance on startup
    usdt_balance: float = 0.0
    usdt_reserved: float = 0.0
    portfolio_start_value: float = 0.0
    portfolio_current_value: float = 0.0
    portfolio_pl_usdt: float = 0.0
    portfolio_pl_pct: float = 0.0
    portfolio_peak_value: float = 0.0
    open_positions_count: int = 0

    # Per-pair state keyed by pair string
    pairs: dict[str, PairState] = field(default_factory=dict)

    # Async lock for thread-safe access
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # Tracking
    total_trades: int = 0
    total_buys: int = 0
    total_sells: int = 0
    profitable_trades: int = 0

    # Drawdown tracking (for dynamic threshold + position sizing)
    drawdown_pct: float = 0.0           # Current drawdown from peak (0 = at peak)
    in_drawdown: bool = False           # True when drawdown > DRAWDOWN_TRIGGER_PCT

    # BTC crash filter (market-wide risk-off)
    btc_crash_active: bool = False      # True when BTC is dumping hard
    btc_crash_until: str = ""           # ISO timestamp when crash mode expires
    btc_15m_change_pct: float = 0.0     # BTC's 15m price change %

    # Long-term portfolio layer (separate from active trading)
    lt_capital_budget: float = 0.0      # Target LT capital (set at startup = total × LONG_TERM_ALLOCATION)
    lt_capital_deployed: float = 0.0    # Currently deployed in LT positions (at cost basis)
    long_term_states: dict = field(default_factory=dict)  # symbol → LongTermState

    # Futures paper-trading layer (simulated only — no real orders)
    futures_states: dict = field(default_factory=dict)       # symbol → FuturesPaperState
    futures_paper_realized_pnl: float = 0.0   # Cumulative realised PnL (paper)
    futures_paper_total_trades: int = 0        # Total closed futures paper positions
    futures_paper_winning_trades: int = 0      # Winning closed futures paper positions

    # Learned signal weights (loaded from signal_weights.py)
    signal_weights: dict = field(default_factory=lambda: {
        "rsi": 1.0, "macd": 1.0, "bollinger": 1.0, "gas": 1.0,
    })

    def to_dict(self) -> dict:
        """Serialize global state (excluding pairs and lock) for DB."""
        return {
            "is_paused": self.is_paused,
            "bot_uptime_start": self.bot_uptime_start.isoformat(),
            "portfolio_total_usdt": self.portfolio_total_usdt,
            "usdt_balance": self.usdt_balance,
            "usdt_reserved": self.usdt_reserved,
            "portfolio_start_value": self.portfolio_start_value,
            "portfolio_current_value": self.portfolio_current_value,
            "portfolio_pl_usdt": self.portfolio_pl_usdt,
            "portfolio_pl_pct": self.portfolio_pl_pct,
            "portfolio_peak_value": self.portfolio_peak_value,
            "open_positions_count": self.open_positions_count,
            "total_trades": self.total_trades,
            "total_buys": self.total_buys,
            "total_sells": self.total_sells,
            "profitable_trades": self.profitable_trades,
            "drawdown_pct": self.drawdown_pct,
            "in_drawdown": self.in_drawdown,
            "signal_weights": self.signal_weights,
            "lt_capital_budget": self.lt_capital_budget,
            "lt_capital_deployed": self.lt_capital_deployed,
            "long_term_states": {
                sym: lt_s.to_dict()
                for sym, lt_s in self.long_term_states.items()
            },
            "futures_states": {
                sym: fp_s.to_dict()
                for sym, fp_s in self.futures_states.items()
            },
            "futures_paper_realized_pnl": self.futures_paper_realized_pnl,
            "futures_paper_total_trades": self.futures_paper_total_trades,
            "futures_paper_winning_trades": self.futures_paper_winning_trades,
        }


def create_bot_state() -> BotState:
    """Create a fresh BotState from config with a PairState per trading pair."""
    state = BotState()
    for pair_cfg in TRADING_PAIRS:
        pair = pair_cfg["pair"]
        base = get_base_asset(pair)
        ps = PairState(
            pair=pair,
            base_asset=base,
            trade_amount_usdt=pair_cfg["trade_amount_usdt"],
            allocated_usdt=pair_cfg["trade_amount_usdt"],
            max_score=get_max_score(pair),
        )
        state.pairs[pair] = ps
    # Initialize long-term states for all configured LT assets
    if LONG_TERM_ENABLED:
        for pair in LONG_TERM_ASSETS:
            state.long_term_states[pair] = LongTermState(symbol=pair)
    # Initialize futures paper states for all trading pairs
    if FUTURES_PAPER_ENABLED:
        for pair_cfg in TRADING_PAIRS:
            pair = pair_cfg["pair"]
            state.futures_states[pair] = FuturesPaperState(symbol=pair)
    return state


def _ensure_state_table(conn: sqlite3.Connection):
    """Create the bot_state persistence table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            global_state TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pair_states (
            pair TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()


def save_state_to_db(state: BotState):
    """Persist current BotState to trades.db for restart recovery."""
    try:
        conn = sqlite3.connect(DB_PATH)
        _ensure_state_table(conn)
        now = datetime.now(timezone.utc).isoformat()

        global_json = json.dumps(state.to_dict())
        conn.execute(
            """INSERT OR REPLACE INTO bot_state (id, global_state, updated_at)
               VALUES (1, ?, ?)""",
            (global_json, now),
        )

        for pair, ps in state.pairs.items():
            ps_json = json.dumps(ps.to_dict())
            conn.execute(
                """INSERT OR REPLACE INTO pair_states (pair, state_json, updated_at)
                   VALUES (?, ?, ?)""",
                (pair, ps_json, now),
            )

        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save state to DB: {e}")


def load_state_from_db() -> BotState | None:
    """Load last known BotState from trades.db. Returns None if no saved state."""
    try:
        conn = sqlite3.connect(DB_PATH)
        _ensure_state_table(conn)

        row = conn.execute(
            "SELECT global_state FROM bot_state WHERE id = 1"
        ).fetchone()
        if row is None:
            conn.close()
            return None

        global_data = json.loads(row[0])
        state = BotState()

        # Restore global fields
        state.is_paused = global_data.get("is_paused", False)
        state.portfolio_total_usdt = global_data.get("portfolio_total_usdt", 0.0)
        state.usdt_balance = global_data.get("usdt_balance", 0.0)
        state.usdt_reserved = global_data.get("usdt_reserved", 0.0)
        state.portfolio_start_value = global_data.get("portfolio_start_value", 0.0)
        state.portfolio_current_value = global_data.get("portfolio_current_value", 0.0)
        state.portfolio_pl_usdt = global_data.get("portfolio_pl_usdt", 0.0)
        state.portfolio_pl_pct = global_data.get("portfolio_pl_pct", 0.0)
        state.portfolio_peak_value = global_data.get("portfolio_peak_value", 0.0)
        state.open_positions_count = global_data.get("open_positions_count", 0)
        state.total_trades = global_data.get("total_trades", 0)
        state.total_buys = global_data.get("total_buys", 0)
        state.total_sells = global_data.get("total_sells", 0)
        state.profitable_trades = global_data.get("profitable_trades", 0)
        state.drawdown_pct = global_data.get("drawdown_pct", 0.0)
        state.in_drawdown = global_data.get("in_drawdown", False)
        loaded_weights = global_data.get("signal_weights", {})
        # Strip any legacy keys from old DB rows
        for _legacy in ("sentiment", "fear_greed", "volume", "whale", "trends"):
            loaded_weights.pop(_legacy, None)
        defaults = {"rsi": 1.0, "macd": 1.0, "bollinger": 1.0, "gas": 1.0}
        defaults.update(loaded_weights)
        state.signal_weights = defaults

        # Restore long-term portfolio layer
        state.lt_capital_budget = global_data.get("lt_capital_budget", 0.0)
        state.lt_capital_deployed = global_data.get("lt_capital_deployed", 0.0)
        lt_states_raw = global_data.get("long_term_states", {})
        state.long_term_states = {}
        for sym, lt_d in lt_states_raw.items():
            try:
                state.long_term_states[sym] = LongTermState.from_dict(lt_d)
            except Exception:
                state.long_term_states[sym] = LongTermState(symbol=sym)

        # Restore futures paper layer
        state.futures_paper_realized_pnl   = global_data.get("futures_paper_realized_pnl", 0.0)
        state.futures_paper_total_trades    = global_data.get("futures_paper_total_trades", 0)
        state.futures_paper_winning_trades  = global_data.get("futures_paper_winning_trades", 0)
        fp_states_raw = global_data.get("futures_states", {})
        state.futures_states = {}
        if FUTURES_PAPER_ENABLED:
            for pair_cfg in TRADING_PAIRS:
                pair = pair_cfg["pair"]
                if pair in fp_states_raw:
                    try:
                        state.futures_states[pair] = FuturesPaperState.from_dict(fp_states_raw[pair])
                    except Exception:
                        state.futures_states[pair] = FuturesPaperState(symbol=pair)
                else:
                    state.futures_states[pair] = FuturesPaperState(symbol=pair)

        # Restore uptime (reset to now — uptime tracks current session)
        state.bot_uptime_start = datetime.now(timezone.utc)

        # Restore per-pair states
        rows = conn.execute("SELECT pair, state_json FROM pair_states").fetchall()
        saved_pairs = {}
        for pair, ps_json in rows:
            ps_data = json.loads(ps_json)
            saved_pairs[pair] = PairState.from_dict(ps_data)

        # Merge saved pairs with current config (config may have changed)
        for pair_cfg in TRADING_PAIRS:
            pair = pair_cfg["pair"]
            base = get_base_asset(pair)
            if pair in saved_pairs:
                ps = saved_pairs[pair]
                # Update config-driven fields in case they changed
                ps.trade_amount_usdt = pair_cfg["trade_amount_usdt"]
                ps.max_score = get_max_score(pair)
                ps.base_asset = base
                state.pairs[pair] = ps
            else:
                # New pair added to config — create fresh
                state.pairs[pair] = PairState(
                    pair=pair,
                    base_asset=base,
                    trade_amount_usdt=pair_cfg["trade_amount_usdt"],
                    allocated_usdt=pair_cfg["trade_amount_usdt"],
                    max_score=get_max_score(pair),
                )

        conn.close()
        logger.info("Restored bot state from trades.db")
        return state

    except Exception as e:
        logger.error(f"Failed to load state from DB: {e}")
        return None
