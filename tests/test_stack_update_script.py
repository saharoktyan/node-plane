"""Exercise snapshot restoration and rollback without touching host services."""
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]


class StackUpdateScriptTests(TestCase):
    def test_controller_failure_restores_binary_units_environment_and_release(self):
        script = (ROOT / 'scripts/update.sh').read_text()
        rollback = script[script.index('rollback_simple() {'):script.index('update_simple() {')]
        for component in ('backend', 'worker', 'driver', 'telegram'):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                previous = root / 'previous'
                previous.mkdir()
                new = root / 'new'
                new.mkdir()
                link = root / 'current'
                link.symlink_to(new)
                binary, unit, env = [root / name for name in ('driver', 'driver.service', '.env')]
                for path in (binary, unit, env):
                    path.write_text('previous ' + path.name)
                binary.chmod(0o755)
                env.chmod(0o600)
                progress = root / 'progress.json'
                source = f'''set -euo pipefail
source {shlex.quote(str(ROOT / 'scripts/lib/stack_update.sh'))}
sudo() {{ "$@"; }}
systemctl() {{ return 0; }}
wait_for_service() {{ return 0; }}
stack_files() {{ printf '%s\\n' {shlex.quote(str(binary))} {shlex.quote(str(unit))} "$STACK_ENV_FILE"; }}
STACK_JOB=test
STACK_COMPONENT={component}
STACK_PROGRESS_FILE={shlex.quote(str(progress))}
SIMPLE_BOT_SERVICE=node-plane-telegram.service
HEALTH_TIMEOUT=1
stack_snapshot {shlex.quote(str(env))}
printf changed > {shlex.quote(str(binary))}
printf changed > {shlex.quote(str(unit))}
printf changed > {shlex.quote(str(env))}
{rollback}
rollback_simple {shlex.quote(str(previous))} {shlex.quote(str(link))} {shlex.quote(str(new))}
'''
                result = subprocess.run(['bash', '-c', source], capture_output=True, text=True)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('Rollback completed.', result.stdout)
                self.assertEqual(link.resolve(), previous)
                for path in (binary, unit, env):
                    self.assertEqual(path.read_text(), 'previous ' + path.name)
                self.assertEqual(env.stat().st_mode & 0o777, 0o600)
                value = json.loads(progress.read_text())
                self.assertEqual(value['rollback_status'], 'succeeded')
                self.assertEqual(value['components'][component], 'failed')

    def test_driver_update_and_health_checks_precede_agent_rollout(self):
        source = (ROOT / 'scripts/update.sh').read_text()
        self.assertIn('setup_driver_agents.sh" --skip-agents --strict --bin-source release', source)
        self.assertIn('import backend.executor; import telegram_client.main', source)
        self.assertIn("get_node_driver().binary_info()", source)
        self.assertIn('/health/ready', source)
        self.assertIn('node-plane-backend-worker.timer', source)
