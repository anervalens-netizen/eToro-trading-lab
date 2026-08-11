# v1 -> v2 migration

## Why parallel migration

v2 changes the economic data model, order lifecycle, persistence, AI queue and research semantics. The old SQLite audit remains evidence and must not be rewritten into a fictional v2 history.

## Mapping

- v1 strategy snapshots -> reference-only historical evidence;
- v1 shadow fills -> optional imported research records, never canonical v2 fills;
- v1 open DEMO broker position -> must be reconciled and either closed by the current owner/executor or explicitly imported as a carried position before v2 new risk is enabled;
- v1 AI packets/reviews -> historical research context only;
- v2 starts a new research epoch with new baselines.

## Migration sequence

1. freeze the deployed v1 SHA/config hash and create verified backup;
2. initialize v2 PostgreSQL schema in parallel;
3. start v2 market archive and read-only/shadow workers with broker writes disabled;
4. validate event-time/data parity and broker instrument mappings;
5. start AI packet/role infrastructure; model outputs remain non-executing until decision/risk integration checks pass;
6. reconcile v1 broker truth and v2 broker truth;
7. stop the v1 write executor;
8. activate the v2 autonomous DEMO executor only through the explicit deployment gate;
9. retain v1 state read-only for rollback/forensics.

## Research epochs

The migration starts a fresh v2 epoch. Old v1 performance may be shown historically but cannot be merged into v2 promotion statistics unless reconstructed through the exact v2 replay/cost/strategy semantics and marked as such.

## Canonical files

Production:

- `postgres_runtime_v2.py`
- `ai_store_postgres_v2.py`
- `etoro_api_current_v2.py`
- `executor_current_v2.py`
- `executor_service_postgres_v2.py`
- `reconciliation_v2.py`
- `dashboard_worker_v2.py`
- `anchor_worker_v2.py`
- `etoro-v2-executor-postgres.service`
- `etoro-v2-reconciliation.service`
- `etoro-v2-dashboard.service`
- `etoro-v2-anchor.service`

Reference/replay:

- `runtime_store_v2.py` (SQLite)
- `executor_service_v2.py` (SQLite reference)

## Transitional files

During development a few adapters were created before the current eToro endpoint contract and PostgreSQL production path were fixed. They are retained only to preserve branch history and tests until final cleanup. Do not configure production around:

- `etoro_api_v2.py` — superseded by `etoro_api_current_v2.py`;
- `postgres_ai_v2.py` — superseded by `ai_store_postgres_v2.py`;
- `etoro-v2-executor.service` — superseded by `etoro-v2-executor-postgres.service`.

Canonical service tests prevent the production units from importing/receiving those transitional capabilities.

## Activation gate

Passing source tests makes v2 a deployment candidate, not an active trading runtime. Keep all v2 units inactive until the PostgreSQL integration test, broker-read reconciliation drill, shadow parity/soak, credential-scope check, exact-SHA backup/deploy and rollback drill pass. Any unresolved v1 or v2 broker/local drift keeps new DEMO risk locked.

## No REAL migration

v2 contains no REAL broker route, REAL credential unit, REAL config or automatic DEMO->REAL promotion. A future REAL implementation is a separate release and must begin with a fresh explicit owner request, broker credential scope and security/fault review.
