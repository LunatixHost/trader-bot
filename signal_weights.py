"""Signal Weights — learn per-signal reliability from trade history.

Reads completed SELL trades from the database, examines which signals
were active at entry time, and computes a reliability weight per signal
based on trade outcomes.

Output: dict mapping signal names to float weights (0.5 to 1.5).
Signals that historically predict profitable trades get boosted;
signals that predict losses get reduced.

Safety features:
- Inertia smoothing: new_weight = 0.7 * old + 0.3 * learned (prevents jumps)
- Max delta clamp: weight can shift at most ±0.2 per learning cycle
- Min trades required: won't diverge from 1.0 until 20+ trades exist
- Drawdown freeze: caller should skip updates during drawdown

Called periodically (e.g. every 5 minutes) to update BotState.signal_weights.
"""

import logging
from config import (
    SIGNAL_WEIGHT_MIN,
    SIGNAL_WEIGHT_MAX,
    SIGNAL_WEIGHT_DECAY,
    SIGNAL_WEIGHT_MIN_TRADES,
    SIGNAL_WEIGHT_WINDOW,
    SIGNAL_WEIGHT_NORMALIZE_RATE,
)
from logger import get_trades_with_snapshots

logger = logging.getLogger("trading_bot.signal_weights")

# All tracked signal names (must match keys in signal_snapshot JSON)
SIGNAL_NAMES = [
    "rsi", "macd", "bollinger", "gas",
]

# Outcome scores: how much each outcome type contributes to a signal's score
OUTCOME_SCORES = {
    "strong_win": 1.0,
    "weak_win": 0.3,
    "scratch": 0.0,
    "loss": -1.0,
}

# Inertia: blend ratio between old and new weights (prevents sudden shifts)
WEIGHT_INERTIA = 0.7       # 70% old weight, 30% new learned weight
# Max allowed change per learning cycle
WEIGHT_MAX_DELTA = 0.2     # Weight can shift at most ±0.2 per cycle


def compute_signal_weights(
    limit: int = SIGNAL_WEIGHT_WINDOW,
    current_weights: dict[str, float] | None = None,
    in_drawdown: bool = False,
) -> dict[str, float]:
    """Compute per-signal reliability weights from trade history.

    Algorithm:
    1. Load recent SELL trades with signal snapshots
    2. For each signal, compute weighted average outcome score
       (exponential decay: recent trades matter more)
    3. Normalize to weight range [SIGNAL_WEIGHT_MIN, SIGNAL_WEIGHT_MAX]
    4. Apply inertia smoothing against current weights
    5. Clamp max delta per cycle to prevent oscillation
    6. If fewer than SIGNAL_WEIGHT_MIN_TRADES, return all 1.0 (neutral)

    Drawdown mode: instead of freezing, learns at 1/3 rate from
    winning trades only. This preserves adaptation to new regimes
    while filtering noise from loss-driven conditions.

    Args:
        limit: Max trades to consider (rolling window)
        current_weights: Previous weights for inertia blending.
        in_drawdown: If True, use reduced learning rate + winners only.

    Returns:
        dict mapping signal name -> weight (0.5 to 1.5)
    """
    trades = get_trades_with_snapshots(limit=limit)

    if len(trades) < SIGNAL_WEIGHT_MIN_TRADES:
        logger.debug(
            f"Only {len(trades)} trades with snapshots "
            f"(need {SIGNAL_WEIGHT_MIN_TRADES}) — using neutral weights"
        )
        return current_weights or {name: 1.0 for name in SIGNAL_NAMES}

    # Drawdown mode: filter to winners only (preserves adaptation, filters noise)
    if in_drawdown:
        trades = [t for t in trades if t.get("outcome_label") in ("strong_win", "weak_win")]
        if len(trades) < 5:
            logger.debug("Drawdown mode: too few winning trades to learn from — keeping current weights")
            return current_weights or {name: 1.0 for name in SIGNAL_NAMES}

    # Per-signal: accumulate (outcome_score * decay_weight) when signal was active
    signal_scores: dict[str, float] = {name: 0.0 for name in SIGNAL_NAMES}
    signal_counts: dict[str, float] = {name: 0.0 for name in SIGNAL_NAMES}

    for i, trade in enumerate(trades):
        # trades are newest-first, so i=0 is most recent
        decay = SIGNAL_WEIGHT_DECAY ** i
        outcome = trade.get("outcome_label", "")
        outcome_score = OUTCOME_SCORES.get(outcome, 0.0)
        snapshot = trade.get("signal_snapshot", {})

        for name in SIGNAL_NAMES:
            signal_value = snapshot.get(name, 0)
            if isinstance(signal_value, (int, float)) and signal_value > 0:
                signal_scores[name] += outcome_score * decay
                signal_counts[name] += decay

    # Compute raw learned weight per signal
    learned = {}
    for name in SIGNAL_NAMES:
        if signal_counts[name] > 0:
            avg_score = signal_scores[name] / signal_counts[name]
            mid = (SIGNAL_WEIGHT_MAX + SIGNAL_WEIGHT_MIN) / 2
            half_range = (SIGNAL_WEIGHT_MAX - SIGNAL_WEIGHT_MIN) / 2
            weight = mid + avg_score * half_range
            learned[name] = max(SIGNAL_WEIGHT_MIN, min(SIGNAL_WEIGHT_MAX, weight))
        else:
            learned[name] = 1.0

    # Inertia rate: reduced during drawdown (1/3 normal)
    inertia = WEIGHT_INERTIA
    if in_drawdown:
        # Higher inertia = more sticky to old weights = slower change
        inertia = 0.9  # 10% new vs normal 30% new
        logger.info("Signal weights: drawdown mode — learning from winners only at reduced rate")

    # Apply inertia smoothing + delta clamping
    weights = {}
    for name in SIGNAL_NAMES:
        new_w = learned[name]

        if current_weights and name in current_weights:
            old_w = current_weights[name]
            blended = inertia * old_w + (1 - inertia) * new_w
            clamped = max(old_w - WEIGHT_MAX_DELTA, min(old_w + WEIGHT_MAX_DELTA, blended))
            weights[name] = max(SIGNAL_WEIGHT_MIN, min(SIGNAL_WEIGHT_MAX, clamped))
        else:
            weights[name] = new_w

    # Periodic normalization: slowly decay all weights toward 1.0
    # This prevents long-term drift in the 24/7 crypto market
    # where there are no natural "reset" cycles
    for name in SIGNAL_NAMES:
        w = weights[name]
        weights[name] = w + SIGNAL_WEIGHT_NORMALIZE_RATE * (1.0 - w)
        weights[name] = max(SIGNAL_WEIGHT_MIN, min(SIGNAL_WEIGHT_MAX, weights[name]))

    logger.info(
        f"Signal weights updated from {len(trades)} trades: "
        + " ".join(f"{k}={v:.2f}" for k, v in weights.items())
    )

    return weights
