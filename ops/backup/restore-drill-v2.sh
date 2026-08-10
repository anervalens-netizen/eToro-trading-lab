#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

backup_root=${ETORO_V2_BACKUP_ROOT:-/storage/backups/db/etoro/v2}
service_file=${ETORO_V2_PGSERVICEFILE:-/etc/etoro-agent/postgres-v2.conf}
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

sqlite_backup="$(find "$backup_root" -maxdepth 1 -type f -name 'v2_*.sqlite3' -printf '%T@ %p\n' | sort -nr | head -1 | cut -d' ' -f2-)"
if [[ -n "$sqlite_backup" ]]; then
  cp "$sqlite_backup" "$work/restore.sqlite3"
  [[ "$(sqlite3 "$work/restore.sqlite3" 'PRAGMA integrity_check;')" == ok ]]
  sqlite3 "$work/restore.sqlite3" 'SELECT COUNT(*) FROM v2_events;' >/dev/null
fi

pg_backup="$(find "$backup_root" -maxdepth 1 -type f -name 'v2_*.pgdump' -printf '%T@ %p\n' | sort -nr | head -1 | cut -d' ' -f2-)"
if [[ -n "$pg_backup" ]]; then
  pg_restore --list "$pg_backup" >/dev/null
  if [[ "${ETORO_V2_ALLOW_RESTORE_DRILL:-NO}" == YES ]]; then
    [[ -f "$service_file" ]]
    export PGSERVICEFILE="$service_file"
    drill_db="etoro_v2_restore_drill_$$"
    createdb --maintenance-db='service=etoro_v2_drill' "$drill_db"
    trap 'dropdb --if-exists --maintenance-db="service=etoro_v2_drill" "$drill_db" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
    pg_restore --exit-on-error --no-owner --no-privileges --dbname="service=etoro_v2_drill dbname=$drill_db" "$pg_backup"
    psql "service=etoro_v2_drill dbname=$drill_db" -Atqc 'SELECT count(*) FROM v2_events;' >/dev/null
    dropdb --maintenance-db='service=etoro_v2_drill' "$drill_db"
  fi
fi

printf 'ETORO_V2_RESTORE_DRILL_OK sqlite=%s postgres_archive=%s full_postgres=%s\n' \
  "${sqlite_backup:-none}" "${pg_backup:-none}" "${ETORO_V2_ALLOW_RESTORE_DRILL:-NO}"
