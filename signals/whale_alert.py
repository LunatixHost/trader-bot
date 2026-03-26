"""Whale detection — large transaction tracking via blockchain explorer APIs.

Tracks movements > $500k for ETH, BTC, BNB, XRP using free chain-specific APIs:
  ETH → Etherscan API (ETHERSCAN_API_KEY)
  BNB → BSCScan via Etherscan (same ETHERSCAN_API_KEY — BSCScan migrated into Etherscan)
  BTC → Blockchain.com (free, no key)
  XRP → XRPL Data API (free, no key)

BCH and SUI are skipped (no reliable free explorer with tx filtering).
Direction: to-exchange = sell pressure, from-exchange = accumulation.
"""

import logging
import aiohttp
from config import ETHERSCAN_API_KEY, WHALE_ALERT_COINS

logger = logging.getLogger("trading_bot.signals.whale_alert")

_last_known: dict[str, str] = {}

MIN_VALUE_USD = 500_000

# ─── Known exchange hot-wallet addresses ─────────────────────────────
# Representative subset of the largest exchanges. In production you'd
# use a larger database; these cover the biggest flows.

EXCHANGE_ADDRESSES_ETH = {
    "0x28c6c06298d514db089934071355e5743bf21d60",  # Binance 14
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549",  # Binance 7
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d",  # Binance 8
    "0x56eddb7aa87536c09ccc2793473599fd21a8b17f",  # Binance 16
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43",  # Coinbase 10
    "0x503828976d22510aad0201ac7ec88293211d23da",  # Coinbase 2
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3",  # Coinbase 3
    "0x2910543af39aba0cd09dbb2d50200b3e800a63d2",  # Kraken 4
    "0x267be1c1d684f78cb4f6a176c4911b741e4ffdc0",  # Kraken 13
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b",  # OKX 1
    "0x98ec059dc3adfbdd63429227115656b07c44a2e5",  # OKX 5
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40",  # Bybit
    "0xd24400ae8bfebb18ca49be86258a3c749cf46853",  # Gemini 4
}

EXCHANGE_ADDRESSES_BNB = {
    "0x28c6c06298d514db089934071355e5743bf21d60",  # Binance 14 (same on BSC)
    "0x8894e0a0c962cb723c1ef8580d0d3bfe8d9ae3c8",  # Binance hot
    "0xe2fc31f816a9b94326492132018c3aecc4a93ae1",  # Binance hot 2
    "0x3c783c21a0383057d128bae431894a5c19f9cf06",  # Binance hot 3
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40",  # Bybit
}

BTC_EXCHANGE_ADDRESSES = {
    "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo",           # Binance cold
    "3M219KR5vEneNb47ewrPfWyb5jQ2DjxRP6",           # Binance cold 2
    "bc1qm34lsc65zpw79lxes69zkqmk6ee3ewf0j77s3",   # Binance cold 3
    "1NDyJtNTjmwk5xPNhjgAMu4HDHigtobu1s",           # Binance hot
    "3Kzh9qAqVWQhEsfQz7zEQL1EuSx5tyNLNS",           # Coinbase cold
    "bc1q7cyrfmck2ffu2ud3rn5l5a8yv6f0chkp0zpemf",  # Coinbase
}

XRP_EXCHANGE_ADDRESSES = {
    "rfkE1aSy9G8Upk4JssnwBxhEv5p4mn2KTy",  # Binance
    "rEb8TK3gBgk5auZkwc6sHnwrGVJH8DuaLh",  # Binance 2
    "rLHzPsX6oXkzU2qL12kHCH08x8FY5nDmo4",  # Binance 3
    "rDsbeomae4FXwgQTJp9Rs64Qg9vDiTCdBv",  # Bitstamp
    "rNxp4h8apvRis6mJf9Sh8C6iRxfrDWN7AV",  # Bitstamp hot
    "rUobSiUpYH2S97Mgb4E3CDXkUPiyf97hGq",  # Uphold
    "r9cZA1mLK5R5Am25ArfXFmqgNwjZgnfk59",  # Ripple
    "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq",  # GateHub
}


def _determine_signal(to_exchange_value: float, from_exchange_value: float) -> str:
    """Determine whale signal from exchange flow direction."""
    if to_exchange_value > from_exchange_value and to_exchange_value > 0:
        return "sell_pressure"
    elif from_exchange_value > to_exchange_value and from_exchange_value > 0:
        return "accumulation"
    return "neutral"


# ─── ETH: Etherscan ──────────────────────────────────────────────────

async def _fetch_eth_whales(session: aiohttp.ClientSession, eth_price: float) -> str:
    """Detect large ETH transfers via Etherscan txlist on a Binance hot wallet."""
    if not ETHERSCAN_API_KEY:
        return _last_known.get("ETH", "neutral")

    try:
        target_addr = "0x28c6c06298d514db089934071355e5743bf21d60"  # Binance 14
        params = {
            "module": "account",
            "action": "txlist",
            "address": target_addr,
            "page": 1,
            "offset": 50,
            "sort": "desc",
            "apikey": ETHERSCAN_API_KEY,
        }

        async with session.get(
            "https://api.etherscan.io/api", params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return _last_known.get("ETH", "neutral")

            data = await resp.json(content_type=None)
            txs = data.get("result", [])
            if not isinstance(txs, list):
                return _last_known.get("ETH", "neutral")

            to_exchange = 0.0
            from_exchange = 0.0

            for tx in txs:
                value_eth = int(tx.get("value", "0")) / 1e18
                value_usd = value_eth * eth_price
                if value_usd < MIN_VALUE_USD:
                    continue

                to_addr = tx.get("to", "").lower()
                from_addr = tx.get("from", "").lower()

                # Inbound to any known exchange = selling pressure
                if to_addr in EXCHANGE_ADDRESSES_ETH and from_addr not in EXCHANGE_ADDRESSES_ETH:
                    to_exchange += value_usd
                # Outbound from exchange to non-exchange = accumulation
                elif from_addr in EXCHANGE_ADDRESSES_ETH and to_addr not in EXCHANGE_ADDRESSES_ETH:
                    from_exchange += value_usd

            return _determine_signal(to_exchange, from_exchange)

    except Exception as e:
        logger.error(f"ETH whale fetch failed: {e}")
        return _last_known.get("ETH", "neutral")


# ─── BNB: BSCScan (same Etherscan key) ───────────────────────────────

async def _fetch_bnb_whales(session: aiohttp.ClientSession, bnb_price: float) -> str:
    """Detect large BNB transfers via BSCScan (Etherscan-managed, same API key)."""
    if not ETHERSCAN_API_KEY:
        return _last_known.get("BNB", "neutral")

    try:
        target_addr = "0x8894e0a0c962cb723c1ef8580d0d3bfe8d9ae3c8"  # Binance BSC hot
        params = {
            "module": "account",
            "action": "txlist",
            "address": target_addr,
            "page": 1,
            "offset": 50,
            "sort": "desc",
            "apikey": ETHERSCAN_API_KEY,
        }

        async with session.get(
            "https://api.bscscan.com/api", params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return _last_known.get("BNB", "neutral")

            data = await resp.json(content_type=None)
            txs = data.get("result", [])
            if not isinstance(txs, list):
                return _last_known.get("BNB", "neutral")

            to_exchange = 0.0
            from_exchange = 0.0

            for tx in txs:
                value_bnb = int(tx.get("value", "0")) / 1e18
                value_usd = value_bnb * bnb_price
                if value_usd < MIN_VALUE_USD:
                    continue

                to_addr = tx.get("to", "").lower()
                from_addr = tx.get("from", "").lower()

                if to_addr in EXCHANGE_ADDRESSES_BNB and from_addr not in EXCHANGE_ADDRESSES_BNB:
                    to_exchange += value_usd
                elif from_addr in EXCHANGE_ADDRESSES_BNB and to_addr not in EXCHANGE_ADDRESSES_BNB:
                    from_exchange += value_usd

            return _determine_signal(to_exchange, from_exchange)

    except Exception as e:
        logger.error(f"BNB whale fetch failed: {e}")
        return _last_known.get("BNB", "neutral")


# ─── BTC: Blockchain.com ─────────────────────────────────────────────

async def _fetch_btc_whales(session: aiohttp.ClientSession, btc_price: float) -> str:
    """Detect large BTC transfers via Blockchain.com API (free, no key)."""
    try:
        url = "https://blockchain.info/unconfirmed-transactions?format=json"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return _last_known.get("BTC", "neutral")

            data = await resp.json(content_type=None)
            txs = data.get("txs", [])

            to_exchange = 0.0
            from_exchange = 0.0

            for tx in txs[:100]:
                # Check outputs for exchange deposits
                for out in tx.get("out", []):
                    addr = out.get("addr", "")
                    value_usd = (out.get("value", 0) / 1e8) * btc_price
                    if value_usd < MIN_VALUE_USD:
                        continue
                    if addr in BTC_EXCHANGE_ADDRESSES:
                        to_exchange += value_usd

                # Check inputs for exchange withdrawals
                for inp in tx.get("inputs", []):
                    prev_out = inp.get("prev_out", {})
                    addr = prev_out.get("addr", "")
                    value_usd = (prev_out.get("value", 0) / 1e8) * btc_price
                    if value_usd < MIN_VALUE_USD:
                        continue
                    if addr in BTC_EXCHANGE_ADDRESSES:
                        from_exchange += value_usd

            return _determine_signal(to_exchange, from_exchange)

    except Exception as e:
        logger.error(f"BTC whale fetch failed: {e}")
        return _last_known.get("BTC", "neutral")


# ─── XRP: XRPL Data API ─────────────────────────────────────────────

async def _fetch_xrp_whales(session: aiohttp.ClientSession, xrp_price: float) -> str:
    """Detect large XRP transfers via XRPL Data API (free, no key)."""
    try:
        target = "rfkE1aSy9G8Upk4JssnwBxhEv5p4mn2KTy"  # Binance
        url = f"https://data.ripple.com/v2/accounts/{target}/payments"
        params = {"limit": 50, "descending": "true", "type": "Payment"}

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return _last_known.get("XRP", "neutral")

            data = await resp.json(content_type=None)
            payments = data.get("payments", [])

            to_exchange = 0.0
            from_exchange = 0.0

            for p in payments:
                # Amount can be a string (XRP drops) or object (IOU)
                amount = p.get("delivered_amount")
                if isinstance(amount, str):
                    xrp_amount = float(amount)
                elif isinstance(amount, dict) and amount.get("currency") == "XRP":
                    xrp_amount = float(amount.get("value", 0))
                else:
                    continue

                value_usd = xrp_amount * xrp_price
                if value_usd < MIN_VALUE_USD:
                    continue

                destination = p.get("destination", "")
                source = p.get("source", "")

                # Inbound to any known exchange
                if destination in XRP_EXCHANGE_ADDRESSES and source not in XRP_EXCHANGE_ADDRESSES:
                    to_exchange += value_usd
                # Outbound from exchange to non-exchange
                elif source in XRP_EXCHANGE_ADDRESSES and destination not in XRP_EXCHANGE_ADDRESSES:
                    from_exchange += value_usd

            return _determine_signal(to_exchange, from_exchange)

    except Exception as e:
        logger.error(f"XRP whale fetch failed: {e}")
        return _last_known.get("XRP", "neutral")


# ─── Main Entry Point ────────────────────────────────────────────────

async def fetch_whale_signal(base_asset: str, current_price: float = 0.0) -> str:
    """Fetch whale transaction data and determine market pressure.

    Uses chain-specific blockchain explorer APIs (all free, no new keys):
      ETH → Etherscan
      BNB → BSCScan (same Etherscan key)
      BTC → Blockchain.com
      XRP → XRPL Data API

    Args:
        base_asset: e.g. "ETH", "BTC" — skips BCH/SUI
        current_price: current USD price of the asset (for value filtering)

    Returns:
        "sell_pressure" | "accumulation" | "neutral"
    """
    if base_asset not in WHALE_ALERT_COINS:
        return "neutral"

    if current_price <= 0:
        return _last_known.get(base_asset, "neutral")

    try:
        async with aiohttp.ClientSession() as session:
            if base_asset == "ETH":
                result = await _fetch_eth_whales(session, current_price)
            elif base_asset == "BTC":
                result = await _fetch_btc_whales(session, current_price)
            elif base_asset == "BNB":
                result = await _fetch_bnb_whales(session, current_price)
            elif base_asset == "XRP":
                result = await _fetch_xrp_whales(session, current_price)
            else:
                result = "neutral"

            _last_known[base_asset] = result
            return result

    except Exception as e:
        logger.error(f"Whale signal fetch failed for {base_asset}: {e}")
        return _last_known.get(base_asset, "neutral")
