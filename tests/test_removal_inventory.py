import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest

from backend.removal_inventory import read_inventory, validate_inventory, inventory_digest
from backend.removal_verifier import RemovalVerifier, RemovalVerificationError


class RemovalInventoryTests(unittest.TestCase):
    def test_custom_paths_overrides_backups_and_container_names_are_captured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / 'node.env'
            env.write_text(f'XRAY_CONFIG="{root}/custom/config.json"\n'
                           'XRAY_CONTAINER_NAME=custom.xray\nAWG_CONTAINER_NAME=custom-awg\n')
            config = root / 'agent.toml'
            config.write_text(f'runtime_root = {json.dumps(str(root / "runtime"))}\n'
                              f'node_env_path = {json.dumps(str(env))}\n')
            (root / 'custom').mkdir()
            backup = root / 'custom/config.json.bak.123'
            backup.write_text('private')
            temporary = [root / 'custom' / name for name in
                         ('.xray-config-interrupted.json', '.xray-user-interrupted.json')]
            for path in temporary:
                path.write_text('private')
            legacy_directory = root / 'custom/config.json.dirbak.20261006'
            legacy_directory.mkdir()
            resources = read_inventory(str(config), str(root / 'agent'))
            self.assertIn(str(backup), resources['paths'])
            for path in temporary:
                self.assertIn(str(path), resources['paths'])
            # A formerly miscreated config directory outside the runtime must
            # be detected, not silently treated as verified absence.
            self.assertIn(str(legacy_directory), resources['paths'])
            self.assertIn(str(root / 'runtime'), resources['paths'])
            self.assertIn(str(root / 'custom/config.json.lock'), resources['paths'])
            self.assertEqual(resources['containers'], ['custom.xray', 'custom-awg'])
            verifier = RemovalVerifier(local=True,
                bot_public_key='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAExample key')
            script = verifier.script(resources)
            self.assertIn(str(root / 'runtime'), script)
            self.assertIn(r'^custom\.xray(-previous-[0-9]+)?$', script)
            self.assertEqual(inventory_digest(resources), inventory_digest({
                **resources, 'paths': list(reversed(resources['paths']))}))

    def test_environment_is_never_executed_or_expanded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, env, sentinel = root/'agent.toml', root/'node.env', root/'executed'
            config.write_text(f'node_env_path = {json.dumps(str(env))}\n')
            env.write_text(f'XRAY_CONFIG="$(touch {sentinel})"\n')
            with self.assertRaises(ValueError):
                read_inventory(str(config), str(root/'agent'))
            self.assertFalse(sentinel.exists())

    def test_capture_resources_rejects_another_host_and_invalid_inventory(self):
        import subprocess
        verifier = RemovalVerifier(local=True, runner=lambda command, **kwargs:
            subprocess.CompletedProcess(command, 0,
                'NODE_PLANE_HOST_ID:0123456789abcdef0123456789abcdef\n'
                'NODE_PLANE_RESOURCES:{}\n', ''))
        with self.assertRaises(RemovalVerificationError):
            verifier.capture_resources('node', 'a' * 64)
        with self.assertRaisesRegex(RemovalVerificationError, 'invalid resource inventory'):
            verifier.capture_resources('node', verifier._fingerprint('0123456789abcdef0123456789abcdef'))

    def test_invalid_resource_paths_and_container_names_are_rejected(self):
        for path in ('/', '/opt', '/opt/../etc', '/opt/runtime\nanything'):
            with self.assertRaises(ValueError):
                validate_inventory({'paths': [path], 'containers': ['xray', 'awg']})
        with self.assertRaises(ValueError):
            validate_inventory({'paths': ['/opt/runtime'], 'containers': ['xray;bad', 'awg']})

    def test_real_verification_script_detects_custom_leftovers_and_previous_containers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in {
                'id': '#!/bin/sh\necho 0\n',
                'systemctl': '#!/bin/sh\nexit 1\n',
                'docker': '#!/bin/sh\nif [ "$1" = ps ]; then printf "%s\\n" "$TEST_CONTAINERS"; fi\n',
            }.items():
                executable = root / name
                executable.write_text(text)
                executable.chmod(0o700)
            leftover = root / 'custom config.json'
            resources = {'paths': [str(leftover)], 'containers': ['custom.xray', 'custom-awg']}
            verifier = RemovalVerifier(local=True,
                bot_public_key='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAExample key')

            def run(names=''):
                return subprocess.run(['sh', '-s'], input=verifier.script(resources), text=True,
                    capture_output=True, env={**os.environ, 'PATH': str(root) + ':' + os.environ['PATH'],
                                             'TEST_CONTAINERS': names}, timeout=10)

            leftover.write_text('private')
            self.assertEqual(run().returncode, 22)
            leftover.unlink()
            self.assertEqual(run('custom.xray-previous-123').returncode, 26)
            # A dot in a configured name must match literally, not as a regex wildcard.
            self.assertEqual(run('customXxray').returncode, 0)
            # A clean local installation has no bot SSH key, but all artifact
            # and container checks must still run.
            verifier.bot_public_key = None
            self.assertEqual(run().returncode, 0)
            leftover.write_text('private')
            self.assertEqual(run().returncode, 22)
            leftover.unlink()
            self.assertEqual(run('custom.xray').returncode, 26)
