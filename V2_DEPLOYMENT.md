# v2 deployment runbook

## Principle

Deploy v2 beside v1 first, with v2 broker writes disabled. Do not run two executors against the same DEMO capital simultaneously.

## 1. Install code

```bash
cd /opt/eToro
git fetch origin
git checkout feat/major-redesign-v1
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[live]'
```

Run the repository tests before service activation.

## 2. PostgreSQL

Create a dedicated database/user and a root-owned DSN credential file:

```text
/etc/etoro-agent/postgres-v2-dsn
```

The file contains only the libpq DSN used by the `etoro-agent` service user. Migrate with the v2 store against the target database before starting workers. Keep v1 SQLite untouched during the parallel validation period.

## 3. Broker credentials

Provision two separate DEMO user keys:

- `/etc/etoro-agent/etoro-demo-read-user-key` — coordinator/market/decision-read processes;
- `/etc/etoro-agent/etoro-demo-write-user-key` — executor only.

The API key remains separate. Apply broker IP whitelist and expiry. Do not reuse a REAL credential.

## 4. Configuration

Use `config/v2-demo.json` for research and `config/v2-demo-execution.json` only for the autonomous DEMO executor. Copy the selected configuration root-only under `/etc/etoro-agent/`.

Verify broker minimum compatibility before allowing a strategy family to become a live master candidate. `compatibility_v2.py` must report compatible candidates rather than assuming shadow notionals are broker-executable.

## 5. Services — shadow/read phase

Install but initially enable only:

```text
etoro-v2-market.service
etoro-v2-coordinator.service
etoro-v2-role-apply.service
etoro-v2-decision-apply.service
etoro-v2-dashboard.service
etoro-v2-anchor.timer
etoro-v2-backup.timer
```

The Dell/AI host runs:

```text
etoro-v2-sol-runner.service
```

At this phase `/etc/etoro-agent/ENABLE_V2_DEMO_EXECUTION` must not exist and the production executor must remain stopped.

## 6. Health verification

Check:

- event-chain validity;
- PostgreSQL connectivity/migrations;
- market archive grows and has no unexplained sequence gaps;
- coordinator deduplicates a closed bar;
- AI packets are claimed once, leases recover after simulated worker crash, and expired packets cannot apply;
- decision applier creates only deterministic intents/orders, never direct broker writes;
- dashboard is reachable only through the existing owner-authenticated reverse-proxy path;
- hourly signed anchors appear in the independent anchor path;
- backup integrity and restore drill pass.

## 7. DEMO autonomous write gate

Only after the above passes:

1. stop/disable any v1 DEMO executor that can write to the same DEMO portfolio;
2. reconcile eToro DEMO positions/orders against local v2 state; expected drift must be zero;
3. ensure v2 trading state starts `LOCKED` or `HALT_NEW`;
4. create `/etc/etoro-agent/ENABLE_V2_DEMO_EXECUTION` manually;
5. start **only** `etoro-v2-executor-postgres.service` as the canonical v2 write service;
6. switch trading state to `ACTIVE` only after the explicit operational readiness check;
7. send the minimum practical DEMO order through the complete coordinator/AI/risk/outbox/executor path;
8. verify ACK, broker truth, fill evidence, local position and audit anchor before continuing unattended.

This gate enables autonomous DEMO execution. It is not a REAL-money gate and does not require per-order human approval.

## 8. Fault drills

Before unattended soak, exercise at least:

- process kill after command persistence but before network send;
- simulated timeout after possible network send -> `UNKNOWN`, no retry;
- stale quote / wide spread / data gap;
- expired AI claim and expired decision packet;
- invalid audit chain copy;
- PostgreSQL restart;
- eToro rate/eligibility/cost endpoint failure;
- mismatched broker/local position;
- restored backup in a disposable environment.

## 9. Soak

Run autonomous DEMO while collecting broker-calibrated spreads/slippage/costs and historical/shadow parity evidence. `V2_RESEARCH_PROTOCOL.md` defines promotion gates. The recommended soak is 30–60 days and cannot be compressed into a software deployment step.

## 10. Rollback

1. create/retain a v2 database backup and audit anchor;
2. stop v2 coordinator/decision/executor workers;
3. remove `ENABLE_V2_DEMO_EXECUTION`;
4. reconcile outstanding DEMO broker orders/positions before enabling another executor;
5. restore the prior code/service set without overwriting the v2 event history;
6. keep v2 PostgreSQL/database and raw archive read-only for forensic comparison.

Never restore an old database over newer audit/execution evidence merely to match application code.
