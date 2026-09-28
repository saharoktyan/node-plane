#!/usr/bin/env bash
set -euo pipefail

# Render with --render-dir for review/tests; otherwise install and start units.
if [[ $# -lt 2 || $# -gt 4 ]]; then
  echo "Usage: $0 BASE_DIR SHARED_DIR [--render-dir DIR]" >&2
  exit 2
fi
base_dir="$1"
shared_dir="$2"
render_dir=""
if [[ $# -gt 2 ]]; then
  if [[ "$3" != "--render-dir" || $# -ne 4 ]]; then
    echo "Expected --render-dir DIR" >&2
    exit 2
  fi
  render_dir="$4"
fi
current_dir="${base_dir}/current"
if [[ ! -x "${current_dir}/.venv/bin/python" || ! -f "${shared_dir}/.env" ]]; then
  echo "Backend release virtualenv or shared environment is missing" >&2
  exit 1
fi
if [[ -z "$render_dir" ]]; then
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run as root to install backend systemd units" >&2
    exit 1
  fi
  render_dir="$(mktemp -d)"
  trap 'rm -rf "$render_dir"' EXIT
fi
mkdir -p "$render_dir"

cat > "${render_dir}/node-plane-backend.service" <<EOF
[Unit]
Description=Node Plane Backend API
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${current_dir}
Environment=NODE_PLANE_BASE_DIR=${base_dir}
Environment=NODE_PLANE_APP_DIR=${current_dir}
Environment=NODE_PLANE_SHARED_DIR=${shared_dir}
Environment=PYTHONPATH=${current_dir}/app
EnvironmentFile=${shared_dir}/.env
ExecStart=${current_dir}/.venv/bin/python -m uvicorn backend.http_api:application --factory --host 127.0.0.1 --port 8080 --no-access-log
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > "${render_dir}/node-plane-backend-worker.service" <<EOF
[Unit]
Description=Node Plane Backend Worker
After=network-online.target postgresql.service node-plane-driver.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=${current_dir}
Environment=NODE_PLANE_BASE_DIR=${base_dir}
Environment=NODE_PLANE_APP_DIR=${current_dir}
Environment=NODE_PLANE_SHARED_DIR=${shared_dir}
Environment=PYTHONPATH=${current_dir}/app
EnvironmentFile=${shared_dir}/.env
ExecStart=${current_dir}/.venv/bin/python -m backend.executor --lock-file ${shared_dir}/data/backend-worker.lock
TimeoutStartSec=1h
EOF

cat > "${render_dir}/node-plane-backend-worker.timer" <<'EOF'
[Unit]
Description=Run Node Plane Backend Worker

[Timer]
OnBootSec=5s
OnUnitInactiveSec=5s
Unit=node-plane-backend-worker.service

[Install]
WantedBy=timers.target
EOF

if [[ $# -gt 2 ]]; then
  exit 0
fi

install -m 0644 "${render_dir}/node-plane-backend.service" /etc/systemd/system/
install -m 0644 "${render_dir}/node-plane-backend-worker.service" /etc/systemd/system/
install -m 0644 "${render_dir}/node-plane-backend-worker.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable node-plane-backend.service node-plane-backend-worker.timer
systemctl restart node-plane-backend.service
systemctl start node-plane-backend-worker.timer
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if "${current_dir}/.venv/bin/python" -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/health/ready", timeout=2).read()' >/dev/null 2>&1; then
    exit 0
  fi
  sleep 1
done
echo "Backend API did not become ready; inspect journalctl -u node-plane-backend" >&2
exit 1
