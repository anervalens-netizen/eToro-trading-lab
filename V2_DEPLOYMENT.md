# v2 deployment runbook

## Principle

Deploy v2 with broker writes disabled. Provisioning retires and masks the v1 writer; two executors must never coexist against the same DEMO capital.

## 1. Install code

After the push-to-main CI passes on one exact 40-character SHA, download and
verify its attested offline bundle, then install that exact candidate:

```bash
mkdir -p /tmp/etoro-v2-release-<exact-sha>
gh run download --repo anervalens-netizen/eToro-trading-lab \
  --name etoro-v2-<exact-sha> --dir /tmp/etoro-v2-release-<exact-sha>
(cd /tmp/etoro-v2-release-<exact-sha> && \
  sha256sum --check --strict RELEASE_ARTIFACT_SHA256SUMS.txt)
gh attestation verify \
  /tmp/etoro-v2-release-<exact-sha>/release-bundle-<exact-sha>.tar.gz \
  --repo anervalens-netizen/eToro-trading-lab
sudo /opt/eToro/ops/deploy/install-v2-release.sh /opt/eToro <exact-sha> \
  /tmp/etoro-v2-release-<exact-sha>/release-bundle-<exact-sha>.tar.gz
```

CI builds the Python 3.12 wheelhouse from the hashed `requirements.lock`,
checksums the wheel/SBOM/locks, packages them with the source commit and tree,
extracts and verifies both internal manifests, publishes a separate artifact
manifest for the compressed bundle and visible SBOM, and attests the bundle.
The installer rejects any commit/tree/lock/checksum
mismatch, performs a network-free `--no-index` install, runs the complete unit
suite before promotion, writes `RELEASE.json`, installs under
`/opt/etoro-v2/releases/<sha>` and atomically switches
`/opt/etoro-v2/current`. Never deploy a dirty checkout or an unattested bundle.

## 2. PostgreSQL

Provision native PostgreSQL, separated OS/DB identities, migrations, grants, keys, units and backup paths:

```bash
sudo ETORO_V2_POSTGRES_PORT=5434 /opt/etoro-v2/current/ops/deploy/provision-v2-host.sh
```

The canonical DSNs are `postgres-v2-engine-dsn`, `postgres-v2-executor-dsn` and `postgres-v2-observer-dsn`. Runtime services only call `require_schema()`; only the provisioning migration runner may alter schema. Keep v1 SQLite untouched during parallel validation.

## 3. Broker credentials

Provision two separate DEMO user keys:

- `/etc/etoro-agent/etoro-demo-read-user-key` — coordinator/market/decision-read processes;
- `/etc/etoro-agent/etoro-demo-write-user-key` — executor only.

The read key must have `Environment=Demo, Permission=Read` only. Startup rejects any write or REAL scope. The write key must have DEMO read+write and no REAL scope. The API key remains separate. Apply broker IP whitelist and expiry. Do not reuse a REAL credential.

## 4. Configuration

Use `config/v2-demo.json` for research and `config/v2-demo-execution.json` only for the autonomous DEMO executor. Copy the selected configuration root-only under `/etc/etoro-agent/`.

Verify broker minimum compatibility before allowing a strategy family to become a live master candidate. `compatibility_v2.py` must report compatible candidates rather than assuming shadow notionals are broker-executable.

## 5. Services — shadow/read phase

`provision-v2-host.sh` installs distinct collector/engine/signer/executor/observer identities and leaves execution disabled. Verify the deployed boundary before starting the shadow lane:

```bash
sudo /opt/etoro-v2/current/ops/security/verify-v2-boundaries.sh full
```

Install but initially enable only:

```text
etoro-v2-market.service
etoro-v2-coordinator.service
etoro-v2-role-apply.service
etoro-v2-decision-apply.service
etoro-v2-dashboard.service
etoro-v2-anchor.timer
etoro-v2-backup.timer
etoro-v2-offhost-backup.timer
etoro-v2-restore-drill.timer
etoro-v2-execution-gate.path
```

The Dell/AI host runs:

```text
etoro-v2-sol-runner.service
```

`etoro-v2-decision-apply.service` is the broker-write-free shadow recorder: it has no network, broker key or signer socket. At this phase `/etc/etoro-v2-control/ENABLE_DEMO_EXECUTION` must not exist; the gate-lock target is active, all write-capable units are stopped, and `etoro-demo-executor.service` is masked.

`LOCKED` is also the canonical shadow state. With the execution gate absent, the
coordinator may create immutable AI packets and the shadow recorder may consume
them, but no order command can be minted or sent. If a gate exists while the
state remains `LOCKED`, the coordinator is inert until the operational readiness
transition to `ACTIVE`. Packets created here carry `SHADOW` authority and no
execution epoch; they can never be claimed by the execution applier.

## 6. Health verification

Check:

- event-chain validity;
- PostgreSQL connectivity/migrations;
- market archive grows and has no unexplained sequence gaps;
- coordinator deduplicates a closed bar;
- AI packets are claimed once, leases recover after simulated worker crash, and expired packets cannot apply;
- decided shadow/old-epoch packets are atomically quarantined at the activation boundary and create zero command/outbox rows;
- shadow decision applier records `broker_write=false` and creates no order command;
- execution decision applier accepts only exact deterministic candidate plans and never writes to the broker itself;
- command, pending broker order, active risk reservation, execution outbox and approval event appear atomically;
- concurrent proposals cannot exceed reserved notional, loss or order-slot budgets;
- dashboard is reachable only through the existing owner-authenticated reverse-proxy path;
- hourly signed anchors appear in the independent anchor path;
- local backup integrity, verified off-host NAS receipt and restore drill pass.

## 7. DEMO autonomous write gate

Only after the above passes:

1. verify the v1 DEMO executor remains masked and inactive;
2. reconcile eToro DEMO positions/orders against local v2 state; expected drift must be zero;
3. ensure v2 trading state starts `LOCKED` or `HALT_NEW` and every active risk reservation maps to an unresolved broker order;
4. stop `etoro-v2-decision-apply.service`, create `/etc/etoro-v2-control/ENABLE_DEMO_EXECUTION`, then start `etoro-v2-decision-apply-execution.service`; it remains inert while state is `LOCKED`;
5. start `etoro-v2-exit-manager.service` and **only** `etoro-v2-executor-postgres.service` as the canonical broker-write path;
6. switch trading state to `ACTIVE` only after the explicit operational readiness check; this state version becomes the sole valid `EXECUTION` epoch and old decided packets are quarantined before claim;
7. send the minimum practical DEMO order through the complete coordinator/AI/risk/outbox/executor path;
8. verify ACK, broker truth, fill evidence, local position and audit anchor before continuing unattended.

This gate enables autonomous DEMO execution. It is not a REAL-money gate and does not require per-order human approval.

Deleting the gate while services are running is an immediate kill operation: executor/applier/exit workers recheck it in-process, pending unstarted commands are rejected atomically, and `etoro-v2-execution-gate.path` isolates all writers.

## 8. Fault drills

Before unattended soak, exercise at least:

- process kill after command persistence but before network send;
- simulated timeout after possible network send -> `UNKNOWN`, no retry;
- `UNKNOWN` and partial-fill paths retain their risk reservation; terminal rejection/absence/final fill release it;
- stale quote / wide spread / data gap;
- final quote outside the sealed execution band, inverted stop/target direction and broker costs exceeding sealed max loss;
- expired AI claim and expired decision packet;
- invalid audit chain copy;
- PostgreSQL restart;
- eToro rate/eligibility/cost endpoint failure;
- mismatched broker/local position;
- restored backup in a disposable environment.

`etoro-v2-restore-drill.timer` performs the disposable PostgreSQL restore weekly; a manual run must also pass before activation.

## 9. Soak

Run autonomous DEMO while collecting broker-calibrated spreads/slippage/costs and historical/shadow parity evidence. `V2_RESEARCH_PROTOCOL.md` defines promotion gates. The recommended soak is 30–60 days and cannot be compressed into a software deployment step.

## 10. Rollback

1. create/retain a v2 database backup and audit anchor;
2. stop v2 coordinator/decision/executor workers;
3. remove `/etc/etoro-v2-control/ENABLE_DEMO_EXECUTION`;
4. reconcile outstanding DEMO broker orders/positions before enabling another executor;
5. restore the prior code/service set without overwriting the v2 event history;
6. keep v2 PostgreSQL/database and raw archive read-only for forensic comparison.

Never restore an old database over newer audit/execution evidence merely to match application code.
