#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

backup_root=${ETORO_V2_BACKUP_ROOT:-/storage/backups/db/etoro/v2}
anchor_root=${ETORO_V2_ANCHOR_ROOT:-/storage/backups/db/etoro/v2-anchors}
offhost_root=${ETORO_V2_OFFHOST_ROOT:-/mnt/nas/backups/server-68/etoro}
health_root=${ETORO_V2_OFFHOST_HEALTH_ROOT:-/var/lib/etoro-v2-offhost}

[[ -d "$backup_root" && -d "$anchor_root" ]] || {
  printf 'ETORO_V2_OFFHOST_ERROR=source_unavailable\n' >&2
  exit 1
}
mount_type=$(findmnt -n -o FSTYPE -T "$offhost_root" 2>/dev/null || true)
case "$mount_type" in
  cifs | nfs | nfs4) ;;
  *)
    printf 'ETORO_V2_OFFHOST_ERROR=destination_not_remote fstype=%s\n' "${mount_type:-none}" >&2
    exit 1
    ;;
esac
[[ "$(stat -c %d "$backup_root")" != "$(stat -c %d "$offhost_root")" ]] || {
  printf 'ETORO_V2_OFFHOST_ERROR=destination_not_independent\n' >&2
  exit 1
}

install -d -m 0750 "$offhost_root/v2" "$offhost_root/v2-anchors" "$offhost_root/receipts"
install -d -m 0750 "$health_root"

partials=()
cleanup() {
  local partial
  for partial in "${partials[@]}"; do
    [[ ! -e "$partial" ]] || unlink "$partial"
  done
}
trap cleanup EXIT

copy_immutable() {
  local source=$1
  local destination=$2
  local partial
  [[ -f "$source" && ! -L "$source" ]] || {
    printf 'ETORO_V2_OFFHOST_ERROR=source_file_invalid file=%s\n' "$source" >&2
    exit 1
  }
  if [[ -e "$destination" ]]; then
    cmp --silent "$source" "$destination" || {
      printf 'ETORO_V2_OFFHOST_ERROR=immutable_conflict file=%s\n' "$destination" >&2
      exit 1
    }
    return
  fi
  partial="${destination}.partial.$$"
  partials+=("$partial")
  install -m 0440 "$source" "$partial"
  [[ "$(sha256sum "$source" | awk '{print $1}')" == "$(sha256sum "$partial" | awk '{print $1}')" ]]
  [[ ! -e "$destination" ]] || {
    printf 'ETORO_V2_OFFHOST_ERROR=destination_race file=%s\n' "$destination" >&2
    exit 1
  }
  mv -T "$partial" "$destination"
}

verify_sidecar() {
  local artifact=$1
  local sidecar="${artifact}.sha256"
  local expected
  [[ -s "$artifact" && -s "$sidecar" ]]
  expected=$(awk 'NR==1 {print $1}' "$sidecar")
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]]
  [[ "$expected" == "$(sha256sum "$artifact" | awk '{print $1}')" ]]
}

marker="$backup_root/LAST_BACKUP_OK"
[[ -s "$marker" ]]
read -r stamp postgres_name assets_name extra <"$marker"
[[ -z "${extra:-}" && "$stamp" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]
[[ "$postgres_name" == "v2_${stamp}.pgdump" ]]
[[ "$assets_name" == "v2_${stamp}.assets.tar.gz" ]]

for name in "$postgres_name" "$assets_name"; do
  verify_sidecar "$backup_root/$name"
  copy_immutable "$backup_root/$name" "$offhost_root/v2/$name"
  copy_immutable "$backup_root/$name.sha256" "$offhost_root/v2/$name.sha256"
done
sqlite_name="v2_${stamp}.sqlite3"
if [[ -e "$backup_root/$sqlite_name" || -e "$backup_root/$sqlite_name.sha256" ]]; then
  verify_sidecar "$backup_root/$sqlite_name"
  copy_immutable "$backup_root/$sqlite_name" "$offhost_root/v2/$sqlite_name"
  copy_immutable "$backup_root/$sqlite_name.sha256" "$offhost_root/v2/$sqlite_name.sha256"
fi

anchor_file=$(find "$anchor_root" -maxdepth 1 -type f -name '*-anchor-*.json' \
  -printf '%T@ %p\n' | sort -nr | head -1 | cut -d' ' -f2-)
[[ -n "$anchor_file" ]]
anchor_name=$(basename "$anchor_file")
copy_immutable "$anchor_file" "$offhost_root/v2-anchors/$anchor_name"

receipt_tmp="$(mktemp "$health_root/.receipt.XXXXXX")"
partials+=("$receipt_tmp")
postgres_hash=$(sha256sum "$backup_root/$postgres_name" | awk '{print $1}')
assets_hash=$(sha256sum "$backup_root/$assets_name" | awk '{print $1}')
anchor_hash=$(sha256sum "$anchor_file" | awk '{print $1}')
printf '%s %s %s %s %s %s %s\n' \
  "$stamp" "$postgres_name" "$postgres_hash" "$assets_name" "$assets_hash" \
  "$anchor_name" "$anchor_hash" >"$receipt_tmp"
receipt="$offhost_root/receipts/${stamp}.receipt"
copy_immutable "$receipt_tmp" "$receipt"

health_tmp="$health_root/.LAST_OFFHOST_OK.$$"
partials+=("$health_tmp")
install -m 0644 "$receipt_tmp" "$health_tmp"
mv -T "$health_tmp" "$health_root/LAST_OFFHOST_OK"
printf 'ETORO_V2_OFFHOST_OK stamp=%s postgres=%s assets=%s anchor=%s\n' \
  "$stamp" "$postgres_name" "$assets_name" "$anchor_name"
