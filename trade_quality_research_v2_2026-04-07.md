# TRADE QUALITY RESEARCH V2 — DEEP BENCHMARK + IMPROVED MASTER PROMPT

**Date:** 2026-04-07  
**Bot Version:** V7.3.6  
**Codebase:** 7,300 lines across 16 Python modules  
**Period reviewed:** April 6–7, 2026

---

## 1) EXECUTIVE SUMMARY

Your bot (V7.3.6) is a 7,300-line custom desk bot running on KuCoin margin with ETH-USDT. It has a probabilistic regime engine (ER/BBW/ADX/DI), Manager/Worker/Orchestrator intent routing, maker-first execution, floating TP with economic floors, and hard-won margin truth/ghost-exit logic. Over the reviewed 24h window it executed ~10 trades across workers SFOL, DIP, and MOMO, with 1 EMERGENCY exit (dd=1.093%), 3 ACTIVITY_WARN events indicating missed moves (up to 2.55% move30), and multiple ENTRY_QUALITY_REJECT events.

**Verdict: Do NOT replace your bot.** But it needs a research sidecar and several surgical upgrades.

---

## 2) REPOSITORY RESEARCH — WHAT EXISTS ON GITHUB

### 2A. Freqtrade (48,368 stars, GPL-3.0)

**What it is:** The most popular open-source Python crypto trading bot. Strategy-lab first, with backtesting, hyperopt/Optuna parameter search, FreqAI (ML integration with LightGBM, XGBoost, etc.), dry-run discipline, and a protections layer that can temporarily halt trading.

**What it does well:**
- **Hyperopt parameter search** — Optuna-backed sweeps over strategy parameters with walk-forward validation. This is the single biggest research tool your bot is missing.
- **Protections layer** — Formal rules like StoplossGuard (pause after N losses), MaxDrawdown (halt at threshold), CooldownPeriod. Your bot has ad-hoc cooldowns but no formal protections framework.
- **FreqAI** — Self-retraining ML models on rolling windows. Supports classifiers and regressors. Trains on a background thread. This is the model for your future ML sidecar.
- **Fee-aware accounting** — Every backtest includes realistic fee impact. Your bot logs trades but doesn't have a research pipeline that scores net-of-fee edge.
- **Dry-run parity** — Live and dry-run use identical logic paths. Your bot has no dry-run mode at all.

**Fit assessment:**
- **Direct fit?** No. Freqtrade's default architecture is strategy-template → backtest → deploy, not your manager/worker/orchestrator real-time loop.
- **Partial fit?** Yes. Its research workflow, protections, and FreqAI design are directly borrowable.
- **What to steal:** (1) Optuna hyperopt harness for your top 10 knobs. (2) Formal protections layer concept for post-loss, post-emergency, post-giveback cooldowns. (3) FreqAI's rolling-retrain architecture for your future ML scorer.
- **What to reject:** Its backtesting engine (too simplistic for your maker-first fills), its strategy template model (too rigid for your intent-based routing).

### 2B. Hummingbot (14,000+ stars, Apache 2.0)

**What it is:** Maker-first market-making framework with 140+ exchange connectors, inventory skew, hanging orders, and cross-exchange arbitrage. Now has a V2 strategy framework with modular Controllers and Executors.

**What it does well:**
- **In-flight order tracking** — ClientOrderTracker maintains a state machine per order (PENDING → OPEN → PARTIALLY_FILLED → FILLED/CANCELLED). Your execution.py has order tracking but it's embedded in procedural logic, not a clean state machine.
- **Inventory skew** — Adjusts bid/ask spreads based on current inventory to reduce directional exposure. Relevant for your position sizing after entries.
- **Hanging orders** — Keeps unfilled orders alive across refresh cycles if they're close to fill, preserving queue position. Your bot cancels and replaces, losing queue position every time.
- **Connector architecture** — Standardized REST/WS interface per exchange. Your client.py and client_patch.py are KuCoin-specific spaghetti. Not urgent to fix but a long-term debt.
- **Strategy V2 Executors** — Modular execution components (PositionExecutor, DCAExecutor, ArbitrageExecutor) that separate strategy intent from execution mechanics. Conceptually similar to your Manager/Worker split but cleaner.

**Fit assessment:**
- **Direct fit?** No. Hummingbot is designed for market-making (buy+sell simultaneously), not your directional regime-driven desk bot.
- **Partial fit?** Yes. Order lifecycle modeling, hanging-order concept, and inventory skew ideas are directly useful.
- **What to steal:** (1) Order state machine concept for cleaner execution.py. (2) Hanging-order logic — if your maker entry is 80%+ through the queue, don't cancel on reprice. (3) Inventory skew concept for position sizing.
- **What to reject:** Its core market-making loop, its connector abstraction (overkill for single-exchange), its governance model.

### 2C. NautilusTrader (21,583 stars, LGPL-3.0)

**What it is:** Production-grade Rust-native trading engine with deterministic event-driven architecture and nanosecond resolution. Python control plane via PyO3/Cython.

**What it does well:**
- **Research-to-live parity** — Same code, same event model, same time semantics in backtest and live. This is the gold standard your bot should aspire to.
- **Deterministic replay** — Can replay historical data through the exact same event pipeline. Your bot has zero replay capability.
- **OMS (Order Management System)** — Explicit separation of execution engine, risk engine, and order management. Your bot mixes all three in execution.py (1,257 lines).
- **Fill simulation** — Latest release (1.224.0, March 2026) added L1 quote-based queue position tracking for backtests, and `fill_limit_inside_spread` control. This is exactly the maker fill probability simulator your research plan calls for.
- **Matching engine** — Configurable liquidity consumption tracking to prevent overfilling displayed book liquidity. Your bot has no fill simulation at all.
- **Event-driven architecture** — Every state change is an event with nanosecond timestamp. Your bot uses procedural loops with `time.time()`.

**Fit assessment:**
- **Direct fit?** No. Too heavy to adopt wholesale. The Rust core is 21K+ lines and the architecture assumes you'll rebuild around it.
- **Best long-term inspiration?** Absolutely. This is the architectural north star.
- **What to steal:** (1) OMS separation concept — split execution.py into order_manager.py, risk_engine.py, and execution.py. (2) Fill simulation model for your research sidecar. (3) Deterministic event log format for replay. (4) Queue position tracking logic.
- **What to reject:** Adopting it as your runtime (too disruptive), its adapter/connector abstraction (overkill), its Rust requirement.

### 2D. Jesse (7,573 stars, MIT)

**What it is:** Clean Python crypto bot focused on developer experience. Simple strategy API, 300+ indicators, built-in backtesting with Web UI, multi-timeframe support, and smart ordering.

**What it does well:**
- **Strategy ergonomics** — A strategy is a class with `should_long()`, `should_short()`, `go_long()`, `go_short()`, `update_position()`. Dead simple. Your bot's strategy.py is 916 lines of interlocking workers.
- **Multi-timeframe without look-ahead bias** — Properly handles candle alignment across timeframes. Your bot uses a single timeframe with manual lookback.
- **Built-in optimization** — Parameter optimization with genetic algorithms and random search.
- **Web UI** — Real-time visualization of backtest results, equity curves, trade distributions.

**Fit assessment:**
- **Direct fit?** No. Jesse's "smart ordering" convenience is the opposite of your explicit maker-first control philosophy.
- **Partial fit?** Yes, for research workflow.
- **What to steal:** (1) Strategy-lab ergonomics for your research harness — define a "research strategy" class that's simpler than your live strategy. (2) Multi-timeframe architecture for your indicator pipeline. (3) Web UI concept for your replay viewer.
- **What to reject:** Its execution model (too abstract for your margin state machine), its single-strategy assumption (you need multi-worker orchestration).

### 2E. Other Notable Repos

**CryptoMarket_Regime_Classifier** — HMM + LSTM regime detection using multi-timeframe features. Your probabilistic ER/BBW/ADX/DI regime is comparable but purely analytical (no ML). This repo is a good reference for when you add ML to regime classification.

**Market Microstructure repos** (Baruch MFE coursework, DeepMarket, LOBFrame) — Academic implementations of queue models, Fokker-Planck dynamics for order book simulation, and trade impact models. These are the theoretical foundation for the fill-probability simulator in your research plan.

**Intelligent Trading Bot** (asavinov) — Feature engineering → ML classification → signal generation pipeline. Good reference for your future ML scorer architecture.

---

## 3) COMPARISON MATRIX — YOUR BOT vs. THE FIELD

| Capability | Your Bot V7.3.6 | Freqtrade | Hummingbot | NautilusTrader | Jesse |
|---|---|---|---|---|---|
| Probabilistic regime engine | ✅ ER/BBW/ADX/DI blend | ❌ (manual) | ❌ | ❌ (user builds) | ❌ |
| Multi-worker orchestration | ✅ Manager/Worker/Orch | ❌ single strategy | ❌ single strategy | ❌ (user builds) | ❌ |
| Maker-first execution | ✅ explicit maker logic | ❌ | ✅ native | ❌ (user builds) | ❌ |
| Floating TP with econ floor | ✅ vol-scaled | ❌ | ❌ | ❌ | ❌ |
| Margin truth hardening | ✅ ghost-exit, stale-clear | ❌ | ❌ | ❌ | ❌ |
| Backtesting | ⚠️ basic (461 lines) | ✅ excellent | ✅ good | ✅ best-in-class | ✅ very good |
| Parameter optimization | ❌ none | ✅ Optuna/hyperopt | ❌ | ❌ (user builds) | ✅ genetic algo |
| Replay / deterministic sim | ❌ none | ⚠️ partial | ❌ | ✅ nanosecond replay | ✅ good |
| Fill simulation / queue model | ❌ none | ❌ | ❌ | ✅ L1 queue tracking | ❌ |
| Dry-run mode | ❌ none | ✅ excellent | ✅ paper trading | ✅ | ✅ |
| Order state machine | ⚠️ procedural | ❌ | ✅ ClientOrderTracker | ✅ OMS | ⚠️ |
| ML integration | ❌ none | ✅ FreqAI | ❌ | ❌ | ❌ |
| Formal protections | ❌ ad-hoc cooldowns | ✅ StoplossGuard etc | ❌ | ❌ | ❌ |
| Trade quality ledger | ❌ none | ❌ | ❌ | ❌ | ❌ |
| Market psychology classifier | ❌ none | ❌ | ❌ | ❌ | ❌ |
| Post-emergency behavioral rules | ⚠️ basic cooldown | ✅ protections | ❌ | ❌ | ❌ |

**Reading this table:** Your bot has 5 things nobody else has out of the box (regime engine, orchestrator, floating TP, margin truth, maker-first with economic floors). But it's missing 7 things at least one competitor does well (parameter search, replay, fill sim, dry-run, order state machine, ML, formal protections).

---

## 4) LOG EVIDENCE — WHAT THE APRIL 6–7 DATA SHOWS

**Trades observed:**
1. SFOL SHORT @ 2105.41 → EXIT_DONE_MARKET (26m hold) — short in CHOP, low p=0.35
2. EXIT_DONE_POST_MAKER → SFOL SHORT @ 2104.67 → EXIT_DONE_POST_MAKER (2h50m hold)
3. DIP VWAP_RALLY LONG @ 2112.58 → EXIT_DONE_POST_MAKER (2h36m hold) — in CHOP p=0.35
4. SFOL SHORT @ 2086.48 → EXIT_DONE_MARKET (26m hold) — in MIXED p=0.38
5. SFOL SHORT @ 2082.87 → EXIT_DONE_MARKET (39m hold) — in CHOP p=0.39
6. SFOL SHORT @ 2072.03 → **EMERGENCY EXIT** dd=1.093% (1h23m) — entered CHOP p=0.17
7. MOMO LONG @ 2098.58 → EXIT_DONE_POST_MAKER (26m hold) — in TREND p=0.69
8. DIP VWAP_DIP LONG @ 2095.86 → EXIT_DONE_MARKET (2h23m hold)
9. DIP VWAP_RALLY LONG @ 2118.24 → entry sent but unclear fill
10. DIP VWAP_DIP LONG @ 2132.36 → EXIT_DONE_POST_MAKER (1h5m hold)

**Key observations:**
- **ENTRY_QUALITY_REJECT** fired on SQMR sells with score 0.59–0.60 (below threshold) — quality gate is working but maybe too tight for squeeze-meanrev
- **EMERGENCY EXIT** on trade #6: entered short at p=0.17 (very low trend probability), then got squeezed. The bot entered in deep chop with almost no directional conviction. This is a **blocker failure** — the entry quality gate should have caught p=0.17 as too low.
- **ACTIVITY_WARN at 07:07** with move30=0.87% — the bot sat idle for 55 minutes while a move happened
- **ACTIVITY_WARN at 10:37** with move30=1.02% — another 93 minutes idle during a MIXED→TREND transition
- **ACTIVITY_WARN at 16:02** with move30=2.55% in TREND p=0.68 — the bot missed a 2.55% move in a trending regime. This is the highest-cost missed opportunity.
- **11 exit_attempts** in state.json — suggests the bot struggled to close a position cleanly

**PROVEN defects:**
1. Trade #6 entered with p=0.17 and got emergency-stopped. The quality gate failed to block a very low-conviction entry.
2. The 16:02 ACTIVITY_WARN shows a 2.55% move in TREND p=0.68 was missed entirely.
3. No trade-quality ledger exists to systematically track these events.

---

## 5) WHAT YOUR BOT IS MISSING — PRIORITIZED

### Tier 1: Must Add Now (Week 1)

**A. Trade Quality Ledger**
For every trade candidate (entered or rejected), record: timestamp, regime snapshot (name, p_trend, p_chop, p_breakout), worker tag, raw score, orchestrator adjusted score, blocker family (if rejected), edge_bps, fill outcome, best excursion, worst excursion, realized PnL bps, hold time. Store as append-only CSV or SQLite. This is the single most important missing piece — without it you cannot measure improvement.

**B. Formal Protections Framework**
Steal from Freqtrade's protections concept. Implement:
- `EmergencyExitCooldown`: After any emergency exit, impose N-minute cooldown in the same direction. Currently your cooldown is generic; it should be directional.
- `GivebackGuard`: After a position that was profitable but exited at breakeven or loss, impose same-direction cooldown.
- `LowConvictionBlock`: Hard block entries when p_trend < 0.20 (trade #6 should never have entered with p=0.17).
- `ConsecutiveLossHalt`: After N consecutive losing trades, pause for M minutes.

**C. Blocker Family Logging**
For every ORCH_NO_INTENT and ENTRY_QUALITY_REJECT, log the specific blocker family: `regime_unsuitable`, `no_directional_bias`, `score_too_low`, `edge_too_thin`, `falling_knife`, `cooldown_active`, `quality_reject`. Your logs already partially do this but it's not systematic enough for analysis.

**D. Maturity Score for Continuation Entries**
The SFOL (short followthrough) worker fired 4 times in a row. After the first SFOL succeeds, the next one should require higher conviction because the easy move is done. Add a `continuation_maturity` score that decays with each successive same-direction entry.

### Tier 2: Research Sidecar (Weeks 2–3)

**E. Replay Harness**
Build a separate script that reads your CSV logs and replays price action + regime state, asking: "If the bot had been running with different parameters, what would have happened?" Start with your top 5 knobs: `long_quality_edge_buffer_bps`, `dip_falling_knife_ret3_max`, entry score threshold, emergency dd threshold, cooldown duration. This borrows from Freqtrade's backtest + Jesse's research ergonomics.

**F. Parameter Sweep (Optuna)**
Wrap your replay harness with Optuna to search parameter space. Target metric: net PnL bps per trade, penalized by max drawdown. Start with 3 knobs, not 30.

**G. Opportunity-Cost Register**
For every ACTIVITY_WARN: reconstruct the move (direction, size, duration), the regime evolution during the move, whether any worker emitted an intent, which blocker prevented entry, and the counterfactual PnL if the blocker had been absent. The 16:02 ACTIVITY_WARN (2.55% missed move in TREND p=0.68) is your most expensive recent event and should be the first case study.

### Tier 3: Execution Quality (Weeks 3–4)

**H. Order State Machine**
Refactor execution.py (1,257 lines) into a cleaner architecture. Steal from NautilusTrader's OMS concept and Hummingbot's ClientOrderTracker:
- `OrderStateMachine`: PENDING → SUBMITTED → OPEN → PARTIAL → FILLED / CANCELLED / REJECTED
- `RiskEngine`: Pre-trade checks (margin available, position limits, protections)
- `ExecutionEngine`: Placement, modification, cancellation logic
This is not urgent for PnL but it reduces bug surface and makes the codebase maintainable.

**I. Maker Fill Probability Estimator**
NautilusTrader's v1.224.0 added L1 queue position tracking. Build a simpler version: given current book depth, spread, and your order size, estimate the probability your maker order fills within T seconds. Use this to decide when to stay maker vs. cross the spread. Feed this into the replay harness to evaluate alternative execution strategies.

**J. Hanging Order Logic**
Steal from Hummingbot: if your entry order has been in the book for >N seconds and is within M% of the inside, don't cancel it on a minor signal change. You lose queue position every time you cancel/replace.

### Tier 4: Innovation Track (Future)

**K. Market Psychology Classifier**
Build as a separate module, not in the hot path. Input: last N candles of OHLCV + volume profile. Output: a label from {EXPANSION, PANIC, EXHAUSTION, SQUEEZE, CHASE, TRAP, ACCEPTANCE, REJECTION} with confidence score.

Feature engineering:
- Speed: rate of price change over last 1/3/5/10 candles
- Range expansion: ATR ratio (current vs. 20-period average)
- Wick asymmetry: (upper_wick - lower_wick) / body_size
- Follow-through quality: did price hold above/below the previous candle's midpoint?
- Volume profile: is volume increasing into the move or fading?
- Reversal failure: did a reversal candle get immediately negated?

This draws on behavioral finance concepts (Kahneman's prospect theory, herding behavior, disposition effect) without being mystical about it. The classifier is a feature for your orchestrator's ranking and blocker override decisions, not a standalone signal.

**L. ML Scorer**
Only after you have 500+ entries in your trade quality ledger. Use gradient-boosted trees (LightGBM, matching FreqAI's approach) for:
- `P(late_entry)`: probability that entering now is too late in the move
- `P(giveback)`: probability that a profitable position reverses to breakeven
- `P(emergency_exit)`: probability that the position hits emergency threshold
- `P(blocker_override_safe)`: probability that overriding a soft blocker is safe

Feed these as additional inputs to the orchestrator's scoring, not as direct order triggers.

**M. Quantum-Inspired Optimization**
Belongs ONLY in the research layer. Quantum annealing-inspired search (e.g., D-Wave's simulated annealing, or QAOA-inspired variational methods) for the combinatorial problem of selecting optimal knob combinations from a large parameter space. This is a substitute for grid search when you have 10+ knobs with interactions. Do not put this in the live bot. Do not call it "quantum trading."

**N. Stochastic Dynamics Concepts**
From mathematical finance, concepts that are directly applicable to your sidecar:
- **Ornstein-Uhlenbeck process** for mean-reversion regime modeling — your squeeze-meanrev worker could benefit from OU-fitted half-life estimates
- **Fokker-Planck equation** for modeling the probability distribution of price evolution given current order book state — directly applicable to fill probability estimation
- **Hawkes process** for modeling self-exciting order flow (bursts of buying/selling that trigger more buying/selling) — useful for your volume/momentum detection
- **Almgren-Chriss framework** for optimal execution scheduling — relevant when you want to split entries/exits across time

---

## 6) ADOPTION PATH — WHAT TO BORROW, WHAT TO REJECT

| Source | Borrow | Reject | Why |
|---|---|---|---|
| Freqtrade | Optuna parameter search, protections framework, FreqAI rolling-retrain pattern, fee-aware accounting | Backtest engine, strategy template, connector model | Your execution is more sophisticated than Freqtrade's; its research tools fill your biggest gap |
| Hummingbot | Order state machine, hanging-order concept, inventory skew for sizing | Market-making core, connector abstraction, governance model | You're directional, not market-making; but their order lifecycle is cleaner than yours |
| NautilusTrader | OMS separation, fill simulation, deterministic replay, event log format | Runtime engine, Rust core, adapter system | Too heavy to adopt; perfect as architectural inspiration |
| Jesse | Research strategy class pattern, multi-timeframe handling, Web UI for replay | Smart ordering, single-strategy assumption | Your explicit control is better; their ergonomics are better |
| CryptoMarket Regime Classifier | HMM + LSTM regime detection pattern | Direct adoption | Good reference for when you add ML to your regime engine |
| DeepMarket / LOBFrame | Fokker-Planck order book dynamics, queue modeling | Direct adoption | Academic foundation for your fill-probability simulator |

---

## 7) UPDATED MASTER PROMPT — V2

```
You are acting as a hybrid of:
- Senior Quantitative Analyst and Execution Trader at a Tier-1 desk
- Senior Financial Engineer and Quant Strategist
- Principal / Senior Software Engineer with production Python discipline
- Senior Site Reliability Engineer focused on event-loop health, resiliency, and state integrity
- Lean / Six Sigma root-cause investigator focused on eliminating real defects, waste, and fragile rework
- Trading systems researcher benchmarking against NautilusTrader, Hummingbot, Freqtrade, and Jesse

MISSION
Perform a live-grade forensic audit and trade-quality improvement review of the provided
trading bot, logs, fills, and current configuration.
Your goal is to improve trade quality, execution quality, and research discipline
without damaging the bot's core identity.

NON-NEGOTIABLE OPERATING RULES
1. Preserve the current engine identity unless evidence clearly proves a better substitution.
2. Separate live-engine improvements from research-sidecar improvements.
3. Treat missed opportunity analysis as first-class, not secondary.
4. Every major conclusion must be labeled PROVEN, INFERRED, PROPOSED, or UNVERIFIED.
5. Never claim fixed, validated, production-ready, profitable, or backtested unless evidence proves it.
6. When recommending a concept from an external repo, always state:
   (a) What it does well, (b) Is it compatible with our identity,
   (c) Direct fit / partial fit / bad fit / future fit, (d) What exactly to borrow or reject, (e) Why.

BENCHMARKING MANDATE
Compare against these classes of open-source strengths:
- Freqtrade: parameter search, protections, dry-run, FreqAI rolling-retrain
- Hummingbot: order state machine, hanging orders, inventory skew, maker lifecycle
- NautilusTrader: OMS separation, fill simulation, deterministic replay, queue tracking
- Jesse: strategy-lab ergonomics, multi-timeframe, research workflow, Web UI

For each benchmark idea answer: What does it do well? Compatible with our identity?
Direct/partial/bad/future fit? What to borrow, adapt, or reject? Why?

MANDATORY TRADE-QUALITY OUTPUTS
1. Executive Verdict
2. Session Scorecard (trades taken, rejected, missed, emergency exits, activity warns)
3. How the Bot Works (architecture summary)
4. Root Cause Register (every defect with PROVEN/INFERRED label)
5. Trade Deep-Dives (every fill with entry/exit/excursion/hold/PnL)
6. Missed-Opportunity Register (every ACTIVITY_WARN with counterfactual)
7. Regime & Strategy Audit (regime accuracy, worker hit rates, orchestrator decisions)
8. Plumbing & Reliability Audit (state machine integrity, error recovery, WS health)
9. Execution & Microstructure Audit (maker fill rates, queue position, spread capture)
10. Knob Audit (which knobs are load-bearing, which are dead weight)
11. External Benchmark Comparison (the matrix from this research doc)
12. Trade Quality Improvement Plan (tiered, with estimated effort and impact)
13. Protections Framework Design (formal behavioral rules)
14. Validation Status (what can be verified from logs vs. what needs live testing)
15. Next Daily Audit Prompt

MISSED-OPPORTUNITY REGISTER FORMAT
For every major missed move:
- exact timestamp window
- direction and move size (bps)
- regime state evolution during the move
- whether a worker emitted an intent
- whether orchestrator stood down or no-intent occurred
- blocker family
- whether the blocker was correct, too strict, too late, or miswired
- estimated opportunity cost in bps and USD

TRADE QUALITY LEDGER REQUIREMENT
For every entry candidate (traded or not):
- timestamp, regime, worker tag, raw score, orch adjusted score
- blocker family (if rejected), edge_bps, maturity state
- fill outcome or missed-trade label
- realized or counterfactual result in bps

INNOVATION TRACK (separate section)
Only fit-for-purpose ideas:
- Market psychology classifier (EXPANSION/PANIC/EXHAUSTION/SQUEEZE/CHASE/TRAP/ACCEPTANCE/REJECTION)
  with feature engineering from speed, range expansion, wick asymmetry, follow-through,
  volume profile, reversal failure
- Execution realism simulator using Fokker-Planck dynamics and queue position modeling
- ML scorer for P(late_entry), P(giveback), P(emergency), P(blocker_override_safe)
  using LightGBM after 500+ ledger entries, following FreqAI patterns
- Ornstein-Uhlenbeck half-life estimation for squeeze-meanrev worker
- Hawkes process for self-exciting order flow detection in momentum worker
- Quantum-inspired optimization (simulated annealing / QAOA-inspired) ONLY for
  research-layer parameter search, NEVER in live execution logic

STYLE
- Write like a desk operator accountable for tomorrow's open
- Be blunt, specific, and evidence-driven
- Keep reporting efficient and high-signal
- No marketing tone, no academic fog, no fake certainty, no decorative complexity
- If you don't have evidence, say UNVERIFIED and move on
```

---

## 8) BUILD PLAN — UPDATED

### Phase A: Week 1 — Trade Quality Foundation
- [ ] Implement trade quality ledger (append-only CSV/SQLite)
- [ ] Add formal protections framework (EmergencyExitCooldown, LowConvictionBlock, GivebackGuard)
- [ ] Add blocker-family logging to every ORCH_NO_INTENT and ENTRY_QUALITY_REJECT
- [ ] Add continuation maturity score for SFOL worker
- [ ] Fix: block entries when p_trend < 0.20 (prevents trade #6 scenario)

### Phase B: Weeks 2–3 — Research Sidecar
- [ ] Build replay harness from CSV logs
- [ ] Wrap with Optuna for parameter search (start with 3–5 knobs)
- [ ] Build opportunity-cost register from ACTIVITY_WARN events
- [ ] Score trade quality by regime bucket and worker, not just aggregate PnL

### Phase C: Weeks 3–4 — Execution Quality
- [ ] Refactor execution.py into OrderStateMachine + RiskEngine + ExecutionEngine
- [ ] Add maker fill probability estimator (simple: book depth + spread + size → P(fill|T))
- [ ] Add hanging order logic (preserve queue position when close to fill)
- [ ] Add dry-run mode (paper trading with identical logic path)

### Phase D: Future — Innovation
- [ ] Market psychology classifier sidecar
- [ ] ML scorer (after 500+ ledger entries)
- [ ] Execution realism simulator (Fokker-Planck inspired)
- [ ] Quantum-inspired parameter search for research layer

---

## 9) DECISION LOG

| Decision | Rationale | Status |
|---|---|---|
| Keep live engine core | Regime engine + orchestrator + maker-first + margin truth are unique advantages | PROVEN |
| Do not replace with Freqtrade | Freqtrade is a strategy lab, not a desk bot; but steal its research tools | PROVEN |
| Do not replace with Hummingbot | It's a market-making framework; your bot is directional | PROVEN |
| Do not replace with NautilusTrader | Too heavy to adopt; best architectural north star | PROVEN |
| Do not replace with Jesse | Too simple for your execution needs; good strategy-lab reference | PROVEN |
| Add trade quality ledger first | Cannot improve what you cannot measure | PROVEN |
| Add formal protections before ML | Behavioral controls yield immediate results; ML needs data | INFERRED |
| Build psychology classifier as sidecar | Live logic is already complex enough; sidecar keeps it clean | PROPOSED |
| Use Optuna for parameter search | Industry standard, Freqtrade-validated, Python-native | PROPOSED |
| ML only after 500+ ledger entries | Insufficient data leads to overfitting | PROPOSED |

---

*End of Trade Quality Research V2*
