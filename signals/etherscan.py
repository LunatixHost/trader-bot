"""Etherscan — on-chain gas and network activity data.

ETH-only signal. Returns early with defaults for all other coins.
High gas + high pending tx count = high network activity = bullish signal.
"""

import logging
import aiohttp
from config import ETHERSCAN_API_KEY, ETHERSCAN_COINS

logger = logging.getLogger("trading_bot.signals.etherscan")

_last_known: dict = {"gas_price": 0, "activity": "normal"}

BASE_URL = "https://api.etherscan.io/api"


async def fetch_etherscan(base_asset: str) -> dict:
    """Fetch Ethereum gas price and network activity.

    Args:
        base_asset: Must be "ETH" — returns defaults for others

    Returns:
        dict with 'gas_price' (int, Gwei) and 'activity' ("high"|"normal"|"low")
    """
    if base_asset not in ETHERSCAN_COINS:
        return {"gas_price": 0, "activity": "normal"}

    if not ETHERSCAN_API_KEY:
        return _last_known.copy()

    try:
        async with aiohttp.ClientSession() as session:
            # Fetch gas price
            gas_params = {
                "module": "gastracker",
                "action": "gasoracle",
                "apikey": ETHERSCAN_API_KEY,
            }
            async with session.get(
                BASE_URL, params=gas_params,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                gas_price = 0
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data.get("status") == "1" and data.get("result"):
                        gas_price = int(float(data["result"].get("ProposeGasPrice", 0)))

            # Fetch pending transaction count as activity proxy
            pending_params = {
                "module": "proxy",
                "action": "eth_getBlockByNumber",
                "tag": "pending",
                "boolean": "false",
                "apikey": ETHERSCAN_API_KEY,
            }
            pending_count = 0
            async with session.get(
                BASE_URL, params=pending_params,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    result = data.get("result")
                    if result and isinstance(result, dict):
                        txs = result.get("transactions", [])
                        pending_count = len(txs)

            # Determine activity level
            if gas_price > 50 and pending_count > 100:
                activity = "high"
            elif gas_price < 15:
                activity = "low"
            else:
                activity = "normal"

            result = {"gas_price": gas_price, "activity": activity}
            _last_known.update(result)
            return result

    except Exception as e:
        logger.error(f"Etherscan fetch failed: {e}")
        return _last_known.copy()
