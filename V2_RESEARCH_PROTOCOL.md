# V2 research and promotion protocol

## Truth contract

No strategy has edge by assumption. Every run binds point-in-time dataset,
feature schema, dynamic calendar, observed cost model, candidate engine and
parameters, simulator, prompt/model, code SHA and config hash. Timestamps are
event-time/received-time aware; future data and silent backfill are forbidden.

## One strategy artifact

`CandidateEngineV2` is the only candidate implementation used by backtest,
replay, shadow and coordinator. A compatibility facade may delegate to it but
cannot implement separate rules. Golden batch hashes prove identical inputs and
outputs. Generic `signal_factory` paths are benchmark-only and are always
promotion-ineligible.

Sol roles are evaluated as veto/rank/critic ablations over the same candidates.
They never create executable terms. Keep an AI lane only if it adds net OOS
value after inference cost without worsening risk or unresolved execution.
Its `self_reported_confidence` and `self_reported_uncertainty` are explicitly
uncalibrated telemetry and have no sizing, risk or execution authority.

## Evidence gates

Promotion requires all of:

- chronological walk-forward folds and one untouched final holdout;
- point-in-time dataset and no survivorship/look-ahead leakage;
- trial inventory, Deflated Sharpe, PBO and Reality-Check-style result;
- identical canonical candidate hashes across historical and shadow paths;
- adverse execution corpus: reject, partial fill, gaps, calendar/events and drift;
- at least 100 closed trades, at least 30 shadow days, preferably 60;
- zero unresolved `UNKNOWN`, critical reconciliation or parity incidents;
- positive untouched OOS result within risk limits;
- explicit `PROMOTE` decision and non-revoked finite validity window.

## Cost model

A promoted release contains observed p95 round-trip cost per executable symbol,
including spread, slippage, fees and financing where applicable. It requires at
least 100 observations, observation age no more than 30 days and stress multiple
at least 2x. The exact values, sample, timestamp, model ID and hash are carried
into ranking and each execution plan.

Shadow may use the conservative static proxy, labeled
`shadow_only_provisional_heuristic`. EXECUTION may not. The deterministic raw
score is explicitly uncalibrated and is never described as expected payoff,
win probability or proof of alpha.

## Deployment release

`StrategyReleaseManifestV2` binds engine, parameters, features, calendar, cost
model, dataset, simulator, OOS, promotion and soak evidence. A separate
root-owned trust file pins its SHA-256. Coordinator, decision committer and
executor each verify current release identity. Missing, stale, revoked or
incomplete evidence blocks OPEN.

The repository intentionally contains no fabricated promoted manifest. The
current platform is suitable for research/shadow evidence collection; DEMO OPEN
remains NO-GO until the empirical gates are genuinely satisfied.
