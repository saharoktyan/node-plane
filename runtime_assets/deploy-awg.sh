#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

docker_cmd() {
  # Bare return can inherit the pending EXIT status on Bash 5.2 during rollback.
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return $?
  fi
  if command -v sudo >/dev/null 2>&1; then
    sudo systemctl enable --now docker >/dev/null 2>&1 || sudo service docker start >/dev/null 2>&1 || true
    if sudo docker info >/dev/null 2>&1; then
      sudo docker "$@"
      return $?
    fi
  fi
  systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return $?
  fi
  echo "Docker daemon is unavailable for this session." >&2
  id >&2 || true
  groups >&2 || true
  ls -l /var/run/docker.sock >&2 || true
  exit 1
}

DOCKER_DIR="${AWG_DOCKER_DIR:-/opt/node-plane-runtime/amnezia-awg}"
IMAGE="${AWG_DOCKER_IMAGE:-node-plane-amnezia-awg:3.1.20260828}"
if [[ "$IMAGE" == "node-plane-amnezia-awg:0.2.16" ]]; then
  IMAGE="node-plane-amnezia-awg:3.1.20260828"
fi
CONTAINER="${AWG_CONTAINER_NAME:-amnezia-awg}"
CFG="${AWG_CONFIG:-/opt/node-plane-runtime/amnezia-awg/data/wg0.conf}"
IFACE="${AWG_IFACE:-wg0}"
PORT="${AWG_SERVER_PORT:-51820}"
IPV6_EFFECTIVE=off
NETWORK_ARGS=()
IPV6_SYSCTL_ARGS=()
if [[ -e /proc/sys/net/ipv6/conf/all/disable_ipv6 ]]; then
  IPV6_SYSCTL_ARGS=(--sysctl net.ipv6.conf.all.disable_ipv6=0 --sysctl net.ipv6.conf.all.forwarding=1)
fi
if [[ "${IPV6_OUTBOUND:-on}" == on ]] && ip -6 route show default | grep -q . && ip -6 address show scope global | grep -q 'inet6 '; then
  # Docker enables forwarding. Keep kernel-managed RA on the uplink working
  # when forwarding changes, including after a reboot.
  RA_UPLINK="$(ip -6 route show default proto ra | awk '/dev/ {for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}')"
  if [[ "$RA_UPLINK" =~ ^[A-Za-z0-9_.:-]+$ ]] && [[ "$(sysctl -n "net/ipv6/conf/$RA_UPLINK/accept_ra" 2>/dev/null || true)" == 1 ]]; then
    install -d -m 0755 /etc/sysctl.d
    printf '%s\n' '# Managed by Node Plane: IPv6 router advertisements' "-net/ipv6/conf/$RA_UPLINK/accept_ra = 2" > /etc/sysctl.d/70-node-plane-ipv6.conf
    sysctl -w "net/ipv6/conf/$RA_UPLINK/accept_ra=2" >/dev/null
  fi
  NETWORK_NAME=node-plane-awg
  NETWORK_PREFIX=fd42:6e70:6177:1::/64
  if ! docker_cmd network inspect "$NETWORK_NAME" >/dev/null 2>&1; then
    docker_cmd network create --driver bridge --ipv6 --subnet "$NETWORK_PREFIX" \
      --label org.node-plane.network=awg "$NETWORK_NAME" >/dev/null
  fi
  NETWORK_INFO="$(docker_cmd network inspect "$NETWORK_NAME")"
  NETWORK_INFO_ENV="$NETWORK_INFO" python3 - <<'PYNET'
import json, os
network = json.loads(os.environ['NETWORK_INFO_ENV'])[0]
if (network.get('Driver') != 'bridge' or not network.get('EnableIPv6')
        or network.get('Labels', {}).get('org.node-plane.network') != 'awg'
        or not any(item.get('Subnet') == 'fd42:6e70:6177:1::/64' for item in network['IPAM']['Config'])):
    raise SystemExit('Existing node-plane-awg network does not match Node Plane ownership or IPv6 configuration')
PYNET
  NETWORK_ARGS=(--network "$NETWORK_NAME")
  IPV6_EFFECTIVE=on
fi

if [[ ! -c /dev/net/tun ]]; then
  echo "/dev/net/tun is not available on this host. AWG userspace runtime cannot start." >&2
  exit 1
fi
if lsmod | grep -q '^amneziawg '; then
  echo "Host kernel module amneziawg is loaded. Refusing to start AWG userspace runtime on a dirty host." >&2
  exit 1
fi

mkdir -p "$DOCKER_DIR/data"
if [[ ! -s "$CFG" ]]; then
  echo "AWG config is missing or empty: $CFG" >&2
  exit 1
fi
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

CFG_BACKUP="$(mktemp "$(dirname "$CFG")/.awg-deploy-backup-XXXXXX")"
cp -p "$CFG" "$CFG_BACKUP"
OLD_CONTAINER=""
OLD_CONTAINER_RENAMED=0
NEW_CONTAINER_ATTEMPTED=0
ROLLBACK_NEEDED=1
restore_previous() {
  if [[ "$ROLLBACK_NEEDED" != 1 ]]; then
    return $?
  fi
  set +e
  cp -p "$CFG_BACKUP" "$CFG"
  if [[ "$NEW_CONTAINER_ATTEMPTED" == 1 ]]; then
    docker_cmd rm -f "$CONTAINER" >/dev/null 2>&1
  fi
  restored=0
  if [[ "$OLD_CONTAINER_RENAMED" == 1 ]]; then
    if docker_cmd rename "$OLD_CONTAINER" "$CONTAINER" >/dev/null 2>&1; then
      if [[ -f /etc/systemd/system/node-plane-leased-awg.service ]]; then
        python3 /opt/node-plane-runtime/apply-profile-intent.py prepare-leased-runtime awg >/dev/null || return 1
      fi
      docker_cmd start "$CONTAINER" >/dev/null 2>&1 && restored=1
    fi
  fi
  rm -f "$CFG_BACKUP"
  if [[ "$restored" == 1 ]]; then
    echo "AWG deployment failed; previous config and container restored" >&2
  else
    echo "AWG deployment failed; previous config restored, but the container needs attention" >&2
  fi
}
trap restore_previous EXIT

python3 /opt/node-plane-runtime/awg_profile.py migrate "$CFG" "${AWG_I1_PRESET:-quic}"
python3 /opt/node-plane-runtime/awg_profile.py validate "$CFG"
python3 /opt/node-plane-runtime/awg_ipv6.py migrate "$CFG"

if docker_cmd ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  OLD_CONTAINER="${CONTAINER}-previous-$$"
  docker_cmd rename "$CONTAINER" "$OLD_CONTAINER" >/dev/null
  OLD_CONTAINER_RENAMED=1
  docker_cmd stop "$OLD_CONTAINER" >/dev/null
fi

NEW_CONTAINER_ATTEMPTED=1
if [[ -f /etc/systemd/system/node-plane-leased-awg.service ]]; then
  python3 /opt/node-plane-runtime/apply-profile-intent.py prepare-leased-runtime awg >/dev/null
fi
docker_cmd run -d \
  --name "$CONTAINER" \
  --restart no \
  --cap-add NET_ADMIN \
  --device /dev/net/tun:/dev/net/tun \
  --sysctl net.ipv4.ip_forward=1 \
  "${IPV6_SYSCTL_ARGS[@]}" \
  "${NETWORK_ARGS[@]}" \
  -e IPV6_OUTBOUND="$IPV6_EFFECTIVE" \
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
    "${IPV6_SYSCTL_ARGS[@]}" \
    "${NETWORK_ARGS[@]}" \
    -e IPV6_OUTBOUND="$IPV6_EFFECTIVE" \
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

if [[ -f /etc/systemd/system/node-plane-leased-awg.service ]]; then
  python3 /opt/node-plane-runtime/apply-profile-intent.py leased-deploy awg >/dev/null
else
  docker_cmd update --restart unless-stopped "$CONTAINER" >/dev/null
fi
ROLLBACK_NEEDED=0
trap - EXIT
rm -f "$CFG_BACKUP"
if [[ -n "$OLD_CONTAINER" ]]; then
  docker_cmd rm -f "$OLD_CONTAINER" >/dev/null
fi
echo "AWG container deployed: $CONTAINER"
