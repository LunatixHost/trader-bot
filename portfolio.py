"""Portfolio — dynamic allocation engine.

Runs every PORTFOLIO_REBALANCE_INTERVAL to auto-adjust USDT allocation
per pair based on confidence scores, volatility, and position caps.
Also tracks overall portfolio value, P/L, and peak value.
"""

import logging
import numpy as np
from config import (
    PORTFOLIO_RESERVE_PCT,
    PORTFOLIO_MAX_POSITIONS,
    TRADE_SIZE_PCT,
)

logger = logging.getLogger("trading_bot.portfolio")


def calculate_volatility(prices: list[float]) -> float:
    """Calculate volatility as standard deviation of hourly returns.

    Args:
        prices: List of hourly close prices (at least 2)

    Returns:
        float volatility score (higher = more volatile)
    """
    if len(prices) < 2:
        return 0.03  # Default moderate volatility

    arr = np.array(prices, dtype=float)
    returns = np.diff(arr) / arr[:-1]
    return float(np.std(returns))


async def rebalance(state, binance_client=None):
    """Update allocations and reserve.

    Scalping mode: each pair gets trade_size as allocation (TRADE_SIZE_PCT of portfolio).
    The actual position cap and reserve are enforced at trade time.
    """
    async with state.lock:
        total = state.portfolio_total_usdt
        state.usdt_reserved = total * PORTFOLIO_RESERVE_PCT
        trade_size = total * TRADE_SIZE_PCT

        for pair, ps in state.pairs.items():
            ps.trade_amount_usdt = trade_size
            ps.allocated_usdt = trade_size

        logger.debug(f"Rebalanced: trade_size=${trade_size:.2f}, reserve=${state.usdt_reserved:.2f}")


def calculate_portfolio_value(state) -> float:
    """Calculate total portfolio value across all three capital layers.

    Formula:
        total = usdt_balance
              + Σ (spot scalp position values)
              + Σ (long-term position values)
              + futures paper unrealised PnL

    Long-term coins are intentionally NOT stored in ps.asset_balance (to keep
    the scalping layer clean), so they must be summed separately here.
    Futures paper trading does not consume real USDT (margin-free simulation),
    so only its unrealised PnL delta is added.

    Args:
        state: BotState object

    Returns:
        float total value in USDT
    """
    # Available USDT (wallet liquid cash — LT buys have already reduced this)
    total = state.usdt_balance

    # Spot scalping positions
    for ps in state.pairs.values():
        if ps.current_price > 0:
            total += ps.asset_balance * ps.current_price

    # Long-term positions (tracked in LongTermState.quantity, NOT ps.asset_balance)
    for sym, lt_s in getattr(state, "long_term_states", {}).items():
        if lt_s.holding and lt_s.quantity > 0:
            ps = state.pairs.get(sym)
            if ps is not None and ps.current_price > 0:
                total += lt_s.quantity * ps.current_price

    # Futures paper unrealised PnL (simulation only — no real USDT consumed)
    try:
        from futures_paper import get_futures_unrealized_pnl
        total += get_futures_unrealized_pnl(state)
    except Exception:
        pass

    return total


def update_portfolio_tracking(state):
    """Update portfolio P/L, peak value, and per-pair metrics.

    Call this after every price update or trade execution.
    """
    # Component breakdown for debug logging
    spot_value = sum(
        ps.asset_balance * ps.current_price
        for ps in state.pairs.values()
        if ps.current_price > 0
    )
    lt_value = sum(
        lt_s.quantity * state.pairs[sym].current_price
        for sym, lt_s in getattr(state, "long_term_states", {}).items()
        if lt_s.holding and lt_s.quantity > 0
        and sym in state.pairs and state.pairs[sym].current_price > 0
    )
    futures_pnl = 0.0
    try:
        from futures_paper import get_futures_unrealized_pnl
        futures_pnl = get_futures_unrealized_pnl(state)
    except Exception:
        pass

    current_value = state.usdt_balance + spot_value + lt_value + futures_pnl
    state.portfolio_current_value = current_value

    logger.debug(
        f"PORTFOLIO CHECK | "
        f"USDT={state.usdt_balance:.2f} | "
        f"SPOT={spot_value:.2f} | "
        f"LT={lt_value:.2f} | "
        f"FUTURES={futures_pnl:.2f} | "
        f"TOTAL={current_value:.2f}"
    )

    # Set start value on first run
    if state.portfolio_start_value <= 0:
        state.portfolio_start_value = current_value

    # Overall P/L
    state.portfolio_pl_usdt = current_value - state.portfolio_start_value
    if state.portfolio_start_value > 0:
        state.portfolio_pl_pct = (
            state.portfolio_pl_usdt / state.portfolio_start_value
        ) * 100
    else:
        state.portfolio_pl_pct = 0.0

    # Track peak value
    if current_value > state.portfolio_peak_value:
        state.portfolio_peak_value = current_value

    # Update per-pair position values
    state.open_positions_count = 0
    for ps in state.pairs.values():
        ps.position_value_usdt = ps.asset_balance * ps.current_price
        if ps.position_value_usdt > ps.peak_value:
            ps.peak_value = ps.position_value_usdt
        if ps.asset_balance > 0:
            state.open_positions_count += 1


async def update_volatility(state, binance_client):
    """Fetch recent price data and update volatility per pair.

    Args:
        state: BotState object
        binance_client: BinanceClient instance
    """
    for pair, ps in state.pairs.items():
        try:
            klines = await binance_client.get_klines(pair, interval="1h", limit=24)
            if klines and len(klines) >= 2:
                prices = [float(k[4]) for k in klines]  # Close prices
                vol = calculate_volatility(prices)
                async with state.lock:
                    ps.volatility = vol
        except Exception as e:
            logger.error(f"Failed to update volatility for {pair}: {e}")
