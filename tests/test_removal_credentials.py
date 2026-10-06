import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from backend.removal_credentials import ManagedRemovalVerifier, credential_directory
from backend.removal_verifier import RemovalVerificationError


class ManagedRemovalCredentialTests(unittest.TestCase):
    MACHINE = '0123456789abcdef0123456789abcdef'
    BOT_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAExample bot-key'

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'verification'
        self.calls = []

        def runner(command, **kwargs):
            self.calls.append((command, kwargs))
            if command[0] == 'ssh-keygen':
                return subprocess.run(command, **kwargs)
            return subprocess.CompletedProcess(command, 0, 'NODE_PLANE_HOST_ID:' + self.MACHINE, '')

        self.verifier = ManagedRemovalVerifier(original_key='/tmp/original-key',
            directory=self.directory, ssh_target='root@node.example',
            bot_public_key=self.BOT_KEY, runner=runner)

    def test_preparation_installs_and_checks_distinct_persistent_key(self):
        self.verifier.prepare('node')
        private = self.directory / 'identity'
        public = private.with_name('identity.pub').read_text().strip()
        self.assertNotEqual(public, self.BOT_KEY)
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
        install = [kwargs['input'] for _, kwargs in self.calls if 'touch "$file"' in kwargs.get('input', '')]
        self.assertEqual(len(install), 1)
        self.assertIn(public, install[0])
        self.assertIn('node_key = "node"', install[0])
        self.assertIn(str(private), self.calls[-1][0])
        self.verifier.prepare('node')
        self.assertEqual(private.with_name('identity.pub').read_text().strip(), public)
        self.assertEqual(sum(command[0] == 'ssh-keygen' for command, _ in self.calls), 1)

    def test_symlink_credential_directory_is_rejected_before_writing(self):
        elsewhere = Path(self.temporary.name) / 'elsewhere'
        elsewhere.mkdir()
        self.directory.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(RemovalVerificationError, 'unsafe verification credential directory'):
            self.verifier.prepare('node')
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_wrong_host_blocks_before_authorizing_verification_key(self):
        self.verifier.runner = lambda command, **kwargs: subprocess.CompletedProcess(command, 21, '', 'agent_node_key_mismatch')
        with self.assertRaises(RemovalVerificationError):
            self.verifier.prepare('node')
        self.assertFalse(self.directory.exists())

    def test_final_check_removes_only_temporary_key_and_only_after_success(self):
        self.verifier.prepare('node')
        public = (self.directory / 'identity.pub').read_text().strip()
        authorized = Path(self.temporary.name) / 'authorized_keys'
        authorized.write_text('unrelated key\n' + public + '\n')
        machine = Path('/etc/machine-id').read_text().strip()
        fingerprint = hashlib.sha256(machine.encode()).hexdigest()

        def runner(command, **kwargs):
            script = kwargs['input'].replace('/root/.ssh/authorized_keys', str(authorized))
            return subprocess.run(['sh', '-s'], **{**kwargs, 'input': script})

        self.verifier.runner = runner
        with patch.object(self.verifier, 'script', return_value='set -eu\nfalse\n'):
            with self.assertRaises(RemovalVerificationError):
                self.verifier.verify('node', fingerprint)
        self.assertIn(public, authorized.read_text())
        marker = f"set -eu\nprintf '%s\\n' 'NODE_PLANE_REMOVED_OK:{machine}'\n"
        with patch.object(self.verifier, 'script', return_value=marker):
            result = self.verifier.verify('node', fingerprint)
        self.assertEqual(result['host_fingerprint'], fingerprint)
        self.assertEqual(authorized.read_text(), 'unrelated key\n')
        self.assertEqual(authorized.stat().st_mode & 0o777, 0o600)
        self.verifier.discard()
        self.assertFalse(self.directory.exists())

    def test_failed_identity_never_removes_temporary_key(self):
        self.verifier.prepare('node')
        self.verifier.runner = lambda command, **kwargs: subprocess.CompletedProcess(command, 0, 'f' * 32, '')
        with self.assertRaisesRegex(RemovalVerificationError, 'host identity changed'):
            self.verifier.verify('node', 'a' * 64)
        self.assertTrue((self.directory / 'identity').exists())

    def test_credential_namespace_follows_controller_and_target(self):
        first = credential_directory('/tmp/ssh/id', 'controller-one', 'root@host')
        self.assertNotEqual(first, credential_directory('/tmp/ssh/id', 'controller-two', 'root@host'))
        self.assertNotEqual(first, credential_directory('/tmp/ssh/id', 'controller-one', 'root@other'))


if __name__ == '__main__':
    unittest.main()
