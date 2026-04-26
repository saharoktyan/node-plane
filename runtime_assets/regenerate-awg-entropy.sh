#!/usr/bin/env bash
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
