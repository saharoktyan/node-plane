#!/usr/bin/env bash
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
