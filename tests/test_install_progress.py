"""The workstation contract is exercised with stubbed host commands only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PHASES = ['configuration', 'release', 'python', 'database', 'identity', 'services', 'driver']


class InstallProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'checkout'
        self.scripts = self.source / 'scripts'
        self.binaries = self.root / 'bin'
        self.release = self.root / 'fake-release'
        self.base = self.root / 'installed'
        self.config = self.root / 'private-install.env'
        self.log = self.root / 'host-actions.log'
        (self.scripts / 'lib').mkdir(parents=True)
        self.binaries.mkdir()
        for relative in ('install.sh', 'lib/install_progress.sh'):
            shutil.copyfile(ROOT / 'scripts' / relative, self.scripts / relative)
        (self.source / '.env.example').write_text('BOT_TOKEN=replace_me\nADMIN_IDS=123456789\n')
        self.config.write_text(
            f'BOT_TOKEN=999:private-test-token\nADMIN_IDS=789\n'
            f'NODE_PLANE_BASE_DIR={self.base}\n'
            'DB_BACKEND=postgres\nPOSTGRES_DSN=\n')
        (self.scripts / 'python_runtime.sh').write_text(
            'select_python_runtime() { printf "%s\\n" "$FAKE_PYTHON"; }\n')
        (self.scripts / 'postgres_runtime.sh').write_text('''
run_as_root() { "$@"; }
auto_provision_simple_postgres() {
  printf 'database\\n' >> "$COMMAND_LOG"
  if [[ "${DATABASE_FAILURE:-0}" != 0 ]]; then return 19; fi
  if [[ -z "$(read_env_value POSTGRES_DSN "$1")" ]]; then
    set_env_value POSTGRES_DSN 'postgresql://user:secret-test@localhost/db' "$1"
  fi
}
''')
        for relative in ('app/backend/admin_cli.py', 'app/telegram_client/main.py', 'requirements.txt'):
            path = self.release / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        (self.release / 'VERSION').write_text('0.4.3-alpha.99\n')
        for name, label in (
            ('install_backend_systemd.sh', 'backend-services'),
            ('install_telegram_client_systemd.sh', 'telegram-service'),
            ('setup_driver_agents.sh', 'driver'),
        ):
            self.executable(self.release / 'scripts' / name, f'''#!/bin/bash
printf '{label}\\n' >> "$COMMAND_LOG"
if [[ '{label}' == driver && "${{DRIVER_FAILURE:-0}}" != 0 ]]; then exit 23; fi
''')
        self.executable(self.binaries / 'python', '''#!/bin/bash
printf 'python %s\\n' "$*" >> "$COMMAND_LOG"
if [[ "$1" == -m && "$2" == venv ]]; then
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python"
fi
if [[ "$*" == *backend.admin_cli* && "${IDENTITY_FAILURE:-0}" != 0 && "$*" != *init-schema* ]]; then
  exit 17
fi
''')
        self.executable(self.binaries / 'git', '''#!/bin/bash
case "$1" in
  fetch) printf 'fetch\\n' >> "$COMMAND_LOG" ;;
  rev-parse)
    if [[ "$2" == --is-inside-work-tree ]]; then printf 'true\\n'
    else printf 'abc1234\\n'; fi ;;
  tag) printf 'v0.4.3-alpha.99\\n' ;;
  show) printf '0.4.3-alpha.99\\n' ;;
  merge-base) exit 0 ;;
  archive) tar -cf - -C "$FAKE_RELEASE" . ;;
  *) exit 91 ;;
esac
''')
        self.environment = {
            **os.environ,
            'PATH': f'{self.binaries}:{os.environ["PATH"]}',
            'FAKE_PYTHON': str(self.binaries / 'python'),
            'FAKE_RELEASE': str(self.release),
            'COMMAND_LOG': str(self.log),
            'NODE_PLANE_AUTO_SETUP_DRIVER_AGENTS_ON_INSTALL': '1',
        }
        for key in ('NODE_PLANE_INSTALL_EVENTS', 'NODE_PLANE_INSTALL_EVENT_FD',
                    'NODE_PLANE_INSTALL_ENV_FILE', 'NODE_PLANE_INSTALL_REF',
                    'NODE_PLANE_UPDATE_BRANCH', 'MODE'):
            self.environment.pop(key, None)

    def executable(self, path, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        path.chmod(0o755)

    def run_install(self, *extra, progress=True, environment=None):
        command = ['bash', str(self.scripts / 'install.sh'), '--non-interactive',
                   '--mode', 'simple', '--env-file', str(self.config),
                   '--branch', 'dev', '--ref', 'v0.4.3-alpha.99']
        if progress:
            command.append('--progress-json')
        return subprocess.run(command + list(extra),
            env={**self.environment, **(environment or {})},
            capture_output=True, text=True, timeout=10)

    def events(self, result):
        return [json.loads(line[len('NODE_PLANE_EVENT '):])
                for line in result.stdout.splitlines() if line.startswith('NODE_PLANE_EVENT ')]

    def test_complete_install_has_fixed_phases_and_no_secrets_in_events(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events(result)
        major = [event for event in events if event['event'] == 'step']
        self.assertEqual([(event['id'], event['state']) for event in major],
                         [(phase, state) for phase in PHASES for state in ('running', 'done')])
        self.assertEqual([event['completed'] for event in major],
                         [count for index in range(7) for count in (index, index + 1)])
        self.assertTrue(all(event['total'] == 7 and event['version'] == 1 for event in events))
        for secret in ('999:private-test-token', 'secret-test', 'BOT_TOKEN', 'POSTGRES_DSN'):
            self.assertNotIn(secret, json.dumps(events))
        self.assertFalse((self.source / '.env').exists())
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.base / 'shared/.env').stat().st_mode & 0o777, 0o600)
        actions = self.log.read_text()
        self.assertIn('backend-services', actions)
        self.assertIn('telegram-service', actions)
        self.assertIn('driver', actions)

    def test_events_disabled_do_not_change_normal_output(self):
        result = self.run_install(progress=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(result), [])
        self.assertIn('Simple mode environment is prepared.', result.stdout)

    def test_missing_credentials_fail_before_any_host_provisioning(self):
        for values, error in (('BOT_TOKEN=replace_me\n', 'BOT_TOKEN is required'),
                              ('BOT_TOKEN=test\nADMIN_IDS=not-a-number\n', 'numeric Telegram')):
            with self.subTest(values=values):
                self.config.write_text(values)
                result = self.run_install()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
                self.assertFalse(self.log.exists())
                self.assertEqual(self.events(result)[-1]['state'], 'failed')
                self.assertEqual(self.events(result)[-1]['completed'], 0)

    def test_missing_explicit_file_is_not_replaced_with_example(self):
        self.config.unlink()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('configuration file is missing', result.stderr)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.log.exists())

    def test_symlinked_installer_config_is_refused_without_modifying_target(self):
        original = self.config.read_text()
        target = self.root / 'foreign.env'
        self.config.rename(target)
        target.chmod(0o644)
        self.config.symlink_to(target)
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not a symlink', result.stderr)
        self.assertEqual(target.read_text(), original)
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertFalse(self.log.exists())

    def test_active_installation_refused_and_shared_state_unchanged(self):
        shared = self.base / 'shared'
        shared.mkdir(parents=True)
        original = 'BOT_TOKEN=keep-active-secret\nADMIN_IDS=12\nNODE_AGENT_TARGETS=existing=host\n'
        (shared / '.env').write_text(original)
        current = self.base / 'current'
        current.mkdir()
        (current / 'sentinel').write_text('working-installation')
        result = self.run_install('--force')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('active installation already exists', result.stderr)
        self.assertEqual((shared / '.env').read_text(), original)
        self.assertEqual((current / 'sentinel').read_text(), 'working-installation')
        self.assertNotIn('database', self.log.read_text())
        self.assertEqual(self.events(result)[-1]['completed'], 0)

    def test_partial_install_retry_preserves_generated_runtime_state(self):
        shared = self.base / 'shared'
        shared.mkdir(parents=True)
        (shared / '.env').write_text(
            f'BOT_TOKEN=999:private-test-token\nADMIN_IDS=789\n'
            'POSTGRES_DSN=postgresql://previous-secret@localhost/previous\n'
            'NODE_AGENT_TARGETS=keep=host:50061\n')
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = (shared / '.env').read_text()
        self.assertIn('POSTGRES_DSN=postgresql://previous-secret@localhost/previous', saved)
        self.assertIn('NODE_AGENT_TARGETS=keep=host:50061', saved)

    def test_database_error_counts_only_completed_phases(self):
        result = self.run_install(environment={'DATABASE_FAILURE': '1'})
        self.assertEqual(result.returncode, 19, result.stderr)
        last = self.events(result)[-1]
        self.assertEqual((last['id'], last['state'], last['completed']), ('database', 'failed', 3))
        self.assertNotIn('backend-services', self.log.read_text())
        self.assertNotIn('Failing command:', result.stderr)
        self.assertNotIn('secret-test', result.stderr)

    def test_machine_mode_refuses_best_effort_driver_success(self):
        result = self.run_install(environment={'DRIVER_FAILURE': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('setup is incomplete', result.stderr)
        last = self.events(result)[-1]
        self.assertEqual((last['id'], last['state'], last['completed']), ('driver', 'failed', 6))

    def test_retry_of_marked_partial_install_preserves_shared_state(self):
        first = self.run_install(environment={'DRIVER_FAILURE': '1'})
        self.assertNotEqual(first.returncode, 0)
        marker = self.base / 'shared/data/workstation-install.incomplete'
        self.assertTrue(marker.exists())
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        shared = self.base / 'shared/.env'
        original = shared.read_text()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(shared.read_text(), original)
        self.assertFalse(marker.exists())
        subsequent = self.run_install()
        self.assertNotEqual(subsequent.returncode, 0)
        self.assertIn('active installation already exists', subsequent.stderr)

    def test_invalid_output_descriptor_refused_before_configuring_host(self):
        result = self.run_install(environment={'NODE_PLANE_INSTALL_EVENT_FD': 'bad'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('open output descriptor', result.stderr)
        self.assertFalse(self.log.exists())

    def test_machine_installs_require_noninteractive_and_driver_setup(self):
        result = self.run_install(environment={'NODE_PLANE_AUTO_SETUP_DRIVER_AGENTS_ON_INSTALL': '0'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires driver setup', result.stderr)
        self.assertFalse(self.log.exists())
        result = subprocess.run(['bash', str(self.scripts / 'install.sh'), '--progress-json'],
            env=self.environment, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires --non-interactive', result.stderr)
        self.assertFalse(self.log.exists())

    def test_environment_opt_in_and_external_descriptor(self):
        self.environment['NODE_PLANE_INSTALL_EVENTS'] = '1'
        self.environment['NODE_PLANE_INSTALL_EVENT_FD'] = '3'
        result = subprocess.run(['bash', '-c', 'exec 3>&1; exec bash "$@"', 'bash',
            str(self.scripts / 'install.sh'), '--non-interactive', '--mode', 'simple',
            '--branch', 'dev', '--ref', 'v0.4.3-alpha.99', '--env-file', str(self.config)],
            env=self.environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(result)[-1]['completed'], 7)

    def test_progress_descriptor_does_not_corrupt_command_substitution(self):
        result = subprocess.run(['bash', '-c', '''
set -euo pipefail
source "$1"
NODE_PLANE_INSTALL_EVENTS=1
install_progress_init
install_progress_begin configuration
value="$(install_progress_detail 'Read runtime configuration'; printf 'captured-value')"
[[ "$value" == captured-value ]]
printf 'CAPTURED %s\\n' "$value"
install_progress_done
''', 'bash', str(self.scripts / 'lib/install_progress.sh')],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('CAPTURED captured-value', result.stdout)
        self.assertEqual(len(self.events(result)), 3)


if __name__ == '__main__':
    unittest.main()
