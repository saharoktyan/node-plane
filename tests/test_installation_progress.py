import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from backend.installation_progress import progress_path, read_progress, write_progress


class InstallationProgressTests(TestCase):
    def test_progress_is_bounded_and_bound_to_exact_operation(self):
        with TemporaryDirectory() as shared, patch.dict(os.environ, NODE_PLANE_SHARED_DIR=shared):
            identity = str(uuid4())
            row = {'id': identity, 'status': 'running'}
            path = progress_path(identity)
            write_progress(path, identity, 'check SSH prerequisites for root@private-host')
            self.assertEqual(read_progress(row), {'stage': 'check_ssh', 'label': 'Checking SSH access'})
            self.assertNotIn('private-host', path.read_text())
            write_progress(path, identity, 'prepare mutual TLS certificate for node1')
            self.assertEqual(read_progress(row)['stage'], 'certificates')
            self.assertIsNone(read_progress(dict(row, status='succeeded')))
            path.write_text(json.dumps({'operation_id': str(uuid4()), 'stage': 'install_agent'}))
            self.assertIsNone(read_progress(row))
            path.write_text('x' * 4096)
            self.assertIsNone(read_progress(row))
            path.write_text(json.dumps({'operation_id': identity, 'stage': 'secret output'}))
            self.assertIsNone(read_progress(row))

    def test_actual_shell_step_publishes_observation_without_leaking_target(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / 'scripts/setup_driver_agents.sh').read_text()
        function = source.split('set_step() {', 1)[1].split('\n}\n', 1)[0]
        with TemporaryDirectory() as shared, patch.dict(os.environ, NODE_PLANE_SHARED_DIR=shared):
            identity = str(uuid4())
            path = progress_path(identity)
            result = subprocess.run(['bash', '-c', 'set_step() {' + function +
                '\n}\nset_step "install node-agent on hidden-host"'],
                env={**os.environ, 'APP_ROOT': str(root),
                     'NODE_PLANE_AGENT_PROGRESS_FILE': str(path),
                     'NODE_PLANE_AGENT_PROGRESS_ID': identity}, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, '')
            self.assertEqual(read_progress({'id': identity, 'status': 'running'})['stage'], 'install_agent')
            self.assertNotIn('hidden-host', path.read_text())

    def test_remote_observer_is_read_only_and_stops_with_executor(self):
        from backend.installation_progress import observe_node_job
        from threading import Event
        from types import SimpleNamespace
        identity = str(uuid4())
        received = Event()
        calls = []
        def getter(node, command):
            calls.append((node, command))
            received.set()
            return {'operation_id': command, 'stage': 'protocol_verify'}
        with TemporaryDirectory() as shared, patch.dict(os.environ, NODE_PLANE_SHARED_DIR=shared):
            row = {'id': identity, 'node_key': 'lv1', 'status': 'running', 'action': 'bootstrap'}
            with observe_node_job(row, SimpleNamespace(node_action_progress=getter)):
                self.assertTrue(received.wait(3))
            self.assertEqual(calls, [('lv1', identity)])
            # Closing the observer may race its last response; either valid observation is safe.
            self.assertIn(read_progress(row)['stage'], {'runtime_sync', 'protocol_verify'})

