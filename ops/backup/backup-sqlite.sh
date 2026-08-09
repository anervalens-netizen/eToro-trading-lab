#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

source_db=/var/lib/etoro-agent/audit.sqlite3
primary_root=/storage/backups/db/etoro
ssd_root=/opt/Mobiup/ops/backups/etoro
stamp="$(date -u +%Y%m%d_%H%M%S)"
primary_partial="$primary_root/audit_${stamp}.sqlite3.partial"
primary_target="$primary_root/audit_${stamp}.sqlite3"
ssd_target="$ssd_root/audit_${stamp}.sqlite3"

test -f "$source_db"
test -d "$primary_root"
test -d "$ssd_root"

sqlite3 "$source_db" ".backup '$primary_partial'"
test "$(sqlite3 "$primary_partial" 'PRAGMA integrity_check;')" = ok
mv "$primary_partial" "$primary_target"
install -m 0600 "$primary_target" "$ssd_target"
test "$(sqlite3 "$ssd_target" 'PRAGMA integrity_check;')" = ok
sha256sum "$primary_target" >"$primary_target.sha256"
sha256sum "$ssd_target" >"$ssd_target.sha256"

printf 'ETORO_BACKUP_OK stamp=%s bytes=%s\n' "$stamp" "$(stat -c %s "$primary_target")"
