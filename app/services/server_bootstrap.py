from __future__ import annotations

import base64
import json
import logging
import re
import shlex
from typing import Dict, Tuple

from config import APP_COMMIT, APP_SEMVER
from services.server_registry import RegisteredServer, get_server, list_servers, update_server_fields
from services.server_runtime import is_running_in_container, run_server_command, write_server_file, write_server_files
from services.ssh_keys import get_public_key
from utils.security import shell_env_assignment


log = logging.getLogger("server_bootstrap")


_JSON_OBJECT_AT_END_RE = re.compile(r"(\{[\s\S]*\})\s*$")
AWG_RUNTIME_CONTAINER = "amnezia-awg"
RUNTIME_VERSION_PATH = "/opt/node-plane-runtime/VERSION"
RUNTIME_BUILD_COMMIT_PATH = "/opt/node-plane-runtime/BUILD_COMMIT"


def _shell_join_args(*args: object) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in args)


def _extract_last_json_object(text: str) -> dict:
    match = _JSON_OBJECT_AT_END_RE.search(text or "")
    if not match:
        raise ValueError("No JSON object found in output")
    return json.loads(match.group(1))


def _docker_status(server: RegisteredServer) -> tuple[bool, str]:
    rc, out = run_server_command(
        server,
        """#!/usr/bin/env bash
set -euo pipefail
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "available"
elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
  echo "available_via_sudo"
else
  echo "missing"
fi
""",
        timeout=30,
    )
    status = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else "missing"
    return (rc == 0 and status in {"available", "available_via_sudo"}, status)


def _docker_install_suggestion(status: str, details: str = "") -> str:
    parts = [
        "Docker недоступен на сервере.",
        "",
        "Bootstrap не может продолжиться без рабочего Docker.",
        "Установи и запусти Docker, затем повтори Probe или Bootstrap.",
        "",
        "Рекомендуемые команды:",
        "apt-get update",
        "apt-get install -y docker.io",
        "apt-cache show docker-compose-plugin >/dev/null 2>&1 && apt-get install -y docker-compose-plugin || true",
        "systemctl enable --now docker || service docker start",
    ]
    if status == "available_via_sudo":
        parts = [
            "Docker доступен только через sudo.",
            "",
            "Для bootstrap это обычно нормально, но если на сервере дальше возникают ошибки доступа к Docker, проверь права пользователя или группу docker.",
        ]
    if details.strip():
        parts.extend(["", "Технические детали:", details.strip()[-1200:]])
    return "\n".join(parts)


NODE_ENV_EXAMPLE = """# Xray
XRAY_CONFIG=/opt/node-plane-runtime/xray/config.json
XRAY_CONTAINER_NAME=xray
XRAY_DOCKER_DIR=/opt/node-plane-runtime/xray
XRAY_DOCKER_IMAGE=ghcr.io/xtls/xray-core:25.12.8
XRAY_INBOUND_TCP_TAG=reality-tcp
XRAY_INBOUND_XHTTP_TAG=reality-xhttp

# AWG / AmneziaWG
AWG_CONTAINER_NAME=amnezia-awg
AWG_DOCKER_DIR=/opt/node-plane-runtime/amnezia-awg
AWG_DOCKER_IMAGE=node-plane-amnezia-awg:0.2.16
AWG_IFACE=wg0
AWG_CONFIG=/opt/node-plane-runtime/amnezia-awg/data/wg0.conf
AWG_SERVER_ADDRESS=10.8.1.0/24
AWG_NETWORK=10.8.1.0/24
AWG_DNS=1.1.1.1
AWG_MTU=1280
AWG_ALLOWED_IPS=0.0.0.0/0
AWG_KEEPALIVE=25
AWG_I1_PRESET=quic
"""


def _runtime_metadata_files() -> Dict[str, Tuple[str, str]]:
    return {
        RUNTIME_VERSION_PATH: (f"{APP_SEMVER}\n", "0644"),
        RUNTIME_BUILD_COMMIT_PATH: (f"{APP_COMMIT}\n", "0644"),
    }


XRAY_ADD_SCRIPT = """#!/usr/bin/env bash
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
"""


XRAY_ADD_EXISTING_SCRIPT = """#!/usr/bin/env bash
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
"""


XRAY_LIST_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env
CONFIG="${XRAY_CONFIG:-/usr/local/etc/xray/config.json}"

python3 - <<PY
import json
j=json.load(open("$CONFIG", encoding="utf-8"))
m={}
for ib in j.get("inbounds",[]):
    for c in (ib.get("settings",{}) or {}).get("clients",[]) or []:
        n=c.get("name")
        u=c.get("id")
        if n and u and n not in m:
            m[n]=u
print("NAME UUID")
for n in sorted(m.keys(), key=str.lower):
    print(n, m[n])
PY
"""


XRAY_DEL_SCRIPT = """#!/usr/bin/env bash
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
"""


XRAY_INIT_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${1:-/opt/node-plane-runtime/xray/config.json}"
PUBLIC_HOST="${2:-}"
SNI_HOST="${3:-www.cloudflare.com}"
TCP_PORT="${4:-443}"
XHTTP_PORT="${5:-8443}"
PATH_PREFIX="${6:-/assets}"
FLOW="${7:-xtls-rprx-vision}"
IMAGE="${8:-ghcr.io/xtls/xray-core:25.12.8}"

if [[ -z "$PUBLIC_HOST" ]]; then
  echo "PUBLIC_HOST is required" >&2
  exit 1
fi

mkdir -p "$(dirname "$CONFIG_PATH")"
if [[ -d "$CONFIG_PATH" ]]; then
  mv "$CONFIG_PATH" "${CONFIG_PATH}.dirbak.$(date +%Y%m%d-%H%M%S)"
fi

docker pull "$IMAGE" >/dev/null 2>&1 || true
X25519_OUT="$(docker run --rm "$IMAGE" x25519)"
read -r PRIVATE_KEY REALITY_PASSWORD < <(
  XRAY_X25519_OUT="$X25519_OUT" python3 - <<'PY'
import os

text = os.environ.get("XRAY_X25519_OUT", "")
lines = [line.rstrip() for line in text.splitlines()]

def extract(prefixes):
    for i, raw in enumerate(lines):
        line = raw.strip()
        lower = line.lower()
        for prefix in prefixes:
            if lower.startswith(prefix):
                tail = line[len(prefix):].lstrip(":").strip()
                if tail:
                    return tail
                for nxt in lines[i + 1:]:
                    candidate = nxt.strip()
                    if candidate:
                        return candidate
    return ""

private_key = extract(("private key", "privatekey"))
reality_password = extract(("password",))
if not reality_password:
    reality_password = extract(("public key", "publickey"))
print(private_key, reality_password)
PY
)
if [[ -z "$PRIVATE_KEY" || -z "$REALITY_PASSWORD" ]]; then
  echo "$X25519_OUT" >&2
  echo "Could not parse xray x25519 output" >&2
  exit 1
fi
SHORT_ID="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(8))
PY
)"

python3 - <<PY
import json

config = {
    "log": {"loglevel": "warning"},
    "stats": {},
    "policy": {
        "levels": {
            "0": {
                "statsUserUplink": True,
                "statsUserDownlink": True
            }
        }
    },
    "api": {"tag": "api", "services": ["StatsService"]},
    "inbounds": [
        {
            "tag": "api",
            "listen": "127.0.0.1",
            "port": 10085,
            "protocol": "dokodemo-door",
            "settings": {"address": "127.0.0.1"},
        },
        {
            "tag": "reality-tcp",
            "listen": "0.0.0.0",
            "port": int(${TCP_PORT}),
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "dest": f"${SNI_HOST}:443",
                    "xver": 0,
                    "serverNames": ["${SNI_HOST}"],
                    "privateKey": "${PRIVATE_KEY}",
                    "shortIds": ["${SHORT_ID}"],
                },
            },
        },
        {
            "tag": "reality-xhttp",
            "listen": "0.0.0.0",
            "port": int(${XHTTP_PORT}),
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {
                "network": "xhttp",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "dest": f"${SNI_HOST}:443",
                    "xver": 0,
                    "serverNames": ["${SNI_HOST}"],
                    "privateKey": "${PRIVATE_KEY}",
                    "shortIds": ["${SHORT_ID}"],
                },
                "xhttpSettings": {"path": "${PATH_PREFIX}"},
            },
        },
    ],
    "outbounds": [
        {"protocol": "freedom", "tag": "direct"},
        {"protocol": "freedom", "tag": "api"},
    ],
    "routing": {
        "rules": [
            {"type": "field", "inboundTag": ["api"], "outboundTag": "api"}
        ]
    },
}

with open("${CONFIG_PATH}", "w", encoding="utf-8") as fh:
    json.dump(config, fh, ensure_ascii=False, indent=2)
PY

chmod 0600 "$CONFIG_PATH"

python3 - <<PY
import json
print(json.dumps({
  "xray_host": "${PUBLIC_HOST}",
  "xray_sni": "${SNI_HOST}",
  "xray_pbk": "${REALITY_PASSWORD}",
  "xray_sid": "${SHORT_ID}",
  "xray_short_id": "${SHORT_ID}",
  "xray_tcp_port": int(${TCP_PORT}),
  "xray_xhttp_port": int(${XHTTP_PORT}),
  "xray_xhttp_path_prefix": "${PATH_PREFIX}",
  "xray_flow": "${FLOW}",
  "xray_fp": "chrome"
}))
PY
"""


XRAY_ENABLE_STATS_SCRIPT = """#!/usr/bin/env bash
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
"""


XRAY_TRAFFIC_SCRIPT = """#!/usr/bin/env bash
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
"""


XRAY_SYNC_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${1:-/opt/node-plane-runtime/xray/config.json}"
PUBLIC_HOST="${2:-}"
FLOW="${3:-xtls-rprx-vision}"
IMAGE="${4:-ghcr.io/xtls/xray-core:25.12.8}"

if [[ -z "$PUBLIC_HOST" ]]; then
  echo "PUBLIC_HOST is required" >&2
  exit 1
fi
if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "Xray config not found: $CONFIG_PATH" >&2
  exit 1
fi

CONFIG_PATH_ENV="$CONFIG_PATH" PUBLIC_HOST_ENV="$PUBLIC_HOST" FLOW_ENV="$FLOW" XRAY_IMAGE_ENV="$IMAGE" python3 - <<'PY'
import json
import os
import subprocess
import sys

cfg_path = os.environ["CONFIG_PATH_ENV"]
public_host = os.environ["PUBLIC_HOST_ENV"]
flow = os.environ["FLOW_ENV"]
image = os.environ["XRAY_IMAGE_ENV"]

cfg = json.load(open(cfg_path, "r", encoding="utf-8"))
tcp = None
xhttp = None
for inbound in cfg.get("inbounds", []):
    tag = inbound.get("tag")
    if tag == "reality-tcp":
        tcp = inbound
    elif tag == "reality-xhttp":
        xhttp = inbound

if not tcp or not xhttp:
    raise SystemExit("Could not find reality-tcp and reality-xhttp inbounds")

reality = (tcp.get("streamSettings", {}) or {}).get("realitySettings", {}) or {}
private_key = reality.get("privateKey") or ""
server_names = reality.get("serverNames") or []
short_ids = reality.get("shortIds") or []
if not private_key:
    raise SystemExit("privateKey is missing in Xray config")

res = subprocess.run(
    ["docker", "run", "--rm", image, "x25519", "-i", private_key],
    capture_output=True,
    text=True,
)
if res.returncode != 0:
    raise SystemExit((res.stderr or res.stdout or "xray x25519 -i failed").strip())

reality_password = ""
for line in (res.stdout or "").splitlines():
    line = line.strip()
    lower = line.lower()
    if lower.startswith("password:"):
        reality_password = line.split(":", 1)[1].strip()
        break
    if lower.startswith("password "):
        reality_password = line.split(None, 1)[1].strip()
        break
    if lower.startswith("public key:"):
        reality_password = line.split(":", 1)[1].strip()
        break
    if lower.startswith("publickey "):
        reality_password = line.split(None, 1)[1].strip()
        break
    if lower in {"password:", "publickey:", "public key:"}:
        continue
    if not reality_password and line and not lower.startswith(("private", "hash32")):
        reality_password = line
        break
if not reality_password:
    raise SystemExit("Could not derive Reality password from private key")

path_prefix = (
    ((xhttp.get("streamSettings", {}) or {}).get("xhttpSettings", {}) or {}).get("path")
    or "/assets"
)

print(json.dumps({
    "xray_host": public_host,
    "xray_sni": server_names[0] if server_names else "",
    "xray_pbk": reality_password,
    "xray_sid": short_ids[0] if short_ids else "",
    "xray_short_id": short_ids[0] if short_ids else "",
    "xray_tcp_port": int(tcp.get("port") or 443),
    "xray_xhttp_port": int(xhttp.get("port") or 8443),
    "xray_xhttp_path_prefix": path_prefix,
    "xray_flow": flow,
    "xray_fp": "chrome",
}))
PY
"""


XRAY_DEPLOY_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return
  fi
  if command -v sudo >/dev/null 2>&1; then
    sudo systemctl enable --now docker >/dev/null 2>&1 || sudo service docker start >/dev/null 2>&1 || true
    if sudo docker info >/dev/null 2>&1; then
      sudo docker "$@"
      return
    fi
  fi
  systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return
  fi
  echo "Docker daemon is unavailable for this session." >&2
  id >&2 || true
  groups >&2 || true
  ls -l /var/run/docker.sock >&2 || true
  exit 1
}

DOCKER_DIR="${XRAY_DOCKER_DIR:-/opt/node-plane-runtime/xray}"
IMAGE="${XRAY_DOCKER_IMAGE:-ghcr.io/xtls/xray-core:25.12.8}"
CONTAINER="${XRAY_CONTAINER_NAME:-xray}"
CONFIG="${XRAY_CONFIG:-/opt/node-plane-runtime/xray/config.json}"

mkdir -p "$DOCKER_DIR"
chmod 0755 /opt >/dev/null 2>&1 || true
chmod 0755 /opt/node-plane-runtime >/dev/null 2>&1 || true
chmod 0755 "$DOCKER_DIR" >/dev/null 2>&1 || true

if [[ -d "$CONFIG" ]]; then
  mv "$CONFIG" "${CONFIG}.dirbak.$(date +%Y%m%d-%H%M%S)"
fi

if [[ ! -f "$CONFIG" ]]; then
  echo "Xray config not found: $CONFIG" >&2
  exit 1
fi

chmod 0600 "$CONFIG" >/dev/null 2>&1 || true

if docker_cmd ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  docker_cmd rm -f "$CONTAINER" >/dev/null
fi

docker_cmd pull "$IMAGE" >/dev/null
docker_cmd run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --user 0:0 \
  --network host \
  -v "$CONFIG:/etc/xray/config.json:ro" \
  "$IMAGE" run -c /etc/xray/config.json >/dev/null

echo "Xray container deployed: $CONTAINER"
"""


AWG_SHOW_ENTROPY_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

CFG="${AWG_CONFIG:-/opt/node-plane-runtime/amnezia-awg/data/wg0.conf}"
PRESET="${AWG_I1_PRESET:-quic}"

if [[ ! -f "$CFG" ]]; then
  echo "AWG config not found: $CFG" >&2
  exit 1
fi

CFG_ENV="$CFG" PRESET_ENV="$PRESET" python3 - <<'PY'
import os

cfg_path = os.environ["CFG_ENV"]
preset = os.environ.get("PRESET_ENV", "quic")
keys = ["Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5"]
values = {key: "" for key in keys}

with open(cfg_path, "r", encoding="utf-8", errors="ignore") as fh:
    for raw in fh:
        line = raw.strip()
        if " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        if key in values:
            values[key] = value.strip()

print(f"preset: {preset}")
for key in keys:
    print(f"{key}: {values[key] or '—'}")
PY
"""


AWG_TEMPLATE_JSON = """{
  "containers": [
    {
      "awg": {
        "H1": "",
        "H2": "",
        "H3": "",
        "H4": "",
        "I1": "",
        "I2": "",
        "I3": "",
        "I4": "",
        "I5": "",
        "Jc": "",
        "Jmax": "",
        "Jmin": "",
        "S1": "",
        "S2": "",
        "S3": "",
        "S4": "",
        "last_config": "",
        "port": "",
        "protocol_version": "2",
        "subnet_address": "10.8.1.0",
        "transport_proto": "udp"
      },
      "container": "amnezia-awg"
    }
  ],
  "defaultContainer": "amnezia-awg",
  "description": "awg",
  "dns1": "1.1.1.1",
  "dns2": "1.0.0.1",
  "hostName": "",
  "nameOverriddenByUser": true
}
"""


AWG_CONF2VPN_PY = """#!/usr/bin/env python3
import json
import re
import subprocess
import sys
from pathlib import Path


def parse_conf(text: str):
    cur = None
    data = {"Interface": {}, "Peer": {}}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\\[(Interface|Peer)\\]$", line, re.I)
        if m:
            cur = m.group(1).capitalize()
            continue
        if cur and "=" in line:
            k, v = map(str.strip, line.split("=", 1))
            data[cur][k] = v
    return data


def _split_csv(value: str):
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def main(conf_path, template_path, out_json_path, decoder_py, container_name="amnezia-awg", description="awg"):
    conf_text = Path(conf_path).read_text(encoding="utf-8", errors="ignore").strip() + "\\n"
    tpl = json.loads(Path(template_path).read_text(encoding="utf-8"))

    cfg = parse_conf(conf_text)
    iface = cfg["Interface"]
    peer = cfg["Peer"]

    client_ip = iface.get("Address", "").split("/", 1)[0]
    endpoint = peer.get("Endpoint", "")
    endpoint_host, endpoint_port = endpoint.rsplit(":", 1)
    allowed = _split_csv(peer.get("AllowedIPs", "")) or ["0.0.0.0/0", "::/0"]
    dns_list = _split_csv(iface.get("DNS", ""))
    dns1 = dns_list[0] if len(dns_list) >= 1 else "1.1.1.1"
    dns2 = dns_list[1] if len(dns_list) >= 2 else "1.0.0.1"
    subnet_address = iface.get("Address", "10.8.1.0/24").split("/", 1)[0].rsplit(".", 1)[0] + ".0"

    awg_obj = {
        "H1": iface.get("H1", ""),
        "H2": iface.get("H2", ""),
        "H3": iface.get("H3", ""),
        "H4": iface.get("H4", ""),
        "I1": iface.get("I1", ""),
        "I2": iface.get("I2", ""),
        "I3": iface.get("I3", ""),
        "I4": iface.get("I4", ""),
        "I5": iface.get("I5", ""),
        "Jc": iface.get("Jc", ""),
        "Jmax": iface.get("Jmax", ""),
        "Jmin": iface.get("Jmin", ""),
        "S1": iface.get("S1", ""),
        "S2": iface.get("S2", ""),
        "S3": iface.get("S3", ""),
        "S4": iface.get("S4", ""),
        "allowed_ips": allowed,
        "clientId": iface.get("PublicKey", ""),
        "client_ip": client_ip,
        "client_priv_key": iface.get("PrivateKey", ""),
        "client_pub_key": iface.get("PublicKey", ""),
        "config": conf_text,
        "hostName": endpoint_host,
        "mtu": iface.get("MTU", "1280"),
        "persistent_keep_alive": peer.get("PersistentKeepalive", "25"),
        "port": int(endpoint_port),
        "psk_key": peer.get("PresharedKey", ""),
        "server_pub_key": peer.get("PublicKey", ""),
    }

    out = tpl
    out["hostName"] = endpoint_host
    out["description"] = description
    out["dns1"] = dns1
    out["dns2"] = dns2
    out["defaultContainer"] = container_name
    out["containers"][0]["container"] = container_name
    out["containers"][0]["awg"]["port"] = str(awg_obj["port"])
    out["containers"][0]["awg"]["transport_proto"] = "udp"
    out["containers"][0]["awg"]["protocol_version"] = "2"
    out["containers"][0]["awg"]["subnet_address"] = subnet_address

    for key in ["H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5", "Jc", "Jmax", "Jmin", "S1", "S2", "S3", "S4"]:
        out["containers"][0]["awg"][key] = str(awg_obj[key])

    out["containers"][0]["awg"]["last_config"] = json.dumps(
        awg_obj,
        ensure_ascii=False,
        indent=4,
    )

    Path(out_json_path).write_text(json.dumps(out, ensure_ascii=False, indent=4), encoding="utf-8")

    res = subprocess.run(
        ["python3", decoder_py, "-i", out_json_path],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print("=== amnezia-config-decoder stderr ===")
        print(res.stderr.strip())
        print("=== amnezia-config-decoder stdout ===")
        print(res.stdout.strip())
        raise SystemExit(res.returncode)

    print(res.stdout.strip())


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: conf2vpn.py <conf> <template.json> <out.json> <amnezia-config-decoder.py> [container_name] [description]")
        sys.exit(1)
    container_name = sys.argv[5] if len(sys.argv) >= 6 else "amnezia-awg"
    description = sys.argv[6] if len(sys.argv) >= 7 else "awg"
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], container_name, description)
"""


AMNEZIA_CONFIG_DECODER_PY = """import collections
import argparse
import base64
import json
import zlib

def encode_config(config):
    json_str = json.dumps(config, indent=4).encode()
    compressed_data = zlib.compress(json_str)
    original_data_len = len(json_str)
    header = original_data_len.to_bytes(4, byteorder='big')
    encoded_data = base64.urlsafe_b64encode(header + compressed_data).decode().rstrip("=")
    return f"vpn://{encoded_data}"

def decode_config(encoded_string):
    encoded_data = encoded_string.replace("vpn://", "")
    padding = 4 - (len(encoded_data) % 4)
    encoded_data += "=" * padding
    compressed_data = base64.urlsafe_b64decode(encoded_data)
    try:
        original_data_len = int.from_bytes(compressed_data[:4], byteorder='big')
        decompressed_data = zlib.decompress(compressed_data[4:])
        if len(decompressed_data) != original_data_len:
            raise ValueError("Invalid length of decompressed data")
        return json.loads(decompressed_data, object_pairs_hook=collections.OrderedDict)
    except zlib.error:
        return json.loads(compressed_data.decode(), object_pairs_hook=collections.OrderedDict)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('encoded_string', metavar='vpn://...', type=str, nargs='?')
    parser.add_argument('-i', '--input', metavar='input.json', type=str)
    parser.add_argument('-o', '--output', metavar='output.json', type=str)
    args = parser.parse_args()
    if args.input and args.encoded_string:
        parser.print_help()
        print("\\nError: Cannot specify both Base64 string and JSON file simultaneously.")
    elif args.input:
        with open(args.input, 'r') as f:
            config = json.load(f)
            encoded_string = encode_config(config)
            print(encoded_string)
    elif args.encoded_string:
        config = decode_config(args.encoded_string)
        if args.output:
            with open(args.output, 'w') as f:
                json.dump(config, f, indent=4)
            print(f"Configuration saved to {args.output}")
        else:
            print(json.dumps(config, indent=4))
    else:
        parser.print_help()
"""


AWG_ADD_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

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

CONTAINER="${AWG_CONTAINER_NAME:-amnezia-awg}"
IFACE="${AWG_IFACE:-wg0}"
CFG="${AWG_CONFIG:-/opt/node-plane-runtime/amnezia-awg/data/wg0.conf}"
SERVER_IP="${AWG_SERVER_IP:-}"
SERVER_PORT="${AWG_SERVER_PORT:-51820}"
CLIENT_DNS="${AWG_DNS:-1.1.1.1}"
CLIENT_MTU="${AWG_MTU:-1280}"
ALLOWED_IPS="${AWG_ALLOWED_IPS:-0.0.0.0/0}"
KEEPALIVE="${AWG_KEEPALIVE:-25}"
I1_PRESET="${AWG_I1_PRESET:-quic}"
CONF2VPN="${AWG_CONF2VPN:-/opt/node-plane-runtime/conf2vpn.py}"
AWG_TEMPLATE="${AWG_TEMPLATE:-/opt/node-plane-runtime/awg-template.json}"
AMNEZIA_DECODER="${AWG_DECODER:-/opt/node-plane-runtime/amnezia-config-decoder.py}"
SERVER_KEY="${SERVER_KEY:-}"
NAME="${1:-}"

if [[ -z "$NAME" ]]; then
  read -rp "Введите имя пользователя: " NAME
fi
if [[ -z "$NAME" ]]; then
  echo "Имя не может быть пустым" >&2
  exit 1
fi
if [[ ! -f "$CFG" ]]; then
  echo "AWG config not found: $CFG" >&2
  echo "Prepare $CFG first or sync the existing config into the mounted data dir." >&2
  exit 1
fi
if [[ -z "$SERVER_IP" ]]; then
  echo "AWG_SERVER_IP is not configured in /etc/node-plane/node.env" >&2
  exit 1
fi
DISPLAY_NAME="$NAME"
if [[ -n "$SERVER_KEY" ]]; then
  DISPLAY_NAME="${SERVER_KEY}-${NAME}"
fi
if ! docker_cmd ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  echo "Container ${CONTAINER} not running" >&2
  exit 1
fi

eval "$(
  CFG_ENV="$CFG" python3 - <<'PY'
import os
import shlex

cfg_path = os.environ["CFG_ENV"]
values = {
    "JC": "",
    "JMIN": "",
    "JMAX": "",
    "S1": "",
    "S2": "",
    "S3": "",
    "S4": "",
    "H1": "",
    "H2": "",
    "H3": "",
    "H4": "",
    "I1": "",
    "I2": "",
    "I3": "",
    "I4": "",
    "I5": "",
}
mapping = {
    "Jc": "JC",
    "Jmin": "JMIN",
    "Jmax": "JMAX",
    "S1": "S1",
    "S2": "S2",
    "S3": "S3",
    "S4": "S4",
    "H1": "H1",
    "H2": "H2",
    "H3": "H3",
    "H4": "H4",
    "I1": "I1",
    "I2": "I2",
    "I3": "I3",
    "I4": "I4",
    "I5": "I5",
}

with open(cfg_path, "r", encoding="utf-8", errors="ignore") as fh:
    for raw in fh:
        line = raw.strip()
        if " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        env_key = mapping.get(key)
        if env_key:
            values[env_key] = value.strip()

for key, value in values.items():
    print(f"{key}={shlex.quote(value)}")
PY
)"

USED_IPS="$(
  docker_cmd exec -i "$CONTAINER" sh -lc \
  "wg show $IFACE allowed-ips | awk '{print \\$NF}' | cut -d/ -f1" | tr -d '\\r'
)"

FREE_IP=""
for i in $(seq 1 254); do
  ip="10.8.1.$i"
  if ! grep -qx "$ip" <<< "$USED_IPS"; then
    FREE_IP="$ip"
    break
  fi
done
if [[ -z "$FREE_IP" ]]; then
  echo "Нет свободных IP" >&2
  exit 1
fi

read -r CLIENT_PRIV CLIENT_PUB CLIENT_PSK < <(
  docker_cmd exec -i "$CONTAINER" sh -lc '
    umask 077
    priv=$(wg genkey)
    pub=$(printf "%s" "$priv" | wg pubkey)
    psk=$(wg genpsk)
    echo "$priv $pub $psk"
  ' | tr -d '\\r'
)

SERVER_PUB="$(docker_cmd exec -i "$CONTAINER" sh -lc "wg show $IFACE public-key" | tr -d '\\r')"

docker_cmd exec -i "$CONTAINER" sh -lc "
  tmp=\\$(mktemp)
  echo '$CLIENT_PSK' > \\$tmp
  wg set $IFACE peer '$CLIENT_PUB' preshared-key \\$tmp allowed-ips '$FREE_IP/32'
  rm -f \\$tmp
"

printf '\n# %s\n[Peer]\nPublicKey = %s\nPresharedKey = %s\nAllowedIPs = %s/32\n' \
  "$DISPLAY_NAME" "$CLIENT_PUB" "$CLIENT_PSK" "$FREE_IP" >> "$CFG"

TMP_CONF="$(mktemp /tmp/awg-client-XXXX.conf)"
TMP_JSON="$(mktemp /tmp/awg-amnezia-XXXX.json)"

cat > "$TMP_CONF" <<EOF
[Interface]
PrivateKey = $CLIENT_PRIV
PublicKey = $CLIENT_PUB
Address = $FREE_IP/32
DNS = $CLIENT_DNS
MTU = $CLIENT_MTU

Jc = $JC
Jmin = $JMIN
Jmax = $JMAX
S1 = $S1
S2 = $S2
S3 = $S3
S4 = $S4
H1 = $H1
H2 = $H2
H3 = $H3
H4 = $H4
I1 = $I1
I2 = $I2
I3 = $I3
I4 = $I4
I5 = $I5

[Peer]
PublicKey = $SERVER_PUB
PresharedKey = $CLIENT_PSK
Endpoint = $SERVER_IP:$SERVER_PORT
AllowedIPs = $ALLOWED_IPS
PersistentKeepalive = $KEEPALIVE
EOF

cat "$TMP_CONF"

if [[ -f "$CONF2VPN" && -f "$AWG_TEMPLATE" && -f "$AMNEZIA_DECODER" ]]; then
  echo
  echo "=========== AMNEZIA TEXT KEY (vpn://) ==========="
  python3 "$CONF2VPN" \
    "$TMP_CONF" \
    "$AWG_TEMPLATE" \
    "$TMP_JSON" \
    "$AMNEZIA_DECODER" \
    "$CONTAINER" \
    "$DISPLAY_NAME"
  echo "================================================="
fi

rm -f "$TMP_CONF" "$TMP_JSON"
"""


AWG_DEL_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

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

CONTAINER="${AWG_CONTAINER_NAME:-amnezia-awg}"
CFG="${AWG_CONFIG:-/opt/node-plane-runtime/amnezia-awg/data/wg0.conf}"
NAME="${1:-}"
if [[ -z "$NAME" ]]; then
  echo "Usage: $0 <name>" >&2
  exit 1
fi
if ! docker_cmd ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  echo "Container ${CONTAINER} not found" >&2
  exit 1
fi
tmp="$(mktemp)"
awk -v name="$NAME" '
  {
    if ($0 == "# " name) {skip=1; next}
    if (skip && NF==0) {skip=0; next}
    if (skip) next
    print
  }
' "$CFG" > "$tmp"
cp -a "$CFG" "${CFG}.bak.$(date +%Y%m%d-%H%M%S)"
mv "$tmp" "$CFG"
docker_cmd restart "$CONTAINER" >/dev/null
echo "OK"
"""


AWG_INIT_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

CFG="${AWG_CONFIG:-/opt/node-plane-runtime/amnezia-awg/data/wg0.conf}"
IFACE="${AWG_IFACE:-wg0}"
SERVER_ADDR="${AWG_SERVER_ADDRESS:-10.8.1.0/24}"
PORT="${AWG_SERVER_PORT:-51820}"
I1_PRESET="${AWG_I1_PRESET:-quic}"

mkdir -p "$(dirname "$CFG")"
if [[ -s "$CFG" ]]; then
  echo "AWG config already exists: $CFG"
  exit 0
fi

PUB_IFACE="$(ip route get 1.1.1.1 | awk '/dev/ {for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}')"
if [[ -z "$PUB_IFACE" ]]; then
  echo "Could not detect public interface" >&2
  exit 1
fi

SERVER_PRIV="$(wg genkey)"
SERVER_PUB="$(printf '%s' "$SERVER_PRIV" | wg pubkey)"

eval "$(
I1_PRESET_ENV="$I1_PRESET" python3 - <<'PY'
import os
import random
import secrets
import shlex

preset = os.environ.get("I1_PRESET_ENV", "").strip().lower()

# AmneziaWG 2.0 docs:
# - Jc: 0..10
# - Jmin/Jmax: 64..1024 and Jmin < Jmax
# - S1/S2/S3: 0..64, S4: 0..32
# - S1 + 56 != S2
# - H1-H4 ranges must not overlap

jc = random.randint(3, 7)
jmin = random.randint(64, 160)
jmax = random.randint(max(jmin + 32, 192), min(jmin + 320, 1024))
s1 = random.randint(0, 64)
s2 = random.randint(0, 64)
while s1 + 56 == s2:
    s2 = random.randint(0, 64)
s3 = random.randint(0, 64)
s4 = random.randint(0, 32)

segments = []
cursor = random.randint(100_000_000, 300_000_000)
for _ in range(4):
    length = random.randint(10_000_000, 120_000_000)
    start = cursor
    end = start + length
    if end > 4_294_967_295:
        raise RuntimeError("Generated H-range exceeds uint32")
    segments.append(f"{start}-{end}")
    cursor = end + random.randint(5_000_000, 80_000_000)

def gen_i_payload() -> str:
    random_prefix = random.randint(0, 3)
    fixed_len = random.randint(12, 48)
    parts = []
    if random_prefix:
        parts.append(f"<r {random_prefix}>")
    parts.append(f"<b 0x{secrets.token_hex(fixed_len)}>")
    if random.random() < 0.35:
        parts.append(f"<r {random.randint(1, 2)}>")
    return "".join(parts)

def gen_i1_payload(kind: str) -> str:
    if kind == "dns":
        return "<rc 2><b 0x01000001000000000000><r 32>"
    if kind == "chaos":
        return f"<b 0x{secrets.token_hex(4)}><rc 4><r {random.randint(500, 1000)}>"
    return "<b 0xc000000001><rc 8><r 1000>"

def preset_values(kind: str):
    if kind == "dns":
        return {
            "I1": gen_i1_payload(kind),
            "I2": "<rc 2><b 0x01000001000000000000><r 64>",
            "I3": "<r 48>",
            "I4": "<r 80>",
            "I5": "<r 40>",
        }
    if kind == "chaos":
        return {
            "I1": gen_i1_payload(kind),
            "I2": f"<r {random.randint(100, 1400)}>",
            "I3": f"<r {random.randint(100, 1400)}>",
            "I4": f"<r {random.randint(100, 1400)}>",
            "I5": f"<r {random.randint(100, 1400)}>",
        }
    return {
        "I1": gen_i1_payload(kind),
        "I2": "<b 0x40><rc 4><r 100>",
        "I3": "<r 1200>",
        "I4": "<r 100>",
        "I5": "<r 1200>",
    }

values = {
    "JC": str(jc),
    "JMIN": str(jmin),
    "JMAX": str(jmax),
    "S1": str(s1),
    "S2": str(s2),
    "S3": str(s3),
    "S4": str(s4),
    "H1": segments[0],
    "H2": segments[1],
    "H3": segments[2],
    "H4": segments[3],
}
values.update(preset_values(preset))

for key, value in values.items():
    print(f"{key}={shlex.quote(value)}")
PY
)"

cat > "$CFG" <<EOF
[Interface]
PrivateKey = $SERVER_PRIV
Address = $SERVER_ADDR
ListenPort = $PORT

Jc = $JC
Jmin = $JMIN
Jmax = $JMAX
S1 = $S1
S2 = $S2
S3 = $S3
S4 = $S4
H1 = $H1
H2 = $H2
H3 = $H3
H4 = $H4
I1 = $I1
I2 = $I2
I3 = $I3
I4 = $I4
I5 = $I5
EOF

chmod 600 "$CFG"
echo "AWG config initialized: $CFG"
echo "Server public key: $SERVER_PUB"
"""


AWG_REGENERATE_ENTROPY_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

CFG="${AWG_CONFIG:-/opt/node-plane-runtime/amnezia-awg/data/wg0.conf}"
PRESET="${AWG_I1_PRESET:-quic}"
CONTAINER="${AWG_CONTAINER_NAME:-amnezia-awg}"

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

if [[ ! -f "$CFG" ]]; then
  echo "AWG config not found: $CFG" >&2
  exit 1
fi

TMP="$(mktemp)"
PRESET_ENV="$PRESET" CFG_ENV="$CFG" TMP_ENV="$TMP" python3 - <<'PY'
import os
import random
import re
import secrets

preset = os.environ.get("PRESET_ENV", "").strip().lower()
cfg_path = os.environ["CFG_ENV"]
tmp_path = os.environ["TMP_ENV"]

jc = random.randint(3, 7)
jmin = random.randint(64, 160)
jmax = random.randint(max(jmin + 32, 192), min(jmin + 320, 1024))
s1 = random.randint(0, 64)
s2 = random.randint(0, 64)
while s1 + 56 == s2:
    s2 = random.randint(0, 64)
s3 = random.randint(0, 64)
s4 = random.randint(0, 32)

segments = []
cursor = random.randint(100_000_000, 300_000_000)
for _ in range(4):
    length = random.randint(10_000_000, 120_000_000)
    start = cursor
    end = start + length
    if end > 4_294_967_295:
        raise RuntimeError("Generated H-range exceeds uint32")
    segments.append(f"{start}-{end}")
    cursor = end + random.randint(5_000_000, 80_000_000)

def gen_i_payload() -> str:
    random_prefix = random.randint(0, 3)
    fixed_len = random.randint(12, 48)
    parts = []
    if random_prefix:
        parts.append(f"<r {random_prefix}>")
    parts.append(f"<b 0x{secrets.token_hex(fixed_len)}>")
    if random.random() < 0.35:
        parts.append(f"<r {random.randint(1, 2)}>")
    return "".join(parts)

def gen_i1_payload(kind: str) -> str:
    if kind == "dns":
        return "<rc 2><b 0x01000001000000000000><r 32>"
    if kind == "chaos":
        return f"<b 0x{secrets.token_hex(4)}><rc 4><r {random.randint(500, 1000)}>"
    return "<b 0xc000000001><rc 8><r 1000>"

def preset_values(kind: str):
    if kind == "dns":
        return {
            "I1": gen_i1_payload(kind),
            "I2": "<rc 2><b 0x01000001000000000000><r 64>",
            "I3": "<r 48>",
            "I4": "<r 80>",
            "I5": "<r 40>",
        }
    if kind == "chaos":
        return {
            "I1": gen_i1_payload(kind),
            "I2": f"<r {random.randint(100, 1400)}>",
            "I3": f"<r {random.randint(100, 1400)}>",
            "I4": f"<r {random.randint(100, 1400)}>",
            "I5": f"<r {random.randint(100, 1400)}>",
        }
    return {
        "I1": gen_i1_payload(kind),
        "I2": "<b 0x40><rc 4><r 100>",
        "I3": "<r 1200>",
        "I4": "<r 100>",
        "I5": "<r 1200>",
    }

values = {
    "Jc": str(jc),
    "Jmin": str(jmin),
    "Jmax": str(jmax),
    "S1": str(s1),
    "S2": str(s2),
    "S3": str(s3),
    "S4": str(s4),
    "H1": segments[0],
    "H2": segments[1],
    "H3": segments[2],
    "H4": segments[3],
}
values.update(preset_values(preset))

text = open(cfg_path, "r", encoding="utf-8", errors="ignore").read()
for key, value in values.items():
    pattern = rf"(?m)^#?\\s*{re.escape(key)} =.*$"
    replacement = f"{key} = {value}"
    if re.search(pattern, text):
        text = re.sub(pattern, replacement, text, count=1)
    else:
        text = re.sub(r"(?m)^(H4 = .*)$", r"\\1\\n" + replacement, text, count=1)

with open(tmp_path, "w", encoding="utf-8") as fh:
    fh.write(text)
PY

python3 -m json.tool /dev/null >/dev/null 2>&1 || true
cp -a "$CFG" "${CFG}.bak.$(date +%Y%m%d-%H%M%S)"
mv "$TMP" "$CFG"
chmod 600 "$CFG"
docker_cmd restart "$CONTAINER" >/dev/null 2>&1 || true
/opt/node-plane-runtime/show-awg-entropy.sh
echo
echo "WARNING: client AWG configs must be reissued after entropy regeneration."
"""


AWG_START_SCRIPT = """#!/bin/sh
set -eu

IFACE="${AWG_IFACE:-wg0}"
CFG="${AWG_CONFIG_FILE:-/opt/amnezia/awg/wg0.conf}"
NETWORK="${AWG_NETWORK:-10.8.1.0/24}"
GO_IMPL="${WG_QUICK_USERSPACE_IMPLEMENTATION:-amneziawg-go}"
GO_PID=""
PUB_IFACE="$(ip route get 1.1.1.1 2>/dev/null | awk '/dev/ {for (i=1;i<=NF;i++) if ($i==\"dev\") {print $(i+1); exit}}' || true)"

echo "AWG runtime starting: iface=$IFACE cfg=$CFG network=$NETWORK"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required command not found: $1" >&2
    exit 1
  }
}

conf_value() {
  key="$1"
  awk -F' = ' -v key="$key" '$1 == key {print $2; exit}' "$CFG"
}

strip_conf() {
  awk '
    function keep_interface(key) {
      return key == "PrivateKey" || key == "ListenPort" || key == "FwMark" || key == "Jc" || key == "Jmin" || key == "Jmax" || key == "S1" || key == "S2" || key == "S3" || key == "S4" || key == "H1" || key == "H2" || key == "H3" || key == "H4" || key == "I1" || key == "I2" || key == "I3" || key == "I4" || key == "I5"
    }
    function keep_peer(key) {
      return key == "PublicKey" || key == "PresharedKey" || key == "AllowedIPs" || key == "Endpoint" || key == "PersistentKeepalive"
    }
    {
      line=$0
      gsub(/\r$/, "", line)
      trimmed=line
      sub(/^[ \t]+/, "", trimmed)
      sub(/[ \t]+$/, "", trimmed)
      if (trimmed == "" || trimmed ~ /^#/) next
      if (trimmed == "[Interface]") {
        section="interface"
        print "[Interface]"
        next
      }
      if (trimmed == "[Peer]") {
        section="peer"
        print ""
        print "[Peer]"
        next
      }
      if (index(trimmed, " = ") == 0 || section == "") next
      split(trimmed, parts, " = ")
      key=parts[1]
      if ((section == "interface" && keep_interface(key)) || (section == "peer" && keep_peer(key))) {
        print trimmed
      }
    }
  ' "$CFG"
}

setup_nat() {
  if [ -n "$PUB_IFACE" ]; then
    iptables -C FORWARD -i "$IFACE" -j ACCEPT >/dev/null 2>&1 || iptables -A FORWARD -i "$IFACE" -j ACCEPT
    iptables -C FORWARD -o "$IFACE" -j ACCEPT >/dev/null 2>&1 || iptables -A FORWARD -o "$IFACE" -j ACCEPT
    iptables -t nat -C POSTROUTING -s "$NETWORK" -o "$PUB_IFACE" -j MASQUERADE >/dev/null 2>&1 || \
      iptables -t nat -A POSTROUTING -s "$NETWORK" -o "$PUB_IFACE" -j MASQUERADE
  fi
}

cleanup_nat() {
  if [ -n "$PUB_IFACE" ]; then
    iptables -D FORWARD -i "$IFACE" -j ACCEPT >/dev/null 2>&1 || true
    iptables -D FORWARD -o "$IFACE" -j ACCEPT >/dev/null 2>&1 || true
    iptables -t nat -D POSTROUTING -s "$NETWORK" -o "$PUB_IFACE" -j MASQUERADE >/dev/null 2>&1 || true
  fi
}

cleanup() {
  cleanup_nat
  ip link del "$IFACE" >/dev/null 2>&1 || true
  if [ -n "$GO_PID" ]; then
    kill "$GO_PID" >/dev/null 2>&1 || true
    wait "$GO_PID" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

if [ ! -f "$CFG" ]; then
  echo "Config not found: $CFG"
  exec sh -c 'while :; do sleep 3600; done'
  exit 0
fi

require_cmd "$GO_IMPL"
require_cmd wg
require_cmd ip
require_cmd awk

ADDR="$(conf_value "Address")"
MTU="$(conf_value "MTU")"
[ -n "$MTU" ] || MTU="1280"

sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true
ip link del "$IFACE" >/dev/null 2>&1 || true

"$GO_IMPL" "$IFACE" &
GO_PID="$!"

for _ in $(seq 1 50); do
  if ip link show "$IFACE" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

if ! ip link show "$IFACE" >/dev/null 2>&1; then
  echo "Userspace AWG interface did not appear: $IFACE" >&2
  exit 1
fi

strip_conf | wg setconf "$IFACE" /dev/stdin

if [ -n "$ADDR" ]; then
  ip address add "$ADDR" dev "$IFACE"
fi
ip link set mtu "$MTU" up dev "$IFACE"

setup_nat

echo "AWG runtime ready: iface=$IFACE pub_iface=${PUB_IFACE:-none}"

exec sh -c 'while :; do sleep 3600; done'
"""


AWG_DOCKERFILE = """FROM amneziavpn/amneziawg-go:0.2.16

LABEL maintainer="AmneziaVPN"

RUN mkdir -p /opt/amnezia
COPY start.sh /opt/amnezia/start.sh
RUN chmod a+x /opt/amnezia/start.sh

RUN echo -e " \\n\\
  fs.file-max = 51200 \\n\\
  \\n\\
  net.core.rmem_max = 67108864 \\n\\
  net.core.wmem_max = 67108864 \\n\\
  net.core.netdev_max_backlog = 250000 \\n\\
  net.core.somaxconn = 4096 \\n\\
  \\n\\
  net.ipv4.tcp_syncookies = 1 \\n\\
  net.ipv4.tcp_tw_reuse = 1 \\n\\
  net.ipv4.tcp_tw_recycle = 0 \\n\\
  net.ipv4.tcp_fin_timeout = 30 \\n\\
  net.ipv4.tcp_keepalive_time = 1200 \\n\\
  net.ipv4.ip_local_port_range = 10000 65000 \\n\\
  net.ipv4.tcp_max_syn_backlog = 8192 \\n\\
  net.ipv4.tcp_max_tw_buckets = 5000 \\n\\
  net.ipv4.tcp_fastopen = 3 \\n\\
  net.ipv4.tcp_mem = 25600 51200 102400 \\n\\
  net.ipv4.tcp_rmem = 4096 87380 67108864 \\n\\
  net.ipv4.tcp_wmem = 4096 65536 67108864 \\n\\
  net.ipv4.tcp_mtu_probing = 1 \\n\\
  net.ipv4.tcp_congestion_control = hybla \\n\\
  # net.ipv4.tcp_congestion_control = cubic \\n\\
  " | sed -e 's/^\\s\\+//g' | tee -a /etc/sysctl.conf && \\
  mkdir -p /etc/security && \\
  echo -e " \\n\\
  * soft nofile 51200 \\n\\
  * hard nofile 51200 \\n\\
  " | sed -e 's/^\\s\\+//g' | tee -a /etc/security/limits.conf

ENTRYPOINT [ "/bin/sh", "/opt/amnezia/start.sh" ]
CMD [ "" ]
"""


AWG_DEPLOY_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return
  fi
  if command -v sudo >/dev/null 2>&1; then
    sudo systemctl enable --now docker >/dev/null 2>&1 || sudo service docker start >/dev/null 2>&1 || true
    if sudo docker info >/dev/null 2>&1; then
      sudo docker "$@"
      return
    fi
  fi
  systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return
  fi
  echo "Docker daemon is unavailable for this session." >&2
  id >&2 || true
  groups >&2 || true
  ls -l /var/run/docker.sock >&2 || true
  exit 1
}

DOCKER_DIR="${AWG_DOCKER_DIR:-/opt/node-plane-runtime/amnezia-awg}"
IMAGE="${AWG_DOCKER_IMAGE:-node-plane-amnezia-awg:0.2.16}"
CONTAINER="${AWG_CONTAINER_NAME:-amnezia-awg}"
CFG="${AWG_CONFIG:-/opt/node-plane-runtime/amnezia-awg/data/wg0.conf}"
IFACE="${AWG_IFACE:-wg0}"
PORT="${AWG_SERVER_PORT:-51820}"

if [[ ! -c /dev/net/tun ]]; then
  echo "/dev/net/tun is not available on this host. AWG userspace runtime cannot start." >&2
  exit 1
fi
if lsmod | grep -q '^amneziawg '; then
  echo "Host kernel module amneziawg is loaded. Refusing to start AWG userspace runtime on a dirty host." >&2
  exit 1
fi

mkdir -p "$DOCKER_DIR/data"
BUILD_LOG="$(mktemp)"
if ! docker_cmd build --no-cache -t "$IMAGE" "$DOCKER_DIR" >"$BUILD_LOG" 2>&1; then
  echo "AWG image build failed." >&2
  tail -n 120 "$BUILD_LOG" >&2 || true
  rm -f "$BUILD_LOG"
  exit 1
fi
echo "AWG image built: $IMAGE"
rm -f "$BUILD_LOG"

if ! docker_cmd run --rm --entrypoint /bin/sh "$IMAGE" -n /opt/amnezia/start.sh >/tmp/awg-syntax.out 2>/tmp/awg-syntax.err; then
  echo "AWG image syntax check failed." >&2
  cat /tmp/awg-syntax.out >&2 || true
  cat /tmp/awg-syntax.err >&2 || true
  exit 1
fi

if docker_cmd ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  docker_cmd rm -f "$CONTAINER" >/dev/null
fi

docker_cmd run -d \
  --name "$CONTAINER" \
  --restart no \
  --cap-add NET_ADMIN \
  --device /dev/net/tun:/dev/net/tun \
  --sysctl net.ipv4.ip_forward=1 \
  -e AWG_IFACE="$IFACE" \
  -e AWG_CONFIG_FILE="/opt/amnezia/awg/$(basename "$CFG")" \
  -e AWG_NETWORK="${AWG_NETWORK:-10.8.1.0/24}" \
  -e AWG_LISTEN_PORT="$PORT" \
  -p "$PORT:$PORT/udp" \
  -v "$DOCKER_DIR/data:/opt/amnezia/awg" \
  "$IMAGE" >/dev/null

sleep 3
STATE="$(docker_cmd inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo unknown)"
if [[ "$STATE" != "running" ]]; then
  echo "AWG container failed to start." >&2
  echo "state: $STATE" >&2
  docker_cmd inspect -f 'exit_code={{.State.ExitCode}} error={{.State.Error}} oom_killed={{.State.OOMKilled}} started_at={{.State.StartedAt}} finished_at={{.State.FinishedAt}}' "$CONTAINER" >&2 || true
  echo "host_arch: $(uname -m 2>/dev/null || echo unknown)" >&2
  docker_cmd image inspect "$IMAGE" -f 'image_os={{.Os}} image_arch={{.Architecture}}' >&2 || true
  docker_cmd logs "$CONTAINER" >&2 || true
  echo "manual_run_output:" >&2
  docker_cmd run --rm \
    --entrypoint /bin/sh \
    --cap-add NET_ADMIN \
    --device /dev/net/tun:/dev/net/tun \
    --sysctl net.ipv4.ip_forward=1 \
    -e AWG_IFACE="$IFACE" \
    -e AWG_CONFIG_FILE="/opt/amnezia/awg/$(basename "$CFG")" \
    -e AWG_NETWORK="${AWG_NETWORK:-10.8.1.0/24}" \
    -e AWG_LISTEN_PORT="$PORT" \
    -v "$DOCKER_DIR/data:/opt/amnezia/awg" \
    "$IMAGE" -c '
      echo "shell: $(command -v sh 2>/dev/null || echo missing)"
      ls -l /opt/amnezia/start.sh || true
      wc -c /opt/amnezia/start.sh || true
      echo "--- start.sh head ---"
      sed -n "1,120p" /opt/amnezia/start.sh || true
      echo "--- run ---"
      /bin/sh -x /opt/amnezia/start.sh
    ' >&2 || true
  exit 1
fi

docker_cmd update --restart unless-stopped "$CONTAINER" >/dev/null 2>&1 || true
echo "AWG container deployed: $CONTAINER"
"""


def _packages_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt_wait() {
  local timeout="${1:-300}"
  local elapsed=0
  while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
    || fuser /var/lib/dpkg/lock >/dev/null 2>&1 \
    || fuser /var/lib/apt/lists/lock >/dev/null 2>&1 \
    || fuser /var/cache/apt/archives/lock >/dev/null 2>&1; do
    if (( elapsed >= timeout )); then
      echo "Timed out waiting for apt/dpkg lock." >&2
      return 1
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
}

apt_run() {
  apt_wait
  apt-get "$@"
}

if command -v apt-get >/dev/null 2>&1; then
  apt_run update
  apt_run install -y ca-certificates curl jq qrencode wireguard-tools python3 iproute2 iptables
  if ! command -v docker >/dev/null 2>&1; then
    apt_run install -y docker.io
    apt-cache show docker-compose-plugin >/dev/null 2>&1 && apt_run install -y docker-compose-plugin || true
  fi
fi
systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true
mkdir -p /etc/node-plane /opt/node-plane-runtime /opt/node-plane-runtime/xray /opt/node-plane-runtime/amnezia-awg/data
touch /etc/node-plane/node.env
"""


def _install_docker_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt_wait() {
  local timeout="${1:-300}"
  local elapsed=0
  while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
    || fuser /var/lib/dpkg/lock >/dev/null 2>&1 \
    || fuser /var/lib/apt/lists/lock >/dev/null 2>&1 \
    || fuser /var/cache/apt/archives/lock >/dev/null 2>&1; do
    if (( elapsed >= timeout )); then
      echo "Timed out waiting for apt/dpkg lock." >&2
      return 1
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
}

apt_run() {
  apt_wait
  apt-get "$@"
}

if ! command -v apt-get >/dev/null 2>&1; then
  echo "На сервере нет apt-get. Автоустановка Docker поддерживается только для Debian/Ubuntu."
  exit 1
fi

echo "Обновляю индекс пакетов..."
apt_run update

echo "Устанавливаю Docker..."
apt_run install -y docker.io
apt-cache show docker-compose-plugin >/dev/null 2>&1 && apt_run install -y docker-compose-plugin || true

echo "Запускаю Docker..."
systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "Docker установлен и доступен."
  exit 0
fi

if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
  echo "Docker установлен и доступен через sudo."
  exit 0
fi

echo "Docker установлен, но всё ещё недоступен для текущего пользователя."
exit 1
"""


def _single_line_note(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parts = [part.strip() for part in raw.replace("\r", "\n").split("\n") if part.strip()]
    return " | ".join(parts)[:1500]


def _mark(server: RegisteredServer, state: str, notes: str = "") -> None:
    update_server_fields(server.key, bootstrap_state=state, notes=_single_line_note(notes))


def _remote_file_exists(server: RegisteredServer, path: str) -> bool:
    rc, _ = run_server_command(server, f"test -f {shlex.quote(path)}", timeout=30)
    return rc == 0


def _port_label(field: str) -> str:
    labels = {
        "xray_tcp_port": "Xray TCP",
        "xray_xhttp_port": "Xray XHTTP",
        "awg_port": "AWG",
    }
    return labels.get(field, field)


def _format_port_status_summary(text: str) -> str:
    port_rows: dict[str, dict[str, str]] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("PORT_STATUS|"):
            _, field, proto, port, status, suggestion = (line.split("|", 5) + [""])[:6]
            row = port_rows.setdefault(field, {})
            row.update({"proto": proto, "port": port, "port_status": status, "port_suggestion": suggestion.strip()})
        elif line.startswith("FIREWALL_STATUS|"):
            _, field, proto, port, status, suggestion = (line.split("|", 5) + [""])[:6]
            row = port_rows.setdefault(field, {})
            row.update({"proto": proto, "port": port, "firewall_status": status, "firewall_suggestion": suggestion.strip()})

    if not port_rows:
        return (text or "").strip()

    lines = ["Сводка по портам:"]
    for field in ("xray_tcp_port", "xray_xhttp_port", "awg_port"):
        row = port_rows.get(field)
        if not row:
            continue
        proto = row.get("proto", "?")
        port = row.get("port", "?")
        port_status = row.get("port_status", "unknown")
        firewall_status = row.get("firewall_status", "unknown")

        port_text_map = {
            "managed": "используется управляемым рантаймом",
            "free": "свободен",
            "busy": "занят",
        }
        firewall_text_map = {
            "open": "открыт в firewall",
            "closed": "закрыт в firewall",
        }
        line = f"- {_port_label(field)} {port}/{proto}: {port_text_map.get(port_status, port_status)}, {firewall_text_map.get(firewall_status, firewall_status)}"
        if port_status == "busy" and row.get("port_suggestion"):
            line += f" | рекомендуемый порт: {row['port_suggestion']}"
        if firewall_status == "closed" and row.get("firewall_suggestion"):
            line += f" | {row['firewall_suggestion']}"
        lines.append(line)
    return "\n".join(lines)


def _cleanup_server_runtime(server: RegisteredServer, preserve_config: bool) -> Tuple[int, str]:
    preserve = "1" if preserve_config else "0"
    script = f"""#!/usr/bin/env bash
set -euo pipefail

docker_cmd() {{
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker "$@"
    return 0
  fi
  if command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    sudo -n docker "$@"
    return 0
  fi
  return 1
}}

docker_rm() {{
  local name="$1"
  docker_cmd rm -f "$name" >/dev/null 2>&1 || true
}}

docker_rmi() {{
  local image="$1"
  if [[ -z "$image" ]]; then
    return
  fi
  docker_cmd rmi -f "$image" >/dev/null 2>&1 || true
}}

rm_path() {{
  local target="$1"
  if rm -rf "$target" >/dev/null 2>&1; then
    return
  fi
  if command -v sudo >/dev/null 2>&1; then
    sudo -n rm -rf "$target" >/dev/null 2>&1 || true
  fi
}}

XRAY_CONTAINER="xray"
AWG_CONTAINER="{AWG_RUNTIME_CONTAINER}"
XRAY_IMAGE_DEFAULT="ghcr.io/xtls/xray-core:25.12.8"
AWG_IMAGE_DEFAULT="node-plane-amnezia-awg:0.2.16"
if [[ -f /etc/node-plane/node.env ]]; then
  source /etc/node-plane/node.env
fi

docker_rm "${{XRAY_CONTAINER_NAME:-$XRAY_CONTAINER}}"
docker_rm "${{AWG_CONTAINER_NAME:-$AWG_CONTAINER}}"
docker_rmi "${{XRAY_DOCKER_IMAGE:-$XRAY_IMAGE_DEFAULT}}"
docker_rmi "${{AWG_DOCKER_IMAGE:-$AWG_IMAGE_DEFAULT}}"
docker_rmi "amneziavpn/amneziawg-go:0.2.16"
docker_cmd image prune -af >/dev/null 2>&1 || true

if [[ "{preserve}" != "1" ]]; then
  if rm -f /etc/node-plane/node.env >/dev/null 2>&1; then
    :
  elif command -v sudo >/dev/null 2>&1; then
    sudo -n rm -f /etc/node-plane/node.env >/dev/null 2>&1 || true
  fi
  rm_path /opt/node-plane-runtime
  echo "Управляемый рантайм удалён вместе с конфигами."
else
  echo "Управляемый рантайм удалён. Существующие конфиги сохранены."
fi

leftovers=()
docker_inspect() {{
  docker_cmd "$@" >/dev/null 2>&1
}}

if docker_inspect container inspect "${{XRAY_CONTAINER_NAME:-$XRAY_CONTAINER}}"; then
  leftovers+=("xray container still present")
fi
if docker_inspect container inspect "${{AWG_CONTAINER_NAME:-$AWG_CONTAINER}}"; then
  leftovers+=("awg container still present")
fi
if docker_inspect image inspect "${{XRAY_DOCKER_IMAGE:-$XRAY_IMAGE_DEFAULT}}"; then
  leftovers+=("xray image still present")
fi
if docker_inspect image inspect "${{AWG_DOCKER_IMAGE:-$AWG_IMAGE_DEFAULT}}"; then
  leftovers+=("awg image still present")
fi
if docker_inspect image inspect "amneziavpn/amneziawg-go:0.2.16"; then
  leftovers+=("amneziavpn/amneziawg-go:0.2.16 still present")
fi
if [[ "{preserve}" != "1" && -e /etc/node-plane/node.env ]]; then
  leftovers+=("/etc/node-plane/node.env still present")
fi
if [[ "{preserve}" != "1" && -e /opt/node-plane-runtime ]]; then
  leftovers+=("/opt/node-plane-runtime still present")
fi

if (( ${{#leftovers[@]}} > 0 )); then
  printf '%s\n' "${{leftovers[@]}}"
  exit 1
fi
"""
    return run_server_command(server, script, timeout=180)


def _remove_bot_ssh_key(server: RegisteredServer) -> Tuple[int, str]:
    ok, public_key = get_public_key()
    if not ok:
        return 1, public_key
    payload = base64.b64encode(public_key.encode("utf-8")).decode("ascii")
    script = f"""#!/usr/bin/env bash
set -euo pipefail

AUTH_KEYS="${{HOME}}/.ssh/authorized_keys"
if [[ ! -f "$AUTH_KEYS" ]]; then
  echo "authorized_keys not present"
  exit 0
fi

python3 - <<'PY'
import base64
from pathlib import Path

key = base64.b64decode({payload!r}).decode("utf-8").strip()
path = Path.home() / ".ssh" / "authorized_keys"
if not path.exists():
    print("authorized_keys not present")
    raise SystemExit(0)
lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
kept = [line for line in lines if line.strip() != key]
if len(kept) == len(lines):
    print("bot ssh key not present")
    raise SystemExit(0)
path.write_text(("\\n".join(kept) + ("\\n" if kept else "")), encoding="utf-8")
print("bot ssh key removed")
PY

chmod 600 "$AUTH_KEYS" >/dev/null 2>&1 || true
"""
    return run_server_command(server, script, timeout=60)


def render_server_node_env(server: RegisteredServer) -> str:
    lines = [
        shell_env_assignment("SERVER_KEY", server.key),
        shell_env_assignment("XRAY_CONFIG", server.xray_config_path),
        shell_env_assignment("XRAY_CONTAINER_NAME", server.xray_service_name),
        shell_env_assignment("XRAY_DOCKER_DIR", "/opt/node-plane-runtime/xray"),
        shell_env_assignment("XRAY_DOCKER_IMAGE", "ghcr.io/xtls/xray-core:25.12.8"),
        shell_env_assignment("XRAY_INBOUND_TCP_TAG", "reality-tcp"),
        shell_env_assignment("XRAY_INBOUND_XHTTP_TAG", "reality-xhttp"),
        shell_env_assignment("AWG_CONTAINER_NAME", AWG_RUNTIME_CONTAINER),
        shell_env_assignment("AWG_DOCKER_DIR", "/opt/node-plane-runtime/amnezia-awg"),
        shell_env_assignment("AWG_DOCKER_IMAGE", "node-plane-amnezia-awg:0.2.16"),
        shell_env_assignment("AWG_IFACE", server.awg_iface),
        shell_env_assignment("AWG_CONFIG", f"/opt/node-plane-runtime/amnezia-awg/data/{server.awg_iface}.conf"),
        shell_env_assignment("AWG_SERVER_ADDRESS", "10.8.1.0/24"),
        shell_env_assignment("AWG_NETWORK", "10.8.1.0/24"),
        shell_env_assignment("AWG_DNS", "1.1.1.1"),
        shell_env_assignment("AWG_MTU", "1280"),
        shell_env_assignment("AWG_ALLOWED_IPS", "0.0.0.0/0"),
        shell_env_assignment("AWG_KEEPALIVE", "25"),
        shell_env_assignment("AWG_I1_PRESET", server.awg_i1_preset),
        shell_env_assignment("AWG_SERVER_IP", server.awg_public_host or server.public_host),
        shell_env_assignment("AWG_SERVER_PORT", server.awg_port),
    ]
    return "\n".join(lines) + "\n"


def _runtime_files() -> Dict[str, Tuple[str, str]]:
    files = {
        "/etc/node-plane/node.env.example": (NODE_ENV_EXAMPLE, "0644"),
        "/opt/node-plane-runtime/init-xray.sh": (XRAY_INIT_SCRIPT, "0755"),
        "/opt/node-plane-runtime/sync-xray.sh": (XRAY_SYNC_SCRIPT, "0755"),
        "/opt/node-plane-runtime/deploy-xray.sh": (XRAY_DEPLOY_SCRIPT, "0755"),
        "/opt/node-plane-runtime/xray-enable-stats.sh": (XRAY_ENABLE_STATS_SCRIPT, "0755"),
        "/opt/node-plane-runtime/xray-add-user.sh": (XRAY_ADD_SCRIPT, "0755"),
        "/opt/node-plane-runtime/xray-add-user-existing.sh": (XRAY_ADD_EXISTING_SCRIPT, "0755"),
        "/opt/node-plane-runtime/xray-list-users.sh": (XRAY_LIST_SCRIPT, "0755"),
        "/opt/node-plane-runtime/xray-list-traffic.sh": (XRAY_TRAFFIC_SCRIPT, "0755"),
        "/opt/node-plane-runtime/xray-del-user.sh": (XRAY_DEL_SCRIPT, "0755"),
        "/opt/node-plane-runtime/conf2vpn.py": (AWG_CONF2VPN_PY, "0644"),
        "/opt/node-plane-runtime/amnezia-config-decoder.py": (AMNEZIA_CONFIG_DECODER_PY, "0644"),
        "/opt/node-plane-runtime/awg-template.json": (AWG_TEMPLATE_JSON, "0644"),
        "/opt/node-plane-runtime/awg-add-user.sh": (AWG_ADD_SCRIPT, "0755"),
        "/opt/node-plane-runtime/awg-del-user.sh": (AWG_DEL_SCRIPT, "0755"),
        "/opt/node-plane-runtime/show-awg-entropy.sh": (AWG_SHOW_ENTROPY_SCRIPT, "0755"),
        "/opt/node-plane-runtime/init-awg.sh": (AWG_INIT_SCRIPT, "0755"),
        "/opt/node-plane-runtime/regenerate-awg-entropy.sh": (AWG_REGENERATE_ENTROPY_SCRIPT, "0755"),
        "/opt/node-plane-runtime/amnezia-awg/Dockerfile": (AWG_DOCKERFILE, "0644"),
        "/opt/node-plane-runtime/amnezia-awg/start.sh": (AWG_START_SCRIPT, "0755"),
        "/opt/node-plane-runtime/deploy-awg.sh": (AWG_DEPLOY_SCRIPT, "0755"),
    }
    files.update(_runtime_metadata_files())
    return files


def sync_server_node_env(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"
    content = render_server_node_env(server)
    rc, out = write_server_file(server, "/etc/node-plane/node.env", content, mode="0600")
    if rc != 0:
        return rc, out
    update_server_fields(server.key, notes="node.env synced from bot")
    return 0, "Файл node.env записан в /etc/node-plane/node.env"


def _runtime_state_from_values(version: str, commit: str) -> str:
    version_value = str(version or "").strip()
    commit_value = str(commit or "").strip()
    if commit_value and commit_value != "unknown":
        if APP_COMMIT != "unknown":
            return "up_to_date" if commit_value == APP_COMMIT else "outdated"
        if version_value:
            return "up_to_date" if version_value == APP_SEMVER else "outdated"
        return "unknown"
    if version_value:
        return "up_to_date" if version_value == APP_SEMVER else "outdated"
    return "unknown"


def get_server_runtime_state(server_key: str) -> dict[str, str]:
    server = get_server(server_key)
    if not server:
        return {
            "state": "missing_server",
            "version": "",
            "commit": "",
            "expected_version": APP_SEMVER,
            "expected_commit": APP_COMMIT,
            "message": f"Сервер {server_key} не найден",
        }
    if server.bootstrap_state != "bootstrapped":
        return {
            "state": "not_bootstrapped",
            "version": "",
            "commit": "",
            "expected_version": APP_SEMVER,
            "expected_commit": APP_COMMIT,
            "message": "bootstrap required",
        }

    script = f"""#!/usr/bin/env bash
set -euo pipefail
if [[ -f {shlex.quote(RUNTIME_VERSION_PATH)} ]]; then
  printf 'version=%s\\n' "$(tr -d '\\r' < {shlex.quote(RUNTIME_VERSION_PATH)} | head -n1)"
fi
if [[ -f {shlex.quote(RUNTIME_BUILD_COMMIT_PATH)} ]]; then
  printf 'commit=%s\\n' "$(tr -d '\\r' < {shlex.quote(RUNTIME_BUILD_COMMIT_PATH)} | head -n1)"
fi
"""
    rc, out = run_server_command(server, script, timeout=30)
    if rc != 0:
        return {
            "state": "unknown",
            "version": "",
            "commit": "",
            "expected_version": APP_SEMVER,
            "expected_commit": APP_COMMIT,
            "message": (out or "").strip() or "failed to read runtime metadata",
        }

    payload: dict[str, str] = {}
    for raw_line in (out or "").splitlines():
        line = raw_line.strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        payload[key.strip()] = value.strip()
    version = str(payload.get("version") or "").strip()
    commit = str(payload.get("commit") or "").strip()
    return {
        "state": _runtime_state_from_values(version, commit),
        "version": version,
        "commit": commit,
        "expected_version": APP_SEMVER,
        "expected_commit": APP_COMMIT,
        "message": "",
    }


def get_servers_needing_runtime_sync() -> list[RegisteredServer]:
    targets: list[RegisteredServer] = []
    for server in list_servers(include_disabled=False):
        if server.bootstrap_state != "bootstrapped":
            continue
        state = str(get_server_runtime_state(server.key).get("state") or "")
        if state in {"outdated", "unknown"}:
            targets.append(server)
    return targets


def sync_server_runtime(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"
    if server.bootstrap_state != "bootstrapped":
        return 1, f"Сервер {server_key} ещё не bootstrap-нут"

    file_rc, file_out = write_server_files(server, _runtime_files(), timeout=180)
    if file_rc != 0:
        return file_rc, file_out

    node_env_rc, node_env_out = write_server_file(server, "/etc/node-plane/node.env", render_server_node_env(server), mode="0600")
    if node_env_rc != 0:
        return node_env_rc, node_env_out

    messages = [
        f"Runtime files synced for {server_key}.",
        "node.env updated.",
    ]
    if "xray" in server.protocol_kinds:
        rc, out = sync_xray_server_settings(server_key)
        if rc != 0:
            return rc, f"{messages[0]}\n\nXray sync failed:\n{out}"
        messages.append("Xray settings synced.")
    update_server_fields(server.key, notes=f"runtime synced to {APP_SEMVER} · {APP_COMMIT}")
    return 0, "\n".join(messages)


def _check_server_ports(server) -> Tuple[int, str]:
    checks: list[tuple[str, str, int]] = []
    if "xray" in server.protocol_kinds:
        checks.append(("xray_tcp_port", "tcp", int(server.xray_tcp_port)))
        checks.append(("xray_xhttp_port", "tcp", int(server.xray_xhttp_port)))
    if "awg" in server.protocol_kinds:
        checks.append(("awg_port", "udp", int(server.awg_port)))
    if not checks:
        return 0, "Проверка портов пропущена"

    payload = "\n".join(f"{field}|{proto}|{port}" for field, proto, port in checks)
    cmd = f"""#!/usr/bin/env bash
set -euo pipefail

cat <<'EOF' >/tmp/node-plane-port-check.txt
{payload}
EOF

docker_cmd() {{
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker "$@"
    return
  fi
  if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    sudo docker "$@"
    return
  fi
  return 1
}}

container_running() {{
  local name="$1"
  docker_cmd ps --format '{{{{.Names}}}}' 2>/dev/null | grep -q "^${{name}}$"
}}

is_busy_tcp() {{
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -H -ltn "( sport = :$port )" 2>/dev/null | grep -q .
    return $?
  fi
  netstat -ltn 2>/dev/null | awk '{{print $4}}' | grep -Eq '[:.]'"$port"'$'
}}

is_busy_udp() {{
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -H -lun "( sport = :$port )" 2>/dev/null | grep -q .
    return $?
  fi
  netstat -lun 2>/dev/null | awk '{{print $4}}' | grep -Eq '[:.]'"$port"'$'
}}

find_free_tcp() {{
  local start="$1"
  local end=$((start + 50))
  local port
  for ((port=start+1; port<=end; port++)); do
    if ! is_busy_tcp "$port"; then
      echo "$port"
      return 0
    fi
  done
  echo ""
}}

find_free_udp() {{
  local start="$1"
  local end=$((start + 50))
  local port
  for ((port=start+1; port<=end; port++)); do
    if ! is_busy_udp "$port"; then
      echo "$port"
      return 0
    fi
  done
  echo ""
}}

has_ufw_rules() {{
  command -v iptables >/dev/null 2>&1 && iptables -S ufw-user-input >/dev/null 2>&1
}}

firewall_allows() {{
  local proto="$1"
  local port="$2"
  if has_ufw_rules; then
    iptables -C ufw-user-input -p "$proto" --dport "$port" -j ACCEPT >/dev/null 2>&1
    return $?
  fi
  return 0
}}

is_managed_xray_port() {{
  local field="$1"
  local port="$2"
  local container="{server.xray_service_name}"
  local cfg="{server.xray_config_path}"
  container_running "$container" || return 1
  [[ -f "$cfg" ]] || return 1
  XRAY_FIELD="$field" XRAY_PORT="$port" XRAY_CFG="$cfg" python3 - <<'PY'
import json
import os
import sys

field = os.environ["XRAY_FIELD"]
port = int(os.environ["XRAY_PORT"])
cfg_path = os.environ["XRAY_CFG"]
tag_map = {{
    "xray_tcp_port": "reality-tcp",
    "xray_xhttp_port": "reality-xhttp",
}}
tag = tag_map.get(field)
if not tag:
    sys.exit(1)
try:
    with open(cfg_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(1)
for inbound in data.get("inbounds", []) or []:
    if str(inbound.get("tag") or "") != tag:
        continue
    try:
        inbound_port = int(inbound.get("port") or 0)
    except Exception:
        inbound_port = 0
    if inbound_port == port:
        sys.exit(0)
sys.exit(1)
PY
}}

is_managed_awg_port() {{
  local port="$1"
  local container="{AWG_RUNTIME_CONTAINER}"
  local cfg="{server.awg_config_path}"
  container_running "$container" || return 1
  [[ -f "$cfg" ]] || return 1
  AWG_PORT="$port" AWG_CFG="$cfg" python3 - <<'PY'
import os
import re
import sys

port = int(os.environ["AWG_PORT"])
cfg_path = os.environ["AWG_CFG"]
listen_port = None
try:
    with open(cfg_path, "r", encoding="utf-8", errors="ignore") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^ListenPort\\s*=\\s*(\\d+)\\s*$", line)
            if m:
                listen_port = int(m.group(1))
                break
except Exception:
    sys.exit(1)
sys.exit(0 if listen_port == port else 1)
PY
}}

while IFS='|' read -r field proto port; do
  [[ -n "$field" ]] || continue
  if [[ "$proto" == "tcp" ]]; then
    if is_busy_tcp "$port"; then
      if is_managed_xray_port "$field" "$port"; then
        echo "PORT_STATUS|$field|$proto|$port|managed|"
      else
        echo "PORT_STATUS|$field|$proto|$port|busy|$(find_free_tcp "$port")"
      fi
    else
      echo "PORT_STATUS|$field|$proto|$port|free|"
    fi
  else
    if is_busy_udp "$port"; then
      if [[ "$field" == "awg_port" ]] && is_managed_awg_port "$port"; then
        echo "PORT_STATUS|$field|$proto|$port|managed|"
      else
        echo "PORT_STATUS|$field|$proto|$port|busy|$(find_free_udp "$port")"
      fi
    else
      echo "PORT_STATUS|$field|$proto|$port|free|"
    fi
  fi
  if firewall_allows "$proto" "$port"; then
    echo "FIREWALL_STATUS|$field|$proto|$port|open|"
  else
    echo "FIREWALL_STATUS|$field|$proto|$port|closed|ufw allow $port/$proto"
  fi
done </tmp/node-plane-port-check.txt
rm -f /tmp/node-plane-port-check.txt
"""
    rc, out = run_server_command(server, cmd, timeout=30)
    if rc != 0:
        return rc, out

    conflicts: list[str] = []
    firewall_conflicts: list[str] = []
    suggestions: list[str] = []
    firewall_suggestions: list[str] = []
    for raw_line in (out or "").splitlines():
        line = raw_line.strip()
        if line.startswith("PORT_STATUS|"):
            _, field, proto, port, status, suggestion = (line.split("|", 5) + [""])[:6]
            if status == "managed":
                continue
            if status == "busy":
                conflicts.append(f"- {field}: {port}/{proto} занят")
                if suggestion.strip():
                    suggestions.append(f"/setserverfield {server.key} {field} {suggestion.strip()}")
            continue
        if line.startswith("FIREWALL_STATUS|"):
            _, field, proto, port, status, suggestion = (line.split("|", 5) + [""])[:6]
            if status == "closed":
                firewall_conflicts.append(f"- {field}: {port}/{proto} не открыт в firewall")
                if suggestion.strip():
                    firewall_suggestions.append(suggestion.strip())
    if conflicts or firewall_conflicts:
        lines = [
            f"Проверка портов для сервера {server.key} завершилась ошибкой.",
        ]
        if conflicts:
            lines.extend(["Занятые порты:", *conflicts])
        if firewall_conflicts:
            if conflicts:
                lines.append("")
            lines.extend(["Не хватает правил firewall:", *firewall_conflicts])
        if suggestions:
            lines.extend(["", "Рекомендуемые команды:", *[f"- {item}" for item in suggestions]])
        if firewall_suggestions:
            lines.extend(["", "Рекомендуемые команды для firewall:", *[f"- {item}" for item in firewall_suggestions]])
        return 1, "\n".join(lines)
    return 0, _format_port_status_summary(out)


def check_server_ports(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"
    return _check_server_ports(server)


def open_server_ports(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"

    checks: list[tuple[str, str, int]] = []
    if "xray" in server.protocol_kinds:
        checks.append(("xray_tcp_port", "tcp", int(server.xray_tcp_port)))
        checks.append(("xray_xhttp_port", "tcp", int(server.xray_xhttp_port)))
    if "awg" in server.protocol_kinds:
        checks.append(("awg_port", "udp", int(server.awg_port)))
    if not checks:
        return 0, "Для этого сервера нет управляемых портов"

    payload = "\n".join(f"{field}|{proto}|{port}" for field, proto, port in checks)
    cmd = f"""#!/usr/bin/env bash
set -euo pipefail

command -v ufw >/dev/null 2>&1 || {{
  echo "UFW не установлен на этой ноде."
  exit 1
}}

cat <<'EOF' >/tmp/node-plane-open-ports.txt
{payload}
EOF

while IFS='|' read -r field proto port; do
  [[ -n "$field" ]] || continue
  ufw allow "$port/$proto" >/dev/null
  echo "OPENED|$field|$proto|$port"
done </tmp/node-plane-open-ports.txt

rm -f /tmp/node-plane-open-ports.txt
ufw reload >/dev/null 2>&1 || true
"""
    rc, out = run_server_command(server, cmd, timeout=60)
    if rc != 0:
        return rc, out

    opened: list[str] = []
    for raw_line in (out or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("OPENED|"):
            continue
        _, field, proto, port = (line.split("|", 3) + [""])[:4]
        opened.append(f"- {field}: {port}/{proto}")
    if not opened:
        return 0, "Правила firewall обновлены."
    return 0, "Открыты правила firewall:\n" + "\n".join(opened)


def probe_server(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"
    if server.transport == "local" and is_running_in_container():
        return (
            1,
            "PROBE_UNSUPPORTED|local_in_container\n"
            "hostname: local-host\n"
            "пользователь: bot\n"
            "ядро: container\n"
            "reason: Local transport is unavailable while the bot runs inside a container.\n"
            "remediation: Register this node with transport=ssh or run the bot on the host via systemd.",
        )
    cmd = """#!/usr/bin/env bash
set -euo pipefail

echo "hostname: $(hostname)"
echo "пользователь: $(whoami)"
echo "ядро: $(uname -a)"

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "docker: доступен"
elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
  echo "docker: доступен через sudo"
else
  echo "docker: недоступен"
fi

if [[ -c /dev/net/tun ]]; then
  echo "tun: доступен"
else
  echo "tun: отсутствует"
fi

if [[ -c /dev/net/tun ]] && { (command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1) || (command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1); }; then
  echo "awg_userspace_ready: да"
else
  echo "awg_userspace_ready: нет"
fi
"""
    rc, out = run_server_command(server, cmd, timeout=20)
    if rc == 0:
        lines = [line.strip() for line in (out or "").splitlines() if line.strip()]
        summary = []
        docker_line = next((line for line in lines if line.startswith("docker:")), "")
        for prefix in ("docker:", "tun:", "awg_userspace_ready:"):
            hit = next((line for line in lines if line.startswith(prefix)), None)
            if hit:
                summary.append(hit)
        port_rc, port_out = _check_server_ports(server)
        if port_out.strip():
            out = (out.rstrip() + "\n\n" + port_out.strip()).strip()
        if docker_line == "docker: недоступен":
            out = (out.rstrip() + "\n\n" + _docker_install_suggestion("missing")).strip()
        if port_rc != 0:
            summary.append("ports: conflict")
        if summary:
            try:
                update_server_fields(server.key, notes="probe: " + " | ".join(summary))
            except Exception:
                pass
        if port_rc != 0:
            return 1, out
    return rc, out


def install_server_docker(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"

    docker_ok, docker_status = _docker_status(server)
    if docker_ok:
        if docker_status == "available_via_sudo":
            return 0, "Docker уже установлен и доступен через sudo."
        return 0, "Docker уже установлен и доступен."

    rc, out = run_server_command(server, _install_docker_script(), timeout=900)
    docker_ok, docker_status = _docker_status(server)
    if docker_ok:
        if docker_status == "available_via_sudo":
            return 0, "DOCKER_INSTALL_STATUS|ok|available_via_sudo"
        return 0, "DOCKER_INSTALL_STATUS|ok|available"

    if rc != 0:
        tail = (out or "").strip()[-2000:]
        return rc, f"DOCKER_INSTALL_STATUS|error|{docker_status}\n{tail}".strip()
    return 1, f"DOCKER_INSTALL_STATUS|error|{docker_status}"


def is_server_docker_available(server_key: str) -> bool:
    server = get_server(server_key)
    if not server:
        return False
    docker_ok, _ = _docker_status(server)
    return docker_ok


def sync_xray_server_settings(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"
    if "xray" not in server.protocol_kinds:
        return 1, f"На сервере {server_key} не включён Xray"

    rc, out = run_server_command(
        server,
        _shell_join_args(
            "/opt/node-plane-runtime/sync-xray.sh",
            server.xray_config_path,
            server.public_host,
            server.xray_flow,
            "ghcr.io/xtls/xray-core:25.12.8",
        ),
        timeout=120,
    )
    if rc != 0:
        return rc, out
    try:
        generated = _extract_last_json_object(out)
    except Exception:
        return 1, f"Не удалось разобрать синхронизированные настройки Xray:\n{out[-1500:]}"
    update_server_fields(server.key, **generated)
    return 0, json.dumps(generated, ensure_ascii=False, indent=2)


def show_awg_entropy(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"
    if "awg" not in server.protocol_kinds:
        return 1, f"На сервере {server_key} не включён AWG"
    return run_server_command(server, "/opt/node-plane-runtime/show-awg-entropy.sh", timeout=60)


def regenerate_awg_entropy(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"
    if "awg" not in server.protocol_kinds:
        return 1, f"На сервере {server_key} не включён AWG"
    return run_server_command(server, "/opt/node-plane-runtime/regenerate-awg-entropy.sh", timeout=180)


def show_server_metrics(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"
    script = _server_metrics_script()
    return run_server_command(server, script, timeout=60)


def _server_metrics_script() -> str:
    return r"""#!/usr/bin/env bash
set -euo pipefail

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return 0
  fi
  if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    sudo docker "$@"
    return 0
  fi
  return 1
}

echo "host: $(hostname)"
echo "kernel: $(uname -srmo)"
if command -v uptime >/dev/null 2>&1; then
  echo "uptime: $(uptime -p 2>/dev/null || uptime)"
fi
if [[ -r /proc/loadavg ]]; then
  echo "loadavg: $(cut -d' ' -f1-3 /proc/loadavg)"
fi
if [[ -r /proc/stat ]]; then
  cpu_usage="$(python3 - <<'PY'
import time

def read():
    with open("/proc/stat", "r", encoding="utf-8") as fh:
        parts = fh.readline().split()[1:]
    values = [int(part) for part in parts[:8]]
    total = sum(values)
    idle = values[3] + values[4]
    return total, idle

t1, i1 = read()
time.sleep(0.2)
t2, i2 = read()
total_delta = max(t2 - t1, 0)
idle_delta = max(i2 - i1, 0)
busy = 0.0 if total_delta <= 0 else (100.0 * (total_delta - idle_delta) / total_delta)
print(f"{busy:.1f}%")
PY
)"
  echo "cpu usage: $cpu_usage"
fi
if command -v nproc >/dev/null 2>&1; then
  echo "cpus: $(nproc)"
fi
if command -v free >/dev/null 2>&1; then
  free -h | awk 'NR==2 {print "memory: " $3 " / " $2 " used"}'
fi
if command -v df >/dev/null 2>&1; then
  df -h / | awk 'NR==2 {print "disk /: " $3 " / " $2 " used (" $5 ")"}'
fi

if docker_cmd ps --format '{{.Names}} {{.Status}}' >/tmp/node-plane-metrics-docker.$$ 2>/dev/null; then
  echo "docker: available"
  xray_status="$(grep '^xray ' /tmp/node-plane-metrics-docker.$$ | cut -d' ' -f2- | head -n1 || true)"
  awg_status="$(grep '^amnezia-awg ' /tmp/node-plane-metrics-docker.$$ | cut -d' ' -f2- | head -n1 || true)"
  echo "xray: ${xray_status:-not running}"
  echo "awg: ${awg_status:-not running}"
  rm -f /tmp/node-plane-metrics-docker.$$
else
  echo "docker: unavailable"
fi
"""


def bootstrap_server(server_key: str, preserve_config: bool = False) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"

    port_rc, port_out = _check_server_ports(server)
    if port_rc != 0:
        _mark(server, "bootstrap_failed", port_out[-1500:])
        return port_rc, port_out

    rc, out = run_server_command(server, _packages_script(), timeout=600)
    if rc != 0:
        docker_ok, docker_status = _docker_status(server)
        if not docker_ok:
            msg = _docker_install_suggestion(docker_status, out)
            _mark(server, "bootstrap_failed", msg[-1500:])
            return 1, msg
        _mark(server, "bootstrap_failed", out[-1500:])
        return rc, out

    docker_ok, docker_status = _docker_status(server)
    if not docker_ok:
        msg = _docker_install_suggestion(docker_status, out)
        _mark(server, "bootstrap_failed", msg[-1500:])
        return 1, msg

    file_rc, file_out = write_server_files(server, _runtime_files(), timeout=180)
    if file_rc != 0:
        _mark(server, "bootstrap_failed", file_out[-1500:])
        return file_rc, file_out

    node_env_rc, node_env_out = write_server_file(server, "/etc/node-plane/node.env", render_server_node_env(server), mode="0600")
    if node_env_rc != 0:
        _mark(server, "bootstrap_failed", node_env_out[-1500:])
        return node_env_rc, node_env_out

    reused_xray_config = False
    reused_awg_config = False

    if "xray" in server.protocol_kinds:
        sni_host = server.xray_sni or "www.cloudflare.com"
        if preserve_config and _remote_file_exists(server, server.xray_config_path):
            reused_xray_config = True
        else:
            rc, out = run_server_command(
                server,
                _shell_join_args(
                    "/opt/node-plane-runtime/init-xray.sh",
                    server.xray_config_path,
                    server.public_host,
                    sni_host,
                    server.xray_tcp_port,
                    server.xray_xhttp_port,
                    server.xray_xhttp_path_prefix,
                    server.xray_flow,
                    "ghcr.io/xtls/xray-core:25.12.8",
                ),
                timeout=180,
            )
            if rc != 0:
                _mark(server, "bootstrap_failed", out[-1500:])
                return rc, out
            try:
                generated = _extract_last_json_object(out)
            except Exception:
                _mark(server, "bootstrap_failed", out[-1500:])
                return 1, f"Не удалось разобрать сгенерированные настройки Xray:\n{out[-1500:]}"
            if not generated.get("xray_pbk"):
                _mark(server, "bootstrap_failed", out[-1500:])
                return 1, f"Сгенерированные настройки Xray неполные:\n{out[-1500:]}"
            update_server_fields(server.key, **generated)
        rc, out = run_server_command(server, "/opt/node-plane-runtime/deploy-xray.sh", timeout=300)
        if rc != 0:
            _mark(server, "bootstrap_failed", out[-1500:])
            return rc, out

    if "awg" in server.protocol_kinds:
        if preserve_config and _remote_file_exists(server, server.awg_config_path):
            reused_awg_config = True
        else:
            rc, out = run_server_command(server, "/opt/node-plane-runtime/init-awg.sh", timeout=120)
            if rc != 0:
                _mark(server, "bootstrap_failed", out[-1500:])
                return rc, out
        rc, out = run_server_command(server, "/opt/node-plane-runtime/deploy-awg.sh", timeout=900)
        if rc != 0:
            _mark(server, "bootstrap_failed", out[-1500:])
            return rc, out

    completed_parts = ["Base packages and helper scripts installed"]
    if "xray" in server.protocol_kinds:
        if reused_xray_config:
            completed_parts.append("Xray config preserved, runtime redeployed")
        else:
            completed_parts.append("Xray settings generated, runtime deployed")
    if "awg" in server.protocol_kinds:
        if reused_awg_config:
            completed_parts.append("AWG config preserved, runtime redeployed")
        else:
            completed_parts.append("AWG runtime deployed")
    summary = ". ".join(completed_parts) + "."

    _mark(
        server,
        "bootstrapped",
        summary,
    )
    return 0, (
        "Bootstrap завершён.\n"
        f"{summary}\n"
        "Рабочий node.env записан в /etc/node-plane/node.env."
    )


def reinstall_server(server_key: str, preserve_config: bool = True) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"

    prefix = "Режим переустановки: с сохранением существующего конфига.\n\n" if preserve_config else "Режим переустановки: чистая переустановка.\n\n"
    if not preserve_config:
        rc, out = _cleanup_server_runtime(server, preserve_config=False)
        if rc != 0:
            _mark(server, "bootstrap_failed", out[-1500:])
            return rc, out
    rc, out = bootstrap_server(server_key, preserve_config=preserve_config)
    return rc, prefix + out


def delete_server_runtime(server_key: str, preserve_config: bool = True) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"

    rc, out = _cleanup_server_runtime(server, preserve_config=preserve_config)
    if rc != 0:
        _mark(server, "bootstrap_failed", out[-1500:])
        return rc, out

    updates = {
        "bootstrap_state": "edited" if preserve_config else "new",
        "notes": "runtime removed; config preserved" if preserve_config else "runtime removed; config wiped",
    }
    if not preserve_config:
        updates.update(
            {
                "xray_pbk": "",
                "xray_short_id": "",
            }
        )
    update_server_fields(server.key, **updates)
    suffix = "Существующие конфиги сохранены." if preserve_config else "Конфиги и директории рантайма удалены."
    return 0, f"Рантайм удалён.\n{suffix}"


def full_cleanup_server(server_key: str, remove_ssh_key: bool = False) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"

    rc, out = delete_server_runtime(server_key, preserve_config=False)
    if rc != 0:
        return rc, out

    cleanup_lines = [out.strip()]
    notes = ["full cleanup completed"]

    if remove_ssh_key and server.transport == "ssh":
        key_rc, key_out = _remove_bot_ssh_key(server)
        if key_rc == 0:
            if key_out.strip():
                cleanup_lines.append(key_out.strip())
            notes.append("ssh key removed")
        else:
            cleanup_lines.append(f"SSH key removal failed: {(key_out or '').strip()[:800]}")
            notes.append("ssh key removal failed")
        update_server_fields(server.key, notes="; ".join(notes))
        return 0, "\n".join(line for line in cleanup_lines if line)

    update_server_fields(server.key, notes="; ".join(notes))
    return 0, "\n".join(line for line in cleanup_lines if line)
