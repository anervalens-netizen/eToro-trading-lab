#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

PASSIVE_UNITS=(
  etoro-v2-sol-model.socket
  etoro-v2-sol-model@.service
  etoro-v2-sol-runner.service
)
EXECUTION_UNITS=(
  etoro-v2-signer.service
  etoro-v2-decision-apply-execution.service
  etoro-v2-exit-manager.service
  etoro-v2-executor-postgres.service
  etoro-v2-executor.service
  etoro-v2-executor-current.service
  etoro-demo-executor.service
)
V2_PASSIVE_SWITCH_ACTIVE=0

passive_release_digest() {
  local path=$1
  tar --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner \
    --format=posix -cf - -C "$path" . | sha256sum | awk '{print $1}'
}

rollback_passive_switch() {
  local release_root=$1
  local previous_target=$2
  local unit_backup=$3
  local active_receipt=$4
  local unit unit_path
  local recovery_failed=0
  local install_bin=${ETORO_V2_INSTALL_BIN:-install}
  local systemctl_bin=${ETORO_V2_SYSTEMCTL_BIN:-systemctl}

  "$systemctl_bin" stop 'etoro-v2-sol-model@*.service' \
    etoro-v2-sol-runner.service etoro-v2-sol-model.socket || recovery_failed=1
  if [[ -n "$previous_target" ]]; then
    ln -s "$previous_target" "$release_root/.rollback-$$" || recovery_failed=1
    if [[ $recovery_failed -eq 0 ]]; then
      mv -Tf "$release_root/.rollback-$$" "$release_root/current" || recovery_failed=1
    fi
  fi
  for unit in "${PASSIVE_UNITS[@]}"; do
    unit_path="${ETORO_V2_SYSTEMD_UNIT_DIR:-/etc/systemd/system}/$unit"
    if grep -Fxq "$unit" "$unit_backup/original-present"; then
      [[ -f "$unit_backup/$unit" ]] || {
        recovery_failed=1
        continue
      }
      "$install_bin" -o root -g root -m 0644 "$unit_backup/$unit" "$unit_path" \
        || recovery_failed=1
    else
      rm -f -- "$unit_path" || recovery_failed=1
    fi
  done
  "${ETORO_V2_SYSTEMCTL_BIN:-systemctl}" daemon-reload || recovery_failed=1
  while IFS= read -r unit; do
    [[ -z "$unit" ]] || "${ETORO_V2_SYSTEMCTL_BIN:-systemctl}" restart "$unit" \
      || recovery_failed=1
  done <"$active_receipt"
  [[ $recovery_failed -eq 0 ]] || {
    printf 'ETORO_V2_PASSIVE_SYNC_ERROR=rollback_failed\n' >&2
    return 1
  }
  printf 'ETORO_V2_PASSIVE_SYNC_ROLLBACK_OK\n' >&2
}

switch_passive_release() {
  local release_root=$1
  local candidate=$2
  local unit_backup=$3
  local active_receipt=$4
  local unit source target previous_target
  local systemctl_bin=${ETORO_V2_SYSTEMCTL_BIN:-systemctl}
  local install_bin=${ETORO_V2_INSTALL_BIN:-install}

  previous_target=$(readlink "$release_root/current")
  : >"$active_receipt"
  : >"$unit_backup/original-present"
  # Complete the non-mutating backup/validation phase before arming rollback.
  for unit in "${PASSIVE_UNITS[@]}"; do
    source="$release_root/releases/$candidate/ops/systemd/$unit"
    target="${ETORO_V2_SYSTEMD_UNIT_DIR:-/etc/systemd/system}/$unit"
    [[ -f "$source" && ! -L "$source" ]] || return 1
    if [[ -f "$target" && ! -L "$target" ]]; then
      "$install_bin" -o root -g root -m 0600 "$target" "$unit_backup/$unit" || return 1
      printf '%s\n' "$unit" >>"$unit_backup/original-present" || return 1
    elif [[ -e "$target" || -L "$target" ]]; then
      return 1
    fi
    if "$systemctl_bin" is-active --quiet "$unit"; then
      printf '%s\n' "$unit" >>"$active_receipt" || return 1
    fi
  done
  [[ "$(readlink "$release_root/current")" == "$previous_target" ]] || return 1
  V2_PASSIVE_SWITCH_ACTIVE=1
  if ! apply_passive_switch \
    "$release_root" "$candidate" "$unit_backup" "$active_receipt" "$previous_target"; then
    if rollback_passive_switch \
      "$release_root" "$previous_target" "$unit_backup" "$active_receipt"; then
      V2_PASSIVE_SWITCH_ACTIVE=0
    fi
    return 1
  fi
}

apply_passive_switch() {
  local release_root=$1
  local candidate=$2
  local unit_backup=$3
  local active_receipt=$4
  local previous_target=$5
  local unit source target link
  local systemctl_bin=${ETORO_V2_SYSTEMCTL_BIN:-systemctl}
  local install_bin=${ETORO_V2_INSTALL_BIN:-install}

  # Arguments retained explicitly so every rollback input is fixed before mutation.
  [[ -d "$unit_backup" && -f "$active_receipt" && -n "$previous_target" ]] || return 1
  for unit in "${PASSIVE_UNITS[@]}"; do
    source="$release_root/releases/$candidate/ops/systemd/$unit"
    target="${ETORO_V2_SYSTEMD_UNIT_DIR:-/etc/systemd/system}/$unit"
    "$install_bin" -o root -g root -m 0644 "$source" "$target" || return 1
  done
  "$systemctl_bin" daemon-reload || return 1
  link="$release_root/.current-${candidate:0:12}-$$"
  ln -s "releases/$candidate" "$link" || return 1
  if ! mv -Tf "$link" "$release_root/current"; then
    rm -f -- "$link" || return 2
    return 1
  fi
  "$systemctl_bin" stop 'etoro-v2-sol-model@*.service' || return 1
  "$systemctl_bin" restart etoro-v2-sol-model.socket || return 1
  "$systemctl_bin" restart etoro-v2-sol-runner.service || return 1
}

if [[ ${ETORO_V2_PASSIVE_SYNC_LIB_ONLY:-0} == 1 ]]; then
  if [[ ${BASH_SOURCE[0]} != "$0" ]]; then
    return 0
  fi
  exit 0
fi

[[ ${EUID} -eq 0 ]] || {
  printf 'ETORO_V2_PASSIVE_SYNC_ERROR=root_required\n' >&2
  exit 1
}
[[ "$(hostname)" == dell-standby ]] || {
  printf 'ETORO_V2_PASSIVE_SYNC_ERROR=wrong_host\n' >&2
  exit 1
}
candidate=${1:-}
[[ "$candidate" =~ ^[0-9a-f]{40}$ ]] || {
  printf 'ETORO_V2_PASSIVE_SYNC_ERROR=sha_invalid\n' >&2
  exit 1
}

release_root=${ETORO_V2_RELEASE_ROOT:-/opt/etoro-v2}
systemctl_bin=${ETORO_V2_SYSTEMCTL_BIN:-systemctl}
ssh_identity=/opt/Mobiup/.ssh/id_ed25519_mobiup_primary_admin
ssh_options=(-i "$ssh_identity" -o IdentitiesOnly=yes -o BatchMode=yes)
remote=andrei@server
remote_release="/opt/etoro-v2/releases/$candidate"
candidate_release="$release_root/releases/$candidate"

[[ -L "$release_root/current" && -r "$ssh_identity" ]] || {
  printf 'ETORO_V2_PASSIVE_SYNC_ERROR=local_authority_invalid\n' >&2
  exit 1
}
[[ ! -e /etc/etoro-v2-control/ENABLE_DEMO_EXECUTION ]] || {
  printf 'ETORO_V2_PASSIVE_SYNC_ERROR=execution_gate_present\n' >&2
  exit 1
}
for credential in \
  /etc/etoro-agent/etoro-demo-read-user-key \
  /etc/etoro-agent/etoro-demo-write-user-key \
  /etc/etoro-agent/etoro-api-key \
  /etc/etoro-agent/v2-risk-signing.key \
  /etc/etoro-agent/postgres-v2-engine-dsn; do
  [[ ! -e "$credential" ]] || {
    printf 'ETORO_V2_PASSIVE_SYNC_ERROR=local_broker_authority_present\n' >&2
    exit 1
  }
done
shopt -s nullglob
local_postgres_dsns=(/etc/etoro-agent/postgres-v2-*-dsn)
shopt -u nullglob
if [[ ${#local_postgres_dsns[@]} -ne 0 ]]; then
  printf 'ETORO_V2_PASSIVE_SYNC_ERROR=local_postgresql_dsn_present\n' >&2
  exit 1
fi
if "$systemctl_bin" is-active --quiet postgresql.service; then
  printf 'ETORO_V2_PASSIVE_SYNC_ERROR=local_postgresql_active\n' >&2
  exit 1
fi
for unit in "${EXECUTION_UNITS[@]}"; do
  if "$systemctl_bin" is-active --quiet "$unit"; then
    printf 'ETORO_V2_PASSIVE_SYNC_ERROR=execution_unit_active unit=%s\n' "$unit" >&2
    exit 1
  fi
done

# candidate is constrained to lowercase 40-hex before this fixed remote expansion.
# shellcheck disable=SC2029
remote_manifest=$(ssh "${ssh_options[@]}" "$remote" \
  "python3 -c \"import json;v=json.load(open('$remote_release/RELEASE.json'));print(v['commit'],v['tree'],v['release_bundle_sha256'])\"")
read -r remote_commit remote_tree remote_bundle <<<"$remote_manifest"
[[ "$remote_commit" == "$candidate" \
  && "$remote_tree" =~ ^[0-9a-f]{40}$ \
  && "$remote_bundle" =~ ^[0-9a-f]{64}$ ]] || {
  printf 'ETORO_V2_PASSIVE_SYNC_ERROR=primary_manifest_invalid\n' >&2
  exit 1
}
# candidate is constrained to lowercase 40-hex before this fixed remote expansion.
# shellcheck disable=SC2029
remote_digest=$(ssh "${ssh_options[@]}" "$remote" \
  "tar --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner --format=posix -cf - -C '$remote_release' . | sha256sum | awk '{print \\$1}'")
[[ "$remote_digest" =~ ^[0-9a-f]{64}$ ]] || {
  printf 'ETORO_V2_PASSIVE_SYNC_ERROR=primary_digest_invalid\n' >&2
  exit 1
}

if [[ ! -d "$candidate_release" ]]; then
  stage="$release_root/releases/.passive-${candidate:0:12}-$$"
  install -d -o root -g root -m 0755 "$stage"
  rsync -aH --delete -e "ssh -i $ssh_identity -o IdentitiesOnly=yes -o BatchMode=yes" \
    "$remote:$remote_release/" "$stage/"
  mv "$stage" "$candidate_release"
fi
local_digest=$(passive_release_digest "$candidate_release")
[[ "$local_digest" == "$remote_digest" ]] || {
  printf 'ETORO_V2_PASSIVE_SYNC_ERROR=release_digest_mismatch\n' >&2
  exit 1
}
local_manifest=$(python3 -c \
  "import json;v=json.load(open('$candidate_release/RELEASE.json'));print(v['commit'],v['tree'],v['release_bundle_sha256'])")
[[ "$local_manifest" == "$remote_manifest" ]] || {
  printf 'ETORO_V2_PASSIVE_SYNC_ERROR=release_manifest_mismatch\n' >&2
  exit 1
}

codex_auth=/home/andrei/.codex/auth.json
codex_binary=/usr/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex
[[ -s "$codex_auth" && -x "$codex_binary" ]] || {
  printf 'ETORO_V2_PASSIVE_SYNC_ERROR=codex_boundary_missing\n' >&2
  exit 1
}
account_hash=$(python3 - "$codex_auth" <<'PY'
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
binary_hash=$(sha256sum "$codex_binary" | awk '{print $1}')
install -d -o root -g root -m 0700 /etc/etoro-agent
for name_value in \
  "v2-codex-account.sha256:$account_hash" \
  "v2-codex-executable.sha256:$binary_hash"; do
  name=${name_value%%:*}
  value=${name_value#*:}
  target="/etc/etoro-agent/$name"
  if [[ -e "$target" ]]; then
    [[ "$(tr -d '\n' <"$target")" == "$value" ]] || {
      printf 'ETORO_V2_PASSIVE_SYNC_ERROR=codex_attestation_drift file=%s\n' "$name" >&2
      exit 1
    }
  else
    printf '%s\n' "$value" >"$target"
    chmod 0644 "$target"
  fi
done

unit_backup=$(mktemp -d)
active_receipt=$(mktemp)
previous_target=$(readlink "$release_root/current")
cleanup() {
  local rc=$?
  trap - EXIT
  if [[ $V2_PASSIVE_SWITCH_ACTIVE -eq 1 ]]; then
    rollback_passive_switch \
      "$release_root" "$previous_target" "$unit_backup" "$active_receipt" || rc=2
  fi
  rm -rf -- "$unit_backup"
  rm -f -- "$active_receipt"
  exit "$rc"
}
trap cleanup EXIT
switch_passive_release "$release_root" "$candidate" "$unit_backup" "$active_receipt"

status=$(systemd-run --wait --pipe --collect --quiet \
  --unit=etoro-v2-passive-probe \
  --property=User=andrei --property=Group=andrei \
  --property=NoNewPrivileges=yes --property=PrivateTmp=yes \
  --property=PrivateDevices=yes --property=ProtectSystem=strict --property=ProtectHome=yes \
  --property=InaccessiblePaths=-/etc/etoro-agent \
  --property=RestrictAddressFamilies='AF_UNIX AF_INET AF_INET6' \
  --property=BindReadOnlyPaths=/home/andrei/.ssh/known_hosts:/run/etoro-v2-sol-runner-known-hosts \
  "$candidate_release/.venv/bin/python" -c \
  'import json;from etoro_agent.sol_runner_v2 import remote_status;print(json.dumps(remote_status(),sort_keys=True,separators=(",",":")))') || {
  printf 'ETORO_V2_PASSIVE_SYNC_ERROR=remote_ai_probe_failed\n' >&2
  exit 1
}
python3 - "$status" "$candidate" "$remote_bundle" <<'PY'
import json
import sys
value = json.loads(sys.argv[1])
assert value["commit"] == sys.argv[2]
assert value["release_bundle_sha256"] == sys.argv[3]
assert value["schema_version"] == 9
assert value["session_user"] == "etoro-ai"
PY
[[ "$($systemctl_bin is-active etoro-v2-sol-model.socket)" == active ]]
[[ "$($systemctl_bin is-active etoro-v2-sol-runner.service)" == active ]]
V2_PASSIVE_SWITCH_ACTIVE=0
printf 'ETORO_V2_PASSIVE_SYNC_OK commit=%s tree=%s bundle=%s digest=%s role=etoro-ai\n' \
  "$candidate" "$remote_tree" "$remote_bundle" "$local_digest"
