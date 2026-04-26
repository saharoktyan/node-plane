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

read -r TAG
TAG="${TAG:-}"
if [[ -z "$TAG" ]]; then
  echo "Имя не может быть пустым" >&2
  exit 1
fi
if [[ ! "$TAG" =~ ^[a-zA-Z0-9._-]+$ ]]; then
  echo "Допустимы только латиница/цифры/._-" >&2
  exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
  echo "Xray config not found: $CONFIG" >&2
  exit 1
fi

tmp="$(mktemp)"
UUID="$(CFG_PATH="$CONFIG" TMP_PATH="$tmp" TAG_NAME="$TAG" TCP_TAG_ENV="$TCP_TAG" XHTTP_TAG_ENV="$XHTTP_TAG" python3 - <<'PY'
import json, sys, uuid
import os

cfg_path=os.environ["CFG_PATH"]
tmp_path=os.environ["TMP_PATH"]
name=os.environ["TAG_NAME"]
tcp_tag=os.environ["TCP_TAG_ENV"]
xhttp_tag=os.environ["XHTTP_TAG_ENV"]

j=json.load(open(cfg_path, "r", encoding="utf-8"))

for ib in j.get("inbounds", []):
    for c in (ib.get("settings", {}) or {}).get("clients", []) or []:
        if c.get("name") == name:
            print("DUPLICATE", file=sys.stderr)
            sys.exit(2)

new_uuid=str(uuid.uuid4())

def add_client(ib, flow=None):
    clients = ib.setdefault("settings", {}).setdefault("clients", [])
    obj={"id": new_uuid, "name": name, "email": name}
    if flow:
        obj["flow"]=flow
    clients.append(obj)

found_tcp=False
found_xhttp=False
for ib in j.get("inbounds", []):
    tag=ib.get("tag")
    if tag==tcp_tag:
        add_client(ib, flow="xtls-rprx-vision")
        found_tcp=True
    elif tag==xhttp_tag:
        add_client(ib, flow=None)
        found_xhttp=True

if not found_tcp or not found_xhttp:
    print(f"MISSING_INBOUNDS tcp={found_tcp} xhttp={found_xhttp}", file=sys.stderr)
    sys.exit(3)

with open(tmp_path, "w", encoding="utf-8") as f:
    json.dump(j, f, ensure_ascii=False, indent=2)

print(new_uuid)
PY
)" || {
  rc=$?
  if [[ $rc -eq 2 ]]; then
    echo "❌ Пользователь '$TAG' уже существует!" >&2
    exit 1
  fi
  echo "❌ Ошибка обновления Xray config (rc=$rc)." >&2
  exit 1
}

SHORT_ID="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(8))
PY
)"

CFG_PATH="$CONFIG" TMP_PATH="$tmp.shortid" SHORT_ID_ENV="$SHORT_ID" python3 - <<'PY'
import json
import os

cfg_path=os.environ["CFG_PATH"]
tmp_path=os.environ["TMP_PATH"]
short_id=os.environ["SHORT_ID_ENV"]
j=json.load(open(cfg_path, "r", encoding="utf-8"))

for ib in j.get("inbounds", []):
    stream = ib.get("streamSettings", {}) or {}
    reality = stream.get("realitySettings", {}) or {}
    short_ids = list(reality.get("shortIds") or [])
    if short_id not in short_ids:
        short_ids.append(short_id)
        reality["shortIds"] = short_ids

with open(tmp_path, "w", encoding="utf-8") as f:
    json.dump(j, f, ensure_ascii=False, indent=2)
PY

python3 -m json.tool "$tmp.shortid" >/dev/null
mv "$tmp.shortid" "$tmp"

python3 -m json.tool "$tmp" >/dev/null
cp -a "$CONFIG" "${CONFIG}.bak.$(date +%Y%m%d-%H%M%S)"
mv "$tmp" "$CONFIG"
docker_cmd restart "$CONTAINER" >/dev/null 2>&1 || true
echo "$UUID $SHORT_ID"
