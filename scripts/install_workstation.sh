#!/usr/bin/env bash
# Install the workstation locally, without Python, sudo or a server connection.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash install_workstation.sh [--channel dev|stable] [--tag vX.Y.Z[-alpha.N]]

Installs Workstation on Linux x86_64 or macOS (Intel/Apple Silicon) and configures PATH.
The default stable channel selects stable releases. Use --channel dev for alpha builds.
An explicit --tag skips discovery and requires that tag's published binaries.
Stable and dev installations are separate. Run node-plane or node-plane-dev.
Saved installation profiles and SSH keys are retained.
Source-only tags/current dev: https://github.com/saharoktyan/node-plane/blob/dev/docs/workstation.md#build-from-source
EOF
}

channel=stable
tag=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel) [[ $# -ge 2 ]] || { usage >&2; exit 1; }; channel="$2"; shift 2 ;;
    --tag) [[ $# -ge 2 ]] || { usage >&2; exit 1; }; tag="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done
[[ "$channel" == dev || "$channel" == stable ]] || { echo 'Channel must be dev or stable.' >&2; exit 1; }
if [[ -n "$tag" && ! "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-alpha\.[0-9]+)?$ ]]; then
  echo 'Invalid release tag.' >&2
  exit 1
fi
[[ ( "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ) || "$(uname -s)" == Darwin ]] || {
  echo 'Supported systems: Linux x86_64 and macOS Intel/Apple Silicon.' >&2
  exit 1
}
for command in curl mktemp grep sed uname; do
  command -v "$command" >/dev/null || { echo "Required command missing: $command" >&2; exit 1; }
done

tmp_dir="$(mktemp -d)"
trap 'rm -rf -- "$tmp_dir"' EXIT
download() {
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 10 --max-time 180 --retry 2 --max-filesize 104857600 \
    "$1" -o "$2"
}
if [[ -z "$tag" ]]; then
  echo '[1/4] Finding the latest workstation release…'
  api='https://api.github.com/repos/saharoktyan/node-plane/releases?per_page=100'
  [[ "$channel" == stable ]] && api='https://api.github.com/repos/saharoktyan/node-plane/releases/latest'
  download "$api" "$tmp_dir/releases.json"
  # Public release discovery needs no interpreter or jq. Tags are strictly bounded.
  tags="$(grep -oE '"tag_name"[[:space:]]*:[[:space:]]*"v[0-9]+\.[0-9]+\.[0-9]+(-alpha\.[0-9]+)?"' "$tmp_dir/releases.json" \
    | sed -E 's/^.*:[[:space:]]*"([^"]+)"$/\1/' || true)"
  while IFS= read -r candidate; do
    if [[ -n "$candidate" && (( "$channel" == dev && "$candidate" == *-alpha.* ) || ( "$channel" == stable && "$candidate" != *-alpha.* )) ]]; then
      tag="$candidate"
      break
    fi
  done <<< "$tags"
  [[ -n "$tag" ]] || {
    echo 'No published Workstation release found for this channel. Use --channel dev for published alphas, or --tag for an exact published version.' >&2
    echo 'For source-only tags or the current dev branch: https://github.com/saharoktyan/node-plane/blob/dev/docs/workstation.md#build-from-source' >&2
    exit 1
  }
else
  echo "[1/4] Using release $tag"
fi

if [[ ( "$channel" == dev && "$tag" != *-alpha.* ) || ( "$channel" == stable && "$tag" == *-alpha.* ) ]]; then
  echo 'The release tag must match the requested Workstation channel.' >&2
  exit 1
fi

base="https://github.com/saharoktyan/node-plane/releases/download/$tag"
echo "[2/4] Downloading the release installer for $tag…"
if download "$base/node-plane-cli-installer.sh" "$tmp_dir/installer.sh"; then
  echo '[3/4] Starting the cargo-dist installer…'
  install_dir="${NODE_PLANE_CLI_INSTALL_DIR:-${CARGO_DIST_FORCE_INSTALL_DIR:-${CARGO_DIST_INSTALL_DIR:-$HOME/.local/share/node-plane-workstation/$channel}}}"
  receipt_base="${XDG_CONFIG_HOME:-$HOME/.config}/node-plane-workstation/$channel"
  XDG_CONFIG_HOME="$receipt_base" CARGO_DIST_FORCE_INSTALL_DIR="$install_dir" NODE_PLANE_CLI_INSTALL_DIR="$install_dir" sh "$tmp_dir/installer.sh"
  [[ -x "$install_dir/node-plane" ]] || { echo 'Installed Workstation executable is missing.' >&2; exit 1; }
  install_dir="$(cd "$install_dir" && pwd -P)"
  mkdir -p "$HOME/.local/bin"
  launcher=node-plane
  if [[ "$channel" == dev ]]; then
    launcher=node-plane-dev
    ln -sfn "$install_dir/node-plane" "$install_dir/$launcher"
  fi
  ln -sfn "$install_dir/node-plane" "$HOME/.local/bin/$launcher"
  echo "[4/4] Workstation installed. Open a new terminal, then run: $launcher"
  exit 0
fi

echo "No published Workstation installer is available for $tag." >&2
echo 'For source-only tags or the current dev branch: https://github.com/saharoktyan/node-plane/blob/dev/docs/workstation.md#build-from-source' >&2
exit 1
