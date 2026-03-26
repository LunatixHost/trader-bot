"""Microstructure signals: bid/ask spread, orderbook imbalance, trade flow.

Orderbook imbalance = bid_volume / (bid_volume + ask_volume), top 5 levels.
  > OB_IMBALANCE_BULL (0.6) → buyers stacking — bullish pressure
  < OB_IMBALANCE_BEAR (0.4) → sellers stacking — bearish pressure

Trade flow ratio = buyer-aggressor volume / total volume, last 50 trades.
  isBuyerMaker=False means the buyer was the aggressor (market buy hit the ask).
  > OB_FLOW_BULL (0.6) → buyers lifting asks — bullish
  < OB_FLOW_BEAR (0.4) → sellers hitting bids — bearish

Both signals are EMA-smoothed across calls to reduce single-tick noise.
Spread is NOT smoothed — it's used as a live gate at buy time.
"""

import logging
from config import OB_EMA_ALPHA

logger = logging.getLogger("trading_bot.signals.orderbook")

# Per-pair smoothed state carried across polling cycles
_smoothed: dict[str, dict] = {}


def _default_result() -> dict:
    return {"spread_pct": 0.0, "imbalance": 0.5, "flow_ratio": 0.5}


async def fetch_orderbook_signals(binance_client, pair: str) -> dict:
    """Fetch orderbook and recent trades, return spread, imbalance, flow_ratio.

    Args:
        binance_client: BinanceClient instance
        pair: e.g. "ETHUSDT"

    Returns:
        dict with:
          spread_pct   — (best_ask - best_bid) / mid_price (raw, unsmoothed)
          imbalance    — EMA-smoothed bid_vol / (bid+ask), top 5 levels
          flow_ratio   — EMA-smoothed buyer-aggressor vol / total, last 50 trades
    """
    try:
        ob = await binance_client.get_orderbook(pair, limit=20)
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])

        if not bids or not asks:
            return _smoothed.get(pair, _default_result())

        # ── Spread (raw — used as gate, not smoothed) ──
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid_price = (best_bid + best_ask) / 2
        spread_pct = (best_ask - best_bid) / mid_price if mid_price > 0 else 0.0

        # ── Imbalance: top 5 levels each side ──
        bid_vol = sum(float(b[1]) for b in bids[:5])
        ask_vol = sum(float(a[1]) for a in asks[:5])
        total_ob_vol = bid_vol + ask_vol
        raw_imbalance = bid_vol / total_ob_vol if total_ob_vol > 0 else 0.5

        # ── Flow ratio: last 50 trades, volume-weighted ──
        trades = await binance_client.get_recent_trades(pair, limit=50)
        buyer_qty = sum(float(t["qty"]) for t in trades if not t.get("isBuyerMaker", True))
        total_qty = sum(float(t["qty"]) for t in trades)
        raw_flow = buyer_qty / total_qty if total_qty > 0 else 0.5

        # ── EMA smoothing ──
        prev = _smoothed.get(pair, _default_result())
        alpha = OB_EMA_ALPHA
        smooth_imbalance = alpha * raw_imbalance + (1 - alpha) * prev["imbalance"]
        smooth_flow = alpha * raw_flow + (1 - alpha) * prev["flow_ratio"]

        result = {
            "spread_pct": round(spread_pct, 6),
            "imbalance": round(smooth_imbalance, 4),
            "flow_ratio": round(smooth_flow, 4),
        }
        _smoothed[pair] = result
        return result

    except Exception as e:
        logger.error(f"Orderbook signal fetch failed for {pair}: {e}")
        return _smoothed.get(pair, _default_result())
