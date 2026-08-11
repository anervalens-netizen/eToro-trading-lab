#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

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
  rm -rf "$evidence"
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
