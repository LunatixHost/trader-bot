# Trading Bot — Complete Documentation

> Last updated: 2026-03-28 (rev 7)
> Mode: **Scalping + Long-Term + Futures (Live)** | Exchange: **Binance USDⓈ-M Futures** | Pairs: ETH, BTC, BNB, XRP, BCH, SUI

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Signal Sources](#2-signal-sources)
3. [Regime Detection](#3-regime-detection)
4. [Confidence Scoring](#4-confidence-scoring)
5. [Effective Score](#5-effective-score)
6. [Entry Filters (BUY Gates)](#6-entry-filters-buy-gates)
7. [Exit System (SELL Logic)](#7-exit-system-sell-logic)
8. [Position Sizing](#8-position-sizing)
9. [Adaptive Signal Weights](#9-adaptive-signal-weights)
10. [Portfolio Management](#10-portfolio-management)
11. [BTC Crash Filter](#11-btc-crash-filter)
12. [Correlation System](#12-correlation-system)
13. [Market Dynamics Engine](#13-market-dynamics-engine)
14. [Long-Term Portfolio Layer](#14-long-term-portfolio-layer)
15. [Live Futures Execution Layer](#15-live-futures-execution-layer)
16. [Futures Paper Trading Layer (Simulation)](#16-futures-paper-trading-layer-simulation)
17. [Discord Control Panel](#17-discord-control-panel)
18. [Web Dashboard](#18-web-dashboard)
19. [Database & Logging](#19-database--logging)
20. [Configuration Reference](#20-configuration-reference)

---

## 1. Architecture Overview

The bot runs as a **single Python asyncio process**. All components — signal polling, trading loop, Discord bot, web API, futures paper layer — run concurrently as async tasks without threads blocking each other.

### Startup Sequence

```
1.  Load .env  →  validate Binance + Discord API keys
2.  Connect to Binance WebSocket/REST (python-binance)
3.  Enforce futures pre-flight via CCXT:
      · enforce_futures_environment() — set One-Way Mode (dualSidePosition=false)
4.  Load saved state from trades.db (or create fresh)
5.  Fetch futures USDT wallet balance (CCXT — futures wallet only, no coin balances)
6.  Fetch initial prices for all pairs (python-binance REST)
7.  Open WebSocket symbol-ticker stream per pair   (price ticks → execution queue)
8.  Open WebSocket kline-close stream per pair     (candle close → indicator recalc)
9.  Init Market Dynamics Engine (DataProvider cache + VolumePairList)
10. Start signal polling tasks per pair (staggered 5s apart to spread rate limits)
11. Start portfolio management tasks
12. Start event-driven execution worker
13. Start kline-close indicator worker (Phase 5)
14. Start long-term portfolio loop (if enabled)
15. Start futures paper trading loop (if enabled)
16. Start Market Dynamics refresh loop
17. Launch Discord bot
18. Start FastAPI web dashboard server
```

### Event-Driven Execution Model

The bot does **not** poll on a timer. Trade evaluation is driven by WebSocket events:

```
symbol_ticker WebSocket tick
        │
        ▼
BinanceClient._price_stream_loop()
  → ps.current_price = new_price
  → if pair not already in queue:
        price_event_queue.put(pair)
        │
        ▼
real_time_execution_worker()  (runs continuously, drains queue)
  → evaluate_and_trade(pair)
  → update_portfolio_tracking()
```

**Indicator recalc at candle close (Phase 5):**
```
kline WebSocket stream  →  kline['x'] == True (candle officially closed)
        │
        ▼
kline_close_queue.put(pair)
        │
        ▼
poll_rsi_on_close()  (dedicated worker, drains queue)
  → fetch_technical_indicators()   ← full recalc at T+0 milliseconds
  → update all ps indicator fields
```

The 10-second REST polling loop (`poll_rsi`) remains active as a fallback. Both paths write identical state fields; neither conflicts.

### Key Files

| File | Purpose |
|------|---------|
| `bot.py` | Main entry point, event loop, signal pollers, futures order execution |
| `futures_execution.py` | **CCXT USDⓈ-M order engine** — entry/exit/sizing/One-Way/isolated margin |
| `strategy.py` | Decision engine: confidence scoring, all BUY/SELL logic |
| `config.py` | All tuneable parameters — single source of truth |
| `state.py` | `BotState`, `PairState` (+ `position_side`, `leverage`), `FuturesPaperState`, `LongTermState` |
| `regime.py` | Regime classification (ATR + EMA spread) |
| `portfolio.py` | Portfolio value tracking, reserve management |
| `signal_weights.py` | Adaptive weight learning from trade history |
| `market_data.py` | DataProvider (kline cache) + VolumePairList (dynamic pairlist) |
| `futures_paper.py` | Simulated futures SHORT/LONG paper positions (separate from live layer) |
| `backtest_engine.py` | Offline historical backtesting engine |
| `binance_client.py` | python-binance: WebSocket streams + market-data REST (read-only) |
| `signals/rsi.py` | RSI, MACD, BB, ATR, regime, wick ratio, liquidity sweep, structure_score, volatility_score |
| `signals/fvg.py` | Fair Value Gap detection on 5m candles |
| `signals/orderbook.py` | Orderbook imbalance + trade flow (microstructure) |
| `signals/etherscan.py` | Ethereum gas + network activity (ETH only) |
| `discord_bot/` | Discord panel, buttons, views |
| `api.py` | FastAPI web dashboard server |
| `trades.db` | SQLite database (WAL mode) — all trades, snapshots, state |

---

## 2. Signal Sources

The bot uses **8 active signal sources** across 4 polling tiers. All signals feed into the confidence scoring engine.

### Tier 1 — Every 10 seconds (`INTERVAL_RSI = 10`, `INTERVAL_ORDERBOOK = 10`)

Computed from Binance OHLCV (15m candles) + live orderbook. Kline-close WebSocket also triggers immediate recalc at candle boundary.

| Signal | What it measures | Buy condition |
|--------|-----------------|---------------|
| **RSI** | Relative Strength Index (14-period, Wilder's smoothing) | RSI < 48 (oversold) |
| **MACD** | MACD histogram crossover (12/26/9 EMA) | Bullish crossover (histogram turning positive) |
| **Bollinger Bands** | Price position relative to 20-period BB (2σ) | Price below lower band |
| **ATR** | Average True Range — used for exits and sizing, not confidence scoring | — |
| **structure_score** | Directional price-action composite from candle data | Positive values boost effective score |
| **volatility_score** | BB bandwidth expansion vs 5 candles prior | Positive values boost effective score |
| **Liquidity Sweep** | Wick below recent swing low, scaled by ATR depth | Sweep score contributes to effective score |
| **Orderbook Imbalance** | bid_vol / (bid + ask), top 5 levels, EMA-smoothed (α=0.3) | Imbalance > 0.6 = buyers stacking |
| **Trade Flow Ratio** | buyer-aggressor volume / total, last 50 trades, EMA-smoothed | Flow ratio > 0.6 = buyers lifting asks |

#### structure_score Calculation

Computed in `signals/rsi.py` from the last closed candle. Range approximately −0.8 to +1.3.

```
is_bullish = last_close > last_open

vol_score:
  if bullish:  min(0.6, max(0.0, (vol_ratio − 1.0) × 0.4))   ← 0 to +0.6
  if bearish:  min(0.0, max(−0.3, (vol_ratio − 1.0) × −0.2)) ← −0.3 to 0

body_score   = max(−0.5, min(0.5, body_ratio × 0.6))
close_score  = (close_position_in_range − 0.5) × 0.4

structure_score = vol_score + body_score + close_score
```

Directional by design: high-volume bearish candles produce negative values; high-volume bullish candles produce positive values. This prevents crash panic-selling from being mis-scored as a bullish signal.

#### volatility_score Calculation

```
bw_now = (bb_upper − bb_lower) / bb_middle
expansion = (bw_now − bw_5_candles_ago) / bw_5_candles_ago

volatility_score = max(−0.3, min(0.5, expansion × 5.0))
```

Positive when BB bandwidth is expanding (directional move in progress). Negative when contracting.

### Tier 2 — Every 60 seconds (`INTERVAL_COINGECKO = 60`)

| Signal | What it measures | Buy condition |
|--------|-----------------|---------------|
| **CoinGecko Volume** | 24h volume vs rolling average | Volume > 1.5× average = spike |

### Tier 3 — Every 2–15 minutes

| Signal | Interval | Pairs | Buy condition |
|--------|----------|-------|---------------|
| **Whale Alert** | 120s | ETH, BTC, BNB, XRP | `whale_signal == "neutral"` or `"accumulation"` |
| **Etherscan Gas** | 300s | ETH only | High network activity |
| **Google Trends** | 900s | All (coin-specific keyword) | Trend score > 50 |

### Tier 4 — Every hour (`INTERVAL_FEAR_GREED = 3600`)

| Signal | What it measures | Buy condition |
|--------|-----------------|---------------|
| **Fear & Greed Index** | Market-wide sentiment (0=Extreme Fear, 100=Extreme Greed) | Score ≤ 65 |

Fear & Greed is **shared across all pairs** — one fetch covers all six coins.

### FVG — Every 60 seconds (`INTERVAL_FVG = 60`)

**Fair Value Gap** detection on 5m candles. Scans up to 6 recent candle triplets. A bullish FVG exists when:
```
Candle[i-2].high  < Candle[i].low        (gap between them)
Candle[i-1] = strong impulse candle       (body/range ≥ 50%)
gap_size ≥ FVG_MIN_GAP_PCT (0.1%) of price
current_price ≤ gap_top × 1.005          (reachable — not already blown past)
```

Entry price = midpoint = `(gap_top + gap_bottom) / 2`

FVG adds **+0.5** to the effective score when active.

**Validation gates (all must pass before an FVG order is placed):**
- Gap size ≥ 0.1% of current price
- Gap age ≤ 300s
- `fast_score ≥ 2.0`
- `orderbook_imbalance ≥ 0.5`
- FVG cooldown: 5 minutes between FVG-driven trades on same pair

**Two entry paths:**

| Path | Condition | Order type |
|------|-----------|------------|
| Standard | `current_price > fvg_mid` | Limit buy at `fvg_mid` |
| Early entry | `fvg_bottom < current_price ≤ fvg_mid` | Market buy immediately |

Limit orders cancelled if unfilled after 60s (`FVG_ORDER_TIMEOUT`).

**FVG-specific exits:**
- **FVG failure**: price drops below `fvg_bottom` → immediate SELL
- **FVG profit accelerator**: gain ≥ 0.25% on FVG entry → immediate SELL

### Signal Applicability Per Pair

| Signal | ETH | BTC | BNB | XRP | BCH | SUI |
|--------|-----|-----|-----|-----|-----|-----|
| RSI / MACD / BB / structure / volatility | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
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
Trend strength = |EMA20 − EMA50| / price

is_volatile  = ATR% > 1.5%
is_trending  = trend_strength > 0.5%

         | Not Trending    | Trending             |
---------|-----------------|----------------------|
Normal   | ranging         | trending             |
Volatile | choppy_volatile | trending_volatile    |
```

### Hysteresis

Regime **must be seen 3 consecutive times** before switching. In extreme volatility (ATR% > 3%), only 2 confirmations needed.

### Trend Direction

`trend_direction` tracks EMA20 vs EMA50 (EMA periods: `REGIME_EMA_FAST = 20`, `REGIME_EMA_SLOW = 50`):

| trend_direction | pair_state.trend |
|-----------------|-----------------|
| `"up"` (EMA20 > EMA50) | `"uptrend"` |
| `"down"` (EMA20 < EMA50) | `"downtrend"` |
| `"flat"` | `"chop"` |

`trend` feeds into scoring penalties, downtrend position sizing, and futures entry gates.

---

## 4. Confidence Scoring

A **weighted, category-capped, regime-adjusted** score. Replaces the old flat signal count.

### Formula

```
confidence = Σ ( signal_value × learned_weight × regime_modifier )
             with per-category caps applied
```

### Signal Categories & Caps

| Category | Signals | Cap |
|----------|---------|-----|
| `mean_reversion` | RSI + Bollinger Bands | 1.5 |
| `momentum` | MACD + Google Trends | 1.5 |
| `sentiment` | CoinGecko Volume | 1.2 |
| `onchain` | Whale Alert + Etherscan Gas | 1.0 |
| `macro` | Fear & Greed | 1.0 |

### Regime Modifiers

| Category | trending | ranging | choppy_volatile | trending_volatile |
|----------|----------|---------|-----------------|-------------------|
| mean_reversion | 0.6× | 1.4× | 0.7× | 0.5× |
| momentum | 1.4× | 0.6× | 0.7× | 1.3× |
| sentiment | 1.0× | 0.8× | 1.3× | 1.2× |
| onchain | 1.0× | 1.0× | 1.2× | 1.0× |
| macro | 1.0× | 1.0× | 1.0× | 0.8× |

### MACD Dampening

MACD's learned weight is additionally halved (`× 0.5`) — it confirms momentum but shouldn't dominate.

---

## 5. Effective Score

The final score used for buy decisions. Stored as `pair_state.effective_score`.

```
raw_effective = confidence + structure_score + volatility_score

buy_score = raw_effective
          − 1.0   if wick_ratio > 60%          (exhaustion / distribution)
          − 0.8   if 0 < ATR% < 0.3%           (minimum edge not met)
          − 1.0   if RR < 1.0                  (reward below risk)
          − 0.5   if 1.0 ≤ RR < 1.20           (marginal R/R)
          − 1.0   if trend == "downtrend"       (counter-trend penalty)
          + 0.3   if trend == "uptrend"         (dip-buying-with-trend boost)
          − 0.3   if regime == "choppy_volatile" (noisy signal environment)
```

`structure_score` is computed from directional candle data (vol ratio, body ratio, close position). `volatility_score` is computed from BB bandwidth expansion. Both are live values from `signals/rsi.py`.

### R/R Calculation

```
expected_reward = max(tp1_pct, ATR% × ATR_TP_MULTIPLIER × 100, 0.35%)
expected_risk   = max(STOP_LOSS_PCT, ATR% × ATR_SL_MULTIPLIER × 100, 0.20%)
RR = expected_reward / expected_risk
```

`STOP_LOSS_PCT` (0.5%) is the floor; `ATR_SL_MULTIPLIER` (1.5×) widens the risk estimate to match the dynamic stop in volatile conditions.

### Dynamic Buy Threshold

```
threshold = dynamic_params["buy_threshold"]   (per-pair, volatility-scaled)
          + DRAWDOWN_THRESHOLD_BOOST (+0.5)   if in_drawdown
          + BTC_CRASH_THRESHOLD_BOOST (+1.0)  if btc_crash_active
```

Per-pair base uses a **volatility factor** (current ATR / 20-period avg ATR), exponentially smoothed, clamped to [0.5×, 1.5×].

Base threshold: **2.0** (`CONFIDENCE_BUY_THRESHOLD`).

---

## 6. Entry Decision System

### Strategy Layer (`strategy.py`)

#### Dead Market (hard block)
```
if ATR% < 0.3% AND trend_strength < 0.1%:
    → HOLD
```

#### Sell Conditions (when holding)
```
RSI > 70                        → SELL (signal)
MACD crossover == "bearish"     → SELL (signal)
orderbook_imbalance < 0.4       → SELL (signal)
```

#### Early Failure Exit
```
if holding AND raw_effective < threshold − 1.5:
    → SELL tagged EARLY_FAILURE  (bypass sell guard)
```

#### Buy Score Soft Penalties

| Condition | Effect |
|-----------|--------|
| `wick_ratio > 0.6` | −1.0 |
| `0 < ATR% < 0.3%` | −0.8 |
| `RR < 1.0` | −1.0 |
| `1.0 ≤ RR < 1.20` | −0.5 |
| `trend == "downtrend"` | −1.0 |
| `trend == "uptrend"` | +0.3 |
| `regime == "choppy_volatile"` | −0.3 |

#### Concurrent Cap (hard block)
```
if NOT holding AND open_positions >= 3:
    → HOLD
```

#### BUY Decision
```
if buy_score >= threshold → BUY
else → HOLD
```

### Execution Layer (`bot.py` — `get_entry_block_reason`)

Secondary gatekeeping after strategy approves BUY. Checks in priority order:

| # | Blocker | Condition |
|---|---------|-----------|
| 1 | `add_blocked` | Holding + `current_price < buy_price` (no averaging into loss) |
| 2 | `cooldown` | Last trade < 300s ago (180s in trending regime); bypass at `eff ≥ threshold + 5.0` |
| 3 | `failed_exit_cooldown` | Recent forced-exit on this pair within cooldown window |
| 4 | `spike_filter` | Single tick > 0.3% move |
| 5 | `unstable_candle` | Last candle range > 2.5× ATR (abnormally large candle) |
| 6 | `spread_filter` | Bid/ask spread > 0.15% (live only — testnet bypassed) |
| 7 | `insufficient_spendable` | `usdt_balance − usdt_reserved ≤ $1` |
| 8 | `position_maxed` | Position ≥ `portfolio × MAX_POSITION_PCT (10%)` |
| 9 | `min_notional` | Computed trade size < `MIN_TRADE_NOTIONAL ($20)` |
| 10 | `fvg_cooldown` | FVG trade within last 300s on same pair |

### Signal Sell Guard (`_should_allow_signal_sell`)

Prevents premature exits from weak sell signals while winners run:

```
Allow SELL if any of:
  1. pnl_pct >= 0.40%                          (at or past TP1 level)
  2. peak_gain >= 0.50% AND pnl_pct > 0         (pulled back from peak but still green)
  3. hold_minutes >= hard_exit_minutes AND green (stale trade)
  4. pnl_pct > 0.40% AND eff < threshold − 1.0  (green + collapsed signal)
```

---

## 7. Exit System (SELL Logic)

```
Every price tick while holding:
1. _get_forced_exit_reason()    ← price/time stops — always execute
2. TP1 check                    ← partial profit target — always execute
3. strategy.get_decision() SELL ← signal-based — filtered by _should_allow_signal_sell
```

### Forced Exit Checks

#### 1. Hard Stop-Loss (Dynamic — Triple Barrier Lower Barrier)

```
atr_sl_pct     = ps.atr_pct × ATR_SL_MULTIPLIER (1.5) × 100
effective_sl   = min(ATR_SL_MAX_PCT (2.0%), max(STOP_LOSS_PCT (0.5%), atr_sl_pct))
hard_stop_price = entry × (1 − effective_sl / 100)

if current_price <= hard_stop_price:
    → HARD_STOP
```

The stop-loss widens with ATR to prevent whipsaw in volatile markets, with a floor of 0.5% and a ceiling of 2.0%.

| ATR | Effective SL |
|-----|-------------|
| < 0.33% | 0.50% (floor) |
| 0.50% | 0.75% |
| 0.80% | 1.20% |
| ≥ 1.33% | 2.00% (cap) |

#### 2. ATR Trailing Stop

```
if trailing_stop > 0 AND current_price <= trailing_stop:
    → TRAIL_STOP
```

**Pre-TP1:** Activates after `ATR_TRAIL_ACTIVATION_PCT` (0.25%) gain.
Trail = `entry − ATR × ATR_TRAILING_MULTIPLIER (1.5×)`, capped at `ATR_MAX_TRAIL_PCT` (2%) below price.

**Post-TP1:** Activates after `TRAILING_ACTIVATION_PCT` (0.6%) gain.
Tight fixed-distance trail: `current_price × (1 − TRAILING_DISTANCE_PCT / 100)` (0.25% below).
Ratchets up only — never loosens.

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

`hard_exit_minutes` is derived from `HARD_EXIT_MINUTES_BASE` (20 min), adjusted per regime.

### TP1 Partial Exit

Checked after forced exits. If `pnl_pct >= tp1_pct` (`BASE_TP1_PCT = 0.6%`):
- Sells **50%** of position (`TP1_SELL_RATIO = 0.5`)
- Moves stop-loss to breakeven + `BREAKEVEN_BUFFER_PCT` (0.20%)
- Activates the tight post-TP1 trailing stop

### Strategy Signal Exits

| sell_reason | Execution |
|-------------|-----------|
| `"signal"` (RSI/MACD/OB) | Gated by `_should_allow_signal_sell` |
| `"early_failure"` | Bypasses guard — always executes as `EARLY_FAILURE` |

### Exit Action Taxonomy

| Action | Description |
|--------|-------------|
| `SIGNAL_SELL` | RSI/MACD/OB sell passed the sell guard |
| `EARLY_FAILURE` | Lost signal conviction (`effective < threshold − 1.5`) |
| `HARD_STOP` | Dynamic stop price hit |
| `TRAIL_STOP` | Trailing stop triggered |
| `TIME_STOP` | Hold time limit with insufficient gain |
| `MAX_HOLD_EXIT` | Absolute maximum hold time exceeded |
| `TP1_FULL_EXIT` | TP1 partial triggered (50% close) |
| `FORCE SELL` | Discord Abandon button |
| `EARLY_STAGNATION` | Early stagnation exit (position showed no progress) |

---

## 8. Position Sizing

### Base Size Formula

```
trade_size = portfolio_total_usdt × TRADE_SIZE_PCT (3%)
           × size_modifier           (regime-based, from signal_weights)
           × correlation_modifier    (0.5× if correlated coin already held)
           × btc_cluster_modifier    (1.0 currently — hook for future use)
           × crash_modifier          (0.25× if BTC crash active)
           × downtrend_modifier      (0.8× if trend == "downtrend")
           × vol_adj                 (Phase 4: inverse volatility, see below)
```

### Phase 4 — Inverse Volatility Sizing (Risk Parity)

Making the stop-loss dynamic (0.5%–2.0%) would create variable dollar risk per trade if position size were fixed. The inverse-volatility adjustment normalises this:

```
atr_sl_pct = min(ATR_SL_MAX_PCT, max(STOP_LOSS_PCT, ps.atr_pct × ATR_SL_MULTIPLIER × 100))
vol_adj    = STOP_LOSS_PCT / atr_sl_pct     (always ≤ 1.0)
trade_size × vol_adj
```

**Effect:** Dollar loss per losing trade stays constant regardless of market regime.

| ATR | SL | vol_adj | $30 base → |
|-----|----|---------|------------|
| 0.3% | 0.50% (floor) | 1.00 | $30.00 |
| 0.5% | 0.75% | 0.67 | $20.00 |
| 0.8% | 1.20% | 0.42 | $12.50 |
| 1.3%+ | 2.00% (cap) | 0.25 | $7.50 |

Capped by `MAX_POSITION_PCT` (10% of portfolio) and floored by `MIN_TRADE_NOTIONAL` ($20).

---

## 9. Adaptive Signal Weights

After **20+ completed trades**, the bot learns which signals predict profitable outcomes.

### Learning Algorithm

Every 5 minutes, the bot:
1. Loads the last 300 completed SELL trades
2. For each signal, checks whether it was active at entry
3. Scores each trade:

| Outcome | Score |
|---------|-------|
| Strong win (> threshold) | +1.0 |
| Weak win | +0.3 |
| Scratch (~0%) | 0.0 |
| Loss | −1.0 |

4. Applies **exponential decay** (`0.95^i`) — recent trades matter more
5. Maps average outcome to weight in `[0.5, 1.5]`

### Stability Controls

| Control | Value |
|---------|-------|
| Inertia blend | 70% old + 30% new |
| Max delta per cycle | ±0.2 |
| Min trades before diverging | 20 |
| Normalization decay | 2% toward 1.0 per cycle |

### Drawdown Mode

During portfolio drawdown: winners-only mode, inertia 90/10, minimum 5 winning trades required.

---

## 10. Portfolio Management

### Capital Allocation

| Parameter | Value | Meaning |
|-----------|-------|---------|
| Portfolio reserve | 50% | Half of capital never deployed |
| Trade size | 3% per trade | Each buy = 3% of total portfolio (before vol_adj) |
| Max position | 10% per coin | No single coin > 10% of portfolio |
| Max concurrent trades | 3 | At most 3 open positions |

### Drawdown Mode

```
if portfolio_current_value < portfolio_peak_value × (1 − 5%):
    in_drawdown = True
    → buy threshold += 0.5
    → trade size × 0.5
    → signal weight learner: winners-only mode
```

Auto-deactivates when portfolio recovers above trigger.

### Rebalancing

Every 5 minutes: recalculates `usdt_reserved`, `trade_amount_usdt`, updates `portfolio_current_value` from live prices.

---

## 11. BTC Crash Filter

```
if BTC 15m price change < −2.0%:
    btc_crash_active = True  (lasts 15 minutes)
    → buy threshold += 1.0 for ALL pairs
    → trade size × 0.25 for ALL pairs
```

After `BTC_CRASH_COOLDOWN` (900s), normal mode resumes automatically.

---

## 12. Correlation System

### Correlation Groups

```python
{"BTC", "ETH", "BCH"}   # Bitcoin ecosystem
{"BNB", "SUI"}           # Binance ecosystem
```

When entering a correlated asset already moving in the same direction as an existing position in the same group:
```
trade_size × 0.5
```

---

## 13. Market Dynamics Engine

`market_data.py` provides two components that all signal pollers share.

### DataProvider — Kline Cache

A 60-second TTL cache for kline (OHLCV) data, keyed by `"{pair}_{interval}"`.

- `fetch_technical_indicators()` and `detect_fvg()` both accept an optional `data_provider` argument
- When provided, klines are served from cache; only one REST call is made per pair per 60s regardless of how many tasks poll simultaneously
- On network failure, returns the last valid cached data (stale-on-error)
- Warm-up: on startup, cache is pre-populated for all pairs before the main loop begins

### VolumePairList

Ranks all USDT pairs by 24-hour quote volume and keeps the top-N most liquid. Refreshes every 5 minutes via `market_dynamics_loop()`.

- Uses `get_ticker()` endpoint (single REST call for all pairs)
- Sorted by `quoteVolume` descending
- Ensures the bot always trades the highest-liquidity USDT pairs

### Module-level Singletons

```python
data_provider   # DataProvider instance, wired into all indicator fetches
volume_pairlist # VolumePairList instance, updated by market_dynamics_loop
```

---

## 14. Long-Term Portfolio Layer

A separate "macro hold" strategy running in `long_term_loop()` alongside the scalper. Completely independent of scalp state.

- **Separate capital**: `LongTermState` per pair holds quantity, entry price, and position state distinct from `PairState`
- **Entry**: requires sustained trend and effective score above `LONG_TERM_ENTRY_THRESHOLD`
- **No TP1 / no trailing / no stagnation exits** — holds through noise
- **Exit**: sustained downtrend (`consecutive_downtrend_cycles` counter) or effective score collapse
- **Triple-long protection**: `FUTURES_TRIPLE_LONG_BLOCK = True` — if spot scalper AND long-term both hold a coin AND futures paper holds a LONG on the same coin, the futures LONG is blocked
- **Enabled**: `LONG_TERM_ENABLED` in config

---

## 15. Live Futures Execution Layer

`futures_execution.py` — the CCXT-based USDⓈ-M real-order engine. Replaces the legacy spot order calls (`place_market_buy` / `place_market_sell`) with proper perpetual futures routing.

### Architecture Split

| Concern | Library | File |
|---------|---------|------|
| Price WebSocket streams | python-binance | `binance_client.py` |
| Kline-close streams | python-binance | `binance_client.py` |
| Orderbook / trade-flow REST | python-binance | `binance_client.py` |
| OHLCV klines for indicators | python-binance | `binance_client.py` |
| **All order execution** | **CCXT** | **`futures_execution.py`** |
| **Balance queries** | **CCXT** | **`futures_execution.py`** |

### Key Safety Invariants

| Invariant | Implementation |
|-----------|---------------|
| **One-Way Mode** | `enforce_futures_environment()` at startup — `dualSidePosition=false` |
| **Isolated Margin** | `prepare_market_for_entry()` before every entry — no cross-margin |
| **Fixed Leverage** | `prepare_market_for_entry()` sets `FUTURES_LEVERAGE` before every entry |
| **reduceOnly exits** | `execute_futures_exit()` always passes `params={'reduceOnly': True}` |
| **USDT-only balance** | `get_futures_usdt_balance()` — never queries base-asset spot wallets |

### Order Flow

```
BUY signal
    │
    ├─ calculate_futures_position_size(usdt_balance, entry_price, sl_price)
    │    = (balance × FUTURES_ACCOUNT_RISK_PCT) / sl_distance_pct / entry_price
    │
    ├─ prepare_market_for_entry(symbol, FUTURES_LEVERAGE)
    │    · set_margin_mode('isolated', symbol)
    │    · set_leverage(5, symbol)
    │
    └─ create_order(type='market', side='buy', amount=contracts)
         ps.position_side = "long"

SELL/exit signal
    │
    ├─ execute_futures_exit(symbol, position_side='long', contracts)
    │
    └─ create_order(type='market', side='sell', amount=contracts,
                    params={'reduceOnly': True})   ← CRITICAL
         ps.position_side = "none"
```

### Position Sizing (Futures Risk Parity)

```
sl_distance_pct = |entry_price - sl_price| / entry_price
max_dollar_loss = usdt_balance × FUTURES_ACCOUNT_RISK_PCT   (default 1%)
notional        = max_dollar_loss / sl_distance_pct
notional        = min(notional, usdt_balance × leverage × 0.95)
contracts       = notional / entry_price
```

**Example** (ETH @ $3,000, ATR-SL = 0.75%, 1% risk, 5x leverage, $1,000 balance):
```
sl_distance = 0.75%  →  max_loss = $10  →  notional = $1,333  →  contracts = 0.444
margin_locked = $1,333 / 5 = $267   (26.7% of balance)
```

### Fee Constants (USDⓈ-M Perpetuals)

| Constant | Value | Description |
|----------|-------|-------------|
| `FUTURES_MAKER_FEE` | 0.0002 (0.02%) | Limit orders — used for fee logging |
| `FUTURES_TAKER_FEE` | 0.0005 (0.05%) | Market orders — used for fee logging |
| `ROUND_TRIP_COST_PCT` | 0.15% | Taker round-trip cost (2 × 0.05% + slippage) |

### Testnet Note

Futures testnet API keys are **separate** from spot testnet keys.
Generate them at: https://testnet.binancefuture.com (GitHub login — no Binance account needed).
The futures testnet comes pre-loaded with ~10,000 USDT.

Set in `.env`:
```
BINANCE_FUTURES_API_KEY=your_futures_testnet_key
BINANCE_FUTURES_SECRET_KEY=your_futures_testnet_secret
```

`futures_execution.py` reads `BINANCE_FUTURES_API_KEY` / `BINANCE_FUTURES_SECRET_KEY` exclusively.
`binance_client.py` (WebSocket + REST market data) continues to use `BINANCE_API_KEY` / `BINANCE_SECRET_KEY` (spot testnet or live spot).

---

## 16. Futures Paper Trading Layer (Simulation)

`futures_paper.py` adds **fully simulated** SHORT and LONG positions. No real Binance futures orders are placed. Completely isolated from live futures P/L.

### Overview

| Feature | Value |
|---------|-------|
| Leverage | 2× (simulated) |
| Position size | Up to $20 notional (ATR-sized) |
| Max concurrent | 2 positions |
| Funding fee | 0.01% per 8-hour interval (simulated) |
| Round-trip cost | 0.30% (deducted at exit) |

### When to SHORT vs LONG

| Side | Required | Blocked during |
|------|----------|----------------|
| SHORT | `trend == "downtrend"` | `trending_volatile` regime |
| LONG | `trend == "uptrend"` | `choppy_volatile` regime |

### Bearish Score for SHORT Entry

```
score = 0.0
+ min(abs(structure_score), 1.5)    if structure_score < 0   (bearish PA)
+ 0.8                               if RSI > RSI_SELL_THRESHOLD (overbought)
+ 0.6                               if macd_crossover == "bearish"
+ 0.3                               if trend == "downtrend"     (confirmation bonus)
+ 0.5                               if ob < FUTURES_OB_BEAR_MAX (0.45)
+ 0.5                               if flow < FUTURES_FLOW_BEAR_MAX (0.45)
```

Minimum score: `FUTURES_MIN_SCORE = 1.5` (regime-adjusted: 1.2 in ranging, 1.8 in volatile).

Without RSI overbought (typical in sustained downtrends where RSI stays 35–55), a realistic confluence:
```
structure_score (−0.5) → +0.5
trend confirmation      → +0.3
ob = 0.43               → +0.5
flow = 0.43             → +0.5
total = 1.8  ✓          (above 1.5 threshold)
```

### Entry Gate (all must pass for SHORT)

1. `FUTURES_PAPER_ENABLED` and `FUTURES_SHORT_ENABLED`
2. No open position on this symbol
3. ATR move expected ≥ `FUTURES_MIN_REWARD_PCT` (0.50%)
4. No re-entry cooldown from recent failed exit
5. Last candle not abnormally wide (< 2.5× ATR)
6. `trend == "downtrend"`
7. `regime != "trending_volatile"`
8. `ob < 0.45` AND `flow < 0.45` (both bearish)
9. `macd_histogram < 0.0` (negative momentum)
10. `score ≥ dynamic_min_score`
11. Open positions < `FUTURES_MAX_CONCURRENT_POSITIONS` (2)

### Exits

| Exit type | Condition |
|-----------|-----------|
| Hard stop | `pnl_pct < −FUTURES_HARD_STOP_PCT (0.60%)` |
| Trailing stop | Activated after 1.5% profit; trails 0.8% |
| TP1 partial | `pnl_pct ≥ FUTURES_TP1_PCT (0.50%)` → close 50% |
| Signal reversal | Bullish MACD cross or ob/flow both flip bullish (while in profit) |
| Stagnation T1 | 12h elapsed, still losing > 1.0% |
| Stagnation T2 | 24h elapsed, still losing > 0.5% |
| Time stop | 72h absolute max hold |

### ATR-Based Position Sizing

```
dollar_risk  = portfolio_value × FUTURES_RISK_PCT_PER_TRADE (0.5%)
stop_pct     = atr_pct × FUTURES_ATR_STOP_MULT (2.0)
notional     = max($5, min($20, dollar_risk / stop_pct))
```

### P/L Accounting

```
SHORT pnl_pct = (entry − current) / entry × 100 × leverage
LONG  pnl_pct = (current − entry) / entry × 100 × leverage

net_pl = gross_pl − round_trip_cost (0.30%) − accrued_funding_fees
```

Tracked separately in `state.futures_paper_realized_pnl`, `futures_paper_total_trades`, `futures_paper_winning_trades`.

---

## 17. Discord Control Panel

Auto-refreshes every **45 seconds**.

### Panel Contents

**GROWTH section:** Start value, current value, total P/L (USDT + %), peak value, max drawdown, uptime

**PER COIN section:** Asset balance, entry price, current price, position value, P/L per coin

**HOLDING section:** Full detail per open position — regime, effective score, signal breakdown, quantity, entry, held time, P/L, current stop-loss (dynamic), trailing stop

**WATCHING section:** Idle pairs — regime, `Eff: X.X / Th: X.X`, signal breakdown, OB/Flow, trend.
Status: `READY ✓`, `HOLD (need +X.X)`, `BLOCKED (reason)`, or `READY but BLOCKED (reason)`

**FUTURES PAPER section:** Open futures positions with side, entry, current P/L, trailing stop, funding accrued. Realized P/L, win rate.

**MARKET STATE:** Fear & Greed, BTC crash filter, drawdown mode

**USDT section:** Available balance, reserved, total trades, win rate

### Buttons

| Button | Action |
|--------|--------|
| ⏸ Pause / ▶ Resume | Toggle trading (signals still update) |
| 📊 Signals | Pair selector → detailed signal card |
| 📈 Chart | Pair selector → candlestick chart with trade markers |
| 💼 Portfolio | Full portfolio breakdown |
| 📜 Trades | Last 10 completed trades with P/L |
| ⚙️ Config | Current config values |
| 🗑️ Abandon | Force-sell a position (real market order, or state-clear if dust < $5) |
| 📊 Exit Analytics | Per-action exit breakdown: count, win%, avg P/L, total P/L |

All buttons locked to `DISCORD_OWNER_ID`.

---

## 18. Web Dashboard

FastAPI + uvicorn server embedded in the main bot process at `http://localhost:8081`.

- **TradingView Lightweight Charts v4.1.1** — candlestick + volume bars
- **300 candles** on load, auto-scroll to latest
- **Watchlist** — all pairs with live prices, colour-coded by trend
- **Signal panel** — effective score, individual signal status, regime
- **Portfolio sidebar** — P/L, drawdown, win rate
- Live updates via WebSocket push from bot state

---

## 19. Database & Logging

All trade history in **`trades.db`** (SQLite, **WAL journal mode** — crash-safe writes).

### Tables

| Table | Contents |
|-------|---------|
| `trades` | Every BUY/SELL: pair, price, qty, USDT, RSI, confidence, P/L, regime, ATR, signal snapshot (JSON), outcome_label, hold_minutes |
| `portfolio_snapshots` | Portfolio value + P/L every 30 minutes |
| `bot_state` | Full serialised BotState (id=1, replaced on save) |
| `pair_states` | Per-pair serialised state, keyed by symbol |

### WAL Mode

All connections (`logger.py`, `state.py`, `panel.py`) open with `PRAGMA journal_mode=WAL`. This makes every write atomic — a process crash or VPS reboot mid-write cannot corrupt the database. On restart, `load_state_from_db()` reliably recovers all open positions, entry prices, trailing stops, and TP1 status.

### Exit Action Taxonomy (`_ALL_SELL_ACTIONS`)

```
SELL, STOP_LOSS, SIGNAL_SELL, EARLY_FAILURE, HARD_STOP, TRAIL_STOP,
TIME_STOP, MAX_HOLD_EXIT, TP1_FULL_EXIT, FORCE SELL, EARLY_STAGNATION,
FUTURES_SHORT_ENTRY, FUTURES_LONG_ENTRY, FUTURES_TP1, FUTURES_SIGNAL_EXIT,
FUTURES_TRAIL_STOP, FUTURES_HARD_STOP, FUTURES_EARLY_STAGNATION, FUTURES_TIME_STOP
```

### Signal Snapshots

Every trade stores the full signal state at entry as a JSON blob — used by the adaptive weight learner to correlate signal states with outcomes.

---

## 20. Phase 8 — Execution Audit & Chaos Testing

Validates the live execution pipe, `reduceOnly` integrity, and WAL crash recovery before deploying full Risk Parity sizing.

### 20.1 Audit Mode

| Config | Default | Meaning |
|--------|---------|---------|
| `AUDIT_MODE` | `True` | Overrides Phase 4 sizing — forces every trade to `AUDIT_MICRO_NOTIONAL` |
| `AUDIT_MICRO_NOTIONAL` | `6.0 USDT` | Notional per trade in audit mode (Binance minimum ~5 USDT) |

Set via `.env`:
```
AUDIT_MODE=True    # Phase 8 testing
AUDIT_MODE=False   # Full Risk Parity restored (post-audit)
```

On startup, if `AUDIT_MODE=True`, the bot logs a visible banner:
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ⚠️  AUDIT MODE ACTIVE (Phase 8 Execution Audit)
  Risk Parity DISABLED — all trades forced to $6 USDT notional
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### 20.2 Chaos Monkey — WAL Recovery Test

1. Boot bot in Audit Mode
2. Wait for a 3-minute kline-close WebSocket to trigger an entry
3. Verify on Binance UI: position is **Isolated**, leverage is correct
4. **`kill -9 <pid>`** — hard kill, no graceful shutdown
5. Reboot the bot
6. **Expected log output:**
```
⚡ WAL RECOVERY: 1 open position(s) restored
→ ETHUSDT: LONG 0.003000 contracts @ $2,150.0000  [SL: $2,139.2500]
Bot will resume exit monitoring immediately.
```

Pass criteria: `position_side`, `asset_balance`, and `buy_price` all match pre-kill values. Bot immediately resumes TP/SL monitoring without re-entering.

### 20.3 reduceOnly Stress Test

1. Allow bot to enter a Micro-Notional Long
2. **Manually close the position on the Binance UI**
3. Wait for the bot's TP or SL to trigger
4. Bot sends `reduceOnly=True` sell — Binance returns error `-2022`
5. **Expected log output:**
```
FUTURES EXIT ETHUSDT: reduceOnly rejected — position already closed on exchange (safe to clear local state).
```

Pass criteria: Bot logs info (not error), clears local state cleanly, **does not open a Short**, does not crash.

### 20.4 Graduation Checklist

Before disabling Audit Mode and enabling full Risk Parity:

- [ ] Isolated margin confirmed in Binance UI on every entry
- [ ] Leverage matches `FUTURES_LEVERAGE` config value
- [ ] WAL recovery log matches pre-kill state after `kill -9`
- [ ] `reduceOnly` rejection logged as info, no crash, no reversed position
- [ ] P/L accounting is correct (check EQUITY Discord channel vs Binance UI)

Set `AUDIT_MODE=False` in `.env` to graduate to full institutional sizing.

---

## 21. Configuration Reference

All values in `config.py`. This is the single source of truth — edit here first, then update this document.

### Signal Thresholds

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `RSI_BUY_THRESHOLD` | 48 | RSI below this = buy signal |
| `RSI_SELL_THRESHOLD` | 70 | RSI above this = sell signal |
| `CONFIDENCE_BUY_THRESHOLD` | 2.0 | Base buy_score required to enter |
| `VOLUME_SPIKE_MULTIPLIER` | 1.5× | Volume must be 1.5× average to count as spike |
| `FEAR_GREED_BUY_MAX` | 65 | F&G at or below this = buy zone |

### Stop-Loss & Take-Profit

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `STOP_LOSS_PCT` | 0.5% | Dynamic SL floor — minimum SL regardless of ATR |
| `ATR_SL_MULTIPLIER` | 1.5× | SL widens to `ATR% × 1.5` in volatile markets |
| `ATR_SL_MAX_PCT` | 2.0% | Hard cap on dynamic SL |
| `ATR_TRAILING_MULTIPLIER` | 1.5× | Pre-TP1 ATR trailing stop multiplier |
| `ATR_TP_MULTIPLIER` | 2.0× | ATR take-profit distance for R/R calculation |
| `ATR_TRAIL_ACTIVATION_PCT` | 0.25% | Pre-TP1 trail activates after +0.25% gain |
| `ATR_MAX_TRAIL_PCT` | 2.0% | Max trail distance once activated |
| `BASE_TP1_PCT` | 0.6% | TP1 threshold — sell 50% at +0.6% |
| `TP1_SELL_RATIO` | 0.5 | 50% of position closed at TP1 |
| `BREAKEVEN_BUFFER_PCT` | 0.20% | Stop moved to entry +0.20% after TP1 hits |
| `TRAILING_ACTIVATION_PCT` | 0.6% | Post-TP1 tight trail activates after +0.6% |
| `TRAILING_DISTANCE_PCT` | 0.25% | Post-TP1 trail distance below current price |

### Scalping / Time Exits

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `HARD_EXIT_MINUTES_BASE` | 20 min | Time stop base — exit if gain ≤ 0.15% after 20min |
| `HARD_EXIT_PL_THRESHOLD_PCT` | 0.15% | Minimum P/L required to avoid time stop |
| `SCALP_HARD_TP_PCT` | 1.2% | Hard TP ceiling for strong winners |
| `SCALP_SMALL_PROFIT_LOW` | 0.40% | Small profit lock lower bound |
| `SCALP_SMALL_PROFIT_HIGH` | 0.60% | Small profit lock upper bound |
| `MAX_CONCURRENT_TRADES` | 3 | Max simultaneous open positions |
| `MIN_TRADE_NOTIONAL` | $20 | Hard floor — no orders below $20 USDT |

### Portfolio

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `PORTFOLIO_RESERVE_PCT` | 50% | Fraction of portfolio never deployed |
| `TRADE_SIZE_PCT` | 3% | Base trade size as % of total portfolio |
| `MAX_POSITION_PCT` | 10% | Max allocation per coin |
| `DRAWDOWN_TRIGGER_PCT` | 5.0% | Drawdown % from peak triggers risk reduction |
| `DRAWDOWN_THRESHOLD_BOOST` | +0.5 | Extra score required during drawdown |

### BTC Crash Filter

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `BTC_CRASH_PCT` | −2.0% | 15m change below this triggers crash mode |
| `BTC_CRASH_THRESHOLD_BOOST` | +1.0 | Extra score required during crash |
| `BTC_CRASH_SIZE_MULTIPLIER` | 0.25× | 25% position size during crash |
| `BTC_CRASH_COOLDOWN` | 900s | Risk-off duration after crash trigger |

### Live Futures Execution (USDⓈ-M)

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `FUTURES_LEVERAGE` | 5 | Default leverage for real futures positions |
| `FUTURES_ACCOUNT_RISK_PCT` | 0.01 (1%) | Max account risk per trade |
| `FUTURES_MAKER_FEE` | 0.0002 | Binance USDⓈ-M maker fee (0.02%) |
| `FUTURES_TAKER_FEE` | 0.0005 | Binance USDⓈ-M taker fee (0.05%) |
| `ROUND_TRIP_COST_PCT` | 0.15% | Futures taker round-trip (fee + slippage) |

### Futures Paper (Simulation)

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `FUTURES_PAPER_ENABLED` | True | Master switch |
| `FUTURES_SHORT_ENABLED` | True | Allow simulated SHORTs |
| `FUTURES_LONG_ENABLED` | True | Allow simulated LONGs |
| `FUTURES_MAX_LEVERAGE` | 2.0× | Simulated leverage |
| `FUTURES_POSITION_SIZE_USDT` | $20 | Max notional per position (ATR-sized) |
| `FUTURES_MIN_SCORE` | 1.5 | Baseline entry score (1.2 ranging / 1.8 volatile) |
| `FUTURES_HARD_STOP_PCT` | 0.60% | Max loss before forced exit |
| `FUTURES_TP1_PCT` | 0.50% | First profit target |
| `FUTURES_TP1_RATIO` | 0.50 | 50% closed at TP1 |
| `FUTURES_TRAIL_ACTIVATION_PCT` | 1.5% | Trail activates after 1.5% profit |
| `FUTURES_TRAIL_DISTANCE_PCT` | 0.8% | Trail distance |
| `FUTURES_TIME_STOP_MINUTES` | 4320 | 72h absolute max hold |
| `FUTURES_CHECK_INTERVAL` | 15s | Evaluation loop interval |
| `FUTURES_FUNDING_RATE_PCT` | 0.01% | Simulated funding per 8h interval |
| `FUTURES_ROUND_TRIP_COST_PCT` | 0.30% | Deducted at exit (taker fee simulation) |

### Trade Cooldown

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `TRADE_COOLDOWN_SECONDS` | 300s | Base cooldown between trades on same pair |
| `TRADE_COOLDOWN_TRENDING` | 180s | Shorter cooldown in trending regimes |
| `COOLDOWN_BYPASS_CONFIDENCE` | 5.0 | Bypass if `eff ≥ threshold + 5.0` AND trending |

### Orderbook / Flow

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `OB_IMBALANCE_BULL` | 0.6 | Buyers stacking (bullish) |
| `OB_IMBALANCE_BEAR` | 0.4 | Sellers stacking (bearish) |
| `OB_FLOW_BULL` | 0.6 | Buyers lifting asks |
| `OB_FLOW_BEAR` | 0.4 | Sellers hitting bids |
| `OB_EMA_ALPHA` | 0.3 | Smoothing alpha (~5-tick period) |

### FVG Entry Controls

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `FVG_MIN_GAP_PCT` | 0.1% | Minimum gap size |
| `FVG_MAX_AGE_SECONDS` | 300s | Discard gaps older than one 5m candle |
| `FVG_FAST_SCORE_MIN` | 2.0 | Minimum fast_score for FVG entry |
| `FVG_OB_MIN` | 0.5 | Minimum orderbook imbalance for FVG entry |
| `FVG_COOLDOWN_SECONDS` | 300s | Min time between FVG trades per pair |
| `FVG_ORDER_TIMEOUT` | 60s | Cancel unfilled limit order after this |

### Polling Intervals

| Signal | Interval |
|--------|---------|
| RSI / MACD / BB / ATR / Regime (REST fallback) | 10s |
| RSI / MACD / BB / ATR / Regime (kline-close WebSocket) | On candle close (~3m) |
| Orderbook imbalance + trade flow | 10s |
| CoinGecko volume | 60s |
| FVG detection | 60s |
| Whale Alert | 120s |
| Etherscan gas | 300s |
| Portfolio rebalance + weight update | 300s |
| Market Dynamics (VolumePairList refresh) | 300s |
| Google Trends | 900s |
| Fear & Greed | 3600s |
| Discord panel refresh | 45s |
| Portfolio snapshot (DB) | 1800s |

---

## Appendix: Decision Flow Summary

```
Every price tick (WebSocket → real_time_execution_worker):
│
├─ HOLDING? ──────────────────────────────────────────────────────────────────┐
│                                                                              │
│  [bot.py — _get_forced_exit_reason]                                         │
│   1. HARD_STOP     price ≤ dynamic_stop (ATR-scaled 0.5–2.0%) → SELL       │
│   2. TRAIL_STOP    price ≤ trailing_stop → SELL                             │
│   3. TIME_STOP     held ≥ 20min AND gain ≤ 0.15% → SELL                    │
│   4. MAX_HOLD_EXIT held ≥ 30min → SELL                                      │
│                                                                              │
│  [bot.py — TP1 check]                                                       │
│   5. TP1_FULL_EXIT gain ≥ 0.6% → sell 50%, move SL to breakeven+0.20%     │
│                                                                              │
│  [strategy.py — signal exits]                                               │
│   6. RSI > 70 → SELL (signal)                                               │
│   7. MACD crossover == "bearish" → SELL (signal)                           │
│   8. orderbook_imbalance < 0.4 → SELL (signal)                             │
│      (gated by _should_allow_signal_sell)                                   │
│   9. EARLY_FAILURE  raw_effective < threshold − 1.5 → SELL (bypass guard)  │
│                                                                              │
│  (none triggered) → continue to scoring                                     │
└─────────────────────────────────────────────────────────────────────────────┘
│
├─ SCORING (strategy.py):
│
│   raw_effective = confidence + structure_score + volatility_score
│
│   buy_score = raw_effective
│             − 1.0  if wick_ratio > 0.6
│             − 0.8  if ATR% < 0.3%
│             − 1.0  if RR < 1.0
│             − 0.5  if 1.0 ≤ RR < 1.20
│             − 1.0  if trend == "downtrend"
│             + 0.3  if trend == "uptrend"
│             − 0.3  if regime == "choppy_volatile"
│
│   HARD blocks: dead market (ATR+trend both low); concurrent cap (3 open)
│
│   if buy_score ≥ threshold → BUY candidate
│
├─ EXECUTION GATE (bot.py — get_entry_block_reason):
│
│   1. add_blocked         holding + price below entry
│   2. cooldown            time since last trade < limit
│   3. failed_exit_cooldown recent forced-exit on this pair
│   4. spike_filter        tick move > 0.3%
│   5. unstable_candle     candle range > 2.5× ATR
│   6. spread_filter       spread > 0.15% (live only)
│   7. insufficient_spendable usdt_balance − reserved ≤ $1
│   8. position_maxed      position ≥ 10% portfolio
│   9. min_notional        ATR-vol-adjusted trade size < $20
│  10. fvg_cooldown        FVG trade within 300s
│
│   (no blocker) → place BUY order
│
│   Size = portfolio × 3%
│         × regime/correlation/crash/downtrend modifiers
│         × vol_adj (STOP_LOSS_PCT / effective_sl_pct)   ← Phase 4
│
└─ (parallel, on 3m candle close via WebSocket):
    poll_rsi_on_close() → immediate full indicator recalc at T+0
```
