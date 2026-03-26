"""Google Trends — search interest per coin.

Uses pytrends library (no API key needed).
Rising trend = retail FOMO building = early bullish signal.
Random delay between calls to avoid rate limiting.
"""

import asyncio
import random
import logging
from functools import partial
from config import GOOGLE_TRENDS_KEYWORDS

logger = logging.getLogger("trading_bot.signals.google_trends")

_last_known: dict[str, int] = {}


def _fetch_trend_sync(keyword: str) -> int:
    """Synchronous Google Trends fetch (run in executor)."""
    try:
        from pytrends.request import TrendReq

        pytrends = TrendReq(hl="en-US", tz=360, timeout=(10, 25))
        pytrends.build_payload([keyword], timeframe="now 7-d", geo="")
        df = pytrends.interest_over_time()

        if df is None or df.empty or keyword not in df.columns:
            return 0

        # Get the latest value (0-100 scale)
        return int(df[keyword].iloc[-1])
    except Exception as e:
        logger.error(f"Google Trends sync fetch failed for '{keyword}': {e}")
        return -1  # Sentinel: caller uses last known


async def fetch_google_trend(base_asset: str) -> int:
    """Fetch Google Trends interest score for a coin.

    Args:
        base_asset: e.g. "ETH", "BTC"

    Returns:
        int trend score 0-100
    """
    keyword = GOOGLE_TRENDS_KEYWORDS.get(base_asset)
    if not keyword:
        return _last_known.get(base_asset, 0)

    try:
        # Random delay to avoid rate limiting (Google is aggressive with 429s)
        delay = random.uniform(5.0, 15.0)
        await asyncio.sleep(delay)

        loop = asyncio.get_running_loop()
        score = await loop.run_in_executor(None, partial(_fetch_trend_sync, keyword))

        if score < 0:
            # Fetch failed — return last known
            return _last_known.get(base_asset, 0)

        _last_known[base_asset] = score
        return score

    except Exception as e:
        logger.error(f"Google Trends fetch failed for {base_asset}: {e}")
        return _last_known.get(base_asset, 0)
