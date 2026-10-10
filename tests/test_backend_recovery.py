from unittest import TestCase
from uuid import uuid4

from tests import test_backend_updates as fixture
from backend.recovery import overview


class RecoveryTests(TestCase):
    setUp = fixture.BackendUpdateTests.setUp
    service = fixture.BackendUpdateTests.service

    def test_queued_update_is_cancellable_but_uncertain_core_requires_evidence(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'stack', target_ref='v0.4.3-alpha.19', branch='dev')
        self.assertEqual(overview(self.db, self.actor)['items'][0]['actions'], ['cancel'])
        self.db.connection.execute("UPDATE backend_update_jobs SET status='blocked',result_json='{}'")
        self.assertEqual(overview(self.db, self.actor)['items'][0]['actions'], [])
        self.db.connection.execute("UPDATE backend_update_jobs SET result_json=?",
            ('{"phase":"core","unit_name":"test-update","secret":"never-show"}',))
        page = overview(self.db, self.actor)
        self.assertEqual(page['items'][0]['id'], job['id'])
        self.assertEqual(page['items'][0]['actions'], ['recheck'])
        self.assertNotIn('never-show', str(page))

    def test_pagination_handles_shrinking_list(self):
        self.service()
        for _ in range(12):
            identity = str(uuid4())
            self.db.connection.execute("INSERT INTO backend_update_jobs(id,actor_id,command_key,kind,intent_json,status,created_at) VALUES (?,?,?,'agents','{}','blocked','now')",
                (identity, self.admin.id, str(uuid4())))
        page = overview(self.db, self.actor, 10)
        self.assertEqual((page['total'], len(page['items']), page['offset']), (12, 2, 10))
        self.db.connection.execute("UPDATE backend_update_jobs SET status='succeeded'")
        page = overview(self.db, self.actor, 10)
        self.assertEqual((page['total'], page['offset'], page['items']), (0, 0, []))

    def test_started_child_prevents_cancel_even_if_parent_still_queued(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'stack', target_ref='v0.4.3-alpha.19', branch='dev')
        self.db.connection.execute("INSERT INTO backend_update_items VALUES (?,'node','{}','awaiting_executor',?,NULL)",
            (job['id'], str(uuid4())))
        self.assertEqual(overview(self.db, self.actor)['items'][0]['actions'], [])

    def test_another_update_gate_does_not_offer_recovery_for_unrelated_job(self):
        service = self.service()
        service.queue(self.actor, str(uuid4()), 'stack', target_ref='v0.4.3-alpha.19', branch='dev')
        self.db.connection.execute('UPDATE backend_controller_update_gate SET job_id=?', (str(uuid4()),))
        page = overview(self.db, self.actor)
        self.assertTrue(page['maintenance_active'])
        self.assertEqual(page['items'][0]['actions'], [])

    def test_inventory_requires_authorized_admin_and_includes_node_removal(self):
        self.service()
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}
        self.assertEqual(self.client.get('/api/v1/system/recovery').status_code, 401)
        self.register = lambda: self.client.post('/api/v1/integrations/telegram/identities/resolve',
            headers={**self.headers, 'Idempotency-Key': str(uuid4())}, json={'telegram_user_id': 202})
        self.register()
        self.assertEqual(self.client.get('/api/v1/system/recovery', headers={**self.headers,
            'X-Node-Plane-Telegram-User-ID': '202'}).status_code, 403)
        self.db.connection.execute("INSERT INTO backend_node_removals VALUES ('old-node',?,'blocked','node_cleanup_unavailable')", (self.admin.id,))
        response = self.client.get('/api/v1/system/recovery', headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['items'][0]['kind'], 'removal')
        self.assertEqual(response.json()['items'][0]['actions'], [])


class RecoveryHTTPTests(TestCase):
    setUp = fixture.BackendUpdateTests.setUp
    service = fixture.BackendUpdateTests.service

    def test_worker_lock_admin_and_targeted_recheck_are_enforced_by_api(self):
        import fcntl
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from fastapi.testclient import TestClient
        from backend.http_api import create_app
        self.service()
        identity = str(uuid4())
        self.db.connection.execute("INSERT INTO backend_nodes(key,title,region,protocols_json,xray_transports_json) VALUES ('n1','Node','EU','[]','[]')")
        self.db.connection.execute("INSERT INTO backend_node_jobs VALUES (?,'n1',? ,?,'sync_runtime',1,?,'blocked',NULL)",
            (identity,self.admin.id,str(uuid4()),'{"node_key":"n1","revision":1}'))
        driver = SimpleNamespace(node_action=Mock(return_value={'node_key':'n1','action':'sync_runtime','revision':1,'result':{'synced':True}}))
        path = f'/api/v1/system/recovery/node/{identity}/recheck'
        headers={**self.headers,'X-Node-Plane-Telegram-User-ID':'101'}
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ',{'NODE_PLANE_SHARED_DIR':directory}), TestClient(create_app(self.db,node_driver=driver)) as client:
            (Path(directory)/'data').mkdir()
            self.assertEqual(client.post(path).status_code,401)
            with (Path(directory)/'data/backend-worker.lock').open('w') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
                busy=client.post(path,headers=headers)
                self.assertEqual(busy.json()['error']['code'],'maintenance_busy')
                driver.node_action.assert_not_called()
            response=client.post(path,headers=headers)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['status'],'succeeded')
            history=client.get('/api/v1/system/recovery/history',headers=headers).json()
            self.assertEqual(history['items'][0]['actor_id'],self.admin.id)
            self.assertEqual(history['items'][0]['operation_id'],identity)
            self.assertEqual(client.post(path,headers=headers).status_code,409)
            driver.node_action.assert_called_once()


class NodeRecoveryTests(TestCase):
    from tests.test_backend_node_jobs import BackendNodeJobTests as Fixture
    setUp = Fixture.setUp
    queue = Fixture.queue

    def test_targeted_recheck_reads_exact_journal_and_audits_failure_without_secrets(self):
        from backend.recovery import act, history
        from backend.authorization import AccessDenied
        from types import SimpleNamespace
        from unittest.mock import Mock
        job = self.queue('sync_runtime')
        self.db.connection.execute("UPDATE backend_node_jobs SET status='blocked'")
        call = Mock(side_effect=TimeoutError('secret-token'))
        driver = SimpleNamespace(node_action=call)
        with self.assertRaises(AccessDenied) as error:
            act(self.db, self.actor, driver, 'node', job['id'], 'recheck')
        self.assertEqual(error.exception.code, 'recovery_unconfirmed')
        self.assertTrue(call.call_args.kwargs['recover'])
        self.assertEqual(call.call_args.args[:2], (job['id'], 'sync_runtime'))
        page = history(self.db, self.actor)
        self.assertEqual(page['items'][0]['actor_id'], self.actor.account.id)
        self.assertEqual(page['items'][0]['outcome'], 'unconfirmed')
        self.assertNotIn('secret-token', str(page))
        self.assertEqual(overview(self.db, self.actor)['items'][0]['status'], 'blocked')
        call.side_effect = None
        call.return_value = {'node_key':'n1','action':'sync_runtime','revision':1,'result': {'synced':True}}
        result = act(self.db, self.actor, driver, 'node', job['id'], 'recheck')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(overview(self.db, self.actor)['items'], [])

    def test_settings_recovery_queues_new_revision_only_after_remote_retirement(self):
        from backend.recovery import act
        from backend.node_settings import NodeSettingsService
        from types import SimpleNamespace
        from unittest.mock import Mock
        self.db.connection.execute("UPDATE backend_nodes SET applied_revision=0 WHERE key='n1'")
        task = NodeSettingsService(self.db).queue(self.actor, 'n1', revision=1, command_key=str(uuid4()))
        self.db.connection.execute("UPDATE backend_node_settings_tasks SET status='blocked'")
        item = overview(self.db, self.actor)['items'][0]
        self.assertEqual((item['kind'], item['actions']), ('settings', ['recheck','resolve']))
        driver = SimpleNamespace(resolve_node_settings=Mock(return_value={'config_matches':False,'containers_running':True}))
        result = act(self.db, self.actor, driver, 'settings', task['id'], 'resolve')
        self.assertEqual(result['status'], 'superseded')
        self.assertNotEqual(result['replacement_id'], task['id'])
        fresh = self.db.connection.execute('SELECT * FROM backend_node_settings_tasks WHERE id=?', (result['replacement_id'],)).fetchone()
        self.assertEqual((fresh['status'], fresh['revision']), ('awaiting_executor',2))
        driver.resolve_node_settings.assert_called_once()

    def test_agent_recheck_requires_bound_identity_and_current_build(self):
        from backend.recovery import act
        from backend.agent_rollout import AgentRolloutService
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        task = AgentRolloutService(self.db).request(self.actor, 'n1', str(uuid4()), transport='local')
        self.db.connection.execute("UPDATE backend_agent_rollouts SET status='blocked'")
        driver = SimpleNamespace(inspect_node=Mock(return_value={'node_key':'n1'}),
            inspect_node_services=Mock(return_value={'agent_commit':'b'*40}))
        with patch('config.APP_COMMIT', 'a'*40):
            result = act(self.db,self.actor,driver,'agent',task['id'],'recheck')
            self.assertEqual(result['status'], 'blocked')
            driver.inspect_node_services.return_value = {'agent_commit':'a'*40}
            result = act(self.db,self.actor,driver,'agent',task['id'],'recheck')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['observation'], {'agent_reachable':True,'agent_current':True})

    def test_unknown_actions_and_maintenance_do_not_call_driver(self):
        from backend.recovery import act
        from backend.authorization import AccessDenied
        from unittest.mock import Mock
        job = self.queue('sync_runtime')
        self.db.connection.execute("UPDATE backend_node_jobs SET status='blocked'")
        driver = Mock()
        with self.assertRaises(AccessDenied):
            act(self.db,self.actor,driver,'node',job['id'],'unlock')
        self.db.connection.execute('INSERT INTO backend_controller_update_gate VALUES (1,?)', (str(uuid4()),))
        with self.assertRaises(AccessDenied):
            act(self.db,self.actor,driver,'node',job['id'],'recheck')
        self.assertEqual(driver.mock_calls, [])


class ProfileRecoveryTests(TestCase):
    from tests.test_backend_executor import BackendExecutorTests as Fixture
    setUp = Fixture.setUp
    headers_for = Fixture.headers_for
    create = Fixture.create
    prepare = Fixture.prepare
    grants = Fixture.grants

    def test_profile_recovery_preserves_old_identity_and_queues_current_access(self):
        from backend.recovery import act, history
        from backend.authorization import Actor, Principal, PrincipalKind, ADMIN_PERMISSIONS
        from backend.executor import IntentExecutor
        from tests.test_backend_executor import FakeDriver
        from unittest.mock import Mock
        profile = self.prepare()
        first = self.grants(profile,1,[{'node_key':'node','protocol':'awg'}]).json()
        driver = FakeDriver(failure=True)
        IntentExecutor(self.db,driver).run_one()
        task = self.db.connection.execute('SELECT id FROM backend_operation_tasks WHERE operation_id=?', (first['operation_id'],)).fetchone()['id']
        actor = Actor(Principal('repair',PrincipalKind.ACCOUNT,ADMIN_PERMISSIONS,self.admin.id),self.admin)
        item = overview(self.db,actor)['items'][0]
        self.assertEqual((item['subject_id'],item['actions']), (profile,['recheck','resolve']))
        driver.resolve = Mock(return_value={'disk_present':False,'live_present':False,'identity_matches':False,'config_available':False})
        result = act(self.db,actor,driver,'profile',task,'resolve')
        self.assertEqual(result['status'],'superseded')
        self.assertIsNotNone(result['replacement_id'])
        driver.resolve.assert_called_once()
        self.assertEqual(len(driver.calls),1)  # recovery did not replay the original mutation
        self.assertEqual(history(self.db,actor)['items'][0]['operation_id'],task)
