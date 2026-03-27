"""USDⓈ-M Futures Execution Engine — CCXT-based order routing.

Handles ALL live order placement for the scalping layer in futures mode.
This module replaces the spot order calls (place_market_buy / place_market_sell)
that previously lived in binance_client.py.

Architecture:
  - WebSocket streams + market-data REST  → binance_client.py (python-binance)
  - Order execution + account management  → this module (CCXT)

Key safety invariants:
  - ALL exits use reduceOnly=True           — prevents double-execution reversals
  - Isolated margin enforced before entry   — single position cannot wipe account
  - One-Way Mode enforced at startup        — no simultaneous long/short overlap
  - USDT-only balance queries              — never checks base-asset spot wallets
"""

import asyncio
import logging
import ccxt.async_support as ccxt

from config import (
    BINANCE_FUTURES_API_KEY, BINANCE_FUTURES_SECRET_KEY, USE_TESTNET,
    FUTURES_LEVERAGE, FUTURES_ACCOUNT_RISK_PCT, STOP_LOSS_PCT,
    ATR_SL_MULTIPLIER, ATR_SL_MAX_PCT,
    AUDIT_MODE, AUDIT_MICRO_NOTIONAL,
)

logger = logging.getLogger("trading_bot.futures_exec")

# ─── CCXT Exchange Instance ───────────────────────────────────────────────────
#
# defaultType='future' routes every API call to the USDⓈ-M Futures endpoint
# (fapi.binance.com) instead of the spot endpoint (api.binance.com).
# This single option is what separates "buy BTC" (spot) from "long BTCUSDT perp".
#
# NOTE: Futures testnet requires SEPARATE API keys from spot testnet.
# Generate them at: https://testnet.binancefuture.com (GitHub login)
# Set BINANCE_FUTURES_API_KEY / BINANCE_FUTURES_SECRET_KEY in .env.

exchange = ccxt.binance({
    'apiKey':          BINANCE_FUTURES_API_KEY,
    'secret':          BINANCE_FUTURES_SECRET_KEY,
    'enableRateLimit': True,
    'options': {
        'defaultType':             'future',   # CRITICAL — USDⓈ-M perpetuals
        'adjustForTimeDifference': True,
    },
})

if USE_TESTNET:
    # set_sandbox_mode patches ALL fapi URL keys in one call (ccxt 4.x official method).
    # Manual per-key overrides only covered fapiPublic/fapiPrivate but missed
    # fapiPrivateV2, fapiPrivateV3, etc. — causing -2008 on fetch_balance.
    exchange.set_sandbox_mode(True)
    logger.info("Futures execution: TESTNET mode active (testnet.binancefuture.com)")


# ─── Startup ─────────────────────────────────────────────────────────────────

async def enforce_futures_environment():
    """Enforce One-Way Mode on the futures account.

    Call ONCE at bot startup.  One-Way Mode (dualSidePosition=false) ensures
    you cannot simultaneously hold a long AND short on the same symbol.
    This makes reduceOnly exits unambiguous — the single open position
    is always the one being closed.

    Binance returns error -4059 / "No need to change" when already in the
    correct mode; we silently pass that case.
    """
    # Sync local clock with Binance server time before any signed request.
    # adjustForTimeDifference in the constructor is passive — it doesn't
    # auto-fetch the offset. Without this, clock skew >1000ms causes -1021.
    try:
        await exchange.load_time_difference()
        logger.info("Futures: clock synced with Binance server time")
    except Exception as e:
        logger.warning(f"Futures: clock sync failed (non-fatal): {e}")

    try:
        await exchange.fapiPrivatePostPositionSideDual({'dualSidePosition': 'false'})
        logger.info("Futures: One-Way Mode confirmed")
    except ccxt.ExchangeError as e:
        err_str = str(e)
        if "-4059" in err_str or "No need to change" in err_str:
            logger.info("Futures: One-Way Mode already active")
        else:
            logger.error(f"Futures: enforce_futures_environment failed: {e}")
            raise
    except Exception as e:
        logger.error(f"Futures: enforce_futures_environment error: {e}")
        raise


# ─── Pre-Flight ──────────────────────────────────────────────────────────────

async def prepare_market_for_entry(symbol: str, leverage: int | None = None):
    """Assert Isolated Margin + Fixed Leverage immediately before every entry.

    Idempotent — safe to call repeatedly; the exchange silently accepts
    redundant mode-match requests or returns a benign error we swallow.
    Running this before EVERY entry (not just the first) guards against
    external changes to the account's margin mode between bot restarts.
    """
    if leverage is None:
        leverage = FUTURES_LEVERAGE

    ccxt_symbol = _to_ccxt_symbol(symbol)

    # 1. Force Isolated Margin — a single bad exit cannot drain the entire wallet
    try:
        await exchange.set_margin_mode('isolated', ccxt_symbol)
    except ccxt.ExchangeError as e:
        err_str = str(e)
        # -4046: "No need to change margin type" — already isolated
        if "-4046" in err_str or "No need to change" in err_str or "already" in err_str.lower():
            pass
        else:
            logger.warning(f"set_margin_mode warning for {symbol}: {e}")

    # 2. Set Leverage
    try:
        await exchange.set_leverage(leverage, ccxt_symbol)
    except ccxt.ExchangeError as e:
        # Some exchanges block leverage change when a position is already open
        logger.warning(f"set_leverage warning for {symbol}: {e}")


# ─── Balance ─────────────────────────────────────────────────────────────────

async def get_futures_usdt_balance() -> float:
    """Query USDT free balance from the USDⓈ-M futures wallet.

    This is the ONLY balance function used by the futures scalping layer.
    Base-asset (BTC, ETH, etc.) balances are irrelevant — futures positions
    are settled entirely in USDT.
    """
    try:
        balance = await exchange.fetch_balance({'type': 'future'})
        return float(balance.get('USDT', {}).get('free', 0.0))
    except Exception as e:
        logger.error(f"get_futures_usdt_balance failed: {e}")
        return 0.0


async def get_futures_total_usdt() -> float:
    """Total futures wallet USDT (free + in margin).

    Used for portfolio value calculations — includes locked margin so the
    reported total doesn't shrink every time a position is opened.
    """
    try:
        balance = await exchange.fetch_balance({'type': 'future'})
        usdt = balance.get('USDT', {})
        total = float(usdt.get('total', usdt.get('free', 0.0)))
        return total
    except Exception as e:
        logger.error(f"get_futures_total_usdt failed: {e}")
        return 0.0


# ─── Sizing ──────────────────────────────────────────────────────────────────

def calculate_futures_position_size(
    usdt_balance: float,
    entry_price: float,
    sl_price: float,
    account_risk_pct: float | None = None,
    leverage: int | None = None,
) -> float:
    """Compute contract size based on strict dollar-risk (Phase 4 risk parity).

    Phase 8 AUDIT MODE: when AUDIT_MODE=True (env default), bypasses all risk-parity
    math and forces every trade to AUDIT_MICRO_NOTIONAL (6 USDT). This validates the
    live execution pipe with minimum capital exposure before full deployment.

    Normal formula (AUDIT_MODE=False):
        sl_distance = |entry_price - sl_price| / entry_price
        max_loss    = usdt_balance × account_risk_pct
        notional    = max_loss / sl_distance
        notional    = min(notional, usdt_balance × leverage × 0.95)
        contracts   = notional / entry_price

    The 0.95 buying-power buffer covers fees + funding so we never attempt
    to deploy precisely 100% of margin capacity.

    Returns:
        Number of base-asset contracts (float).
        Returns 0.0 if any input is invalid or degenerate.
    """
    if entry_price <= 0:
        return 0.0

    # ── Phase 8: Audit Mode override ─────────────────────────────────────────
    if AUDIT_MODE:
        logger.warning(
            f"⚠️  AUDIT MODE ACTIVE — Risk Parity overridden. "
            f"Forcing micro-notional: ${AUDIT_MICRO_NOTIONAL:.1f} USDT "
            f"(set AUDIT_MODE=False in .env to restore full sizing)"
        )
        return AUDIT_MICRO_NOTIONAL / entry_price

    # ── Normal Phase 4 Risk Parity ────────────────────────────────────────────
    if account_risk_pct is None:
        account_risk_pct = FUTURES_ACCOUNT_RISK_PCT
    if leverage is None:
        leverage = FUTURES_LEVERAGE

    if sl_price <= 0 or usdt_balance <= 0:
        return 0.0

    sl_distance = abs(entry_price - sl_price) / entry_price
    if sl_distance <= 0:
        return 0.0

    max_loss    = usdt_balance * account_risk_pct
    notional    = max_loss / sl_distance

    max_buying_power = usdt_balance * leverage * 0.95
    if notional > max_buying_power:
        logger.warning(
            f"Risk-parity notional ${notional:.2f} exceeds buying power "
            f"${max_buying_power:.2f} — capping to {leverage}x"
        )
        notional = max_buying_power

    return notional / entry_price


async def format_futures_amount(symbol: str, amount: float) -> float:
    """Round amount to the exchange-valid precision for a futures contract.

    Uses CCXT's amount_to_precision() after loading markets once.
    Falls back to the raw amount on any error.
    """
    try:
        await _ensure_markets_loaded()
        ccxt_symbol = _to_ccxt_symbol(symbol)
        return float(exchange.amount_to_precision(ccxt_symbol, amount))
    except Exception as e:
        logger.warning(f"format_futures_amount failed for {symbol}: {e}")
        return amount


# ─── Entry ───────────────────────────────────────────────────────────────────

async def execute_futures_entry(
    symbol: str,
    side: str,
    contracts: float,
    leverage: int | None = None,
) -> dict | None:
    """Place a USDⓈ-M futures market entry order.

    Args:
        symbol:    Binance pair format ("BTCUSDT")
        side:      "buy"  → opens Long  |  "sell" → opens Short
        contracts: Number of base-asset contracts
        leverage:  Leverage multiplier (defaults to FUTURES_LEVERAGE from config)

    Returns CCXT unified order dict on success, None on failure.
    Order fields used by callers:
        order['filled']  — quantity filled
        order['average'] — average fill price
        order['cost']    — total USDT value (filled × average)
    """
    if leverage is None:
        leverage = FUTURES_LEVERAGE

    await prepare_market_for_entry(symbol, leverage)

    ccxt_symbol = _to_ccxt_symbol(symbol)
    contracts   = await format_futures_amount(symbol, contracts)

    if contracts <= 0:
        logger.warning(f"execute_futures_entry: rounded contracts = 0 for {symbol}")
        return None

    try:
        order = await exchange.create_order(
            symbol=ccxt_symbol,
            type='market',
            side=side,
            amount=contracts,
        )
        fill_price = order.get('average') or order.get('price') or 0
        logger.info(
            f"FUTURES ENTRY {side.upper()} {symbol}: {contracts} contracts "
            f"@ ~${fill_price:.4f} ({leverage}x leverage)"
        )
        return order
    except Exception as e:
        logger.error(f"execute_futures_entry failed for {symbol}: {e}")
        return None


async def execute_futures_limit_entry(
    symbol: str,
    side: str,
    contracts: float,
    limit_price: float,
    leverage: int | None = None,
) -> dict | None:
    """Place a GTC futures LIMIT entry order (used by FVG subsystem).

    The order rests at limit_price until filled or cancelled.
    Monitor fill status via get_futures_order_status(); cancel with
    cancel_futures_order() on timeout.
    """
    if leverage is None:
        leverage = FUTURES_LEVERAGE

    await prepare_market_for_entry(symbol, leverage)

    ccxt_symbol = _to_ccxt_symbol(symbol)
    contracts   = await format_futures_amount(symbol, contracts)

    if contracts <= 0:
        logger.warning(f"execute_futures_limit_entry: rounded contracts = 0 for {symbol}")
        return None

    try:
        order = await exchange.create_order(
            symbol=ccxt_symbol,
            type='limit',
            side=side,
            amount=contracts,
            price=limit_price,
            params={'timeInForce': 'GTC'},
        )
        logger.info(
            f"FUTURES LIMIT {side.upper()} {symbol}: {contracts} contracts "
            f"@ ${limit_price:.4f} (orderId={order.get('id')})"
        )
        return order
    except Exception as e:
        logger.error(f"execute_futures_limit_entry failed for {symbol}: {e}")
        return None


# ─── Exit ────────────────────────────────────────────────────────────────────

async def execute_futures_exit(
    symbol: str,
    position_side: str,
    contracts: float,
) -> dict | None:
    """Close a futures position with a reduceOnly market order.

    CRITICAL: reduceOnly=True is the safety guardrail that prevents
    a network-retry (duplicate order) from accidentally reversing the
    position instead of just closing it.

    Args:
        symbol:        Binance pair format ("BTCUSDT")
        position_side: Current holding direction — "long" or "short"
        contracts:     Number of contracts to close

    Returns CCXT unified order dict on success, None on failure.
    """
    exit_side   = 'sell' if position_side == 'long' else 'buy'
    ccxt_symbol = _to_ccxt_symbol(symbol)
    contracts   = await format_futures_amount(symbol, contracts)

    if contracts <= 0:
        logger.warning(f"execute_futures_exit: rounded contracts = 0 for {symbol}")
        return None

    try:
        order = await exchange.create_order(
            symbol=ccxt_symbol,
            type='market',
            side=exit_side,
            amount=contracts,
            params={'reduceOnly': True},    # ← CRITICAL GUARDRAIL — never remove
        )
        fill_price = order.get('average') or order.get('price') or 0
        logger.info(
            f"FUTURES EXIT {exit_side.upper()} {symbol}: {contracts} contracts "
            f"@ ~${fill_price:.4f}"
        )
        return order
    except ccxt.ExchangeError as e:
        err_str = str(e)
        # -2022: ReduceOnly Order is rejected (position already flat)
        # -4061: Order's notional must be less than allowed (dust)
        # These are expected during the Phase 8 reduceOnly stress test and on
        # normal position close races — log info not error, return sentinel.
        if "-2022" in err_str or "ReduceOnly" in err_str:
            logger.info(
                f"FUTURES EXIT {symbol}: reduceOnly rejected — position already "
                f"closed on exchange (safe to clear local state). Detail: {e}"
            )
            return {"_already_closed": True, "filled": contracts, "average": 0}
        if "-4061" in err_str or "notional" in err_str.lower():
            logger.warning(
                f"FUTURES EXIT {symbol}: dust notional rejected by exchange — "
                f"clearing local state. Detail: {e}"
            )
            return {"_dust_reject": True, "filled": contracts, "average": 0}
        logger.error(f"execute_futures_exit failed for {symbol}: {e}")
        return None
    except Exception as e:
        logger.error(f"execute_futures_exit unexpected error for {symbol}: {e}")
        return None


# ─── Order Management ────────────────────────────────────────────────────────

async def cancel_futures_order(symbol: str, order_id: int | str) -> bool:
    """Cancel an open futures order. Returns True if successfully cancelled."""
    try:
        ccxt_symbol = _to_ccxt_symbol(symbol)
        await exchange.cancel_order(str(order_id), ccxt_symbol)
        logger.info(f"Cancelled futures order {order_id} for {symbol}")
        return True
    except Exception as e:
        logger.error(f"cancel_futures_order failed for {symbol} #{order_id}: {e}")
        return False


async def get_futures_order_status(symbol: str, order_id: int | str) -> dict | None:
    """Fetch the current status of a futures order.

    Returned dict uses CCXT unified fields:
        status:   'open' | 'closed' | 'canceled'
        filled:   qty filled so far
        average:  average fill price
        cost:     total USDT filled value
    """
    try:
        ccxt_symbol = _to_ccxt_symbol(symbol)
        return await exchange.fetch_order(str(order_id), ccxt_symbol)
    except Exception as e:
        logger.error(f"get_futures_order_status failed for {symbol} #{order_id}: {e}")
        return None


# ─── Graceful Shutdown ───────────────────────────────────────────────────────

async def close_exchange():
    """Gracefully close the CCXT HTTP session on bot shutdown."""
    try:
        await exchange.close()
        logger.info("CCXT futures exchange connection closed")
    except Exception:
        pass


# ─── Internal Helpers ────────────────────────────────────────────────────────

_markets_loaded = False


async def _ensure_markets_loaded():
    """Load markets once per session (needed for amount_to_precision)."""
    global _markets_loaded
    if not _markets_loaded:
        await exchange.load_markets()
        _markets_loaded = True


def _to_ccxt_symbol(binance_symbol: str) -> str:
    """Convert Binance symbol format to CCXT format for USDⓈ-M futures.

    Binance: "BTCUSDT"  →  CCXT (with defaultType='future'): "BTC/USDT"
    """
    if binance_symbol.endswith("USDT"):
        base = binance_symbol[:-4]
        return f"{base}/USDT"
    # Generic fallback for non-USDT pairs (BNB/BTC etc.)
    if len(binance_symbol) > 3:
        return f"{binance_symbol[:-3]}/{binance_symbol[-3:]}"
    return binance_symbol
