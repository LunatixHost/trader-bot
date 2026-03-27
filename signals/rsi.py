"""Technical indicators: RSI, MACD, Bollinger Bands, ATR, and regime detection.

All indicators are calculated from the same Binance OHLCV fetch (one API call per pair).
Falls back to manual calculation if pandas-ta is unavailable (Python 3.14 compat).

ATR and regime detection are computed here (not in a separate call) because
they use the same candle data — no extra API request needed.
"""

import logging
import pandas as pd
from regime import detect_regime
from config import ATR_PERIOD

logger = logging.getLogger("trading_bot.signals.rsi")

# Try pandas-ta; fall back to manual calculations
try:
    import pandas_ta as ta
    HAS_PANDAS_TA = True
    logger.info("pandas-ta available — using library for indicators")
except ImportError:
    HAS_PANDAS_TA = False
    logger.warning("pandas-ta not available — using manual indicator calculations")

# Last known values per pair (fallback on error)
_last_known: dict[str, dict] = {}


def _default_result() -> dict:
    return {
        "rsi": 50.0,
        "macd": 0.0,
        "macd_signal": 0.0,
        "macd_histogram": 0.0,
        "macd_crossover": "none",
        "bb_upper": 0.0,
        "bb_middle": 0.0,
        "bb_lower": 0.0,
        "bb_position": "inside",
        "bb_squeeze": False,
        # Wick detection
        "wick_ratio": 0.0,
        # ATR & Regime (computed from same candles, no extra API call)
        "atr": 0.0,
        "atr_pct": 0.0,
        "avg_atr_20": 0.0,          # 20-period rolling mean of ATR series
        "trend_strength": 0.0,
        "trend_direction": "flat",  # "up" | "down" | "flat"
        "regime": "ranging",
        # Liquidity sweep scoring
        "liquidity_sweep_score": 0.0,
    }


# ─── Manual Indicator Calculations (Wilder's EMA fallback) ───────────

def _wilders_ema(values: pd.Series, period: int) -> pd.Series:
    """Wilder's Exponential Moving Average (alpha = 1/period)."""
    alpha = 1.0 / period
    return values.ewm(alpha=alpha, adjust=False).mean()


def _manual_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Calculate RSI using Wilder's smoothing method."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    avg_gain = _wilders_ema(gain, period)
    avg_loss = _wilders_ema(loss, period)

    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def _manual_macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Calculate MACD line, signal line, and histogram."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _manual_bbands(
    close: pd.Series, length: int = 20, std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Calculate Bollinger Bands (upper, middle, lower)."""
    middle = close.rolling(window=length).mean()
    rolling_std = close.rolling(window=length).std()
    upper = middle + (rolling_std * std)
    lower = middle - (rolling_std * std)
    return upper, middle, lower


# ─── Main Function ────────────────────────────────────────────────────

async def fetch_technical_indicators(
    binance_client, pair: str, data_provider=None
) -> dict:
    """Fetch OHLCV data and calculate RSI, MACD, and Bollinger Bands.

    Args:
        binance_client:  The BinanceClient instance (used as fallback if no cache)
        pair:            Trading pair string, e.g. "ETHUSDT"
        data_provider:   Optional DataProvider for cached kline fetches.
                         When provided, avoids duplicate REST calls across pollers.

    Returns:
        dict with rsi, macd bundle, bb bundle
    """
    try:
        if data_provider is not None:
            klines = await data_provider.get_klines(pair, interval="3m", limit=120)
        else:
            klines = await binance_client.get_klines(pair, interval="3m", limit=120)
        if not klines or len(klines) < 30:
            logger.warning(f"Insufficient kline data for {pair}: {len(klines)} candles")
            return _last_known.get(pair, _default_result())

        df = pd.DataFrame(klines, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore",
        ])
        df["close"] = df["close"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["open"] = df["open"].astype(float)
        df["volume"] = df["volume"].astype(float)

        result = _default_result()

        # ── RSI ──
        if HAS_PANDAS_TA:
            rsi_series = df.ta.rsi(length=14)
            if rsi_series is not None and not rsi_series.empty:
                result["rsi"] = float(rsi_series.iloc[-1])
        else:
            rsi_series = _manual_rsi(df["close"], 14)
            if not rsi_series.empty:
                val = rsi_series.iloc[-1]
                if pd.notna(val):
                    result["rsi"] = float(val)

        # ── MACD ──
        if HAS_PANDAS_TA:
            macd_df = df.ta.macd(fast=12, slow=26, signal=9)
            if macd_df is not None and not macd_df.empty:
                cols = macd_df.columns.tolist()
                macd_val = float(macd_df[cols[0]].iloc[-1]) if pd.notna(macd_df[cols[0]].iloc[-1]) else 0.0
                signal_val = float(macd_df[cols[1]].iloc[-1]) if pd.notna(macd_df[cols[1]].iloc[-1]) else 0.0
                hist_val = float(macd_df[cols[2]].iloc[-1]) if pd.notna(macd_df[cols[2]].iloc[-1]) else 0.0

                result["macd"] = macd_val
                result["macd_signal"] = signal_val
                result["macd_histogram"] = hist_val
        else:
            macd_line, signal_line, histogram = _manual_macd(df["close"])
            if not macd_line.empty:
                result["macd"] = float(macd_line.iloc[-1]) if pd.notna(macd_line.iloc[-1]) else 0.0
                result["macd_signal"] = float(signal_line.iloc[-1]) if pd.notna(signal_line.iloc[-1]) else 0.0
                result["macd_histogram"] = float(histogram.iloc[-1]) if pd.notna(histogram.iloc[-1]) else 0.0

        # MACD crossover detection (compare last two values)
        if HAS_PANDAS_TA and macd_df is not None and len(macd_df) >= 2:
            cols = macd_df.columns.tolist()
            prev_macd = float(macd_df[cols[0]].iloc[-2]) if pd.notna(macd_df[cols[0]].iloc[-2]) else 0.0
            prev_signal = float(macd_df[cols[1]].iloc[-2]) if pd.notna(macd_df[cols[1]].iloc[-2]) else 0.0
            curr_macd = result["macd"]
            curr_signal = result["macd_signal"]

            if prev_macd <= prev_signal and curr_macd > curr_signal:
                result["macd_crossover"] = "bullish"
            elif prev_macd >= prev_signal and curr_macd < curr_signal:
                result["macd_crossover"] = "bearish"
            else:
                result["macd_crossover"] = "none"
        elif not HAS_PANDAS_TA and len(df) >= 2:
            macd_line, signal_line, _ = _manual_macd(df["close"])
            if len(macd_line) >= 2:
                prev_m = float(macd_line.iloc[-2]) if pd.notna(macd_line.iloc[-2]) else 0.0
                prev_s = float(signal_line.iloc[-2]) if pd.notna(signal_line.iloc[-2]) else 0.0
                curr_m = result["macd"]
                curr_s = result["macd_signal"]

                if prev_m <= prev_s and curr_m > curr_s:
                    result["macd_crossover"] = "bullish"
                elif prev_m >= prev_s and curr_m < curr_s:
                    result["macd_crossover"] = "bearish"

        # ── Bollinger Bands ──
        if HAS_PANDAS_TA:
            bb_df = df.ta.bbands(length=20, std=2)
            if bb_df is not None and not bb_df.empty:
                cols = bb_df.columns.tolist()
                # pandas-ta bbands columns: BBL, BBM, BBU, BBB, BBP
                for col in cols:
                    col_lower = col.lower()
                    if "bbl" in col_lower:
                        val = bb_df[col].iloc[-1]
                        result["bb_lower"] = float(val) if pd.notna(val) else 0.0
                    elif "bbm" in col_lower:
                        val = bb_df[col].iloc[-1]
                        result["bb_middle"] = float(val) if pd.notna(val) else 0.0
                    elif "bbu" in col_lower:
                        val = bb_df[col].iloc[-1]
                        result["bb_upper"] = float(val) if pd.notna(val) else 0.0
        else:
            bb_upper, bb_middle, bb_lower = _manual_bbands(df["close"])
            if not bb_upper.empty:
                result["bb_upper"] = float(bb_upper.iloc[-1]) if pd.notna(bb_upper.iloc[-1]) else 0.0
                result["bb_middle"] = float(bb_middle.iloc[-1]) if pd.notna(bb_middle.iloc[-1]) else 0.0
                result["bb_lower"] = float(bb_lower.iloc[-1]) if pd.notna(bb_lower.iloc[-1]) else 0.0

        # BB position: where is current price relative to the bands?
        current_close = float(df["close"].iloc[-1])
        if result["bb_upper"] > 0 and result["bb_lower"] > 0:
            if current_close >= result["bb_upper"]:
                result["bb_position"] = "above_upper"
            elif current_close <= result["bb_lower"]:
                result["bb_position"] = "below_lower"
            else:
                result["bb_position"] = "inside"

            # Squeeze detection: bands narrowing (bandwidth < 4% of middle)
            if result["bb_middle"] > 0:
                bandwidth = (result["bb_upper"] - result["bb_lower"]) / result["bb_middle"]
                result["bb_squeeze"] = bandwidth < 0.04

        # ── Wick ratio (last candle — detects exhaustion spikes) ──
        last_high = float(df["high"].iloc[-1])
        last_low = float(df["low"].iloc[-1])
        last_close = float(df["close"].iloc[-1])
        last_open = float(df["open"].iloc[-1])
        candle_range = last_high - last_low
        if candle_range > 0:
            # Upper wick: distance from close to high (for buys, we care about upward rejection)
            upper_wick = last_high - max(last_close, last_open)
            result["wick_ratio"] = upper_wick / candle_range
        else:
            result["wick_ratio"] = 0.0
        # Candle range as % of close — compared against ATR% to detect violent candles
        result["candle_range_pct"] = (candle_range / last_close * 100) if last_close > 0 else 0.0

        # ── ATR & Regime Detection (uses same candle data) ──
        try:
            regime_data = detect_regime(df["high"], df["low"], df["close"], pair=pair)
            result["atr"] = regime_data["atr"]
            result["atr_pct"] = regime_data["atr_pct"]
            result["trend_strength"] = regime_data["trend_strength"]
            result["trend_direction"] = regime_data.get("trend_direction", "flat")
            result["regime"] = regime_data["regime"]
        except Exception as e:
            logger.warning(f"Regime detection failed for {pair}: {e}")

        # ── 20-period average ATR (for dynamic volatility_factor) ──
        try:
            prev_close = df["close"].shift(1)
            tr = pd.concat([
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ], axis=1).max(axis=1)
            atr_series = tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean()
            if len(atr_series) >= 20:
                result["avg_atr_20"] = float(atr_series.iloc[-20:].mean())
            elif len(atr_series) > 0:
                result["avg_atr_20"] = float(atr_series.mean())
        except Exception as e:
            logger.warning(f"avg_atr_20 calculation failed for {pair}: {e}")

        # ── Liquidity Sweep Scoring (15m candles) ──
        # A sweep occurs when the last candle's low breaks below the recent low
        # AND the candle closes ABOVE that level — a false breakdown / spring.
        # Scaled by how deep the wick went relative to ATR (deeper = stronger signal).
        try:
            if len(df) >= 22:
                recent_low = float(df["low"].iloc[-21:-1].min())  # prior 20 candles
                last_low = float(df["low"].iloc[-1])
                last_close = float(df["close"].iloc[-1])
                atr_val = result["atr"]

                if last_low < recent_low and last_close > recent_low and atr_val > 0:
                    sweep_depth = recent_low - last_low
                    # Scale: 1 ATR of sweep depth → +1.5 score; capped at 1.5
                    sweep_score = min(1.5, (sweep_depth / atr_val) * 1.5)
                    result["liquidity_sweep_score"] = round(sweep_score, 3)
                else:
                    result["liquidity_sweep_score"] = 0.0
        except Exception as e:
            logger.warning(f"Liquidity sweep calc failed for {pair}: {e}")

        # Cache as last known value
        _last_known[pair] = result
        return result

    except Exception as e:
        logger.error(f"Error calculating indicators for {pair}: {e}")
        return _last_known.get(pair, _default_result())
