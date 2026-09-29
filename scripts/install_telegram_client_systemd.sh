#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 4 ]]; then
  echo "Usage: $0 BASE_DIR SHARED_DIR [--render-dir DIR | --activate]" >&2
  exit 2
fi
base_dir="$1"
shared_dir="$2"
render_dir=""
activate=0
render_only=0
if [[ $# -gt 2 ]]; then
  case "$3" in
    --render-dir)
      [[ $# -eq 4 ]] || { echo "Expected --render-dir DIR" >&2; exit 2; }
      render_dir="$4"
      render_only=1
      ;;
    --activate)
      [[ $# -eq 3 ]] || { echo "Unexpected argument" >&2; exit 2; }
      activate=1
      ;;
    *) echo "Unknown option: $3" >&2; exit 2 ;;
  esac
fi
current_dir="${base_dir}/current"
if [[ ! -x "${current_dir}/.venv/bin/python" || ! -f "${shared_dir}/.env" ]]; then
  echo "Installed release or shared environment is missing" >&2
  exit 1
fi
read_env_value() {
  sed -n "s/^${1}=//p" "${shared_dir}/.env" | tail -n 1
}
bot_token="$(read_env_value BOT_TOKEN)"
adapter_token_file="$(read_env_value NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE)"
if [[ -z "$bot_token" || -z "$adapter_token_file" \
   || ! -f "$adapter_token_file" || ! -r "$adapter_token_file" ]]; then
  echo "BOT_TOKEN and a readable NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE are required" >&2
  exit 1
fi
if [[ -z "$render_dir" ]]; then
  [[ "$(id -u)" -eq 0 ]] || { echo "Run as root to install a systemd unit" >&2; exit 1; }
  render_dir="$(mktemp -d)"
  trap 'rm -rf "$render_dir"' EXIT
fi
mkdir -p "$render_dir"
cat > "${render_dir}/node-plane-telegram.service" <<EOF
[Unit]
Description=Node Plane Telegram Client
After=network-online.target node-plane-backend.service
Wants=network-online.target node-plane-backend.service

[Service]
Type=simple
WorkingDirectory=${current_dir}
Environment=NODE_PLANE_APP_DIR=${current_dir}
Environment=NODE_PLANE_SHARED_DIR=${shared_dir}
Environment=PYTHONPATH=${current_dir}/app
EnvironmentFile=${shared_dir}/.env
ExecStart=${current_dir}/.venv/bin/python -m telegram_client.main
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

[[ "$render_only" -eq 1 ]] && exit 0
install -m 0644 "${render_dir}/node-plane-telegram.service" /etc/systemd/system/
systemctl daemon-reload
if [[ "$activate" -eq 0 ]]; then
  echo "node-plane-telegram.service installed but inactive. Run with --activate after testing the backend." >&2
  exit 0
fi
legacy_was_active=0
legacy_was_enabled=0
if systemctl is-enabled --quiet node-plane.service; then
  legacy_was_enabled=1
fi
if systemctl is-active --quiet node-plane.service; then
  legacy_was_active=1
fi
if [[ "$legacy_was_enabled" -eq 1 ]]; then
  systemctl disable node-plane.service
fi
if [[ "$legacy_was_active" -eq 1 ]]; then
  if ! systemctl stop node-plane.service; then
    if [[ "$legacy_was_enabled" -eq 1 ]]; then systemctl enable node-plane.service; fi
    exit 1
  fi
fi
restore_legacy() {
  if [[ "$legacy_was_enabled" -eq 1 ]]; then systemctl enable node-plane.service; fi
  if [[ "$legacy_was_active" -eq 1 ]]; then systemctl start node-plane.service; fi
}
if ! systemctl enable node-plane-telegram.service; then
  restore_legacy
  exit 1
fi
if ! systemctl restart node-plane-telegram.service; then
  systemctl disable node-plane-telegram.service || true
  restore_legacy
  echo "New client failed to start; previous bot was restored if it was active" >&2
  exit 1
fi
sleep 2
if ! systemctl is-active --quiet node-plane-telegram.service; then
  systemctl stop node-plane-telegram.service || true
  systemctl disable node-plane-telegram.service || true
  restore_legacy
  echo "New client stopped after startup; inspect journalctl -u node-plane-telegram" >&2
  exit 1
fi
echo "node-plane-telegram.service is active"
