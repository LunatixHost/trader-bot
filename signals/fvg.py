"""Fair Value Gap (FVG) detection on 5-minute candles.

A bullish FVG is created when a strong impulse candle leaves an unfilled
price gap between the prior candle's high and the next candle's low:

    Candle[i-2].high  < Candle[i].low
    Candle[i-1] = impulse (strongly bullish body, body/range >= 0.5)

The gap is defined by:
    gap_bottom = Candle[i-2].high
    gap_top    = Candle[i].low

Entry price (limit) = midpoint = (gap_top + gap_bottom) / 2

We scan the most recent N candle triplets, stopping at the first valid FVG
that the current price has not yet left far behind (within 0.5% above gap_top).
"""

import logging
from datetime import datetime, timezone
import pandas as pd
from config import FVG_MIN_GAP_PCT

logger = logging.getLogger("trading_bot.signals.fvg")

_DEFAULT = {
    "fvg_detected": False,
    "fvg_buy_price": 0.0,
    "fvg_top": 0.0,
    "fvg_bottom": 0.0,
    "fvg_direction": "bullish",
    "fvg_detected_time": "",
}

# How many 5m candle triplets to scan back for a recent FVG
_SCAN_WINDOW = 6
# Price must be within this % above gap_top to still consider the FVG reachable
_PRICE_PROXIMITY_PCT = 0.005  # 0.5%
# Minimum body/range ratio for the impulse candle to qualify
_MIN_IMPULSE_RATIO = 0.50


async def detect_fvg(binance_client, pair: str) -> dict:
    """Detect a bullish Fair Value Gap on 5m candles.

    Args:
        binance_client: BinanceClient instance (for kline fetches).
        pair: e.g. "ETHUSDT"

    Returns:
        dict with keys:
            fvg_detected  (bool)  — True if a valid, reachable FVG was found
            fvg_buy_price (float) — limit entry = midpoint of the gap
            fvg_top       (float) — upper bound of the gap (Candle[i].low)
            fvg_bottom    (float) — lower bound of the gap (Candle[i-2].high)
    """
    try:
        # Fetch a few extra candles beyond the scan window so the triplet at
        # position 0 has valid prior context.
        klines = await binance_client.get_klines(
            pair, interval="5m", limit=_SCAN_WINDOW + 5
        )
        if not klines or len(klines) < 3:
            return _DEFAULT.copy()

        df = pd.DataFrame(klines, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].astype(float)

        current_price = float(df["close"].iloc[-1])

        # Scan most-recent triplets first (newest gap is most actionable)
        for i in range(len(df) - 1, 1, -1):
            c_prev2 = df.iloc[i - 2]   # candle before the impulse
            c_mid   = df.iloc[i - 1]   # impulse candle
            c_curr  = df.iloc[i]       # candle after the impulse

            # ── Impulse quality check ──
            mid_body  = c_mid["close"] - c_mid["open"]
            mid_range = c_mid["high"] - c_mid["low"]
            if mid_range <= 0 or mid_body / mid_range < _MIN_IMPULSE_RATIO:
                continue  # Not a strong bullish impulse candle

            gap_bottom = float(c_prev2["high"])
            gap_top    = float(c_curr["low"])

            if gap_top <= gap_bottom:
                continue  # No gap (candles overlap)

            # ── Minimum gap size: filter out noise (gaps < 0.1% of price) ──
            if current_price > 0 and (gap_top - gap_bottom) / current_price < FVG_MIN_GAP_PCT:
                continue

            # ── Reachability check ──
            # Current price must be close enough that a pullback into the gap
            # is realistic — skip gaps the price already blew far past.
            if current_price > gap_top * (1 + _PRICE_PROXIMITY_PCT):
                continue

            midpoint = (gap_top + gap_bottom) / 2.0
            now_iso = datetime.now(timezone.utc).isoformat()
            logger.info(
                f"FVG detected on {pair}: gap ${gap_bottom:.4f}–${gap_top:.4f} "
                f"mid ${midpoint:.4f} (current ${current_price:.4f}, "
                f"gap={((gap_top - gap_bottom) / current_price):.3%})"
            )
            return {
                "fvg_detected":      True,
                "fvg_buy_price":     round(midpoint, 8),
                "fvg_top":           round(gap_top, 8),
                "fvg_bottom":        round(gap_bottom, 8),
                "fvg_direction":     "bullish",
                "fvg_detected_time": now_iso,
            }

        return _DEFAULT.copy()

    except Exception as e:
        logger.error(f"FVG detection error for {pair}: {e}")
        return _DEFAULT.copy()
