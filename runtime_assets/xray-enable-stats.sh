#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

CONFIG="${XRAY_CONFIG:-/opt/node-plane-runtime/xray/config.json}"
CONTAINER="${XRAY_CONTAINER_NAME:-xray}"
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

CONFIG_ENV="$CONFIG" TMP_ENV="$tmp" python3 - <<'PY'
import json
import os
import sys

cfg_path = os.environ["CONFIG_ENV"]
tmp_path = os.environ["TMP_ENV"]

cfg = json.load(open(cfg_path, "r", encoding="utf-8"))
changed = False

if "stats" not in cfg or not isinstance(cfg.get("stats"), dict):
    cfg["stats"] = {}
    changed = True

policy = cfg.setdefault("policy", {})
levels = policy.setdefault("levels", {})
level0 = levels.setdefault("0", {})
if level0.get("statsUserUplink") is not True:
    level0["statsUserUplink"] = True
    changed = True
if level0.get("statsUserDownlink") is not True:
    level0["statsUserDownlink"] = True
    changed = True

api = cfg.setdefault("api", {})
if api.get("tag") != "api":
    api["tag"] = "api"
    changed = True
services = list(api.get("services") or [])
if "StatsService" not in services:
    services.append("StatsService")
    api["services"] = services
    changed = True

inbounds = cfg.setdefault("inbounds", [])
api_inbound = None
for inbound in inbounds:
    if inbound.get("tag") == "api":
        api_inbound = inbound
        break
if api_inbound is None:
    api_inbound = {
        "tag": "api",
        "listen": "127.0.0.1",
        "port": 10085,
        "protocol": "dokodemo-door",
        "settings": {"address": "127.0.0.1"},
    }
    inbounds.insert(0, api_inbound)
    changed = True
else:
    if api_inbound.get("listen") != "127.0.0.1":
        api_inbound["listen"] = "127.0.0.1"
        changed = True
    if int(api_inbound.get("port") or 0) != 10085:
        api_inbound["port"] = 10085
        changed = True
    if api_inbound.get("protocol") != "dokodemo-door":
        api_inbound["protocol"] = "dokodemo-door"
        changed = True
    settings = api_inbound.setdefault("settings", {})
    if settings.get("address") != "127.0.0.1":
        settings["address"] = "127.0.0.1"
        changed = True

outbounds = cfg.setdefault("outbounds", [])
if not any(ob.get("tag") == "api" for ob in outbounds):
    outbounds.append({"protocol": "freedom", "tag": "api"})
    changed = True

routing = cfg.setdefault("routing", {})
rules = routing.setdefault("rules", [])
if not any(rule.get("outboundTag") == "api" and "api" in (rule.get("inboundTag") or []) for rule in rules):
    rules.insert(0, {"type": "field", "inboundTag": ["api"], "outboundTag": "api"})
    changed = True

for inbound in inbounds:
    for client in ((inbound.get("settings") or {}).get("clients") or []):
        name = client.get("name")
        if name and client.get("email") != name:
            client["email"] = name
            changed = True

with open(tmp_path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=2)

print("changed" if changed else "ok")
PY

status="$(tail -n 1 "$tmp" 2>/dev/null || true)"
if [[ "$status" == "changed" || "$status" == "ok" ]]; then
  true
fi

result="$(CONFIG_ENV="$CONFIG" TMP_ENV="$tmp" python3 - <<'PY'
import json
import os
import sys

tmp_path = os.environ["TMP_ENV"]
cfg = json.load(open(tmp_path, "r", encoding="utf-8"))
print(json.dumps(cfg))
PY
)" >/dev/null 2>&1 || true

changed="$(CONFIG_ENV="$CONFIG" TMP_ENV="$tmp" python3 - <<'PY'
import json
import os

cfg_path = os.environ["CONFIG_ENV"]
tmp_path = os.environ["TMP_ENV"]
old = json.load(open(cfg_path, "r", encoding="utf-8"))
new = json.load(open(tmp_path, "r", encoding="utf-8"))
print("1" if old != new else "0")
PY
)"

if [[ "$changed" == "1" ]]; then
  python3 -m json.tool "$tmp" >/dev/null
  cp -a "$CONFIG" "${CONFIG}.bak.$(date +%Y%m%d-%H%M%S)"
  mv "$tmp" "$CONFIG"
  chmod 0600 "$CONFIG"
  docker_cmd restart "$CONTAINER" >/dev/null 2>&1 || true
  echo "enabled"
else
  rm -f "$tmp"
  echo "ok"
fi
