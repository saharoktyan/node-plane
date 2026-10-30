import json
import threading
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from uuid import uuid4

from backend.authorization import AccessDenied
from backend.recovery import overview, act
from backend.executor import Worker, IntentExecutor
from backend.node_settings import NodeSettingsExecutor
from backend.node_operations import NodeOperations
from backend.updates import UpdateService
from backend.profiles import ProfileRepository
from backend.agent_rollout import AgentRolloutService
from backend.config_issuance import ConfigIssuanceService

from tests import test_backend_updates as fixture_updates
from tests import test_backend_executor as fixture_executor


class ForceUpdateAdmissionTests(TestCase):
    setUp = fixture_updates.BackendUpdateTests.setUp
    service = fixture_updates.BackendUpdateTests.service
    node = fixture_updates.BackendUpdateTests.node

    def _create_profile(self):
        repo = ProfileRepository(self.db)
        return repo.create_profile(runtime_name=f'user-{uuid4().hex[:8]}', display_name='Test User', owner_account_id=self.admin.id)

    def test_core_only_version_update_admission_with_awaiting_work_behind_blocked_node(self):
        service = self.service()
        self.node()  # creates node lv1
        profile_id = self._create_profile()

        # Node has a blocked job
        blocked_job_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_node_jobs VALUES (?,'lv1',?,?,'sync_runtime',1,'{}','blocked',NULL)",
            (blocked_job_id, self.admin.id, str(uuid4()))
        )

        # Queued settings task behind the blocked node
        settings_task_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_node_settings_tasks VALUES (?,'lv1',?,?,2,'{}','awaiting_executor',NULL)",
            (settings_task_id, self.admin.id, str(uuid4()))
        )

        # Queued profile task behind the blocked node
        op_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_operations VALUES (?,?,?,'1','awaiting_executor','now')",
            (op_id, profile_id, self.admin.id)
        )
        profile_task_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_operation_tasks VALUES (?,?,'lv1','awg','dev-1','ensure','awaiting_executor','{}',NULL,NULL,NULL,NULL)",
            (profile_task_id, op_id)
        )

        # 1. Version update admission succeeds despite awaiting settings & profile tasks
        version_job = service.queue(self.actor, str(uuid4()), 'version', target_ref='v0.4.3-alpha.19', branch='dev')
        self.assertEqual(version_job['status'], 'awaiting_executor')
        self.assertEqual(version_job['kind'], 'version')

        # Controller gate was set for this version update
        gate = self.db.connection.execute("SELECT job_id FROM backend_controller_update_gate WHERE id=1").fetchone()
        self.assertEqual(gate['job_id'], version_job['id'])

        # 2. Clear update to test stack update admission with the exact same awaiting tasks
        self.db.connection.execute("DELETE FROM backend_controller_update_gate")
        self.db.connection.execute("DELETE FROM backend_update_jobs WHERE id=?", (version_job['id'],))

        # Stack update admission is denied (awaiting tasks must not exist for stack update)
        with self.assertRaises(AccessDenied) as ctx:
            service.queue(self.actor, str(uuid4()), 'stack', target_ref='v0.4.3-alpha.19', branch='dev')
        self.assertEqual(ctx.exception.code, 'maintenance_busy')
        self.assertEqual(ctx.exception.status, 409)

    def test_running_work_denies_core_only_version_update_admission(self):
        service = self.service()
        self.node()
        profile_id = self._create_profile()

        for table in (
            'backend_node_jobs',
            'backend_node_settings_tasks',
            'backend_operation_tasks',
            'backend_agent_rollouts',
        ):
            with self.subTest(table=table):
                task_id = str(uuid4())
                if table == 'backend_operation_tasks':
                    op_id = str(uuid4())
                    self.db.connection.execute("INSERT INTO backend_operations VALUES (?,?,?,'1','running','now')", (op_id, profile_id, self.admin.id))
                    self.db.connection.execute(
                        "INSERT INTO backend_operation_tasks VALUES (?,?,'lv1','awg','dev-1','ensure','running','{}',NULL,NULL,NULL,NULL)",
                        (task_id, op_id)
                    )
                elif table == 'backend_node_settings_tasks':
                    self.db.connection.execute(
                        "INSERT INTO backend_node_settings_tasks VALUES (?,'lv1',?,?,2,'{}','running',NULL)",
                        (task_id, self.admin.id, str(uuid4()))
                    )
                elif table == 'backend_agent_rollouts':
                    self.db.connection.execute(
                        "INSERT INTO backend_agent_rollouts VALUES (?,'lv1',?,?,'{}','running')",
                        (task_id, self.admin.id, str(uuid4()))
                    )
                else:
                    self.db.connection.execute(
                        "INSERT INTO backend_node_jobs VALUES (?,'lv1',?,?,'sync_runtime',1,'{}','running',NULL)",
                        (task_id, self.admin.id, str(uuid4()))
                    )

                with self.assertRaises(AccessDenied) as ctx:
                    service.queue(self.actor, str(uuid4()), 'version', target_ref='v0.4.3-alpha.19', branch='dev')
                self.assertEqual(ctx.exception.code, 'maintenance_busy')

                self.db.connection.execute(f"DELETE FROM {table} WHERE id=?", (task_id,))

        # Active temporary config issuing or revoking also denies version update
        temp_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_temporary_configs(id, actor_id, principal_id, command_key, node_key, node_revision, protocol, transport, duration_seconds, runtime_name, intent_json, status, created_at, expires_at) "
            "VALUES (?, ?, 'p1', ?, 'lv1', 1, 'awg', 'vpn', 43200, 'tmp', '{}', 'issuing', 'now', 'never')",
            (temp_id, self.admin.id, str(uuid4()))
        )
        with self.assertRaises(AccessDenied) as ctx:
            service.queue(self.actor, str(uuid4()), 'version', target_ref='v0.4.3-alpha.19', branch='dev')
        self.assertEqual(ctx.exception.code, 'maintenance_busy')

    def test_worker_drain_cannot_dispatch_queued_jobs_while_version_update_pending_and_preserves_queues(self):
        self.service()
        self.node()
        profile_id = self._create_profile()

        # Set up a pending core version update with gate active
        version_job_id = str(uuid4())
        plan = json.dumps({'phase': 'core', 'unit_name': 'test-update', 'launch_state': 'not_started'})
        self.db.connection.execute(
            "INSERT INTO backend_update_jobs(id, actor_id, command_key, kind, intent_json, status, result_json, created_at) "
            "VALUES (?, ?, ?, 'version', '{}', 'running', ?, 'now')",
            (version_job_id, self.admin.id, str(uuid4()), plan)
        )
        self.db.connection.execute("INSERT INTO backend_controller_update_gate VALUES (1, ?)", (version_job_id,))

        # Queued node job
        node_job_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_node_jobs VALUES (?, 'lv1', ?, ?, 'sync_runtime', 1, '{}', 'awaiting_executor', NULL)",
            (node_job_id, self.admin.id, str(uuid4()))
        )

        # Queued profile task
        op_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_operations VALUES (?, ?, ?, 1, 'awaiting_executor', 'now')",
            (op_id, profile_id, self.admin.id)
        )
        profile_task_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_operation_tasks VALUES (?, ?, 'lv1', 'awg', 'dev-1', 'ensure', 'awaiting_executor', '{}', NULL, NULL, NULL, NULL)",
            (profile_task_id, op_id)
        )

        # Queued settings task
        settings_task_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_node_settings_tasks VALUES (?, 'lv1', ?, ?, 2, '{}', 'awaiting_executor', NULL)",
            (settings_task_id, self.admin.id, str(uuid4()))
        )

        # Build Worker instance with mock driver
        channel = Mock()
        driver = Mock()
        driver.execute.return_value = {'succeeded': True}
        driver.node_action.return_value = {
            'node_key': 'lv1',
            'action': 'sync_runtime',
            'revision': 1,
            'result': {'synced': True}
        }
        driver.apply_node_settings.return_value = '{}'

        worker = Worker(self.db, channel)
        worker.executor.driver = driver
        worker.node_executor.driver = driver
        worker.node_jobs.driver = driver

        # 1. IntentExecutor.run_one() directly returns False when core_upgrade is active
        self.assertFalse(worker.executor.run_one())

        # 2. Worker.drain() runs only update_executor while gate is active
        stop_event = threading.Event()
        # Mock update_executor.run_one() to return False so drain doesn't loop forever
        worker.update_executor.run_one = Mock(return_value=False)

        worker.drain(stop_event)

        # Queued jobs must still be awaiting_executor! (Not dispatched)
        nj = self.db.connection.execute("SELECT status FROM backend_node_jobs WHERE id=?", (node_job_id,)).fetchone()
        self.assertEqual(nj['status'], 'awaiting_executor')

        pt = self.db.connection.execute("SELECT status FROM backend_operation_tasks WHERE id=?", (profile_task_id,)).fetchone()
        self.assertEqual(pt['status'], 'awaiting_executor')

        st = self.db.connection.execute("SELECT status FROM backend_node_settings_tasks WHERE id=?", (settings_task_id,)).fetchone()
        self.assertEqual(st['status'], 'awaiting_executor')

        driver.execute.assert_not_called()
        driver.node_action.assert_not_called()

        # 3. Remove gate: Queues were preserved intact and can now be drained
        self.db.connection.execute("DELETE FROM backend_controller_update_gate WHERE id=1")
        self.db.connection.execute("UPDATE backend_update_jobs SET status='succeeded' WHERE id=?", (version_job_id,))

        # Restore update_executor.run_one mock
        worker.update_executor.run_one = Mock(return_value=False)

        # Next drain cycle will dispatch ordinary services
        worker.drain(stop_event)

        # Verify that node jobs / settings were touched / claimed
        nj_after = self.db.connection.execute("SELECT status FROM backend_node_jobs WHERE id=?", (node_job_id,)).fetchone()
        self.assertIn(nj_after['status'], {'running', 'succeeded'})

    def test_settings_resolve_available_with_queued_profiles_but_not_running_or_blocked(self):
        self.service()
        self.node()
        settings = {'awg_port': 51820, 'public_host': 'node.example'}
        self.db.connection.execute(
            "UPDATE backend_nodes SET settings_json=?, protocols_json=?, desired_revision=2, applied_revision=1 WHERE key='lv1'",
            (json.dumps(settings), json.dumps(['awg']))
        )
        profile_id = self._create_profile()

        settings_task_id = str(uuid4())
        intent = {'protocols': ['awg'], 'settings': settings, 'node_key': 'lv1', 'revision': 2}
        self.db.connection.execute(
            "INSERT INTO backend_node_settings_tasks VALUES (?, 'lv1', ?, ?, 2, ?, 'blocked', NULL)",
            (settings_task_id, self.admin.id, str(uuid4()), json.dumps(intent))
        )

        op_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_operations VALUES (?, ?, ?, 1, 'awaiting_executor', 'now')",
            (op_id, profile_id, self.admin.id)
        )
        profile_task_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_operation_tasks VALUES (?, ?, 'lv1', 'awg', 'dev-1', 'ensure', 'awaiting_executor', '{}', NULL, NULL, NULL, NULL)",
            (profile_task_id, op_id)
        )

        # 1. Profile is merely awaiting_executor: resolve IS available in settings actions
        items = overview(self.db, self.actor)['items']
        settings_item = next(i for i in items if i['id'] == settings_task_id)
        self.assertIn('resolve', settings_item['actions'])
        self.assertIn('force', settings_item['actions'])

        # Resolving succeeds
        driver = SimpleNamespace(
            resolve_node_settings=Mock(return_value={'config_matches': False, 'containers_running': True})
        )
        result = act(self.db, self.actor, driver, 'settings', settings_task_id, 'resolve')
        self.assertEqual(result['status'], 'superseded')
        self.assertIsNotNone(result['replacement_id'])

        # 2. Profile task is blocked: resolve is NOT available
        new_settings_id = result['replacement_id']
        self.db.connection.execute("UPDATE backend_node_settings_tasks SET status='blocked' WHERE id=?", (new_settings_id,))
        self.db.connection.execute("UPDATE backend_operation_tasks SET status='blocked' WHERE id=?", (profile_task_id,))

        items = overview(self.db, self.actor)['items']
        settings_item = next(i for i in items if i['id'] == new_settings_id)
        self.assertNotIn('resolve', settings_item['actions'])
        self.assertIn('force', settings_item['actions'])

        # Attempting resolve raises AccessDenied permission_denied
        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver, 'settings', new_settings_id, 'resolve')
        self.assertEqual(ctx.exception.code, 'permission_denied')

        # 3. Profile task is running: resolve is NOT available
        self.db.connection.execute("UPDATE backend_node_settings_tasks SET status='blocked' WHERE id=?", (new_settings_id,))
        self.db.connection.execute("UPDATE backend_operation_tasks SET status='running' WHERE id=?", (profile_task_id,))

        items = overview(self.db, self.actor)['items']
        settings_item = next(i for i in items if i['id'] == new_settings_id)
        self.assertNotIn('resolve', settings_item['actions'])
        self.assertIn('force', settings_item['actions'])

        with self.assertRaises(AccessDenied) as ctx:
            act(self.db, self.actor, driver, 'settings', new_settings_id, 'resolve')
        self.assertEqual(ctx.exception.code, 'permission_denied')

    def test_direct_run_one_denial_for_newly_guarded_claims_under_core_gate(self):
        self.service()
        self.node()
        profile_id = self._create_profile()

        # Set up a pending core version update with gate active
        version_job_id = str(uuid4())
        plan = json.dumps({'phase': 'core', 'unit_name': 'test-update', 'launch_state': 'not_started'})
        self.db.connection.execute(
            "INSERT INTO backend_update_jobs(id, actor_id, command_key, kind, intent_json, status, result_json, created_at) "
            "VALUES (?, ?, ?, 'version', '{}', 'running', ?, 'now')",
            (version_job_id, self.admin.id, str(uuid4()), plan)
        )
        self.db.connection.execute("INSERT INTO backend_controller_update_gate VALUES (1, ?)", (version_job_id,))

        # 1. Seed awaiting_executor tasks for the three newly guarded claim paths
        # backend_node_jobs (NodeOperations)
        node_job_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_node_jobs VALUES (?, 'lv1', ?, ?, 'sync_runtime', 1, '{}', 'awaiting_executor', NULL)",
            (node_job_id, self.admin.id, str(uuid4()))
        )

        # backend_agent_rollouts (AgentRolloutService)
        agent_rollout_id = str(uuid4())
        intent = {'transport': 'local', 'node_key': 'lv1', 'command_key': str(uuid4())}
        self.db.connection.execute(
            "INSERT INTO backend_agent_rollouts VALUES (?, 'lv1', ?, ?, ?, 'awaiting_executor')",
            (agent_rollout_id, self.admin.id, str(uuid4()), json.dumps(intent))
        )

        # backend_config_issuances (ConfigIssuanceService)
        issuance_id = str(uuid4())
        self.db.connection.execute(
            "INSERT INTO backend_config_issuances VALUES (?, ?, ?, ?, 'lv1', 'awg', 'vpn', 'dev-1', 1, 1, 1, 'awaiting_executor', NULL, 'now', '2099-01-01T00:00:00')",
            (issuance_id, self.admin.id, str(uuid4()), profile_id)
        )

        # Set up real services with tracked driver/runner
        driver = Mock()
        runner = Mock(return_value=True)

        node_ops = NodeOperations(self.db, driver)
        agent_service = AgentRolloutService(self.db, runner)
        issuance_service = ConfigIssuanceService(self.db, driver)

        guard_before = self.db.connection.execute("SELECT revision FROM backend_account_guard WHERE id=1").fetchone()['revision']

        # 2. Direct run_one calls must all return False under core gate
        self.assertFalse(node_ops.run_one())
        self.assertFalse(agent_service.run_one())
        self.assertFalse(issuance_service.run_one())

        # Each run_one transaction updated account guard for serialization
        guard_after = self.db.connection.execute("SELECT revision FROM backend_account_guard WHERE id=1").fetchone()['revision']
        self.assertEqual(guard_after, guard_before + 3)

        # 3. Queues must be preserved in awaiting_executor state (unclaimed)
        nj = self.db.connection.execute("SELECT status FROM backend_node_jobs WHERE id=?", (node_job_id,)).fetchone()
        self.assertEqual(nj['status'], 'awaiting_executor')

        ar = self.db.connection.execute("SELECT status FROM backend_agent_rollouts WHERE id=?", (agent_rollout_id,)).fetchone()
        self.assertEqual(ar['status'], 'awaiting_executor')

        ci = self.db.connection.execute("SELECT status FROM backend_config_issuances WHERE id=?", (issuance_id,)).fetchone()
        self.assertEqual(ci['status'], 'awaiting_executor')

        # 4. Zero remote driver / runner calls made
        self.assertEqual(driver.mock_calls, [])
        self.assertEqual(runner.mock_calls, [])

        # 5. Lifting the core gate releases dispatch
        self.db.connection.execute("DELETE FROM backend_controller_update_gate WHERE id=1")
        self.db.connection.execute("UPDATE backend_update_jobs SET status='succeeded' WHERE id=?", (version_job_id,))

        driver.node_action.return_value = {
            'node_key': 'lv1',
            'action': 'sync_runtime',
            'revision': 1,
            'result': {'synced': True}
        }
        self.assertTrue(node_ops.run_one())
        nj_after = self.db.connection.execute("SELECT status FROM backend_node_jobs WHERE id=?", (node_job_id,)).fetchone()
        self.assertEqual(nj_after['status'], 'succeeded')
        driver.node_action.assert_called_once()

        self.assertTrue(agent_service.run_one())
        ar_after = self.db.connection.execute("SELECT status FROM backend_agent_rollouts WHERE id=?", (agent_rollout_id,)).fetchone()
        self.assertEqual(ar_after['status'], 'succeeded')
        runner.assert_called_once()
