# Changelog

## 0.6.0 - 2026-08-12

- remove the complete installable/runtime V1 surface and retain only a forensic Git tombstone;
- make `etoro-v2` inspection-only and PostgreSQL the sole operational authority/writer;
- enforce strict configuration, broker/domain schemas, one immutable account snapshot and canonical broker request bytes;
- split OS and PostgreSQL authority per collector, candidate, AI, decision, exit, reconciler, control, executor and observer;
- preserve gated reduce-only CLOSE in `LOCKED`, quarantine poison outbox rows, serialize fills and update peak equity atomically;
- unify backtest/parity/shadow/live candidates behind one hashed `CandidateEngineV2`;
- require a signed, finite-lived empirical strategy release with OOS/promotion/soak/calendar/simulator and per-symbol observed-cost evidence before OPEN;
- persist WebSocket connection epoch/full-snapshot eligibility and fail closed on contract/calendar/foreign-activity drift;
- attest the exact ChatGPT Codex account/executable/model boundary and forbid Platform API fallback;
- validate exact PR-head/main SHA with full module typing, PostgreSQL concurrency, reproducible wheel, allowlist, SBOM and provenance.
- isolate shadow and execution decision UIDs, PostgreSQL roles, DSNs and signer access; bind every signed OPEN and outbox item to the exact active epoch;
- reject authenticated REST/WebSocket redirects and fail closed on conflicting broker identity, economic, timestamp, account and market-data aliases;
- recheck command expiry/seal/epoch immediately before broker writes and clear stale pre-submit classifications on every new outbox lease.

## 0.5.15 - 2026-08-12

- retire the enabled v1 Sol/MiniMax polling services and detached Dell runtime;
- remove their installable systemd and package entrypoints while retaining only offline forensic helpers;
- make every v2 release install stop and remove stale v1 AI units so they cannot return on restart or reprovision;
- document the immutable v2 Sol runner/socket/model boundary as the only live AI authority.

## 0.5.14 - 2026-08-12

- bind every deterministic candidate to its exact feature/market evidence even when the strategy emits no extra event reference;
- expose a clearly non-probabilistic payoff proxy, stressed cost and basis-point units so the critic cannot infer units or mistake the raw score for calibrated expected value;
- label current eToro DEMO cash as broker truth and omit the unrelated research initial-cash scale from live model context;
- cover the exact live packet shape and retain the critic as a veto-only safety boundary.

## 0.5.13 - 2026-08-12

- replace the impossible `NoNewPrivileges` plus setuid-sudo nesting with a root-owned socket-activated model sandbox;
- pin the protected runner to an explicit read-only SSH known-host file and preserve terminal versus retryable model/AF_UNIX failure classes;
- keep the coordinator-facing runner non-privileged and credential-blind while the one-request worker sees only ChatGPT auth, model scratch space and fixed executables;
- align all three role schemas with the supported structured-output subset and move size/range/economic enforcement into local fail-closed validators;
- cover the strict socket protocol, exact direct Codex argv, all three live schema handshakes and systemd isolation contract.

## 0.5.12 - 2026-08-12

- serialize the configured capital mandate into strict packet-safe primitives before building live coordinator packets;
- cover the exact live portfolio context through deterministic candidate and decision-packet construction.

## 0.5.11 - 2026-08-12

- connect the configured executable `simple_statistical_baseline` family to the canonical coordinator candidate generator;
- preserve deterministic net-of-cost filtering and broker-compatible plan selection before any AI packet can be queued;
- cover the EURUSD baseline candidate path with a regression.

## 0.5.10 - 2026-08-12

- accept the current numeric eToro DEMO cost component field while retaining strict legacy compatibility and disagreement rejection;
- handle the live WebSocket transport heartbeat as systemd liveness without overstating market-data health;
- require a deadline-bound, ID- and order-bound authentication/subscription handshake before market data, with strict topic/instrument/sequence identity and alias validation;
- capture every non-heartbeat frame before parsing, finish complete multi-message envelopes before gap resynchronization, and bind each accepted event to its exact immutable raw wire artifact;
- cover the observed live response contracts and fail-closed drift paths with regressions.

## 0.5.9 - 2026-08-12

- bind every inference claim to the current gate-aware `SHADOW` or `EXECUTION` authority epoch;
- expire pending/error packets from closed authority epochs before model budget is claimed;
- prove the boundary with PostgreSQL coverage for zero stale budget consumption and one current-epoch claim.

## 0.5.8 - 2026-08-12

- derive the restore-drill schema expectation from the installed runtime instead of a stale hardcoded migration version;
- add a regression contract that rejects fixed schema literals in the canonical restore gate.

## 0.5.7 - 2026-08-12

- bind every canonical AI packet to either broker-write-free `SHADOW` authority or one durable `EXECUTION` trading-state epoch;
- atomically quarantine decided packets from closed authority epochs before any execution claim;
- require the same active execution epoch at deterministic command commit and reject reduce-only commands while `LOCKED`;
- add PostgreSQL migration and regression coverage for the shadow-to-execution readiness boundary.

## 0.5.6 - 2026-08-12

- accept canonical full CLOSE commands in the isolated socket signer while preserving strict partial-close quantity provenance;
- test OPEN, partial CLOSE and full CLOSE across the production signer IPC boundary;
- allow broker-write-free shadow packet generation in canonical `LOCKED` state when the execution gate is absent.

## 0.5.5 - 2026-08-12

- move the off-host freshness receipt into a dedicated systemd-managed state directory readable by the observer instead of a non-traversable legacy parent.

## 0.5.4 - 2026-08-12

- publish successful restore-drill freshness through a root-only atomic post-step while the PostgreSQL drill itself remains unable to modify backup archives.

## 0.5.3 - 2026-08-12

- aligned the disposable restore drill with the canonical JSON-backed position quantity schema and added a contract regression assertion.

## 0.5.2 - 2026-08-12

- made the v3 integrity migration atomically backfill populated append-only event tables and added a real PostgreSQL v2-to-v5 upgrade regression test.

## 0.5.1 - 2026-08-12

- packaged the legacy PostgreSQL schema for non-editable wheel installs and added an offline installed-wheel resource gate to CI/release creation.

## 0.5.0 - 2026-08-11

- repaired signed full/partial CLOSE execution and duplicate reduce-fill replay;
- added exact order/history reconciliation and continuous broker-side SL/TP projection;
- added dynamic execution gate, atomic lock/invalidation, systemd path isolation and legacy writer retirement;
- added deterministic exit manager independent of AI decisions;
- hardened event idempotency, PostgreSQL serialization, economic fingerprints and AI dead-letter flows;
- made commodities research-only under the current 1,000 USD mandate;
- added exit slippage, financing, calendar P&L and durable shadow peak equity;
- expanded health freshness/SLO checks and complete checksum/restore/application smoke drills;
- removed the installable v1 broker-writer unit and CLI entrypoints;
- added push-to-main CI, coverage/type/shell/systemd/SQL gates, pinned CI dependencies, hashed runtime lock, SBOM, independently verifiable artifact/internal manifests and attested offline release provenance.

## 0.4.0 - 2026-08-11

- introduced the PostgreSQL v2 unified kernel, isolated signer, AI queue, immutable release flow, backup/restore and shadow runtime.
