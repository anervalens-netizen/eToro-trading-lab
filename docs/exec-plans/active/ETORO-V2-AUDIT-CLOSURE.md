# ETORO-V2-AUDIT-CLOSURE - Canonical V2 audit closure and synchronized deploy

Status: ACTIVE
Overall outcome: UNVERIFIED
Owner: primary Codex agent
Created: 2026-08-12T17:18:00+03:00
Updated: 2026-08-12T21:47:00+03:00

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
- Branch and exact HEAD: `codex/canonical-v2-redesign` / `cbfbc8d6aa1bd80f6c838d80b1ac49c61cf85094`
- Working tree: clean except protected untracked user audit `eToro-trading-lab-audit-2872f0e6.md`
- Protected newer work: commits `03aada5..cbfbc8d`; user audit file above
- Relevant runtime/deployed state: must be re-probed before deployment; execution gate must remain absent

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
| AC-3 | PR merges to main and immutable v0.6.0 evidence targets exact main SHA | no branch/release ambiguity | main CI SUCCESS; checksum, SBOM, bundle, provenance/attestation match | release target equals main/deployed SHA | pending | UNVERIFIED |
| AC-4 | Primary and passive Dell use the same V2-only release, schema 8, no legacy runtime/services | execution gate absent; REAL false | installer/provision probes, service/user/path/schema/health checks | exact SHA/digest, healthy allowed services, zero `etoro-agent` legacy processes | pending | UNVERIFIED |
| AC-5 | GitHub/local refs and worktrees contain no redundant merged audit branch/worktree | preserve unique user audit file | fetch/prune, worktree/branch/ref comparison | N/A | pending | UNVERIFIED |
| AC-6 | Empirical execution remains fail-closed without release/OOS/soak proof | no REAL and no execution gate | startup/executor promotion test and runtime gate probe | research/shadow only until evidence exists | pending | UNVERIFIED |

## Tasks and dependencies

| Task | Scope | Depends on | Owner | Attempts | State |
|---|---|---|---|---:|---|
| T1 | Fix candidate marker namespace and PostgreSQL regression | none | primary | 1 | PASS |
| T2 | Exact-head CI and fresh independent audit | T1 | independent auditor | 1 | PASS |
| T3 | Resolve thread, merge, main CI, immutable release | T2 | primary | 1 | PASS |
| T4 | Safe deploy and runtime proof on primary + Dell | T3 | primary | 1 | BUILDING |
| T5 | Cleanup merged refs/worktrees and final synchronization proof | T4 | primary | 0 | READY |

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

## Decisions

- Permit only the exact production marker namespace `^v2_coordinator_bar:(POSITION_REVIEW|ENTRY_REVIEW):[A-Z0-9._-]+$`; do not retain unused broader legacy prefix.
- Verify through `PostgresRuntimeStoreV2.state_set()` under the actual candidate role, not a string-only test.

## Auditor verdicts

- `cbfbc8d6`: PASS before the later automated prefix finding; P0/P1/P2 = 0 for the audited rollback delta. A fresh verdict is required after T1.
- `9ec11f6c`: PASS for AC-1/AC-2/AC-6; exact PR CI `31622332818` SUCCESS; P0/P1/P2 = 0.

## Evidence index

- Prior PR CI: run `31621355595`, job `94196713574`, 228 tests, 69% coverage, mypy 73 modules.
- Prior rollback audit: independent GO on `cbfbc8d6`.
- AC-1 local evidence: `test_service_grants_preserve_economic_owner_and_audit_fail_safe` PASS on PostgreSQL 18.4 exact CI digest; `tests.test_v2_coordinator_contract` 6/6 PASS; Ruff PASS.
- AC-2: fresh auditor PASS and PR CI run `31622332818`.
- AC-3: merged main `242aa4716bc1ed2c1e778c07a5bfc4ce278b5987`; main CI run `31622984683` SUCCESS; v0.6.0 bundle digest `ae290493...53c01b`, one GitHub attestation.
- Bootstrap hotfix: independent PASS on `3adea041`; merged main `2523d23c0a9f81b3a1dd2f105f4678dd3a9742cb`; main CI `31624023489` SUCCESS; v0.6.1 bundle digest `d30fab74...a7b8`, one GitHub attestation.

## Integration, regression, and deployment

- Integrated diff: PASS on merged `242aa471`
- Global checks: PASS on PR and main
- Published artifact/SHA: v0.6.0 / `242aa471`; not deployed because guarded installer exposed a bootstrap permission defect
- Runtime health/logs/user flow: UNVERIFIED

## Risks and remaining work

- Final candidate SHA will change once for T1; all acceptance and release evidence must bind the new SHA.
- Host cutover changes service identities already implemented in the candidate; deployment must use the guarded installer and remain execution-disabled.

## Next exact step

Commit the transactional config cutover, run rollback/security gates and independent audit, publish the exact replacement release, then retry primary and Dell synchronization.

## Resume procedure

1. Read `AGENTS.md`, this plan, and relevant V2 architecture/deployment docs.
2. Recheck HEAD, branch, working tree, PR head/checks, and both hosts; preserve the untracked audit file.
3. Continue from `Next exact step`; do not merge until final exact-head CI and a fresh independent PASS.
