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
- current eToro DEMO eligibility/cost/open/close gateway with local conservative rate limits;
- PostgreSQL production state/event/outbox runtime and SQLite replay/reference store;
- signed audit-chain anchors, backups/restore drills, owner-only dashboard;
- research registry, untouched holdout, DSR/PBO/Reality-Check helpers, parity, promotion/retirement and soak gates;
- PR-only/manual CI with full unit suite plus focused v2 lint/security/dependency checks;
- separate market-read and executor-write credential/service definitions;
- no REAL execution route/config/service.

## Critical audit disposition (`78ba99e1aeb856fc88f452a70df0492f2620a7bf`)

- C0-01 OS isolation: **open**; requires the owner-authorized identity deployment and negative permission tests described below.
- C0-02 atomic proposal/pending/reservation: implemented in both stores; PostgreSQL concurrency proof remains a runtime gate.
- C0-03 stale send recovery: leased outbox recovery moves orphaned `SUBMITTING` to `UNKNOWN`/`HALT_NEW` without retry; server fault drill remains a runtime gate.
- C0-04 final-quote re-risk: signed entry band, direction, slippage and fresh cost-inclusive dollar-loss checks are implemented and property-tested; live broker payload and instrument precision validation remain runtime gates.

## Requires deployment/runtime evidence

These items cannot be truthfully marked complete by committing code alone:

1. provision distinct collector/signer/executor OS identities, isolate their state and credentials, disable signer networking and pass negative permission tests;
2. verify the repository CI on the final PR head;
3. run PostgreSQL integration and concurrent-reservation tests against the actual server database;
4. provision/read broker DEMO credentials and verify current endpoint payloads against the account;
5. validate WebSocket topics/sequence behavior with the live account/feed;
6. calibrate actual eToro spread/slippage/financing distributions;
7. execute restore/fault drills in the server environment;
8. perform historical vs shadow parity on recorded real feed events;
9. run 30–60 day autonomous DEMO soak;
10. accumulate adequate closed trades/regime coverage;
11. consume the untouched OOS set once and evaluate promotion criteria.

The checked-in service units still share the `etoro-agent` OS identity, so the unattended v2 executor must remain disabled. Until every runtime/empirical gate passes, v2 is an implemented research/execution architecture, not a profitability or REAL-readiness claim.
