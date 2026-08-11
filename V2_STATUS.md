# v2 implementation status

## Implemented in code

- shared deterministic economic kernel across replay/shadow/live adapters;
- explicit ACK/fill/partial/UNKNOWN lifecycle and idempotent commands;
- mandatory quote provenance, signed execution bands and final-quote max-loss re-risk including broker costs;
- atomic command/pending-order/risk-reservation/outbox/event bundles in SQLite and PostgreSQL;
- deterministic stop/target/time/invalidation exits including gap-through-stop;
- exact-Decimal fills/positions/P&L/financing;
- compact strategy families, real Wilder ADX support, true relative-value research contract;
- quantitative commodity release surprise and term-structure carry research;
- immutable data catalog, calendars, structured events, WebSocket raw archive;
- cost calibration/stress, feature snapshots and tradability gate;
- simple regularized statistical benchmark;
- stateless AI roles, strict schemas, immutable packet hashes, lease/claim tokens, daily budgets and run telemetry;
- autonomous coordinator + portfolio decision applier + deterministic risk bridge;
- concurrent complete-batch market collection with capture/quote skew and correlated-bar barriers;
- model OPEN decisions bound to one deterministic broker-compatible candidate plan; the model cannot invent symbol, side, sizing, stop, target, horizon or slippage;
- current eToro DEMO eligibility/cost/open/close gateway with local conservative rate limits;
- PostgreSQL production state/event/outbox runtime and SQLite replay/reference store;
- checksummed PostgreSQL migration graph, distinct engine/executor/observer roles and a real concurrent-reservation integration test;
- signed audit-chain anchors, backups/restore drills, owner-only dashboard;
- research registry, untouched holdout, DSR/PBO/Reality-Check helpers, parity, promotion/retirement and soak gates;
- PR/push-main CI with coverage, critical-lifecycle type checking, full unit/fault/restart suite, shell/systemd/SQL, security and dependency gates;
- distinct collector/engine/signer/executor/observer OS identities, isolated Unix-socket signer and negative runtime boundary probe;
- broker-write-free shadow decision service separated from the gate-controlled execution applier;
- immutable exact-SHA releases, pinned runtime lock, systemd readiness/watchdogs and automated disposable restore drill;
- no REAL execution route/config/service.

## Final lifecycle audit remediation

- full and partial CLOSE now carry independent signed reduce provenance and reach the exact broker request path;
- ACK-only opens, exact terminal close fills and later broker-side SL/TP are reconciled from order lookup/history;
- the execution gate is rechecked at every write boundary and its removal atomically locks and invalidates unstarted work;
- mandatory exits run in an independent deterministic worker; AI `HOLD` is consumed through the deterministic applier;
- the legacy executor unit/CLI entrypoints are removed; provisioning masks any previously installed copy and verifies one-writer state;
- event duplicate-body conflicts rollback and lock; PostgreSQL checks under its advisory chain lock;
- inference/apply queues terminate poison packets in `DEAD_LETTER`;
- backtest/shadow costs, financing, period P&L and peak equity are no longer lifetime/entry-only approximations;
- health uses signed chain checkpoints plus bounded incremental verification, service/data/queue/reconciliation freshness and backup/restore evidence;
- PostgreSQL schema version is 5 and package version is 0.5.4.

## Critical audit disposition (`78ba99e1aeb856fc88f452a70df0492f2620a7bf`)

- C0-01 OS isolation: implemented through distinct identities, root-only credentials, a no-network signer with `SO_PEERCRED`, separated DB roles and executable negative tests; each deployed host must still pass `ops/security/verify-v2-boundaries.sh`.
- C0-02 atomic proposal/pending/reservation: implemented in both stores and covered by a real PostgreSQL concurrent double-spend test.
- C0-03 stale send recovery: leased outbox recovery moves orphaned `SUBMITTING` to `UNKNOWN`/`HALT_NEW` without retry and is fault-tested.
- C0-04 final-quote re-risk: signed entry band, direction, slippage and fresh cost-inclusive dollar-loss checks are implemented and grid/property-tested; live payload/precision calibration remains empirical.

## Requires deployment/runtime evidence

These items cannot be truthfully marked complete by committing code alone:

1. verify repository CI on the final candidate SHA;
2. provision a separate eToro `Environment=Demo, Permission=Read` user key; a write-capable key is rejected by collector services;
3. validate WebSocket topics/sequence behavior with that live read-only identity;
4. calibrate actual eToro spread/slippage/financing distributions;
5. perform historical vs shadow parity on recorded real feed events;
6. run the 30–60 day autonomous DEMO soak;
7. accumulate adequate closed trades/regime coverage;
8. consume the untouched OOS set once and evaluate promotion criteria.

The executor and execution decision applier remain disabled unless the explicit DEMO gate exists. Until every empirical gate passes, v2 is a hardened DEMO research/execution architecture, not a profitability or REAL-readiness claim.
