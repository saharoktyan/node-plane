#!/usr/bin/env bash
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
