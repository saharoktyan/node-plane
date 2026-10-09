"""Read-only host verification after agent self-uninstall.

SSH uses an independently configured root credential and a pinned known_hosts
entry. The bot's own SSH key is removed by the agent's transient unit.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess

from .removal_inventory import validate_inventory, inventory_digest


class RemovalVerificationError(ValueError):
    pass


class RemovalVerifier:
    SUCCESS = 'NODE_PLANE_REMOVED_OK'

    def __init__(self, *, local=False, ssh_target=None, ssh_identity_file=None,
                 ssh_port=22, bot_public_key=None, runner=subprocess.run):
        if local == bool(ssh_target):
            raise ValueError('choose exactly one verification target')
        if ssh_target is not None and not re.fullmatch(r'root@[A-Za-z0-9.\[\]:_-]{1,255}', ssh_target):
            raise ValueError('SSH verification target must be root@host')
        if type(ssh_port) is not int or not 1 <= ssh_port <= 65535:
            raise ValueError('invalid SSH port')
        if bot_public_key is not None:
            if (not isinstance(bot_public_key, str) or not 20 <= len(bot_public_key) <= 4096
                    or any(char in bot_public_key for char in '\r\n\0')):
                raise ValueError('invalid bot SSH public key')
            parts = bot_public_key.strip().split()
            if (len(parts) < 2 or not parts[0].startswith(('ssh-', 'ecdsa-'))
                    or not re.fullmatch(r'[A-Za-z0-9+/=]{16,}', parts[1])):
                raise ValueError('invalid bot SSH public key')
        self.local = local
        self.ssh_target = ssh_target
        self.ssh_identity_file = str(ssh_identity_file) if ssh_identity_file else None
        self.ssh_port = ssh_port
        self.bot_public_key = bot_public_key.strip() if bot_public_key is not None else None
        self.runner = runner

    def script(self, resources=None):
        if self.bot_public_key is None and not self.local:
            raise ValueError('bot SSH public key is required for final verification')
        key = shlex.quote(self.bot_public_key or '')
        resource_checks = ''
        containers = ['xray', 'amnezia-awg']
        if resources is not None:
            resources = validate_inventory(resources)
            containers = resources['containers']
            resource_checks = '\n'.join('check_absent ' + shlex.quote(path) + ' managed_path'
                                         for path in resources['paths'])
        return f'''set -eu
if [ "$(id -u)" -ne 0 ] || ! command -v systemctl >/dev/null 2>&1; then
    printf '%s\\n' 'verification_unavailable' >&2
    exit 20
fi
attempt=0
while systemctl is-active --quiet node-plane-agent.service && [ "$attempt" -lt 15 ]; do
    sleep 1
    attempt=$((attempt + 1))
done
if systemctl is-active --quiet node-plane-agent.service; then
    printf '%s\\n' 'agent_still_active' >&2
    exit 21
fi
check_absent() {{
    if [ -e "$1" ] || [ -L "$1" ]; then
        printf 'artifact_present:%s\\n' "$2" >&2
        exit 22
    fi
}}
check_absent /etc/systemd/system/node-plane-agent.service agent_unit
check_absent /etc/systemd/system/node-plane-lease-expiry.timer lease_expiry_timer
check_absent /etc/systemd/system/node-plane-lease-expiry.service lease_expiry_service
check_absent /etc/systemd/system/node-plane-leased-awg.service leased_awg_unit
check_absent /etc/systemd/system/node-plane-leased-xray.service leased_xray_unit
check_absent /usr/local/bin/node-plane-agent agent_binary
check_absent /etc/node-plane agent_config
check_absent /opt/node-plane-runtime runtime
check_absent /var/lib/node-plane-agent agent_state
check_absent /var/log/node-plane-agent agent_logs
{resource_checks}
for proc in /proc/[0-9]*/comm; do
    [ -r "$proc" ] || continue
    if [ "$(cat "$proc")" = node-plane-agen ]; then
        printf '%s\\n' 'agent_process_alive' >&2
        exit 23
    fi
done
key={key}
if [ -n "$key" ]; then
for file in /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys; do
    [ -f "$file" ] || continue
    if grep -Fqx -- "$key" "$file"; then
        printf '%s\\n' 'bot_ssh_key_present' >&2
        exit 24
    fi
done
fi
if command -v docker >/dev/null 2>&1; then
    if ! docker info >/dev/null 2>&1; then
        printf '%s\\n' 'docker_unavailable' >&2
        exit 25
    fi
    names="$(docker ps -a --format '{{{{.Names}}}}')" || exit 25
    for pattern in {' '.join(shlex.quote('^' + re.escape(name) + '(-previous-[0-9]+)?$') for name in containers)}; do
        if printf '%s\\n' "$names" | grep -Eq "$pattern"; then
            printf '%s\\n' 'managed_container_present' >&2
            exit 26
        fi
    done
fi
printf '%s:%s\\n' '{self.SUCCESS}' "$(cat /etc/machine-id)"
'''

    def identity_script(self, node_key):
        if not isinstance(node_key, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', node_key):
            raise ValueError('invalid node key')
        expected = shlex.quote(f'node_key = "{node_key}"')
        return f'''set -eu
if [ "$(id -u)" -ne 0 ] || ! systemctl is-active --quiet node-plane-agent.service; then
    printf '%s\\n' 'agent_identity_unavailable' >&2
    exit 20
fi
if ! grep -Fxq -- {expected} /etc/node-plane/agent.toml; then
    printf '%s\\n' 'agent_node_key_mismatch' >&2
    exit 21
fi
printf 'NODE_PLANE_HOST_ID:%s\\n' "$(cat /etc/machine-id)"
'''

    def _run(self, script):
        command = ['sh', '-s'] if self.local else [
            'ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=10', '-p', str(self.ssh_port)]
        if not self.local:
            if self.ssh_identity_file:
                command += ['-o', 'IdentitiesOnly=yes', '-i', self.ssh_identity_file]
            command += ['--', self.ssh_target, 'sh -s']
        try:
            result = self.runner(command, input=script, text=True,
                                 capture_output=True, timeout=40, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RemovalVerificationError('node verification unavailable') from error
        if result.returncode != 0:
            detail = result.stderr.strip()
            if not re.fullmatch(r'(?:artifact_present:[a-z_]+|[a-z_]+)', detail):
                detail = 'node verification failed'
            raise RemovalVerificationError(detail)
        return result.stdout.strip()

    @staticmethod
    def _fingerprint(machine_id):
        if not re.fullmatch(r'[0-9a-f]{32}', machine_id):
            raise RemovalVerificationError('invalid host identity')
        return hashlib.sha256(machine_id.encode('ascii')).hexdigest()

    def capture_identity(self, node_key):
        output = self._run(self.identity_script(node_key))
        prefix = 'NODE_PLANE_HOST_ID:'
        if not output.startswith(prefix):
            raise RemovalVerificationError('agent identity unavailable')
        return self._fingerprint(output[len(prefix):])

    def capture_resources(self, node_key, expected_fingerprint):
        # Read directly through the independently authenticated host connection,
        # while the agent and its configuration still exist. Never execute env.
        reader = Path(__file__).with_name('removal_inventory.py').read_text()
        script = self.identity_script(node_key) + '''
pid="$(systemctl show node-plane-agent.service --property=MainPID --value)"
binary="$(readlink "/proc/$pid/exe")"
python3 - "$binary" <<'NODE_PLANE_INVENTORY_PY'
''' + reader + '''
import sys
print('NODE_PLANE_RESOURCES:' + json.dumps(read_inventory('/etc/node-plane/agent.toml', sys.argv[1]), sort_keys=True))
NODE_PLANE_INVENTORY_PY
'''
        output = self._run(script).splitlines()
        if (len(output) != 2 or not output[0].startswith('NODE_PLANE_HOST_ID:')
                or self._fingerprint(output[0].partition(':')[2]) != expected_fingerprint
                or not output[1].startswith('NODE_PLANE_RESOURCES:')):
            raise RemovalVerificationError('resource inventory unavailable')
        try:
            return validate_inventory(json.loads(output[1].partition(':')[2]))
        except (ValueError, TypeError) as error:
            raise RemovalVerificationError('invalid resource inventory') from error

    def verify(self, node_key, expected_fingerprint, resources=None):
        return self._verify_script(node_key, expected_fingerprint, resources, self.script(resources))

    def _verify_script(self, node_key, expected_fingerprint, resources, script):
        if not re.fullmatch(r'[0-9a-f]{64}', expected_fingerprint):
            raise ValueError('invalid expected host fingerprint')
        output = self._run(script)
        prefix = self.SUCCESS + ':'
        if not output.startswith(prefix):
            raise RemovalVerificationError('node verification failed')
        fingerprint = self._fingerprint(output[len(prefix):])
        if fingerprint != expected_fingerprint:
            raise RemovalVerificationError('host identity changed')
        return {'method': 'local' if self.local else 'ssh',
                'target': 'local' if self.local else self.ssh_target,
                'host_fingerprint': fingerprint,
                'checked_at': datetime.now(timezone.utc).isoformat(),
                'inventory_digest': inventory_digest(resources) if resources is not None else None,
                'result': 'agent_and_standard_artifacts_absent'}
