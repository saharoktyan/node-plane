"""Administration summaries and read-only, secret-free server probes."""
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient
from backend.http_api import create_app
from tests.test_backend_nodes import BackendNodeTests


class ObservationTests(TestCase):
    setUp = BackendNodeTests.setUp
    create = BackendNodeTests.create
    admin_headers = BackendNodeTests.admin_headers

    def test_overview_publishes_actual_alert_and_request_policy_switches(self):
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_system_settings VALUES ('alert_policy',?)", (json.dumps({'enabled':False}),))
            conn.execute("INSERT INTO backend_system_settings VALUES ('access_requests_enabled','0')")
        with TestClient(create_app(self.db)) as client:
            response = client.get('/api/v1/admin/overview', headers=self.admin_headers())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()['alerts_enabled'])
            self.assertFalse(response.json()['requests_enabled'])

    def test_probe_filters_secrets_and_invalid_metrics_without_changing_settings(self):
        self.assertEqual(self.create().status_code, 201)
        facts = {'docker': True, 'xray_installed': True, 'xray_running': True,
                 'xray_config_valid': True, 'awg_installed': False, 'awg_running': False,
                 'awg_config_valid': False, 'agent_version': '1.0', 'runtime_version': '0.9',
                 'runtime_drift': True, 'entropy': ['PRIVATE'], 'private_key': 'PRIVATE',
                 'host_metrics': {'ram_used_percent': 35, 'disk_free_percent': 70,
                     'load1': float('nan'), 'cpus': True, 'load5': -1, 'unknown': 'PRIVATE'}}
        driver = SimpleNamespace(inspect_node_services=Mock(return_value=facts))
        before = dict(self.db.connection.execute("SELECT * FROM backend_nodes WHERE key='lv1'").fetchone())
        with TestClient(create_app(self.db, node_driver=driver)) as client:
            self.assertEqual(client.get('/api/v1/nodes/lv1/probe').status_code, 401)
            driver.inspect_node_services.assert_not_called()
            response = client.get('/api/v1/nodes/lv1/probe', headers=self.admin_headers())
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data['host_metrics'], {'ram_used_percent': 35, 'disk_free_percent': 70})
        self.assertNotIn('PRIVATE', response.text)
        self.assertTrue(data['settings_pending'])
        self.assertTrue(data['agent_reachable'])
        self.assertTrue(data['checked_at'])
        self.assertGreaterEqual(data['response_ms'], 0)
        driver.inspect_node_services.assert_called_once_with('lv1')
        after = dict(self.db.connection.execute("SELECT * FROM backend_nodes WHERE key='lv1'").fetchone())
        self.assertEqual(before, after)

    def test_agent_only_probe_has_no_failing_protocols_and_keeps_unknowns(self):
        self.create()
        self.db.connection.execute("UPDATE backend_nodes SET protocols_json='[]' WHERE key='lv1'")
        self.db.connection.commit()
        driver = SimpleNamespace(inspect_node_services=Mock(return_value={'agent_version': '1.0'}))
        with TestClient(create_app(self.db, node_driver=driver)) as client:
            response = client.get('/api/v1/nodes/lv1/probe', headers=self.admin_headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['protocols'], [])
        self.assertIsNone(response.json()['docker'])
        self.assertEqual(response.json()['host_metrics'], {})

    def test_probe_failure_is_not_reported_as_a_healthy_observation(self):
        self.create()
        driver = SimpleNamespace(inspect_node_services=Mock(side_effect=TimeoutError('PRIVATE')))
        with TestClient(create_app(self.db, node_driver=driver)) as client:
            response = client.get('/api/v1/nodes/lv1/probe', headers=self.admin_headers())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'node_agent_unavailable')
        self.assertNotIn('PRIVATE', response.text)

    def test_fleet_diagnostics_keep_other_servers_visible_when_one_is_unreachable(self):
        self.create()
        self.db.connection.execute("UPDATE backend_nodes SET applied_revision=1,enabled=1 WHERE key='lv1'")
        self.db.connection.execute("INSERT INTO backend_nodes(key,title,region,protocols_json,enabled) VALUES ('disabled','Disabled','EU','[]',0)")
        self.db.connection.commit()
        driver = SimpleNamespace(inspect_node_services=Mock(side_effect=TimeoutError('PRIVATE')))
        with TestClient(create_app(self.db, node_driver=driver)) as client:
            self.assertEqual(client.get('/api/v1/system/recovery/fleet').status_code, 401)
            response = client.get('/api/v1/system/recovery/fleet', headers=self.admin_headers())
        self.assertEqual(response.status_code, 200, response.text)
        page = response.json()
        self.assertEqual(page['total'], 2)
        self.assertEqual(len(page['items']), 2)
        self.assertEqual(page['errors'], 1)
        self.assertEqual({call.args[0] for call in driver.inspect_node_services.call_args_list}, {'disabled','lv1'})
        self.assertNotIn('PRIVATE', response.text)

    def test_fleet_pages_do_not_probe_servers_outside_the_requested_page(self):
        self.create()
        for index in range(6):
            self.db.connection.execute("INSERT INTO backend_nodes(key,title,region,protocols_json,enabled) VALUES (?,?,'EU','[]',0)",
                (f'other{index}', f'Other {index}'))
        self.db.connection.execute("UPDATE backend_nodes SET enabled=1 WHERE key='other0'")
        self.db.connection.commit()
        driver = SimpleNamespace(inspect_node_services=Mock(return_value={}))
        with TestClient(create_app(self.db, node_driver=driver)) as client:
            response = client.get('/api/v1/system/recovery/fleet?offset=5', headers=self.admin_headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['total'], 7)
        self.assertEqual(len(response.json()['items']), 2)
        self.assertIsNone(response.json()['next_offset'])
        self.assertEqual({call.args[0] for call in driver.inspect_node_services.call_args_list}, {'other5','lv1'})

    def test_invalid_server_metadata_does_not_hide_other_diagnostic_results(self):
        self.create()
        self.db.connection.execute("UPDATE backend_nodes SET protocols_json='invalid-json' WHERE key='lv1'")
        self.db.connection.execute("INSERT INTO backend_nodes(key,title,region,protocols_json,enabled) VALUES ('other','Other','EU','[]',0)")
        self.db.connection.commit()
        driver = SimpleNamespace(inspect_node_services=Mock())
        with TestClient(create_app(self.db, node_driver=driver)) as client:
            response = client.get('/api/v1/system/recovery/fleet', headers=self.admin_headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()['items']), 2)
        self.assertEqual(response.json()['errors'], 1)
        self.assertIn('registry', [c['id'] for item in response.json()['items'] for c in item['checks']])
        driver.inspect_node_services.assert_called_once_with('other')

    def test_overview_exposes_maintenance_and_blocked_agent_installation_without_probing(self):
        self.create()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_system_settings VALUES ('backup_policy',?)", (json.dumps({'enabled': True}),))
            conn.execute("INSERT INTO schema_meta VALUES ('updates_update_available','1')")
            conn.execute("INSERT INTO schema_meta VALUES ('updates_remote_version','9.0.0')")
            conn.execute("INSERT INTO backend_agent_rollouts (id,node_key,actor_account_id,command_key,intent_json,status) VALUES (?,'lv1',?,?,'{}','blocked')",
                         (str(uuid4()), self.admin.id, str(uuid4())))
        driver = SimpleNamespace(inspect_node_services=Mock(side_effect=AssertionError('No implicit probes')))
        with TestClient(create_app(self.db, node_driver=driver)) as client:
            response = client.get('/api/v1/admin/overview', headers=self.admin_headers())
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data['tasks']['blocked'], 1)
        self.assertTrue(data['backup']['enabled'])
        self.assertTrue(data['updates']['available'])
        self.assertEqual(data['updates']['latest_version'], '9.0.0')
        self.assertEqual([node['key'] for node in data['problem_nodes']], ['lv1'])
        self.assertIn('unacknowledged_alerts', data)
        self.assertIn('alerts_enabled', data)
        self.assertTrue(data['requests_enabled'])
        driver.inspect_node_services.assert_not_called()
