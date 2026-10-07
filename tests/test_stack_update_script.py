"""Exercise snapshot restoration and rollback without touching host services."""
import json
from itertools import product
from pathlib import Path
import shlex
import subprocess
import tempfile
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]


class StackUpdateScriptTests(TestCase):
    def test_update_preserves_runtime_credentials_and_uses_private_atomic_replacement(self):
        script=(ROOT / 'scripts/update.sh').read_text()
        setters=script[script.index('set_env_value_in_file() {'):script.index('detect_mode() {')]
        sync=script[script.index('sync_shared_env() {'):script.index('fetch_code() {')]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);checkout=root/'checkout';checkout.mkdir();shared=root/'shared';shared.mkdir()
            source=checkout/'.env';source.write_text('BOT_TOKEN=bootstrap\nADMIN_IDS=123\n')
            runtime=shared/'.env';runtime.write_text('BOT_TOKEN=current\nPOSTGRES_DSN=postgresql://existing\nNODE_PLANE_POSTGRES_PASSWORD=unchanged\nNODE_AGENT_TARGETS=lv1=localhost:50061\nNODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE=/private/token\n')
            before=runtime.stat().st_ino
            result=subprocess.run(['bash','-c',f'set -euo pipefail\nREPO_ROOT={shlex.quote(str(checkout))}\nMODE=simple\n{setters}\n{sync}\nsync_shared_env {shlex.quote(str(shared))}'],cwd=checkout,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            value=runtime.read_text()
            for line in ('BOT_TOKEN=current','POSTGRES_DSN=postgresql://existing','NODE_PLANE_POSTGRES_PASSWORD=unchanged','NODE_AGENT_TARGETS=lv1=localhost:50061','NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE=/private/token'):
                self.assertIn(line,value)
            self.assertIn('NODE_PLANE_SOURCE_DIR='+str(checkout),value)
            self.assertIn('NODE_PLANE_INSTALL_MODE=simple',value)
            self.assertEqual(runtime.stat().st_mode & 0o777,0o600)
            self.assertNotEqual(runtime.stat().st_ino,before)
            self.assertEqual(source.read_text(),'BOT_TOKEN=bootstrap\nADMIN_IDS=123\n')
            self.assertEqual(list(shared.glob('.env-update.*')),[])

    def test_controller_failure_restores_binary_units_environment_and_release(self):
        script = (ROOT / 'scripts/update.sh').read_text()
        rollback = script[script.index('rollback_simple() {'):script.index('update_simple() {')]
        for component, healthy in product(('backend', 'worker', 'driver', 'telegram'), (True, False)):
            with self.subTest(component=component, rollback_health=healthy), tempfile.TemporaryDirectory() as directory:
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
wait_for_service() {{ return {0 if healthy else 1}; }}
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
                self.assertIn('Rollback completed.' if healthy else 'Rollback failed.',
                    result.stdout if healthy else result.stderr)
                self.assertEqual(link.resolve(), previous)
                for path in (binary, unit, env):
                    self.assertEqual(path.read_text(), 'previous ' + path.name)
                self.assertEqual(env.stat().st_mode & 0o777, 0o600)
                value = json.loads(progress.read_text())
                self.assertEqual(value['rollback_status'], 'succeeded' if healthy else 'failed')
                self.assertEqual(value['components'][component], 'failed')

    def test_driver_update_and_health_checks_precede_agent_rollout(self):
        source = (ROOT / 'scripts/update.sh').read_text()
        self.assertIn('setup_driver_agents.sh" --skip-agents --strict --bin-source release', source)
        self.assertIn('import backend.executor; import telegram_client.main', source)
        self.assertIn("GrpcIntentDriver(channel).binary_info()", source)
        self.assertIn('/health/ready', source)
        self.assertIn('node-plane-backend-worker.timer', source)
