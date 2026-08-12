#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

assert_v2_provision_quiescent() {
  local execution_gate=${ETORO_V2_EXECUTION_GATE_FILE:-/etc/etoro-v2-control/ENABLE_DEMO_EXECUTION}
  local systemctl_bin=${ETORO_V2_SYSTEMCTL_BIN:-systemctl}
  local unit unit_state unit_rc
  local -a writer_units=(
    etoro-v2-decision-apply-execution.service
    etoro-v2-executor-postgres.service
    etoro-v2-exit-manager.service
  )

  if [[ -e "$execution_gate" || -L "$execution_gate" ]]; then
    printf 'ETORO_V2_PROVISION_ERROR=execution_gate_present\n' >&2
    return 1
  fi
  if [[ ! -x "$systemctl_bin" ]] && ! command -v "$systemctl_bin" >/dev/null 2>&1; then
    printf 'ETORO_V2_PROVISION_ERROR=writer_state_unverifiable\n' >&2
    return 1
  fi
  for unit in "${writer_units[@]}"; do
    unit_state=
    unit_rc=0
    unit_state=$("$systemctl_bin" is-active "$unit" 2>/dev/null) || unit_rc=$?
    case "$unit_rc:$unit_state" in
      3:inactive|4:unknown) ;;
      0:active)
        printf 'ETORO_V2_PROVISION_ERROR=writer_not_inactive unit=%s state=active\n' \
          "$unit" >&2
        return 1
        ;;
      *)
        printf 'ETORO_V2_PROVISION_ERROR=writer_state_unverifiable unit=%s state=%s\n' \
          "$unit" "${unit_state:-unverifiable}" >&2
        return 1
        ;;
    esac
  done
}

if [[ ${ETORO_V2_PROVISION_LIB_ONLY:-0} == 1 ]]; then
  if [[ ${BASH_SOURCE[0]} != "$0" ]]; then
    return 0
  fi
  exit 0
fi

[[ ${EUID} -eq 0 ]] || {
  printf 'ETORO_V2_PROVISION_ERROR=root_required\n' >&2
  exit 1
}

release=${1:-/opt/etoro-v2/current}
mode=${2:-full}
pg_port=${ETORO_V2_POSTGRES_PORT:-5434}
[[ "$mode" == full || "$mode" == --bootstrap-control ]] || {
  printf 'ETORO_V2_PROVISION_ERROR=mode_invalid\n' >&2
  exit 1
}
[[ -x "$release/.venv/bin/python" && -s "$release/RELEASE.json" ]] || {
  printf 'ETORO_V2_PROVISION_ERROR=immutable_release_missing\n' >&2
  exit 1
}
assert_v2_provision_quiescent
[[ "$pg_port" =~ ^[0-9]+$ ]] || {
  printf 'ETORO_V2_PROVISION_ERROR=postgres_port_invalid\n' >&2
  exit 1
}

# A pre-existing control plane must be proven dormant before bootstrap changes
# any OS identity, credential file, database role, schema, or grant.
database_exists=$(sudo -u postgres psql -p "$pg_port" -d postgres -Atqc \
  "SELECT 1 FROM pg_database WHERE datname='etoro_v2'")
if [[ "$database_exists" == 1 ]]; then
  state_relation=$(sudo -u postgres psql -p "$pg_port" -d etoro_v2 -Atqc \
    "SELECT to_regclass('public.v2_trading_state') IS NOT NULL")
  [[ "$state_relation" == t ]] || {
    printf 'ETORO_V2_PROVISION_ERROR=preexisting_trading_state_unverifiable\n' >&2
    exit 1
  }
  pre_migration_state=$(sudo -u postgres psql -p "$pg_port" -d etoro_v2 -Atqc \
    "SELECT state FROM v2_trading_state WHERE singleton=TRUE")
  [[ "$pre_migration_state" == LOCKED ]] || {
    printf 'ETORO_V2_PROVISION_ERROR=preexisting_trading_state_not_locked state=%s\n' \
      "${pre_migration_state:-unverifiable}" >&2
    exit 1
  }
fi

install -D -o root -g root -m 0644 \
  "$release/ops/systemd/etoro-v2.sysusers" /etc/sysusers.d/etoro-v2.conf
install -D -o root -g root -m 0644 \
  "$release/ops/systemd/etoro-v2.tmpfiles" /etc/tmpfiles.d/etoro-v2.conf
systemd-sysusers /etc/sysusers.d/etoro-v2.conf
systemd-tmpfiles --create /etc/tmpfiles.d/etoro-v2.conf

install -d -o root -g root -m 0700 /etc/etoro-agent
install -d -o root -g root -m 0755 /etc/etoro-v2-control
if [[ "$mode" == full ]]; then
  install -o root -g root -m 0600 "$release/config/v2-demo.json" /etc/etoro-agent/v2-demo.json
  install -o root -g root -m 0600 "$release/config/v2-demo-execution.json" /etc/etoro-agent/v2-demo-execution.json
  command -v setfacl >/dev/null 2>&1 || {
    printf 'ETORO_V2_PROVISION_ERROR=setfacl_unavailable\n' >&2
    exit 1
  }
  for ancestor in /storage/backups /storage/backups/db /storage/backups/db/etoro; do
    [[ -d "$ancestor" ]] || {
      printf 'ETORO_V2_PROVISION_ERROR=backup_ancestor_missing\n' >&2
      exit 1
    }
    setfacl -m u:etoro-observer:--x,u:postgres:--x "$ancestor"
  done
  install -d -o etoro-observer -g postgres -m 2770 /storage/backups/db/etoro/v2
  install -d -o etoro-observer -g etoro-observer -m 0750 /storage/backups/db/etoro/v2-anchors
  setfacl -m u:andrei:r-x /storage/backups/db/etoro/v2 /storage/backups/db/etoro/v2-anchors
  install -d -o andrei -g etoro-observer -m 0750 /var/lib/etoro-v2-offhost
fi

sudo -u postgres psql -p "$pg_port" -d postgres -v ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='etoro-v2-owner') THEN
    CREATE ROLE "etoro-v2-owner" NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='etoro-engine') THEN
    CREATE ROLE "etoro-engine" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='etoro-candidate') THEN
    CREATE ROLE "etoro-candidate" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='etoro-ai') THEN
    CREATE ROLE "etoro-ai" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='etoro-decision') THEN
    CREATE ROLE "etoro-decision" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='etoro-exit') THEN
    CREATE ROLE "etoro-exit" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='etoro-reconciler') THEN
    CREATE ROLE "etoro-reconciler" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='etoro-control') THEN
    CREATE ROLE "etoro-control" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='etoro-executor') THEN
    CREATE ROLE "etoro-executor" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='etoro-observer') THEN
    CREATE ROLE "etoro-observer" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='etoro-collector') THEN
    CREATE ROLE "etoro-collector" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
END
$$;
ALTER ROLE "etoro-engine" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE "etoro-candidate" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE "etoro-ai" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE "etoro-decision" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE "etoro-exit" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE "etoro-reconciler" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE "etoro-control" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE "etoro-executor" NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE "etoro-observer" NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE "etoro-collector" NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
SQL

if [[ "$database_exists" != 1 ]]; then
  sudo -u postgres createdb -p "$pg_port" -O etoro-v2-owner etoro_v2
fi
sudo -u postgres psql -p "$pg_port" -d postgres -v ON_ERROR_STOP=1 \
  -c 'ALTER DATABASE etoro_v2 OWNER TO "etoro-v2-owner"' >/dev/null

printf 'dbname=etoro_v2 host=/var/run/postgresql port=%s user=etoro-candidate\n' "$pg_port" \
  >/etc/etoro-agent/postgres-v2-candidate-dsn
printf 'dbname=etoro_v2 host=/var/run/postgresql port=%s user=etoro-ai\n' "$pg_port" \
  >/etc/etoro-agent/postgres-v2-ai-dsn
printf 'dbname=etoro_v2 host=/var/run/postgresql port=%s user=etoro-decision\n' "$pg_port" \
  >/etc/etoro-agent/postgres-v2-decision-dsn
printf 'dbname=etoro_v2 host=/var/run/postgresql port=%s user=etoro-exit\n' "$pg_port" \
  >/etc/etoro-agent/postgres-v2-exit-dsn
printf 'dbname=etoro_v2 host=/var/run/postgresql port=%s user=etoro-reconciler\n' "$pg_port" \
  >/etc/etoro-agent/postgres-v2-reconciler-dsn
printf 'dbname=etoro_v2 host=/var/run/postgresql port=%s user=etoro-control\n' "$pg_port" \
  >/etc/etoro-agent/postgres-v2-control-dsn
printf 'dbname=etoro_v2 host=/var/run/postgresql port=%s user=etoro-executor\n' "$pg_port" \
  >/etc/etoro-agent/postgres-v2-executor-dsn
printf 'dbname=etoro_v2 host=/var/run/postgresql port=%s user=etoro-observer\n' "$pg_port" \
  >/etc/etoro-agent/postgres-v2-observer-dsn
printf 'dbname=etoro_v2 host=/var/run/postgresql port=%s user=etoro-collector\n' "$pg_port" \
  >/etc/etoro-agent/postgres-v2-collector-dsn
rm -f /etc/etoro-agent/postgres-v2-engine-dsn
chown root:root /etc/etoro-agent/postgres-v2-*-dsn
chmod 0600 /etc/etoro-agent/postgres-v2-*-dsn
printf '[etoro_v2_backup]\ndbname=etoro_v2\nhost=/var/run/postgresql\nport=%s\nuser=etoro-observer\n' \
  "$pg_port" >/etc/etoro-agent/postgres-v2-backup.conf
printf '[etoro_v2_restore]\nhost=/var/run/postgresql\nport=%s\nuser=postgres\n' \
  "$pg_port" >/etc/etoro-agent/postgres-v2-restore.conf
chown root:root /etc/etoro-agent/postgres-v2-backup.conf /etc/etoro-agent/postgres-v2-restore.conf
chmod 0600 /etc/etoro-agent/postgres-v2-backup.conf /etc/etoro-agent/postgres-v2-restore.conf

migration_dsn=$(mktemp /run/etoro-v2-migration-dsn.XXXXXX)
trap 'rm -f "$migration_dsn"' EXIT
printf 'dbname=etoro_v2 host=/var/run/postgresql port=%s user=postgres\n' "$pg_port" >"$migration_dsn"
chown postgres:postgres "$migration_dsn"
chmod 0600 "$migration_dsn"
sudo -u postgres "$release/.venv/bin/python" -m etoro_agent.postgres_migrate_v2 \
  --dsn-file "$migration_dsn" --set-role etoro-v2-owner
sudo -u postgres psql -p "$pg_port" -d etoro_v2 -v ON_ERROR_STOP=1 \
  --single-transaction -f "$release/ops/postgres/grants_v2.sql" >/dev/null
rm -f "$migration_dsn"
trap - EXIT

post_migration_state=$("$release/.venv/bin/python" - /etc/etoro-agent/postgres-v2-control-dsn <<'PY'
from pathlib import Path
import sys

import psycopg

dsn = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
with psycopg.connect(dsn) as connection:
    connection.read_only = True
    with connection.cursor() as cursor:
        cursor.execute("SELECT state FROM v2_trading_state WHERE singleton=TRUE")
        rows = cursor.fetchall()
    connection.rollback()
if len(rows) != 1:
    raise RuntimeError("PostgreSQL trading state singleton is not uniquely verifiable")
print(str(rows[0][0]))
PY
) || {
  printf 'ETORO_V2_PROVISION_ERROR=post_migration_trading_state_unverifiable\n' >&2
  exit 1
}
[[ "$post_migration_state" == LOCKED ]] || {
  printf 'ETORO_V2_PROVISION_ERROR=post_migration_trading_state_not_locked state=%s\n' \
    "$post_migration_state" >&2
  exit 1
}

if [[ "$mode" == --bootstrap-control ]]; then
  printf 'ETORO_V2_PROVISION_BOOTSTRAP_OK postgres_port=%s state=LOCKED writers=inactive\n' \
    "$pg_port"
  exit 0
fi

if [[ ! -e /etc/etoro-agent/v2-risk-signing.key && ! -e /etc/etoro-agent/v2-risk-verifying.pub ]]; then
  "$release/.venv/bin/python" -c 'from etoro_agent.signing_keys_v2 import generate_signing_keypair; generate_signing_keypair("/etc/etoro-agent/v2-risk-signing.key", "/etc/etoro-agent/v2-risk-verifying.pub")'
elif [[ ! -s /etc/etoro-agent/v2-risk-signing.key || ! -s /etc/etoro-agent/v2-risk-verifying.pub ]]; then
  printf 'ETORO_V2_PROVISION_ERROR=risk_keypair_incomplete\n' >&2
  exit 1
fi
if [[ ! -e /etc/etoro-agent/v2-anchor-signing.key && ! -e /etc/etoro-agent/v2-anchor-verifying.pub ]]; then
  "$release/.venv/bin/python" -c 'from etoro_agent.signing_keys_v2 import generate_signing_keypair; generate_signing_keypair("/etc/etoro-agent/v2-anchor-signing.key", "/etc/etoro-agent/v2-anchor-verifying.pub")'
elif [[ ! -s /etc/etoro-agent/v2-anchor-signing.key || ! -s /etc/etoro-agent/v2-anchor-verifying.pub ]]; then
  printf 'ETORO_V2_PROVISION_ERROR=anchor_keypair_incomplete\n' >&2
  exit 1
fi
chown root:root /etc/etoro-agent/v2-*-signing.key /etc/etoro-agent/v2-*-verifying.pub
chmod 0600 /etc/etoro-agent/v2-*-signing.key
chmod 0644 /etc/etoro-agent/v2-*-verifying.pub

# Pin only non-secret hashes for the ChatGPT-authenticated Codex boundary. The
# account token/identity value itself never leaves auth.json and is never logged.
codex_auth=/home/andrei/.codex/auth.json
codex_binary=/usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex
if [[ -s "$codex_auth" && -x "$codex_binary" ]]; then
  codex_account_hash=$("$release/.venv/bin/python" - "$codex_auth" <<'PY'
import hashlib
import json
import pathlib
import sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert value.get("auth_mode") == "chatgpt" and value.get("OPENAI_API_KEY") in (None, "")
account_id = value.get("tokens", {}).get("account_id")
assert isinstance(account_id, str) and account_id.strip()
print(hashlib.sha256(account_id.strip().encode()).hexdigest())
PY
  )
  codex_executable_hash=$(sha256sum "$codex_binary" | awk '{print $1}')
  for name_value in \
    "v2-codex-account.sha256:$codex_account_hash" \
    "v2-codex-executable.sha256:$codex_executable_hash"; do
    name=${name_value%%:*}
    value=${name_value#*:}
    target="/etc/etoro-agent/$name"
    if [[ -e "$target" ]]; then
      [[ "$(tr -d '\n' <"$target")" == "$value" ]] || {
        printf 'ETORO_V2_PROVISION_ERROR=codex_attestation_drift file=%s\n' "$name" >&2
        exit 1
      }
    else
      printf '%s\n' "$value" >"$target"
    fi
    chown root:root "$target"
    chmod 0644 "$target"
  done
fi

# Market data is public research evidence. Grant the backup-only observer read
# access without broadening collector write authority.
setfacl -m u:etoro-observer:--x /var/lib/etoro-collector
setfacl -m d:u:etoro-observer:rX /var/lib/etoro-collector
setfacl -R -m u:etoro-observer:rX /var/lib/etoro-collector/data-v2
setfacl -m d:u:etoro-observer:rX /var/lib/etoro-collector/data-v2
if [[ -e /var/lib/etoro-collector/market-archive-v2.sqlite3 ]]; then
  setfacl -m u:etoro-observer:r-- /var/lib/etoro-collector/market-archive-v2.sqlite3
fi

systemctl disable --now \
  etoro-v2-executor.service \
  etoro-v2-executor-current.service \
  etoro-v2-decision-apply-execution.service \
  etoro-v2-executor-postgres.service >/dev/null 2>&1 || true

# Retire every mutable-checkout v1 unit before installing canonical V2. Keep a
# content-addressed forensic copy, then reserve each legacy name with a mask.
legacy_units=(
  etoro-backup.service
  etoro-backup.timer
  etoro-dashboard.service
  etoro-demo-executor.service
  etoro-minimax-runner.service
  etoro-news-scanner.service
  etoro-shadow.service
  etoro-sol-runner.service
)
systemctl disable --now "${legacy_units[@]}" >/dev/null 2>&1 || true
install -d -o root -g root -m 0700 /var/lib/etoro-v2/retired-units
for legacy_name in "${legacy_units[@]}"; do
  legacy_path="/etc/systemd/system/$legacy_name"
  if [[ -f "$legacy_path" && ! -L "$legacy_path" ]]; then
    legacy_hash=$(sha256sum "$legacy_path" | awk '{print $1}')
    install -o root -g root -m 0600 "$legacy_path" \
      "/var/lib/etoro-v2/retired-units/${legacy_name}.${legacy_hash}"
  fi
  if [[ -e "$legacy_path" || -L "$legacy_path" ]]; then
    rm -f "$legacy_path"
  fi
done
systemctl daemon-reload
systemctl mask --now "${legacy_units[@]}" >/dev/null
for legacy_name in "${legacy_units[@]}"; do
  [[ "$(systemctl is-enabled "$legacy_name" 2>/dev/null || true)" == masked ]] || {
    printf 'ETORO_V2_PROVISION_ERROR=legacy_runtime_not_masked unit=%s\n' \
      "$legacy_name" >&2
    exit 1
  }
  if systemctl is-active --quiet "$legacy_name"; then
    printf 'ETORO_V2_PROVISION_ERROR=legacy_runtime_active unit=%s\n' \
      "$legacy_name" >&2
    exit 1
  fi
done
rm -f \
  /etc/systemd/system/etoro-v2-executor.service \
  /etc/systemd/system/etoro-v2-executor-current.service
install -o root -g root -m 0644 "$release"/ops/systemd/etoro-v2-*.service /etc/systemd/system/
install -o root -g root -m 0644 "$release"/ops/systemd/etoro-v2-*.socket /etc/systemd/system/
install -o root -g root -m 0644 "$release"/ops/systemd/etoro-v2-*.timer /etc/systemd/system/
install -o root -g root -m 0644 "$release"/ops/systemd/etoro-v2-*.path /etc/systemd/system/
install -o root -g root -m 0644 "$release"/ops/systemd/etoro-v2-*.target /etc/systemd/system/
systemctl daemon-reload
[[ ! -e /etc/etoro-agent/ENABLE_V2_DEMO_EXECUTION ]] || {
  printf 'ETORO_V2_PROVISION_ERROR=retired_execution_gate_present\n' >&2
  exit 1
}
[[ ! -e /etc/etoro-v2-control/ENABLE_DEMO_EXECUTION ]] || {
  printf 'ETORO_V2_PROVISION_ERROR=unexpected_execution_gate\n' >&2
  exit 1
}
systemctl enable --now etoro-v2-execution-gate.path >/dev/null
systemctl start etoro-v2-execution-gate-lock.target
systemctl enable --now \
  etoro-v2-anchor.timer \
  etoro-v2-backup.timer \
  etoro-v2-restore-drill.timer \
  etoro-v2-offhost-backup.timer >/dev/null

read_key=missing
write_key=missing
[[ -s /etc/etoro-agent/etoro-demo-read-user-key ]] && read_key=present
[[ -s /etc/etoro-agent/etoro-demo-write-user-key ]] && write_key=present
printf 'ETORO_V2_PROVISION_OK postgres_port=%s read_key=%s write_key=%s executor=disabled\n' \
  "$pg_port" "$read_key" "$write_key"
