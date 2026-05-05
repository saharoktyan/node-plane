#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="$ROOT_DIR/VERSION"

RUN_TESTS="${RUN_TESTS:-1}"
CREATE_TAG=1
BUILD_ARTIFACTS=1
PUBLISH_RELEASE=0
DRAFT_RELEASE=1
ARTIFACTS_DIR="${ARTIFACTS_DIR:-$ROOT_DIR/dist/releases}"
CURRENT_STEP="startup"

usage() {
  cat <<'EOF' >&2
Usage:
  scripts/tag_release.sh vX.Y.Z[-alpha.N] [options]

Options:
  --skip-tests         Do not run preflight checks (python tests + cargo check)
  --no-build           Do not build/package rust artifacts
  --no-tag             Do not create a git tag (prep-only mode)
  --publish            Publish GitHub release with artifacts via gh CLI
  --no-draft           When used with --publish, create non-draft release
  --artifacts-dir DIR  Release artifacts output directory
  -h, --help           Show this help

Env:
  RUN_TESTS=0|1
  ARTIFACTS_DIR=<path>
EOF
}

set_step() {
  CURRENT_STEP="$1"
}

on_error() {
  local exit_code="$1"
  echo >&2
  echo "tag_release failed during step: ${CURRENT_STEP}" >&2
  echo "Failing command: ${BASH_COMMAND}" >&2
  echo "Exit code: ${exit_code}" >&2
}

trap 'on_error $?' ERR

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

for arg in "$@"; do
  if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
    usage
    exit 0
  fi
done

if [[ $# -lt 1 ]]; then
  usage
  exit 1
fi

TAG="$1"
shift

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-tests)
      RUN_TESTS=0
      shift
      ;;
    --no-build)
      BUILD_ARTIFACTS=0
      shift
      ;;
    --no-tag)
      CREATE_TAG=0
      shift
      ;;
    --publish)
      PUBLISH_RELEASE=1
      shift
      ;;
    --no-draft)
      DRAFT_RELEASE=0
      shift
      ;;
    --artifacts-dir)
      ARTIFACTS_DIR="${2:-}"
      shift 2
      ;;
    --artifacts-dir=*)
      ARTIFACTS_DIR="${1#*=}"
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ ! "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-alpha\.[0-9]+)?$ ]]; then
  echo "Invalid tag format: $TAG" >&2
  usage
  exit 1
fi

cd "$ROOT_DIR"
need_cmd git
need_cmd sha256sum

if [[ ! -f "$VERSION_FILE" ]]; then
  echo "VERSION file not found: $VERSION_FILE" >&2
  exit 1
fi

VERSION="$(tr -d '\r\n' < "$VERSION_FILE")"
EXPECTED_VERSION="${TAG#v}"
if [[ "$VERSION" != "$EXPECTED_VERSION" ]]; then
  echo "VERSION mismatch: VERSION=$VERSION, tag expects $EXPECTED_VERSION" >&2
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Working tree is not clean. Commit or stash changes before tagging." >&2
  exit 1
fi
if [[ -n "$(git status --short --untracked-files=no)" ]]; then
  echo "Tracked working tree state is not clean." >&2
  exit 1
fi

if [[ $CREATE_TAG -eq 1 ]]; then
  if git rev-parse --verify --quiet "$TAG" >/dev/null; then
    echo "Tag already exists: $TAG" >&2
    exit 1
  fi
fi

if [[ $PUBLISH_RELEASE -eq 1 ]]; then
  need_cmd gh
  if ! gh auth status >/dev/null 2>&1; then
    echo "GitHub CLI is installed but not authenticated." >&2
    echo "Run: gh auth login" >&2
    exit 1
  fi
fi

run_preflight_checks() {
  set_step "python tests"
  python3 -m unittest discover -s tests
  set_step "cargo check node-driver"
  (cd rust/node-driver && cargo check)
  set_step "cargo check node-agent"
  (cd rust/node-agent && cargo check)
}

build_release_artifacts() {
  need_cmd cargo
  need_cmd tar
  local out_dir="$1"
  local tag="$2"
  local short_sha
  short_sha="$(git rev-parse --short HEAD)"

  local release_dir="${out_dir}/${tag}"
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  mkdir -p "$release_dir"

  set_step "cargo build --release node-driver"
  (cd rust/node-driver && cargo build --release)
  set_step "cargo build --release node-agent"
  (cd rust/node-agent && cargo build --release)

  local driver_bin="rust/node-driver/target/release/node-plane-driver"
  local agent_bin="rust/node-agent/target/release/node-plane-agent"
  [[ -x "$driver_bin" ]] || { echo "Missing built driver binary: $driver_bin" >&2; exit 1; }
  [[ -x "$agent_bin" ]] || { echo "Missing built agent binary: $agent_bin" >&2; exit 1; }

  local driver_name="node-plane-driver-linux-amd64"
  local agent_name="node-plane-agent-linux-amd64"
  local checksums_file="SHA256SUMS.txt"

  cp "$driver_bin" "${tmp_dir}/${driver_name}"
  cp "$agent_bin" "${tmp_dir}/${agent_name}"
  chmod +x "${tmp_dir}/${driver_name}" "${tmp_dir}/${agent_name}"

  set_step "package driver artifact"
  tar -C "$tmp_dir" -czf "${release_dir}/${driver_name}.tar.gz" "$driver_name"
  set_step "package agent artifact"
  tar -C "$tmp_dir" -czf "${release_dir}/${agent_name}.tar.gz" "$agent_name"

  (
    cd "$release_dir"
    sha256sum "${driver_name}.tar.gz" "${agent_name}.tar.gz" > "$checksums_file"
  )

  cat > "${release_dir}/RELEASE_METADATA.txt" <<EOF
tag=${tag}
version=${VERSION}
commit=${short_sha}
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

  rm -rf "$tmp_dir"
  echo "Artifacts prepared in: ${release_dir}"
  ls -1 "${release_dir}"
}

publish_github_release() {
  local out_dir="$1"
  local tag="$2"
  local release_dir="${out_dir}/${tag}"
  if [[ ! -d "$release_dir" ]]; then
    echo "Artifacts dir not found for publish: $release_dir" >&2
    exit 1
  fi

  local -a flags
  if [[ $DRAFT_RELEASE -eq 1 ]]; then
    flags+=(--draft)
  fi
  if [[ "$tag" == *"-alpha."* ]]; then
    flags+=(--prerelease)
  fi

  set_step "publish github release"
  gh release create "$tag" \
    "${release_dir}"/*.tar.gz \
    "${release_dir}/SHA256SUMS.txt" \
    "${release_dir}/RELEASE_METADATA.txt" \
    "${flags[@]}" \
    --title "$tag" \
    --notes "Automated release artifacts for ${tag}"
}

if [[ "$RUN_TESTS" == "1" ]]; then
  run_preflight_checks
else
  echo "Skipping preflight tests/checks."
fi

if [[ "$BUILD_ARTIFACTS" == "1" ]]; then
  build_release_artifacts "$ARTIFACTS_DIR" "$TAG"
else
  echo "Skipping artifact build."
fi

if [[ $CREATE_TAG -eq 1 ]]; then
  set_step "create git tag"
  git tag "$TAG"
  echo "Created tag $TAG on $(git rev-parse --short HEAD)"
else
  echo "Tag creation skipped (--no-tag)."
fi

if [[ $PUBLISH_RELEASE -eq 1 ]]; then
  publish_github_release "$ARTIFACTS_DIR" "$TAG"
  echo "Published GitHub release for ${TAG}."
else
  echo "GitHub publish skipped. Use --publish when ready."
fi

echo
echo "Release prep complete."
echo "Next steps:"
echo "  git push origin $(git rev-parse --abbrev-ref HEAD)"
if [[ $CREATE_TAG -eq 1 ]]; then
  echo "  git push origin ${TAG}"
fi
