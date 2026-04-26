#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

CONFIG="${XRAY_CONFIG:-/usr/local/etc/xray/config.json}"
CONTAINER="${XRAY_CONTAINER_NAME:-xray}"
NAME="${1:-}"
if [[ -z "$NAME" ]]; then
  echo "Usage: $0 <name>" >&2
  exit 1
fi
tmp="$(mktemp)"

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return
  fi
  if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    sudo docker "$@"
    return
  fi
  echo "Docker is not available for this user." >&2
  exit 1
}

python3 - <<PY
import json

cfg_path="$CONFIG"
tmp_path="$tmp"
name="$NAME"
j=json.load(open(cfg_path, encoding="utf-8"))
removed = 0
for ib in j.get("inbounds", []):
    settings = ib.get("settings", {})
    clients = settings.get("clients", [])
    if not clients:
        continue
    new_clients = [c for c in clients if c.get("name") != name]
    removed += len(clients) - len(new_clients)
    settings["clients"] = new_clients

json.dump(j, open(tmp_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(removed)
PY

python3 -m json.tool "$tmp" >/dev/null
cp -a "$CONFIG" "${CONFIG}.bak.$(date +%Y%m%d-%H%M%S)"
mv "$tmp" "$CONFIG"
docker_cmd restart "$CONTAINER" >/dev/null 2>&1 || true
echo "OK"
