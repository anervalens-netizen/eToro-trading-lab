#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

runtime_db=${ETORO_V2_SQLITE_DB:-/var/lib/etoro-agent/v2.sqlite3}
backup_root=${ETORO_V2_BACKUP_ROOT:-/storage/backups/db/etoro/v2}
postgres_dsn_file=${ETORO_V2_POSTGRES_DSN_FILE:-}
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_root"

sqlite_target="$backup_root/v2_${stamp}.sqlite3"
if [[ -f "$runtime_db" ]]; then
  sqlite3 "$runtime_db" ".backup '$sqlite_target.partial'"
  [[ "$(sqlite3 "$sqlite_target.partial" 'PRAGMA integrity_check;')" == ok ]]
  mv "$sqlite_target.partial" "$sqlite_target"
  sha256sum "$sqlite_target" >"$sqlite_target.sha256"
fi

[[ -n "$postgres_dsn_file" && -s "$postgres_dsn_file" ]] || {
  printf 'ETORO_V2_BACKUP_ERROR=postgres_dsn_unavailable\n' >&2
  exit 1
}
command -v pg_dump >/dev/null 2>&1 || {
  printf 'ETORO_V2_BACKUP_ERROR=pg_dump_unavailable\n' >&2
  exit 1
}
command -v pg_restore >/dev/null 2>&1 || {
  printf 'ETORO_V2_BACKUP_ERROR=pg_restore_unavailable\n' >&2
  exit 1
}
pg_target="$backup_root/v2_${stamp}.pgdump"
PGDATABASE="$(<"$postgres_dsn_file")" \
  pg_dump --format=custom --compress=6 --file="$pg_target.partial"
pg_restore --list "$pg_target.partial" >/dev/null
mv "$pg_target.partial" "$pg_target"
sha256sum "$pg_target" >"$pg_target.sha256"

find "$backup_root" -type f -mtime +45 -delete
printf 'ETORO_V2_BACKUP_OK stamp=%s sqlite=%s postgres=%s\n' "$stamp" "${sqlite_target:-none}" "${pg_target:-none}"
