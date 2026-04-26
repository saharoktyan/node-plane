#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return
  fi
  if command -v sudo >/dev/null 2>&1; then
    sudo systemctl enable --now docker >/dev/null 2>&1 || sudo service docker start >/dev/null 2>&1 || true
    if sudo docker info >/dev/null 2>&1; then
      sudo docker "$@"
      return
    fi
  fi
  systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return
  fi
  echo "Docker daemon is unavailable for this session." >&2
  id >&2 || true
  groups >&2 || true
  ls -l /var/run/docker.sock >&2 || true
  exit 1
}

DOCKER_DIR="${AWG_DOCKER_DIR:-/opt/node-plane-runtime/amnezia-awg}"
IMAGE="${AWG_DOCKER_IMAGE:-node-plane-amnezia-awg:0.2.16}"
CONTAINER="${AWG_CONTAINER_NAME:-amnezia-awg}"
CFG="${AWG_CONFIG:-/opt/node-plane-runtime/amnezia-awg/data/wg0.conf}"
IFACE="${AWG_IFACE:-wg0}"
PORT="${AWG_SERVER_PORT:-51820}"

if [[ ! -c /dev/net/tun ]]; then
  echo "/dev/net/tun is not available on this host. AWG userspace runtime cannot start." >&2
  exit 1
fi
if lsmod | grep -q '^amneziawg '; then
  echo "Host kernel module amneziawg is loaded. Refusing to start AWG userspace runtime on a dirty host." >&2
  exit 1
fi

mkdir -p "$DOCKER_DIR/data"
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

if docker_cmd ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  docker_cmd rm -f "$CONTAINER" >/dev/null
fi

docker_cmd run -d \
  --name "$CONTAINER" \
  --restart no \
  --cap-add NET_ADMIN \
  --device /dev/net/tun:/dev/net/tun \
  --sysctl net.ipv4.ip_forward=1 \
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

docker_cmd update --restart unless-stopped "$CONTAINER" >/dev/null 2>&1 || true
echo "AWG container deployed: $CONTAINER"
