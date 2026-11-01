#!/usr/bin/env bash
# Install the workstation locally, without Python, sudo or a server connection.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash install_workstation.sh [--channel dev|beta|stable] [--tag vX.Y.Z[-alpha.N|-beta.N|-rc.N]]

Installs Workstation on Linux x86_64 or macOS (Intel/Apple Silicon) and configures PATH.
The default stable channel selects stable releases. Use --channel beta for public betas/RCs, or --channel dev for alpha builds.
An explicit --tag skips discovery and requires that tag's published binaries.
All channels install separately. Run node-plane, node-plane-beta or node-plane-dev.
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
[[ "$channel" == dev || "$channel" == beta || "$channel" == stable ]] || { echo 'Channel must be dev, beta or stable.' >&2; exit 1; }
if [[ -n "$tag" && ! "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-(alpha|beta|rc)\.[0-9]+)?$ ]]; then
  echo 'Invalid release tag.' >&2
  exit 1
fi
[[ ( "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ) || "$(uname -s)" == Darwin ]] || {
  echo 'Supported systems: Linux x86_64 and macOS Intel/Apple Silicon.' >&2
  exit 1
}
for command in curl mktemp grep sed uname awk sort; do
  command -v "$command" >/dev/null || { echo "Required command missing: $command" >&2; exit 1; }
done

tmp_dir="$(mktemp -d)"
trap 'rm -rf -- "$tmp_dir"' EXIT
download() {
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 10 --max-time 180 --retry 2 --max-filesize 104857600 \
    "$1" -o "$2"
}
configure_launcher_path() {
  # Keep cargo-dist's private binary prefixes off PATH: every prefix contains
  # a binary named node-plane, including beta/dev installations.
  [[ "${NODE_PLANE_CLI_NO_MODIFY_PATH:-${INSTALLER_NO_MODIFY_PATH:-0}}" != 1 ]] || return 0
  local shared="$HOME/.local/share/node-plane-workstation" rc shell_name="${SHELL:-}"
  mkdir -p "$shared"
  cat > "$shared/env" <<'EOF'
case ":${PATH:-}:" in
  *:"$HOME/.local/bin":*) ;;
  *) export PATH="$HOME/.local/bin${PATH:+:$PATH}" ;;
esac
EOF
  local entry='. "$HOME/.local/share/node-plane-workstation/env"'
  local files=("$HOME/.profile")
  for rc in "$HOME/.bash_profile" "$HOME/.bash_login"; do
    [[ ! -f "$rc" ]] || files+=("$rc")
  done
  if [[ "${shell_name##*/}" == bash || -f "$HOME/.bashrc" ]]; then files+=("$HOME/.bashrc"); fi
  if [[ "${shell_name##*/}" == zsh || -f "${ZDOTDIR:-$HOME}/.zshenv" ]]; then
    files+=("${ZDOTDIR:-$HOME}/.zshenv")
  fi
  for rc in "${files[@]}"; do
    mkdir -p "$(dirname "$rc")"
    if ! grep -qxF "$entry" "$rc" 2>/dev/null; then printf '\n%s\n' "$entry" >> "$rc"; fi
  done
  if [[ "${shell_name##*/}" == fish || -d "${XDG_CONFIG_HOME:-$HOME/.config}/fish" ]]; then
    mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d"
    cat > "${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/node-plane.fish" <<'EOF'
if not contains -- "$HOME/.local/bin" $PATH
    set -gx PATH "$HOME/.local/bin" $PATH
end
EOF
  fi
}
matches_channel() {
  case "$channel" in
    dev) [[ "$1" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-alpha\.(0|[1-9][0-9]*)$ ]] ;;
    beta) [[ "$1" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-(beta|rc)\.(0|[1-9][0-9]*)$ ]] ;;
    stable) [[ "$1" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] ;;
  esac
}
verify_metadata() {
  local candidate="$1" key
  matches_channel "$candidate" || return 1
  download "https://github.com/saharoktyan/node-plane/releases/download/$candidate/RELEASE_METADATA.txt" "$tmp_dir/metadata" || return 1
  [[ "$(wc -c < "$tmp_dir/metadata")" -le 4096 ]] || return 1
  for key in format channel tag version commit; do
    [[ "$(grep -c "^$key=" "$tmp_dir/metadata")" == 1 ]] || return 1
  done
  grep -qxF 'format=2' "$tmp_dir/metadata" &&
    grep -qxF "channel=$channel" "$tmp_dir/metadata" &&
    grep -qxF "tag=$candidate" "$tmp_dir/metadata" &&
    grep -qxF "version=${candidate#v}" "$tmp_dir/metadata" &&
    grep -qxE 'commit=[0-9a-f]{40}' "$tmp_dir/metadata"
}
if [[ -z "$tag" ]]; then
  echo '[1/4] Finding the latest workstation release…'
  : > "$tmp_dir/tags"
  for page in {1..20}; do
    download "https://api.github.com/repos/saharoktyan/node-plane/releases?per_page=100&page=$page" "$tmp_dir/releases.json"
    grep -oE '"tag_name"[[:space:]]*:[[:space:]]*"v[0-9]+\.[0-9]+\.[0-9]+(-(alpha|beta|rc)\.[0-9]+)?"' "$tmp_dir/releases.json" \
      | sed -E 's/^.*:[[:space:]]*"([^"]+)"$/\1/' >> "$tmp_dir/tags" || true
    count="$( { grep -oE '"tag_name"[[:space:]]*:' "$tmp_dir/releases.json" || true; } | wc -l)"
    [[ "$count" -ge 100 ]] || break
  done
  # Numeric SemVer ordering is portable to macOS (sort -V is not available).
  tags="$(awk -F'[-.]' '{print substr($1,2), $2, $3, ($4=="rc"?2:($4=="beta"?1:0)), ($5+0), $0}' "$tmp_dir/tags" \
    | sort -k1,1nr -k2,2nr -k3,3nr -k4,4nr -k5,5nr | awk '{print $6}' || true)"
  while IFS= read -r candidate; do
    if [[ -n "$candidate" ]] && verify_metadata "$candidate"; then tag="$candidate"; break; fi
  done <<< "$tags"
  [[ -n "$tag" ]] || { echo 'No published Workstation release in this channel with current release metadata.' >&2; exit 1; }
else
  verify_metadata "$tag" || { echo 'The release tag lacks matching channel metadata.' >&2; exit 1; }
  echo "[1/4] Using release $tag"
fi

base="https://github.com/saharoktyan/node-plane/releases/download/$tag"
echo "[2/4] Downloading the release installer for $tag…"
if download "$base/node-plane-cli-installer.sh" "$tmp_dir/installer.sh"; then
  echo '[3/4] Starting the cargo-dist installer…'
  install_dir="${NODE_PLANE_CLI_INSTALL_DIR:-${CARGO_DIST_FORCE_INSTALL_DIR:-${CARGO_DIST_INSTALL_DIR:-$HOME/.local/share/node-plane-workstation/$channel}}}"
  receipt_base="${XDG_CONFIG_HOME:-$HOME/.config}/node-plane-workstation/$channel"
  XDG_CONFIG_HOME="$receipt_base" CARGO_DIST_FORCE_INSTALL_DIR="$install_dir" NODE_PLANE_CLI_INSTALL_DIR="$install_dir" NODE_PLANE_CLI_NO_MODIFY_PATH=1 sh "$tmp_dir/installer.sh"
  [[ -x "$install_dir/node-plane" ]] || { echo 'Installed Workstation executable is missing.' >&2; exit 1; }
  install_dir="$(cd "$install_dir" && pwd -P)"
  mkdir -p "$HOME/.local/bin"
  launcher=node-plane
  if [[ "$channel" != stable ]]; then
    launcher="node-plane-$channel"
    ln -sfn "$install_dir/node-plane" "$install_dir/$launcher"
  fi
  ln -sfn "$install_dir/node-plane" "$HOME/.local/bin/$launcher"
  configure_launcher_path
  echo "[4/4] Workstation installed. Open a new terminal, then run: $launcher"
  exit 0
fi

echo "No published Workstation installer is available for $tag." >&2
echo 'For source-only tags or the current dev branch: https://github.com/saharoktyan/node-plane/blob/dev/docs/workstation.md#build-from-source' >&2
exit 1
