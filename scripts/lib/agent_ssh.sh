#!/usr/bin/env bash
# Run agent installer commands as root or with noninteractive sudo. Keep stdin
# untouched so callers can stream installation scripts through `bash -s`.
agent_ssh() {
  local count=$# target command payload quoted
  (( count >= 2 )) || return 2
  target="${@:count-1:1}"
  command="${@:count:1}"
  payload='sudo() {
    if [ "$EUID" -eq 0 ]; then
      "$@"
    else
      command sudo -n "$@"
    fi
  }
  export -f sudo
  '
  payload+="$command"
  # POSIX shell quoting works even when the remote login shell is not bash.
  quoted="${payload//\'/\'\\\'\'}"
  ssh "${@:1:count-2}" "$target" "bash -c '$quoted'"
}

# Stream this script over the authenticated SSH connection. SSH_CONNECTION is
# supplied by sshd on the node, so NAT uses the controller's actual source IP.
agent_firewall_script() {
  cat <<'FIREWALL'
set -euo pipefail
fail() {
  echo "AGENT_FIREWALL_FAILED: $1" >&2
  exit 1
}
port="$(python3 - "${1:-}" <<'PY'
import sys
value = sys.argv[1]
if not value.isascii() or not value.isdecimal() or len(value) > 5 or not 1 <= int(value) <= 65535:
    raise SystemExit('Invalid agent port')
print(int(value))
PY
)" || fail 'Invalid agent port.'
if ! command -v ufw >/dev/null 2>&1; then
  echo 'UFW is not installed; agent port firewall configuration skipped.'
  exit 0
fi
status="$(sudo env LC_ALL=C ufw status)" || fail 'Could not inspect UFW.'
case "${status%%$'\n'*}" in
  'Status: inactive')
    echo 'UFW is inactive; keeping it disabled.'
    exit 0
    ;;
  'Status: active') ;;
  *) fail 'Unrecognized UFW status.' ;;
esac
controller_ip="$(python3 - "${SSH_CONNECTION:-}" <<'PY'
import ipaddress
import sys
parts = sys.argv[1].split()
if len(parts) != 4:
    raise SystemExit('SSH connection source is missing')
try:
    address = ipaddress.ip_address(parts[0])
except ValueError:
    raise SystemExit('SSH connection source is invalid')
if address.is_unspecified or address.is_multicast or '%' in str(address):
    raise SystemExit('SSH connection source is not a unicast IP address')
print(address)
PY
)" || fail 'Cannot determine controller source IP; refusing an unrestricted rule.'
sudo ufw allow proto tcp from "$controller_ip" to any port "$port" comment 'Node Plane agent' \
  || fail 'Could not allow the controller through UFW.'
echo "Agent TCP port ${port} allowed from controller ${controller_ip}."
FIREWALL
}
