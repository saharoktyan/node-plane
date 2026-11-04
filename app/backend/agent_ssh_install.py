"""Install command over the controller's own SSH access, once.

"Install over SSH" in the bot: the controller logs in with its key, runs the
same one-time install command an administrator would paste, and logs out.
The agent then enrolls by its token like any other; the controller keeps no
SSH session and never uses SSH for that node again.
"""
import os
import shlex
import subprocess

INSTALLER = '/tmp/node-plane-agent-installer.sh'
TIMEOUT = 900


def script(steps):
    """Root or passwordless sudo; the bot cannot ask for a sudo password."""
    installer, arguments = steps['installer'], steps['arguments']
    if not installer['url'].startswith('https://'):
        raise ValueError('installer must be downloaded over HTTPS')
    run = ' '.join(shlex.quote(argument) for argument in arguments)
    return (f'set -e\n'
            f'if [ "$(id -u)" = 0 ]; then as_root=; '
            f'elif sudo -n true 2>/dev/null; then as_root="sudo -n"; '
            f'else echo "NODE_PLANE_SSH_SUDO_REQUIRED" >&2; exit 41; fi\n'
            f'curl -fsSLo {INSTALLER} {shlex.quote(installer["url"])}\n'
            f"echo '{installer['sha256']}  {INSTALLER}' | sha256sum -c - >/dev/null\n"
            f'$as_root sh {INSTALLER} {run}\n')


def failure(returncode, output):
    """A fixed failure code for the operator; raw output is not shown."""
    if returncode == 0:
        return None
    if 'Permission denied (publickey' in output:
        return 'ssh_authentication'
    if 'Host key verification failed' in output or 'REMOTE HOST IDENTIFICATION HAS CHANGED' in output:
        return 'ssh_host_key'
    if 'NODE_PLANE_SSH_SUDO_REQUIRED' in output:
        return 'ssh_prerequisites'
    if returncode == 255:
        return 'ssh_unreachable'
    return 'agent_install_failed'


def run(ssh_target, steps, runner=subprocess.run):
    """Run the installer on `ssh_target`; the failure code, or None."""
    from config import SSH_KEY, SSH_KNOWN_HOSTS_PATH
    key = os.environ.get('SSH_KEY') or SSH_KEY
    known_hosts = os.environ.get('SSH_KNOWN_HOSTS_PATH') or SSH_KNOWN_HOSTS_PATH
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
               '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
               '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={known_hosts}',
               '-o', 'IdentitiesOnly=yes', '-i', key, '--', ssh_target, 'sh -s']
    try:
        result = runner(command, input=script(steps), capture_output=True, text=True,
                        timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return 'agent_install_timeout'
    except OSError:
        return 'ssh_unavailable'
    return failure(result.returncode, (result.stdout or '') + (result.stderr or ''))
