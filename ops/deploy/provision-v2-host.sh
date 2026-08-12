#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ ${EUID} -eq 0 ]] || {
  printf 'ETORO_V2_PROVISION_ERROR=root_required\n' >&2
  exit 1
}

release=${1:-/opt/etoro-v2/current}
pg_port=${ETORO_V2_POSTGRES_PORT:-5434}
[[ -x "$release/.venv/bin/python" && -s "$release/RELEASE.json" ]] || {
  printf 'ETORO_V2_PROVISION_ERROR=immutable_release_missing\n' >&2
  exit 1
}
[[ "$pg_port" =~ ^[0-9]+$ ]] || {
  printf 'ETORO_V2_PROVISION_ERROR=postgres_port_invalid\n' >&2
  exit 1
}

install -D -o root -g root -m 0644 \
  "$release/ops/systemd/etoro-v2.sysusers" /etc/sysusers.d/etoro-v2.conf
install -D -o root -g root -m 0644 \
  "$release/ops/systemd/etoro-v2.tmpfiles" /etc/tmpfiles.d/etoro-v2.conf
systemd-sysusers /etc/sysusers.d/etoro-v2.conf
systemd-tmpfiles --create /etc/tmpfiles.d/etoro-v2.conf

install -d -o root -g root -m 0700 /etc/etoro-agent
install -d -o root -g root -m 0755 /etc/etoro-v2-control
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

sudo -u postgres psql -p "$pg_port" -d postgres -v ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='etoro-v2-owner') THEN
    CREATE ROLE "etoro-v2-owner" NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='etoro-engine') THEN
    CREATE ROLE "etoro-engine" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
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
ALTER ROLE "etoro-engine" NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE "etoro-executor" NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE "etoro-observer" NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE "etoro-collector" NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
SQL

database_exists=$(sudo -u postgres psql -p "$pg_port" -d postgres -Atqc "SELECT 1 FROM pg_database WHERE datname='etoro_v2'")
if [[ "$database_exists" != 1 ]]; then
  sudo -u postgres createdb -p "$pg_port" -O etoro-v2-owner etoro_v2
fi
sudo -u postgres psql -p "$pg_port" -d postgres -v ON_ERROR_STOP=1 \
  -c 'ALTER DATABASE etoro_v2 OWNER TO "etoro-v2-owner"' >/dev/null

printf 'dbname=etoro_v2 host=/var/run/postgresql port=%s user=etoro-engine\n' "$pg_port" \
  >/etc/etoro-agent/postgres-v2-engine-dsn
printf 'dbname=etoro_v2 host=/var/run/postgresql port=%s user=etoro-executor\n' "$pg_port" \
  >/etc/etoro-agent/postgres-v2-executor-dsn
printf 'dbname=etoro_v2 host=/var/run/postgresql port=%s user=etoro-observer\n' "$pg_port" \
  >/etc/etoro-agent/postgres-v2-observer-dsn
printf 'dbname=etoro_v2 host=/var/run/postgresql port=%s user=etoro-collector\n' "$pg_port" \
  >/etc/etoro-agent/postgres-v2-collector-dsn
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
  -f "$release/ops/postgres/grants_v2.sql" >/dev/null
rm -f "$migration_dsn"
trap - EXIT

if [[ ! -e /etc/etoro-agent/v2-risk-signing.key && ! -e /etc/etoro-agent/v2-risk-verifying.pub ]]; then
  "$release/.venv/bin/python" -c 'from etoro_agent.risk import generate_signing_keypair; generate_signing_keypair("/etc/etoro-agent/v2-risk-signing.key", "/etc/etoro-agent/v2-risk-verifying.pub")'
elif [[ ! -s /etc/etoro-agent/v2-risk-signing.key || ! -s /etc/etoro-agent/v2-risk-verifying.pub ]]; then
  printf 'ETORO_V2_PROVISION_ERROR=risk_keypair_incomplete\n' >&2
  exit 1
fi
if [[ ! -e /etc/etoro-agent/v2-anchor-signing.key && ! -e /etc/etoro-agent/v2-anchor-verifying.pub ]]; then
  "$release/.venv/bin/python" -c 'from etoro_agent.risk import generate_signing_keypair; generate_signing_keypair("/etc/etoro-agent/v2-anchor-signing.key", "/etc/etoro-agent/v2-anchor-verifying.pub")'
elif [[ ! -s /etc/etoro-agent/v2-anchor-signing.key || ! -s /etc/etoro-agent/v2-anchor-verifying.pub ]]; then
  printf 'ETORO_V2_PROVISION_ERROR=anchor_keypair_incomplete\n' >&2
  exit 1
fi
chown root:root /etc/etoro-agent/v2-*-signing.key /etc/etoro-agent/v2-*-verifying.pub
chmod 0600 /etc/etoro-agent/v2-*-signing.key
chmod 0644 /etc/etoro-agent/v2-*-verifying.pub

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

# Retire the mutable-checkout v1 writer before any v2 unit can be installed.
# Preserve an exact local copy if the legacy unit was installed in /etc, then
# replace its name with a persistent mask so it cannot regain credentials.
systemctl disable --now etoro-demo-executor.service >/dev/null 2>&1 || true
legacy_unit=/etc/systemd/system/etoro-demo-executor.service
if [[ -f "$legacy_unit" && ! -L "$legacy_unit" ]]; then
  install -d -o root -g root -m 0700 /var/lib/etoro-v2/retired-units
  install -o root -g root -m 0600 \
    "$legacy_unit" /var/lib/etoro-v2/retired-units/etoro-demo-executor.service
  sha256sum /var/lib/etoro-v2/retired-units/etoro-demo-executor.service \
    >/var/lib/etoro-v2/retired-units/etoro-demo-executor.service.sha256
  rm -f "$legacy_unit"
fi
systemctl mask --now etoro-demo-executor.service >/dev/null
[[ "$(systemctl is-enabled etoro-demo-executor.service 2>/dev/null || true)" == masked ]] || {
  printf 'ETORO_V2_PROVISION_ERROR=legacy_executor_not_masked\n' >&2
  exit 1
}
if systemctl is-active --quiet etoro-demo-executor.service; then
  printf 'ETORO_V2_PROVISION_ERROR=legacy_executor_active\n' >&2
  exit 1
fi
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
