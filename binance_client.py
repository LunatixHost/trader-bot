import asyncio
import logging
import math
from binance import AsyncClient, BinanceSocketManager
from config import (
    BINANCE_API_KEY, BINANCE_SECRET_KEY, USE_TESTNET,
    TRADING_PAIRS,
)

logger = logging.getLogger("trading_bot.binance")


class ExecutionError(Exception):
    """Raised when an order cannot be safely executed (e.g. dust notional)."""
    pass


class BinanceClient:
    """Async wrapper around python-binance for REST + WebSocket operations."""

    def __init__(self):
        self.client: AsyncClient | None = None
        self.bsm: BinanceSocketManager | None = None
        self._ws_tasks: list[asyncio.Task] = []
        self._running = False
        self._step_size_cache: dict[str, float] = {}    # pair → stepSize
        self._min_qty_cache: dict[str, float] = {}      # pair → minQty

        # Event-driven execution bridge.
        # The WebSocket stream enqueues pair symbols here on every price tick.
        # A dedicated worker in bot.py drains the queue and calls evaluate_and_trade()
        # immediately — no timer polling needed.
        # Deduplication: _queued_pairs prevents the same pair from flooding the queue
        # between evaluations. When a pair is already pending, new ticks are silently
        # dropped until the worker dequeues and clears the entry.
        self.price_event_queue: asyncio.Queue = asyncio.Queue()
        self._queued_pairs: set[str] = set()

    # ─── Connection ───────────────────────────────────────────────────

    async def connect(self):
        """Initialize the async Binance client."""
        self.client = await AsyncClient.create(
            api_key=BINANCE_API_KEY,
            api_secret=BINANCE_SECRET_KEY,
            testnet=USE_TESTNET,
        )
        self.bsm = BinanceSocketManager(self.client, max_queue_size=10000)
        mode = "TESTNET" if USE_TESTNET else "LIVE"
        logger.info(f"Connected to Binance ({mode})")

    async def disconnect(self):
        """Clean shutdown of client and all WebSocket streams."""
        self._running = False
        for task in self._ws_tasks:
            task.cancel()
        self._ws_tasks.clear()
        if self.client:
            await self.client.close_connection()
            logger.info("Binance client disconnected")

    # ─── REST Functions ───────────────────────────────────────────────

    async def get_balance(self, asset: str) -> float:
        """Get the free balance of an asset (e.g. 'ETH', 'USDT')."""
        try:
            account = await self.client.get_account()
            for bal in account["balances"]:
                if bal["asset"] == asset:
                    return float(bal["free"])
            return 0.0
        except Exception as e:
            logger.error(f"Failed to get balance for {asset}: {e}")
            return 0.0

    async def get_current_price(self, pair: str) -> float:
        """Get the latest price for a trading pair (e.g. 'ETHUSDT')."""
        try:
            ticker = await self.client.get_symbol_ticker(symbol=pair)
            return float(ticker["price"])
        except Exception as e:
            logger.error(f"Failed to get price for {pair}: {e}")
            return 0.0

    async def place_market_buy(self, pair: str, usdt_amount: float) -> dict | None:
        """Buy the base asset of a pair with a given USDT amount (quoteOrderQty)."""
        try:
            order = await self.client.order_market_buy(
                symbol=pair,
                quoteOrderQty=f"{usdt_amount:.2f}",
            )
            filled_qty = float(order.get("executedQty", 0))
            filled_quote = float(order.get("cummulativeQuoteQty", 0))
            logger.info(
                f"BUY {pair}: {filled_qty} for {filled_quote:.2f} USDT"
            )
            return order
        except Exception as e:
            logger.error(f"Failed to place market buy for {pair}: {e}")
            return None

    async def place_market_sell(
        self, pair: str, coin_amount: float, current_price: float = 0.0
    ) -> dict | None:
        """Sell a given amount of the base asset for a pair.

        Raises ExecutionError if the notional value is below $15 (dust guard).
        Accepts an optional current_price to avoid a redundant ticker fetch;
        if not provided, fetches it internally.
        """
        try:
            # Format to LOT_SIZE step using math.floor (safe truncation)
            step_size = await self.get_step_size(pair)
            quantity = self.calc_quantity(coin_amount * (current_price or 1.0),
                                          current_price or 1.0, step_size)
            if quantity <= 0:
                # Fallback to legacy formatter if calc fails
                quantity = await self._format_quantity(pair, coin_amount)
            if quantity <= 0:
                logger.warning(f"Quantity too small to sell for {pair}: {coin_amount}")
                return None

            # Fetch current price if caller didn't provide it (needed for safety gate)
            if current_price <= 0:
                ticker = await self.client.get_symbol_ticker(symbol=pair)
                current_price = float(ticker.get("price", 0))

            notional = quantity * current_price
            if notional < 15:
                raise ExecutionError(
                    f"Notional value too low; check precision logic — "
                    f"{pair}: {quantity} × {current_price:.4f} = ${notional:.2f} < $15"
                )

            s = f"{step_size:.10f}".rstrip("0")
            precision = len(s.split(".")[-1]) if "." in s else 0
            qty_str = f"{quantity:.{precision}f}"

            order = await self.client.order_market_sell(
                symbol=pair,
                quantity=qty_str,
            )
            filled_qty = float(order.get("executedQty", 0))
            filled_quote = float(order.get("cummulativeQuoteQty", 0))
            logger.info(
                f"SELL {pair}: {filled_qty} for {filled_quote:.2f} USDT"
            )
            return order
        except ExecutionError:
            raise  # Let bot.py decide how to handle dust
        except Exception as e:
            logger.error(f"Failed to place market sell for {pair}: {e}")
            return None

    async def get_step_size(self, pair: str) -> float:
        """Return the LOT_SIZE stepSize for a pair (cached after first fetch).

        Uses a module-level cache so repeated calls within a cycle are free.
        Fallback to 1e-8 (effectively no rounding) if the fetch fails.
        """
        if pair in self._step_size_cache:
            return self._step_size_cache[pair]
        try:
            info = await self.client.get_symbol_info(pair)
            if info:
                for f in info.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        step = float(f["stepSize"])
                        min_qty = float(f["minQty"])
                        self._step_size_cache[pair] = step
                        self._min_qty_cache[pair] = min_qty
                        return step
        except Exception as e:
            logger.error(f"get_step_size failed for {pair}: {e}")
        self._step_size_cache[pair] = 1e-8
        return 1e-8

    def calc_quantity(
        self, usdt_amount: float, current_price: float, step_size: float
    ) -> float:
        """Compute the largest valid quantity that fits in usdt_amount.

        Uses math.floor to avoid over-spending due to floating-point rounding.
        Formula: floor(usdt / price / step) * step
        """
        if current_price <= 0 or step_size <= 0:
            return 0.0
        raw = usdt_amount / current_price / step_size
        return math.floor(raw) * step_size

    async def place_limit_buy(
        self, pair: str, quantity: float, price: float
    ) -> dict | None:
        """Place a GTC LIMIT BUY order for an explicit quantity at a given price.

        Used for FVG entries where we want to buy at a specific level, not at market.
        Raises ExecutionError if the notional value is below $15.
        """
        try:
            notional = quantity * price
            if notional < 15:
                raise ExecutionError(
                    f"Notional value too low for limit buy on {pair}: "
                    f"{quantity} × {price:.4f} = ${notional:.2f} < $15"
                )
            # Format quantity to correct precision
            step_size = await self.get_step_size(pair)
            quantity = self.calc_quantity(quantity * price, price, step_size)
            if quantity <= 0:
                logger.warning(f"Limit buy quantity rounds to 0 for {pair}")
                return None

            s = f"{step_size:.10f}".rstrip("0")
            precision = len(s.split(".")[-1]) if "." in s else 0
            qty_str = f"{quantity:.{precision}f}"

            # Price precision: use 8 decimal places as safe default
            price_str = f"{price:.8f}".rstrip("0").rstrip(".")

            order = await self.client.order_limit_buy(
                symbol=pair,
                quantity=qty_str,
                price=price_str,
                timeInForce="GTC",
            )
            logger.info(
                f"LIMIT BUY {pair}: {qty_str} @ {price_str} "
                f"(orderId={order.get('orderId')})"
            )
            return order
        except ExecutionError:
            raise
        except Exception as e:
            logger.error(f"Failed to place limit buy for {pair}: {e}")
            return None

    async def cancel_order(self, pair: str, order_id: int) -> bool:
        """Cancel an open order. Returns True if successfully cancelled."""
        try:
            await self.client.cancel_order(symbol=pair, orderId=order_id)
            logger.info(f"Cancelled order {order_id} for {pair}")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id} for {pair}: {e}")
            return False

    async def get_order_status(self, pair: str, order_id: int) -> dict | None:
        """Check the status of an existing order."""
        try:
            return await self.client.get_order(symbol=pair, orderId=order_id)
        except Exception as e:
            logger.error(f"get_order_status failed for {pair} #{order_id}: {e}")
            return None

    async def get_orderbook(self, pair: str, limit: int = 20) -> dict:
        """Fetch the current order book for a pair (bids + asks)."""
        try:
            return await self.client.get_order_book(symbol=pair, limit=limit)
        except Exception as e:
            logger.error(f"get_orderbook failed for {pair}: {e}")
            return {}

    async def get_recent_trades(self, pair: str, limit: int = 50) -> list:
        """Fetch the most recent trades for a pair.

        Each trade has 'qty' and 'isBuyerMaker' fields.
        isBuyerMaker=False means the buyer was the aggressor (market buy).
        """
        try:
            return await self.client.get_recent_trades(symbol=pair, limit=limit)
        except Exception as e:
            logger.error(f"get_recent_trades failed for {pair}: {e}")
            return []

    async def get_klines(
        self, pair: str, interval: str = "1h", limit: int = 100
    ) -> list[list]:
        """Fetch OHLCV candles for a pair.

        Returns list of [open_time, open, high, low, close, volume, ...].
        """
        try:
            klines = await self.client.get_klines(
                symbol=pair, interval=interval, limit=limit
            )
            return klines
        except Exception as e:
            logger.error(f"Failed to get klines for {pair}: {e}")
            return []

    # ─── WebSocket ────────────────────────────────────────────────────

    async def start_price_streams(self, state):
        """Open a symbol ticker WebSocket per configured pair.

        Updates pair_state.current_price on every tick.
        """
        self._running = True
        for pair_cfg in TRADING_PAIRS:
            symbol = pair_cfg["pair"].lower()
            task = asyncio.create_task(
                self._price_stream_loop(symbol, pair_cfg["pair"], state)
            )
            self._ws_tasks.append(task)
        logger.info(
            f"Started WebSocket price streams for {len(TRADING_PAIRS)} pairs"
        )

    async def _price_stream_loop(self, symbol_lower: str, pair: str, state):
        """Single pair WebSocket stream with automatic reconnect."""
        while self._running:
            try:
                async with self.bsm.symbol_ticker_socket(symbol_lower) as stream:
                    while self._running:
                        msg = await stream.recv()
                        if msg is None:
                            break
                        if "e" in msg and msg["e"] == "error":
                            logger.error(f"WS error for {pair}: {msg}")
                            break
                        price = float(msg.get("c", 0))  # 'c' = last price
                        if price > 0 and pair in state.pairs:
                            # No lock needed: asyncio is single-threaded and
                            # these are plain float assignments with no awaits
                            # between them. Locking here caused lock contention
                            # with evaluate_and_trade, backing up the WS queue
                            # until BinanceWebsocketQueueOverflow fired.
                            ps = state.pairs[pair]
                            if ps.current_price > 0:
                                ps.prev_price = ps.current_price
                            ps.current_price = price

                            # Signal the execution worker — only if this pair
                            # is not already waiting in the queue (dedup guard).
                            # Fast ticks between evaluations are intentionally
                            # dropped; the worker always reads the latest price
                            # directly from ps.current_price when it runs.
                            if pair not in self._queued_pairs:
                                self._queued_pairs.add(pair)
                                self.price_event_queue.put_nowait(pair)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"WS stream error for {pair}: {e}")
                if self._running:
                    await asyncio.sleep(5)  # Wait before reconnect

    # ─── Helpers ──────────────────────────────────────────────────────

    async def _check_min_notional(self, pair: str, quantity: float) -> bool:
        """Check if order meets MIN_NOTIONAL filter (min order value in USDT).

        Returns True if order is large enough, False if it's dust.
        """
        try:
            info = await self.client.get_symbol_info(pair)
            if info is None:
                return True  # Can't check — let Binance decide

            min_notional = 5.0  # Default fallback
            for f in info.get("filters", []):
                if f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                    min_notional = float(f.get("minNotional", 5.0))
                    break

            # Estimate order value using current price
            ticker = await self.client.get_symbol_ticker(symbol=pair)
            price = float(ticker.get("price", 0))
            if price <= 0:
                return True

            order_value = quantity * price
            return order_value >= min_notional
        except Exception as e:
            logger.error(f"MIN_NOTIONAL check failed for {pair}: {e}")
            return True  # Fail open — let Binance reject if needed

    async def _format_quantity(self, pair: str, amount: float) -> float:
        """Format quantity according to the symbol's LOT_SIZE filter.

        Uses math.floor to guarantee we never exceed the intended amount.
        Kept for backwards-compat; new code should use calc_quantity() directly.
        """
        try:
            step_size = await self.get_step_size(pair)
            min_qty = self._min_qty_cache.get(pair, 0.0)
            if step_size > 0:
                s = f"{step_size:.10f}".rstrip("0")
                precision = len(s.split(".")[-1]) if "." in s else 0
                # math.floor truncation: floor(amount / step) * step
                quantity = math.floor(amount / step_size) * step_size
                quantity = round(quantity, precision)
                if quantity < min_qty:
                    return 0.0
                return quantity
            return amount
        except Exception as e:
            logger.error(f"Failed to format quantity for {pair}: {e}")
            return amount


# Module-level singleton
binance = BinanceClient()
