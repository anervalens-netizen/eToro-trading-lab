# V2 status — 0.6.7 canonical runtime

## Implemented

- v1 source, `etoro-agent`, legacy configs/schema/tests/units and install surface removed;
- one inspection-only CLI and one PostgreSQL DEMO writer;
- strict configs/codecs/domain invariants; exact bool, finite numbers, unknown fields rejected;
- one immutable broker account snapshot per decision and foreign/copy/manual fail-closed;
- `LOCKED` lock-new with gated reduce-only; gate absent is absolute writer freeze;
- bounded poison-outbox quarantine/dead-letter behavior and continued FIFO processing;
- serialized concurrent fills with position lock/hash-CAS; atomic peak equity;
- least-privilege OS/PostgreSQL roles per service; shadow/execution decision identities are
  separate; heartbeat writes are bound to exact login identity; retired engine role cannot
  login/connect;
- one canonical `CandidateEngineV2` across backtest/parity/shadow/coordinator;
- signed strategy-release gate for OOS, promotion, soak, simulator, calendar and observed costs;
- dynamic finite-lived calendar and WS connection-epoch/full-snapshot eligibility;
- rollback-compatible nine-column market index plus companion eligibility metadata;
- strict broker response/identity/pagination and canonical request-byte contracts;
- authenticated REST/WS redirects forbidden; OPEN seal, expiry and execution epoch rechecked
  immediately before each possible broker-write boundary;
- shared cross-process rate limiter, exit priority and instrument close precision rules;
- bounded/attested ChatGPT Codex model sandbox with no Platform API fallback;
- exact-SHA CI, all-module type checking, full tests, PostgreSQL concurrency, secret scan,
  reproducible wheel, production allowlist, SBOM and provenance;
- immutable offline install, legacy masks, backup/off-host/restore and signed audit anchors;
- no REAL route/config/service.

Gate-absent health requires the full market/coordinator/AI/shadow/reconciliation lane.
Historical dead letters remain visible but only terminal failures from the last 15 minutes
degrade readiness.

## Empirical gate status

No repository artifact claims profitable OOS edge, successful promotion or a
30–60 day soak. No trusted strategy manifest is shipped. Therefore:

- local research/shadow: eligible after boundary and health verification;
- unattended eToro DEMO OPEN: **NO-GO until real evidence manifest exists**;
- strict reduce-only DEMO exit: architecture-ready only with gate retained and broker-bound position;
- REAL: **NO-GO and unsupported**.

Default deployment remains `LOCKED`, execution gate absent, writer services
inactive and zero automatic promotion. Release/host evidence is recorded after
merge and deploy; this source document does not pretend a candidate SHA is
already deployed.
