"""Operation-owned SSH verification credentials survive agent uninstallation."""
import fcntl
import hashlib
import os
from pathlib import Path
import shlex
import tempfile

from .removal_verifier import RemovalVerifier, RemovalVerificationError


class ManagedRemovalVerifier(RemovalVerifier):
    def __init__(self, *, original_key, directory, **kwargs):
        self.directory = Path(directory)
        self.original_key = str(original_key)
        super().__init__(ssh_identity_file=self.directory / 'identity', **kwargs)

    def prepare(self, node_key):
        # Do not install a credential on a different host/agent by mistake.
        original = RemovalVerifier(ssh_target=self.ssh_target,
            ssh_identity_file=self.original_key, ssh_port=self.ssh_port,
            bot_public_key=self.bot_public_key, runner=self.runner)
        fingerprint = original.capture_identity(node_key)
        if any(path.is_symlink() for path in (self.directory, *self.directory.parents)):
            raise RemovalVerificationError('unsafe verification credential directory')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise RemovalVerificationError('unsafe verification credential directory')
        os.chmod(self.directory, 0o700)
        with os.fdopen(os.open(self.directory / 'lock',
                              os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            private = self.directory / 'identity'
            if private.is_symlink() or Path(str(private) + '.pub').is_symlink():
                raise RemovalVerificationError('unsafe verification credential file')
            if not private.exists():
                with tempfile.TemporaryDirectory(dir=self.directory) as temporary:
                    generated = Path(temporary) / 'identity'
                    result = self.runner(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '',
                        '-C', 'node-plane-removal-verification', '-f', str(generated)],
                        capture_output=True, text=True, timeout=15, check=False)
                    if result.returncode:
                        raise RemovalVerificationError('verification credential generation failed')
                    for path in (generated, Path(str(generated) + '.pub')):
                        with path.open('rb') as handle:
                            os.fsync(handle.fileno())
                    # Private key is the completion marker, published last.
                    os.replace(str(generated) + '.pub', str(private) + '.pub')
                    os.replace(generated, private)
                    descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            os.chmod(private, 0o600)
            public = Path(str(private) + '.pub').read_text().strip()
            # Reuse public-key validation before passing it into a shell script.
            RemovalVerifier(ssh_target=self.ssh_target, bot_public_key=public)
            original._run(original.identity_script(node_key) + f'''
umask 077
mkdir -p /root/.ssh
chmod 700 /root/.ssh
key={shlex.quote(public)}
file=/root/.ssh/authorized_keys
[ ! -L "$file" ] || exit 22
touch "$file"
chmod 600 "$file"
grep -Fqx -- "$key" "$file" || printf '\\n%s\\n' "$key" >> "$file"
''')
            if self.capture_identity(node_key) != fingerprint:
                raise RemovalVerificationError('host identity changed')

    def verify(self, node_key, expected_fingerprint, resources=None):
        # Check identity and every managed artifact before removing the temporary
        # authorized key. An established SSH session remains usable afterwards.
        script = self.script(resources)
        public = Path(self.ssh_identity_file + '.pub').read_text().strip()
        RemovalVerifier(ssh_target=self.ssh_target, bot_public_key=public)
        machine_id = self._run("cat /etc/machine-id")
        if self._fingerprint(machine_id) != expected_fingerprint:
            raise RemovalVerificationError('host identity changed')
        suffix = f'''
[ "$(cat /etc/machine-id)" = {shlex.quote(machine_id)} ] || exit 23
python3 - {shlex.quote(public)} <<'NODE_PLANE_REMOVE_VERIFICATION_KEY'
from pathlib import Path
import os
import sys
import tempfile
path = Path('/root/.ssh/authorized_keys')
if path.is_symlink():
    raise RuntimeError('authorized keys is a symlink')
key = sys.argv[1]
lines = path.read_text().splitlines(keepends=True)
with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as temporary:
    temporary.writelines(line for line in lines if line.rstrip('\\r\\n') != key)
    temporary.flush()
    os.fsync(temporary.fileno())
    name = temporary.name
os.replace(name, path)
if key in path.read_text().splitlines():
    raise RuntimeError('verification key still present')
NODE_PLANE_REMOVE_VERIFICATION_KEY
'''
        # Run final artifact checks and key removal in the same SSH session.
        # A failed/ambiguous response never certifies retirement.
        return self._verify_script(node_key, expected_fingerprint, resources, script + suffix)

    def discard(self):
        for name in ('identity', 'identity.pub', 'lock'):
            (self.directory / name).unlink(missing_ok=True)
        self.directory.rmdir()


def credential_directory(original_key, controller_id, target):
    name = hashlib.sha256(target.encode()).hexdigest()
    return Path(original_key).parent / 'removal-verification' / controller_id / name
