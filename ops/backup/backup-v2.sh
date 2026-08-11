#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

runtime_db=${ETORO_V2_SQLITE_DB:-/var/lib/etoro-agent/v2.sqlite3}
backup_root=${ETORO_V2_BACKUP_ROOT:-/storage/backups/db/etoro/v2}
postgres_service=${ETORO_V2_POSTGRES_SERVICE:-}
release=${ETORO_V2_RELEASE_PATH:-/opt/etoro-v2/current}
catalog=${ETORO_V2_DATA_CATALOG:-/var/lib/etoro-collector/data-v2}
market_index=${ETORO_V2_MARKET_INDEX:-/var/lib/etoro-collector/market-archive-v2.sqlite3}
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_root"

sqlite_target="$backup_root/v2_${stamp}.sqlite3"
if [[ -f "$runtime_db" ]]; then
  sqlite3 "$runtime_db" ".backup '$sqlite_target.partial'"
  [[ "$(sqlite3 "$sqlite_target.partial" 'PRAGMA integrity_check;')" == ok ]]
  mv "$sqlite_target.partial" "$sqlite_target"
  sha256sum "$sqlite_target" >"$sqlite_target.sha256"
fi

[[ -n "$postgres_service" && -n "${PGSERVICEFILE:-}" && -s "$PGSERVICEFILE" ]] || {
  printf 'ETORO_V2_BACKUP_ERROR=postgres_service_unavailable\n' >&2
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
pg_dump --dbname="service=$postgres_service" \
  --format=custom --compress=6 --file="$pg_target.partial"
pg_restore --list "$pg_target.partial" >/dev/null
mv "$pg_target.partial" "$pg_target"
sha256sum "$pg_target" >"$pg_target.sha256"
chmod 0640 "$pg_target" "$pg_target.sha256"

for release_evidence in \
  RELEASE.json RELEASE_CANDIDATE.json SHA256SUMS.txt WHEELHOUSE_SHA256SUMS.txt \
  sbom.cdx.json requirements.lock requirements-dev.lock \
  config/v2-demo.json config/v2-demo-execution.json; do
  [[ -s "$release/$release_evidence" ]] || {
    printf 'ETORO_V2_BACKUP_ERROR=release_evidence_unavailable:%s\n' "$release_evidence" >&2
    exit 1
  }
done
mapfile -t release_wheels < <(find "$release/dist" -maxdepth 1 -type f -name 'etoro_demo_agent-*.whl' -print)
[[ ${#release_wheels[@]} -eq 1 ]] || {
  printf 'ETORO_V2_BACKUP_ERROR=release_wheel_count_invalid\n' >&2
  exit 1
}
mapfile -t dependency_wheels < <(find "$release/wheelhouse" -maxdepth 1 -type f -name '*.whl' -print)
[[ ${#dependency_wheels[@]} -gt 0 ]] || {
  printf 'ETORO_V2_BACKUP_ERROR=offline_wheelhouse_missing\n' >&2
  exit 1
}
metadata_target="$backup_root/v2_${stamp}.assets.tar.gz"
assets=(
  "opt/etoro-v2/current/RELEASE.json"
  "opt/etoro-v2/current/RELEASE_CANDIDATE.json"
  "opt/etoro-v2/current/SHA256SUMS.txt"
  "opt/etoro-v2/current/WHEELHOUSE_SHA256SUMS.txt"
  "opt/etoro-v2/current/sbom.cdx.json"
  "opt/etoro-v2/current/requirements.lock"
  "opt/etoro-v2/current/requirements-dev.lock"
  "opt/etoro-v2/current/wheelhouse"
  "opt/etoro-v2/current/dist/$(basename "${release_wheels[0]}")"
  "opt/etoro-v2/current/config/v2-demo.json"
  "opt/etoro-v2/current/config/v2-demo-execution.json"
)
for public_key in \
  /etc/etoro-agent/v2-risk-verifying.pub \
  /etc/etoro-agent/v2-anchor-verifying.pub; do
  [[ -r "$public_key" ]] && assets+=("${public_key#/}")
done
[[ -r "$market_index" ]] && assets+=("${market_index#/}")
[[ -d "$catalog" && -r "$catalog" ]] && assets+=("${catalog#/}")
tar --create --gzip --file="$metadata_target.partial" --dereference \
  --one-file-system --directory=/ "${assets[@]}"
tar --list --file="$metadata_target.partial" >/dev/null
mv "$metadata_target.partial" "$metadata_target"
sha256sum "$metadata_target" >"$metadata_target.sha256"
chmod 0640 "$metadata_target" "$metadata_target.sha256"

marker="$backup_root/LAST_BACKUP_OK"
printf '%s %s %s\n' "$stamp" "$(basename "$pg_target")" "$(basename "$metadata_target")" \
  >"$marker.partial"
mv "$marker.partial" "$marker"
chmod 0640 "$marker"

find "$backup_root" -type f -mtime +45 -delete
printf 'ETORO_V2_BACKUP_OK stamp=%s sqlite=%s postgres=%s assets=%s\n' \
  "$stamp" "${sqlite_target:-none}" "$pg_target" "$metadata_target"
