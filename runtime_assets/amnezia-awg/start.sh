#!/bin/sh
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
