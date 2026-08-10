# v2 implementation status

## Implemented in code

- shared deterministic economic kernel across replay/shadow/live adapters;
- explicit ACK/fill/partial/UNKNOWN lifecycle and idempotent commands;
- mandatory quote provenance and global deterministic risk contract;
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
- separate market-read and executor-write credential/service boundaries;
- no REAL execution route/config/service.

## Requires deployment/runtime evidence

These items cannot be truthfully marked complete by committing code alone:

1. verify the repository CI on the final PR head;
2. run PostgreSQL integration tests against the actual server database;
3. provision/read broker DEMO credentials and verify current endpoint payloads against the account;
4. validate WebSocket topics/sequence behavior with the live account/feed;
5. calibrate actual eToro spread/slippage/financing distributions;
6. execute restore/fault drills in the server environment;
7. perform historical vs shadow parity on recorded real feed events;
8. run 30–60 day autonomous DEMO soak;
9. accumulate adequate closed trades/regime coverage;
10. consume the untouched OOS set once and evaluate promotion criteria.

Until these runtime/empirical gates pass, v2 is an implemented research/execution architecture, not a profitability or REAL-readiness claim.
