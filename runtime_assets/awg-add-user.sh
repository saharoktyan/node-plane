#!/usr/bin/env bash
set -euo pipefail
umask 077
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
PROFILE_TOOL="${AWG_PROFILE_TOOL:-/opt/node-plane-runtime/awg_profile.py}"
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
python3 "$PROFILE_TOOL" validate "$CFG"
if [[ -z "$SERVER_IP" ]]; then
  echo "AWG_SERVER_IP is not configured in /etc/node-plane/node.env" >&2
  exit 1
fi
DISPLAY_NAME="$NAME"
PROFILE_DESCRIPTION="$NAME"
if [[ -n "$SERVER_KEY" ]]; then
  DISPLAY_NAME="${SERVER_KEY}-${NAME}"
  PROFILE_DESCRIPTION="${SERVER_KEY} · ${NAME}"
fi
if [[ ! "$DISPLAY_NAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "Invalid AWG profile name" >&2
  exit 1
fi
if ! docker_cmd ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  echo "Container ${CONTAINER} not running" >&2
  exit 1
fi
for asset in "$CONF2VPN" "$AWG_TEMPLATE" "$AMNEZIA_DECODER"; do
  if [[ ! -f "$asset" ]]; then
    echo "AWG client config asset is missing: $asset" >&2
    exit 1
  fi
done

CLIENTS_DIR="${AWG_CLIENTS_DIR:-/opt/node-plane-runtime/awg-clients}"
CLIENT_RESULT="${CLIENTS_DIR}/${DISPLAY_NAME}.txt"
mkdir -p "$CLIENTS_DIR"
chmod 700 "$CLIENTS_DIR"
if grep -Fxq "# ${DISPLAY_NAME}" "$CFG"; then
  if [[ -s "$CLIENT_RESULT" ]]; then
    cat "$CLIENT_RESULT"
    exit 0
  fi
  # Earlier failed operations could leave a peer without a saved private key.
  # Remove that orphan before creating a replacement the bot can deliver.
  STALE_PUB="$(awk -v name="$DISPLAY_NAME" '
    $0 == "# " name {found=1; next}
    found && /^PublicKey = / {print $3; exit}
    found && NF == 0 {exit}
  ' "$CFG")"
  if [[ -z "$STALE_PUB" ]]; then
    echo "Existing AWG peer has no public key: ${DISPLAY_NAME}" >&2
    exit 1
  fi
  docker_cmd exec -i "$CONTAINER" wg set "$IFACE" peer "$STALE_PUB" remove
  STALE_TMP="$(mktemp "$(dirname "$CFG")/.awg-orphan-XXXXXX")"
  awk -v name="$DISPLAY_NAME" '
    $0 == "# " name {skip=1; next}
    skip && NF == 0 {skip=0; next}
    skip {next}
    {print}
  ' "$CFG" > "$STALE_TMP"
  chmod 600 "$STALE_TMP"
  mv "$STALE_TMP" "$CFG"
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
    "HEADER_PROTECTION_KEY": "",
    "CONTENT_PADDING_ADDITION": "",
    "REKEY_AFTER_TIME": "",
    "REKEY_TIMEOUT": "",
    "REJECT_AFTER_TIME": "",
    "KEEPALIVE_TIMEOUT": "",
    "MAX_HANDSHAKE_ATTEMPTS": "",
    "RANDOM_TRAILERS": "",
    "DISABLE_COOKIES": "",
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
    "HeaderProtectionKey": "HEADER_PROTECTION_KEY",
    "ContentPaddingAddition": "CONTENT_PADDING_ADDITION",
    "RekeyAfterTime": "REKEY_AFTER_TIME",
    "RekeyTimeout": "REKEY_TIMEOUT",
    "RejectAfterTime": "REJECT_AFTER_TIME",
    "KeepaliveTimeout": "KEEPALIVE_TIMEOUT",
    "MaxHandshakeAttempts": "MAX_HANDSHAKE_ATTEMPTS",
    "RandomTrailers": "RANDOM_TRAILERS",
    "DisableCookies": "DISABLE_COOKIES",
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
  "wg show $IFACE allowed-ips | awk '{print \$NF}' | cut -d/ -f1" | tr -d '\r'
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
  ' | tr -d '\r'
)

SERVER_PUB="$(docker_cmd exec -i "$CONTAINER" sh -lc "wg show $IFACE public-key" | tr -d '\r')"

docker_cmd exec -i "$CONTAINER" sh -lc "
  tmp=\$(mktemp)
  echo '$CLIENT_PSK' > \$tmp
  wg set $IFACE peer '$CLIENT_PUB' preshared-key \$tmp allowed-ips '$FREE_IP/32'
  rm -f \$tmp
"

printf '\n# %s\n[Peer]\nPublicKey = %s\nPresharedKey = %s\nAllowedIPs = %s/32\n' \
  "$DISPLAY_NAME" "$CLIENT_PUB" "$CLIENT_PSK" "$FREE_IP" >> "$CFG"

TMP_CONF="$(mktemp /tmp/awg-client-XXXX.conf)"
TMP_JSON="$(mktemp /tmp/awg-amnezia-XXXX.json)"
RESULT_TMP="$(mktemp "${CLIENTS_DIR}/.client-XXXXXX")"
trap 'rm -f "$TMP_CONF" "$TMP_JSON" "$RESULT_TMP"' EXIT

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
HeaderProtectionKey = $HEADER_PROTECTION_KEY
ContentPaddingAddition = $CONTENT_PADDING_ADDITION
RandomTrailers = $RANDOM_TRAILERS
DisableCookies = $DISABLE_COOKIES

[Peer]
PublicKey = $SERVER_PUB
PresharedKey = $CLIENT_PSK
Endpoint = $SERVER_IP:$SERVER_PORT
AllowedIPs = $ALLOWED_IPS
PersistentKeepalive = $KEEPALIVE
EOF

for optional in \
  "RekeyAfterTime:$REKEY_AFTER_TIME" \
  "RekeyTimeout:$REKEY_TIMEOUT" \
  "RejectAfterTime:$REJECT_AFTER_TIME" \
  "KeepaliveTimeout:$KEEPALIVE_TIMEOUT" \
  "MaxHandshakeAttempts:$MAX_HANDSHAKE_ATTEMPTS"; do
  key="${optional%%:*}"
  value="${optional#*:}"
  if [[ -n "$value" ]]; then
    sed -i "/^\[Peer\]$/i ${key} = ${value}\n" "$TMP_CONF"
  fi
done
python3 "$PROFILE_TOOL" validate "$TMP_CONF"

{
  cat "$TMP_CONF"
  echo
  echo "=========== AMNEZIA TEXT KEY (vpn://) ==========="
  python3 "$CONF2VPN" \
    "$TMP_CONF" \
    "$AWG_TEMPLATE" \
    "$TMP_JSON" \
    "$AMNEZIA_DECODER" \
    "$CONTAINER" \
    "$PROFILE_DESCRIPTION"
  echo "================================================="
} > "$RESULT_TMP"
chmod 600 "$RESULT_TMP"
mv "$RESULT_TMP" "$CLIENT_RESULT"
cat "$CLIENT_RESULT"
