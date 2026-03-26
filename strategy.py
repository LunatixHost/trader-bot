import logging
from config import (
    RSI_BUY_THRESHOLD,
    RSI_SELL_THRESHOLD,
    FEAR_GREED_BUY_MAX,
    CONFIDENCE_BUY_THRESHOLD,
    WHALE_ALERT_COINS,
    ETHERSCAN_COINS,
    SIGNAL_CATEGORY_CAPS,
    REGIME_MODIFIERS,
    DRAWDOWN_THRESHOLD_BOOST,
    BTC_CRASH_THRESHOLD_BOOST,
    NO_TRADE_ATR_PCT,
    NO_TRADE_TREND_PCT,
    MAX_CONCURRENT_TRADES,
    OB_IMBALANCE_BEAR,
    WICK_RATIO_THRESHOLD,
    MIN_EDGE_PCT,
    ATR_TP_MULTIPLIER,
    ATR_TRAILING_MULTIPLIER,
    BASE_TP1_PCT,
    STOP_LOSS_PCT,
    RSI_OB_FLOW_MIN,
    ECONOMIC_MIN_REWARD_PCT,
)

logger = logging.getLogger("trading_bot.strategy")


def _get_raw_signals(pair_state, bot_state):
    base = pair_state.base_asset
    signals = {}

    signals["rsi"] = 1.0 if pair_state.rsi < RSI_BUY_THRESHOLD else 0.0
    signals["bollinger"] = 1.0 if pair_state.bb_position == "below_lower" else 0.0
    signals["macd"] = 1.0 if pair_state.macd_crossover == "bullish" else 0.0
    signals["trends"] = 1.0 if pair_state.google_trend > 50 else 0.0
    signals["volume"] = 1.0 if pair_state.volume_spike else 0.0

    signals["whale"] = (
        1.0
        if (base in WHALE_ALERT_COINS and pair_state.whale_signal in ("neutral", "accumulation"))
        else 0.0
    )
    signals["gas"] = (
        1.0
        if (base in ETHERSCAN_COINS and pair_state.etherscan_activity == "high")
        else 0.0
    )

    if bot_state.fear_greed_score <= FEAR_GREED_BUY_MAX:
        signals["fear_greed"] = 1.0 - (bot_state.fear_greed_score / 100.0)
    else:
        signals["fear_greed"] = 0.0

    return signals, signals.copy()


SIGNAL_CATEGORIES = {
    "rsi": "mean_reversion",
    "bollinger": "mean_reversion",
    "macd": "momentum",
    "trends": "momentum",
    "volume": "sentiment",
    "whale": "onchain",
    "gas": "onchain",
    "fear_greed": "macro",
}


def calculate_confidence(pair_state, bot_state):
    signals, snapshot = _get_raw_signals(pair_state, bot_state)

    weights = bot_state.signal_weights
    regime_mods = REGIME_MODIFIERS.get(
        pair_state.regime, REGIME_MODIFIERS["ranging"]
    )

    category_scores = {cat: 0.0 for cat in SIGNAL_CATEGORY_CAPS}

    for name, value in signals.items():
        if not value:
            continue

        cat = SIGNAL_CATEGORIES.get(name)
        if not cat:
            continue

        weight = weights.get(name, 1.0)
        if name == "macd":
            weight *= 0.5

        mod = regime_mods.get(cat, 1.0)
        category_scores[cat] += value * weight * mod

    total = sum(
        min(category_scores[c], SIGNAL_CATEGORY_CAPS[c])
        for c in category_scores
    )
    return max(0.0, total), snapshot


def _dynamic_threshold(pair_state, bot_state):
    threshold = pair_state.dynamic_params.get(
        "buy_threshold", CONFIDENCE_BUY_THRESHOLD
    )

    if bot_state.in_drawdown:
        threshold += DRAWDOWN_THRESHOLD_BOOST

    if bot_state.btc_crash_active:
        threshold += BTC_CRASH_THRESHOLD_BOOST

    # Regime/trend bias: computed by _update_dynamic_params each RSI cycle.
    # UPTREND -0.2 (entry easier), RANGING 0.0 (baseline), DOWNTREND +0.2 (tighter).
    threshold += pair_state.dynamic_params.get("threshold_bias", 0.0)

    return threshold


def get_decision(pair_state, bot_state):
    # Clear stale sell_reason so a previous SELL cycle never leaks into a HOLD/BUY cycle
    pair_state.dynamic_params.pop("sell_reason", None)

    confidence, snapshot = calculate_confidence(pair_state, bot_state)

    pair_state.confidence_weighted = round(confidence, 2)
    pair_state.confidence_score = int(round(confidence))

    # Raw signal quality before execution-quality adjustments
    effective = confidence + pair_state.structure_score + pair_state.volatility_score

    holding = pair_state.asset_balance > 0

    # --- SELL conditions for held positions (unchanged — preserve risk control) ---
    if holding:
        # Peak-retrace exit: protect profits that have partially reversed.
        # Peak must reach ≥ 0.30% to ensure the trade had real, fee-viable profit before
        # this protection fires. Was 0.15% — too small, could trigger on sub-break-even peaks.
        # Retrace ≥ 0.10% triggers the exit to protect the remaining gain.
        peak_gain = getattr(pair_state, "peak_gain_pct", 0.0)
        if peak_gain >= 0.30 and pair_state.buy_price > 0 and pair_state.current_price > 0:
            current_gain = (pair_state.current_price - pair_state.buy_price) / pair_state.buy_price * 100
            retrace = peak_gain - current_gain
            if retrace >= 0.10:
                pair_state.effective_score = round(effective, 3)
                pair_state.dynamic_params["sell_reason"] = "peak_retrace"
                return "SELL", snapshot

        if pair_state.rsi > RSI_SELL_THRESHOLD:
            pair_state.effective_score = round(effective, 3)
            pair_state.dynamic_params["sell_reason"] = "signal"
            return "SELL", snapshot
        if pair_state.macd_crossover == "bearish":
            pair_state.effective_score = round(effective, 3)
            pair_state.dynamic_params["sell_reason"] = "signal"
            return "SELL", snapshot
        if pair_state.orderbook_imbalance < OB_IMBALANCE_BEAR:
            pair_state.effective_score = round(effective, 3)
            pair_state.dynamic_params["sell_reason"] = "signal"
            return "SELL", snapshot

    threshold = _dynamic_threshold(pair_state, bot_state)

    # Persist effective threshold so panel can display the correct number
    pair_state.dynamic_params["effective_threshold"] = round(threshold, 2)

    # --- Weighted BUY scoring: soft penalties replace hard execution gates ---
    #
    # Every condition below nudges the score rather than hard-blocking.
    # High-conviction setups (effective >> threshold) can trade through
    # adverse conditions; weak setups are naturally filtered.
    #
    # p = penalty_scale: regime-adaptive multiplier on general market-quality penalties.
    #   UPTREND  0.8 — dips more likely to bounce, ease the general filtering
    #   RANGING  1.0 — baseline (unchanged)
    #   DOWNTREND 1.1 — slightly heavier filtering on general conditions
    # Note: trend-specific penalties (downtrend -0.6/-0.3, uptrend +0.3) are NOT
    # scaled — they are already regime-differentiated by definition.
    p = pair_state.dynamic_params.get("penalty_scale", 1.0)

    buy_score = effective

    # Dead market: soft penalty instead of hard block.
    if (
        pair_state.atr_pct < NO_TRADE_ATR_PCT
        and pair_state.trend_strength < NO_TRADE_TREND_PCT
    ):
        buy_score -= 0.5 * p

    # Wick: large upper wick = exhaustion / distribution → penalty
    if pair_state.wick_ratio > WICK_RATIO_THRESHOLD:
        buy_score -= 0.4 * p

    # Min edge: very low ATR constrains expected move → penalty.
    # Strengthened from -0.3 to -0.4: dead-market ATR environments need clearer discouragement.
    if 0 < pair_state.atr_pct < MIN_EDGE_PCT / 100:
        buy_score -= 0.4 * p

    # Economic viability: ATR-based expected reward vs minimum viable profit after fees.
    # Expected reward = ATR × TP_multiplier × 100 (the realistic 1-ATR-unit move).
    # If this is below ECONOMIC_MIN_REWARD_PCT (0.35%), the setup can't realistically
    # produce a net-positive trade even with a clean exit — strong penalty applied.
    # This is distinct from MIN_EDGE_PCT (which checks absolute ATR size);
    # this checks whether the expected REWARD clears the fee hurdle.
    if pair_state.atr_pct > 0:
        atr_reward_pct = pair_state.atr_pct * ATR_TP_MULTIPLIER * 100
        if atr_reward_pct < ECONOMIC_MIN_REWARD_PCT:
            buy_score -= 0.5 * p  # Strong: expected move can't beat round-trip costs

    # R/R quality: graduated penalty when expected reward doesn't justify risk
    if pair_state.atr_pct > 0:
        tp1 = pair_state.dynamic_params.get("tp1", BASE_TP1_PCT)
        atr_reward = pair_state.atr_pct * ATR_TP_MULTIPLIER * 100
        expected_reward = max(tp1, atr_reward, 0.35)
        atr_risk = min(pair_state.atr_pct * ATR_TRAILING_MULTIPLIER * 100, 2.0)
        expected_risk = max(STOP_LOSS_PCT, atr_risk, 0.20)
        rr = expected_reward / expected_risk if expected_risk > 0 else 0.0
        if rr < 1.0:
            buy_score -= 0.3 * p
        elif rr < 1.20:
            buy_score -= 0.2 * p

    # Read microstructure state once — used by both the downtrend and general OB/flow checks.
    ob = getattr(pair_state, "orderbook_imbalance", 0.5)
    flow = getattr(pair_state, "flow_ratio", 0.5)

    # Trend direction.
    # Downtrend: two-layer penalty (NOT regime-scaled — these ARE the regime logic).
    #   Layer 1 (-0.6) — unconditional base penalty for counter-trend entry risk.
    #   Layer 2 (-0.3) — additional when OB OR flow shows no buyer activity.
    # Uptrend: slight boost for entering dips with the primary trend.
    trend = getattr(pair_state, "trend", "chop")
    if trend == "downtrend":
        buy_score -= 0.6
        if ob < 0.5 or flow < 0.5:
            buy_score -= 0.3
    elif trend == "uptrend":
        buy_score += 0.3

    # Regime: choppy volatile is low-quality signal environment → regime-scaled penalty
    if pair_state.regime == "choppy_volatile":
        buy_score -= 0.2 * p

    # Weak microstructure (general, any trend): both OB and flow below neutral.
    if ob < 0.5 and flow < 0.5:
        buy_score -= 0.4 * p

    # RSI confirmation: RSI oversold is a dip signal, not a bounce signal.
    # Require at least one of OB/flow above RSI_OB_FLOW_MIN to confirm buyers responding.
    if pair_state.rsi < RSI_BUY_THRESHOLD and ob < RSI_OB_FLOW_MIN and flow < RSI_OB_FLOW_MIN:
        buy_score -= 0.4 * p

    # Early failure exit: if holding and raw signal quality has deteriorated sharply,
    # exit before forced stops are hit (uses raw effective, not penalized buy_score)
    if holding and effective < threshold - 1.5:
        pair_state.effective_score = round(buy_score, 3)
        pair_state.dynamic_params["sell_reason"] = "early_failure"
        return "SELL", snapshot

    # Store the penalized buy_score as effective_score — this is what panel shows.
    pair_state.effective_score = round(buy_score, 3)

    # Concurrent position cap (hard limit — capital safety)
    if not holding:
        open_positions = sum(
            ps.asset_balance > 0 for ps in bot_state.pairs.values()
        )
        if open_positions >= MAX_CONCURRENT_TRADES:
            return "HOLD", snapshot

    # High-conviction override: if raw signal quality clears threshold by a
    # comfortable margin, allow the buy even if penalties dragged buy_score below.
    # Explicitly excluded in downtrend: continuation risk is real and the
    # downtrend penalty stack must be respected, not bypassed by signal strength alone.
    if not holding and trend != "downtrend" and effective >= threshold + 0.8:
        return "BUY", snapshot

    if buy_score >= threshold:
        return "BUY", snapshot

    return "HOLD", snapshot


def get_trade_size_modifier(bot_state):
    return 0.5 if bot_state.in_drawdown else 1.0


def get_btc_cluster_size_modifier(pair_state, bot_state):
    return 1.0


def get_correlation_modifier(pair_state, bot_state):
    return 1.0
