"""Check embedded remote shell contracts without installing or contacting hosts."""
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'rust/node-plane-cli/src/installer.rs'


def shell_constants():
    source = INSTALLER.read_text()
    raw = {name: body for name, body in re.findall(
        r'const\s+(\w+)\s*:\s*&str\s*=\s*r#"(.*?)"#;', source, re.DOTALL)}
    for name, body in re.findall(
            r'const\s+(\w+)\s*:\s*&str\s*=\s*("(?:[^"\\]|\\.)*");', source):
        raw[name] = json.loads(body)
    return raw


class WorkstationInstallerShellTests(unittest.TestCase):
    def test_embedded_shell_scripts_have_valid_bash_syntax(self):
        constants = shell_constants()
        self.assertTrue({'PREFLIGHT', 'PREPARE_HOST', 'PREPARE_SOURCE_PREFIX',
                         'PREPARE_SOURCE_SUFFIX', 'CHECK_WORK_OWNER', 'VERIFY',
                         'DIAGNOSE'}.issubset(constants))
        for name, source in constants.items():
            with self.subTest(script=name):
                result = subprocess.run(['bash', '-n'], input=source,
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
        composed = (constants['PREPARE_SOURCE_PREFIX'] + '\nbranch=\'dev\'\n'
                    'tag=\'v0.4.3-alpha.48\'\n' + constants['PREPARE_SOURCE_SUFFIX'])
        result = subprocess.run(['bash', '-n'], input=composed,
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def verification_python(self):
        source = shell_constants()['VERIFY']
        body = re.search(r"<<'PY'\n(.*?)\nPY(?:\n|$)", source, re.DOTALL)
        self.assertIsNotNone(body, 'Token validation must read Python input, never token argv')
        ast.parse(body.group(1))
        return body.group(1)

    def run_token_check(self, should_fail=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, shared, stubs = root / 'current', root / 'shared', root / 'stub-modules'
            app.mkdir()
            shared.mkdir()
            stubs.mkdir()
            private_token = '123:private-fixture-token-abcdefghijk'
            config = shared / '.env'
            config.write_text(f'BOT_TOKEN={private_token}\nADMIN_IDS=42\nDB_BACKEND=postgres\n')
            config.chmod(0o600)
            log = root / 'calls.log'
            (stubs / 'aiogram.py').write_text('''
import os
from pathlib import Path
class Bot:
    def __init__(self, token):
        assert token == os.environ['EXPECTED_TOKEN']
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        with open(os.environ['CALLS_FILE'], 'a') as file:
            file.write('closed\\n')
    async def get_me(self, request_timeout=None):
        assert request_timeout == 10
        Path(os.environ['CALLS_FILE']).write_text('get_me\\n')
        if os.environ.get('SIMULATE_FAILURE') == '1':
            raise ValueError(os.environ['EXPECTED_TOKEN'])
''')
            env = {**os.environ,
                   'NODE_PLANE_APP_DIR': str(app), 'NODE_PLANE_SHARED_DIR': str(shared),
                   'PYTHONPATH': f'{stubs}:{ROOT / "app"}',
                   'EXPECTED_TOKEN': private_token, 'CALLS_FILE': str(log),
                   'SIMULATE_FAILURE': '1' if should_fail else '0'}
            for key in ('BOT_TOKEN', 'ADMIN_IDS', 'DB_BACKEND'):
                env.pop(key, None)
            result = subprocess.run([sys.executable, '-'], input=self.verification_python(),
                env=env, capture_output=True, text=True, timeout=5)
            self.assertNotIn(private_token, result.stdout + result.stderr)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(log.read_text().splitlines(), ['get_me', 'closed'])
            return result

    def test_token_validation_loads_private_runtime_env_and_closes_async_bot(self):
        result = self.run_token_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_token_validation_does_not_disclose_token(self):
        result = self.run_token_check(should_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Telegram token or network validation failed', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def run_prepare(self, variant):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            opt = root / 'opt'
            base = opt / 'node-plane'
            binaries = root / 'bin'
            binaries.mkdir()
            base.mkdir(parents=True)
            os_release = root / 'os-release'
            os_release.write_text('ID=ubuntu\n')
            action_log = root / 'actions.log'
            for name, body in {
                'apt-get': '#!/bin/sh\nprintf "apt-get %s\\n" "$*" >> "$ACTION_LOG"\n',
                'timeout': '#!/bin/sh\nshift\nexec "$@"\n',
                'apt-cache': '#!/bin/sh\nexit 0\n',
                'python3.12': '#!/bin/sh\nexit 0\n',
                'stat': '''#!/bin/bash
if [[ "$2" == %u ]]; then
  if [[ "$3" == "${UNOWNED_CHILD:-}" ]]; then echo 1000; else echo 0; fi
else
  /usr/bin/stat "$@"
fi
''',
            }.items():
                path = binaries / name
                path.write_text(body)
                path.chmod(0o755)
            env = {**os.environ, 'PATH': f'{binaries}:{os.environ["PATH"]}',
                   'ACTION_LOG': str(action_log)}
            child = base / 'shared'
            if variant == 'symlink':
                foreign = root / 'foreign-service'
                foreign.mkdir()
                (foreign / 'sentinel').write_text('keep')
                child.symlink_to(foreign)
            elif variant in ('unowned', 'writable'):
                child.mkdir()
                if variant == 'unowned':
                    env['UNOWNED_CHILD'] = str(child)
                else:
                    child.chmod(0o777)
            elif variant == 'active':
                (base / 'current').mkdir()
                (base / 'current/sentinel').write_text('keep')
            script = shell_constants()['PREPARE_HOST']
            script = script.replace('. /etc/os-release', f'. "{os_release}"')
            script = re.sub(r'/opt(?=/|[\s\";])', lambda _: str(opt), script)
            result = subprocess.run(['bash'], input=script, env=env,
                capture_output=True, text=True, timeout=5)
            actions = action_log.read_text() if action_log.exists() else ''
            if variant == 'symlink':
                self.assertEqual((foreign / 'sentinel').read_text(), 'keep')
            if variant == 'active':
                self.assertEqual((base / 'current/sentinel').read_text(), 'keep')
            return result, actions

    def test_prepare_refuses_active_stack_before_package_mutations(self):
        result, actions = self.run_prepare('active')
        self.assertEqual(result.returncode, 14, result.stderr)
        self.assertEqual(actions, '')

    def test_prepare_rejects_symlinked_unowned_or_writable_managed_children(self):
        for variant in ('symlink', 'unowned', 'writable'):
            with self.subTest(variant=variant):
                result, actions = self.run_prepare(variant)
                self.assertEqual(result.returncode, 13, result.stderr)
                self.assertEqual(actions, '')


if __name__ == '__main__':
    unittest.main()
