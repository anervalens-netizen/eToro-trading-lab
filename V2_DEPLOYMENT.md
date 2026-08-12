# V2 deployment runbook

## Invariant

Deploy exact merged SHA from the GitHub-attested offline bundle. Never deploy a
dirty checkout. Provisioning leaves DEMO writes disabled; deploy is not an
activation decision.

## Install exact release

```bash
gh run download --repo anervalens-netizen/eToro-trading-lab \
  --name etoro-v2-<sha> --dir /tmp/etoro-v2-release-<sha>
(cd /tmp/etoro-v2-release-<sha> && \
  sha256sum --check --strict RELEASE_ARTIFACT_SHA256SUMS.txt)
gh attestation verify \
  /tmp/etoro-v2-release-<sha>/release-bundle-<sha>.tar.gz \
  --repo anervalens-netizen/eToro-trading-lab
sudo /opt/eToro/ops/deploy/install-v2-release.sh /opt/eToro <sha> \
  /tmp/etoro-v2-release-<sha>/release-bundle-<sha>.tar.gz
sudo ETORO_V2_POSTGRES_PORT=5434 \
  /opt/etoro-v2/current/ops/deploy/provision-v2-host.sh
```

The installer first materializes the verified candidate under `releases/<sha>`,
then runs that candidate's `provision-v2-host.sh --bootstrap-control`. Bootstrap
requires the execution gate absent and every writer inactive. For an existing
database it verifies `LOCKED` before migration; for a fresh database it creates
the owner/service identities and DSNs. Before migration it records the exact
previous `v2_meta.schema_version` in a root-only rollback receipt. It then
migrates transactionally, applies grants and verifies the unique state is
`LOCKED`. Only after bootstrap succeeds
may the installer back up the installed read-only unit files, install the
candidate unit identities, atomically change `current`, and restart/revalidate
the previously active services. Bootstrap deliberately preserves the legacy
engine DSN and grants until that unit/process cutover succeeds. Failure restores
the old symlink, unit files, exact previous schema-version marker and the
previous release's grant matrix before it restarts all old services. Additive
migration rows/functions remain. While an
older marker is active, the v8 economic-event trigger permits only the fully
privileged legacy engine to complete its old atomic fill/position/event
projection; all new split roles remain constrained. A forward retry restores
marker 9 and the strict role-owned-heartbeat/reconciler authority. This keeps rollback
non-destructive and executable. Unverifiable recovery stops the whole read-only
set. Only a successful candidate runtime retires the engine role/DSN. The second
command completes host files, keys and units; it is idempotent and leaves
execution disabled.

CI checks exact PR-head/main SHA, full history secrets, full tests/coverage,
all-module mypy, Ruff, Bandit, dependency locks, shell/systemd, PostgreSQL
fault/concurrency tests, two byte-identical clean wheel builds, exact production
allowlist, SBOM, checksums and provenance attestation. Installer verifies all
hashes offline, repairs relocated entrypoint shebangs, tests the installed wheel
and atomically changes `/opt/etoro-v2/current`.

## Identities and DSNs

Provisioning creates service-scoped identities and DSNs under
`/etc/etoro-agent/`. Shadow decision and execution decision never share an OS
UID or a PostgreSQL role/DSN. `etoro-decision` can only consume and annotate the
shadow AI queue; `etoro-decision-exec` alone can create deterministic intents,
commands, reservations and outbox entries. Only `etoro-decision-exec` can reach
the signer socket.
`postgres-v2-engine-dsn` is removed and `etoro-engine` is NOLOGIN. Schema
migration runs only under the owner/migration boundary; services call
`require_schema()` and cannot migrate.

Credentials:

- `etoro-demo-read-user-key`: collector/candidate/decision-exec/reconciler reads;
- `etoro-demo-write-user-key`: executor only;
- `etoro-api-key`: separate API identity;
- signing private keys: signer/anchor only;
- strategy manifest/trust and calendar: root-owned, hash-pinned evidence.

All eToro credentials must be DEMO. REAL is unsupported.

## Safe default services

With `/etc/etoro-v2-control/ENABLE_DEMO_EXECUTION` absent, enable only market,
coordinator, role apply, shadow decision apply, dashboard, anchors, backup,
off-host backup, restore drill and gate watcher. The gate-lock target stops all
writer units and the risk signer. Keep database state `LOCKED`. Health requires
fresh market, coordinator, role-apply, shadow-decision and reconciliation
heartbeats. Historical dead letters remain evidence; only a dead letter from
the last 15 minutes degrades readiness.

## Passive Dell AI compute

Dell is passive for database and broker authority but active for bounded Sol
model compute. After the exact release is healthy on primary, synchronize it:

```bash
sudo /opt/eToro/ops/deploy/sync-v2-passive-runtime.sh <exact-main-sha>
```

The procedure verifies primary `RELEASE.json` and a recursive release digest,
copies no credential, atomically changes Dell `current`, and restarts only the
Sol model socket/template and runner. It requires local PostgreSQL, gate,
signer, executor and broker credentials absent. Its final non-mutating remote
wire probe proves schema 11, exact commit/bundle and `session_user=etoro-ai`; any
failed cutover restores the exact prior symlink, units and active Sol surfaces.

Before any future DEMO activation, all must pass:

1. `ops/security/verify-v2-boundaries.sh full`;
2. exact release/CLI/legacy-unit checks;
3. zero unknown orders, broker/local drift and foreign/manual/copy activity;
4. event chain, backup, off-host receipt and disposable restore;
5. full snapshot eligibility and dynamic calendar freshness;
6. signed strategy release with OOS, PROMOTE, adverse-simulator, observed-cost and soak gates;
7. fault drills for timeout-after-send, poison outbox, partial/concurrent fills, PostgreSQL restart and gate removal.

Only then may an operator create the gate, start execution applier, exit manager
and the sole PostgreSQL executor, and transition the state to `ACTIVE`. The
resulting state version is the only current execution epoch.

Removing the gate is the immediate kill operation. It freezes all broker writes,
including CLOSE; `LOCKED` with gate retained is the lock-new state that keeps
strict reduce-only exits available.

## Legacy cleanup

Installer/provisioning stop, archive by content hash, remove and mask every v1
unit name. The installed distribution must expose only `etoro-v2`; importing v1
modules or running `etoro-agent` must fail. Worktrees/branches are removed only
after their commits are merged/tagged and no unique work exists.

## Post-deploy evidence

Record exact commit/tree, artifact digest/attestation, package version, current
symlink, PostgreSQL schema, service identities, gate/state, exposure counts,
legacy masks, event-chain result, backup/restore/off-host freshness and rollback
target. On Dell record the matching release/tree/bundle/recursive digest and
the bounded remote `etoro-ai` status proof; Dell never receives broker or local
database authority.
