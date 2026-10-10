from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from tests.test_backend_http import BackendHTTPTests
from backend.authorization import Actor, Principal, PrincipalKind, ADMIN_PERMISSIONS, AccessDenied
from backend.updates import UpdateService, same_commit
from backend.agent_rollout import AgentRolloutService


class BackendUpdateTests(TestCase):
    setUp = BackendHTTPTests.setUp

    def service(self, **driver):
        install_env = patch.dict('os.environ', {'NODE_PLANE_APP_DIR': '/opt/node-plane/current'})
        install_env.start()
        self.addCleanup(install_env.stop)
        sleeper = patch('backend.updates.time.sleep')
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.actor = Actor(Principal('test', PrincipalKind.SERVICE, ADMIN_PERMISSIONS), self.admin)
        self.updater = SimpleNamespace(
            get_updates_overview=Mock(return_value={'branch': 'dev', 'update_supported': True}),
            list_available_versions=Mock(return_value={'status': 'ok', 'branch': 'dev', 'versions': [
                {'version': '0.4.3-alpha.19', 'ref': 'v0.4.3-alpha.19', 'allowed': True,
                 'action': 'upgrade', 'kind': 'tag', 'commit': 'a' * 40},
                {'version': '0.3.0', 'ref': 'v0.3.0', 'allowed': False, 'action': 'blocked', 'kind': 'tag'},
                {'version': 'dev HEAD', 'ref': 'origin/dev', 'allowed': True,
                 'action': 'upgrade', 'kind': 'head', 'commit': 'b' * 40}]}),
            is_driver_agents_setup_supported=Mock(return_value=True),
            schedule_update=Mock(return_value={'status': 'running', 'unit_name': 'test-update'}),
            refresh_update_run_state=Mock(return_value={'last_run_status': 'success', 'last_run_unit': 'test-update'}))
        self.driver = SimpleNamespace(binary_info=Mock(return_value={'commit': 'a' * 40}),
            inspect_node_services=Mock(return_value={'agent_commit': 'a' * 40, 'runtime_commit': 'a' * 40}), **driver)
        return UpdateService(self.db, self.driver, self.updater)

    def node(self):
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_nodes(key,title,region,enabled,protocols_json,xray_transports_json) VALUES ('lv1','Latvia','EU',0,'[]','[]')")
            conn.execute("INSERT INTO backend_node_connections(node_key,transport,ssh_target) VALUES ('lv1','ssh','root@lv1.example')")

    def test_dismiss_is_persistent_per_administrator_and_never_cancels_an_active_job(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'version',
            target_ref='v0.4.3-alpha.19', branch='dev')
        with self.assertRaises(AccessDenied) as rejected:
            service.dismiss_result(self.actor, job['id'])
        self.assertEqual(rejected.exception.code, 'update_result_active')
        self.db.connection.execute("UPDATE backend_update_jobs SET status='succeeded' WHERE id=?", (job['id'],))
        service.dismiss_result(self.actor, job['id'])
        service.dismiss_result(self.actor, job['id'])
        rebuilt = UpdateService(self.db, self.driver, self.updater)
        self.assertEqual(rebuilt.dismissed_results(self.actor), [job['id']])
        other = Actor(Principal('other', PrincipalKind.SERVICE, ADMIN_PERMISSIONS),
                      SimpleNamespace(id='other-admin', role='admin', status='approved'))
        self.assertEqual(rebuilt.dismissed_results(other), [])
        self.assertEqual(rebuilt.get(self.actor, job['id'])['status'], 'succeeded')

    def test_dismiss_http_endpoint_preserves_job_and_checks_admin_permission(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'version',
            target_ref='v0.4.3-alpha.19', branch='dev')
        path = '/api/v1/system/updates/jobs/' + job['id'] + '/dismiss'
        admin_headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}
        active = self.client.post(path, headers=admin_headers)
        self.assertEqual(active.status_code, 409, active.text)
        self.db.connection.execute("UPDATE backend_update_jobs SET status='succeeded' WHERE id=?", (job['id'],))
        dismissed = self.client.post(path, headers=admin_headers)
        self.assertEqual(dismissed.status_code, 200, dismissed.text)
        self.assertEqual(dismissed.json(), {'dismissed_job_id': job['id']})
        self.assertEqual(service.dismissed_results(self.actor), [job['id']])
        BackendHTTPTests.register(self, 102)
        denied = self.client.post(path, headers={**self.headers, 'X-Node-Plane-Telegram-User-ID': '102'})
        self.assertEqual(denied.status_code, 403, denied.text)

    def test_version_target_validation_and_idempotent_worker_launch(self):
        service = self.service()
        for ref, branch in [('v0.3.0', 'dev'), ('arbitrary;command', 'dev'), ('v0.4.3-alpha.19', 'main')]:
            with self.assertRaises(AccessDenied):
                service.queue(self.actor, str(uuid4()), 'version', target_ref=ref, branch=branch)
        key = str(uuid4())
        first = service.queue(self.actor, key, 'version', target_ref='v0.4.3-alpha.19', branch='dev')
        self.assertEqual(service.queue(self.actor, key, 'version', target_ref='v0.4.3-alpha.19', branch='dev')['id'], first['id'])
        self.assertTrue(service.run_one())
        service.updater.schedule_update.assert_called_once_with(branch='dev', target_ref='v0.4.3-alpha.19')
        self.assertTrue(service.run_one())
        self.assertEqual(service.get(self.actor, first['id'])['status'], 'succeeded')
        service.updater.schedule_update.assert_called_once()

    def test_dev_head_is_pinned_to_confirmed_commit(self):
        service = self.service()
        service.queue(self.actor, str(uuid4()), 'version', target_ref='origin/dev', branch='dev')
        service.run_one()
        service.updater.schedule_update.assert_called_once_with(branch='dev', target_ref='b' * 40)

    def test_interrupted_systemd_launch_never_replays(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'version', target_ref='v0.4.3-alpha.19', branch='dev')
        self.db.connection.execute("UPDATE backend_update_jobs SET status='running',result_json=NULL")
        service.recover()
        self.assertFalse(service.run_one())
        service.updater.schedule_update.assert_not_called()
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'blocked')

    def test_rollout_checks_binary_and_runtime_commits_separately(self):
        service = self.service()
        self.node()
        service.driver.inspect_node_services.return_value = {'agent_commit': 'a' * 40, 'runtime_commit': 'b' * 40}
        with patch('config.APP_COMMIT', 'a' * 40):
            value = service.rollout_overview(self.actor)
        self.assertFalse(value['agents_required'])
        self.assertTrue(value['runtimes_required'])
        service.driver.inspect_node_services.side_effect = RuntimeError('secret error')
        with patch('config.APP_COMMIT', 'a' * 40):
            value = service.rollout_overview(self.actor)
        self.assertEqual(value['nodes'][0]['agent_status'], 'unknown')
        self.assertNotIn('secret', str(value))

    def test_batch_queues_only_outdated_agents_and_verifies_actual_result(self):
        service = self.service()
        self.node()
        service.driver.inspect_node_services.return_value = {'agent_commit': 'b' * 40, 'runtime_commit': 'a' * 40}
        with patch('config.APP_COMMIT', 'a' * 40):
            job = service.queue(self.actor, str(uuid4()), 'agents')
        self.assertTrue(service.run_one())
        child = service.get(self.actor, job['id'])['items'][0]['child_id']
        self.assertTrue(AgentRolloutService(self.db, lambda args: True).run_one())
        # An installer exit code alone cannot make the old binary current.
        service.run_one()
        service.run_one()
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'blocked')
        self.assertIsNotNone(child)

    def test_runtime_batch_reuses_native_durable_job(self):
        service = self.service()
        self.node()
        service.driver.inspect_node_services.return_value = {'agent_commit': 'a' * 40, 'runtime_commit': 'b' * 40}
        with patch('config.APP_COMMIT', 'a' * 40):
            job = service.queue(self.actor, str(uuid4()), 'runtimes')
        service.run_one()
        item = service.get(self.actor, job['id'])['items'][0]
        self.assertEqual(self.db.connection.execute('SELECT action FROM backend_node_jobs WHERE id=?', (item['child_id'],)).fetchone()['action'], 'sync_runtime')
        self.db.connection.execute("UPDATE backend_node_jobs SET status='succeeded'")
        service.driver.inspect_node_services.return_value['runtime_commit'] = 'a' * 40
        service.run_one()
        service.run_one()
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'succeeded')

    def test_agent_restart_verification_retries_reads_without_replaying_install(self):
        service = self.service()
        self.node()
        service.driver.inspect_node_services.return_value = {'agent_commit': 'b' * 40}
        with patch('config.APP_COMMIT', 'a' * 40):
            job = service.queue(self.actor, str(uuid4()), 'agents')
        service.run_one()
        runner = Mock(return_value=True)
        rollout = AgentRolloutService(self.db, runner)
        with patch.dict('os.environ', {'NODE_PLANE_APP_DIR': '/opt/node-plane/current'}):
            self.assertTrue(rollout.run_one())
        service.driver.inspect_node_services.side_effect = [
            RuntimeError('restart in progress'), {'agent_commit': 'b' * 40}, {'agent_commit': 'a' * 40}]
        service.run_one()
        service.run_one()
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'succeeded')
        runner.assert_called_once()
        self.assertFalse(rollout.run_one())

    def test_agent_verification_remains_blocked_if_service_never_returns(self):
        service = self.service()
        verified, error = service._verify_commit(Mock(side_effect=RuntimeError()), 'agent_commit', 'a' * 40)
        self.assertFalse(verified)
        self.assertEqual(error, 'update_verification_unavailable')

    def test_current_components_do_not_offer_updates(self):
        service = self.service()
        self.node()
        with patch('config.APP_COMMIT', 'a' * 40):
            with self.assertRaises(AccessDenied) as error:
                service.queue(self.actor, str(uuid4()), 'agents')
        self.assertEqual(error.exception.code, 'update_not_required')
        self.assertFalse(same_commit('unknown', 'unknown'))

    def test_driver_only_rollout_works_without_registered_nodes(self):
        service = self.service()
        service.runner = Mock(return_value=True)
        with patch('config.APP_COMMIT', 'b' * 40):
            job = service.queue(self.actor, str(uuid4()), 'agents')
        self.assertEqual(service.get(self.actor, job['id'])['items'][0]['node_key'], '@driver')
        service.driver.binary_info.return_value = {'commit': 'b' * 40}
        with patch.dict('os.environ', {'NODE_PLANE_APP_DIR': '/opt/node-plane/current'}):
            service.run_one()
        service.run_one()
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'succeeded')
        self.assertIn('--skip-agents', service.runner.call_args.args[0])

    def test_interrupted_driver_install_is_blocked_without_replay(self):
        service = self.service()
        with patch('config.APP_COMMIT', 'b' * 40):
            job = service.queue(self.actor, str(uuid4()), 'agents')
        self.db.connection.execute("UPDATE backend_update_items SET status='running' WHERE node_key='@driver'")
        service.runner = Mock()
        service.recover()
        service.run_one()
        service.runner.assert_not_called()
        self.assertEqual(service.get(self.actor, job['id'])['status'], 'blocked')

    def test_versions_route_and_commands_require_admin_and_uuid_key(self):
        service = self.service()
        from backend.http_api import create_app
        from fastapi.testclient import TestClient
        with patch('backend.updates.UpdateService.updater', new=property(lambda _: self.updater)):
            with TestClient(create_app(self.db, node_driver=self.driver)) as client:
                headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}
                value = client.get('/api/v1/system/updates/versions?offset=2', headers=headers)
                self.assertEqual(value.status_code, 200, value.text)
                self.assertEqual(value.json()['items'][0]['ref'], 'origin/dev')
                body = {'kind': 'version', 'target_ref': 'v0.4.3-alpha.19', 'branch': 'dev'}
                self.assertEqual(client.post('/api/v1/system/updates/run', headers=headers, json=body).status_code, 422)
                response = client.post('/api/v1/system/updates/run', headers={**headers, 'Idempotency-Key': str(uuid4())}, json=body)
                self.assertEqual(response.status_code, 202, response.text)
                member = self.identities.resolve_telegram(102)
                self.db.connection.execute("UPDATE backend_accounts SET status='approved' WHERE id=?", (member.id,))
                self.assertEqual(client.get('/api/v1/system/updates/versions', headers={**headers, 'X-Node-Plane-Telegram-User-ID': '102'}).status_code, 403)
