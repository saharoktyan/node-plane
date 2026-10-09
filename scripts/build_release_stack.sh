#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$ROOT_DIR/dist/release-stack"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

cd "$ROOT_DIR"
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
  echo "Server release artifacts require Linux x86_64." >&2
  exit 1
}

if ! command -v protoc >/dev/null 2>&1; then
  if [[ "${CI:-}" == true ]] && command -v apt-get >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y protobuf-compiler
  else
    echo "protoc is required to build the Node Plane driver and agent." >&2
    exit 1
  fi
fi

TAG="${GITHUB_REF_NAME:-}"
if [[ -z "$TAG" ]]; then
  VERSION="$(tr -d '\r\n' < VERSION)"
  TAG="v$VERSION"
fi
COMMIT="${GITHUB_SHA:-$(git rev-parse HEAD)}"
SHORT_COMMIT="${COMMIT:0:12}"
[[ "$TAG" == "v$(tr -d '\r\n' < VERSION)" ]] || {
  echo "Release tag must match VERSION." >&2
  exit 1
}

mkdir -p "$OUT_DIR"
cargo build --release --manifest-path rust/node-driver/Cargo.toml
cargo build --release --manifest-path rust/node-agent/Cargo.toml

DRIVER_NAME="node-plane-driver-linux-amd64"
AGENT_NAME="node-plane-agent-linux-amd64"
CONTROLLER_NAME="node-plane-controller.tar.gz"
WORKSTATION_NAME="node-plane-cli-linux-amd64"
WORKSTATION_ARCHIVE="$ROOT_DIR/target/distrib/node-plane-cli-x86_64-unknown-linux-gnu.tar.gz"
[[ -f "$WORKSTATION_ARCHIVE" ]] || {
  echo "Build/download cargo-dist local artifacts before packaging the stack." >&2
  exit 1
}

cp rust/node-driver/target/release/node-plane-driver "$TMP_DIR/$DRIVER_NAME"
cp rust/node-agent/target/release/node-plane-agent "$TMP_DIR/$AGENT_NAME"
chmod 0755 "$TMP_DIR/$DRIVER_NAME" "$TMP_DIR/$AGENT_NAME"

# Keep the old Linux asset through the transition so installed alpha copies
# can discover this release using their existing updater.
tar -xOzf "$WORKSTATION_ARCHIVE" node-plane-cli-x86_64-unknown-linux-gnu/node-plane \
  > "$TMP_DIR/$WORKSTATION_NAME"
chmod 0755 "$TMP_DIR/$WORKSTATION_NAME"

for binary in "$TMP_DIR/$DRIVER_NAME" "$TMP_DIR/$AGENT_NAME" "$TMP_DIR/$WORKSTATION_NAME"; do
  readelf -h "$binary" >/dev/null
  required="$(readelf --version-info "$binary" | grep -oE 'GLIBC_[0-9]+\.[0-9]+' | sed 's/^GLIBC_//' | sort -Vu | tail -n 1 || true)"
  if [[ -n "$required" && "$(printf '%s\n%s\n' 2.36 "$required" | sort -V | tail -n 1)" != 2.36 ]]; then
    echo "$binary requires glibc $required; Debian 12 compatibility requires 2.36 or older." >&2
    exit 1
  fi
done

tar -C "$TMP_DIR" -czf "$OUT_DIR/$DRIVER_NAME.tar.gz" "$DRIVER_NAME"
tar -C "$TMP_DIR" -czf "$OUT_DIR/$AGENT_NAME.tar.gz" "$AGENT_NAME"
tar -C "$TMP_DIR" -czf "$OUT_DIR/$WORKSTATION_NAME.tar.gz" "$WORKSTATION_NAME"
python3 scripts/controller_release.py build --root "$ROOT_DIR" \
  --archive "$OUT_DIR/$CONTROLLER_NAME" --ref "$TAG" --commit "$COMMIT"

(
  cd "$OUT_DIR"
  sha256sum "$CONTROLLER_NAME" "$DRIVER_NAME.tar.gz" "$AGENT_NAME.tar.gz" "$WORKSTATION_NAME.tar.gz" > SHA256SUMS.txt
)

cat > "$OUT_DIR/RELEASE_METADATA.txt" <<EOF
tag=$TAG
version=${TAG#v}
commit=$SHORT_COMMIT
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

echo "Prepared Node Plane controller, driver and agent release artifacts in $OUT_DIR"
