#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="$ROOT_DIR/VERSION"

usage() {
  echo "Usage: $0 vX.Y.Z[-alpha.N]" >&2
}

if [[ $# -ne 1 ]]; then
  usage
  exit 1
fi

TAG="$1"
if [[ ! "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-alpha\.[0-9]+)?$ ]]; then
  echo "Invalid tag format: $TAG" >&2
  usage
  exit 1
fi

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

cd "$ROOT_DIR"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Working tree is not clean. Commit or stash changes before tagging." >&2
  exit 1
fi

if [[ -n "$(git status --short --untracked-files=no)" ]]; then
  echo "Tracked working tree state is not clean." >&2
  exit 1
fi

if git rev-parse --verify --quiet "$TAG" >/dev/null; then
  echo "Tag already exists: $TAG" >&2
  exit 1
fi

git tag "$TAG"
echo "Created tag $TAG on $(git rev-parse --short HEAD)"
