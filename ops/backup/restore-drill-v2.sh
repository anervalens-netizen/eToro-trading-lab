#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

backup_root=${ETORO_V2_BACKUP_ROOT:-/storage/backups/db/etoro/v2}
admin_service=${ETORO_V2_RESTORE_SERVICE:-}
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
    [[ -n "$admin_service" && -n "${PGSERVICEFILE:-}" && -s "$PGSERVICEFILE" ]]
    admin_dsn="service=$admin_service"
    drill_db="etoro_v2_restore_drill_$$"
    createdb --maintenance-db="$admin_dsn" "$drill_db"
    cleanup_drill() {
      dropdb --if-exists --maintenance-db="$admin_dsn" "$drill_db" >/dev/null 2>&1 || true
      rm -rf "$work"
    }
    trap cleanup_drill EXIT
    pg_restore --exit-on-error --no-owner --no-privileges \
      --dbname="$admin_dsn dbname=$drill_db" "$pg_backup"
    psql "$admin_dsn dbname=$drill_db" -Atqc 'SELECT count(*) FROM v2_events;' >/dev/null
    psql "$admin_dsn dbname=$drill_db" -Atqc "SELECT value FROM v2_meta WHERE key='schema_version';" \
      | grep -qx '2'
    dropdb --maintenance-db="$admin_dsn" "$drill_db"
    trap 'rm -rf "$work"' EXIT
  fi
fi

printf 'ETORO_V2_RESTORE_DRILL_OK sqlite=%s postgres_archive=%s full_postgres=%s\n' \
  "${sqlite_backup:-none}" "${pg_backup:-none}" "${ETORO_V2_ALLOW_RESTORE_DRILL:-NO}"
