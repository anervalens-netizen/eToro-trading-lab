# Final audit remediation — v0.5.8

Scope: remediation of the GitHub-only audit comparing baseline `78ba99e1aeb856fc88f452a70df0492f2620a7bf` with candidate `4f105a2e8cc5f49016752894f42a1ca7ca081e27`.

The follow-up review of v0.5.6 found one additional activation-boundary issue:
`DECIDED` shadow packets could remain claimable after the execution gate was
created. v0.5.7 closes that path with durable authority mode/epoch binding.

## P0 disposition

| Finding | Disposition | Primary evidence |
|---|---|---|
| CLOSE provenance mismatch | Fixed | `domain_v2.py`, `kernel_v2.py`, `executor_v2.py`; full/partial broker-request tests in `test_v2_executor_recovery.py`; canonical full/partial CLOSE tests across the isolated signer socket in `test_v2_security_boundary.py` |
| Incomplete terminal reconciliation | Fixed | exact order lookup, close detail and history projection in `reconciliation_v2.py`; ACK-only, close/partial-close and broker SL tests in `test_v2_reconciliation.py` |
| Startup-only execution gate | Fixed | in-process checks in executor/applier/exit manager, atomic invalidation, `etoro-v2-execution-gate.path` and lock target; gate-removal test |
| AI-dependent exits | Fixed | independent `exit_manager_v2.py`; HOLD passes deterministic applier; time-stop-without-AI test |
| Multiple broker writers | Fixed | v1 unit/CLI writer entrypoints removed; provisioning masks/removes installed legacy unit; all write units conflict; boundary verifier asserts mask/inactive state |

## P1 and economic disposition

- Every AI packet is durably classified as `SHADOW` with no execution epoch or `EXECUTION` with the exact `v2_trading_state.version`. Queue and claim operations lock/check that authority against current state. On transition, decided packets from older authority are atomically moved to `EXPIRED` with an `AIPacketAuthorityExpired` event.
- The execution applier is inert until gate plus `ACTIVE` establish a current epoch. Kernel command persistence rechecks that epoch in the same transaction as command/order/reservation/outbox/event creation. The executor independently rejects both OPEN and reduce-only work in `LOCKED` before any broker request.
- Event idempotency stores/comparisons use canonical body and hash. Conflicts rollback the economic transaction and force `LOCKED`; PostgreSQL takes the chain advisory lock before lookup.
- `None` and zero reduce quantities are distinct; zero is rejected. Duplicate close fill replay resolves against historical closed positions.
- Broker/local fingerprints cover position/instrument/symbol/side/quantity/entry/exposure/costs plus pending/UNKNOWN order identity.
- AI inference and apply have independent ceilings and auditable `DEAD_LETTER`/`FAILED_TERMINAL` outcomes.
- OIL/NATGAS and unsupported multi-leg profiles remain in research but are absent from the execution profile set; the live-DEMO config loader rejects any incompatible/research-only profile.
- Historical/shadow adapters charge adverse exit slippage and elapsed financing; period P&L uses calendar baselines and shadow peak equity is durable.
- Market/intent/quote/position/order/fill/bar/cash/cost inputs reject non-finite economics, symbol mismatches and negative volume/pending cash.

## Operations and supply chain

- PostgreSQL schema v6; checked migration history. The market collector can write only its own heartbeat through a bounded `SECURITY DEFINER` function.
- Health uses a signed full-chain checkpoint plus bounded incremental verification, heartbeat/feed/queue/outbox/reconciliation/drift/backup/restore freshness and execution-lock state.
- Backup includes database, the complete offline dependency wheelhouse, release/config/public-key evidence and market catalog/archive. A fail-closed job copies immutable artifacts and signed anchors to a verified CIFS/NFS destination and records a freshness receipt. Restore verifies every sidecar, wheelhouse, release identity, chain and economic invariant, then starts the read model against the disposable database.
- CI runs on PR and push to main with full tests, coverage threshold, critical type checking, fault/restart tests, shell/systemd/SQL validation, dependency/security checks and secret guard.
- GitHub Actions and PostgreSQL service image are immutable-pinned. Runtime dependencies carry hashes; CI emits a CycloneDX SBOM plus an attested exact-SHA offline wheelhouse bundle. Install rejects commit/tree/lock/checksum mismatch, performs no dependency-network access and runs the full suite before symlink promotion.
- Repository version is 0.5.8, with changelog, explicit all-rights-reserved license, one canonical v2 runtime ADR, separate private-key recovery procedure, installed-wheel schema/resource gate, populated-database migration coverage, runtime-derived restore schema assertions, a privilege-separated atomic recovery marker and a systemd-managed off-host health state directory.

## Deliberately still blocked

The code remediation does not manufacture empirical trading evidence. A controlled,
operator-observed minimum DEMO smoke may run only after exact-release, credential,
boundary, health and zero-drift gates pass, and must return to `LOCKED` after the
OPEN/FILL/FULL-CLOSE/reconciliation proof. Unattended autonomous promotion remains
blocked until cost calibration, complete live DEMO fault drills, zero unexplained
drift, 30–60 days soak, preregistered sample/regime coverage and the one-time
untouched holdout all pass. REAL remains categorically unsupported.
