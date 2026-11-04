#!/bin/sh
# Installs the Node Plane agent on this server for token enrollment.
#
# The controller shows the command with a one-time token; run it as root.
# No SSH key or sudo access is given to the controller: the agent generates
# its own key and waits until the controller dials in, proves the token and
# issues its certificate. The agent port then stays open under mutual TLS,
# or allows only the controller's address when the controller asks.
set -eu

REPO="saharoktyan/node-plane"
PORT=50061
NODE_KEY=""
TOKEN=""
RELEASE=""
AGENT_SHA256=""
AGENT_URL=""
ASSET="node-plane-agent-linux-amd64"

fail() {
  echo "Node Plane agent installation failed: $1" >&2
  exit 1
}

usage() {
  cat >&2 <<'USAGE'
Usage: install_agent.sh --node-key KEY --token TOKEN --release TAG --agent-sha256 SHA256 [--port PORT]
                        [--agent-url URL]
USAGE
  exit 2
}

while [ $# -gt 0 ]; do
  [ $# -ge 2 ] || usage
  case "$1" in
    --node-key) NODE_KEY="$2" ;;
    --token) TOKEN="$2" ;;
    --release) RELEASE="$2" ;;
    --agent-sha256) AGENT_SHA256="$2" ;;
    --agent-url) AGENT_URL="$2" ;;
    --port) PORT="$2" ;;
    *) usage ;;
  esac
  shift 2
done

printf '%s' "$NODE_KEY" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$' || fail "invalid node key"
printf '%s' "$TOKEN" | grep -Eq '^[0-9]+\.[0-9a-f]{64}$' || fail "invalid enrollment token"
printf '%s' "$AGENT_SHA256" | grep -Eq '^[0-9a-f]{64}$' || fail "invalid agent checksum"
printf '%s' "$PORT" | grep -Eq '^[0-9]{1,5}$' && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] \
  || fail "invalid agent port"
if [ -z "$AGENT_URL" ]; then
  printf '%s' "$RELEASE" | grep -Eq '^v[0-9A-Za-z.+-]+$' || fail "invalid release"
  AGENT_URL="https://github.com/${REPO}/releases/download/${RELEASE}/${ASSET}.tar.gz"
fi

[ "$(id -u)" -eq 0 ] || fail "run it as root, for example with sudo"
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] || fail "Linux x86_64 is required"
command -v systemctl >/dev/null 2>&1 || fail "systemd is required"
for tool in tar sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || fail "$tool is required"
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
echo "Downloading the agent..."
if command -v curl >/dev/null 2>&1; then
  curl -fsSL --retry 3 -o "$work/agent.tar.gz" "$AGENT_URL" || fail "download failed: $AGENT_URL"
elif command -v wget >/dev/null 2>&1; then
  wget -q -O "$work/agent.tar.gz" "$AGENT_URL" || fail "download failed: $AGENT_URL"
else
  fail "curl or wget is required"
fi
echo "$AGENT_SHA256  $work/agent.tar.gz" | sha256sum -c - >/dev/null 2>&1 \
  || fail "the downloaded agent does not match the controller's checksum"
tar -xzf "$work/agent.tar.gz" -C "$work" "$ASSET" || fail "unexpected agent archive"
install -m 0755 "$work/$ASSET" /usr/local/bin/node-plane-agent

# Speed limits need these; the agent reports when they are missing.
missing=""
command -v tc >/dev/null 2>&1 || missing="$missing iproute2"
command -v nft >/dev/null 2>&1 || missing="$missing nftables"
command -v nsenter >/dev/null 2>&1 || missing="$missing util-linux"
if [ -n "$missing" ] && command -v apt-get >/dev/null 2>&1; then
  # shellcheck disable=SC2086 # package names
  DEBIAN_FRONTEND=noninteractive apt-get install -y $missing >/dev/null 2>&1 \
    || { apt-get update >/dev/null 2>&1 \
      && DEBIAN_FRONTEND=noninteractive apt-get install -y $missing >/dev/null 2>&1; } \
    || echo "Speed limits need these packages:$missing" >&2
fi

umask 077
mkdir -p /etc/node-plane/tls
chmod 0700 /etc/node-plane/tls
# The node this server had: the controller refuses to take the agent of
# another of its active nodes, and archives another controller's journal.
if [ -f /etc/node-plane/agent.toml ]; then
  sed -n 's/^node_key *= *"\(.*\)" *$/\1/p' /etc/node-plane/agent.toml | head -n 1 \
    > /etc/node-plane/tls/enrollment.previous-node
fi
# A new key and certificate come with enrollment; the CA stays until then
# so the agent can tell which controller had it.
rm -f /etc/node-plane/tls/server.key /etc/node-plane/tls/server.crt
cat > /etc/node-plane/agent.toml <<EOF
node_key = "${NODE_KEY}"
listen_addr = "0.0.0.0:${PORT}"
tls_certificate_path = "/etc/node-plane/tls/server.crt"
tls_key_path = "/etc/node-plane/tls/server.key"
tls_client_ca_path = "/etc/node-plane/tls/ca.crt"
EOF
chmod 0644 /etc/node-plane/agent.toml
printf '%s\n' "$TOKEN" > /etc/node-plane/tls/enrollment.token

if command -v ufw >/dev/null 2>&1 && LC_ALL=C ufw status 2>/dev/null | head -n 1 | grep -q '^Status: active'; then
  # Until enrollment the token protects the port; enrollment then applies
  # the access the administrator chose.
  ufw allow "${PORT}/tcp" comment 'Node Plane agent enrollment' >/dev/null \
    || fail "could not open the agent port in ufw"
fi

cat > /etc/systemd/system/node-plane-agent.service <<EOF
[Unit]
Description=Node Plane Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=NODE_AGENT_CONFIG_PATH=/etc/node-plane/agent.toml
Environment=NODE_AGENT_NODE_KEY=${NODE_KEY}
Environment=NODE_AGENT_LISTEN_ADDR=0.0.0.0:${PORT}
ExecStart=/usr/local/bin/node-plane-agent
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 /etc/systemd/system/node-plane-agent.service
# The agent owns its systemd hardening drop-in; see docs/agent.md.
timeout 30 /usr/local/bin/node-plane-agent harden >/dev/null
systemctl daemon-reload
systemctl enable node-plane-agent >/dev/null 2>&1 || fail "could not enable the agent service"
systemctl restart node-plane-agent || fail "could not start the agent service"
systemctl is-active --quiet node-plane-agent || fail "the agent service is not running"

echo "The Node Plane agent is installed and waits for the controller on TCP port ${PORT}."
echo "If the server's provider has its own firewall, allow that port from the controller."
echo "Then finish adding the node in Node Plane."
