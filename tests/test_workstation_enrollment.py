"""Controller-side SSH enrollment verification without real hosts or credentials."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import pathlib
import re
import stat
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'rust/node-plane-cli/src/enrollment.rs'
MATCH = re.search(r'const CONTROLLER_LOGIN_SCRIPT: &str = r#"\n(.*?)\n"#;', SOURCE.read_text(), re.S)
assert MATCH is not None
HELPER: dict[str, object] = {'__name__': 'workstation_enrollment_fixture'}
exec(compile(MATCH.group(1), '<controller-login-helper>', 'exec'), HELPER)


def public_key(fill: int = 1) -> str:
    algorithm = b'ssh-ed25519'
    wire = len(algorithm).to_bytes(4, 'big') + algorithm + (32).to_bytes(4, 'big') + bytes([fill]) * 32
    return 'ssh-ed25519 ' + base64.b64encode(wire).decode('ascii')


class ControllerEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='node-plane-enrollment-test-')
        self.root = pathlib.Path(self.directory.name)
        self.root.chmod(0o700)
        self.key = self.root / 'controller-private-key'
        self.key.write_text('fixture key never retrieved by workstation\n')
        self.key.chmod(0o600)
        self.hosts = self.root / 'known_hosts'
        self.payload = {'host': 'node.example', 'port': 22, 'user': 'root', 'host_key': public_key()}
        self.calls = []

    def tearDown(self):
        self.directory.cleanup()

    def runner(self, arguments):
        self.calls.append(arguments)
        self.assertEqual(arguments[:3], ['ssh', '-F', '/dev/null'])
        self.assertIn('BatchMode=yes', arguments)
        self.assertIn('IdentitiesOnly=yes', arguments)
        self.assertIn('StrictHostKeyChecking=yes', arguments)
        self.assertIn('GlobalKnownHostsFile=/dev/null', arguments)
        self.assertIn('UpdateHostKeys=no', arguments)
        self.assertEqual(arguments[-3:], ['--', self.payload['host'].lower(), 'printf NODE_PLANE_KEY_VERIFIED'])
        temporary = pathlib.Path(next(x.split('=', 1)[1] for x in arguments if x.startswith('UserKnownHostsFile=')))
        self.assertEqual(stat.S_IMODE(temporary.stat().st_mode), 0o600)
        self.assertNotEqual(temporary, self.hosts)
        endpoints = f'[{self.payload["host"].lower()}]:{self.payload["port"]}'
        if self.payload['port'] == 22:
            endpoints = self.payload['host'].lower() + ',' + endpoints
        self.assertEqual(temporary.read_text(), endpoints + ' ' + self.payload['host_key'] + '\n')
        self.assertEqual(self.key.read_text(), 'fixture key never retrieved by workstation\n')
        return 0, b'NODE_PLANE_KEY_VERIFIED'

    def verify(self, runner=None, fallback=None):
        HELPER['verify'](self.payload, self.key, self.hosts, fallback, runner or self.runner)

    def test_verification_pins_exact_host_and_preserves_other_lines_idempotently(self):
        original = '# other administrators\nother.example ' + public_key(2) + '\n\n\n'
        self.hosts.write_text(original)
        self.hosts.chmod(0o644)
        self.verify()
        contents = self.hosts.read_text()
        self.assertTrue(contents.startswith(original))
        self.assertEqual(stat.S_IMODE(self.hosts.stat().st_mode), 0o644)
        self.verify()
        self.assertEqual(self.hosts.read_text(), contents)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(list(self.root.glob('.node-plane-login-*')), [])

    def test_nonstandard_port_and_ipv6_use_bracketed_host_only(self):
        self.payload.update(host='2001:db8::1', port=2222)
        self.verify()
        self.assertEqual(self.hosts.read_text(), '[2001:db8::1]:2222 ' + public_key() + '\n')
        self.verify()
        self.assertEqual(len(self.hosts.read_text().splitlines()), 1)

    def test_failed_or_forced_command_login_never_persists_trust(self):
        for result in [(255, b''), (0, b'forced command output'), (0, b'NODE_PLANE_KEY_VERIFIED\n')]:
            with self.subTest(result=result):
                with self.assertRaisesRegex(HELPER['EnrollmentError'], 'login_failed'):
                    self.verify(runner=lambda arguments: result)
                self.assertFalse(self.hosts.exists())
        self.assertEqual(list(self.root.glob('.node-plane-login-*')), [])

    def test_conflicting_existing_pin_refuses_before_login(self):
        original = 'node.example ' + public_key(2) + '\n'
        self.hosts.write_text(original)
        with self.assertRaisesRegex(HELPER['EnrollmentError'], 'host_key_conflict'):
            self.verify()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.hosts.read_text(), original)

    def test_hashed_and_wildcard_pins_detect_conflict_without_replacing_other_hosts(self):
        salt = b'fixture-hash-salt'
        value = hmac.new(salt, b'node.example', hashlib.sha1).digest()
        hashed = '|1|' + base64.b64encode(salt).decode() + '|' + base64.b64encode(value).decode()
        for host in (hashed, '*.example', '[node.example]:22'):
            original = host + ' ' + public_key(2) + '\n'
            self.hosts.write_text(original)
            with self.subTest(host=host), self.assertRaisesRegex(HELPER['EnrollmentError'], 'host_key_conflict'):
                self.verify()
            self.assertEqual(self.hosts.read_text(), original)
        self.assertEqual(self.calls, [])

    def test_revoked_and_certificate_authority_entries_are_not_silently_accepted(self):
        for marker in ('@revoked', '@cert-authority'):
            self.hosts.write_text(marker + ' node.example ' + public_key() + '\n')
            with self.subTest(marker=marker), self.assertRaisesRegex(HELPER['EnrollmentError'], 'host_key_conflict'):
                self.verify()

    def test_matching_hashed_pin_is_reused_and_negation_is_respected(self):
        salt = b'fixture-hash-salt'
        value = hmac.new(salt, b'node.example', hashlib.sha1).digest()
        hashed = '|1|' + base64.b64encode(salt).decode() + '|' + base64.b64encode(value).decode()
        original = hashed + ' ' + public_key() + '\n'
        self.hosts.write_text(original)
        self.verify()
        self.assertEqual(self.hosts.read_text(), original)
        self.hosts.write_text('!node.example,*.example ' + public_key(2) + '\n')
        self.verify()
        self.assertIn('node.example,[node.example]:22 ' + public_key(), self.hosts.read_text())

    def test_both_configured_and_fallback_known_hosts_are_pinned_with_one_directory_lock(self):
        fallback = self.root / 'fallback_known_hosts'
        fallback.write_text('other.example ' + public_key(2) + '\n')
        self.verify(fallback=fallback)
        self.assertIn('node.example,[node.example]:22 ', self.hosts.read_text())
        self.assertTrue(fallback.read_text().startswith('other.example '))
        self.assertIn('node.example,[node.example]:22 ', fallback.read_text())

    def test_unsafe_key_or_known_hosts_files_are_refused(self):
        self.key.chmod(0o644)
        with self.assertRaisesRegex(HELPER['EnrollmentError'], 'unsafe_state'):
            self.verify()
        self.key.chmod(0o600)
        external = self.root / 'external'
        external.write_text('preserved')
        self.hosts.symlink_to(external)
        with self.assertRaisesRegex(HELPER['EnrollmentError'], 'unsafe_state'):
            self.verify()
        self.assertEqual(external.read_text(), 'preserved')
        self.hosts.unlink()
        os.link(external, self.hosts)
        with self.assertRaisesRegex(HELPER['EnrollmentError'], 'unsafe_state'):
            self.verify()

    def test_invalid_targets_never_run_ssh_or_touch_trust(self):
        for change in ({'host': '-oProxyCommand=evil'}, {'host': 'node;rm -rf /'},
                       {'user': '-p'}, {'port': 0}, {'port': True},
                       {'host_key': public_key() + '\nother'}):
            with self.subTest(change=change), self.assertRaisesRegex(HELPER['EnrollmentError'], 'invalid_target'):
                HELPER['verify']({**self.payload, **change}, self.key, self.hosts, runner=self.runner)
        self.assertFalse(self.hosts.exists())
        self.assertEqual(self.calls, [])

    def test_subprocess_runner_returns_only_fixed_probe_output(self):
        import sys
        code, output = HELPER['run_ssh']([sys.executable, '-c', 'print("NODE_PLANE_KEY_VERIFIED", end="")'])
        self.assertEqual((code, output), (0, b'NODE_PLANE_KEY_VERIFIED'))
        with self.assertRaisesRegex(HELPER['EnrollmentError'], 'login_failed'):
            HELPER['run_ssh']([sys.executable, '-c', 'print("x" * 10000)'])

    def test_fresh_install_without_ssh_key_selects_managed_backend_identity(self):
        config = SimpleNamespace(SSH_KEY='', SSH_KNOWN_HOSTS_PATH='/opt/node-plane/shared/ssh/known_hosts')
        with mock.patch.dict('sys.modules', {'config': config}), mock.patch.dict(os.environ, {}, clear=True):
            private_key, known_hosts, fallback = HELPER['configured_paths']()
        self.assertEqual(private_key, '/opt/node-plane/shared/ssh/id_ed25519')
        self.assertEqual(known_hosts, '/opt/node-plane/shared/ssh/known_hosts')
        self.assertEqual(fallback, pathlib.Path.home() / '.ssh/known_hosts')
        config.SSH_KEY = '/root/.ssh/controller_key'
        with mock.patch.dict('sys.modules', {'config': config}), mock.patch.dict(os.environ, {'SSH_KNOWN_HOSTS_PATH': str(self.hosts)}, clear=True):
            private_key, _, fallback = HELPER['configured_paths']()
        self.assertEqual(private_key, '/root/.ssh/controller_key')
        self.assertIsNone(fallback)


if __name__ == '__main__':
    unittest.main()
