"""Regression tests for agent rollout installer execution and release resolution.

Verifies AgentRolloutService(None)._execute invokes the target release installer
via the updated current symlink (never the old release), forwards transport flags
(local vs ssh), skip_driver flag, and forwards progress identity environment.
Also tests real shell detect_release_ref preferring controller APP_ROOT VERSION.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest import TestCase
from uuid import uuid4

from backend.agent_rollout import AgentRolloutService

ROOT = Path(__file__).resolve().parents[1]
DRIVER_AGENTS_SCRIPT = (ROOT / 'scripts' / 'setup_driver_agents.sh').read_text()
DETECT_RELEASE_REF_FN = DRIVER_AGENTS_SCRIPT[
    DRIVER_AGENTS_SCRIPT.index('detect_release_ref() {'):DRIVER_AGENTS_SCRIPT.index('asset_url() {')
]


class AgentInstallerReleaseTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.releases_dir = self.root / 'releases'
        self.old_release = self.releases_dir / 'old'
        self.new_release = self.releases_dir / 'new'
        self.shared_dir = self.root / 'shared'
        self.progress_dir = self.shared_dir / 'data' / 'installation-progress'
        self.records_dir = self.shared_dir / 'records'
        self.current_link = self.root / 'current'

        for directory in (self.old_release / 'scripts', self.new_release / 'scripts',
                          self.progress_dir, self.records_dir):
            directory.mkdir(parents=True)

        self.old_marker = self.records_dir / 'old_installer_executed.marker'

        # Old release installer fails if mistakenly invoked
        old_installer = self.old_release / 'scripts' / 'setup_driver_agents.sh'
        old_installer.write_text(f"""#!/usr/bin/env bash
touch '{self.old_marker}'
echo "ERROR: Old installer was unexpectedly invoked!" >&2
exit 99
""")
        old_installer.chmod(0o755)

        # New release installer records invocation and succeeds
        new_installer = self.new_release / 'scripts' / 'setup_driver_agents.sh'
        new_installer.write_text(f"""#!/usr/bin/env bash
set -euo pipefail
record_path="{self.records_dir}/run-${{NODE_PLANE_AGENT_PROGRESS_ID:-unknown}}.json"
python3 - "$@" <<'PY' > "$record_path"
import json, os, sys
data = {{
    "installer": "new",
    "args": sys.argv[1:],
    "progress_file": os.environ.get("NODE_PLANE_AGENT_PROGRESS_FILE"),
    "progress_id": os.environ.get("NODE_PLANE_AGENT_PROGRESS_ID"),
    "install_rust": os.environ.get("NODE_PLANE_INSTALL_RUST"),
}}
print(json.dumps(data))
PY
exit 0
""")
        new_installer.chmod(0o755)

        # Simulate release switch: current link switched from old to new
        self.current_link.symlink_to(self.old_release)
        self.current_link.unlink()
        self.current_link.symlink_to(self.new_release)

        self._orig_env = dict(os.environ)
        os.environ['NODE_PLANE_APP_DIR'] = str(self.current_link)
        os.environ['NODE_PLANE_SHARED_DIR'] = str(self.shared_dir)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        self.temp_dir.cleanup()

    def test_execute_invokes_new_release_installer_with_local_transport(self):
        task_id = str(uuid4())
        intent = {
            'transport': 'local',
            'ssh_target': None,
            'ssh_port': 22,
        }
        row = {
            'id': task_id,
            'node_key': 'node-local-alpha',
            'intent_json': json.dumps(intent),
        }

        # Seed pre-existing/inherited progress environment to ensure it gets overridden
        os.environ['NODE_PLANE_AGENT_PROGRESS_FILE'] = '/inherited/stale/progress.json'
        os.environ['NODE_PLANE_AGENT_PROGRESS_ID'] = 'inherited-stale-id'

        service = AgentRolloutService(None)
        succeeded = service._execute(row)

        self.assertTrue(succeeded)
        self.assertFalse(self.old_marker.exists(), "Old release installer should never have run")

        record_file = self.records_dir / f'run-{task_id}.json'
        self.assertTrue(record_file.is_file())
        record = json.loads(record_file.read_text())

        self.assertEqual(record['installer'], 'new')
        self.assertEqual(record['progress_id'], task_id)
        expected_progress_file = str(self.progress_dir / f'{task_id}.json')
        self.assertEqual(record['progress_file'], expected_progress_file)

        args = record['args']
        self.assertIn('--backend-node-key', args)
        self.assertEqual(args[args.index('--backend-node-key') + 1], 'node-local-alpha')
        self.assertIn('--bin-source', args)
        self.assertEqual(args[args.index('--bin-source') + 1], 'auto')
        self.assertIn('--backend-local', args)
        self.assertNotIn('--skip-driver', args)
        self.assertNotIn('--backend-ssh-target', args)
        self.assertNotIn('--backend-ssh-port', args)

    def test_execute_invokes_new_release_installer_with_ssh_and_skip_driver(self):
        task_id = str(uuid4())
        intent = {
            'transport': 'ssh',
            'ssh_target': 'operator@192.168.12.34',
            'ssh_port': 2222,
            'skip_driver': True,
        }
        row = {
            'id': task_id,
            'node_key': 'node-remote-bravo',
            'intent_json': json.dumps(intent),
        }

        service = AgentRolloutService(None)
        succeeded = service._execute(row)

        self.assertTrue(succeeded)
        self.assertFalse(self.old_marker.exists(), "Old release installer should never have run")

        record_file = self.records_dir / f'run-{task_id}.json'
        self.assertTrue(record_file.is_file())
        record = json.loads(record_file.read_text())

        self.assertEqual(record['installer'], 'new')
        self.assertEqual(record['progress_id'], task_id)
        expected_progress_file = str(self.progress_dir / f'{task_id}.json')
        self.assertEqual(record['progress_file'], expected_progress_file)

        args = record['args']
        self.assertIn('--backend-node-key', args)
        self.assertEqual(args[args.index('--backend-node-key') + 1], 'node-remote-bravo')
        self.assertIn('--bin-source', args)
        self.assertEqual(args[args.index('--bin-source') + 1], 'auto')
        self.assertIn('--skip-driver', args)
        self.assertIn('--backend-ssh-target', args)
        self.assertEqual(args[args.index('--backend-ssh-target') + 1], 'operator@192.168.12.34')
        self.assertIn('--backend-ssh-port', args)
        self.assertEqual(args[args.index('--backend-ssh-port') + 1], '2222')
        self.assertNotIn('--backend-local', args)

    def test_execute_cleans_inherited_rust_flag_and_forwards_job_identity(self):
        task_id = str(uuid4())
        intent = {
            'transport': 'local',
            'ssh_target': None,
            'ssh_port': 22,
        }
        row = {
            'id': task_id,
            'node_key': 'node-clean-env',
            'intent_json': json.dumps(intent),
        }

        os.environ['NODE_PLANE_INSTALL_RUST'] = '1'

        service = AgentRolloutService(None)
        succeeded = service._execute(row)

        self.assertTrue(succeeded)
        record = json.loads((self.records_dir / f'run-{task_id}.json').read_text())
        self.assertIsNone(record['install_rust'])
        self.assertEqual(record['progress_id'], task_id)

    def test_detect_release_ref_prefers_controller_app_root_version(self):
        controller_app = self.root / 'controller_app'
        launcher_repo = self.root / 'launcher_repo'
        controller_app.mkdir()
        launcher_repo.mkdir()

        (controller_app / 'VERSION').write_text('2.5.1\n')
        (launcher_repo / 'VERSION').write_text('1.0.0\n')

        # Test APP_ROOT/VERSION takes precedence over launcher REPO_ROOT
        script = f"""set -euo pipefail
APP_ROOT='{controller_app}'
REPO_ROOT='{launcher_repo}'
RELEASE_REF=''
{DETECT_RELEASE_REF_FN}
detect_release_ref
"""
        result = subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'v2.5.1')

        # Test explicit RELEASE_REF takes precedence over VERSION file
        script_explicit_ref = f"""set -euo pipefail
APP_ROOT='{controller_app}'
REPO_ROOT='{launcher_repo}'
RELEASE_REF='v9.8.7'
{DETECT_RELEASE_REF_FN}
detect_release_ref
"""
        result_explicit = subprocess.run(['bash', '-c', script_explicit_ref], capture_output=True, text=True, timeout=10)
        self.assertEqual(result_explicit.returncode, 0, result_explicit.stderr)
        self.assertEqual(result_explicit.stdout.strip(), 'v9.8.7')

        # Test missing VERSION falls back to latest
        empty_app = self.root / 'empty_app'
        empty_app.mkdir()
        script_fallback = f"""set -euo pipefail
APP_ROOT='{empty_app}'
REPO_ROOT='{launcher_repo}'
RELEASE_REF=''
{DETECT_RELEASE_REF_FN}
detect_release_ref
"""
        result_fallback = subprocess.run(['bash', '-c', script_fallback], capture_output=True, text=True, timeout=10)
        self.assertEqual(result_fallback.returncode, 0, result_fallback.stderr)
        self.assertEqual(result_fallback.stdout.strip(), 'latest')
