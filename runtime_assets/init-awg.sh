#!/usr/bin/env bash
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
