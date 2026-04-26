#!/usr/bin/env bash
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
