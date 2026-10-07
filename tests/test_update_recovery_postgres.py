"""Run separately against disposable PostgreSQL; systemd and agents are fixtures."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from backend.authorization import AccessDenied
from backend.agent_rollout import AgentRolloutService
from backend.updates import UpdateService
from backend.maintenance_gate import admit
from tests import test_backups_postgres as fixture
from tests.test_core_reliability_postgres import Crash


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class UpdateRecoveryPostgresTests(unittest.TestCase):
    drop_schema = fixture.BackupsPostgresTests.drop_schema

    def setUp(self):
        fixture.BackupsPostgresTests.setUp(self)
        self.updater = SimpleNamespace(
            get_updates_overview=Mock(return_value={'branch': 'dev', 'update_supported': True}),
            list_available_versions=Mock(return_value={'status': 'ok', 'versions': [
                {'ref': 'v-test', 'kind': 'tag', 'allowed': True, 'commit': 'a' * 40}]}),
            schedule_update=Mock(return_value={'status': 'running', 'unit_name': 'test-update'}),
            refresh_update_run_state=Mock(return_value={'last_run_status': 'success', 'last_run_unit': 'test-update'}))
        self.driver = SimpleNamespace(inspect_node_services=Mock(return_value={'agent_commit': 'a' * 40}))
        self.service = UpdateService(self.db, self.driver, self.updater)
        self.service.initialize_schema()

    def queue(self):
        return self.service.queue(self.actor, str(uuid4()), 'stack', target_ref='v-test', branch='dev')

    def gate(self):
        with self.db.connect() as conn:
            return conn.execute('SELECT job_id FROM backend_controller_update_gate WHERE id=1').fetchone()

    def node(self, key):
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_nodes(key,title,region,protocols_json) VALUES (?,?,'Europe','[]')", (key, key))
            conn.execute("INSERT INTO backend_node_connections(node_key,transport) VALUES (?,'local')", (key,))

    def fail_core(self, rollback='failed'):
        job = self.queue()
        self.service.run_one()
        self.updater.refresh_update_run_state.return_value['last_run_status'] = 'failed'
        with patch.object(self.service, '_stack_progress', return_value={'rollback_status': rollback}):
            self.service.run_one()
        return job

    def test_core_failure_rolls_back_before_agents_and_releases_gate(self):
        self.node('one')
        job = self.fail_core('succeeded')
        self.assertEqual(self.service.get(self.actor, job['id'])['status'], 'rolled_back')
        self.assertEqual(self.service.get(self.actor, job['id'])['items'][0]['status'], 'skipped')
        self.assertIsNone(self.gate())
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS count FROM backend_agent_rollouts').fetchone()['count'], 0)

    def test_failed_rollback_keeps_gate_until_durable_rollback_confirmation(self):
        job = self.fail_core()
        self.assertIsNotNone(self.gate())
        with patch.object(self.service, '_stack_progress', return_value={'rollback_status': 'failed'}):
            self.assertEqual(self.service.recheck(self.actor, job['id'])['status'], 'blocked')
        self.assertIsNotNone(self.gate())
        with patch.object(self.service, '_stack_progress', return_value={'rollback_status': 'succeeded'}):
            self.assertEqual(self.service.recheck(self.actor, job['id'])['status'], 'rolled_back')
        self.assertIsNone(self.gate())
        self.updater.schedule_update.assert_called_once()

    def test_late_health_confirmation_resumes_without_install_replay(self):
        job = self.queue()
        self.service.run_one()
        with patch.object(self.service, '_stack_progress', return_value={}):
            self.service.run_one()
        self.assertEqual(self.service.get(self.actor, job['id'])['status'], 'blocked')
        with patch.object(self.service, '_stack_progress', return_value={'status': 'succeeded'}):
            restored = self.service.recheck(self.actor, job['id'])
        self.assertEqual(restored['status'], 'running')
        self.assertEqual(restored['result']['phase'], 'agents')
        self.assertIsNone(self.gate())
        self.service.run_one()
        self.assertEqual(self.service.get(self.actor, job['id'])['status'], 'succeeded')
        self.updater.schedule_update.assert_called_once()

    def test_other_update_unit_cannot_unlock_failed_job(self):
        job = self.fail_core()
        self.updater.refresh_update_run_state.return_value['last_run_unit'] = 'unrelated-update'
        with patch.object(self.service, '_stack_progress', return_value={'rollback_status': 'succeeded'}):
            self.assertEqual(self.service.recheck(self.actor, job['id'])['status'], 'blocked')
        self.assertIsNotNone(self.gate())

    def test_queued_cancel_is_idempotent_and_releases_gate_without_launch(self):
        self.node('one')
        job = self.queue()
        result = self.service.cancel(self.actor, job['id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['items'][0]['status'], 'skipped')
        self.assertEqual(self.service.cancel(self.actor, job['id']), result)
        self.assertIsNone(self.gate())
        with self.db.transaction() as conn:
            admit(conn)
        self.assertFalse(self.service.run_one())
        self.updater.schedule_update.assert_not_called()

    def test_started_update_cannot_be_cancelled(self):
        job = self.queue()
        self.service.run_one()
        with self.assertRaises(AccessDenied) as denied:
            self.service.cancel(self.actor, job['id'])
        self.assertEqual(denied.exception.code, 'update_cancel_unsafe')
        self.assertIsNotNone(self.gate())

    def test_crash_at_launch_boundary_is_blocked_and_never_replayed(self):
        job = self.queue()
        self.updater.schedule_update.side_effect = Crash()
        with self.assertRaises(Crash):
            self.service.run_one()
        self.service.recover()
        self.assertFalse(self.service.run_one())
        with self.assertRaises(AccessDenied) as denied:
            self.service.recheck(self.actor, job['id'])
        self.assertEqual(denied.exception.code, 'update_recovery_unconfirmed')
        self.assertIsNotNone(self.gate())
        self.updater.schedule_update.assert_called_once()

    def test_agent_failure_is_partial_and_other_agent_continues(self):
        self.node('one')
        self.node('two')
        job = self.queue()
        self.service.run_one()
        with patch.object(self.service, '_stack_progress', return_value={'status': 'succeeded'}):
            self.service.run_one()
        runner = Mock(side_effect=[False, True])
        rollouts = AgentRolloutService(self.db, runner)
        with patch.dict(os.environ, {'NODE_PLANE_APP_DIR': '/opt/node-plane/current'}):
            for _ in range(10):
                changed = self.service.run_one()
                rollout_changed = rollouts.run_one()
                if not changed and not rollout_changed:
                    break
        result = self.service.get(self.actor, job['id'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual({item['status'] for item in result['items']}, {'blocked', 'succeeded'})
        self.assertIsNone(self.gate())
        self.assertEqual(runner.call_count, 2)
        self.assertTrue(all('--skip-driver' in call.args[0] for call in runner.call_args_list))
        self.updater.schedule_update.assert_called_once()

    def test_demoted_actor_cannot_release_gate(self):
        job = self.queue()
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET role='member' WHERE id=?", (self.admin.id,))
        with self.assertRaises(AccessDenied) as denied:
            self.service.cancel(self.actor, job['id'])
        self.assertEqual(denied.exception.code, 'permission_denied')
        self.assertIsNotNone(self.gate())

    def test_http_cancel_remains_authorized_and_reachable_behind_own_gate(self):
        import tempfile
        from pathlib import Path
        from backend.credentials import CredentialService
        from backend.authorization import PrincipalKind, ADMIN_PERMISSIONS
        from backend.http_api import create_app
        from fastapi.testclient import TestClient
        _, token = CredentialService(self.db).issue(PrincipalKind.ACCOUNT, ADMIN_PERMISSIONS, account_id=self.admin.id)
        headers = {'Authorization': 'Bearer ' + token}
        job = self.queue()
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'data').mkdir()
            with patch.dict(os.environ, {'NODE_PLANE_SHARED_DIR': directory}), TestClient(create_app(self.db, node_driver=self.driver)) as client:
                path = f"/api/v1/system/updates/jobs/{job['id']}/cancel"
                self.assertEqual(client.post(path).status_code, 401)
                self.assertEqual(client.post('/api/v1/system/updates/check', headers=headers).status_code, 409)
                response = client.post(path, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['status'], 'cancelled')
        self.assertIsNone(self.gate())
