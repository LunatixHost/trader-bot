"""Regime Detection — classify current market conditions per pair.

Uses ATR (volatility) + EMA spread (directionality) to classify into one of:
- "trending"           — moderate ATR, clear direction
- "ranging"            — low ATR, no clear direction
- "choppy_volatile"    — high ATR, no clear direction (danger zone)
- "trending_volatile"  — high ATR, clear direction (strong move)

Hysteresis: regime must persist for REGIME_PERSIST_CANDLES consecutive
readings before switching. Prevents flickering at threshold boundaries.

Called once per RSI cycle per pair. Uses the same 15m candle data
that rsi.py already fetches, so no extra API calls needed.
"""

import logging
import pandas as pd
from config import (
    ATR_PERIOD,
    REGIME_ATR_HIGH_THRESHOLD,
    REGIME_ATR_EXTREME_THRESHOLD,
    REGIME_TREND_THRESHOLD,
    REGIME_EMA_FAST,
    REGIME_EMA_SLOW,
)

logger = logging.getLogger("trading_bot.regime")

# Hysteresis: require this many consecutive readings before regime switch
REGIME_PERSIST_CANDLES = 3

# Per-pair hysteresis state: tracks candidate regime and how long it's been seen
_regime_candidates: dict[str, dict] = {}
# key = pair, value = {"candidate": str, "count": int, "confirmed": str}


def compute_atr(highs: pd.Series, lows: pd.Series, closes: pd.Series,
                period: int = ATR_PERIOD) -> float:
    """Compute Average True Range from OHLC data.

    ATR = smoothed average of True Range over `period` candles.
    True Range = max(high-low, |high-prev_close|, |low-prev_close|)

    Args:
        highs: Series of high prices
        lows: Series of low prices
        closes: Series of close prices
        period: Lookback period (default from config)

    Returns:
        Current ATR value (absolute price units)
    """
    prev_close = closes.shift(1)
    tr1 = highs - lows
    tr2 = (highs - prev_close).abs()
    tr3 = (lows - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Wilder's smoothing (same as RSI uses)
    atr = true_range.ewm(alpha=1.0 / period, adjust=False).mean()
    return float(atr.iloc[-1]) if not atr.empty and pd.notna(atr.iloc[-1]) else 0.0


def compute_trend_strength(closes: pd.Series,
                           fast: int = REGIME_EMA_FAST,
                           slow: int = REGIME_EMA_SLOW) -> float:
    """Compute trend strength as |EMA_fast - EMA_slow| / price.

    Values:
    - > 0.005: clear directional trend
    - < 0.005: no clear direction (ranging or choppy)

    Returns:
        Trend strength as fraction of price (0.0 to ~0.05)
    """
    if len(closes) < slow:
        return 0.0

    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    price = closes.iloc[-1]

    if price <= 0:
        return 0.0

    return abs(float(ema_fast.iloc[-1]) - float(ema_slow.iloc[-1])) / price


def compute_trend_direction(closes: pd.Series,
                            fast: int = REGIME_EMA_FAST,
                            slow: int = REGIME_EMA_SLOW) -> str:
    """Compute trend direction from EMA crossover.

    Returns:
        "up" if EMA_fast > EMA_slow (bullish)
        "down" if EMA_fast < EMA_slow (bearish)
        "flat" if insufficient data
    """
    if len(closes) < slow:
        return "flat"

    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()

    if float(ema_fast.iloc[-1]) > float(ema_slow.iloc[-1]):
        return "up"
    else:
        return "down"


def classify_regime(atr_pct: float, trend_strength: float) -> str:
    """Classify market regime from ATR% and trend strength.

    Args:
        atr_pct: ATR as percentage of price (e.g. 0.02 = 2%)
        trend_strength: |EMA20 - EMA50| / price

    Returns:
        One of: "trending", "ranging", "choppy_volatile", "trending_volatile"
    """
    is_volatile = atr_pct > REGIME_ATR_HIGH_THRESHOLD
    is_trending = trend_strength > REGIME_TREND_THRESHOLD

    if is_volatile and is_trending:
        return "trending_volatile"
    elif is_volatile and not is_trending:
        return "choppy_volatile"
    elif is_trending:
        return "trending"
    else:
        return "ranging"


def _apply_hysteresis(pair: str, raw_regime: str, atr_pct: float = 0.0) -> str:
    """Apply hysteresis to prevent regime flickering at boundaries.

    The regime must be seen for N consecutive readings before it replaces
    the current confirmed regime. In extreme volatility (ATR > extreme
    threshold), N is reduced to 2 for faster response.

    Args:
        pair: Trading pair identifier (for per-pair tracking)
        raw_regime: The newly classified regime (before hysteresis)
        atr_pct: Current ATR as % of price (for adaptive confirmation)

    Returns:
        The confirmed regime (may lag behind raw by a few candles)
    """
    # Adaptive: fewer candles needed in extreme volatility
    required = REGIME_PERSIST_CANDLES
    if atr_pct > REGIME_ATR_EXTREME_THRESHOLD:
        required = max(1, REGIME_PERSIST_CANDLES - 1)  # 2 instead of 3

    if pair not in _regime_candidates:
        _regime_candidates[pair] = {
            "candidate": raw_regime,
            "count": 1,
            "confirmed": raw_regime,
        }
        return raw_regime

    state = _regime_candidates[pair]

    if raw_regime == state["confirmed"]:
        state["candidate"] = raw_regime
        state["count"] = 0
        return raw_regime

    if raw_regime == state["candidate"]:
        state["count"] += 1
        if state["count"] >= required:
            old = state["confirmed"]
            state["confirmed"] = raw_regime
            state["count"] = 0
            logger.info(f"Regime switch {pair}: {old} → {raw_regime} (confirmed after {required} candles)")
            return raw_regime
        else:
            return state["confirmed"]
    else:
        state["candidate"] = raw_regime
        state["count"] = 1
        return state["confirmed"]


def detect_regime(highs: pd.Series, lows: pd.Series,
                  closes: pd.Series, pair: str = "") -> dict:
    """Full regime detection from OHLC candle data.

    Args:
        highs: Series of high prices
        lows: Series of low prices
        closes: Series of close prices
        pair: Trading pair identifier (for hysteresis tracking)

    Returns:
        dict with: atr, atr_pct, trend_strength, regime
    """
    price = float(closes.iloc[-1]) if not closes.empty else 0.0

    atr = compute_atr(highs, lows, closes)
    atr_pct = atr / price if price > 0 else 0.0
    trend = compute_trend_strength(closes)
    direction = compute_trend_direction(closes)
    raw_regime = classify_regime(atr_pct, trend)

    # Apply hysteresis to prevent flickering (adaptive: faster in extreme vol)
    regime = _apply_hysteresis(pair, raw_regime, atr_pct) if pair else raw_regime

    logger.debug(
        f"Regime {pair}: {regime} (raw: {raw_regime}) | "
        f"ATR: {atr:.4f} ({atr_pct:.4%}) | Trend: {trend:.5f}"
    )

    return {
        "atr": atr,
        "atr_pct": atr_pct,
        "trend_strength": trend,
        "trend_direction": direction,
        "regime": regime,
    }
