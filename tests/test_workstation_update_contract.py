"""The workstation uses account credentials and durable backend update jobs.

No installer, SSH connection or systemd command runs in these API contract tests.
"""
import json
import importlib
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.authorization import PrincipalKind
from backend.http_api import create_app
from tests import test_backend_updates as fixture


class WorkstationUpdateContractTests(TestCase):
    setUp = fixture.BackendUpdateTests.setUp
    service = fixture.BackendUpdateTests.service
    node = fixture.BackendUpdateTests.node

    def client_with_updates(self):
        self.updates = self.service()
        updater_patch = patch('backend.updates.UpdateService.updater',
                              new=property(lambda _: self.updater))
        updater_patch.start()
        self.addCleanup(updater_patch.stop)
        client = TestClient(create_app(self.db, node_driver=self.driver))
        self.addCleanup(client.close)
        return client

    def account_headers(self, account=None, scopes=None):
        account = account or self.admin
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT,
            frozenset(scopes or {'settings.manage', 'account.self.read'}),
            account_id=account.id)
        return {'Authorization': 'Bearer ' + token}

    @staticmethod
    def intent(**changes):
        return {'kind': 'stack', 'target_ref': 'v0.4.3-alpha.19',
                'branch': 'dev', **changes}

    def queue(self, client, headers, key=None, **changes):
        return client.post('/api/v1/system/updates/run',
            headers={**headers, 'Idempotency-Key': key or str(uuid4())},
            json=self.intent(**changes))

    def release_core_gate(self):
        # Simulate the verified core phase boundary, not a recovery action.
        with self.db.transaction() as conn:
            conn.execute('DELETE FROM backend_controller_update_gate')

    def test_account_credential_queues_full_stack_and_reads_progress_through_gate(self):
        client = self.client_with_updates()
        headers = self.account_headers()
        response = self.queue(client, headers)
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()
        self.assertEqual(job['kind'], 'stack')
        self.assertEqual(job['status'], 'awaiting_executor')
        self.assertEqual(job['target_ref'], 'v0.4.3-alpha.19')
        self.assertEqual(job['branch'], 'dev')
        self.assertEqual(job['result'], {'resolved_ref': 'v0.4.3-alpha.19',
            'expected_commit': 'a' * 40, 'phase': 'core'})
        self.assertEqual(job['items'], [])
        self.assertNotIn(headers['Authorization'][7:], response.text)
        self.assertNotIn('actor_id', job)
        self.assertNotIn('command_key', job)
        progress = client.get('/api/v1/system/updates/jobs/' + job['id'],
                              headers=headers)
        self.assertEqual(progress.status_code, 200, progress.text)
        self.assertEqual(progress.json()['id'], job['id'])
        self.assertEqual(progress.headers['Cache-Control'], 'no-store')
        blocked = client.post('/api/v1/system/updates/check', headers=headers)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()['error']['code'], 'system_cleanup_in_progress')
        self.updater.schedule_update.assert_not_called()

    def test_lost_queue_response_cannot_be_replayed_through_the_core_gate(self):
        client = self.client_with_updates()
        headers = self.account_headers()
        key = str(uuid4())
        first = self.queue(client, headers, key)
        self.assertEqual(first.status_code, 202, first.text)
        replay = self.queue(client, headers, key)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json()['error']['code'], 'system_cleanup_in_progress')
        with self.db.connect() as conn:
            exact = conn.execute('SELECT id FROM backend_update_jobs '
                'WHERE actor_id=? AND command_key=?', (self.admin.id, key)).fetchone()
            self.assertEqual(exact['id'], first.json()['id'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM backend_update_jobs').fetchone()[0], 1)
        self.updater.schedule_update.assert_not_called()

    def test_recheck_reconciles_exact_blocked_job_without_relaunching_installation(self):
        client=self.client_with_updates()
        headers=self.account_headers(scopes={'settings.manage','maintenance.manage'})
        job=self.queue(client,headers).json()
        self.updates.run_one()
        current=self.updates.get(self.actor,job['id'])
        self.updates._finish(job['id'],'blocked',{**current['result'],'error_code':'update_verification_unavailable'})
        with tempfile.TemporaryDirectory() as shared, patch.dict('os.environ',{'NODE_PLANE_SHARED_DIR':shared}), patch('backend.updates.UpdateService._stack_progress',return_value={'status':'succeeded'}):
            (Path(shared)/'data').mkdir()
            result=client.post('/api/v1/system/updates/jobs/'+job['id']+'/recheck',headers=headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['status'],'running')
        self.assertEqual(result.json()['result']['phase'],'agents')
        self.updater.schedule_update.assert_called_once()

    def test_new_session_for_same_account_preserves_mutation_identity(self):
        client = self.client_with_updates()
        key = str(uuid4())
        original_headers = self.account_headers()
        first = self.queue(client, original_headers, key)
        self.release_core_gate()
        replacement_headers = self.account_headers()
        self.assertNotEqual(original_headers, replacement_headers)
        replay = self.queue(client, replacement_headers, key)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()['id'], first.json()['id'])
        conflict = self.queue(client, replacement_headers, key, target_ref='origin/dev')
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()['error']['code'], 'idempotency_conflict')
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_update_jobs').fetchone()[0], 1)

    def test_same_key_for_a_different_account_is_a_different_mutation(self):
        client = self.client_with_updates()
        key = str(uuid4())
        first = self.queue(client, self.account_headers(), key).json()
        self.updates._finish(first['id'], 'succeeded')
        another = self.identities.create_account(status='approved')
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET role='admin' WHERE id=?", (another.id,))
        second = self.queue(client, self.account_headers(another), key)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertNotEqual(second.json()['id'], first['id'])

    def test_admin_role_and_scope_are_checked_again_after_reconnection(self):
        client = self.client_with_updates()
        headers = self.account_headers()
        first = self.queue(client, headers).json()
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET role='member' WHERE id=?", (self.admin.id,))
        denied = client.get('/api/v1/system/updates/jobs/' + first['id'], headers=headers)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()['error']['code'], 'permission_denied')
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET role='admin' WHERE id=?", (self.admin.id,))
        restricted = self.account_headers(scopes={'account.self.read'})
        self.assertEqual(client.get('/api/v1/system/updates/jobs/' + first['id'],
                                   headers=restricted).status_code, 403)
        delegated = {**headers, 'X-Node-Plane-Telegram-User-ID': '101'}
        self.assertEqual(client.get('/api/v1/system/updates/jobs/' + first['id'],
                                   headers=delegated).status_code, 403)

    def test_partial_rollback_and_blocked_results_are_distinct_durable_outcomes(self):
        client = self.client_with_updates()
        self.node()
        headers = self.account_headers()
        job = self.queue(client, headers).json()
        components = {'backend': 'succeeded', 'worker': 'succeeded',
                      'driver': 'succeeded', 'telegram': 'succeeded'}
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_update_items SET status='blocked',"
                "error_code='node_update_failed' WHERE job_id=?", (job['id'],))
        for status, phase, rollback in (
            ('partial', 'agents', None),
            ('rolled_back', 'core', 'succeeded'),
            ('blocked', 'core', 'failed'),
        ):
            with self.subTest(status=status):
                result = {'phase': phase, 'components': components,
                          'error_code': 'core_update_failed'}
                if rollback is not None:
                    result['rollback_status'] = rollback
                self.updates._finish(job['id'], status, result)
                response = client.get('/api/v1/system/updates/jobs/' + job['id'], headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                value = response.json()
                self.assertEqual(value['status'], status)
                self.assertEqual(value['result'], result)
                self.assertEqual(value['items'][0]['node_key'], 'lv1')
                self.assertEqual(value['items'][0]['title'], 'Latvia')
                self.assertEqual(value['items'][0]['region'], 'EU')
                self.assertEqual(value['items'][0]['phase'], 'agent')
                self.assertEqual(value['items'][0]['error_code'], 'node_update_failed')
                self.assertNotIn('ssh_target', response.text)

    def test_version_catalog_paginates_and_dev_head_is_pinned_before_execution(self):
        client = self.client_with_updates()
        headers = self.account_headers()
        catalog = client.get('/api/v1/system/updates/versions?offset=2', headers=headers)
        self.assertEqual(catalog.status_code, 200, catalog.text)
        self.assertEqual(catalog.json()['total'], 3)
        self.assertEqual(catalog.json()['offset'], 2)
        self.assertIsNone(catalog.json()['next_offset'])
        self.assertEqual(catalog.json()['items'][0]['ref'], 'origin/dev')
        head = self.queue(client, headers, target_ref='origin/dev')
        self.assertEqual(head.status_code, 202, head.text)
        self.assertEqual(head.json()['target_ref'], 'origin/dev')
        self.assertEqual(head.json()['result']['resolved_ref'], 'b' * 40)
        self.assertEqual(head.json()['result']['expected_commit'], 'b' * 40)

    def test_channel_selection_and_unsafe_targets_fail_without_queueing(self):
        client = self.client_with_updates()
        headers = self.account_headers()
        for changes, code in (
            ({'branch': 'main'}, 'update_selection_changed'),
            ({'target_ref': 'v0.3.0'}, 'update_target_blocked'),
            ({'target_ref': 'origin/dev;systemctl restart'}, 'update_target_blocked'),
        ):
            with self.subTest(changes=changes):
                response = self.queue(client, headers, **changes)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()['error']['code'], code)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_update_jobs').fetchone()[0], 0)
        self.updater.schedule_update.assert_not_called()

    def test_check_returns_check_result_and_overview_adds_exact_latest_job(self):
        client = self.client_with_updates()
        headers = self.account_headers()
        check_payload = {'status': 'available', 'branch': 'dev',
            'remote_version': '0.4.3-alpha.19', 'upstream_ref': 'v0.4.3-alpha.19'}
        overview_payload = {'update_supported': True, 'branch': 'dev', 'dev_track': 'tag',
            'current_version': '0.4.3-alpha.18', 'update_available': True,
            'remote_version': '0.4.3-alpha.19', 'upstream_ref': 'v0.4.3-alpha.19'}
        self.updater.check_for_updates = Mock(return_value=check_payload)
        self.updater.get_updates_overview.return_value = overview_payload
        # The service-layer module owns a process-global production database.
        # Substitute it at the HTTP boundary so this test stays self-contained.
        package = importlib.import_module('app.services')
        with patch.dict('sys.modules', {'app.services.updates': self.updater}), \
                patch.object(package, 'updates', self.updater, create=True):
            result = client.post('/api/v1/system/updates/check', headers=headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), check_payload)
        self.updater.check_for_updates.assert_called_once_with()
        job = self.queue(client, headers).json()
        with patch.dict('sys.modules', {'app.services.updates': self.updater}), \
                patch.object(package, 'updates', self.updater, create=True):
            overview = client.get('/api/v1/system/updates', headers=headers)
        self.assertEqual(overview.status_code, 200, overview.text)
        self.assertEqual(overview.json()['latest_job']['id'], job['id'])
        self.assertEqual(overview.json()['upstream_ref'], 'v0.4.3-alpha.19')
        self.assertNotIn('command_key', json.dumps(overview.json()))
