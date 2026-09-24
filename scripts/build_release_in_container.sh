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
mkdir -p "${ROOT_DIR}/dist/.cargo-home"
"$CONTAINER_RUNTIME" run --rm \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=${ROOT_DIR},target=/work" \
  --workdir /work \
  --env CARGO_HOME=/work/dist/.cargo-home \
  --env CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}" \
  "$IMAGE_NAME" \
  bash scripts/tag_release.sh "$TAG" --skip-tests --no-tag

echo "Portable artifacts are ready in dist/releases/${TAG}."
echo "Create the tag with: scripts/tag_release.sh ${TAG} --no-build"
echo "Push the tag, then publish with: scripts/tag_release.sh ${TAG} --no-tag --no-build --publish --no-draft"
