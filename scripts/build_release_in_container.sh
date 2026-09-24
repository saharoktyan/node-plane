#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${1:-}"
if [[ ! "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-alpha\.[0-9]+)?$ ]]; then
  echo "Usage: scripts/build_release_in_container.sh vX.Y.Z[-alpha.N]" >&2
  exit 1
fi

if command -v docker >/dev/null 2>&1; then
  CONTAINER_RUNTIME=docker
elif command -v podman >/dev/null 2>&1; then
  CONTAINER_RUNTIME=podman
else
  echo "Docker or Podman is required to build release binaries against Debian 12." >&2
  exit 1
fi

IMAGE_NAME="node-plane-release-builder:bookworm"
"$CONTAINER_RUNTIME" build -f "${ROOT_DIR}/scripts/Dockerfile.release" -t "$IMAGE_NAME" "${ROOT_DIR}/scripts"
REPO_UID="$(stat -c %u "$ROOT_DIR")"
REPO_GID="$(stat -c %g "$ROOT_DIR")"
mkdir -p "${ROOT_DIR}/dist/.cargo-home"
if [[ "$(id -u)" == "0" ]]; then
  # sudo changes our UID to root; keep generated files owned by the checkout owner.
  chown -R "${REPO_UID}:${REPO_GID}" "${ROOT_DIR}/dist"
fi
"$CONTAINER_RUNTIME" run --rm \
  --user "${REPO_UID}:${REPO_GID}" \
  --mount "type=bind,source=${ROOT_DIR},target=/work" \
  --workdir /work \
  --env CARGO_HOME=/work/dist/.cargo-home \
  --env CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}" \
  --env GIT_CONFIG_COUNT=1 \
  --env GIT_CONFIG_KEY_0=safe.directory \
  --env GIT_CONFIG_VALUE_0=/work \
  "$IMAGE_NAME" \
  bash scripts/tag_release.sh "$TAG" --skip-tests --no-tag

echo "Portable artifacts are ready in dist/releases/${TAG}."
echo "Create the tag with: scripts/tag_release.sh ${TAG} --no-build"
echo "Push the tag, then publish with: scripts/tag_release.sh ${TAG} --no-tag --no-build --publish --no-draft"
