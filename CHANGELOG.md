# Changelog

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
