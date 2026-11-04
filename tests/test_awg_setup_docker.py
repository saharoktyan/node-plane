"""The agent's init-awg.sh and regenerate-awg-entropy.sh entry points, with
a fake Docker and no Python on PATH, checked with the controller's
awg_profile.py. In the Docker suite, where CI builds the agent."""
import base64
import os
import pathlib
import shlex
import shutil
import subprocess
import tempfile
import unittest

import awg_profile

AGENT_BINARY = pathlib.Path(__file__).resolve().parents[1] / 'rust/node-agent/target/debug/node-plane-agent'


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_DOCKER') == '1', 'requires the Docker suite')
class AwgSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.tools = self.root / 'bin'
        self.tools.mkdir()
        # No host wg command: only what the entry points use.
        for name in ('bash', 'cp'):
            (self.tools / name).symlink_to(shutil.which(name))
        self.config = self.root / 'wg0.conf'
        self.node_env = self.root / 'node.env'
        self.node_env.write_text(
            f'AWG_CONFIG={shlex.quote(str(self.config))}\n'
            'AWG_SERVER_ADDRESS=10.8.1.1/24\nAWG_I1_PRESET=dns\nAWG_CONTAINER_NAME=awg-test\n')

    def run_script(self, name, check=True):
        return subprocess.run([str(AGENT_BINARY), 'entry', name], capture_output=True, text=True, check=check,
                              env={**os.environ, 'PATH': str(self.tools), 'NODE_PLANE_AGENT_BIN': str(AGENT_BINARY),
                                   'NODE_PLANE_NODE_ENV': str(self.node_env)})

    def test_init_creates_a_valid_config_once(self):
        self.run_script('init-awg.sh')
        values = awg_profile.interface_values(self.config.read_text())
        self.assertEqual(len(base64.b64decode(values['PrivateKey'], validate=True)), 32)
        self.assertIn('Address = 10.8.1.1/24, fd42:6e70:6177:2::1/64', self.config.read_text())
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        awg_profile.validate(values)
        previous = self.config.read_text()
        self.assertIn('already exists', self.run_script('init-awg.sh').stdout)
        self.assertEqual(self.config.read_text(), previous)
        # A clean reinstall generates fresh keys and parameters.
        self.config.unlink()
        self.run_script('init-awg.sh')
        regenerated = awg_profile.interface_values(self.config.read_text())
        awg_profile.validate(regenerated)
        self.assertNotEqual(regenerated['PrivateKey'], values['PrivateKey'])
        self.assertNotEqual(regenerated['HeaderProtectionKey'], values['HeaderProtectionKey'])
        self.assertTrue(3 <= int(regenerated['Jc']) <= 7)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['bin', 'node.env', 'wg0.conf'])

    def fake_docker(self, state):
        docker = self.tools / 'docker'
        docker.write_text(f'#!/bin/sh\necho "$*" >> {shlex.quote(str(self.root / "docker.log"))}\n'
                          f'case "$1" in inspect) echo {state} ;; esac\n')
        docker.chmod(0o755)

    def test_regenerate_restarts_and_reports_or_restores(self):
        self.run_script('init-awg.sh')
        before = self.config.read_text()
        self.fake_docker('exited')
        failed = self.run_script('regenerate-awg-entropy.sh', check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('previous config restored', failed.stderr)
        self.assertEqual(self.config.read_text(), before)
        self.fake_docker('running')
        done = self.run_script('regenerate-awg-entropy.sh')
        self.assertNotEqual(self.config.read_text(), before)
        self.assertEqual(awg_profile.interface_values(self.config.read_text())['PrivateKey'],
                         awg_profile.interface_values(before)['PrivateKey'])
        self.assertIn('preset: dns', done.stdout)
        self.assertIn('HeaderProtectionKey: [set]', done.stdout)
        self.assertTrue(done.stdout.rstrip().endswith('must be reissued after entropy regeneration.'))
        self.assertIn('restart awg-test', (self.root / 'docker.log').read_text())
        self.assertFalse(any(p.name.startswith('.awg-regenerate-backup') for p in self.root.iterdir()))


if __name__ == '__main__':
    unittest.main()
