#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

assert_v2_cutover_preconditions() {
  local python_bin=$1
  local execution_gate=${ETORO_V2_EXECUTION_GATE_FILE:-/etc/etoro-v2-control/ENABLE_DEMO_EXECUTION}
  local state_dsn_file=${ETORO_V2_RELEASE_STATE_DSN_FILE:-/etc/etoro-agent/postgres-v2-control-dsn}
  local systemctl_bin=${ETORO_V2_SYSTEMCTL_BIN:-systemctl}
  local trading_state unit unit_state unit_rc
  local -a writer_units=(
    etoro-v2-decision-apply-execution.service
    etoro-v2-executor-postgres.service
    etoro-v2-exit-manager.service
  )

  if [[ -e "$execution_gate" || -L "$execution_gate" ]]; then
    printf 'ETORO_V2_RELEASE_ERROR=execution_gate_present\n' >&2
    return 1
  fi
  if [[ ! -f "$state_dsn_file" || ! -s "$state_dsn_file" ]]; then
    printf 'ETORO_V2_RELEASE_ERROR=trading_state_unverifiable\n' >&2
    return 1
  fi
  if [[ ! -x "$python_bin" ]]; then
    printf 'ETORO_V2_RELEASE_ERROR=release_python_unavailable\n' >&2
    return 1
  fi
  if ! trading_state=$("$python_bin" - "$state_dsn_file" <<'PY'
from pathlib import Path
import sys

import psycopg

dsn = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
if not dsn:
    raise RuntimeError("PostgreSQL state credential is empty")
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
  ); then
    printf 'ETORO_V2_RELEASE_ERROR=trading_state_unverifiable\n' >&2
    return 1
  fi
  if [[ "$trading_state" != LOCKED ]]; then
    printf 'ETORO_V2_RELEASE_ERROR=trading_state_not_locked state=%s\n' \
      "$trading_state" >&2
    return 1
  fi
  if [[ ! -x "$systemctl_bin" ]] && ! command -v "$systemctl_bin" >/dev/null 2>&1; then
    printf 'ETORO_V2_RELEASE_ERROR=writer_state_unverifiable\n' >&2
    return 1
  fi
  for unit in "${writer_units[@]}"; do
    unit_state=
    unit_rc=0
    unit_state=$("$systemctl_bin" is-active "$unit" 2>/dev/null) || unit_rc=$?
    case "$unit_rc:$unit_state" in
      3:inactive|4:unknown) ;;
      0:active)
        printf 'ETORO_V2_RELEASE_ERROR=writer_not_inactive unit=%s state=active\n' \
          "$unit" >&2
        return 1
        ;;
      *)
        printf 'ETORO_V2_RELEASE_ERROR=writer_state_unverifiable unit=%s state=%s\n' \
          "$unit" "${unit_state:-unverifiable}" >&2
        return 1
        ;;
    esac
  done
}

prepare_v2_control_plane() {
  local release=$1
  local python_bin=$2
  local provision="$release/ops/deploy/provision-v2-host.sh"

  if [[ ! -x "$provision" ]]; then
    printf 'ETORO_V2_RELEASE_ERROR=bootstrap_provisioner_unavailable\n' >&2
    return 1
  fi
  # Candidate provisioning is deliberately limited to identities, DSNs,
  # migration and grants. It cannot install/start units or change current.
  "$provision" "$release" --bootstrap-control || return 1
  assert_v2_cutover_preconditions "$python_bin"
}

promote_v2_current_symlink() {
  local release_root=$1
  local candidate=$2
  local python_bin=$3
  local link="$release_root/.current-${candidate:0:12}-$$"
  local previous_target=

  if [[ -e "$release_root/current" && ! -L "$release_root/current" ]]; then
    printf 'ETORO_V2_RELEASE_ERROR=current_is_not_symlink\n' >&2
    return 1
  fi
  if [[ -L "$release_root/current" ]]; then
    previous_target=$(readlink "$release_root/current")
  fi
  assert_v2_cutover_preconditions "$python_bin" || return 1
  ln -s "releases/$candidate" "$link" || return 1
  # Re-evaluate immediately before the only operation that changes current.
  if ! assert_v2_cutover_preconditions "$python_bin"; then
    rm -f -- "$link"
    return 1
  fi
  if ! mv -Tf "$link" "$release_root/current"; then
    rm -f -- "$link"
    return 1
  fi
  # Detect a gate/state race across rename and restore the exact prior authority.
  if ! assert_v2_cutover_preconditions "$python_bin"; then
    rollback_v2_current_symlink "$release_root" "$previous_target"
    return 1
  fi
}

restart_v2_read_only_services() {
  local release_root=${1:-}
  local previous_target=${2:-}
  local systemctl_bin=${ETORO_V2_SYSTEMCTL_BIN:-systemctl}
  local ps_bin=${ETORO_V2_PS_BIN:-ps}
  local id_bin=${ETORO_V2_ID_BIN:-id}
  local unit expected_user active_state pid actual_user actual_group groups expected_group
  local active_rc restart_failed=0 recovery_failed=0 capture_failed=0
  local current_release expected_release='' previous_release=''
  declare -A old_pids=()
  declare -A candidate_pids=()
  local -a active_specs=()
  local -a read_only_units=(
    etoro-v2-market.service:etoro-collector:etoro-api-clients
    etoro-v2-coordinator.service:etoro-candidate:etoro-api-clients
    etoro-v2-decision-apply.service:etoro-decision:-
    etoro-v2-role-apply.service:etoro-ai:-
    etoro-v2-reconciliation.service:etoro-reconciler:etoro-api-clients
    etoro-v2-dashboard.service:etoro-observer:-
    etoro-v2-anchor.service:etoro-observer:-
  )

  if [[ -n "$release_root" ]]; then
    if ! expected_release=$(readlink -f "$release_root/current") \
      || [[ -z "$expected_release" ]]; then
      printf 'ETORO_V2_RELEASE_ERROR=current_release_unverifiable\n' >&2
      restart_failed=1
    fi
    if [[ -n "$previous_target" ]]; then
      previous_release=$(readlink -f "$release_root/$previous_target" 2>/dev/null || true)
    fi
  fi
  if ! "$systemctl_bin" daemon-reload; then
    printf 'ETORO_V2_RELEASE_ERROR=systemd_reload_failed\n' >&2
    restart_failed=1
    capture_failed=1
  fi
  for expected_group in "${read_only_units[@]}"; do
    IFS=: read -r unit expected_user groups <<<"$expected_group"
    active_state=
    active_rc=0
    active_state=$("$systemctl_bin" is-active "$unit" 2>/dev/null) || active_rc=$?
    if [[ "$active_state" == active ]]; then
      [[ $active_rc -eq 0 ]] || {
        printf 'ETORO_V2_RELEASE_ERROR=read_service_state_unverifiable unit=%s\n' \
          "$unit" >&2
        restart_failed=1
        capture_failed=1
        break
      }
      old_pids["$unit"]=$("$systemctl_bin" show --property MainPID --value "$unit")
      active_specs+=("$expected_group")
    elif [[ ! "$active_rc:$active_state" =~ ^(3:inactive|4:unknown)$ ]]; then
      printf 'ETORO_V2_RELEASE_ERROR=read_service_state_unverifiable unit=%s state=%s\n' \
        "$unit" "${active_state:-unverifiable}" >&2
      restart_failed=1
      capture_failed=1
      break
    fi
  done
  if [[ $restart_failed -eq 0 ]]; then
    for expected_group in "${active_specs[@]}"; do
      IFS=: read -r unit expected_user groups <<<"$expected_group"
      current_release=$(readlink -f "$release_root/current" 2>/dev/null || true)
      if [[ -n "$release_root" && "$current_release" != "$expected_release" ]]; then
        printf 'ETORO_V2_RELEASE_ERROR=current_changed_during_read_service_restart\n' >&2
        restart_failed=1
        break
      fi
      if ! "$systemctl_bin" restart "$unit"; then
        printf 'ETORO_V2_RELEASE_ERROR=read_service_restart_failed unit=%s\n' "$unit" >&2
        restart_failed=1
        break
      fi
      candidate_pids["$unit"]=$("$systemctl_bin" show --property MainPID --value "$unit")
    done
  fi
  if [[ $restart_failed -eq 0 ]]; then
    for expected_group in "${active_specs[@]}"; do
      IFS=: read -r unit expected_user groups <<<"$expected_group"
      current_release=$(readlink -f "$release_root/current" 2>/dev/null || true)
      if [[ -n "$release_root" && "$current_release" != "$expected_release" ]]; then
        printf 'ETORO_V2_RELEASE_ERROR=current_changed_during_read_service_validation\n' >&2
        restart_failed=1
        break
      fi
      active_state=$("$systemctl_bin" is-active "$unit" 2>/dev/null || true)
      if [[ "$active_state" != active ]]; then
        printf 'ETORO_V2_RELEASE_ERROR=read_service_not_active unit=%s state=%s\n' \
          "$unit" "${active_state:-unverifiable}" >&2
        restart_failed=1
        break
      fi
      pid=$("$systemctl_bin" show --property MainPID --value "$unit")
      if [[ ! "$pid" =~ ^[1-9][0-9]*$ ]]; then
        printf 'ETORO_V2_RELEASE_ERROR=read_service_pid_invalid unit=%s\n' "$unit" >&2
        restart_failed=1
        break
      fi
      if [[ "$pid" == "${old_pids[$unit]:-}" ]]; then
        printf 'ETORO_V2_RELEASE_ERROR=read_service_pid_not_replaced unit=%s pid=%s\n' \
          "$unit" "$pid" >&2
        restart_failed=1
        break
      fi
      actual_user=$("$ps_bin" -o user= -p "$pid" | xargs)
      actual_group=$("$ps_bin" -o group= -p "$pid" | xargs)
      if [[ "$actual_user" != "$expected_user" || "$actual_user" == etoro-engine ]]; then
        printf 'ETORO_V2_RELEASE_ERROR=read_service_identity_stale unit=%s user=%s\n' \
          "$unit" "${actual_user:-missing}" >&2
        restart_failed=1
        break
      fi
      if [[ "$actual_group" != "$expected_user" || "$actual_group" == etoro-engine ]]; then
        printf 'ETORO_V2_RELEASE_ERROR=read_service_primary_group_stale unit=%s group=%s\n' \
          "$unit" "${actual_group:-missing}" >&2
        restart_failed=1
        break
      fi
      if [[ "$groups" != - ]] && ! "$id_bin" -nG "$expected_user" | tr ' ' '\n' | grep -Fxq "$groups"; then
        printf 'ETORO_V2_RELEASE_ERROR=read_service_group_missing unit=%s group=%s\n' \
          "$unit" "$groups" >&2
        restart_failed=1
        break
      fi
    done
  fi
  [[ $restart_failed -ne 0 ]] || return 0

  # If no transaction context was supplied, preserve the library helper's
  # historical behavior for isolated validation tests.
  [[ -n "$release_root" ]] || return 1
  if ! rollback_v2_current_symlink "$release_root" "$previous_target"; then
    recovery_failed=1
  fi
  "$systemctl_bin" daemon-reload || recovery_failed=1
  if [[ $capture_failed -ne 0 || ( -z "$previous_release" && ${#active_specs[@]} -gt 0 ) ]]; then
    recovery_failed=1
  fi
  if [[ $recovery_failed -eq 0 ]]; then
    for expected_group in "${active_specs[@]}"; do
      IFS=: read -r unit expected_user groups <<<"$expected_group"
      current_release=$(readlink -f "$release_root/current" 2>/dev/null || true)
      if [[ "$current_release" != "$previous_release" ]]; then
        recovery_failed=1
        break
      fi
      if ! "$systemctl_bin" restart "$unit"; then
        recovery_failed=1
        break
      fi
    done
  fi
  if [[ $recovery_failed -eq 0 ]]; then
    for expected_group in "${active_specs[@]}"; do
      IFS=: read -r unit expected_user groups <<<"$expected_group"
      active_state=$("$systemctl_bin" is-active "$unit" 2>/dev/null || true)
      pid=$("$systemctl_bin" show --property MainPID --value "$unit" 2>/dev/null || true)
      actual_user=$("$ps_bin" -o user= -p "$pid" 2>/dev/null | xargs)
      actual_group=$("$ps_bin" -o group= -p "$pid" 2>/dev/null | xargs)
      current_release=$(readlink -f "$release_root/current" 2>/dev/null || true)
      if [[ "$active_state" != active || ! "$pid" =~ ^[1-9][0-9]*$ \
        || "$pid" == "${candidate_pids[$unit]:-}" \
        || "$actual_user" != "$expected_user" || "$actual_group" != "$expected_user" \
        || "$current_release" != "$previous_release" ]]; then
        recovery_failed=1
        break
      fi
      if [[ "$groups" != - ]] && ! "$id_bin" -nG "$expected_user" | tr ' ' '\n' | grep -Fxq "$groups"; then
        recovery_failed=1
        break
      fi
    done
  fi
  if [[ $recovery_failed -ne 0 ]]; then
    for expected_group in "${read_only_units[@]}"; do
      IFS=: read -r unit _ _ <<<"$expected_group"
      "$systemctl_bin" stop "$unit" >/dev/null 2>&1 || true
    done
    printf 'ETORO_V2_RELEASE_ERROR=read_service_recovery_failed_all_stopped\n' >&2
    return 1
  fi
  printf 'ETORO_V2_RELEASE_RECOVERY_OK=old_release_restored services=%s\n' \
    "${#active_specs[@]}" >&2
  return 1
}

rollback_v2_current_symlink() {
  local release_root=$1
  local previous_target=$2
  local rollback_link="$release_root/.current-rollback-$$"

  if [[ -n "$previous_target" ]]; then
    ln -s "$previous_target" "$rollback_link"
    mv -Tf "$rollback_link" "$release_root/current"
  else
    rm -f -- "$release_root/current"
  fi
}

if [[ ${ETORO_V2_RELEASE_LIB_ONLY:-0} == 1 ]]; then
  if [[ ${BASH_SOURCE[0]} != "$0" ]]; then
    return 0
  fi
  exit 0
fi

[[ ${EUID} -eq 0 ]] || {
  printf 'ETORO_V2_RELEASE_ERROR=root_required\n' >&2
  exit 1
}

repo=${1:-/opt/eToro}
candidate=${2:-}
bundle=${3:-${ETORO_V2_RELEASE_BUNDLE:-}}
release_root=${ETORO_V2_RELEASE_ROOT:-/opt/etoro-v2}

[[ -d "$repo/.git" && "$candidate" =~ ^[0-9a-f]{40}$ && -f "$bundle" ]] || {
  printf 'ETORO_V2_RELEASE_ERROR=repo_sha_or_bundle_invalid\n' >&2
  exit 1
}
resolved=$(git -C "$repo" rev-parse "$candidate^{commit}")
[[ "$resolved" == "$candidate" ]] || {
  printf 'ETORO_V2_RELEASE_ERROR=sha_not_exact\n' >&2
  exit 1
}
git -C "$repo" diff-tree --no-commit-id --name-only -r "$candidate" >/dev/null

install -d -o root -g root -m 0755 "$release_root/releases"
release="$release_root/releases/$candidate"
if [[ ! -d "$release" ]]; then
  evidence=$(mktemp -d)
  stage=$(mktemp -d "$release_root/releases/.stage-${candidate:0:12}-XXXXXX")
  trap 'rm -rf "$evidence" "$stage"' EXIT
  if tar --list --gzip --file="$bundle" \
    | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
    printf 'ETORO_V2_RELEASE_ERROR=bundle_path_invalid\n' >&2
    exit 1
  fi
  tar --extract --gzip --file="$bundle" --directory="$evidence" --no-same-owner
  [[ -s "$evidence/SHA256SUMS.txt" && -s "$evidence/WHEELHOUSE_SHA256SUMS.txt" \
    && -s "$evidence/RELEASE_CANDIDATE.json" ]] || {
    printf 'ETORO_V2_RELEASE_ERROR=bundle_evidence_missing\n' >&2
    exit 1
  }
  (
    cd "$evidence"
    sha256sum --check --strict SHA256SUMS.txt
    sha256sum --check --strict WHEELHOUSE_SHA256SUMS.txt
  )
  bundle_commit=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["commit"])' "$evidence/RELEASE_CANDIDATE.json")
  bundle_tree=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["tree"])' "$evidence/RELEASE_CANDIDATE.json")
  source_tree=$(git -C "$repo" rev-parse "$candidate^{tree}")
  [[ "$bundle_commit" == "$candidate" && "$bundle_tree" == "$source_tree" ]] || {
    printf 'ETORO_V2_RELEASE_ERROR=bundle_candidate_mismatch\n' >&2
    exit 1
  }
  git -C "$repo" archive "$candidate" | tar -x -C "$stage"
  [[ -s "$stage/requirements.lock" ]] || {
    printf 'ETORO_V2_RELEASE_ERROR=requirements_lock_missing\n' >&2
    exit 1
  }
  cmp --silent "$stage/requirements.lock" "$evidence/requirements.lock" || {
    printf 'ETORO_V2_RELEASE_ERROR=bundle_lock_mismatch\n' >&2
    exit 1
  }
  mapfile -t project_wheels < <(find "$evidence/dist" -maxdepth 1 -type f -name 'etoro_demo_agent-*.whl' -print)
  [[ ${#project_wheels[@]} -eq 1 ]] || {
    printf 'ETORO_V2_RELEASE_ERROR=project_wheel_count_invalid\n' >&2
    exit 1
  }
  cp -a "$evidence/wheelhouse" "$stage/wheelhouse"
  install -d -m 0755 "$stage/dist"
  cp "${project_wheels[0]}" "$stage/dist/"
  cp "$evidence/SHA256SUMS.txt" "$evidence/WHEELHOUSE_SHA256SUMS.txt" \
    "$evidence/RELEASE_CANDIDATE.json" "$evidence/sbom.cdx.json" "$stage/"
  python3 -m venv "$stage/.venv"
  "$stage/.venv/bin/pip" install --disable-pip-version-check --no-index \
    --find-links "$stage/wheelhouse" --require-hashes -r "$stage/requirements.lock"
  "$stage/.venv/bin/pip" install --disable-pip-version-check --no-index --no-deps \
    "$stage/dist/$(basename "${project_wheels[0]}")"
  "$stage/.venv/bin/python" -m compileall -q "$stage/src"
  (
    cd "$stage"
    PYTHONWARNINGS=error .venv/bin/python -m unittest discover -s tests
  )
  # Some setuptools versions leave an in-tree wheel build directory. It is
  # derived state, not part of the immutable source candidate.
  if [[ -d "$stage/build" ]]; then
    find "$stage/build" -depth -delete
  fi
  lock_hash=$(sha256sum "$stage/requirements.lock" | awk '{print $1}')
  config_hash=$(sha256sum "$stage/config/v2-demo.json" | awk '{print $1}')
  execution_config_hash=$(sha256sum "$stage/config/v2-demo-execution.json" | awk '{print $1}')
  wheelhouse_hash=$(sha256sum "$stage/WHEELHOUSE_SHA256SUMS.txt" | awk '{print $1}')
  wheel_hash=$(sha256sum "$stage/dist/$(basename "${project_wheels[0]}")" | awk '{print $1}')
  sbom_hash=$(sha256sum "$stage/sbom.cdx.json" | awk '{print $1}')
  bundle_hash=$(sha256sum "$bundle" | awk '{print $1}')
  installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '{"commit":"%s","tree":"%s","requirements_sha256":"%s","wheelhouse_manifest_sha256":"%s","wheel_sha256":"%s","sbom_sha256":"%s","release_bundle_sha256":"%s","provenance":"github-attested-main-bundle","config_sha256":"%s","execution_config_sha256":"%s","tests":"complete_unittest_suite","installed_at":"%s"}\n' \
    "$candidate" "$source_tree" "$lock_hash" "$wheelhouse_hash" "$wheel_hash" "$sbom_hash" "$bundle_hash" "$config_hash" "$execution_config_hash" "$installed_at" \
    >"$stage/RELEASE.json"
  chown -R root:root "$stage"
  # mktemp starts at 0700. Runtime identities need read/traverse access, never
  # write access, to the shared immutable code and virtual environment.
  chmod -R u=rwX,go=rX "$stage"
  mv "$stage" "$release"
  # The venv was assembled under a temporary path. Reinstall the project wheel
  # from the final immutable location so console-script shebangs cannot retain
  # the now-deleted .stage path.
  project_wheel="$release/dist/$(basename "${project_wheels[0]}")"
  "$release/.venv/bin/python" -m pip install --disable-pip-version-check \
    --no-index --no-deps --force-reinstall "$project_wheel" >/dev/null
  expected_shebang="#!$release/.venv/bin/python"
  [[ "$(head -n 1 "$release/.venv/bin/etoro-v2")" == "$expected_shebang" ]] || {
    printf 'ETORO_V2_RELEASE_ERROR=console_script_not_relocatable\n' >&2
    exit 1
  }
  "$release/.venv/bin/etoro-v2" --config "$release/config/v2-demo.json" \
    release-info >/dev/null
  chown -R root:root "$release"
  chmod -R u=rwX,go=rX "$release"
  rm -rf "$evidence"
  trap - EXIT
fi

[[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["commit"])' "$release/RELEASE.json")" == "$candidate" ]] || {
  printf 'ETORO_V2_RELEASE_ERROR=manifest_sha_mismatch\n' >&2
  exit 1
}
previous_current_target=
if [[ -L "$release_root/current" ]]; then
  previous_current_target=$(readlink "$release_root/current")
fi
prepare_v2_control_plane "$release" "$release/.venv/bin/python"
promote_v2_current_symlink "$release_root" "$candidate" "$release/.venv/bin/python"
if ! restart_v2_read_only_services "$release_root" "$previous_current_target"; then
  printf 'ETORO_V2_RELEASE_ERROR=read_service_restart_failed_current_rolled_back\n' >&2
  exit 1
fi

# V2 is the only installable runtime. Preserve any old local unit as forensic
# evidence, then mask every legacy name so a detached checkout cannot revive it.
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
for legacy_unit in "${legacy_units[@]}"; do
  legacy_path="/etc/systemd/system/$legacy_unit"
  if [[ -f "$legacy_path" && ! -L "$legacy_path" ]]; then
    legacy_hash=$(sha256sum "$legacy_path" | awk '{print $1}')
    install -o root -g root -m 0600 "$legacy_path" \
      "/var/lib/etoro-v2/retired-units/${legacy_unit}.${legacy_hash}"
  fi
  if [[ -e "$legacy_path" || -L "$legacy_path" ]]; then
    rm -f "$legacy_path"
  fi
done
systemctl daemon-reload
systemctl mask --now "${legacy_units[@]}" >/dev/null
for legacy_unit in "${legacy_units[@]}"; do
  if systemctl is-active --quiet "$legacy_unit"; then
    printf 'ETORO_V2_RELEASE_ERROR=legacy_runtime_active unit=%s\n' \
      "$legacy_unit" >&2
    exit 1
  fi
  if [[ "$(systemctl is-enabled "$legacy_unit" 2>/dev/null || true)" != masked ]]; then
    printf 'ETORO_V2_RELEASE_ERROR=legacy_runtime_not_masked unit=%s\n' \
      "$legacy_unit" >&2
    exit 1
  fi
done
printf 'ETORO_V2_RELEASE_OK sha=%s path=%s\n' "$candidate" "$release"
