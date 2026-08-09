# External adversarial review prompt — eToro DEMO Trading Lab

You are an independent review panel combining five senior roles:

1. quantitative researcher and statistician;
2. market-microstructure and short-horizon trading specialist;
3. AI/LLM agent architect;
4. trading risk, security, and reliability engineer;
5. skeptical capital allocator responsible for a small personal account.

Your task is to audit the project below and recommend the best direction. Do not defend the current design. A partial or total pivot is explicitly acceptable and preferred if evidence supports it.

Use current information as of 2026-08-09. If web access is available, verify unstable claims against primary sources: official eToro Public API/MCP documentation, official OpenAI/Codex documentation, official framework documentation, exchange/broker specifications, and original research papers. Cite direct links and distinguish verified facts, estimates, assumptions, and your own inferences. Do not rely on affiliate trading sites or marketing claims.

## Owner objective and constraints

- We are currently using virtual/paper money only.
- Eventual real-money capital, only after validation and a separate security review: USD 1,000 total.
- Desired horizon: intraday to several days, not long-term investing.
- Objective: maximize expected net return subject to explicit survival, loss, and drawdown constraints. There is no assumption or promise that profit is achievable.
- Do not enable, recommend enabling, or simulate authorization for real-money execution now.
- The LLM must never be able to bypass risk limits, access eToro credentials, call broker write tools directly, approve its own orders, or modify its own risk policy.
- OpenAI inference should use the owner's ChatGPT Pro subscription through authenticated Codex, not a separately billed OpenAI Platform API key. There must be no automatic paid-API fallback.
- MiniMax through OpenCode is available for research, test generation, and independent review, but it must never receive eToro credentials or autonomous execution authority.
- Official eToro MCP semantics currently require the exact write request to be displayed to the owner and an explicit one-time approval for each broker write. Do not propose silently bypassing this rule. Clearly separate fully autonomous shadow fills from actual eToro DEMO broker writes.
- Strategies and model outputs are untrusted inputs. Only deterministic code may enforce risk and authorize a technically valid order.

## What exists now

Repository: Python 3.11+, current commit `d2940d8`.

Implemented:

- Official eToro MCP integration with an allowlist of read-only data routes and exactly one possible write route, restricted to eToro DEMO.
- Seven-instrument catalog: EURUSD, SPX500, NSDQ100, AAPL, TSLA, BTC, ETH.
- UTC candle ingestion and validation for mapping, duplicates, gaps, stale/future data, OHLC integrity, and market sessions.
- Twelve deterministic strategy hypotheses, each mapped to an isolated local shadow ledger starting with USD 1,000:
  1. ORB 15-minute immediate;
  2. ORB 15-minute retest;
  3. first/last 30-minute momentum;
  4. Donchian ATR breakout;
  5. EMA 9/21 plus ADX;
  6. Bollinger squeeze breakout;
  7. Bollinger/RSI mean reversion;
  8. ATR shock fade;
  9. London breakout EURUSD;
  10. New York/London overlap momentum EURUSD;
  11. SPX/Nasdaq pairs mean reversion;
  12. EURUSD four-hour time-series momentum.
- TradeIntent-only strategy contract.
- Deterministic risk engine with allowed symbols, order/symbol/gross exposure, leverage, open-position count, trade frequency, spread/staleness, stop-distance, daily/weekly/monthly loss and drawdown controls.
- Default numerical configuration: USD 1,000 per shadow ledger; USD 500 maximum order, symbol, and gross exposure; one open position; 10 trades/day; USD 5 risk/trade; USD 15 daily, USD 30 weekly, and USD 50 monthly loss thresholds; 10% maximum drawdown; 15-minute minimum trade interval. These values are provisional research limits, not validated recommendations.
- Fail-closed persistent kill switch.
- Ed25519 risk seals. The risk process owns the private key; the executor has only the public key. The seal binds the exact DEMO account/route/method/body, request ID, intent, quote, risk configuration/state, expiry, and nonce.
- Immutable proposal plus exact, atomic, one-time approval; timeout/5xx becomes UNKNOWN and is not retried automatically.
- Deterministic paper fills, exact-Decimal portfolio accounting, realized/unrealized P&L, fees, financing, daily P&L, and strategy attribution.
- Deterministic backtest and chronological non-overlapping walk-forward harness with configurable commission, spread, slippage, drawdown, and ranking.
- NautilusTrader 1.231.0 only for offline/replay timing, not as an eToro broker adapter.
- SQLite WAL/FULL runtime audit with append-only hash chain. PostgreSQL append-only schema/store exists, but production has not migrated to it and its live integration test is not currently exercised.
- Authentik owner-only dashboard at `https://trading.astancu.eu`, behind Cloudflare Tunnel and Caddy, showing 12 portfolios, P&L, health, audit, kill state, proposals, approvals, and order lifecycle.
- Production services currently run the shadow worker every 60 seconds and the dashboard. Health reports DEMO, `real_money=false`, audit-chain valid, and broker execution disabled.
- Current targeted test result: 53 passing, one optional PostgreSQL integration test skipped because no integration DSN is configured locally.

Relevant project files, if supplied with this prompt:

- `README.md`
- `ARCHITECTURE.md`
- `RUNBOOK.md`
- `config/demo.json`
- `src/etoro_agent/strategy.py`
- `src/etoro_agent/engine.py`
- `src/etoro_agent/risk.py`
- `src/etoro_agent/execution.py`
- `src/etoro_agent/market.py`
- `src/etoro_agent/backtest.py`
- `src/etoro_agent/portfolio.py`
- `src/etoro_agent/audit.py`
- `src/etoro_agent/mcp.py`
- `src/etoro_agent/dashboard.py`
- `ops/postgres/schema.sql`
- `tests/`

## What does not exist or is not validated

- No LLM/AI is currently in the trading decision loop. The live shadow engine is deterministic.
- None of the 12 strategies has demonstrated a robust positive out-of-sample edge after realistic eToro costs.
- There is no long-duration shadow track record, statistically adequate fill sample, or evidence supporting promotion of a top three.
- The current backtest capability exists, but the project does not yet have a comprehensive, versioned, survivorship-safe multi-year intraday dataset or calibrated eToro execution model.
- No robust bid/ask history, order-book data, corporate-action pipeline, economic calendar, earnings calendar, news/sentiment feed, or independent second market-data source is integrated.
- No LLM decision schema, prompt/evaluation suite, hallucination/adversarial testing, confidence calibration, or ablation proving that an LLM adds value over deterministic baselines exists.
- No autonomous eToro DEMO broker execution is enabled. Shadow fills are local simulations.
- Runtime persistence is still SQLite; PostgreSQL is only prepared.
- No profitability claim, edge validation, or real-money readiness claim is being made.

## Proposed next architecture

Keep a continuously running deterministic core, but invoke `gpt-5.6-sol` only as a bounded decision component:

1. Market collector updates continuously, initially every 60 seconds.
2. At each relevant candle close, deterministic filters detect actionable setups, regime changes, data anomalies, or positions requiring review.
3. When triggered, one consolidated, sanitized, immutable decision packet covers all relevant instruments and portfolio state.
4. A finite Codex execution authenticated with ChatGPT Pro invokes Sol. No OpenAI Platform API key and no paid fallback.
5. Sol receives no secrets or broker tools. It may return only schema-validated `TradeIntent` or HOLD, including rationale, evidence references, expiry, uncertainty, invalidation conditions, suggested stop, target, and size ceiling.
6. Canonical state lives in the database/audit log, not in chat memory. Runs are finite; a daily resumable session may be used only as a convenience and is rotated. The decision packet remains the source of truth.
7. The deterministic master risk layer rechecks broker truth, quote freshness, costs, portfolio exposure, loss limits, kill state, and audit availability. It can reject or reduce an intent but never relax policy.
8. Approved intents execute automatically only in isolated local shadow portfolios. Actual eToro DEMO writes continue to follow the official exact one-time owner-approval rule.
9. If Codex authentication, plan quota, market data, reconciliation, or audit is unavailable, no new AI-driven position opens. Deterministic risk-reducing exits remain possible.
10. Initial budget target: roughly 10–30 consolidated Sol decisions per active day, driven by meaningful events rather than a permanently running chat.

## Proposed development and validation roadmap

### Stage A — establish research truth

- Build a versioned raw/normalized data catalog with event time, source provenance, symbol mapping, sessions/DST, corporate actions, missing-data policy, and immutable snapshot hashes.
- Acquire enough intraday bid/ask or conservative spread proxies to represent eToro costs, financing, latency, partial fills, gaps, and slippage.
- Define benchmark strategies and a no-trade baseline.
- Pre-register strategy parameters and acceptance metrics before testing.

### Stage B — validate or eliminate deterministic strategies

- Run walk-forward and untouched out-of-sample tests across multiple regimes.
- Control for multiple testing, parameter search, data snooping, and correlated strategies.
- Use realistic costs and stress them above observed levels.
- Drop, merge, or redesign strategies rather than forcing all 12 to survive.
- Run live shadow ledgers long enough to reconcile simulated versus observable broker conditions.

### Stage C — add Sol as an experiment, not an assumption

- Implement the sanitized decision packet, strict output schema, time/usage budgets, and read-only Codex worker.
- Compare at least: deterministic baseline; Sol veto-only; Sol ranking; Sol intent generation; and deliberately simple statistical/ML alternatives.
- Use replayable frozen inputs so identical model/version/prompt runs can be evaluated for variance and reproducibility.
- Record model identifier, prompt/config hashes, input hashes, output, reasons, latency, usage, and downstream risk decisions without secrets.
- Require evidence that Sol improves net out-of-sample performance, calibration, or drawdown after its operational cost. Otherwise remove it from the live decision path.

### Stage D — operational hardening

- Complete PostgreSQL migration, backup/restore proof, reconciliation, crash recovery, UNKNOWN-order handling, health alerts, fault injection, and kill drills.
- Extend the dashboard with data quality, strategy version, backtest/OOS results, decision packets, model rationale, risk rejections, drift, and benchmark comparison.
- Run shadow-only soak tests. No automatic promotion.

### Stage E — future gates, not current scope

- Define statistically meaningful minimum observations, time-in-market, regime coverage, net OOS profitability, drawdown, reconciliation, security, and operational incident gates.
- Any eToro DEMO broker-write activation requires the existing owner approval semantics.
- Any real-money work requires a new explicit owner request, a separate release/configuration, a fresh security review, and no automatic switch from DEMO.

## Questions you must answer

1. Give one verdict: **PROCEED**, **PROCEED WITH MAJOR REDESIGN**, **PIVOT**, or **STOP**. Explain in at most 150 words.
2. Is eToro technically and economically suitable for USD 1,000 short-horizon trading after spreads, financing, minimum sizes, leverage rules, latency, API limitations, and approval requirements? If not, recommend a better research/execution architecture while keeping real money disabled.
3. Is an LLM likely to add measurable edge here? Identify the highest-value role for Sol: primary intent generator, veto/risk critic, regime classifier, research analyst, news interpreter, strategy selector, or no live role. Rank these options with evidence.
4. Should Sol runs be fresh, resumable per day, resumable per strategy, or stateless? Recommend exact trigger cadence, context construction, maximum calls/day, failure behavior, and model fallback policy under ChatGPT Pro limits.
5. Audit the 12 strategies. For each, mark **KEEP**, **MERGE**, **REDESIGN**, or **DROP**, and state the required data, target instruments/session, failure modes, and validation test.
6. Propose missing strategies or non-strategy baselines. Prefer a smaller, diverse hypothesis set over cosmetic variation.
7. Design a statistically defensible backtest and live-shadow protocol for this small account. Include sample-size reasoning, walk-forward design, untouched test sets, multiple-hypothesis correction, regime segmentation, cost stress, leakage controls, benchmarks, and promotion/retirement rules.
8. Propose a market-data stack and identify what may legally and practically be sourced from eToro versus an independent provider. Address timestamp alignment, bid/ask, spreads, financing, corporate actions, news, economic events, and licensing/retention.
9. Review all deterministic risk limits. Recommend provisional DEMO values for a future USD 1,000 master account and explain which constraints must be global across strategies. Include risk of ruin, gap/slippage limitations, correlated exposure, daily/weekly/monthly losses, order rate, overnight/weekend rules, and strategy allocation.
10. Threat-model the Sol/Codex boundary, prompt injection through market/news data, compromised dependencies, credential isolation, approval binding, audit integrity, kill behavior, and dashboard/authentication path.
11. Identify contradictions, false confidence, missing controls, overengineering, and components that should be removed.
12. Recommend whether to retain NautilusTrader, replace it, or expand its role. Compare only against concrete alternatives suitable for this project.
13. Define a minimum experiment that can falsify the thesis quickly and cheaply before more engineering is spent.
14. Provide a prioritized roadmap for the next 7 days, 30 days, and 90 days, with explicit deliverables and go/no-go gates.
15. List the 10 most important unanswered questions for the owner or broker.

## Required answer format

1. Executive verdict.
2. Five strongest aspects.
3. Ten highest-severity problems, with severity and evidence.
4. Scorecard from 0–10 for data, strategy science, AI design, backtesting, risk, execution, security, auditability, operations, and economic viability.
5. Recommended target architecture, preferably with a compact diagram.
6. Strategy keep/merge/redesign/drop table.
7. Sol role, scheduling, context, and evaluation design.
8. Data and backtesting plan.
9. Recommended provisional risk policy for USD 1,000 DEMO.
10. Seven-day, 30-day, and 90-day roadmap.
11. Explicit pivot option: what you would build instead from scratch and why.
12. Assumptions, unknowns, source links, and confidence level.

Be concrete and adversarial. Quantify recommendations where defensible. If evidence is insufficient, say `UNKNOWN` and specify the cheapest experiment needed. Do not claim that past performance, an LLM, or more complex infrastructure implies future profit.
