from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.http_api import create_app
from backend.node_settings import NodeSettingsExecutor, _digest
import tests.test_backend_http as fixture_http


class NodeServiceBoundariesTests(unittest.TestCase):
    setUp = fixture_http.BackendHTTPTests.setUp
    register = fixture_http.BackendHTTPTests.register
    provision = fixture_http.BackendHTTPTests.provision

    def admin_headers(self, **extra):
        return {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101', **extra}

    def create_node(self, node_key='n1', protocols=None, **extra):
        body = {
            'key': node_key,
            'title': 'Node ' + node_key,
            'region': 'EU',
            'protocols': protocols if protocols is not None else [],
            'xray_transports': [],
            'settings': {'public_host': 'node.example'},
            **extra,
        }
        return self.client.post(
            '/api/v1/nodes',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4())}),
            json=body,
        )

    def test_base_node_creation_and_metadata_edits_do_not_trigger_runtime_settings_apply(self):
        created = self.create_node('n1', protocols=[])
        self.assertEqual(created.status_code, 201, created.text)
        node = created.json()
        self.assertFalse(node['enabled'])
        self.assertFalse(node['agent_installed'])
        self.assertFalse(node['settings_apply_pending'])
        self.assertEqual(node['desired_revision'], 1)
        self.assertEqual(node['applied_revision'], 0)
        self.assertEqual(node['protocols'], [])

        # Editing base metadata increments desired_revision, but must not mark settings apply pending
        edited = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'title': 'Updated Title', 'flag': 'FR', 'notes': 'Base node notes'},
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        updated = edited.json()
        self.assertEqual(updated['desired_revision'], 2)
        self.assertEqual(updated['applied_revision'], 0)
        self.assertFalse(updated['settings_apply_pending'])
        self.assertFalse(updated['agent_installed'])

        # Applying settings on an uninstalled node must be rejected before creating any task
        apply_response = self.client.post(
            '/api/v1/nodes/n1/apply-settings',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"2"'}),
        )
        self.assertEqual(apply_response.status_code, 409, apply_response.text)
        self.assertEqual(apply_response.json()['error']['code'], 'node_installation_required')
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM backend_node_settings_tasks WHERE node_key='n1'").fetchone())

    def test_metadata_and_ssh_edits_on_a_vpn_node_do_not_deploy_services(self):
        created = self.create_node('n1', protocols=['awg'], transport='ssh', ssh_target='root@old.example')
        self.assertEqual(created.status_code, 201, created.text)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_nodes SET applied_revision=1,enabled=1 WHERE key='n1'")
        response = self.client.patch('/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'title': 'Renamed', 'notes': 'Host metadata', 'ssh_target': 'root@new.example'})
        self.assertEqual(response.status_code, 200, response.text)
        node = response.json()
        self.assertEqual((node['applied_revision'], node['desired_revision']), (2, 2))
        self.assertFalse(node['settings_apply_pending'])
        self.assertEqual(node['protocols'], ['awg'])
        self.assertTrue(node['agent_installed'])
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM backend_node_settings_tasks WHERE node_key='n1'").fetchone())
            self.assertIsNone(conn.execute("SELECT 1 FROM backend_node_jobs WHERE node_key='n1'").fetchone())

    def test_agent_installation_boundary_separates_agent_from_vpn_runtime(self):
        self.assertEqual(self.create_node('n1', protocols=[]).status_code, 201)

        # Record agent rollout receipt without any VPN protocol configuration
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO backend_agent_rollouts (id, node_key, actor_account_id, command_key, intent_json, status) VALUES (?, 'n1', ?, ?, '{}', 'succeeded')",
                (str(uuid4()), self.admin.id, str(uuid4())),
            )

        node = self.client.get('/api/v1/nodes/n1', headers=self.admin_headers()).json()
        self.assertTrue(node['agent_installed'])
        self.assertEqual(node['applied_revision'], 0)
        self.assertFalse(node['settings_apply_pending'])

        # Installed agent with no protocols or pending removals rejects settings apply
        apply_response = self.client.post(
            '/api/v1/nodes/n1/apply-settings',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
        )
        self.assertEqual(apply_response.status_code, 409, apply_response.text)
        self.assertEqual(apply_response.json()['error']['code'], 'settings_already_applied')

    def test_bootstrap_coordinator_enforces_vpn_protocol_boundary(self):
        self.assertEqual(self.create_node('n1', protocols=[]).status_code, 201)

        # Attempting bootstrap on an agent-only node via API rejects with node_settings_incomplete
        response = self.client.post(
            '/api/v1/nodes/n1/bootstrap',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4())}),
            json={'revision': 1, 'action': 'bootstrap'},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()['error']['code'], 'node_settings_incomplete')

    def test_connectivity_and_live_probe_boundary_preserves_installed_state(self):
        self.assertEqual(self.create_node('n1', protocols=[]).status_code, 201)

        # Uninstalled node rejects probe
        uninstalled_probe = self.client.get('/api/v1/nodes/n1/probe', headers=self.admin_headers())
        self.assertEqual(uninstalled_probe.status_code, 409)
        self.assertEqual(uninstalled_probe.json()['error']['code'], 'node_installation_required')

        # Record agent installation
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO backend_agent_rollouts (id, node_key, actor_account_id, command_key, intent_json, status) VALUES (?, 'n1', ?, ?, '{}', 'succeeded')",
                (str(uuid4()), self.admin.id, str(uuid4())),
            )

        # Ephemeral driver connection failure does not alter durable agent_installed state
        failing_driver = SimpleNamespace(
            inspect_node_services=Mock(side_effect=TimeoutError('connection timed out'))
        )
        with TestClient(create_app(self.db, node_driver=failing_driver)) as client:
            probe_fail = client.get('/api/v1/nodes/n1/probe', headers=self.admin_headers())
            self.assertEqual(probe_fail.status_code, 503)
            self.assertEqual(probe_fail.json()['error']['code'], 'node_agent_unavailable')

            # Durable state remains installed
            node_read = client.get('/api/v1/nodes/n1', headers=self.admin_headers()).json()
            self.assertTrue(node_read['agent_installed'])

        # Successful driver probe on agent-only node returns empty protocol list and no settings pending
        facts = {
            'agent_version': '1.2.3',
            'docker': False,
            'runtime_version': '',
            'desired_runtime_version': '',
            'runtime_drift': False,
            'host_metrics': {'ram_used_percent': 25.0, 'disk_free_percent': 80.0},
        }
        working_driver = SimpleNamespace(inspect_node_services=Mock(return_value=facts))
        with TestClient(create_app(self.db, node_driver=working_driver)) as client:
            probe_ok = client.get('/api/v1/nodes/n1/probe', headers=self.admin_headers())
            self.assertEqual(probe_ok.status_code, 200, probe_ok.text)
            probe_data = probe_ok.json()
            self.assertTrue(probe_data['agent_reachable'])
            self.assertFalse(probe_data['docker'])
            self.assertEqual(probe_data['protocols'], [])
            self.assertFalse(probe_data['settings_pending'])

    def test_adding_and_removing_vpn_protocols_maintains_agent_and_settings_pending_boundaries(self):
        self.assertEqual(self.create_node('n1', protocols=[]).status_code, 201)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO backend_agent_rollouts (id, node_key, actor_account_id, command_key, intent_json, status) VALUES (?, 'n1', ?, ?, '{}', 'succeeded')",
                (str(uuid4()), self.admin.id, str(uuid4())),
            )

        # 1. Add AWG protocol to agent-only node
        add_patch = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'protocols': ['awg']},
        )
        self.assertEqual(add_patch.status_code, 200, add_patch.text)
        self.assertTrue(add_patch.json()['settings_apply_pending'])
        self.assertTrue(add_patch.json()['agent_installed'])

        # Queue apply task
        apply_queue = self.client.post(
            '/api/v1/nodes/n1/apply-settings',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"2"'}),
        )
        self.assertEqual(apply_queue.status_code, 202, apply_queue.text)

        # Execute apply via driver
        driver = Mock()
        driver.inspect_node.return_value = {'health_state': 'running', 'xray_config_present': False, 'awg_config_present': True}
        driver.apply_node_settings.side_effect = lambda cmd, intent: json.dumps(
            {'node_key': 'n1', 'revision': 2, 'settings_sha256': _digest(intent)}
        )
        self.assertTrue(NodeSettingsExecutor(self.db, driver).run_one())

        # Verify applied state
        applied_node = self.client.get('/api/v1/nodes/n1', headers=self.admin_headers()).json()
        self.assertEqual(applied_node['applied_revision'], 2)
        self.assertFalse(applied_node['settings_apply_pending'])
        self.assertEqual(applied_node['protocols'], ['awg'])
        self.assertTrue(applied_node['agent_installed'])

        # 2. Remove all protocols
        remove_patch = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"2"'}),
            json={'protocols': [], 'xray_transports': []},
        )
        self.assertEqual(remove_patch.status_code, 200, remove_patch.text)
        self.assertTrue(remove_patch.json()['settings_apply_pending'])
        self.assertTrue(remove_patch.json()['agent_installed'])

        # Queue removal apply task
        remove_queue = self.client.post(
            '/api/v1/nodes/n1/apply-settings',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"3"'}),
        )
        self.assertEqual(remove_queue.status_code, 202, remove_queue.text)

        # Execute removal apply via driver
        driver.apply_node_settings.side_effect = lambda cmd, intent: json.dumps(
            {'node_key': 'n1', 'revision': 3, 'settings_sha256': _digest(intent)}
        )
        self.assertTrue(NodeSettingsExecutor(self.db, driver).run_one())

        # Verify final state: agent remains installed, settings_apply_pending is false, protocols are empty
        final_node = self.client.get('/api/v1/nodes/n1', headers=self.admin_headers()).json()
        self.assertEqual(final_node['applied_revision'], 3)
        self.assertFalse(final_node['settings_apply_pending'])
        self.assertEqual(final_node['protocols'], [])
        self.assertTrue(final_node['agent_installed'])

    def test_vpn_specific_node_operations_rejected_on_agent_only_node(self):
        self.assertEqual(self.create_node('n1', protocols=[]).status_code, 201)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO backend_agent_rollouts (id, node_key, actor_account_id, command_key, intent_json, status) VALUES (?, 'n1', ?, ?, '{}', 'succeeded')",
                (str(uuid4()), self.admin.id, str(uuid4())),
            )

        for action in ('sync_xray', 'regenerate_entropy', 'reinstall_clean', 'reinstall_keep',
                       'sync_runtime', 'sync_env', 'check_ports', 'open_ports', 'cleanup_runtime', 'reconcile_access'):
            response = self.client.post(
                '/api/v1/nodes/n1/actions',
                headers=self.admin_headers(**{'Idempotency-Key': str(uuid4())}),
                json={'action': action, 'revision': 1},
            )
            self.assertEqual(
                response.status_code,
                422,
                f"Action '{action}' was expected to be rejected with 422 on agent-only node, got {response.status_code}: {response.text}",
            )
            self.assertEqual(response.json()['error']['code'], 'config_not_supported')
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM backend_node_jobs WHERE node_key='n1'").fetchone())
