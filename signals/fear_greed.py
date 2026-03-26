"""Fear & Greed Index — market-wide sentiment gauge.

Source: Alternative.me (free, no API key).
Called once per cycle and shared across all pairs.
"""

import logging
import aiohttp

logger = logging.getLogger("trading_bot.signals.fear_greed")

_last_known: dict = {"score": 50, "label": "Neutral"}

ENDPOINT = "https://api.alternative.me/fng/?limit=1"


async def fetch_fear_greed() -> dict:
    """Fetch the current Fear & Greed Index.

    Returns:
        dict with 'score' (int 0-100) and 'label' (str)
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(ENDPOINT, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning(f"Fear & Greed API returned {resp.status}")
                    return _last_known.copy()

                data = await resp.json(content_type=None)
                if not data or "data" not in data or not data["data"]:
                    return _last_known.copy()

                entry = data["data"][0]
                result = {
                    "score": int(entry.get("value", 50)),
                    "label": entry.get("value_classification", "Neutral"),
                }

                _last_known.update(result)
                return result

    except Exception as e:
        logger.error(f"Fear & Greed fetch failed: {e}")
        return _last_known.copy()
