"""CoinGecko — volume, market cap, price change data per coin.

Free tier, no API key required. Called once per pair per cycle.
Detects volume spikes (1.5x 7-day average).
"""

import logging
import aiohttp
from config import COINGECKO_IDS, VOLUME_SPIKE_MULTIPLIER

logger = logging.getLogger("trading_bot.signals.coingecko")

_last_known: dict[str, dict] = {}
_volume_history: dict[str, list[float]] = {}  # Rolling volume samples per coin

BASE_URL = "https://api.coingecko.com/api/v3/coins"


def _default_result() -> dict:
    return {
        "volume_24h": 0.0,
        "market_cap": 0.0,
        "change_pct_24h": 0.0,
        "volume_spike": False,
    }


async def fetch_coingecko(base_asset: str) -> dict:
    """Fetch volume, market cap, and price change data for a coin.

    Args:
        base_asset: e.g. "ETH", "BTC"

    Returns:
        dict with volume_24h, market_cap, change_pct_24h, volume_spike
    """
    coin_id = COINGECKO_IDS.get(base_asset)
    if not coin_id:
        return _last_known.get(base_asset, _default_result())

    try:
        url = (
            f"{BASE_URL}/{coin_id}"
            f"?localization=false&tickers=false&community_data=false"
            f"&developer_data=false&sparkline=false"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429:
                    logger.warning("CoinGecko rate limited")
                    return _last_known.get(base_asset, _default_result())
                if resp.status != 200:
                    logger.warning(f"CoinGecko returned {resp.status} for {base_asset}")
                    return _last_known.get(base_asset, _default_result())

                data = await resp.json(content_type=None)
                market = data.get("market_data", {})

                volume_24h = float(market.get("total_volume", {}).get("usd", 0))
                market_cap = float(market.get("market_cap", {}).get("usd", 0))
                change_pct = float(market.get("price_change_percentage_24h", 0) or 0)

                # Volume spike detection: compare to rolling average
                if base_asset not in _volume_history:
                    _volume_history[base_asset] = []
                history = _volume_history[base_asset]
                history.append(volume_24h)
                # Keep last 10 samples (~10 minutes of data)
                if len(history) > 10:
                    history.pop(0)
                avg_volume = sum(history) / len(history) if history else volume_24h
                volume_spike = (
                    len(history) >= 3  # Need at least 3 samples for meaningful average
                    and volume_24h > avg_volume * VOLUME_SPIKE_MULTIPLIER
                )

                result = {
                    "volume_24h": volume_24h,
                    "market_cap": market_cap,
                    "change_pct_24h": change_pct,
                    "volume_spike": volume_spike,
                }

                _last_known[base_asset] = result
                return result

    except Exception as e:
        logger.error(f"CoinGecko fetch failed for {base_asset}: {e}")
        return _last_known.get(base_asset, _default_result())
