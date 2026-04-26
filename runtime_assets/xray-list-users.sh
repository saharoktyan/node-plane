#!/usr/bin/env bash
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
