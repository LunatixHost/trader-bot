"""Market Dynamics Engine — centralized kline cache and dynamic pairlist.

Two components:

DataProvider
    Thread-safe (asyncio-safe) kline cache keyed by "{pair}_{interval}".
    Requests for the same pair+interval within cache_expiry seconds are served
    from memory instead of hitting the Binance REST API.  This eliminates
    duplicate kline fetches when multiple signal pollers (RSI, FVG, etc.) run
    concurrently on the same pair.

    Cache key: "{pair}_{interval}"  e.g. "ETHUSDT_3m"
    TTL: 60 s (one candle period for 1m; safe margin for 3m/5m intervals)

VolumePairList
    Dynamically ranks all active USDT pairs by 24h quote volume and keeps a
    rolling top-N list.  Refreshed every VOLUME_PAIRLIST_INTERVAL seconds in a
    background loop.  Purely informational at present — does not override
    TRADING_PAIRS; used for market-breadth insight.

Usage (from bot.py):
    from market_data import data_provider, volume_pairlist
    # data_provider is wired into fetch_technical_indicators + detect_fvg
    # volume_pairlist is refreshed by market_dynamics_loop()
"""

import asyncio
import logging
import time
from typing import Dict, List, Tuple

logger = logging.getLogger("trading_bot.market_data")

# How long a cached kline set is considered fresh (seconds).
# 60 s is safe for 1m candles; 3m/5m sets change even more slowly.
_CACHE_TTL_SECS = 60

# How many top USDT pairs to track (by 24h quote volume)
_VOLUME_PAIRLIST_MAX = 20

# How often to refresh the dynamic pairlist (seconds)
VOLUME_PAIRLIST_INTERVAL = 300  # 5 minutes


class DataProvider:
    """Centralized kline cache with TTL expiry.

    All kline fetches in signal pollers should go through
    ``await data_provider.get_klines(pair, interval, limit)``
    instead of calling ``binance_client.get_klines()`` directly.
    The first caller fetches from REST; subsequent callers within
    the TTL window receive the cached copy for free.
    """

    def __init__(self, binance_client):
        # binance_client: the BinanceClient instance from binance_client.py
        self._client = binance_client
        # {cache_key: (klines_list, fetched_at_timestamp)}
        self._cache: Dict[str, Tuple[List, float]] = {}

    async def get_klines(
        self, pair: str, interval: str = "1h", limit: int = 100
    ) -> List:
        """Return OHLCV klines, serving from cache when data is fresh.

        Falls back to a direct REST call and caches the result on miss or expiry.
        Returns an empty list on failure (mirrors BinanceClient.get_klines behaviour).
        """
        cache_key = f"{pair}_{interval}"
        now = time.time()

        if cache_key in self._cache:
            cached_klines, fetched_at = self._cache[cache_key]
            if now - fetched_at < _CACHE_TTL_SECS:
                return cached_klines

        # Cache miss or expired — fetch from REST
        try:
            klines = await self._client.get_klines(pair, interval=interval, limit=limit)
            if klines:
                self._cache[cache_key] = (klines, now)
            return klines
        except Exception as e:
            logger.error(f"DataProvider fetch failed for {pair}/{interval}: {e}")
            # Return stale cache if available rather than empty list
            if cache_key in self._cache:
                logger.warning(
                    f"DataProvider returning stale cache for {pair}/{interval}"
                )
                return self._cache[cache_key][0]
            return []

    def invalidate(self, pair: str, interval: str | None = None):
        """Manually evict a cache entry.

        If interval is None, evicts all entries for the pair across all intervals.
        Useful after a forced price action (e.g. large market sell) that makes
        the cached klines stale before the TTL expires.
        """
        if interval:
            self._cache.pop(f"{pair}_{interval}", None)
        else:
            to_remove = [k for k in self._cache if k.startswith(f"{pair}_")]
            for k in to_remove:
                del self._cache[k]


class VolumePairList:
    """Dynamic ranking of top USDT pairs by 24h quote volume.

    Not used for trade execution — purely for market-breadth monitoring and
    future adaptive pairlist features.  The list is logged on each refresh so
    the operator can see which pairs are most active.
    """

    def __init__(self, binance_client, max_pairs: int = _VOLUME_PAIRLIST_MAX):
        self._client = binance_client
        self.max_pairs = max_pairs
        self.active_pairs: List[str] = []
        self._last_refresh: float = 0.0

    async def refresh_pairs(self):
        """Fetch all tickers, sort by quoteVolume, keep top-N USDT pairs."""
        try:
            tickers = await self._client.client.get_ticker()
            usdt_pairs = [
                t for t in tickers
                if isinstance(t, dict) and t.get("symbol", "").endswith("USDT")
            ]
            sorted_pairs = sorted(
                usdt_pairs,
                key=lambda x: float(x.get("quoteVolume", 0)),
                reverse=True,
            )
            self.active_pairs = [p["symbol"] for p in sorted_pairs[: self.max_pairs]]
            self._last_refresh = time.time()
            logger.info(
                f"VolumePairList refreshed — top {len(self.active_pairs)} pairs: "
                + ", ".join(self.active_pairs[:5])
                + ("…" if len(self.active_pairs) > 5 else "")
            )
        except Exception as e:
            logger.error(f"VolumePairList refresh failed: {e}")


# ─── Module-level singletons ──────────────────────────────────────────
# Initialized lazily in init_market_data() below so that the BinanceClient
# is fully connected before these objects try to use it.

data_provider: DataProvider | None = None
volume_pairlist: VolumePairList | None = None


def init_market_data(binance_client):
    """Create the module-level DataProvider and VolumePairList singletons.

    Must be called once during bot startup, after BinanceClient.connect().
    """
    global data_provider, volume_pairlist
    data_provider   = DataProvider(binance_client)
    volume_pairlist = VolumePairList(binance_client)
    logger.info("Market Dynamics Engine initialised (DataProvider + VolumePairList)")


async def market_dynamics_loop():
    """Background loop: warm the kline cache and refresh the volume pairlist.

    Runs as a standalone asyncio task.  On startup it immediately triggers a
    pairlist refresh; afterwards it repeats every VOLUME_PAIRLIST_INTERVAL
    seconds.  The DataProvider cache warms itself on-demand (first fetch per
    pair populates the cache), so no explicit pre-warm loop is needed here.
    """
    from config import TRADING_PAIRS

    if data_provider is None or volume_pairlist is None:
        logger.error("market_dynamics_loop called before init_market_data()")
        return

    logger.info("Market Dynamics Engine loop started")

    while True:
        try:
            await volume_pairlist.refresh_pairs()

            # Pre-warm the kline cache for all configured trading pairs so
            # the first poll_rsi / poll_fvg evaluation never waits on a cold
            # REST request at startup.
            for pair_cfg in TRADING_PAIRS:
                pair = pair_cfg["pair"]
                await data_provider.get_klines(pair, interval="3m", limit=120)
                await data_provider.get_klines(pair, interval="5m", limit=15)
                await asyncio.sleep(0.1)  # gentle rate-limit spacing

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"market_dynamics_loop error: {e}")

        await asyncio.sleep(VOLUME_PAIRLIST_INTERVAL)
