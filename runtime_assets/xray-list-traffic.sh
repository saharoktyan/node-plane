#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

CONTAINER="${XRAY_CONTAINER_NAME:-xray}"
CONFIG="${XRAY_CONFIG:-/opt/node-plane-runtime/xray/config.json}"

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

if ! docker_cmd inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "xray telemetry failed: container '$CONTAINER' not found" >&2
  exit 1
fi

if [[ "$(docker_cmd inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)" != "true" ]]; then
  echo "xray telemetry failed: container '$CONTAINER' is not running" >&2
  exit 1
fi

CONFIG_SUMMARY="$(CONFIG_ENV="$CONFIG" python3 - <<'PY'
import json
import os

cfg_path = os.environ["CONFIG_ENV"]
try:
    cfg = json.load(open(cfg_path, "r", encoding="utf-8"))
except Exception as exc:
    print(f"config_read_error={exc}")
    raise SystemExit(0)

api = cfg.get("api") or {}
services = list(api.get("services") or [])
policy = (cfg.get("policy") or {}).get("levels") or {}
level0 = policy.get("0") or {}
has_api_inbound = any((ib.get("tag") == "api") for ib in (cfg.get("inbounds") or []))
has_api_route = any(
    rule.get("outboundTag") == "api" and "api" in (rule.get("inboundTag") or [])
    for rule in ((cfg.get("routing") or {}).get("rules") or [])
)

print(
    "stats="
    + ("yes" if isinstance(cfg.get("stats"), dict) else "no")
    + ", api_tag="
    + str(api.get("tag") or "—")
    + ", services="
    + ",".join(services or ["—"])
    + ", uplink="
    + ("yes" if level0.get("statsUserUplink") is True else "no")
    + ", downlink="
    + ("yes" if level0.get("statsUserDownlink") is True else "no")
    + ", api_inbound="
    + ("yes" if has_api_inbound else "no")
    + ", api_route="
    + ("yes" if has_api_route else "no")
)
PY
)"

set +e
RAW="$(docker_cmd exec -i "$CONTAINER" xray api statsquery --server=127.0.0.1:10085 2>&1)"
rc=$?
if [ "$rc" -ne 0 ]; then
  RAW="$(docker_cmd exec -i "$CONTAINER" /usr/local/bin/xray api statsquery --server=127.0.0.1:10085 2>&1)"
  rc=$?
fi
if [ "$rc" -ne 0 ]; then
  RAW="$(docker_cmd exec -i "$CONTAINER" /usr/bin/xray api statsquery --server=127.0.0.1:10085 2>&1)"
  rc=$?
fi
set -e
if [ "$rc" -ne 0 ]; then
  echo "xray telemetry failed: statsquery rc=$rc" >&2
  echo "config: $CONFIG_SUMMARY" >&2
  echo "output:" >&2
  echo "$RAW" >&2
  exit 1
fi

XRAY_STATS_RAW="$RAW" python3 - <<'PY'
import json
import os
import re

text = os.environ.get("XRAY_STATS_RAW", "")
items = {}

try:
    payload = json.loads(text)
    for stat in payload.get("stat") or []:
        name = str(stat.get("name") or "")
        value = int(stat.get("value") or 0)
        match = re.fullmatch(r'user>>>(.*?)>>>traffic>>>(uplink|downlink)', name)
        if not match:
            continue
        email, direction = match.groups()
        rec = items.setdefault(email, {"name": email, "uplink_bytes_total": 0, "downlink_bytes_total": 0})
        if direction == "uplink":
            rec["uplink_bytes_total"] = value
        else:
            rec["downlink_bytes_total"] = value
except Exception:
    matches = re.findall(
        r'name:\\s*"user>>>(.*?)>>>traffic>>>(uplink|downlink)"\\s*value:\\s*([0-9]+)',
        text,
        flags=re.S,
    )
    for email, direction, value in matches:
        rec = items.setdefault(email, {"name": email, "uplink_bytes_total": 0, "downlink_bytes_total": 0})
        if direction == "uplink":
            rec["uplink_bytes_total"] = int(value)
        else:
            rec["downlink_bytes_total"] = int(value)

print(json.dumps(sorted(items.values(), key=lambda item: item["name"].lower()), ensure_ascii=False))
PY
