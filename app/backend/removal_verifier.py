"""Read-only host verification after agent self-uninstall.

SSH uses an independently configured root credential and a pinned known_hosts
entry. The bot's own SSH key is removed by the agent's transient unit.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import shlex
import subprocess


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

    def script(self):
        if self.bot_public_key is None:
            raise ValueError('bot SSH public key is required for final verification')
        key = shlex.quote(self.bot_public_key)
        return f'''set -eu
if [ "$(id -u)" -ne 0 ] || ! command -v systemctl >/dev/null 2>&1; then
    printf '%s\\n' 'verification_unavailable' >&2
    exit 20
fi
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
check_absent /usr/local/bin/node-plane-agent agent_binary
check_absent /etc/node-plane agent_config
check_absent /opt/node-plane-runtime runtime
check_absent /var/lib/node-plane-agent agent_state
check_absent /var/log/node-plane-agent agent_logs
for proc in /proc/[0-9]*/comm; do
    [ -r "$proc" ] || continue
    if [ "$(cat "$proc")" = node-plane-agen ]; then
        printf '%s\\n' 'agent_process_alive' >&2
        exit 23
    fi
done
key={key}
for file in /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys; do
    [ -f "$file" ] || continue
    if grep -Fqx -- "$key" "$file"; then
        printf '%s\\n' 'bot_ssh_key_present' >&2
        exit 24
    fi
done
if command -v docker >/dev/null 2>&1; then
    if ! docker info >/dev/null 2>&1; then
        printf '%s\\n' 'docker_unavailable' >&2
        exit 25
    fi
    for container in xray amnezia-awg; do
        if docker container inspect "$container" >/dev/null 2>&1; then
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

    def verify(self, node_key, expected_fingerprint):
        if not re.fullmatch(r'[0-9a-f]{64}', expected_fingerprint):
            raise ValueError('invalid expected host fingerprint')
        output = self._run(self.script())
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
                'result': 'agent_and_standard_artifacts_absent'}
