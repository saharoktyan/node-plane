#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

CONFIG="${XRAY_CONFIG:-/usr/local/etc/xray/config.json}"
CONTAINER="${XRAY_CONTAINER_NAME:-xray}"
TCP_TAG="${XRAY_INBOUND_TCP_TAG:-reality-tcp}"
XHTTP_TAG="${XRAY_INBOUND_XHTTP_TAG:-reality-xhttp}"

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

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "Usage: $0 <name> <uuid> [short_id]" >&2
  exit 1
fi

TAG="$1"
UUID="$2"
SHORT_ID="${3:-}"
tmp="$(mktemp)"

CFG_PATH="$CONFIG" TMP_PATH="$tmp" TAG_NAME="$TAG" UUID_VALUE="$UUID" SHORT_ID_ENV="$SHORT_ID" TCP_TAG_ENV="$TCP_TAG" XHTTP_TAG_ENV="$XHTTP_TAG" python3 - <<'PY'
import json
import os

cfg_path=os.environ["CFG_PATH"]
tmp_path=os.environ["TMP_PATH"]
name=os.environ["TAG_NAME"]
uuid=os.environ["UUID_VALUE"]
short_id=os.environ["SHORT_ID_ENV"]
tcp_tag=os.environ["TCP_TAG_ENV"]
xhttp_tag=os.environ["XHTTP_TAG_ENV"]

j=json.load(open(cfg_path, "r", encoding="utf-8"))

def ensure_client(ib, flow=None):
    clients = ib.setdefault("settings", {}).setdefault("clients", [])
    for c in clients:
        if c.get("name")==name:
            c["id"]=uuid
            c["email"]=name
            if flow:
                c["flow"]=flow
            else:
                c.pop("flow", None)
            return
    obj={"id": uuid, "name": name, "email": name}
    if flow:
        obj["flow"]=flow
    clients.append(obj)

found_tcp=False
found_xhttp=False
for ib in j.get("inbounds", []):
    tag=ib.get("tag")
    if tag==tcp_tag:
        ensure_client(ib, flow="xtls-rprx-vision")
        found_tcp=True
    elif tag==xhttp_tag:
        ensure_client(ib, flow=None)
        found_xhttp=True
    stream = ib.get("streamSettings", {}) or {}
    reality = stream.get("realitySettings", {}) or {}
    short_ids = list(reality.get("shortIds") or [])
    if short_id and short_id not in short_ids:
        short_ids.append(short_id)
        reality["shortIds"] = short_ids

if not found_tcp or not found_xhttp:
    raise SystemExit(f"Не найдены нужные inbound tags. tcp={found_tcp}, xhttp={found_xhttp}")

json.dump(j, open(tmp_path,"w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY

python3 -m json.tool "$tmp" >/dev/null
cp -a "$CONFIG" "${CONFIG}.bak.$(date +%Y%m%d-%H%M%S)"
mv "$tmp" "$CONFIG"
docker_cmd restart "$CONTAINER" >/dev/null 2>&1 || true
echo "OK"
