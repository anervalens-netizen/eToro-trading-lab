# ETORO-V2-AUDIT-CLOSURE - Canonical V2 audit closure and synchronized deploy

Status: COMPLETE
Overall outcome: PASS
Owner: primary Codex agent
Created: 2026-08-12T17:18:00+03:00
Updated: 2026-08-13T02:22:00+03:00

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
| AC-1 | Candidate role can persist the exact coordinator bar marker once; unrelated metadata remains denied | least privilege and protected meta keys | real PostgreSQL SET ROLE test; exact key succeeds, unrelated key raises | coordinator marker/heartbeat observed and advancing | exact-role CI plus live auditor | PASS |
| AC-2 | Final PR head has zero open P0-P2 and all CI gates green | no weakened gate | exact-head CI SUCCESS plus fresh independent auditor PASS | N/A | PR CI `31649026918`; final audit 0/0/0 | PASS |
| AC-3 | PR merges to main and one immutable final release targets the exact main SHA | no branch/release ambiguity or reuse of an older version | main CI SUCCESS; checksum, SBOM, bundle, provenance/attestation and GitHub release target match | release target equals primary deployed SHA | v0.6.11 `6d8f23a`; CI `31649234353`; bundle `1b8ae3a...00014` | PASS |
| AC-4 | Primary uses the exact final V2-only release at schema 11 with no legacy runtime/processes | execution gate absent; REAL false; Dell topology is verified separately by AC-10 | installer/provision probes plus service/user/path/schema/health checks on primary | exact SHA/digest, schema 11, healthy allowed services and zero legacy processes | exact live release/schema/service audit | PASS |
| AC-5 | GitHub/local refs and worktrees contain no redundant merged audit branch/worktree | preserve unique user audit file | fetch/prune, worktree/branch/ref comparison | N/A | zero open PRs; only `main`; one worktree/host; audit preserved | PASS |
| AC-6 | Empirical execution remains fail-closed without release/OOS/soak proof | no REAL and no execution gate | startup/executor promotion test and runtime gate probe | research/shadow only until evidence exists | `LOCKED`; gate absent; REAL false; all writers inactive | PASS |
| AC-7 | Each PostgreSQL login identity can update only its exact owned heartbeat through the real `PostgresRuntimeStoreV2.heartbeat()` path | no direct heartbeat-table write for service roles; observer remains read-only; collector keeps only its constrained market function | disposable PostgreSQL actual-LOGIN test plus spoof/direct-write denials | active owned heartbeats advance; inactive identities remain inactive | exact-login CI and live role/heartbeat audit | PASS |
| AC-8 | Market index survives candidate -> exact v0.5.15 rollback writer -> candidate round-trip | no indexed/raw observation loss; immutable raw catalog preserved; live DB is never used as the test target | isolated online-copy round-trip and raw hash verification | live index advances with equal base/eligibility counts and integrity `ok` | isolated 123003-row proof plus live auditor | PASS |
| AC-9 | Exact final primary release sustains the complete gate-absent research/shadow runtime for at least two coordinator intervals | execution/signer inactive; gate absent; `LOCKED`; no matching process | sample health/heartbeats/index/logs/restarts over at least 130 seconds | economics `0/0/0/0`; reconciliation counts zero/drift empty/hash present | independent 0/70/140 proof; zero priority/error/quality markers | PASS |
| AC-10 | Dell runs the same immutable release as bounded passive AI compute, without local PostgreSQL or broker execution authority | fixed remote `etoro-ai`; no local DB/broker credential/signer/gate/executor | rollback-tested passive sync with manifest/digest/wire proof | exact release/digest; Sol healthy; primary remains healthy | digest `9bbd6759...1cde`; role `etoro-ai`; zero errors | PASS |

## Tasks and dependencies

| Task | Scope | Depends on | Owner | Attempts | State |
|---|---|---|---|---:|---|
| T1 | Fix candidate marker namespace and PostgreSQL regression | none | primary | 1 | PASS |
| T2 | Exact-head CI and fresh independent audit | T1 | independent auditor | 1 | PASS |
| T3 | Resolve thread, merge, main CI, immutable release | T2 | primary | 1 | PASS |
| T4 | Safe deploy and runtime proof on primary + Dell | T8 | primary | 7 | PASS |
| T5 | Cleanup merged refs/worktrees and final synchronization proof | T4 | primary | 1 | PASS |
| T6 | Close heartbeat-role and rollback-index runtime gaps with executable regressions | T3 | primary | 3 | PASS |
| T7 | Fresh independent audit, exact-head CI, merge, exact-main CI and immutable release | T6 | independent auditor + primary | 1 | PASS |
| T8 | Close the live market-heartbeat enum mismatch, re-release and repeat runtime proof | T7 | primary + independent auditor | 1 | PASS |
| T9 | Close the live control-role schema-read gap and repeat exact release/runtime/Dell proof | T8 | primary + independent auditor | 2 | PASS |
| T10 | Close the passive remote-digest shell expansion and repeat exact release/runtime/Dell proof | T9 | primary + independent auditor | 1 | PASS |
| T11 | Normalize passive PAX metadata and finish exact primary/Dell convergence | T10 | primary + independent auditor | 1 | PASS |
| T12 | Isolate the Dell post-cutover wire probe from the newly started runner | T11 | primary + independent auditor | 1 | PASS |
| T13 | Cover the coordinator candle lookback with the pinned market calendar | T12 | primary + independent auditor | 2 | PASS |

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
- 2026-08-13T00:23:00+03:00 Re-audit of `2dec28a` remained FAIL with one P1: a failed rollback stop poisoned symlink restoration yet unit restore/restart could continue, and recovery evidence was always deleted. No PR/deploy occurred.
- 2026-08-13T00:29:00+03:00 Rollback phases are now independent and individually verified: candidate stop, exact prior symlink, all unit restores and daemon reload must all succeed before any prior service restart. Uncertain recovery preserves unit/active receipts and reports their paths. Four rollback-operation failure modes plus persistent uncertain-recovery evidence preservation pass; re-audit required.
- 2026-08-13T00:34:00+03:00 Fresh independent re-audit of exact implementation `05e90f8` returned PASS with P0/P1/P2 = 0. AC-7..AC-10 implementation readiness is accepted; runtime clauses remain UNVERIFIED. T6 is PASS and T7 starts with exact-head CI.
- 2026-08-12T23:50:00+03:00 PR #31 and exact-main CI completed, v0.6.5 was attested/published and its guarded primary install reached schema 9 with all execution surfaces inactive. The required 135-second live proof then found a new deterministic incompatibility: market rows advanced from 123066 to 123144, but the worker's `synchronizing` heartbeat was rejected by the schema-v5 function allowlist and remained stale at `starting`. No economic state, gate, restart or broker write changed. A first edit of historical v5 was correctly rejected by the migration-checksum regression; T8 now uses additive schema 10, and T4 attempt 5 remains BUILDING pending a new exact release plus repeated proof.
- 2026-08-13T00:01:00+03:00 Exact `a7fcac5403623134412af200496eb7612dfec019` passed the new independent implementation audit with P0/P1/P2 = 0. Historical migrations v1-v9 are byte-identical; schema 10 changes only the constrained market-heartbeat status allowlist. All 249 non-PostgreSQL tests, all 14 PostgreSQL 18.4 integrations, 41 security/release tests, Ruff, mypy and shell syntax pass. T8 moved to VERIFYING for exact-head CI.
- 2026-08-13T00:03:00+03:00 Evidence-only descendant `3f056de` preserved the technical PASS but re-audit found one plan-only P2: `Next exact step` still requested already completed PostgreSQL and independent-audit work. No executable content changed; the resume instruction now starts at exact-head CI.
- 2026-08-13T00:25:00+03:00 PR #32 merged as `c847f00`, exact-main CI and v0.6.6 provenance passed, and guarded primary deployment reached schema 10. Three samples over 130 seconds proved health 200/LOCKED, heartbeat and market-index progress, zero economic effects and zero restarts. Terminal journal inspection then exposed a failed gate-lock oneshot: `etoro-control` could not read `v2_meta` before its lock transaction. Dell remained on v0.5.15 and its old retired-engine wire calls were stopped pending sync. T9 adds schema 11 plus the exact-role regression; no gate or broker writer was activated.
- 2026-08-13T00:25:30+03:00 T9 candidate passes all 14 PostgreSQL 18.4 integrations, including exact `etoro-control` `require_schema()` plus atomic lock, and all 41 security/release tests; Ruff, format and shell syntax pass. T9 moved to VERIFYING for an independent exact-SHA audit.
- 2026-08-13T00:31:00+03:00 Independent audit of `dc91438` returned FAIL with one P2: the control proof used an admin session plus a suffixed NOLOGIN role, so it did not establish the exact production login or isolate schema 11 from the final grants file. The corrected regression connects through the exact `etoro-control` LOGIN DSN before grants to prove migration-owned schema reads, then after convergent grants proves `session_user=current_user=etoro-control`, `require_schema()`, atomic lock and direct UPDATE denial. Re-audit required; no PR/deploy occurred.
- 2026-08-13T00:34:00+03:00 Re-audit of exact `fb165b4ba4ecf12c3894810d982ae2228d5c93b3` returned PASS for implementation readiness with P0/P1/P2 = 0. The only delta after the rejected candidate is test and plan evidence; T9 is PASS and integration may proceed.
- 2026-08-13T00:53:00+03:00 PR #33 merged as `289f242`, exact-main CI and v0.6.7 provenance passed, and primary deployment reached schema 11. Gate-lock now succeeds under the exact control login. A 132-second proof passed health 200/LOCKED, all required heartbeat/index progress, zero economic effects/restarts/execution processes/journal errors. Dell sync then stopped before staging because the remote digest command rendered the script positional `$1` inside awk as a backslashed SHA. Dell remains on its prior release with the failing runner intentionally stopped. T10 removes that expansion entirely via fixed-field `cut`; no database, authority or broker surface changes.
- 2026-08-13T00:56:00+03:00 T10 targeted verification passes all 41 security/release tests, Ruff, format, shell syntax and diff checks. The exact fixed command returns a 64-hex recursive digest against the live primary release. T10 moved to VERIFYING for independent audit.
- 2026-08-13T00:58:00+03:00 Independent audit of exact `e14106d945fb5a8ec46416eb5b32574351d531d8` returned PASS for implementation readiness with P0/P1/P2 = 0. The auditor independently executed the fixed command against primary and proved a lowercase 64-hex digest. T10 is PASS; release/runtime clauses remain UNVERIFIED.
- 2026-08-13T01:08:00+03:00 PR #34 merged as `a226d1c`, exact-main CI and v0.6.8 provenance passed, and primary promotion remained schema 11/LOCKED/writers inactive. Dell sync passed the shell-safe remote command and staged byte-identical content, then stopped before switch because POSIX tar embedded rsync-specific PAX `atime/ctime`. Content checksum dry-run showed no difference; deleting those PAX keys produced the same `ed096d...d8829` digest on both hosts. Dell `current` remains the prior release. T11 normalizes both digest sides; no content, schema, authority or broker change.
- 2026-08-13T01:10:00+03:00 T11 targeted verification passes 41/41 security/release tests, Ruff, format, shell syntax and diff checks. The candidate local function and exact remote command both return `ed096d...d8829` for the staged byte-identical release. T11 moved to VERIFYING for independent audit.
- 2026-08-13T01:12:00+03:00 Independent audit of exact `31cbfde641a58084006101129de7aa176358161f` returned PASS for implementation readiness with P0/P1/P2 = 0. Both hosts use GNU tar 1.35; the auditor independently reproduced the equal lowercase 64-hex digest. T11 is PASS; release/runtime clauses remain UNVERIFIED.
- 2026-08-13T01:22:00+03:00 v0.6.9 exact-main CI, provenance, release and primary promotion passed at `36a7893`; primary is schema 11, health 200/LOCKED and execution-disabled. Dell reached the post-switch read-only AI probe, where the newly started runner concurrently owned the fixed transient `etoro-v2-ai-wire.service`; sync rolled back the exact old symlink/units successfully. T12 derives a bounded remote unit name from the worker identity, assigns the cutover probe its own identity and excludes the non-instantiable model template from active receipts. No database, broker, gate or credential authority changes.
- 2026-08-13T01:27:00+03:00 T12 targeted verification passed 46 runtime/security tests plus 4 release-surface tests, Ruff, format, bash syntax and diff checks. A live non-mutating candidate probe used distinct unit `etoro-v2-ai-wire-18a491e1c6c6`, returned exact `session_user=etoro-ai`/schema 11 and left primary health 200/LOCKED. Fresh independent audit of `a83b3aa45337f4770fb36ab87374711a0a5e3281` returned PASS with P0/P1/P2 = 0; T12 is PASS.
- 2026-08-13T01:51:00+03:00 PR #36, exact-head/main CI, v0.6.10 provenance, primary promotion and Dell convergence passed at `149eb9b`; Dell returned exact commit/tree/bundle/digest and `session_user=etoro-ai`. Primary samples at 0/65/130 seconds stayed health 200/LOCKED, heartbeats and index advanced `138481 -> 139048`, integrity stayed `ok` and economic counts stayed `0/0/0/0`. Final journal inspection found recurring fail-closed `MarketDataQualityError` for every non-crypto execution symbol: the 500-candle lookback begins August 5 while the pinned calendar validity begins August 12, so legitimate daily/weekend closures cannot be explained. T13 extends only that signed source release's backward coverage; no schedule window, schema, broker, gate, REAL or credential change.
- 2026-08-13T02:00:00+03:00 Initial T13 audit of `abbc5319` rejected one P2: `.days` truncated the stated 31-day bound. Exact `62fd879f8ad225bb15bd760a557e7137e093b4bd` compares the full duration and proves exact 31 days accepted plus 31 days + 1 second rejected. Independent re-audit returned PASS with P0/P1/P2 = 0; live authenticated read-only evidence showed all 15 scheduled gaps across the five affected symbols explained with zero residual quality issues. T13 is PASS.

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
- Live v0.6.5 proof exposed that the collector deliberately emits `synchronizing` while a connected snapshot is not yet eligible, but `v2_record_market_heartbeat` rejected that exact state. The index kept advancing, so a short health sample was misleading until the five-minute heartbeat freshness window expired.
- Live v0.6.6 proof exposed that the gate guard calls `require_schema()` before locking, while the exact grants omitted `v2_meta` SELECT for `etoro-control`; earlier tests exercised its lock transaction but not its schema precondition under that login.
- Live v0.6.7 Dell sync exposed that four backslashes in the double-quoted remote awk program expand the script positional `$1` before SSH, producing an invalid remote program. The failure is pre-staging and changed no Dell release/unit.
- Live v0.6.8 Dell sync exposed non-deterministic POSIX PAX `atime/ctime` headers after rsync. File checksums are identical; canonical release digest must delete both keys on primary and Dell.

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
- Exact `2dec28a` re-audit: FAIL; one AC-10 P1 rollback-of-rollback ordering/evidence gap. Repaired in the next candidate; re-audit required.
- Exact `05e90f8` re-audit: PASS for implementation readiness; P0/P1/P2 = 0. Runtime/deploy gates explicitly remain UNVERIFIED.
- Exact `a7fcac5403623134412af200496eb7612dfec019` schema-10 audit: PASS for T8/AC-4/AC-7/AC-9/AC-10 implementation readiness; P0/P1/P2 = 0. Exact release and runtime clauses remain UNVERIFIED.
- Exact `dc91438` T9 audit: FAIL with one P2 for missing exact control LOGIN/DSN proof. Exact `fb165b4ba4ecf12c3894810d982ae2228d5c93b3` re-audit: PASS for implementation readiness; P0/P1/P2 = 0. Release/runtime clauses remain UNVERIFIED.
- Exact `e14106d945fb5a8ec46416eb5b32574351d531d8` T10 audit: PASS for implementation readiness; P0/P1/P2 = 0. Exact v0.6.8 release and runtime clauses remain UNVERIFIED.
- Exact `31cbfde641a58084006101129de7aa176358161f` T11 audit: PASS for implementation readiness; P0/P1/P2 = 0. Exact v0.6.9 release and runtime clauses remain UNVERIFIED.
- v0.6.9 live Dell cutover: rollback PASS after a real fixed-name transient wire collision; primary stayed exact/healthy and Dell returned to `2872f0e6` with prior active receipt restored.
- Exact `a83b3aa45337f4770fb36ab87374711a0a5e3281` T12 audit: PASS for implementation readiness; P0/P1/P2 = 0. Runner/probe wire units and matching credential directories are distinct; rollback/passive authority remain intact.
- v0.6.10 runtime proof: primary/Dell convergence and all state/health/progress predicates PASS, but AC-9 journal-marker predicate remains UNVERIFIED because the calendar validity did not cover its own 500-candle validation lookback.
- Exact `abbc5319be605cf22dfde1d33b570b15ed54d528` T13 audit: FAIL with one P2 for a truncated validity bound. Exact `62fd879f8ad225bb15bd760a557e7137e093b4bd` re-audit: PASS for implementation readiness; P0/P1/P2 = 0.

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
- T8 schema-10 evidence: 249 non-PostgreSQL tests PASS at 65% branch coverage; 14/14 PostgreSQL 18.4 integrations PASS including migration-checksum rollback and exact collector `synchronizing` call; 41/41 security/release tests PASS; independent exact-SHA audit PASS.
- T9 targeted evidence: 14/14 PostgreSQL 18.4 integrations and 41/41 security/release tests PASS; the control login reads both schema authorities, executes the fail-closed lock path and retains no direct table mutation grant; Ruff, format and shell syntax PASS.
- T9 first independent audit: exact `dc91438` FAIL with P0/P1=0, P2=1 for SET ROLE versus exact LOGIN coverage; replaced by a pre-grants migration proof and post-grants lock proof through the exact login DSN.
- T10 targeted evidence: 41/41 security/release tests PASS and the fixed remote digest command returned `421e0f7f...98fa3b53` against the immutable primary v0.6.7 release; Ruff, format, shell syntax and diff checks PASS.
- T11 targeted evidence: 41/41 security/release tests PASS; local/remote PAX-normalized digest equality `ed096d...d8829`; checksum dry-run reports no file-content differences; Ruff, format, shell syntax and diff checks PASS.
- T12 targeted evidence: 46 runtime/security plus 4 release-surface tests PASS; live read-only candidate probe returned exact `etoro-ai`, schema 11 and primary `36a7893`; auditor independently passed the full 38-test security-boundary module, ShellCheck and identity/credential/rollback inspection.
- T13 targeted evidence: 19 calendar/market/coordinator/release tests PASS; all 15 live broker gaps explained and no residual quality issue across EURUSD/SPX500/NSDQ100/AAPL/TSLA; exact 31-day boundary proven; Ruff and diff checks PASS.

## Integration, regression, and deployment

- Integrated diff: PASS on merged `6d8f23a06e8d955028896ef2a0b1c86e01f63500`
- Global checks: PR `31649026918` and main `31649234353` SUCCESS; 251 installer tests PASS
- Published artifact/SHA: v0.6.11 / `6d8f23a`; bundle `1b8ae3a...00014`; one Sigstore attestation
- Runtime: primary/Dell exact; schema 11; health 200/LOCKED; zero economic effects/errors/restarts; Dell passive `etoro-ai`
- Final independent verdict: PASS; AC-1 through AC-10 PASS; P0/P1/P2 = 0/0/0

## Risks and remaining work

- No open acceptance gap. No profitability or DEMO execution-readiness claim exists.
- Future execution remains separately gated by signed OOS/promotion/soak evidence; REAL remains unsupported.

## Next exact step

None. Objective complete; preserve the fail-closed research/shadow runtime.

## Resume procedure

1. Read `AGENTS.md`, this plan, and relevant V2 architecture/deployment docs.
2. Recheck HEAD, branch, working tree, PR head/checks, and both hosts; preserve the untracked audit file.
3. Continue from `Next exact step`; do not merge until final exact-head CI and a fresh independent PASS.
