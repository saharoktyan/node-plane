import subprocess
import unittest

from backend.removal_verifier import RemovalVerifier, RemovalVerificationError


class RemovalVerifierTests(unittest.TestCase):
    KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAExample bot-key'

    def test_ssh_verification_uses_strict_host_key_and_independent_identity(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, 'NODE_PLANE_REMOVED_OK\n', '')
        verifier = RemovalVerifier(ssh_target='root@node.example', ssh_port=2222,
            ssh_identity_file='/tmp/admin-key', bot_public_key=self.KEY, runner=runner)
        evidence = verifier.verify('node')
        self.assertEqual(evidence['result'], 'agent_and_standard_artifacts_absent')
        command, kwargs = calls[0]
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertIn('/tmp/admin-key', command)
        self.assertIn('2222', command)
        self.assertIn('node-plane-agent.service', kwargs['input'])
        self.assertIn('bot_ssh_key_present', kwargs['input'])
        self.assertEqual(evidence['target'], 'root@node.example')

    def test_unavailable_or_failed_host_check_never_succeeds(self):
        def failed(command, **kwargs):
            return subprocess.CompletedProcess(command, 22, '', 'artifact_present:agent_binary\n')
        verifier = RemovalVerifier(local=True, bot_public_key=self.KEY, runner=failed)
        with self.assertRaisesRegex(RemovalVerificationError, 'artifact_present:agent_binary'):
            verifier.verify('node')
        def unreachable(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 40)
        verifier.runner = unreachable
        with self.assertRaises(RemovalVerificationError):
            verifier.verify('node')

    def test_invalid_target_or_key_is_rejected(self):
        for target in ('user@host', 'root@host;touch /tmp/x', '-oProxyCommand=bad'):
            with self.assertRaises(ValueError):
                RemovalVerifier(ssh_target=target, bot_public_key=self.KEY)
        with self.assertRaises(ValueError):
            RemovalVerifier(local=True, bot_public_key=self.KEY + '\nother key')


if __name__ == '__main__':
    unittest.main()
