#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

read -r name
if [[ ! "$name" =~ ^[a-zA-Z0-9._-]+$ ]]; then
  echo "Invalid profile name" >&2
  exit 1
fi

config="${XRAY_CONFIG:-/opt/node-plane-runtime/xray/config.json}"
uuid="$(python3 -c 'import uuid; print(uuid.uuid4())')"
/opt/node-plane-runtime/xray-add-user-existing.sh "$name" "$uuid" >/dev/null
short_id="$(CONFIG_PATH="$config" python3 - <<'PY'
import json
import os
config = json.load(open(os.environ["CONFIG_PATH"], encoding="utf-8"))
for inbound in config["inbounds"]:
    if inbound.get("tag") == "reality-tcp":
        print(inbound["streamSettings"]["realitySettings"]["shortIds"][0])
        break
PY
)"
echo "$uuid $short_id"
