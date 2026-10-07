"""Controller identity defaults must agree before the first node is installed."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SshKeyDefaultTests(unittest.TestCase):
    def config(self, key=None, stored=None):
        with tempfile.TemporaryDirectory() as folder:
            shared = Path(folder)
            if stored is not None:
                (shared / '.env').write_text(f'SSH_KEY={stored}\n')
            environment = {**os.environ, 'NODE_PLANE_SHARED_DIR': folder,
                           'NODE_PLANE_APP_DIR': str(ROOT), 'DB_BACKEND': 'postgres',
                           'PYTHONPATH': str(ROOT / 'app')}
            environment.pop('SSH_KEY', None)
            environment.pop('SSH_KNOWN_HOSTS_PATH', None)
            if key is not None:
                environment['SSH_KEY'] = key
            result = subprocess.run([sys.executable, '-c',
                'import json, config; print(json.dumps([config.SSH_KEY, config.SSH_KNOWN_HOSTS_PATH]))'],
                env=environment, capture_output=True, text=True, check=True, timeout=10)
            paths = json.loads(result.stdout)
            return paths, str(shared / 'ssh' / 'id_ed25519'), str(shared / 'ssh' / 'known_hosts')

    def test_fresh_install_uses_shared_identity(self):
        paths, key, hosts = self.config()
        self.assertEqual(paths, [key, hosts])

    def test_empty_legacy_key_uses_same_default(self):
        for value in ('', '   '):
            with self.subTest(value=value):
                paths, key, _ = self.config(stored=value)
                self.assertEqual(paths[0], key)

    def test_explicit_identity_is_not_replaced(self):
        paths, _, _ = self.config(stored='/root/.ssh/custom')
        self.assertEqual(paths[0], '/root/.ssh/custom')
        paths, _, _ = self.config(key='/root/.ssh/env-override', stored='/root/.ssh/stored')
        self.assertEqual(paths[0], '/root/.ssh/env-override')

    def test_setup_persists_only_missing_default_and_dry_run_does_not_write(self):
        source = (ROOT / 'scripts' / 'setup_driver_agents.sh').read_text()
        start = source.index('# Use the same controller identity as config.SSH_KEY')
        end = source.index('\nAPP_ROOT=', start)
        block = source[start:end]
        for value, dry_run, writes in [('', 0, 'write'), ('', 1, ''), ('/custom/key', 0, '')]:
            with self.subTest(value=value, dry_run=dry_run):
                result = subprocess.run(['bash', '-c',
                    'set -eu\nset_env_value_if_changed() { echo write; }\n' + block + '\nprintf "key=%s\\n" "$SSH_KEY"'],
                    env={**os.environ, 'SHARED_ROOT': '/fixture/shared', 'SSH_KEY': value,
                         'DRY_RUN': str(dry_run), 'ENV_FILE': '/fixture/.env', 'ENV_CHANGED': '0'},
                    capture_output=True, text=True, check=True, timeout=10)
                self.assertEqual('write' in result.stdout, bool(writes))
                self.assertIn('key=' + (value or '/fixture/shared/ssh/id_ed25519'), result.stdout)
