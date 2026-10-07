#!/usr/bin/env bash
# Separate release environments, with immutable package files shared through uv.
NODE_PLANE_UV_VERSION=0.12.23

ensure_controller_host_tools() {
  local item command package ssh_package=openssh-client
  local -a missing=()
  if command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
    ssh_package=openssh-clients
  fi
  for item in git:git python3:python3 curl:curl openssl:openssl sudo:sudo \
      "ssh:${ssh_package}" "ssh-keygen:${ssh_package}" flock:util-linux \
      systemctl:systemd tar:tar gzip:gzip sha256sum:coreutils timeout:coreutils find:findutils; do
    command="${item%%:*}"; package="${item#*:}"
    if ! command -v "$command" >/dev/null 2>&1; then missing+=("$package"); fi
  done
  if [[ ! -s /etc/ssl/certs/ca-certificates.crt && ! -s /etc/pki/tls/certs/ca-bundle.crt ]]; then
    missing+=(ca-certificates)
  fi
  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "Installing controller host prerequisites: ${missing[*]}"
    install_packages_if_needed "${missing[@]}" || return 1
  fi
}

select_controller_python() {
  local shared_dir="$1" selected
  if selected="$(select_python_runtime 2>/dev/null)"; then
    printf '%s\n' "$selected"
    return 0
  fi
  # Explicit interpreter choices must not silently fall back.
  if [[ -n "${NODE_PLANE_PYTHON_BIN:-}" ]]; then
    select_python_runtime
    return 1
  fi
  PYTHON_BIN="$(command -v python3)" || return 1
  ensure_uv_runtime "$shared_dir" >&2 || return 1
  if selected="$(UV_NO_CONFIG=1 UV_PYTHON_DOWNLOADS=never \
      UV_PYTHON_INSTALL_DIR="${shared_dir}/tools/python" \
      "$UV_BIN" python find --managed-python 3.12 2>/dev/null)"; then
    readlink -f "$selected"
    return 0
  fi
  UV_NO_CONFIG=1 UV_PYTHON_INSTALL_DIR="${shared_dir}/tools/python" \
    "$UV_BIN" python install 3.12 >&2 || return 1
  selected="$(UV_NO_CONFIG=1 UV_PYTHON_INSTALL_DIR="${shared_dir}/tools/python" \
    "$UV_BIN" python find --managed-python 3.12)" || return 1
  readlink -f "$selected"
}

ensure_uv_runtime() {
  local shared_dir="$1" installer
  UV_CACHE_DIR="${shared_dir}/cache/uv"
  export UV_CACHE_DIR
  UV_BIN="${NODE_PLANE_UV_BIN:-${shared_dir}/tools/uv-${NODE_PLANE_UV_VERSION}/uv}"
  mkdir -p "$UV_CACHE_DIR"
  if [[ -n "${NODE_PLANE_UV_BIN:-}" ]]; then
    "$UV_BIN" --version >/dev/null || return 1
    return 0
  fi
  if [[ ! -x "$UV_BIN" ]]; then
    installer="$(mktemp)"
    if ! "$PYTHON_BIN" - "$NODE_PLANE_UV_VERSION" "$installer" <<'PY'
import pathlib
import sys
import urllib.request
url = f'https://astral.sh/uv/{sys.argv[1]}/install.sh'
request = urllib.request.Request(url, headers={'User-Agent': 'node-plane-installer'})
with urllib.request.urlopen(request, timeout=60) as response:
    pathlib.Path(sys.argv[2]).write_bytes(response.read())
PY
    then
      rm -f "$installer"
      return 1
    fi
    if ! UV_UNMANAGED_INSTALL="$(dirname "$UV_BIN")" sh "$installer"; then
      rm -f "$installer"
      return 1
    fi
    rm -f "$installer"
  fi
  [[ "$("$UV_BIN" --version)" == "uv ${NODE_PLANE_UV_VERSION}"* ]] || {
    echo "Unexpected uv version at ${UV_BIN}" >&2
    return 1
  }
}

install_release_dependencies() {
  local release_dir="$1" shared_dir="$2"
  ensure_uv_runtime "$shared_dir" || return 1
  if [[ ! -x "${release_dir}/.venv/bin/python" ]]; then
    UV_NO_CONFIG=1 UV_PYTHON_DOWNLOADS=never "$UV_BIN" venv \
      --python "$PYTHON_BIN" "${release_dir}/.venv" || return 1
  fi
  UV_NO_CONFIG=1 UV_PYTHON_DOWNLOADS=never UV_CACHE_DIR="$UV_CACHE_DIR" \
    "$UV_BIN" pip install --python "${release_dir}/.venv/bin/python" \
      --link-mode hardlink --compile-bytecode -r "${release_dir}/requirements.txt" || return 1
  # Older, reused environments can still contain the retired Telegram client.
  UV_NO_CONFIG=1 "$UV_BIN" pip uninstall --python "${release_dir}/.venv/bin/python" \
    python-telegram-bot || return 1
}

retain_successful_releases() {
  local base_dir="$1" current_release="$2" previous_release="$3" shared_dir="$4"
  # A cleanup failure must never roll back a healthy installation.
  if ! "$PYTHON_BIN" "${SCRIPT_DIR}/release_retention.py" \
    --base "$base_dir" --current "$current_release" --previous "$previous_release"; then
    echo "Automatic release cleanup needs attention; the working stack was retained." >&2
    return 0
  fi
  if [[ -n "${UV_BIN:-}" && -x "$UV_BIN" ]]; then
    UV_NO_CONFIG=1 UV_CACHE_DIR="${shared_dir}/cache/uv" "$UV_BIN" cache prune || \
      echo "uv cache pruning did not finish; the working stack was retained." >&2
  fi
}
