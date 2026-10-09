import subprocess
import unittest
import hashlib

from backend.removal_verifier import RemovalVerifier, RemovalVerificationError


class RemovalVerifierTests(unittest.TestCase):
    KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAExample bot-key'
    MACHINE_ID = '0123456789abcdef0123456789abcdef'
    FINGERPRINT = hashlib.sha256(MACHINE_ID.encode()).hexdigest()

    def test_ssh_verification_uses_strict_host_key_and_independent_identity(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs))
            marker = ('NODE_PLANE_HOST_ID:' if 'agent.toml' in kwargs['input']
                      else 'NODE_PLANE_REMOVED_OK:')
            return subprocess.CompletedProcess(command, 0, marker + self.MACHINE_ID + '\n', '')
        verifier = RemovalVerifier(ssh_target='root@node.example', ssh_port=2222,
            ssh_identity_file='/tmp/admin-key', bot_public_key=self.KEY, runner=runner)
        self.assertEqual(verifier.capture_identity('node'), self.FINGERPRINT)
        evidence = verifier.verify('node', self.FINGERPRINT)
        self.assertEqual(evidence['result'], 'agent_and_standard_artifacts_absent')
        self.assertEqual(evidence['host_fingerprint'], self.FINGERPRINT)
        command, kwargs = calls[0]
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertIn('/tmp/admin-key', command)
        self.assertIn('2222', command)
        self.assertIn('node-plane-agent.service', kwargs['input'])
        self.assertIn('node_key = "node"', kwargs['input'])
        self.assertIn('bot_ssh_key_present', calls[1][1]['input'])
        self.assertIn('node-plane-lease-expiry.timer', calls[1][1]['input'])
        self.assertIn('node-plane-lease-expiry.service', calls[1][1]['input'])
        self.assertEqual(evidence['target'], 'root@node.example')

    def test_only_local_verification_can_omit_ssh_key(self):
        verifier = RemovalVerifier(local=True, runner=lambda command, **kwargs:
            subprocess.CompletedProcess(command, 0, 'NODE_PLANE_REMOVED_OK:' + self.MACHINE_ID + '\n', ''))
        self.assertIn("key=''", verifier.script())
        self.assertEqual(verifier.verify('node', self.FINGERPRINT)['target'], 'local')
        with self.assertRaisesRegex(ValueError, 'public key is required'):
            RemovalVerifier(ssh_target='root@node.example').script()

    def test_unavailable_or_failed_host_check_never_succeeds(self):
        def failed(command, **kwargs):
            return subprocess.CompletedProcess(command, 22, '', 'artifact_present:agent_binary\n')
        verifier = RemovalVerifier(local=True, bot_public_key=self.KEY, runner=failed)
        with self.assertRaisesRegex(RemovalVerificationError, 'artifact_present:agent_binary'):
            verifier.verify('node', self.FINGERPRINT)
        def unreachable(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 40)
        verifier.runner = unreachable
        with self.assertRaises(RemovalVerificationError):
            verifier.verify('node', self.FINGERPRINT)

    def test_wrong_agent_or_changed_host_cannot_verify_removal(self):
        def wrong_agent(command, **kwargs):
            return subprocess.CompletedProcess(command, 21, '', 'agent_node_key_mismatch\n')
        verifier = RemovalVerifier(local=True, runner=wrong_agent)
        with self.assertRaisesRegex(RemovalVerificationError, 'agent_node_key_mismatch'):
            verifier.capture_identity('node')
        def changed_host(command, **kwargs):
            return subprocess.CompletedProcess(command, 0,
                'NODE_PLANE_REMOVED_OK:ffffffffffffffffffffffffffffffff\n', '')
        verifier = RemovalVerifier(local=True, bot_public_key=self.KEY, runner=changed_host)
        with self.assertRaisesRegex(RemovalVerificationError, 'host identity changed'):
            verifier.verify('node', self.FINGERPRINT)

    def test_invalid_target_or_key_is_rejected(self):
        for target in ('user@host', 'root@host;touch /tmp/x', '-oProxyCommand=bad'):
            with self.assertRaises(ValueError):
                RemovalVerifier(ssh_target=target, bot_public_key=self.KEY)
        with self.assertRaises(ValueError):
            RemovalVerifier(local=True, bot_public_key=self.KEY + '\nother key')


if __name__ == '__main__':
    unittest.main()
