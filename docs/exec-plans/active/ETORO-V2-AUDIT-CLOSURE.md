# ETORO-V2-AUDIT-CLOSURE - Canonical V2 audit closure and synchronized deploy

Status: ACTIVE
Overall outcome: UNVERIFIED
Owner: primary Codex agent
Created: 2026-08-12T17:18:00+03:00
Updated: 2026-08-13T00:18:00+03:00

## Objective

Close the supplied 60-finding audit with one canonical V2-only DEMO runtime, merge an independently accepted exact SHA, publish its immutable release, deploy the same release to primary and passive Dell, verify safety/health, and remove redundant merged branches/worktrees/legacy services.

## Non-goals and limits

- REAL trading remains disabled, unsupported, and unrepresentable.
- Do not create the DEMO execution gate or claim empirical profitability/readiness without a promoted strategy manifest and required OOS/soak evidence.
- Do not expose credentials or delete the user audit file `eToro-trading-lab-audit-2872f0e6.md`.
- Persistent production data/backups remain protected; migrations and deploys must be rollback-safe.

## Sources

- User-supplied audit anchored at `2872f0e6e19298520092fa22a7c98dbb3cb90c6c` / v0.5.15.
- PR #19: `https://github.com/anervalens-netizen/eToro-trading-lab/pull/19`.
- `AGENTS.md`, `V2_ARCHITECTURE.md`, `V2_DEPLOYMENT.md`, `V2_SECURITY.md`.

## Baseline

- Repository root: `/opt/eToro`
- Host: `dell-standby`
- Branch and exact HEAD: `codex/v2-runtime-recovery` / `0dabcaf64fe2a6a8831aebe33cf5d0828deb6352`
- GitHub/local `main`: exact `0dabcaf64fe2a6a8831aebe33cf5d0828deb6352`, tag `v0.6.4`, no open PR; one worktree.
- Working tree: clean except protected untracked user audit `eToro-trading-lab-audit-2872f0e6.md`
- Protected newer work: merged releases `v0.6.0..v0.6.4`; user audit file above.
- Primary runtime is still rollback release `2872f0e6e19298520092fa22a7c98dbb3cb90c6c`, schema 6, `LOCKED`, gate absent, zero open positions, zero UNKNOWN orders, zero pending outbox and zero open reconciliation cases.
- Primary `/healthz` is 503/error: stale `v2-market` and error `v2-coordinator`; coordinator has watchdog restarts. Dell remains passive on the same old release.
- Mutable market index has 12 columns after the failed candidate run, while rollback release inserts 9 positional values. Raw WebSocket artifacts remain fresh, but the old index is stale and the collector silently reconnects.

## Minimal code map

- `ops/postgres/schema_v7.sql`: constrained runtime metadata namespaces.
- `src/etoro_agent/coordinator_v2.py`: actual `v2_coordinator_bar:{mode}:{symbol}` idempotency marker.
- `tests/test_v2_postgres_runtime.py`: real PostgreSQL role/authority and rollback acceptance.
- `.github/workflows/v2-ci.yml`: exact-SHA global/release gates.
- `ops/deploy/install-v2-release.sh`, `ops/deploy/provision-v2-host.sh`: rollback-safe immutable host cutover.

## Acceptance contract

| ID | Observable behavior | Must not change | Verification and expected result | Runtime proof | Evidence | Status |
|---|---|---|---|---|---|---|
| AC-1 | Candidate role can persist the exact coordinator bar marker once; unrelated metadata remains denied | least privilege and protected meta keys | real PostgreSQL SET ROLE test; exact key succeeds, unrelated key raises | coordinator healthy and no repeated same-bar packet budget after deploy | pending | UNVERIFIED |
| AC-2 | Final PR head has zero open P0-P2 and all CI gates green | no weakened gate | exact-head CI SUCCESS plus fresh independent auditor PASS | N/A | pending | UNVERIFIED |
| AC-3 | PR merges to main and one immutable final release targets the exact main SHA | no branch/release ambiguity or reuse of an older version | main CI SUCCESS; checksum, SBOM, bundle, provenance/attestation and GitHub release target match | release target equals primary deployed SHA | pending | UNVERIFIED |
| AC-4 | Primary uses the exact final V2-only release at schema 9 with no legacy runtime/processes | execution gate absent; REAL false; Dell topology is verified separately by AC-10 | installer/provision probes plus service/user/path/schema/health checks on primary | exact SHA/digest, schema 9, healthy allowed services and zero `etoro-agent` legacy processes | pending | UNVERIFIED |
| AC-5 | GitHub/local refs and worktrees contain no redundant merged audit branch/worktree | preserve unique user audit file | fetch/prune, worktree/branch/ref comparison | N/A | pending | UNVERIFIED |
| AC-6 | Empirical execution remains fail-closed without release/OOS/soak proof | no REAL and no execution gate | startup/executor promotion test and runtime gate probe | research/shadow only until evidence exists | pending | UNVERIFIED |
| AC-7 | Each PostgreSQL login identity can update only its exact owned heartbeat through the real `PostgresRuntimeStoreV2.heartbeat()` path | no direct heartbeat-table write for service roles; observer remains read-only; collector keeps only its constrained market function | disposable PostgreSQL with actual LOGIN identities/DSNs; call the method twice on the same key and assert one updated row for candidate=`v2-coordinator`, AI=`v2-role-apply`, decision=`v2-decision-shadow`, decision-exec=`v2-decision-apply`, exit=`v2-exit-manager`, reconciler=`v2-reconciliation`, executor=`v2-demo-executor`; every cross-role service spoof and direct table write fails; collector function succeeds/direct write fails; observer reads/direct write fails | active primary roles candidate/AI/decision/reconciler expose only their expected `session_user` and their owned heartbeats advance; inactive decision-exec/exit/executor identities remain read-only-probeable while their services stay inactive and heartbeat timestamps do not advance | pending | UNVERIFIED |
| AC-8 | Market index survives candidate -> exact v0.5.15 rollback writer -> candidate round-trip | no indexed/raw observation loss; immutable raw catalog preserved; live DB is never used as the test target | migrate 8-, 9- and current 12-column fixtures; candidate writes base+eligibility metadata, exact rollback positional writer succeeds, candidate writes again; `PRAGMA integrity_check=ok`, all original row/raw hashes preserved, eligibility rows remain correlated; verify referenced fixture raw bytes by SHA-256 | create an SQLite online backup copy of the live index, run the same round-trip only on the isolated copy, verify live index continues advancing and no market restart/error growth | pending | UNVERIFIED |
| AC-9 | Exact final primary release sustains the complete gate-absent research/shadow runtime for at least two coordinator intervals | gate absent, `LOCKED`; signer, `etoro-v2-decision-apply-execution`, `etoro-v2-exit-manager`, `etoro-v2-executor-postgres`, retired `etoro-v2-executor`, retired `etoro-v2-executor-current` and legacy `etoro-demo-executor` inactive with no matching process; historical terminal AI evidence preserved | health requires market/coordinator/reconciliation/role-apply/decision-shadow when gate absent and returns 503 for any error/staleness or dead letter in the last 15 minutes while exposing historical total separately; capture journal cursor and restart counters, then sample at start, >=65s and >=130s: required heartbeat timestamps and market index count advance, `/healthz` is 200 `locked`, and no new `V2_*_ERROR=`, traceback, error-priority records or restarts appear | primary exact SHA/schema 9; SQL counts are exactly zero for `v2_positions.status='OPEN'`, `v2_broker_orders.status='UNKNOWN'`, undelivered `v2_outbox`, open `v2_reconciliation_cases`; reconciliation remains `broker_position_count=0`, `broker_pending_order_count=0`, `economic_drift=[]` with nonempty broker snapshot hash; audit/backup/restore/off-host valid | pending | UNVERIFIED |
| AC-10 | Dell runs the same immutable release as bounded passive AI compute, without local PostgreSQL or broker execution authority | preserve the fixed remote `etoro-ai` identity; no local DB, eToro broker credential, signer, gate or executor activation | test a narrow passive sync procedure that verifies primary and staged recursive release digest plus `RELEASE.json`, atomically switches Dell, restarts only Sol model/socket/runner surfaces, and on every failed cutover restores the exact prior symlink/unit set and healthy prior services | Dell release SHA/tree/bundle digest equal primary; local PostgreSQL/execution inactive; a non-mutating remote wire probe returns the final release/schema and `session_user=etoro-ai`; Sol runner/socket healthy, no new errors, primary remains healthy and gate absent | pending | UNVERIFIED |

## Tasks and dependencies

| Task | Scope | Depends on | Owner | Attempts | State |
|---|---|---|---|---:|---|
| T1 | Fix candidate marker namespace and PostgreSQL regression | none | primary | 1 | PASS |
| T2 | Exact-head CI and fresh independent audit | T1 | independent auditor | 1 | PASS |
| T3 | Resolve thread, merge, main CI, immutable release | T2 | primary | 1 | PASS |
| T4 | Safe deploy and runtime proof on primary + Dell | T7 | primary | 4 | READY |
| T5 | Cleanup merged refs/worktrees and final synchronization proof | T4 | primary | 0 | READY |
| T6 | Close heartbeat-role and rollback-index runtime gaps with executable regressions | T3 | primary | 1 | VERIFYING |
| T7 | Fresh independent audit, exact-head CI, merge, exact-main CI and immutable release | T6 | independent auditor + primary | 0 | READY |

## Progress and transitions

- 2026-08-12T17:18:00+03:00 Baseline recorded; all criteria initialized UNVERIFIED. PR head `cbfbc8d6` had independent GO and CI `31621355595` SUCCESS, but a later automated review found the concrete candidate metadata prefix mismatch. T1 moved to BUILDING.
- 2026-08-12T17:24:00+03:00 T1 implementation complete: coordinator writes lowercase canonical `v2_coordinator_bar:{entry_review|position_review}:{symbol}`; DB permits only that candidate namespace. Real PostgreSQL candidate-role test and six coordinator contract tests passed. T1 moved to VERIFYING; AC-1 awaits independent authorization.
- 2026-08-12T20:34:00+03:00 Fresh auditor PASS on exact `9ec11f6c`; PR #19 merged as verified main `242aa471`; main CI `31622984683` SUCCESS and release v0.6.0 published.
- 2026-08-12T20:39:00+03:00 First guarded primary install failed before cutover: root-only filtered grants file was unreadable by postgres. Installer restored schema 6; old release/services remained healthy, LOCKED, gate absent. T4 remains BUILDING, attempt 1.
- 2026-08-12T20:43:00+03:00 Bootstrap ownership hotfix passed independent audit and exact-head/main CI, merged as verified `2523d23`, and v0.6.1 was published with provenance.
- 2026-08-12T20:50:00+03:00 Second guarded install reached schema 8, then the post-migration proof exposed root-to-control peer-auth mismatch. Schema marker rolled back to 6; no service/symlink cutover occurred. Exact `etoro-control` peer proof succeeded read-only on the live host; T4 attempt 2 remains BUILDING.
- 2026-08-12T20:57:00+03:00 Independent audit of the peer-auth delta found stale package/status versions. Package version now derives from installed release metadata, status is 0.6.2, and a release-surface regression binds package, distribution, project and documentation versions.
- 2026-08-12T21:05:00+03:00 Auditor found the same root-to-control peer mismatch in the installer's repeated cutover preconditions. All four state probes now use OS `etoro-control`; executable tests assert every invocation and a live read-only default-path probe on primary returned `CUTOVER_PRECONDITION_PEER_OK`.
- 2026-08-12T21:09:00+03:00 v0.6.2 bootstrap and schema 8 succeeded, then candidate market restart rejected the old 0.5.15 config missing `model_id`, `portfolio_id`, and `release_namespace`. Rollback restored release/schema and stopped read services fail-safe; primary manually restored all prior read services active under schema 6/LOCKED/gate absent.
- 2026-08-12T21:15:00+03:00 Runtime configs now stage with candidate units and are part of the same rollback transaction. Regression proves a second-service failure restores old symlink, units, both configs, schema and every previously active process.
- 2026-08-12T21:24:00+03:00 Independent audit found unchecked backup/restore failures. Unit/config writes are now atomic same-directory renames; every backup/install/restore result is explicit, recovery failure stops all affected services and preserves backup evidence. Injected backup/install failure tests plus the complete 23-test security boundary pass.
- 2026-08-12T21:30:00+03:00 Re-audit found unchecked temp creation and removal. Both unit/config stages now restore after temp failure and every restore removal is checked. Injected config/unit temp failures and restore-remove failure join backup/install/restart tests; security boundary is 26/26 PASS.
- 2026-08-12T21:37:00+03:00 Next audit found manifest creation/append and post-rename symlink rollback unchecked. Manifest lifecycle now uses explicit checked operations; partial-stage metadata failure stops services and preserves backups; symlink rollback is mandatory and checked. Cleanup failures are terminal. The executable post-rename race regression proves exact old target restoration; security boundary is 28/28 PASS.
- 2026-08-12T21:41:00+03:00 Re-audit found top-level promotion status collapsed by `if !`. Caller now preserves rc=1 (verified recovery) versus rc=2 (uncertain authority); rc=2 always stops read services and preserves unit/config backups. Security boundary is 29/29 PASS.
- 2026-08-12T21:46:00+03:00 Automated exact-head review found unit backups still discarded when config-stage unit restore failed. All unit/config/schema evidence is now retained for every uncertain recovery and discarded only after a verified rollback; regression asserts the cleanup branch separation.
- 2026-08-12T21:51:00+03:00 Final exhaustive audit found two bootstrap branches still deleting the schema receipt after failed restoration. Both now retain the receipt and stop services on uncertain recovery; cleanup occurs only after verified restore. Complete security boundary remains 29/29 PASS.
- 2026-08-12T22:34:00+03:00 Owner resumed with explicit end-to-end authorization and required Contract-Build-Prove. Baseline shows v0.6.4 was not promoted: rollback release/schema are active and fail-closed, but user-facing health is 503. T6 started with AC-7..AC-9 UNVERIFIED; no execution gate or broker write is authorized by this recovery.
- 2026-08-12T22:44:00+03:00 Fresh pre-build auditor verdict FAIL. AC-7 allowed cross-role heartbeat spoofing; AC-9 omitted role-apply/shadow-decision and could never reach HTTP 200 with four historical dead letters; AC-8 lacked a full candidate/rollback/candidate isolated-copy round-trip; Dell acceptance contradicted its active bounded remote AI runner and had no passive sync path. Contract expanded to AC-7..AC-10 and remains UNVERIFIED; no implementation started before re-critique.
- 2026-08-12T23:02:00+03:00 Auditor re-critique remained FAIL because AC-7 treated intentionally inactive execution roles as live, AC-4 still mixed obsolete schema/Dell assumptions, AC-9 lacked exact inactive and zero-state predicates, and AC-10 lacked failed-cutover rollback plus a functional remote-role proof. Contract now has an explicit primary/Dell topology matrix, exact inactive units, exact economic-zero predicates, digest equality and rollback/read-only remote proof; implementation is still paused for re-critique.
- 2026-08-12T23:08:00+03:00 Second re-critique PASS with zero P0/P1/P2. Implementation started only after contract authorization.
- 2026-08-12T23:36:00+03:00 Coherent v0.6.5 candidate implemented: schema-9 login-bound heartbeat function and real-DSN regression; rollback-compatible market base/eligibility split and previous-grant rollback; complete gate-absent health; signer lock; non-mutating AI wire status; rollback-tested passive Dell sync. Targeted market/health/security/release tests, Ruff and mypy pass; all 13 real PostgreSQL integration tests pass. An online backup of the untouched 123003-row live 12-column index completed candidate -> v0.5.15 positional writer -> candidate with integrity `ok`, 123005 correlated rows and a referenced raw SHA-256 match. T6 remains BUILDING pending global gate and final audit.
- 2026-08-13T00:02:00+03:00 Global local evidence is complete on unchanged content: all 233 non-PostgreSQL tests passed under coverage; the first combined run lost its disposable PostgreSQL container after five PostgreSQL successes and produced only eight `AdminShutdown` infrastructure errors; the separately completed PostgreSQL gate is 13/13 PASS. Combined test inventory is 246, coverage 68%, compile/mypy 73 modules/Node/Ruff lint+format/Bandit/pip-audit/pip-check/ShellCheck all PASS. T6 moved to VERIFYING for fresh exact-candidate audit.
- 2026-08-13T00:12:00+03:00 Fresh exact-candidate audit of `5e726b4` returned FAIL: AC-10 armed outer rollback only after the switch returned, did not stop candidate template instances during rollback, and omitted the API key/most local PostgreSQL DSNs from Dell absence checks; P2 requested direct public HTTP 503 coverage for recent dead letters. No PR/deploy occurred.
- 2026-08-13T00:18:00+03:00 AC-10 repair completes a read-only backup/validation phase before arming rollback, explicitly checks every subsequent mutation, restores on injected install/daemon/symlink/restart failures, stops candidate model/runner/socket before prior-unit restore, and rejects API key plus any local PostgreSQL DSN. Five pre-restart failure modes and restart rollback pass; recent-dead-letter `/healthz` response is directly proven 503. T6 remains VERIFYING for re-audit.

## Attempts, failures, and discoveries

- Prior head `66bc594` failed independent acceptance on incomplete schema rollback; `cbfbc8d6` added marker-scoped legacy economic compatibility and exact baseline runtime transaction proof, then received independent GO.
- Latest head review found `schema_v7.sql` permits `last_coordinated_bar:` while production coordinator writes `v2_coordinator_bar:`; same-bar idempotency is therefore not operational under the candidate role.
- T1 attempt 1 produced measurable closure: actual marker write/read succeeds through `PostgresRuntimeStoreV2.state_set()` under candidate SET ROLE; legacy/invalid/critic keys raise; coordinator contract remains 6/6 green.
- T4 attempt 1 safely failed before promotion at `psql -f` with `Permission denied`; rollback evidence showed current still `2872f0e6`, schema 6, state LOCKED, gate absent, read services active. Root cause: filtered bootstrap grant file retained mktemp root ownership while psql intentionally runs as postgres.
- T4 attempt 2 safely failed before promotion when root attempted peer authentication as PostgreSQL `etoro-control`. Root cause: the post-migration proof used the control DSN without adopting the matching OS identity. Live read-only reproduction under OS `etoro-control` returned `LOCKED`.
- Peer-fix audit found version drift (`__version__=0.2.0`, status 0.6.0) while packaging was 0.6.2; this P2 was closed by deriving the package version from distribution metadata and testing all release surfaces together.
- Peer-fix re-audit found four installer state probes still running as root. They now use the same exact `etoro-control` OS/DB identity; three cutover/bootstrap regressions and the live default-path precondition pass.
- T4 attempt 3 reached candidate service restart and exposed config/unit migration ordering. No new runtime was left active; schema/release rolled back and read services were recovered. Configs now have explicit stage/backup/restore lifecycle within installer rollback.
- Config-cutover audit rejected the first delta because Bash conditional contexts suppressed implicit errexit and some restores were ignored. The implementation now avoids implicit errexit, distinguishes recovered failure from recovery failure, preserves failed-recovery backups, and tests injected I/O failures.
- The next re-audit found `mktemp`/`rm` gaps after partial stage. These are now explicit fail-closed transitions; there are no unchecked filesystem mutations in unit/config stage or restore paths.
- Audit then found transaction-metadata and symlink-rollback gaps. Both are now explicit: no stage starts without a backup manifest, every manifest append is verified before overwrite, and a post-rename race cannot be reported recovered until the old symlink is restored.
- Top-level promotion handling now preserves the uncertain-failure status instead of collapsing it, so old units/config/schema can never be restarted against an unverified current symlink.
- Recovery evidence lifetime is now tied to verified recovery: no unit/config backup or schema receipt is deleted on an uncertain stage, promotion, or restart rollback.
- The same evidence-lifetime invariant now covers bootstrap failure and post-bootstrap precondition failure; no installer branch discards the schema receipt after a failed restore.
- Candidate v0.6.4 failed readiness because all direct heartbeat roles except executor have INSERT+UPDATE but lack SELECT required by the real `INSERT ... ON CONFLICT DO UPDATE` statement. Existing privilege assertions did not call the production method under each role.
- The safe rollback restored Git/unit/config/schema authority, but not mutable SQLite shape: candidate initialization expanded `market_archive_v2` from 9 to 12 columns and exact v0.5.15 uses positional 9-value INSERT. The swallowed WebSocket exception produces repeated auth/subscribe/snapshot raw artifacts while the index and market heartbeat remain stale.
- Direct table heartbeat grants also permit one compromised service to overwrite another service's primary-key row. Repair must bind one PostgreSQL login to one service name, not merely add SELECT.
- Gate-absent health currently requires only market/coordinator/reconciliation and turns every historical dead-letter warning into HTTP 503. Required shadow workers and recent-vs-total dead-letter semantics must be explicit.
- Dell is passive for broker/runtime authority but intentionally active as bounded Sol/AI compute using the primary `etoro-ai` role. Synchronization must preserve that lane while keeping local PostgreSQL, broker credentials and execution authority absent.

## Decisions

- Permit only the exact production marker namespace `^v2_coordinator_bar:(POSITION_REVIEW|ENTRY_REVIEW):[A-Z0-9._-]+$`; do not retain unused broader legacy prefix.
- Verify through `PostgresRuntimeStoreV2.state_set()` under the actual candidate role, not a string-only test.
- Keep the execution gate absent throughout recovery. Completion means a healthy research/shadow runtime, not DEMO OPEN activation or a profitability claim.
- Preserve backward write compatibility for the immutable rollback release at the mutable market-index boundary; do not delete or truncate the live index to make the candidate pass.
- Exercise exact service code under exact PostgreSQL roles; static `has_table_privilege` assertions are supporting evidence only.
- Preserve historical dead letters; only recent terminal AI failure affects readiness. Never delete production rows to obtain health 200.
- Treat Dell as passive AI compute, not an inert filesystem copy. A supported synchronization path may restart only its model/runner surfaces and must not provision local database or broker execution.

## Auditor verdicts

- `cbfbc8d6`: PASS before the later automated prefix finding; P0/P1/P2 = 0 for the audited rollback delta. A fresh verdict is required after T1.
- `9ec11f6c`: PASS for AC-1/AC-2/AC-6; exact PR CI `31622332818` SUCCESS; P0/P1/P2 = 0.
- Pre-build AC-7..AC-9 critique at baseline `0dabcaf`: FAIL; largest gap was role-owned heartbeat trust and complete health coverage.
- First AC-7..AC-10 re-critique: FAIL; largest gap was an exact primary/Dell active-versus-inactive topology matrix. The second revision awaits re-critique.
- Second AC-7..AC-10 re-critique: PASS; P0/P1/P2 = 0. Implementation authorized.
- Exact `5e726b4` final implementation audit: FAIL; AC-10 P1 transaction/authority gaps and one HTTP-level P2. Repaired in the next candidate; re-audit required.

## Evidence index

- Prior PR CI: run `31621355595`, job `94196713574`, 228 tests, 69% coverage, mypy 73 modules.
- Prior rollback audit: independent GO on `cbfbc8d6`.
- AC-1 local evidence: `test_service_grants_preserve_economic_owner_and_audit_fail_safe` PASS on PostgreSQL 18.4 exact CI digest; `tests.test_v2_coordinator_contract` 6/6 PASS; Ruff PASS.
- AC-2: fresh auditor PASS and PR CI run `31622332818`.
- AC-3: merged main `242aa4716bc1ed2c1e778c07a5bfc4ce278b5987`; main CI run `31622984683` SUCCESS; v0.6.0 bundle digest `ae290493...53c01b`, one GitHub attestation.
- Bootstrap hotfix: independent PASS on `3adea041`; merged main `2523d23c0a9f81b3a1dd2f105f4678dd3a9742cb`; main CI `31624023489` SUCCESS; v0.6.1 bundle digest `d30fab74...a7b8`, one GitHub attestation.
- T6 targeted: market/health/security/release regressions PASS; Ruff PASS; mypy 73 modules PASS; PostgreSQL 18.4 integration 13/13 PASS, including exact LOGIN DSNs, repeated same-key updates, all cross-role spoof/direct-write denials, collector function and observer read-only checks.
- T6 global local: 233 non-PostgreSQL tests PASS plus 13 PostgreSQL tests PASS; total inventory 246; coverage 68%; compile, mypy, Node syntax, Ruff lint/format, Bandit, dependency audit/consistency and ShellCheck PASS. The interrupted combined container run is not counted as a pass.
- AC-8 isolated live-copy proof: source shape 12 and 123003 rows; final shape 9 plus companion, 123005/123005 correlated rows, `PRAGMA integrity_check=ok`; referenced raw artifact SHA-256 matched `b85d997a...556aa`. Live source was opened read-only through SQLite online backup and not used as a test target.

## Integration, regression, and deployment

- Integrated diff: PASS on merged `242aa471`
- Global checks: PASS on PR and main
- Published artifact/SHA: v0.6.0 / `242aa471`; not deployed because guarded installer exposed a bootstrap permission defect
- Runtime health/logs/user flow: UNVERIFIED

## Risks and remaining work

- Final candidate SHA will change once for T1; all acceptance and release evidence must bind the new SHA.
- Host cutover changes service identities already implemented in the candidate; deployment must use the guarded installer and remain execution-disabled.

## Next exact step

Commit the frozen candidate, obtain a fresh independent acceptance verdict against its exact SHA and raw evidence, then run exact-head PR CI. Merge/publish only after PASS and green CI.

## Resume procedure

1. Read `AGENTS.md`, this plan, and relevant V2 architecture/deployment docs.
2. Recheck HEAD, branch, working tree, PR head/checks, and both hosts; preserve the untracked audit file.
3. Continue from `Next exact step`; do not merge until final exact-head CI and a fresh independent PASS.
