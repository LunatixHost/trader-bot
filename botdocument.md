# Trading Bot — Complete Documentation

> Last updated: 2026-03-26 (rev 3)
> Mode: **Scalping** | Exchange: **Binance Spot** | Pairs: ETH, BTC, BNB, XRP, BCH, SUI

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Signal Sources](#2-signal-sources)
3. [Regime Detection](#3-regime-detection)
4. [Confidence Scoring](#4-confidence-scoring)
5. [Effective Score](#5-effective-score)
6. [Entry Filters (BUY Gates)](#6-entry-filters-buy-gates)
7. [Exit System (SELL Logic)](#7-exit-system-sell-logic)
8. [Adaptive Signal Weights](#8-adaptive-signal-weights)
9. [Portfolio Management](#9-portfolio-management)
10. [BTC Crash Filter](#10-btc-crash-filter)
11. [Correlation System](#11-correlation-system)
12. [Discord Control Panel](#12-discord-control-panel)
13. [Web Dashboard](#13-web-dashboard)
14. [Database & Logging](#14-database--logging)
15. [Configuration Reference](#15-configuration-reference)

---

## 1. Architecture Overview

The bot runs as a **single Python asyncio process**. All components — signal polling, trading loop, Discord bot, web API — run concurrently as async tasks without threads blocking each other.

### Startup Sequence

```
1. Load .env  →  validate Binance + Discord API keys
2. Connect to Binance (testnet or live)
3. Load saved state from trades.db (or create fresh)
4. Open WebSocket price stream per pair
5. Start signal polling tasks per pair (staggered to avoid rate limits)
6. Launch Discord bot
7. Start FastAPI web server (dashboard)
8. Begin trading loop
```

### Main Loop (per pair, every price tick)

```
WebSocket tick → update current_price
       ↓
get_decision(pair_state, bot_state)
       ↓
   ┌───────────────────────────────────────┐
   │  If HOLDING:  Run SELL checks         │
   │  If NOT:      Run BUY gates           │
   └───────────────────────────────────────┘
       ↓
  BUY → execute_buy()   SELL → execute_sell()   HOLD → nothing
```

### Key Files

| File | Purpose |
|------|---------|
| `bot.py` | Main entry point, trading loop, signal pollers, order execution |
| `strategy.py` | Decision engine: confidence scoring, all BUY/SELL logic |
| `config.py` | All tuneable parameters |
| `state.py` | `BotState` + `PairState` dataclasses, DB persistence |
| `regime.py` | Regime classification (ATR + EMA spread) |
| `portfolio.py` | Portfolio value tracking, reserve management |
| `signal_weights.py` | Adaptive weight learning from trade history |
| `signals/rsi.py` | RSI, MACD, Bollinger Bands, ATR, wick ratio, liquidity sweep |
| `signals/coingecko.py` | Volume spike detection, 24h price change |
| `signals/whale_alert.py` | On-chain large transaction detection |
| `signals/etherscan.py` | Ethereum gas + network activity (ETH only) |
| `signals/google_trends.py` | Google Trends search interest per coin |
| `signals/fear_greed.py` | Crypto Fear & Greed Index |
| `signals/fvg.py` | Fair Value Gap detection on 5m candles |
| `signals/orderbook.py` | Orderbook imbalance + trade flow (microstructure) |
| `discord_bot/` | Discord panel, buttons, views |
| `dashboard/` | Web terminal (FastAPI + TradingView Lightweight Charts) |
| `trades.db` | SQLite database — all trades, snapshots, signal history |

---

## 2. Signal Sources

The bot uses **8 active signal sources** across 4 polling tiers. All signals feed into the confidence scoring engine. A 9th source (CryptoCompare social stats) is implemented in `signals/cryptocompare.py` but currently disabled — its output is not consumed by the strategy.

### Tier 1 — Every 10 seconds (`INTERVAL_RSI = 10`, `INTERVAL_ORDERBOOK = 10`)

Computed from the same Binance OHLCV fetch (15m candles) plus a live orderbook fetch. No heavy API calls.

| Signal | What it measures | Buy condition |
|--------|-----------------|---------------|
| **RSI** | Relative Strength Index (14-period, Wilder's smoothing) | RSI < 55 (oversold) |
| **MACD** | MACD histogram crossover (12/26/9 EMA) | Bullish crossover (histogram turning positive) |
| **Bollinger Bands** | Price position relative to 20-period BB (2σ) | Price below lower band |
| **ATR** | Average True Range — used for exits, not scoring | — |
| **Liquidity Sweep** | Wick below recent swing low scaled by ATR depth | Sweep score contributes to effective score |
| **Orderbook Imbalance** | bid_vol / (bid + ask), top 5 levels, EMA-smoothed (α=0.3) | Imbalance > 0.6 = buyers stacking |
| **Trade Flow Ratio** | buyer-aggressor volume / total, last 50 trades, EMA-smoothed | Flow ratio > 0.6 = buyers lifting asks |

### Tier 2 — Every 60 seconds (`INTERVAL_COINGECKO = 60`)

| Signal | What it measures | Buy condition |
|--------|-----------------|---------------|
| **CoinGecko Volume** | 24h volume vs rolling average | Volume > 1.5× average = spike |

Volume spike sets both `volume_spike` and `volume_confirmed` — used by the chop gate and momentum decay exit.

### Tier 3 — Every 2–15 minutes

| Signal | Interval | Pairs | Buy condition |
|--------|----------|-------|---------------|
| **Whale Alert** | 120s | ETH, BTC, BNB, XRP | `whale_signal == "neutral"` or `"accumulation"` |
| **Etherscan Gas** | 300s | ETH only | High network activity |
| **Google Trends** | 900s | All (coin-specific keyword) | Trend score > 50 |

### Tier 4 — Every hour (`INTERVAL_FEAR_GREED = 3600`)

| Signal | What it measures | Buy condition |
|--------|-----------------|---------------|
| **Fear & Greed Index** | Market-wide sentiment (0=Extreme Fear, 100=Extreme Greed) | Score ≤ 50 (fear zone) |

Fear & Greed is **shared across all pairs** — one fetch covers all six coins.

### FVG — Every 60 seconds (`INTERVAL_FVG = 60`)

**Fair Value Gap** detection on 5m candles. A FVG is an imbalance gap where a middle candle's body doesn't overlap the wicks of the candles before and after it.

FVG is an **active execution edge**, not a passive score boost. Detected gaps are validated before triggering any order:

**Validation gates (all must pass):**
- Gap size ≥ 0.1% of current price (`FVG_MIN_GAP_PCT`) — smaller gaps are noise
- Gap age ≤ 300s (`FVG_MAX_AGE_SECONDS`) — stale gaps (> 1 candle old) are discarded
- `fast_score ≥ 2.0` — minimum technical conviction required
- `orderbook_imbalance ≥ 0.5` — microstructure must not be bearish
- FVG cooldown: 5 minutes between FVG-driven trades on the same pair

**Two entry paths:**

| Path | Condition | Order type |
|------|-----------|------------|
| Standard | `current_price > fvg_mid` (price above gap midpoint — approaching from above) | Limit buy at `fvg_mid` |
| Early entry | `fvg_bottom < current_price ≤ fvg_mid` (price already inside the gap) | Market buy immediately |

- Standard path: limit order placed, `fvg_limit_active = True` while waiting for fill. Order cancelled if unfilled after **60s** (`FVG_ORDER_TIMEOUT`).
- Early path: market buy executed; `fvg_entry = True` flag set on position.

**FVG-specific exits (highest priority after hard stop-loss):**
- **FVG failure exit**: if price drops below `fvg_bottom` while in a FVG entry → immediate SELL
- **FVG profit accelerator**: if gain ≥ 0.25% on a FVG entry → immediate SELL (tighter than micro TP)

FVG adds **+0.5** to the effective score when active (only if `fvg_detected = True`).

### Signal Applicability Per Pair

| Signal | ETH | BTC | BNB | XRP | BCH | SUI |
|--------|-----|-----|-----|-----|-----|-----|
| RSI / MACD / BB | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Orderbook / Flow | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| CoinGecko Volume | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Fear & Greed | ✓ shared | ✓ shared | ✓ shared | ✓ shared | ✓ shared | ✓ shared |
| Google Trends | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Whale Alert | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |
| Etherscan Gas | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ |

---

## 3. Regime Detection

Every RSI cycle, the bot classifies each pair into one of **4 regimes** using ATR% and EMA spread.

### Classification Logic

```
ATR% = ATR / current_price
Trend strength = |EMA20 - EMA50| / price

is_volatile  = ATR% > 1.5%
is_trending  = trend_strength > 0.5%

         | Not Trending  | Trending           |
---------|---------------|---------------------|
Normal   | ranging       | trending            |
Volatile | choppy_volatile | trending_volatile |
```

### Hysteresis

Regime **must be seen 3 consecutive times** before switching. In extreme volatility (ATR% > 3%), only 2 confirmations needed. This prevents the regime from flickering at boundaries on every candle.

### Trend Direction

Separately from regime, `trend_direction` tracks EMA20 vs EMA50:
- `"up"` → EMA20 > EMA50 (bullish medium-term)
- `"down"` → EMA20 < EMA50 (bearish medium-term)
- `"flat"` → insufficient data

`trend_direction` feeds into:
- `pair_state.trend` label (`"uptrend"` / `"downtrend"` / `"chop"`)
- Downtrend guard in BUY logic

### Regime → `pair_state.trend` Label

| trend_direction | pair_state.trend |
|-----------------|-----------------|
| `"up"` | `"uptrend"` |
| `"down"` | `"downtrend"` |
| `"flat"` | `"chop"` |

---

## 4. Confidence Scoring

Replaces the old flat signal count with a **weighted, category-capped, regime-adjusted** score.

### Formula

```
confidence = Σ ( signal_value × learned_weight × regime_modifier )
             with per-category caps applied
```

### Signal Categories & Caps

Each category has a maximum contribution to prevent correlated signals from double-counting.

| Category | Signals | Cap |
|----------|---------|-----|
| `mean_reversion` | RSI + Bollinger Bands | 1.5 |
| `momentum` | MACD + Google Trends | 1.5 |
| `sentiment` | CoinGecko Volume | 1.2 |
| `onchain` | Whale Alert + Etherscan Gas | 1.0 |
| `macro` | Fear & Greed | 1.0 |

### Regime Modifiers

Each regime boosts/reduces category weights:

| Category | trending | ranging | choppy_volatile | trending_volatile |
|----------|----------|---------|-----------------|-------------------|
| mean_reversion | 0.6× | 1.4× | 0.7× | 0.5× |
| momentum | 1.4× | 0.6× | 0.7× | 1.3× |
| sentiment | 1.0× | 0.8× | 1.3× | 1.2× |
| onchain | 1.0× | 1.0× | 1.2× | 1.0× |
| macro | 1.0× | 1.0× | 1.0× | 0.8× |

> **Example**: In `ranging` regime, RSI and BB are boosted (1.4×) because mean-reversion works best in sideways markets. MACD is reduced (0.6×) because it gives false signals in chop.

### MACD Dampening

MACD's learned weight is additionally halved (`× 0.5`) because it's a smoothing filter rather than a primary trigger — it confirms momentum but shouldn't dominate.

---

## 5. Effective Score

The final score used for buy decisions is the **buy_score**, built from raw confidence with soft adjustments applied. It is stored as `pair_state.effective_score` at the end of every strategy cycle.

```
raw_effective = confidence + structure_score + volatility_score

buy_score = raw_effective
          − 1.0   if wick_ratio > 60%          (exhaustion / distribution)
          − 0.8   if 0 < ATR% < 0.3%           (minimum edge not met)
          − 1.0   if RR < 1.0                  (reward doesn't justify risk)
          − 0.5   if 1.0 ≤ RR < 1.20          (marginal R/R)
          − 1.0   if trend == "downtrend"       (counter-trend penalty)
          + 0.3   if trend == "uptrend"         (dip-buying-with-trend boost)
          − 0.3   if regime == "choppy_volatile" (noisy signal environment)
```

`structure_score` and `volatility_score` are computed by `regime.py` and stored on `PairState`. Both are currently `0.0` in all regimes (reserved for future expansion).

**Key principle:** conditions that previously hard-blocked trades are now score penalties. A high-conviction setup can absorb penalties and still exceed the threshold; a weak setup will not.

### R/R Calculation

```
expected_reward = max(tp1_pct, ATR% × ATR_TP_MULTIPLIER × 100, 0.35%)
expected_risk   = max(STOP_LOSS_PCT, ATR% × ATR_TRAILING_MULTIPLIER × 100, 0.20%)
RR = expected_reward / expected_risk
```

### Dynamic Buy Threshold

The threshold the buy_score must exceed is not fixed:

```
threshold = pair_state.dynamic_params["buy_threshold"]   ← per-pair base (volatility-scaled)
          + DRAWDOWN_THRESHOLD_BOOST  if in_drawdown      (+ 0.5)
          + BTC_CRASH_THRESHOLD_BOOST if btc_crash_active (+ 1.0)
```

The per-pair base threshold uses a **volatility factor** (current ATR / 20-period avg ATR) with exponential smoothing, clamped to [0.5×, 1.5×]. High volatility → higher threshold.

Base threshold: **3** (configurable via `CONFIDENCE_BUY_THRESHOLD`).

---

## 6. Entry Decision System (Weighted Scoring)

The bot uses a **weighted scoring system, not sequential gating**. Adverse conditions reduce the `buy_score` (soft penalties) rather than hard-blocking the trade. Only a small number of execution-safety checks remain as hard stops.

### Strategy Layer (`strategy.py`)

`get_decision()` produces BUY / SELL / HOLD. It applies all signal interpretation, scoring, and penalty logic.

#### Dead Market (hard block)
```
if ATR% < 0.3% AND trend_strength < 0.1%:
    → HOLD  (both conditions simultaneously — market too dead for any edge)
```

#### Sell Conditions (when holding)
Three signal-based exits trigger a SELL from strategy, tagged with `sell_reason = "signal"`:
```
RSI > RSI_SELL_THRESHOLD (70)   → SELL
MACD crossover == "bearish"     → SELL
orderbook_imbalance < 0.4       → SELL
```

#### Early Failure Exit (when holding)
```
if holding AND raw_effective < threshold - 1.5:
    sell_reason = "early_failure"
    → SELL  (trade has lost signal conviction — exit before forced stops trigger)
```
Uses `raw_effective` (pre-penalty score) — compares underlying signal quality, not the penalized buy_score.

#### Buy Score Soft Penalties
After computing `buy_score = raw_effective`, conditions reduce it:

| Condition | Penalty |
|-----------|---------|
| `wick_ratio > 0.6` | −1.0 (exhaustion spike) |
| `0 < ATR% < 0.3%` | −0.8 (minimum edge not met) |
| `RR < 1.0` | −1.0 (reward below risk) |
| `1.0 ≤ RR < 1.20` | −0.5 (marginal R/R) |
| `trend == "downtrend"` | −1.0 (counter-trend) |
| `trend == "uptrend"` | +0.3 (dip buying with trend) |
| `regime == "choppy_volatile"` | −0.3 (noisy environment) |

#### Concurrent Position Cap (hard block)
```
if NOT holding AND open_positions >= MAX_CONCURRENT_TRADES (3):
    → HOLD  (capital safety — hard limit)
```

#### BUY Decision
```
if buy_score >= threshold:
    → BUY
else:
    → HOLD
```

### Execution Layer (`bot.py`)

`execute_buy()` has a second set of checks via `get_entry_block_reason()`. These run after strategy approves BUY and enforce capital safety / market-quality gates. Any blocker returns early without placing an order.

The same function (`_entry_block_reason` mirror) is used by the panel to display the real blocking reason.

#### Execution Blockers (in priority order)

| # | Blocker | Condition |
|---|---------|-----------|
| 1 | `add_blocked` | Holding and `current_price < buy_price` (no averaging down into loss) |
| 2 | `cooldown` | Time since last trade < cooldown (300s base, 180s in trending regimes); bypass if `effective_score ≥ threshold + 5.0` AND regime is trending |
| 3 | `spike_filter` | Single tick moved > 0.3% — avoid chasing spikes |
| 4 | `spread_filter` | Bid/ask spread > 0.15% (live only — testnet spreads are unreliable) |
| 5 | `insufficient_spendable` | `usdt_balance − usdt_reserved ≤ $1` |
| 6 | `position_maxed` | Position value ≥ `portfolio × MAX_POSITION_PCT (10%)` |
| 7 | `min_notional` | Computed trade size < `MIN_TRADE_NOTIONAL ($20)` |
| 8 | `fvg_cooldown` | FVG trade within last 300s on same pair |

If any blocker returns a string, the buy is rejected and the reason is logged (debug level). The panel's WATCHING block shows this reason alongside the score.

### Signal Sell Guard (`_should_allow_signal_sell`)

Strategy-originated SELLs tagged `sell_reason = "signal"` pass through a guard in `execute_sell()`. This prevents premature exits from weak sell signals while winners are still running.

```
Allow SELL if any of:
  1. pnl_pct >= max(tp1_pct, 0.35%)       (at or past TP1 — capture the gain)
  2. hold_minutes >= hard_exit_minutes AND in profit (stale trade, still green)
  3. pnl_pct > 0.10% AND eff < threshold − 1.0  (small green + collapsed signal — let it go)
```

Protective exits (`sell_reason = "early_failure"` or `"risk_exit"`) bypass this guard entirely and always execute.

---

## 7. Exit System (SELL Logic)

Exits are split across two layers: **forced exits** (time/price stops, checked in `bot.py`) and **signal exits** (from strategy, filtered by a sell guard). The layers run in this order per trade cycle:

```
1. _get_forced_exit_reason()    ← price/time stops — always execute
2. TP1 check                    ← profit target — always execute
3. strategy.get_decision() SELL ← signal-based — filtered by _should_allow_signal_sell
```

### Sell Action Taxonomy

Every SELL writes a `note` to the trades table. The canonical set:

| Action | Source | Description |
|--------|--------|-------------|
| `SIGNAL_SELL` | Strategy signal | RSI/MACD/OB sell passed the sell guard |
| `EARLY_FAILURE` | Strategy scoring | Trade lost signal conviction (`effective < threshold − 1.5`) |
| `HARD_STOP` | Forced exit | Hard stop price hit |
| `TRAIL_STOP` | Forced exit | Trailing stop triggered |
| `TIME_STOP` | Forced exit | Hold time limit reached with insufficient gain |
| `MAX_HOLD_EXIT` | Forced exit | Absolute maximum hold time exceeded |
| `TP1_FULL_EXIT` | Profit target | TP1 threshold hit |
| `FORCE SELL` | Discord manual | Owner-initiated via Abandon button |
| `STOP_LOSS` | Legacy | Used in older trades; equivalent to HARD_STOP |

### Forced Exit Checks (`_get_forced_exit_reason`)

Checked before strategy decisions, in priority order:

#### 1. Hard Stop-Loss
```
hard_stop_price = entry × (1 − STOP_LOSS_PCT / 100)  ← also incorporates ATR-based initial stop
if current_price <= hard_stop_price:
    → HARD_STOP
```

#### 2. ATR Trailing Stop
```
if trailing_stop > 0 AND current_price <= trailing_stop:
    → TRAIL_STOP
```
- Activates after `TRAILING_ACTIVATION_PCT` (0.5%) gain
- Trails at `TRAILING_DISTANCE_PCT` (0.15%) below current price, ratchets up only
- ATR-based initial stop = `entry − ATR × ATR_TRAILING_MULTIPLIER (2.0×)`, capped at 2%

#### 3. Time Stop
```
if hold_minutes >= hard_exit_minutes AND pnl_pct <= 0.15%:
    → TIME_STOP
```

#### 4. Max Hold Exit
```
if hold_minutes >= hard_exit_minutes × 1.5:
    → MAX_HOLD_EXIT
```

`hard_exit_minutes` is derived from `SCALP_HARD_HOLD_MINUTES` (20 min), adjusted by regime.

### TP1 Exit

Checked after forced exits. If `pnl_pct >= tp1_pct` (dynamic, default `BASE_TP1_PCT = 0.3%`):
- Calls `execute_sell(note="TP1_FULL_EXIT")` directly — does not go through signal guard.

### Strategy Signal Exits

Strategy returns SELL with `sell_reason` tag. The execution layer routes by tag:

- `sell_reason = "early_failure"` → **bypass** sell guard, execute as `EARLY_FAILURE`
- `sell_reason = "risk_exit"` → **bypass** sell guard (reserved, future use)
- `sell_reason = "signal"` (RSI/MACD/OB) → **gated** by `_should_allow_signal_sell`

When the sell guard blocks a signal sell, the trade continues to be managed by the forced exits. This prevents premature exits from noisy signal reversals while winners are still running.

### Dust Handling

When `execute_sell` is called on a position worth less than $5:
- No Binance API call is made (it would fail anyway)
- Position is cleared from state immediately
- Trade is not logged (no real transaction occurred)

---

## 8. Adaptive Signal Weights

After accumulating **20+ completed trades**, the bot begins learning which signals actually predict profitable outcomes.

### Learning Algorithm

Every 5 minutes (`PORTFOLIO_REBALANCE_INTERVAL`), the bot:

1. Loads the last 300 completed SELL trades from the DB
2. For each signal, checks whether it was active at entry time
3. Scores each trade as:

| Outcome | Score |
|---------|-------|
| Strong win (> threshold) | +1.0 |
| Weak win | +0.3 |
| Scratch (~0%) | 0.0 |
| Loss | −1.0 |

4. Applies **exponential decay** (`0.95^i`) so recent trades matter more than old ones
5. Maps the average outcome score to a weight in range `[0.5, 1.5]`

### Stability Controls

| Control | Value | Purpose |
|---------|-------|---------|
| Inertia blend | 70% old + 30% new | Prevents sudden jumps from a few bad trades |
| Max delta per cycle | ±0.2 | Hard clamp on how fast any weight can shift |
| Min trades | 20 | Weights stay at 1.0 until enough history exists |
| Normalization | 2% decay toward 1.0 per cycle | Prevents long-term drift in 24/7 markets |

### Drawdown Mode

During portfolio drawdown, the weight learner:
- Filters to **winning trades only** (ignores loss trades to avoid overreacting)
- Reduces inertia to 90% old / 10% new (much slower adaptation)
- If fewer than 5 winning trades available, keeps current weights unchanged

---

## 9. Portfolio Management

### Capital Allocation

| Parameter | Value | Meaning |
|-----------|-------|---------|
| Portfolio reserve | 50% | Half of total capital is never deployed |
| Trade size | 3% per trade | Each buy = 3% of total portfolio value |
| Max position | 10% per coin | No single coin can exceed 10% of portfolio |
| Max concurrent trades | 3 | At most 3 open positions at any time |

On a $82,000 portfolio:
- Reserve: ~$41,000 never touched
- Trade size: ~$1,640 per entry
- Max in any coin: ~$8,200

### Portfolio Value Calculation

```
total_value = usdt_balance + Σ(asset_balance × current_price for all pairs)
```

Recalculated on **every WebSocket price tick** — P/L is always live, not delayed.

### Drawdown Mode

```
if portfolio_current_value < portfolio_peak_value × (1 - 5%):
    in_drawdown = True
    → buy threshold += 0.5  (harder to enter)
    → trade size × 0.5      (half position sizes)
    → signal weight learner switches to winners-only mode
```
Drawdown mode automatically deactivates when the portfolio recovers above the trigger.

### Rebalancing

Every 5 minutes, `rebalance()` recalculates:
- `usdt_reserved = total_portfolio × 50%`
- `trade_amount_usdt = total_portfolio × 2%` per pair

---

## 10. BTC Crash Filter

Monitors BTC's 15-minute price change. When BTC drops sharply:

```
if BTC 15m change < -2.0%:
    btc_crash_active = True  (lasts 15 minutes)
    → buy threshold += 1.0 for ALL pairs
    → trade size × 0.25 for ALL pairs (25% of normal)
```

This is a **market-wide risk-off mode**. When BTC dumps -2% in 15 minutes, cascading liquidations typically drag all altcoins down with it. The filter reduces exposure across every pair while the storm passes.

After 15 minutes (`BTC_CRASH_COOLDOWN = 900s`), normal mode resumes automatically.

---

## 11. Correlation System

### Correlation Groups

```python
{"BTC", "ETH", "BCH"}   # Bitcoin ecosystem
{"BNB", "SUI"}           # Binance ecosystem
```

When entering a position in a correlated asset that's already moving in the same direction as an existing position in the same group, trade size is **halved**:

```
if correlated_asset has open position AND same direction:
    trade_size × 0.5
```

This prevents doubling down on the same macro move with two separate trades.

### BTC Cluster Size Modifier (Altcoins)

`get_btc_cluster_size_modifier()` returns a trade-size multiplier for altcoins (BNB, SUI, XRP). Currently returns `1.0` (pass-through) — the hook exists for future implementation of cluster-aware sizing.

---

## 12. Discord Control Panel

The Discord bot posts a **live panel** in the `#trading-bot` channel, auto-refreshing every **10 seconds**.

### Panel Contents

**GROWTH section:**
- Start value, current value, total P/L (USDT + %), peak value, max drawdown, uptime

**PER COIN section (all pairs):**
- Asset balance, entry price, current price, position value, P/L per coin

**HOLDING section (open positions):**
- Full detail per open position: regime, effective score, signal breakdown (BOS/Sweep/Vol/RSI/Trend), quantity, entry, held time, P/L, stop-loss, trailing stop

**WATCHING section (no position):**
- Summary per idle pair: regime, `Eff: X.X / Th: X.X`, signal breakdown (BOS/Sweep/Vol/RSI), OB/Flow, trend
- Status shows `READY ✓`, `HOLD (need +X.X)`, `BLOCKED (reason)`, or `READY but BLOCKED (reason)` — the last case means strategy approved BUY but execution would still reject it (e.g. cooldown, no capital)
- Block reason is computed by the same `_entry_block_reason()` logic as `execute_buy()`, keeping panel and execution permanently aligned

**MARKET STATE:**
- Fear & Greed score + label
- BTC crash filter status
- Drawdown mode status

**USDT section:**
- Available balance, reserved amount, total trades, win rate

### Buttons

| Button | Action |
|--------|--------|
| ⏸ Pause / ▶ Resume | Toggle trading (signals still update, no new orders placed) |
| 📊 Signals | Select a pair → detailed signal card with all raw values |
| 📈 Chart | Select a pair → candlestick chart image with BUY/SELL markers |
| 💼 Portfolio | Full portfolio breakdown embed |
| 📜 Trades | Last 10 completed trades with P/L |
| ⚙️ Config | Current config values (thresholds, intervals, mode) |
| 🗑️ Abandon | Force-sell a position — places a real market sell order regardless of P/L. Falls back to state-clear if position is below Binance's minimum notional ($5). Shows all open positions with current P/L before asking for confirmation. |
| 📊 Exit Analytics | Per-action exit breakdown: count, win%, avg P/L, total P/L for every sell action type (SIGNAL_SELL, EARLY_FAILURE, HARD_STOP, TRAIL_STOP, TIME_STOP, etc.) |

### Owner Lock

All buttons check `interaction.user.id == DISCORD_OWNER_ID` before responding. Non-owners see a permission denied message.

### Abandon / Force Sell Flow

```
Click 🗑️ Abandon
    → Shows buttons for each open position (label = "BTC +2.7%", colour = red if loss)
    → Click a coin → confirmation card showing: qty, value, entry, P/L, dust warning if < $5
    → Click ✅ Confirm Sell → execute_sell() called with note="FORCE SELL"
        → If value ≥ $5: real Binance market sell order placed
        → If value < $5 (dust): state cleared locally, no API call
    → Click ❌ Cancel → nothing happens, view expires in 30s
```

---

## 13. Web Dashboard

A FastAPI + uvicorn server runs embedded in the main bot process, serving a real-time trading terminal at `http://localhost:8000`.

### Features

- **TradingView Lightweight Charts v4.1.1** — professional candlestick chart with volume bars
- **300 candles** loaded on startup, auto-scroll to latest
- **Watchlist** — all pairs with live prices, colour-coded by trend
- **Signal panel** — live effective score, individual signal status, regime label
- **Portfolio sidebar** — P/L, drawdown, win rate
- Live data via WebSocket push from the bot's state

---

## 14. Database & Logging

All trade history is stored in **`trades.db`** (SQLite).

### Tables

| Table | Contents |
|-------|---------|
| `trades` | Every BUY and SELL: pair, price, qty, USDT, RSI, F&G, confidence, P/L, regime, ATR, signal snapshot (JSON), outcome_label, hold_minutes, confidence_weighted |
| `portfolio_snapshots` | Portfolio value + P/L saved every 30 minutes |
| `bot_state` | Full bot state (serialised JSON) — id=1, one row, replaced on save |
| `pair_states` | Per-pair state (serialised JSON), keyed by pair symbol — restored on restart |

### Sell Action Taxonomy in DB

All exit trades are identified by their `action` column. The canonical set (`_ALL_SELL_ACTIONS` in `logger.py`) covers:

```
SELL, STOP_LOSS, SIGNAL_SELL, EARLY_FAILURE, HARD_STOP, TRAIL_STOP,
TIME_STOP, MAX_HOLD_EXIT, TP1_FULL_EXIT, FORCE SELL
```

Any query filtering for "exit trades" must use this full set, not just `('SELL', 'STOP_LOSS')`. All DB helper functions in `logger.py` (`get_trade_stats`, `get_trade_summary`, `get_trades_with_snapshots`, `get_exit_analytics`) use parameterized queries against `_ALL_SELL_ACTIONS`.

### Exit Analytics

`get_exit_analytics()` returns a breakdown per action type:

```python
{
    "total_exits": 42,
    "by_action": {
        "SIGNAL_SELL": {"count": 18, "wins": 12, "total_pl": 34.50, "avg_pl": 1.92},
        "HARD_STOP":   {"count":  8, "wins":  0, "total_pl": -28.40, "avg_pl": -3.55},
        ...
    }
}
```

Used by `build_exit_analytics_embed()` in the Discord panel.

### Signal Snapshots

Every trade stores the full signal state at entry as a JSON blob:
```json
{
  "rsi": 1.0, "macd": 0.0, "bollinger": 1.0,
  "fear_greed": 0.72, "volume": 0.0, "whale": 1.0, "gas": 0.0, "trends": 0.0,
  "raw_rsi": 38.4, "raw_macd_hist": -0.0012, "raw_bb_position": "below_lower",
  "raw_fear_greed": 28, "raw_google_trend": 62, "raw_whale": "neutral",
  "raw_volume_spike": false, "raw_gas": 0
}
```

This snapshot is what the adaptive weight learner reads to correlate signal states with trade outcomes.

### State Persistence

The full `BotState` (all pair states, balances, positions, signal weights) is saved to `trades.db` after every trade and every 30-minute snapshot. On restart, the bot reloads all state — open positions, entry prices, trailing stops, TP1 status — so nothing is lost across restarts.

---

## 15. Configuration Reference

All values in `config.py`.

### Signal Thresholds

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `RSI_BUY_THRESHOLD` | 55 | RSI below this = buy signal (enter near neutral, more frequent) |
| `RSI_SELL_THRESHOLD` | 70 | RSI above this = sell signal (overbought) |
| `FEAR_GREED_BUY_MAX` | 65 | F&G at or below this = buy zone (trade through moderate optimism) |
| `VOLUME_SPIKE_MULTIPLIER` | 1.5× | Volume must be 1.5× average to count as spike |
| `CONFIDENCE_BUY_THRESHOLD` | 3 | Base buy_score required to buy |

### Stop-Loss & Take-Profit

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `STOP_LOSS_PCT` | 1.0% | Hard stop — exit if price drops 1% from entry |
| `ATR_TRAILING_MULTIPLIER` | 2.0× | Trailing stop = entry − ATR × 2.0 |
| `ATR_TP_MULTIPLIER` | 2.5× | ATR take-profit = entry + ATR × 2.5 |
| `ATR_TRAIL_ACTIVATION_PCT` | 0.8% | Trailing stop only activates after +0.8% gain |
| `ATR_MAX_TRAIL_PCT` | 2.0% | Max trail distance once activated |

### Scalping Parameters

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `SCALP_MICRO_TP_PCT` | 0.3% | Micro TP — full exit unless BOTH volume AND MACD bullish |
| `SCALP_SOFT_TP_PCT` | 0.5% | Soft TP if neither MACD nor volume alive |
| `SCALP_HARD_TP_PCT` | 1.0% | Hard take-profit ceiling |
| `SCALP_SMALL_PROFIT_LOW` | 0.2% | Small profit lock — lower bound |
| `SCALP_SMALL_PROFIT_HIGH` | 0.4% | Small profit lock — upper bound |
| `SCALP_EARLY_STAG_MINUTES_T1` | 5 min | Stagnation Tier 1: kill at 5min if P/L < 0.1% |
| `SCALP_EARLY_STAG_MIN_PCT_T1` | 0.10% | Stagnation Tier 1: minimum P/L threshold |
| `SCALP_EARLY_STAG_MINUTES` | 8 min | Stagnation Tier 2: kill at 8min if P/L < 0.15% |
| `SCALP_EARLY_STAG_MIN_PCT` | 0.15% | Stagnation Tier 2: minimum P/L threshold |
| `SCALP_SOFT_HOLD_MINUTES` | 12 min | Exit if P/L < 0.3% after 12 minutes |
| `SCALP_HARD_HOLD_MINUTES` | 20 min | Absolute max hold time |
| `SCALP_STAGNATION_MIN_PCT` | 0.3% | Required P/L at soft hold time |
| `MAX_CONCURRENT_TRADES` | 3 | Max simultaneous open positions |
| `BASE_TP1_PCT` | 0.3% | Micro TP threshold (previously partial TP1 at 0.75%) |
| `TP1_SELL_RATIO` | 100% | Full exit at micro TP (no partial sells) |
| `TRAILING_ACTIVATION_PCT` | 0.5% | Activate trailing stop after +0.5% gain |
| `TRAILING_DISTANCE_PCT` | 0.15% | Trailing distance below current price |
| `MIN_TRADE_NOTIONAL` | $20 | Hard floor — no orders below $20 USDT |

### Portfolio

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `PORTFOLIO_RESERVE_PCT` | 50% | Fraction of portfolio never deployed |
| `TRADE_SIZE_PCT` | 3% | Trade size as % of total portfolio |
| `MAX_POSITION_PCT` | 10% | Max allocation per coin |
| `DRAWDOWN_TRIGGER_PCT` | 5.0% | Drawdown % from peak that triggers risk reduction |
| `DRAWDOWN_THRESHOLD_BOOST` | +0.5 | Extra score required during drawdown |
| `DRAWDOWN_SIZE_REDUCTION` | 0.5× | Half position sizes during drawdown |

### BTC Crash Filter

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `BTC_CRASH_PCT` | −2.0% | 15m change below this triggers crash mode |
| `BTC_CRASH_THRESHOLD_BOOST` | +1.0 | Extra score required during crash |
| `BTC_CRASH_SIZE_MULTIPLIER` | 0.25× | 25% position size during crash |
| `BTC_CRASH_COOLDOWN` | 900s | Stay in risk-off for 15 min after crash |

### Adaptive Weights

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `SIGNAL_WEIGHT_MIN` | 0.5 | Floor weight for any signal |
| `SIGNAL_WEIGHT_MAX` | 1.5 | Ceiling weight for any signal |
| `SIGNAL_WEIGHT_WINDOW` | 300 trades | Rolling learning window |
| `SIGNAL_WEIGHT_MIN_TRADES` | 20 | Min trades before weights diverge from 1.0 |
| `SIGNAL_WEIGHT_DECAY` | 0.95 | Exponential decay per trade (recent = more important) |
| `SIGNAL_WEIGHT_NORMALIZE_RATE` | 2% | Per-cycle decay toward 1.0 |

### Trade Cooldown

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `TRADE_COOLDOWN_SECONDS` | 300s | Base cooldown between trades on same asset |
| `TRADE_COOLDOWN_TRENDING` | 180s | Shorter cooldown in trending/trending_volatile regime |
| `COOLDOWN_BYPASS_CONFIDENCE` | 5.0 | Bypass cooldown if `effective_score ≥ threshold + 5.0` AND regime in `COOLDOWN_BYPASS_REGIMES` |
| `COOLDOWN_BYPASS_REGIMES` | trending, trending_volatile | Regimes where cooldown bypass is permitted |

### Entry Path Tuning

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `WICK_RATIO_THRESHOLD` | 0.6 | Upper wick fraction above this = exhaustion penalty (−1.0 buy_score) |
| `MIN_EDGE_PCT` | 0.3% | ATR% below this = insufficient edge penalty (−0.8 buy_score) |
| `SPREAD_MAX_PCT` | 0.15% | Max bid/ask spread — wider = execution blocker (live only) |
| `SPIKE_FILTER_PCT` | 0.3% | Max single-tick price move — larger = execution blocker |
| `NO_TRADE_ATR_PCT` | 0.003 | ATR% floor for dead-market gate (both must fail simultaneously) |
| `NO_TRADE_TREND_PCT` | 0.001 | Trend strength floor for dead-market gate |

### Orderbook / Flow Signals

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `OB_IMBALANCE_BULL` | 0.6 | Imbalance above this = bullish (buyers stacking) |
| `OB_IMBALANCE_BEAR` | 0.4 | Imbalance below this = bearish gate / exit trigger |
| `OB_FLOW_BULL` | 0.6 | Flow ratio above this = bullish (buyers lifting asks) |
| `OB_FLOW_BEAR` | 0.4 | Flow ratio below this = bearish gate / exit trigger |
| `OB_SCORE_BOOST` | 0.5 | Score added per bullish microstructure signal (max +1.0) |
| `OB_EMA_ALPHA` | 0.3 | EMA smoothing alpha for imbalance and flow (~5-tick period) |

### FVG Entry Controls

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `FVG_MIN_GAP_PCT` | 0.1% | Minimum gap size — smaller gaps are noise |
| `FVG_MAX_AGE_SECONDS` | 300s | Discard gaps older than one 5m candle |
| `FVG_FAST_SCORE_MIN` | 2.0 | Minimum fast_score required for any FVG entry |
| `FVG_OB_MIN` | 0.5 | Minimum orderbook imbalance required for FVG entry |
| `FVG_EARLY_TP_PCT` | 0.25% | Accelerated take-profit for FVG-entry positions |
| `FVG_COOLDOWN_SECONDS` | 300s | Minimum time between FVG trades on same pair |
| `FVG_ORDER_TIMEOUT` | 60s | Cancel unfilled FVG limit order after this long |

### Polling Intervals

| Signal | Interval |
|--------|---------|
| RSI / MACD / BB / ATR / Regime | 10s |
| CoinGecko volume | 60s |
| FVG detection | 60s |
| Whale Alert | 120s |
| Etherscan gas | 300s |
| Portfolio rebalance + weight update | 300s |
| Google Trends | 900s |
| Fear & Greed | 3600s |
| Discord panel refresh | 10s |
| Portfolio snapshot (DB) | 1800s |

---

## Appendix: Decision Flow Summary

```
Every price tick (WebSocket):
│
├─ HOLDING? ──────────────────────────────────────────────────────────────────┐
│                                                                               │
│  [bot.py — _get_forced_exit_reason]                                          │
│   1.  HARD_STOP      price ≤ hard_stop_price → SELL                         │
│   2.  TRAIL_STOP     price ≤ trailing_stop → SELL                           │
│   3.  TIME_STOP      open ≥ hard_exit_min AND gain ≤ 0.15% → SELL          │
│   4.  MAX_HOLD_EXIT  open ≥ hard_exit_min × 1.5 → SELL                     │
│                                                                               │
│  [bot.py — TP1 check]                                                        │
│   5.  TP1_FULL_EXIT  gain ≥ tp1_pct → SELL (bypasses signal guard)          │
│                                                                               │
│  [strategy.py — signal exits, tagged sell_reason = "signal"]                 │
│   6.  RSI > 70 → SELL                                                        │
│   7.  MACD crossover == "bearish" → SELL                                     │
│   8.  orderbook_imbalance < 0.4 → SELL                                       │
│       (all three gated by _should_allow_signal_sell unless bypassed)         │
│                                                                               │
│  [strategy.py — scoring deterioration]                                       │
│   9.  EARLY_FAILURE  raw_effective < threshold − 1.5 → SELL (bypasses guard)│
│                                                                               │
│   (none triggered) → fall through to BUY scoring                             │
└──────────────────────────────────────────────────────────────────────────────┘
│
├─ SCORING (strategy.py — all pairs whether holding or not):
│
│   raw_effective = confidence + structure_score + volatility_score
│
│   buy_score = raw_effective
│             − 1.0  if wick_ratio > 0.6        (exhaustion)
│             − 0.8  if ATR% < 0.3%             (min edge not met)
│             − 1.0  if RR < 1.0                (bad risk/reward)
│             − 0.5  if 1.0 ≤ RR < 1.20        (marginal R/R)
│             − 1.0  if trend == "downtrend"    (counter-trend penalty)
│             + 0.3  if trend == "uptrend"      (dip with trend boost)
│             − 0.3  if regime == "choppy_volatile"
│
│   HARD blocks (strategy layer):
│   - Dead market: ATR% < 0.3% AND trend_strength < 0.1% → HOLD
│   - Concurrent cap: open_positions ≥ 3 (NOT holding) → HOLD
│
│   if buy_score ≥ threshold → BUY
│   else → HOLD
│
├─ EXECUTION (bot.py — get_entry_block_reason, only if BUY):
│
│   1. add_blocked       holding + price below entry → skip
│   2. cooldown          time since last trade < limit → skip
│                        (bypass if eff ≥ threshold+5.0 AND trending regime)
│   3. spike_filter      tick move > 0.3% → skip
│   4. spread_filter     spread > 0.15% (live only) → skip
│   5. insufficient_spendable  usdt_balance − reserved ≤ $1 → skip
│   6. position_maxed    position ≥ 10% portfolio → skip
│   7. min_notional      trade size < $20 → skip
│   8. fvg_cooldown      FVG trade within 300s → skip
│
│   (no blocker) → place BUY order
```
