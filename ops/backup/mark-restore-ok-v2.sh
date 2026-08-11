#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ ${EUID} -eq 0 ]] || {
  printf 'ETORO_V2_RESTORE_MARKER_ERROR=root_required\n' >&2
  exit 1
}

backup_root=${ETORO_V2_BACKUP_ROOT:-/storage/backups/db/etoro/v2}
[[ -d "$backup_root" && ! -L "$backup_root" ]] || {
  printf 'ETORO_V2_RESTORE_MARKER_ERROR=backup_root_invalid\n' >&2
  exit 1
}

marker="$backup_root/LAST_RESTORE_DRILL_OK"
partial=$(mktemp "$backup_root/.LAST_RESTORE_DRILL_OK.XXXXXX")
cleanup() {
  [[ ! -e "$partial" ]] || rm -f -- "$partial"
}
trap cleanup EXIT

date -u +%Y-%m-%dT%H:%M:%SZ >"$partial"
chown etoro-observer:postgres "$partial"
chmod 0640 "$partial"
if command -v setfacl >/dev/null 2>&1; then
  setfacl -m u:andrei:r-- "$partial"
fi
mv -f -- "$partial" "$marker"
trap - EXIT

printf 'ETORO_V2_RESTORE_MARKER_OK path=%s\n' "$marker"
