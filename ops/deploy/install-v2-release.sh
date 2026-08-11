#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

[[ ${EUID} -eq 0 ]] || {
  printf 'ETORO_V2_RELEASE_ERROR=root_required\n' >&2
  exit 1
}

repo=${1:-/opt/eToro}
candidate=${2:-}
release_root=${ETORO_V2_RELEASE_ROOT:-/opt/etoro-v2}

[[ -d "$repo/.git" && "$candidate" =~ ^[0-9a-f]{40}$ ]] || {
  printf 'ETORO_V2_RELEASE_ERROR=repo_or_sha_invalid\n' >&2
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
  stage=$(mktemp -d "$release_root/releases/.stage-${candidate:0:12}-XXXXXX")
  trap 'rm -rf "$stage"' EXIT
  git -C "$repo" archive "$candidate" | tar -x -C "$stage"
  [[ -s "$stage/requirements.lock" ]] || {
    printf 'ETORO_V2_RELEASE_ERROR=requirements_lock_missing\n' >&2
    exit 1
  }
  python3 -m venv "$stage/.venv"
  "$stage/.venv/bin/pip" install --disable-pip-version-check -r "$stage/requirements.lock"
  "$stage/.venv/bin/pip" install --disable-pip-version-check --no-build-isolation --no-deps "$stage"
  "$stage/.venv/bin/python" -m compileall -q "$stage/src"
  # Some setuptools versions leave an in-tree wheel build directory. It is
  # derived state, not part of the immutable source candidate.
  if [[ -d "$stage/build" ]]; then
    find "$stage/build" -depth -delete
  fi
  source_tree=$(git -C "$repo" rev-parse "$candidate^{tree}")
  lock_hash=$(sha256sum "$stage/requirements.lock" | awk '{print $1}')
  config_hash=$(sha256sum "$stage/config/v2-demo.json" | awk '{print $1}')
  execution_config_hash=$(sha256sum "$stage/config/v2-demo-execution.json" | awk '{print $1}')
  installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '{"commit":"%s","tree":"%s","requirements_sha256":"%s","config_sha256":"%s","execution_config_sha256":"%s","installed_at":"%s"}\n' \
    "$candidate" "$source_tree" "$lock_hash" "$config_hash" "$execution_config_hash" "$installed_at" \
    >"$stage/RELEASE.json"
  chown -R root:root "$stage"
  chmod -R go-w "$stage"
  mv "$stage" "$release"
  trap - EXIT
fi

[[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["commit"])' "$release/RELEASE.json")" == "$candidate" ]] || {
  printf 'ETORO_V2_RELEASE_ERROR=manifest_sha_mismatch\n' >&2
  exit 1
}
link="$release_root/.current-${candidate:0:12}-$$"
[[ ! -e "$release_root/current" || -L "$release_root/current" ]] || {
  printf 'ETORO_V2_RELEASE_ERROR=current_is_not_symlink\n' >&2
  exit 1
}
ln -s "releases/$candidate" "$link"
mv -Tf "$link" "$release_root/current"
printf 'ETORO_V2_RELEASE_OK sha=%s path=%s\n' "$candidate" "$release"
