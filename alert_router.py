"""Alert Router — fire-and-forget notification queue.

The trading engine calls dispatch_*() which is a non-blocking put_nowait
(~0.001ms).  A dedicated background task in the Discord bot drains the queue
and handles the slow Discord API calls completely decoupled from execution.
"""

import asyncio
import logging

logger = logging.getLogger("trading_bot.alert_router")


class DiscordAlertQueue:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()

    def dispatch_trade_alert(
        self,
        action: str,
        pair: str,
        pair_state,
        bot_state,
        pl: float = 0.0,
    ) -> None:
        """Non-blocking.  Drop payload onto queue and return immediately."""
        payload = {
            "type":       "TRADE",
            "action":     action,
            "pair":       pair,
            "pair_state": pair_state,
            "bot_state":  bot_state,
            "pl":         pl,
        }
        self.queue.put_nowait(payload)
        logger.debug(f"Alert queued: {action} {pair}")

    def dispatch_futures_alert(
        self,
        action: str,
        symbol: str,
        side: str,
        entry_price: float,
        size: float,
        pl: float = 0.0,
        sl_price: float = 0.0,
        tp_price: float = 0.0,
    ) -> None:
        """Non-blocking.  Futures entry/exit notification."""
        payload = {
            "type":        "FUTURES",
            "action":      action,       # LONG_ENTRY, SHORT_ENTRY, LONG_EXIT, SHORT_EXIT
            "symbol":      symbol,
            "side":        side,         # 'long' | 'short'
            "entry_price": entry_price,
            "size":        size,
            "pl":          pl,
            "sl_price":    sl_price,
            "tp_price":    tp_price,
        }
        self.queue.put_nowait(payload)
        logger.debug(f"Futures alert queued: {action} {symbol}")

    def dispatch_system_alert(self, message: str, level: str = "warning") -> None:
        """Non-blocking.  System-level warning/info to SIGNALS channel."""
        payload = {
            "type":    "SYSTEM",
            "message": message,
            "level":   level,   # 'info' | 'warning' | 'error'
        }
        self.queue.put_nowait(payload)


# Single global instance imported by both bot.py and futures_execution.py
alert_router = DiscordAlertQueue()
