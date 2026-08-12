# v2 research and validation protocol

## Objective

Maximize expected **net** return under a fixed survival/risk contract. A strategy, model or agent is assumed to have no edge until it survives the gates below.

## Data contract

Every research run records:

- immutable raw/normalized dataset snapshot IDs and hashes;
- event time and received time;
- symbol/instrument mapping;
- session/calendar/DST version;
- corporate/event metadata when used;
- feature version;
- cost model version;
- strategy/prompt/model version;
- code SHA and config hash.

No run may silently backfill data into an existing snapshot.

## Experimental lanes

- **A deterministic** — compact rule baseline.
- **B Sol ranking/veto** — AI may rank/veto deterministic candidates.
- **C Sol ablation** — retained as a research label, but AI may only select/veto a supplied deterministic candidate; it cannot construct executable economic terms.
- **D Sol + critic** — direct/selected intent plus independent adversarial critic.
- **E simple ML** — dependency-light regularized statistical baseline.
- **F no-trade** — zero-risk benchmark.

Compare lanes on identical event windows and cost assumptions. Model cost/latency is part of lane economics.

## Walk-forward protocol

1. Define parameter space and acceptance criteria before observing the final holdout.
2. Use chronological train/validation/test folds; test windows must not overlap.
3. Carry only information available at each event time.
4. Keep a final untouched period outside all tuning and model/prompt selection.
5. Use one-way registry semantics: once final holdout is consumed, the experiment cannot be presented as untouched again.
6. Segment results by volatility/trend/range/stress regime and market session.
7. Compare against no-trade, simple deterministic and simple statistical baselines.

## Multiple testing

For each family/lane record the total number of tried variants, including discarded variants. Promotion evidence includes:

- Deflated Sharpe probability;
- Probability of Backtest Overfitting;
- White Reality Check-style bootstrap p-value;
- number of trials / correlated alternatives;
- untouched OOS result.

A raw Sharpe or top-ranked backtest is insufficient.

## Cost model

Research must include commissions/fees, bid/ask spread, slippage, financing and broker minimum constraints. Use observed/calibrated P50/P95 distributions where available. At minimum, a promoted hypothesis must remain profitable at the configured stressed cost multiple (default gate: 2x calibrated round-trip cost).

The live coordinator additionally refuses deterministic candidates whose non-probabilistic payoff proxy does not clear conservative provisional costs. The raw score is never treated as a calibrated win probability; this is only a tradability filter, not expected value or proof of alpha.

## Historical/shadow parity

For identical recorded market events, historical and shadow modes must produce the same decisions, fills, exits and P&L within the configured numerical tolerance. Any unexplained delta blocks promotion.

## DEMO soak

Code completion is not a substitute for elapsed market evidence. Before any REAL release discussion, require at least:

- 30 calendar days minimum, preferably 60;
- >=100 closed trades for a candidate family/lane before statistical promotion claims;
- multiple sessions/regimes;
- zero unresolved `UNKNOWN` orders;
- zero critical reconciliation incidents;
- zero unexplained historical/shadow parity drift;
- broker cost and slippage calibration from actual DEMO observations.

If the strategy is intentionally low-frequency, trade-count gates must be reconsidered explicitly rather than silently relaxed.

## Default promotion gate

`PromotionGateV2` currently requires:

- >=100 closed trades;
- >=30 shadow days;
- Deflated Sharpe probability >=0.95;
- PBO <=0.20;
- Reality Check p-value <=0.05;
- max drawdown <=10%;
- profit factor >=1.10;
- profitable under >=2x cost stress;
- parity delta <=$0.01;
- zero `UNKNOWN` orders;
- zero critical incidents;
- untouched test executed exactly once and net positive.

These are provisional research gates, not guarantees of profitability.

## Retirement

A promoted hypothesis returns to shadow if any of the following occurs:

- unresolved execution ambiguity;
- data-quality or timestamp-contract breach;
- cost model materially underestimates observed execution;
- statistically meaningful OOS degradation;
- drawdown or loss gate breach;
- structural broker rule/market-session change;
- new research epoch invalidates comparability.

## AI evaluation

The LLM is retained in the live decision path only if lane ablation demonstrates incremental value after model cost and without worse risk. Compare at least deterministic baseline, veto/rank, direct intent, direct+critic, simple ML and no-trade.

Model confidence is never interpreted as calibrated probability by default and never replaces the candidate's raw deterministic signal score. Calibration must be measured separately against outcomes and/or process correctness.
