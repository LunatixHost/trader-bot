"""CryptoCompare — social media stats per coin.

Tracks Reddit posts, comments, and Twitter mentions.
Rising social mentions = retail interest building.
"""

import logging
import aiohttp
from config import CRYPTOCOMPARE_API_KEY, CRYPTOCOMPARE_COIN_IDS

logger = logging.getLogger("trading_bot.signals.cryptocompare")

_last_known: dict[str, dict] = {}

ENDPOINT = "https://min-api.cryptocompare.com/data/social/coin/latest"


def _default_result() -> dict:
    return {"social_score": 0, "reddit_posts": 0, "twitter_mentions": 0}


async def fetch_social_stats(base_asset: str) -> dict:
    """Fetch social media activity stats for a coin.

    Args:
        base_asset: e.g. "ETH", "BTC"

    Returns:
        dict with 'social_score' (int), 'reddit_posts', 'twitter_mentions'
    """
    coin_id = CRYPTOCOMPARE_COIN_IDS.get(base_asset)
    if coin_id is None:
        return _last_known.get(base_asset, _default_result())

    try:
        headers = {}
        if CRYPTOCOMPARE_API_KEY:
            headers["authorization"] = f"Apikey {CRYPTOCOMPARE_API_KEY}"

        params = {"coinId": coin_id}

        async with aiohttp.ClientSession() as session:
            async with session.get(
                ENDPOINT, params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"CryptoCompare returned {resp.status} for {base_asset}")
                    return _last_known.get(base_asset, _default_result())

                data = await resp.json(content_type=None)
                social_data = data.get("Data", {})

                # Reddit data
                reddit = social_data.get("Reddit", {})
                reddit_posts = int(reddit.get("posts_per_day", 0))
                reddit_comments = int(reddit.get("comments_per_day", 0))

                # Twitter data
                twitter = social_data.get("Twitter", {})
                twitter_mentions = int(twitter.get("followers", 0))

                # Simple social score: combine Reddit activity and Twitter presence
                # Scale: 0 (dead) to 100 (viral)
                reddit_score = min(reddit_posts + reddit_comments, 500)
                twitter_score = min(twitter_mentions // 1000, 500)
                social_score = min(
                    int((reddit_score + twitter_score) / 10), 100
                )

                result = {
                    "social_score": social_score,
                    "reddit_posts": reddit_posts,
                    "twitter_mentions": twitter_mentions,
                }

                _last_known[base_asset] = result
                return result

    except Exception as e:
        logger.error(f"CryptoCompare fetch failed for {base_asset}: {e}")
        return _last_known.get(base_asset, _default_result())
