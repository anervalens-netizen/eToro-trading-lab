#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

backup_root=${ETORO_V2_BACKUP_ROOT:-/storage/backups/db/etoro/v2}
admin_service=${ETORO_V2_RESTORE_SERVICE:-}
release=${ETORO_V2_RELEASE_PATH:-/opt/etoro-v2/current}
work="$(mktemp -d)"
dashboard_pid=
trap 'rm -rf "$work"' EXIT

sqlite_backup="$(find "$backup_root" -maxdepth 1 -type f -name 'v2_*.sqlite3' -printf '%T@ %p\n' | sort -nr | head -1 | cut -d' ' -f2-)"
if [[ -n "$sqlite_backup" ]]; then
  [[ -s "$sqlite_backup.sha256" ]]
  sha256sum --check --status "$sqlite_backup.sha256"
  cp "$sqlite_backup" "$work/restore.sqlite3"
  [[ "$(sqlite3 "$work/restore.sqlite3" 'PRAGMA integrity_check;')" == ok ]]
  "$release/.venv/bin/python" - "$work/restore.sqlite3" <<'PY'
import sys
from etoro_agent.runtime_store_v2 import RuntimeStoreV2
store = RuntimeStoreV2(sys.argv[1])
assert store.verify_event_chain()
assert all(item.quantity >= 0 for item in store.positions())
assert all(item["reserved_notional_usd"] > 0 for item in store.active_risk_reservations())
store.close()
PY
fi

pg_backup="$(find "$backup_root" -maxdepth 1 -type f -name 'v2_*.pgdump' -printf '%T@ %p\n' | sort -nr | head -1 | cut -d' ' -f2-)"
[[ -n "$pg_backup" && -s "$pg_backup.sha256" ]] || {
  printf 'ETORO_V2_RESTORE_ERROR=postgres_backup_or_checksum_missing\n' >&2
  exit 1
}
sha256sum --check --status "$pg_backup.sha256"
pg_restore --list "$pg_backup" >/dev/null

assets_backup="${pg_backup%.pgdump}.assets.tar.gz"
[[ -s "$assets_backup" && -s "$assets_backup.sha256" ]]
sha256sum --check --status "$assets_backup.sha256"
mkdir -p "$work/assets"
tar --extract --gzip --file="$assets_backup" --directory="$work/assets"
python3 -m json.tool "$work/assets/opt/etoro-v2/current/RELEASE.json" >/dev/null
python3 -m json.tool "$work/assets/opt/etoro-v2/current/RELEASE_CANDIDATE.json" >/dev/null
python3 -m json.tool "$work/assets/opt/etoro-v2/current/config/v2-demo.json" >/dev/null
python3 -m json.tool "$work/assets/opt/etoro-v2/current/sbom.cdx.json" >/dev/null
(
  cd "$work/assets/opt/etoro-v2/current"
  sha256sum --check --strict SHA256SUMS.txt
  sha256sum --check --strict WHEELHOUSE_SHA256SUMS.txt
)
python3 - "$work/assets/opt/etoro-v2/current" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
release = json.loads((root / "RELEASE.json").read_text(encoding="utf-8"))
candidate = json.loads((root / "RELEASE_CANDIDATE.json").read_text(encoding="utf-8"))
assert release["commit"] == candidate["commit"]
assert release["tree"] == candidate["tree"]
assert release["requirements_sha256"] == hashlib.sha256(
    (root / "requirements.lock").read_bytes()
).hexdigest()
assert release["wheelhouse_manifest_sha256"] == hashlib.sha256(
    (root / "WHEELHOUSE_SHA256SUMS.txt").read_bytes()
).hexdigest()
PY

if [[ "${ETORO_V2_ALLOW_RESTORE_DRILL:-NO}" == YES ]]; then
    [[ -n "$admin_service" && -n "${PGSERVICEFILE:-}" && -s "$PGSERVICEFILE" ]]
    command -v curl >/dev/null 2>&1
    admin_dsn="service=$admin_service"
    drill_db="etoro_v2_restore_drill_$$"
    createdb --maintenance-db="$admin_dsn" "$drill_db"
    cleanup_drill() {
      if [[ -n "$dashboard_pid" ]] && kill -0 "$dashboard_pid" >/dev/null 2>&1; then
        kill "$dashboard_pid"
        wait "$dashboard_pid" || true
      fi
      dropdb --if-exists --maintenance-db="$admin_dsn" "$drill_db" >/dev/null 2>&1 || true
      rm -rf "$work"
    }
    trap cleanup_drill EXIT
    pg_restore --exit-on-error --no-owner --no-privileges \
      --dbname="$admin_dsn dbname=$drill_db" "$pg_backup"
    psql "$admin_dsn dbname=$drill_db" -v ON_ERROR_STOP=1 -Atqc \
      'SELECT count(*) FROM v2_events;' >/dev/null
    psql "$admin_dsn dbname=$drill_db" -Atqc "SELECT value FROM v2_meta WHERE key='schema_version';" \
      | grep -qx '5'
    psql "$admin_dsn dbname=$drill_db" -v ON_ERROR_STOP=1 -Atqc \
      "SELECT count(*) FROM v2_positions WHERE state->>'quantity' IS NULL OR status NOT IN ('OPEN','CLOSED');" \
      | grep -qx '0'
    psql "$admin_dsn dbname=$drill_db" -v ON_ERROR_STOP=1 -Atqc \
      "SELECT count(*) FROM v2_risk_reservations r JOIN v2_order_commands c USING(order_command_id) WHERE r.state='ACTIVE' AND c.reduce_only;" \
      | grep -qx '0'
    psql "$admin_dsn dbname=$drill_db" -v ON_ERROR_STOP=1 -Atqc \
      "SELECT count(*) FROM v2_outbox o LEFT JOIN v2_order_commands c ON c.order_command_id=o.payload->>'order_command_id' WHERE c.order_command_id IS NULL;" \
      | grep -qx '0'
    psql "$admin_dsn dbname=$drill_db" -v ON_ERROR_STOP=1 -Atqc \
      "WITH totals AS (
         SELECT c.intent_id,
                SUM(CASE WHEN c.reduce_only THEN -f.quantity ELSE f.quantity END) AS quantity,
                SUM(f.fee_usd) AS fees,
                SUM(f.financing_usd) AS financing
         FROM v2_fills f JOIN v2_order_commands c USING(order_command_id)
         GROUP BY c.intent_id
       )
       SELECT count(*) FROM v2_positions p LEFT JOIN totals t USING(intent_id)
       WHERE abs((p.state->>'quantity')::numeric-coalesce(t.quantity,0))>0.000000000001
          OR abs((p.state->>'fees_accrued')::numeric-coalesce(t.fees,0))>0.000000000001
          OR abs((p.state->>'financing_accrued')::numeric-coalesce(t.financing,0))>0.000000000001
          OR (p.status='OPEN' AND (p.state->>'quantity')::numeric<=0)
          OR (p.status='CLOSED' AND (p.state->>'quantity')::numeric<>0);" \
      | grep -qx '0'
    psql "$admin_dsn dbname=$drill_db" -v ON_ERROR_STOP=1 -Atqc \
      "WITH totals AS (
         SELECT order_command_id,SUM(quantity) AS quantity FROM v2_fills GROUP BY order_command_id
       )
       SELECT count(*) FROM v2_broker_orders b LEFT JOIN totals t USING(order_command_id)
       WHERE abs(b.filled_quantity-coalesce(t.quantity,0))>0.000000000001;" \
      | grep -qx '0'
    psql "$admin_dsn dbname=$drill_db" -v ON_ERROR_STOP=1 -Atqc \
      "SELECT count(*) FROM v2_outbox
       WHERE (claimed_by IS NULL)<> (claim_token IS NULL)
          OR (claim_token IS NULL)<> (lease_expires_at IS NULL);" \
      | grep -qx '0'
    psql "$admin_dsn dbname=$drill_db" -v ON_ERROR_STOP=1 -Atqc \
      "SELECT count(*) FROM v2_ai_packets
       WHERE (state='CLAIMED' AND (claimed_by IS NULL OR claim_token IS NULL OR lease_expires_at IS NULL))
          OR (state<>'CLAIMED' AND (claimed_by IS NOT NULL OR claim_token IS NOT NULL OR lease_expires_at IS NOT NULL))
          OR ((apply_claimed_by IS NULL)<>(apply_claim_token IS NULL))
          OR ((apply_claim_token IS NULL)<>(apply_lease_expires_at IS NULL))
          OR (apply_claim_token IS NOT NULL AND state<>'DECIDED')
          OR (state='DEAD_LETTER' AND (terminal_reason IS NULL OR dead_lettered_at IS NULL));" \
      | grep -qx '0'
    "$release/.venv/bin/python" - "$admin_dsn dbname=$drill_db" \
      "$work/assets/opt/etoro-v2/current/config/v2-demo.json" <<'PY'
import sys
from etoro_agent.dashboard_v2 import PostgresDashboardServiceV2
from etoro_agent.postgres_runtime_v2 import PostgresRuntimeStoreV2
store = PostgresRuntimeStoreV2.from_dsn(sys.argv[1])
store.require_schema()
assert store.verify_event_chain()
store.positions()
store.pending_outbox()
store.close()
snapshot = PostgresDashboardServiceV2(sys.argv[1], sys.argv[2]).snapshot()
assert snapshot["real_money"] is False
assert snapshot["account_mode"] == "DEMO"
PY
    dashboard_dsn_file="$work/dashboard-dsn"
    proxy_secret="$work/proxy-secret"
    dashboard_socket="$work/dashboard.sock"
    printf '%s dbname=%s\n' "$admin_dsn" "$drill_db" >"$dashboard_dsn_file"
    printf 'restore-drill-boundary\n' >"$proxy_secret"
    ETORO_DASHBOARD_OWNER=restore-drill \
      ETORO_PROXY_SECRET_FILE="$proxy_secret" \
      "$release/.venv/bin/python" -m etoro_agent.dashboard_worker_v2 \
      --postgres-dsn-file "$dashboard_dsn_file" \
      --config "$work/assets/opt/etoro-v2/current/config/v2-demo.json" \
      --uds "$dashboard_socket" >"$work/dashboard.log" 2>&1 &
    dashboard_pid=$!
    for _ in $(seq 1 50); do
      [[ -S "$dashboard_socket" ]] && break
      kill -0 "$dashboard_pid" >/dev/null 2>&1
      sleep 0.1
    done
    [[ -S "$dashboard_socket" ]]
    curl --fail --silent --show-error --unix-socket "$dashboard_socket" \
      -H 'x-etoro-proxy-secret: restore-drill-boundary' \
      -H 'x-authentik-username: restore-drill' \
      http://localhost/api/v2/snapshot \
      | python3 -c 'import json,sys; value=json.load(sys.stdin); assert value["account_mode"]=="DEMO" and value["real_money"] is False'
    kill "$dashboard_pid"
    wait "$dashboard_pid" || true
    dashboard_pid=
    dropdb --maintenance-db="$admin_dsn" "$drill_db"
    trap 'rm -rf "$work"' EXIT
fi

printf 'ETORO_V2_RESTORE_DRILL_OK sqlite=%s postgres_archive=%s full_postgres=%s\n' \
  "${sqlite_backup:-none}" "${pg_backup:-none}" "${ETORO_V2_ALLOW_RESTORE_DRILL:-NO}"
