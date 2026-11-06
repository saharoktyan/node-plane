#!/usr/bin/env bash
# Builds the web cabinet (web/cabinet) into OUT (default dist/cabinet) and
# packs it as node-plane-cabinet.tar.gz beside it (cabinet/ and LICENSE), for
# a cabinet served apart from the controller. The controller's package
# carries the same files.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT_DIR/dist/cabinet}"
command -v npm >/dev/null 2>&1 || { echo "npm is required to build the web cabinet." >&2; exit 1; }
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

cd "$ROOT_DIR/web/cabinet"
npm ci --no-audit --no-fund
npm run build
rm -rf "$OUT"
mkdir -p "$(dirname "$OUT")"
cp -R build "$OUT"
cp -R build "$STAGE/cabinet"
cp "$ROOT_DIR/LICENSE" "$STAGE/LICENSE"
tar -C "$STAGE" -czf "$(dirname "$OUT")/node-plane-cabinet.tar.gz" cabinet LICENSE
