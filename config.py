import os
from dotenv import load_dotenv

load_dotenv()

# ─── Mode ────────────────────────────────────────────────────────────────
USE_TESTNET = True  # Switch to False for live trading — NEVER change without explicit instruction

# ─── API Keys ────────────────────────────────────────────────────────────
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_OWNER_ID = int(os.getenv("DISCORD_OWNER_ID", "0"))
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")

# ─── Signal Thresholds ───────────────────────────────────────────────────
RSI_BUY_THRESHOLD = 55          # RSI below this = oversold = buy signal (raised from 45 → more frequent entries)
RSI_SELL_THRESHOLD = 70         # RSI above this = overbought = sell signal (raised from 65 → hold deeper into momentum)
VOLUME_SPIKE_MULTIPLIER = 1.5   # Volume 1.5x the average = spike
CONFIDENCE_BUY_THRESHOLD = 2.7  # Min weighted score to trigger a buy (base; dynamic params scale it)
STOP_LOSS_PCT = 0.6             # Hard stop-loss: tighter for scalping; keeps losers smaller than prior 1.0%
PROFIT_TARGET_PCT = 1.2         # Fallback TP when ATR exits are off

# ─── ATR-Based Exits (replaces fixed TP for adaptive exits) ─────────────
ATR_PERIOD = 14                 # ATR lookback in candles (14 × 15m = 3.5 hours)
ATR_TRAILING_MULTIPLIER = 1.5   # Tighter ATR trail for scalp-sized risk
ATR_TP_MULTIPLIER = 2.0         # Reward still > risk, but reachable intraday
ATR_MAX_TRAIL_PCT = 0.02        # Max trail distance once in profit (2% of price)
ATR_TRAIL_ACTIVATION_PCT = 0.25 # Trail activates after meaningful move — avoids noise stop-outs on small bounces
USE_ATR_EXITS = True            # True = ATR-based exits, False = fixed % exits

# ─── Economic Viability Constants ────────────────────────────────────
# Round-trip execution cost assumption:
#   Binance spot fee: 0.1% per side = 0.2% round-trip
#   Slippage (market order): ~0.03–0.05%
#   Total: ~0.25% round-trip cost
# A trade must have realistic expected profit ABOVE this to be worthwhile.
ROUND_TRIP_COST_PCT = 0.25      # Assumed total round-trip cost (fee + slippage)
ECONOMIC_MIN_REWARD_PCT = 0.35  # Minimum expected reward for a setup to be economically viable
                                # = ROUND_TRIP_COST_PCT + ~0.10% buffer
                                # ATR × ATR_TP_MULTIPLIER × 100 must clear this before entry

# ─── Scalp TP/SL Ladder ──────────────────────────────────────────────
BASE_TP1_PCT = 0.35             # TP1 raised from 0.25% to clear round-trip costs at even minimum momentum_factor
                                # 0.35 × 0.75 (min momentum) = 0.2625% — above 0.25% break-even
TP1_SELL_RATIO = 0.5            # Sell half at TP1; let the rest run
BREAKEVEN_BUFFER_PCT = 0.15     # Move SL to entry +0.15% after TP1 (was 0.10%) — gives remaining position more buffer
TRAILING_ACTIVATION_PCT = 0.35  # Post-TP1 tight trailing: aligned with new TP1 level (was 0.25%)
TRAILING_DISTANCE_PCT = 0.25    # Post-TP1 trail distance: 0.25% below current price

# ─── Velocity Exits (Kill Stagnation) ────────────────────────────────
HARD_EXIT_MINUTES_BASE = 20     # Faster time stop for dead scalp positions
HARD_EXIT_PL_THRESHOLD_PCT = 0.15  # Dead trades should show some progress quickly

# ─── Scalping Execution Parameters ───────────────────────────────────
SCALP_MICRO_TP_PCT       = 0.35  # Micro TP aligned with new BASE_TP1_PCT (documentation only)
SCALP_HARD_TP_PCT        = 1.2   # Hard ceiling for stronger winners (unchanged)
SCALP_SOFT_TP_PCT        = 0.5   # Soft TP after a meaningful move (was 0.7%)
SCALP_SMALL_PROFIT_LOW   = 0.25  # Signal-sell floor: gross gain must clear round-trip costs (was 0.08%)
SCALP_SMALL_PROFIT_HIGH  = 0.35  # Signal-sell target: aligned with new TP1 (was 0.25%)
SCALP_EARLY_STAG_MINUTES_T1 = 6  # Slightly more room before killing dead trades
SCALP_EARLY_STAG_MIN_PCT_T1 = 0.05  # Truly dead trades only
SCALP_EARLY_STAG_MINUTES = 10     # Give good setups a bit more time
SCALP_EARLY_STAG_MIN_PCT = 0.10  # Lower threshold so winners are not cut prematurely
SCALP_SOFT_HOLD_MINUTES  = 15    # Soft hold a bit longer for developing moves
SCALP_HARD_HOLD_MINUTES  = 25    # Absolute max hold still short, but less aggressive
SCALP_STAGNATION_MIN_PCT = 0.15   # Softer requirement by soft-hold time
MAX_CONCURRENT_TRADES    = 3     # Max open positions; extras must be highest-confidence
# Volatility-adjusted exit windows are computed dynamically per pair via trend_factor

# ─── Dynamic Parameter Bounds ────────────────────────────────────────
DYNAMIC_VOL_FACTOR_MIN = 0.5    # Min volatility factor (low vol → lower threshold)
DYNAMIC_VOL_FACTOR_MAX = 1.5    # Max volatility factor (high vol → higher threshold)
DYNAMIC_VOL_SMOOTH = 0.2        # Exponential smoothing alpha for volatility factor

# ─── Regime Detection ───────────────────────────────────────────────────
REGIME_ATR_HIGH_THRESHOLD = 0.015   # ATR/price > this = volatile
REGIME_ATR_EXTREME_THRESHOLD = 0.03 # ATR/price > this = extreme (faster regime switch)
REGIME_TREND_THRESHOLD = 0.005      # |EMA20-EMA50|/price > this = trending
REGIME_EMA_FAST = 20                # Fast EMA period for trend detection
REGIME_EMA_SLOW = 50                # Slow EMA period for trend detection

# No-trade zone: skip trading when market is too dead for signals to be meaningful
NO_TRADE_ATR_PCT = 0.003            # ATR/price below this = dead market
NO_TRADE_TREND_PCT = 0.001          # Trend strength below this = no direction

# ─── Crypto Market Filters ───────────────────────────────────────────────
# BTC crash filter: when BTC dumps hard, enter risk-off mode for ALL pairs
BTC_CRASH_PCT = -2.0                # BTC 15m change below this = crash
BTC_CRASH_THRESHOLD_BOOST = 1.0     # Add +1 to buy threshold during crash (was +2, reduced)
BTC_CRASH_SIZE_MULTIPLIER = 0.25    # 25% position size during crash
BTC_CRASH_COOLDOWN = 900            # Stay in risk-off for 15 min after crash detected

# Wick filter: reject buys during exhaustion spikes
WICK_RATIO_THRESHOLD = 0.6          # Upper wick > 60% of candle range = exhaustion

# Minimum edge: skip trades where expected move < fees + spread
MIN_EDGE_PCT = 0.3                  # Require at least 0.3% expected edge (was 0.4)

# Minimum trade notional: prevent dust trades at order level
MIN_TRADE_NOTIONAL = 20.0           # Hard floor: never place order below $20 USDT

# RSI confirmation: when RSI is the primary trigger, require at least one of
# OB or flow to reach this level to confirm buyers are actually responding to the dip.
# Below this on both signals = dip without buyer follow-through → penalty applied.
RSI_OB_FLOW_MIN = 0.55              # OB or flow must reach 0.55 to confirm a bounce

# Spread filter: skip buys when bid/ask spread is too wide (live only)
SPREAD_MAX_PCT = 0.0015             # 0.15% max spread — wider = hidden cost kills edge

# Spike filter: skip buys when price moved sharply on the last WebSocket tick
SPIKE_FILTER_PCT = 0.003            # 0.3% single-tick move = spike, avoid chasing

# Unstable candle filter: skip buys when the most recent candle range is abnormally
# large relative to ATR. ATR is the 14-period average candle range, so 2.0× means
# the last candle was twice the average — a genuine outlier, not normal noise.
UNSTABLE_CANDLE_ATR_MULT = 2.0      # candle_range_pct > ATR_pct × this → unstable_candle block

# Fast-score entry path: lightweight pre-filter for high-conviction setups
FAST_SCORE_THRESHOLD = 2.5          # fast_score >= this: threshold -0.5
FAST_SCORE_THRESHOLD_REDUCTION = 0.5  # Reduction for soft fast-entry
FAST_SCORE_THRESHOLD_HARD = 3.0     # fast_score >= this: threshold -1.0 (strong conviction)
FAST_SCORE_THRESHOLD_REDUCTION_HARD = 1.0  # Reduction for hard fast-entry

# ─── Signal Categories (prevent double-counting correlated signals) ──────
# Each category has a max contribution cap to total confidence
SIGNAL_CATEGORY_CAPS = {
    "mean_reversion": 1.5,   # RSI + Bollinger
    "momentum": 1.5,         # MACD
    "onchain": 1.0,          # Etherscan gas activity
}

# ─── Regime-Based Category Modifiers ─────────────────────────────────────
# Multipliers applied to signal categories based on current market regime.
# Encodes the key insight: mean-reversion signals are reliable in ranges but
# trap-like in strong trends; momentum signals (MACD) lead in trends, chop in ranges.
REGIME_MODIFIERS = {
    "trending": {
        "mean_reversion": 0.9,  # RSI/BB useful for dip entries in trends
        "momentum": 1.4,        # MACD works well
        "onchain": 1.0,
    },
    "ranging": {
        "mean_reversion": 1.4,  # RSI/BB are king in ranges
        "momentum": 0.6,        # MACD chops in ranges
        "onchain": 1.0,
    },
    "choppy_volatile": {
        "mean_reversion": 0.7,  # Unreliable — price whipsaws
        "momentum": 0.7,        # MACD gives false signals
        "onchain": 1.2,
    },
    "trending_volatile": {
        "mean_reversion": 0.5,  # Very unreliable in fast moves
        "momentum": 1.3,        # Trend is strong — follow it
        "onchain": 1.0,
    },
}

# ─── Regime-Specific Signal Modifiers (Priority 2) ───────────────────────
# Per-signal multipliers applied ON TOP of REGIME_MODIFIERS category mults.
# Provides signal-level granularity:
#   - RSI/Bollinger boosted in ranging (mean-reversion edge), reduced in strong trends
#   - MACD more reliable in trending, less in chop
#   - Gas (Etherscan) has uniform edge regardless of regime (onchain is regime-agnostic)
REGIME_SIGNAL_MODIFIERS = {
    "ranging": {
        "rsi": 1.3,         # Oversold in range = high-conviction bounce signal
        "bollinger": 1.3,   # BB lower band in range = reliable reversal
        "macd": 0.8,        # MACD crossovers in ranges are often false
        "gas": 1.0,
    },
    "trending": {
        "rsi": 0.8,         # RSI oversold in downtrend = catching a falling knife
        "bollinger": 0.8,   # BB lower band in trend = not a reversal, continuation
        "macd": 1.2,        # MACD follows the trend — trustworthy
        "gas": 1.0,
    },
    "choppy_volatile": {
        "rsi": 0.7,         # All mean-reversion signals are traps in choppy vol
        "bollinger": 0.7,
        "macd": 0.8,
        "gas": 1.1,
    },
    "trending_volatile": {
        "rsi": 0.6,         # RSI absolutely unreliable in velocity moves
        "bollinger": 0.6,
        "macd": 1.1,        # Only momentum signals have edge
        "gas": 1.0,
    },
}

# ─── Adaptive Weight Bounds ──────────────────────────────────────────────
SIGNAL_WEIGHT_MIN = 0.5         # Floor for learned signal weights
SIGNAL_WEIGHT_MAX = 1.5         # Ceiling for learned signal weights
SIGNAL_WEIGHT_DECAY = 0.95      # Exponential decay for older trades (recent trades matter more)
SIGNAL_WEIGHT_MIN_TRADES = 20   # Minimum trades before weights diverge from 1.0
SIGNAL_WEIGHT_WINDOW = 300      # Rolling window: learn from last N trades
SIGNAL_WEIGHT_NORMALIZE_RATE = 0.02  # Per-cycle decay toward 1.0 (2% per 5 min)

# ─── Dynamic Threshold (drawdown-based risk modulation) ──────────────────
DRAWDOWN_THRESHOLD_BOOST = 0.5  # Add +0.5 to buy threshold when in drawdown (was +1, reduced)
DRAWDOWN_TRIGGER_PCT = 5.0      # Portfolio drawdown % that triggers risk reduction
DRAWDOWN_SIZE_REDUCTION = 0.5   # Halve position size when in drawdown
DOWNTREND_SIZE_MULTIPLIER = 0.6 # Reduce position to 60% in downtrend — limits damage if continuation occurs

# ─── Trade Cooldown ──────────────────────────────────────────────────────
TRADE_COOLDOWN_SECONDS = 300        # 5 min between trades on same asset (was 600)
TRADE_COOLDOWN_TRENDING = 180       # 3 min cooldown in trending regimes
COOLDOWN_BYPASS_CONFIDENCE = 5.0    # Confidence above this can bypass cooldown entirely
COOLDOWN_BYPASS_REGIMES = {"trending", "trending_volatile"}  # Regimes that allow bypass

# ─── Failed-Exit Re-Entry Suppression ────────────────────────────────────
# After a losing/weak exit, suppress re-entry on the same pair for a configurable
# window to prevent immediate churn (BUY → EARLY_STAGNATION → BUY → repeat).
# Applied pair-specifically; does not affect other pairs.
FAILED_EXIT_COOLDOWN_STAGNATION = 720   # 12 min after EARLY_STAGNATION / EARLY_STAGNATION_2
FAILED_EXIT_COOLDOWN_TRAIL_STOP = 600   # 10 min after TRAIL_STOP
FAILED_EXIT_COOLDOWN_HARD_STOP  = 1500  # 25 min after HARD_STOP

# ─── Futures Paper-Trading Layer ─────────────────────────────────────────
# Simulated-only futures trading layer.  No real orders are ever placed.
# Completely separate from spot scalping and long-term layers.
# Priority: SHORT positions in bearish/downtrend conditions.
FUTURES_PAPER_ENABLED               = True    # Master switch for the entire futures paper layer
FUTURES_SHORT_ENABLED               = True    # Allow paper SHORT positions
FUTURES_LONG_ENABLED                = True    # Paper LONG positions — momentum scalps only (hedged isolation)
FUTURES_MAX_LEVERAGE                = 2.0     # Maximum simulated leverage multiplier (conservative)
FUTURES_POSITION_SIZE_USDT          = 20.0    # Max notional per futures paper position (USDT) — ATR sizing uses this as cap
# ── Priority 1: Volatility-Adjusted Score Gate ────────────────────────────
# Minimum bearish/bullish score required before a futures entry qualifies.
# Scaled by current regime's ATR instead of a single static value:
#   ranging       → FUTURES_MIN_SCORE_RANGING   (1.2) — quieter, score hard to reach otherwise
#   trending      → FUTURES_MIN_SCORE            (1.5) — baseline
#   *_volatile    → FUTURES_MIN_SCORE_VOLATILE   (1.8) — noisy, require stronger conviction
FUTURES_MIN_SCORE                   = 1.5     # Baseline (trending regime)
FUTURES_MIN_SCORE_RANGING           = 1.2     # Lower bar for quiet, low-ATR ranging markets
FUTURES_MIN_SCORE_VOLATILE          = 1.8     # Higher bar during high-volatility regimes
# ── Priority 3: ATR-Based Position Sizing ────────────────────────────────
# Instead of always risking a fixed $20, size each position so a hard stop
# always loses exactly FUTURES_RISK_PCT_PER_TRADE % of total portfolio.
# Formula: notional = (portfolio × RISK_PCT) / (atr_pct × ATR_STOP_MULT)
# Result is capped at FUTURES_POSITION_SIZE_USDT and floored at $5.
FUTURES_ATR_SIZING_ENABLED          = True    # Enable dynamic ATR-based position sizing
FUTURES_RISK_PCT_PER_TRADE          = 0.005   # 0.5% of portfolio = max dollar loss per futures trade
FUTURES_ATR_STOP_MULT               = 2.0     # ATR multiplier for expected stop-distance estimate
FUTURES_MAX_CONCURRENT_POSITIONS    = 2       # Max open futures paper positions at once
FUTURES_TP1_PCT                     = 0.50    # First profit target (% price move in direction) — raised 0.40→0.50 for better edge
FUTURES_TP1_RATIO                   = 0.50    # Fraction of position closed at TP1
FUTURES_HARD_STOP_PCT               = 0.60    # Max loss before forced exit (% of entry price)
FUTURES_TRAIL_ACTIVATION_PCT        = 1.5     # Only start trailing after 1.5% profit (swing-trader: was 0.40)
FUTURES_TRAIL_DISTANCE_PCT          = 0.8     # Give it 0.8% room to move (swing-trader: was 0.20)
FUTURES_STAGNATION_MINUTES_T1       = 720     # 12 hours — swing-trader patience (was 8 min scalper)
FUTURES_STAGNATION_THRESHOLD_T1     = -1.0    # Only exit if losing >1% after 12h (was +0.05%)
FUTURES_STAGNATION_MINUTES_T2       = 1440    # 24 hours — swing-trader T2 (was 15 min scalper)
FUTURES_STAGNATION_THRESHOLD_T2     = -0.50   # Only exit if losing >0.5% after 24h (was +0.10%)
FUTURES_TIME_STOP_MINUTES           = 4320    # 72 hours / 3 days absolute max hold (swing-trader: was 45)
FUTURES_REENTRY_COOLDOWN_SECS       = 600     # Min gap before re-entering same symbol after failed exit
FUTURES_ROUND_TRIP_COST_PCT         = 0.30    # Assumed cost for futures round-trip (taker fees only)
FUTURES_MIN_REWARD_PCT              = 0.50    # Min expected reward after costs
FUTURES_ATR_UNECONOMIC_MULT         = 2.0     # Same as spot ATR_TP_MULTIPLIER for expected move calc
FUTURES_CHECK_INTERVAL              = 15      # Evaluation loop interval (seconds)
# ── Funding Fee Simulation ────────────────────────────────────────────────
# Real Binance perpetual funding settles every 8 hours.
# Typical rate: 0.01% per interval (positive = longs pay shorts).
# We apply a symmetric cost to both sides (longs AND shorts pay) so paper
# P&L reflects the true "rent" of holding a leveraged position.
FUTURES_FUNDING_RATE_PCT            = 0.01    # 0.01% of notional per 8-hour interval
FUTURES_FUNDING_INTERVAL_SECS       = 28800   # 8 hours in seconds
# Entry quality filters (hard blocks — not penalties)
FUTURES_OB_BEAR_MAX                 = 0.45    # OB imbalance must be BELOW this for SHORT confirmation
FUTURES_FLOW_BEAR_MAX               = 0.45    # Flow ratio must be BELOW this for SHORT confirmation
FUTURES_MACD_HISTOGRAM_MAX          = 0.0     # MACD histogram must be BELOW this (negative = bearish momentum)
FUTURES_OB_BULL_MIN                 = 0.55    # OB imbalance must be ABOVE this for LONG confirmation
FUTURES_FLOW_BULL_MIN               = 0.55    # Flow ratio must be ABOVE this for LONG confirmation
# Triple-long safety: if spot + LT both hold a coin, block LONG futures on that coin
FUTURES_TRIPLE_LONG_BLOCK           = True    # Enable triple-long risk protection

# ─── Long-Term Portfolio Layer ───────────────────────────────────────────
# Passive hold layer that captures macro movement independent of scalping.
# Capital is split: LONG_TERM_ALLOCATION goes here, the rest to active trading.
# Entry/exit use the same effective_score signal already computed each cycle —
# no new indicators, just stricter thresholds and much lower evaluation frequency.
LONG_TERM_ENABLED = True
LONG_TERM_ALLOCATION = 0.25             # 25% of portfolio for long-term — conservative initial rollout
LONG_TERM_ASSETS = ["BTCUSDT"]          # BTC only for first deployment; add ETH when validated
LONG_TERM_ENTRY_THRESHOLD = 2.5         # Slightly above scalp floor — allows accumulation in good-not-perfect conditions
LONG_TERM_EXIT_THRESHOLD = 1.2          # Exit only on genuine collapse (not normal score drift or brief weakness)
LONG_TERM_TIMEFRAME = "1h"             # Conceptual frame — LT decisions run at low frequency
LONG_TERM_CHECK_INTERVAL = 600          # Re-evaluate every 10 minutes — this is not a scalper
LONG_TERM_DOWNTREND_EXIT_CYCLES = 8     # 8 × 10 min = 80 min of sustained downtrend before exiting
                                        # Prevents noise-exits on short-term dips (BTC dips are normal)

# ─── Portfolio Management ────────────────────────────────────────────────
PORTFOLIO_TOTAL_USDT = 0        # 0 = auto-detect from wallet balance on startup
PORTFOLIO_RESERVE_PCT = 0.50    # Keep 50% in reserve — never deploy more than half
PORTFOLIO_MAX_POSITIONS = 6     # Allow positions in all 6 coins
TRADE_SIZE_PCT = 0.03           # Each trade = 3% of portfolio (raised from 2% → more capital deployed)
MAX_POSITION_PCT = 0.10         # Max 10% of portfolio in any single coin
PORTFOLIO_REBALANCE_INTERVAL = 300  # Recalculate allocations every 5 minutes
VOLATILITY_LOOKBACK = 24        # Hours of price history to calculate volatility

# ─── Active Trading Pairs ────────────────────────────────────────────────
TRADING_PAIRS = [
    {"pair": "ETHUSDT",  "trade_amount_usdt": 1.80},
    {"pair": "BTCUSDT",  "trade_amount_usdt": 1.80},
    {"pair": "BNBUSDT",  "trade_amount_usdt": 1.50},
    {"pair": "XRPUSDT",  "trade_amount_usdt": 1.50},
    {"pair": "BCHUSDT",  "trade_amount_usdt": 1.20},
    {"pair": "SUIUSDT",  "trade_amount_usdt": 1.20},
]

# ─── Signal Applicability Per Coin ────────────────────────────────────────
# Etherscan gas activity signal: ETH-only (on-chain activity tracking)
ETHERSCAN_COINS = {"ETH"}

# Max confidence score per pair (RSI + MACD + BB; ETH adds Etherscan gas)
MAX_SCORE = {
    "ETH": 4,
    "BTC": 3,
    "BNB": 3,
    "XRP": 3,
    "BCH": 3,
    "SUI": 3,
}

def get_base_asset(pair: str) -> str:
    """Extract base asset from pair string, e.g. 'ETHUSDT' -> 'ETH'."""
    return pair.replace("USDT", "")

def get_max_score(pair: str) -> int:
    """Get the maximum confidence score for a given pair."""
    base = get_base_asset(pair)
    return MAX_SCORE.get(base, 6)

# ─── Check Intervals (seconds) ──────────────────────────────────────────
INTERVAL_ORDERBOOK = 10         # Orderbook imbalance + trade flow per pair
INTERVAL_RSI = 10               # RSI + MACD + BB recalculation per pair

# ─── FVG Entry Controls ────────────────────────────────────────────────────
# These govern when FVG signals translate to actual orders.
FVG_MIN_GAP_PCT = 0.001         # Minimum gap size: 0.1% of price — smaller = noise
FVG_MAX_AGE_SECONDS = 300       # Discard gaps older than 5 minutes (one 5m candle)
FVG_FAST_SCORE_MIN = 2.0        # Minimum fast_score required for any FVG-driven entry
FVG_OB_MIN = 0.5                # Microstructure floor for FVG entry (softer than main 0.4 gate)
FVG_EARLY_TP_PCT = 0.25         # Accelerated take-profit for FVG entries — tighter than micro TP
FVG_COOLDOWN_SECONDS = 300      # 5 min between FVG trades on the same pair
FVG_ORDER_TIMEOUT = 60          # Cancel unfilled limit order after 60 seconds
INTERVAL_ETHERSCAN = 300        # Etherscan on-chain data (ETH only)
INTERVAL_FVG = 60              # Fair Value Gap detection per pair (5m candles)
INTERVAL_DISCORD_PANEL = 45    # 45 s: 4 channels per cycle — stays well under Discord's per-channel rate limit
INTERVAL_PORTFOLIO_SNAPSHOT = 1800  # Save portfolio snapshot every 30 min

# ─── Discord ─────────────────────────────────────────────────────────────
# ─── Orderbook / Flow Signals ─────────────────────────────────────────────
# Microstructure signals fetched from Binance orderbook + recent trades.
# Imbalance = bid_volume / (bid + ask), top 5 levels.
# Flow ratio = buyer-aggressor volume / total, last 50 trades.
# Both are EMA-smoothed across polling cycles to reduce noise.
OB_IMBALANCE_BULL = 0.6         # Imbalance above this = buyers stacking → bullish
OB_IMBALANCE_BEAR = 0.4         # Imbalance below this = sellers stacking → bearish
OB_FLOW_BULL = 0.6              # Flow ratio above this = buyers lifting asks → bullish
OB_FLOW_BEAR = 0.4              # Flow ratio below this = sellers hitting bids → bearish
OB_SCORE_BOOST = 0.5            # Each bullish signal adds this to effective_score
OB_EMA_ALPHA = 0.3              # Smoothing alpha (≈ 5-tick period, ~50s at 10s interval)

DISCORD_PANEL_CHANNEL_NAME = "trading-bot"   # Legacy fallback name (kept for import compat)

# ── Multi-Channel Terminal Topology ──────────────────────────────────────────
# Each key maps to a Discord channel ID.
# MAIN    — Active positions + control buttons (Pause, Chart, Abandon…)
# SIGNALS — Trade ledger: entry/exit notifications, read-only
# AUDIT   — Entry block reasons (why the bot isn't entering), read-only
# EQUITY  — Realized PnL, funding costs, win-rate accounting, read-only
# ADMIN   — System health, uptime, error log, master overrides + buttons
DISCORD_CHANNELS = {
    "MAIN":    1486961092906455061,
    "SIGNALS": 1486961144206856252,
    "AUDIT":   1486961180529393685,
    "EQUITY":  1486961201907896410,
    "ADMIN":   1485302487740055632,
}

# ─── Binance Endpoints ───────────────────────────────────────────────────
BINANCE_TESTNET_URL = "https://testnet.binance.vision"
BINANCE_TESTNET_WS = "wss://testnet.binance.vision/ws"
BINANCE_LIVE_URL = "https://api.binance.com"
BINANCE_LIVE_WS = "wss://stream.binance.com:9443/ws"
