#!/usr/bin/env bash
# Install the workstation locally, without Python, sudo or a server connection.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash install_workstation.sh [--channel dev|stable] [--tag vX.Y.Z[-alpha.N]]

Installs Workstation on Linux x86_64 or macOS (Intel/Apple Silicon) and configures PATH.
The default dev channel includes alpha releases. An exact tag overrides discovery.
Saved installation profiles and SSH keys are retained.
EOF
}

channel=dev
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
    if [[ -n "$candidate" && ( "$channel" == dev || "$candidate" != *-alpha.* ) ]]; then
      tag="$candidate"
      break
    fi
  done <<< "$tags"
  [[ -n "$tag" ]] || { echo 'No published release found for this channel. Try --channel dev or --tag.' >&2; exit 1; }
else
  echo "[1/4] Using release $tag"
fi

base="https://github.com/saharoktyan/node-plane/releases/download/$tag"
echo "[2/4] Downloading the release installer for $tag…"
if download "$base/node-plane-cli-installer.sh" "$tmp_dir/installer.sh"; then
  echo '[3/4] Starting the cargo-dist installer…'
  sh "$tmp_dir/installer.sh"
  echo '[4/4] Workstation installed. Open a new terminal, then run: node-plane'
  exit 0
fi

# Older alpha releases used the original Linux-only archive and install receipt.
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
  echo 'This release has no macOS installer. Choose a newer release.' >&2
  exit 1
}
for command in tar sha256sum; do
  command -v "$command" >/dev/null || { echo "Required command missing: $command" >&2; exit 1; }
done
asset='node-plane-cli-linux-amd64.tar.gz'
member='node-plane-cli-linux-amd64'
echo "[2/4] Downloading workstation $tag…"
download "$base/$asset" "$tmp_dir/$asset"
download "$base/SHA256SUMS.txt" "$tmp_dir/SHA256SUMS.txt"
echo '[3/4] Verifying the download…'
expected=
while read -r checksum filename extra; do
  if [[ "${filename#\*}" == "$asset" ]]; then
    [[ -z "$expected" && "$checksum" =~ ^[[:xdigit:]]{64}$ && -z "${extra:-}" ]] || {
      echo 'Invalid or duplicate workstation checksum.' >&2; exit 1;
    }
    expected="$checksum"
  fi
done < "$tmp_dir/SHA256SUMS.txt"
[[ -n "$expected" ]] || { echo 'Workstation checksum missing.' >&2; exit 1; }
printf '%s  %s\n' "$expected" "$tmp_dir/$asset" | sha256sum --check --status || {
  echo 'Workstation checksum mismatch; nothing was installed.' >&2; exit 1;
}
[[ "$(tar -tzf "$tmp_dir/$asset")" == "$member" ]] || {
  echo 'Unexpected workstation archive contents.' >&2; exit 1;
}
# Stream the only expected member; never extract archive paths onto the filesystem.
tar -xOzf "$tmp_dir/$asset" "$member" > "$tmp_dir/$member"
chmod 700 "$tmp_dir/$member"
[[ "$("$tmp_dir/$member" --version)" == "node-plane ${tag#v}" ]] || {
  echo 'Workstation binary version does not match the release.' >&2; exit 1;
}
echo '[4/4] Installing the workstation…'
"$tmp_dir/$member" self install
echo 'Open a new terminal, then run: node-plane'
