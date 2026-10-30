from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
import unittest
from uuid import uuid4

from backend.authorization import (
    AccessDenied, Account, Actor, ADMIN_PERMISSIONS, Principal, PrincipalKind
)
from backend.executor import IntentExecutor
from backend.grant_policies import raw_read, store, materialize
from backend.node_settings import NodeSettingsExecutor, _digest
from backend.operations import OperationRepository
from backend.profiles import ProfileRepository
from backend.temporary_configs import TemporaryConfigService
from tests import test_backend_http


class FakeUnifiedDriver:
    def __init__(self, failure=False):
        self.calls = []
        self.failure = failure
        self.receipts = {}
        self.states = {}

    def execute(self, task_id, intent):
        self.calls.append(('execute', task_id, intent))
        if self.failure:
            raise TimeoutError('rpc timeout')
        return {
            'driver_operation_id': str(task_id),
            'succeeded': True,
            'result_json': json.dumps({'status': 'ok'})
        }

    def inspect_node(self, node_key):
        return {
            'health_state': 'running',
            'xray_config_present': True,
            'awg_config_present': True,
        }

    def apply_node_settings(self, command_id, intent):
        self.calls.append(('apply_node_settings', command_id, intent))
        return json.dumps({
            'node_key': intent['node_key'],
            'revision': intent['revision'],
            'settings_sha256': _digest(intent)
        })

    def node_action(self, identity, action, intent, recover=False):
        self.calls.append(('node_action', identity, action, intent))
        if action == 'temporary_status':
            return self.states.get(
                identity,
                {
                    'status': 'active',
                    'enforcement_ready': True,
                    'expires_at': self.receipts.get(identity, {}).get(
                        'expires_at',
                        (datetime.now(timezone.utc) + timedelta(seconds=43200)).isoformat()
                    )
                }
            )
        if action == 'temporary_revoke':
            return {'revocation_mode': 'peer_removed'}
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=intent.get('lease_seconds', 43200))).isoformat()
        receipt = {
            'expires_at': expiry,
            'revocation_mode': 'peer_removed' if intent.get('protocol') != 'xray' else 'new_connections_only',
            'xray_uuid': intent.get('uuid', ''),
            'wg_conf': '[Interface]\nPrivateKey = secret\n',
            'vpn_key': 'vpn://secret'
        }
        self.receipts[identity] = receipt
        self.states[identity] = {'status': 'active', 'expires_at': expiry, 'enforcement_ready': True}
        return receipt

    def refresh_awg_config(self, node_key, wg_conf, profile_name):
        return {
            'wg_conf': '[Interface]\nPrivateKey = secret\n\n[Peer]\nEndpoint = node.example:51820\n',
            'vpn_key': 'vpn://secret'
        }

    def read_xray_public(self, node_key):
        return {'public_key': 'fake_key'}


class ProtocolRemovalRevocationTests(unittest.TestCase):
    setUp = test_backend_http.BackendHTTPTests.setUp
    provision = test_backend_http.BackendHTTPTests.provision

    def admin_headers(self, **extra):
        return {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101', **extra}

    def create_node(self, key='n1', protocols=None, settings=None, transport=None, ssh_target=None):
        protocols = ['awg', 'xray'] if protocols is None else protocols
        default_settings = {
            'public_host': f'{key}.example.test',
            'xray_sni': 'www.cloudflare.com',
            'xray_tcp_port': 443,
            'xray_xhttp_port': 8443,
            'xray_xhttp_path': '/assets',
            'awg_port': 51820,
        }
        if settings:
            default_settings.update(settings)
        body = {
            'key': key,
            'title': f'Node {key}',
            'region': 'EU',
            'protocols': protocols,
            'xray_transports': ['tcp', 'xhttp'] if 'xray' in protocols else [],
            'settings': default_settings,
        }
        if transport:
            body['transport'] = transport
            body['ssh_target'] = ssh_target
        return self.client.post(
            '/api/v1/nodes',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4())}),
            json=body
        )

    def apply_settings(self, node_key='n1', revision=1):
        task = self.client.post(
            f'/api/v1/nodes/{node_key}/apply-settings',
            headers=self.admin_headers(
                **{'Idempotency-Key': str(uuid4()), 'If-Match': f'"{revision}"'}
            )
        ).json()

        class Driver:
            def inspect_node(self, nk):
                return {'health_state': 'running', 'xray_config_present': True, 'awg_config_present': True}

            def apply_node_settings(self, command_id, intent):
                return json.dumps({
                    'node_key': intent['node_key'],
                    'revision': intent['revision'],
                    'settings_sha256': _digest(intent)
                })

        worker = NodeSettingsExecutor(self.db, Driver())
        worker.run_one()
        return task

    def test_admin_inherited_grant_no_longer_vetoes_removal(self):
        from backend.profiles import ProfileRepository
        ProfileRepository(self.db).ensure_account_profile(self.admin.id)
        created = self.create_node('n1')
        self.assertEqual(created.status_code, 201)
        self.apply_settings('n1', 1)

        with self.db.transaction() as conn:
            admin_profile = conn.execute(
                "SELECT id FROM backend_profiles WHERE owner_account_id=?", (self.admin.id,)
            ).fetchone()['id']
            materialize(conn, admin_profile)
            grants = conn.execute(
                "SELECT node_key, protocol FROM backend_grants WHERE profile_id=?", (admin_profile,)
            ).fetchall()
            self.assertIn(('n1', 'awg'), [(g['node_key'], g['protocol']) for g in grants])
            self.assertIn(('n1', 'xray'), [(g['node_key'], g['protocol']) for g in grants])

        # Remove xray from n1
        response = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'protocols': ['awg']}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['protocols'], ['awg'])

        # Admin grant for xray on n1 must be revoked; awg grant remains
        with self.db.connect() as conn:
            grants = conn.execute(
                "SELECT node_key, protocol FROM backend_grants WHERE profile_id=?", (admin_profile,)
            ).fetchall()
            grant_pairs = [(g['node_key'], g['protocol']) for g in grants]
            self.assertNotIn(('n1', 'xray'), grant_pairs)
            self.assertIn(('n1', 'awg'), grant_pairs)

            # Delete task recorded in backend_operation_tasks
            task = conn.execute(
                """SELECT t.action, t.status, t.protocol FROM backend_operation_tasks t
                   JOIN backend_operations o ON o.id=t.operation_id
                   WHERE o.profile_id=? AND t.node_key='n1' AND t.protocol='xray'
                   ORDER BY o.desired_revision DESC LIMIT 1""",
                (admin_profile,)
            ).fetchone()
            self.assertIsNotNone(task)
            self.assertEqual(task['action'], 'delete')
            self.assertEqual(task['status'], 'awaiting_executor')

    def test_member_explicit_and_derived_policies_pruned_and_not_reintroduced(self):
        self.assertEqual(self.create_node('n1').status_code, 201)
        self.apply_settings('n1', 1)

        provisioned = self.provision(102).json()['account']
        with self.db.transaction() as conn:
            member_profile = conn.execute(
                "SELECT id FROM backend_profiles WHERE owner_account_id=?", (provisioned['id'],)
            ).fetchone()['id']
            region_id = conn.execute(
                "SELECT region_id FROM backend_node_regions WHERE node_key='n1'"
            ).fetchone()['region_id']
            # Store explicit grants and a region rule
            store(conn, member_profile, {
                'explicit_grants': [{'node_key': 'n1', 'protocol': 'xray'}, {'node_key': 'n1', 'protocol': 'awg'}],
                'rules': [{'scope': 'region', 'region_id': region_id, 'protocols': ['awg', 'xray']}],
                'exclusions': [],
            })
            materialize(conn, member_profile)

        # Remove xray from n1
        response = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'protocols': ['awg']}
        )
        self.assertEqual(response.status_code, 200, response.text)

        with self.db.transaction() as conn:
            # Explicit xray grant must be pruned
            saved = raw_read(conn, member_profile)
            self.assertEqual(saved['explicit_grants'], [{'node_key': 'n1', 'protocol': 'awg'}])
            # Region rule remains intact
            self.assertEqual(
                saved['rules'],
                [{'scope': 'region', 'region_id': region_id, 'protocols': ['awg', 'xray']}]
            )
            # Materialize must NOT reintroduce absent protocol
            materialize(conn, member_profile)
            grants = conn.execute(
                "SELECT node_key, protocol FROM backend_grants WHERE profile_id=?", (member_profile,)
            ).fetchall()
            grant_pairs = [(g['node_key'], g['protocol']) for g in grants]
            self.assertNotIn(('n1', 'xray'), grant_pairs)
            self.assertIn(('n1', 'awg'), grant_pairs)

    def test_admin_preserved_member_policy_pruned(self):
        from backend.profiles import ProfileRepository
        ProfileRepository(self.db).ensure_account_profile(self.admin.id)
        self.assertEqual(self.create_node('n1').status_code, 201)
        self.apply_settings('n1', 1)

        with self.db.transaction() as conn:
            admin_profile = conn.execute(
                "SELECT id FROM backend_profiles WHERE owner_account_id=?", (self.admin.id,)
            ).fetchone()['id']
            # Store a preserved member policy for this admin
            store(conn, admin_profile, {
                'explicit_grants': [{'node_key': 'n1', 'protocol': 'xray'}, {'node_key': 'n1', 'protocol': 'awg'}],
                'rules': [],
                'exclusions': [],
            })

        # Remove xray from n1
        response = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'protocols': ['awg']}
        )
        self.assertEqual(response.status_code, 200, response.text)

        with self.db.connect() as conn:
            saved = raw_read(conn, admin_profile)
            self.assertEqual(saved['explicit_grants'], [{'node_key': 'n1', 'protocol': 'awg'}])

    def test_historical_ensures_and_all_awg_devices(self):
        self.assertEqual(self.create_node('n1').status_code, 201)
        self.apply_settings('n1', 1)

        provisioned = self.provision(102).json()['account']
        with self.db.transaction() as conn:
            member_profile = conn.execute(
                "SELECT id, runtime_name FROM backend_profiles WHERE owner_account_id=?", (provisioned['id'],)
            ).fetchone()
            prof_id = member_profile['id']
            # Add two devices
            dev1 = str(uuid4())
            dev2 = str(uuid4())
            conn.execute(
                "INSERT INTO backend_devices(id, profile_id, display_name, runtime_name, status, created_at) VALUES (?, ?, 'Phone', 'dev_1', 'active', '2026-10-01')",
                (dev1, prof_id)
            )
            conn.execute(
                "INSERT INTO backend_devices(id, profile_id, display_name, runtime_name, status, created_at) VALUES (?, ?, 'Laptop', 'dev_2', 'active', '2026-10-01')",
                (dev2, prof_id)
            )
            # An older ensure task that is queued
            old_op = str(uuid4())
            conn.execute(
                "INSERT INTO backend_operations(id, profile_id, actor_id, desired_revision, status, created_at) VALUES (?, ?, ?, 1, 'awaiting_executor', '2026-10-01')",
                (old_op, prof_id, self.admin.id)
            )
            old_task = str(uuid4())
            conn.execute(
                """INSERT INTO backend_operation_tasks(id, operation_id, node_key, protocol, device_id, action, status, intent_json)
                   VALUES (?, ?, 'n1', 'awg', ?, 'ensure', 'awaiting_executor', '{}')""",
                (old_task, old_op, dev1)
            )

        # Remove awg from n1
        response = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'protocols': ['xray']}
        )
        self.assertEqual(response.status_code, 200, response.text)

        with self.db.connect() as conn:
            # Old ensure must be superseded
            old_status = conn.execute(
                "SELECT status FROM backend_operation_tasks WHERE id=?", (old_task,)
            ).fetchone()['status']
            self.assertEqual(old_status, 'superseded')

            # Delete tasks created for both AWG devices
            delete_tasks = conn.execute(
                """SELECT t.device_id, t.action, t.status FROM backend_operation_tasks t
                   JOIN backend_operations o ON o.id=t.operation_id
                   WHERE o.profile_id=? AND t.node_key='n1' AND t.protocol='awg' AND t.id != ?
                   ORDER BY t.device_id""",
                (prof_id, old_task)
            ).fetchall()
            self.assertGreaterEqual(len(delete_tasks), 2)
            for dt in delete_tasks:
                self.assertEqual(dt['action'], 'delete')
                self.assertEqual(dt['status'], 'awaiting_executor')

    def test_temporary_configs_revocation_lifecycle(self):
        self.assertEqual(self.create_node('n1').status_code, 201)
        self.apply_settings('n1', 1)

        t_queued = str(uuid4())
        t_active = str(uuid4())
        t_blocked = str(uuid4())
        t_revoke_blocked = str(uuid4())
        t_retained = str(uuid4())

        with self.db.transaction() as conn:
            # Queued config for xray
            conn.execute(
                """INSERT INTO backend_temporary_configs(id, actor_id, principal_id, command_key, node_key, node_revision,
                   protocol, transport, duration_seconds, runtime_name, intent_json, status, created_at)
                   VALUES (?, ?, 'p1', ?, 'n1', 1, 'xray', 'tcp', 43200, 'tmp_q', '{"ensure": true}', 'queued', '2026-10-01')""",
                (t_queued, self.admin.id, str(uuid4()))
            )
            # Active config for xray
            conn.execute(
                """INSERT INTO backend_temporary_configs(id, actor_id, principal_id, command_key, node_key, node_revision,
                   protocol, transport, duration_seconds, runtime_name, intent_json, status, created_at, expires_at)
                   VALUES (?, ?, 'p1', ?, 'n1', 1, 'xray', 'tcp', 43200, 'tmp_a', '{"ensure": true}', 'active', '2026-10-01', '2026-11-01T00:00:00+00:00')""",
                (t_active, self.admin.id, str(uuid4()))
            )
            # Blocked config for xray
            conn.execute(
                """INSERT INTO backend_temporary_configs(id, actor_id, principal_id, command_key, node_key, node_revision,
                   protocol, transport, duration_seconds, runtime_name, intent_json, status, created_at, error_code)
                   VALUES (?, ?, 'p1', ?, 'n1', 1, 'xray', 'tcp', 43200, 'tmp_b', '{"ensure": true}', 'blocked', '2026-10-01', 'outcome_unknown')""",
                (t_blocked, self.admin.id, str(uuid4()))
            )
            # Revoke_blocked config for xray
            conn.execute(
                """INSERT INTO backend_temporary_configs(id, actor_id, principal_id, command_key, node_key, node_revision,
                   protocol, transport, duration_seconds, runtime_name, intent_json, status, created_at, error_code)
                   VALUES (?, ?, 'p1', ?, 'n1', 1, 'xray', 'tcp', 43200, 'tmp_rb', '{"ensure": true}', 'revoke_blocked', '2026-10-01', 'outcome_unknown')""",
                (t_revoke_blocked, self.admin.id, str(uuid4()))
            )
            # Retained protocol awg config
            conn.execute(
                """INSERT INTO backend_temporary_configs(id, actor_id, principal_id, command_key, node_key, node_revision,
                   protocol, transport, duration_seconds, runtime_name, intent_json, status, created_at, expires_at)
                   VALUES (?, ?, 'p1', ?, 'n1', 1, 'awg', 'vpn', 43200, 'tmp_ret', '{"ensure": true}', 'active', '2026-10-01', '2026-11-01T00:00:00+00:00')""",
                (t_retained, self.admin.id, str(uuid4()))
            )

        # Remove xray from n1
        response = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'protocols': ['awg']}
        )
        self.assertEqual(response.status_code, 200, response.text)

        with self.db.connect() as conn:
            by_id = {
                r['id']: r for r in conn.execute(
                    "SELECT id, status, intent_json FROM backend_temporary_configs WHERE node_key='n1'"
                ).fetchall()
            }
            # Queued -> cancelled
            self.assertEqual(by_id[t_queued]['status'], 'cancelled')
            self.assertEqual(by_id[t_queued]['intent_json'], '{"ensure": true}')
            # Active -> revoking
            self.assertEqual(by_id[t_active]['status'], 'revoking')
            self.assertEqual(by_id[t_active]['intent_json'], '{"ensure": true}')
            # Blocked -> revoking
            self.assertEqual(by_id[t_blocked]['status'], 'revoking')
            self.assertEqual(by_id[t_blocked]['intent_json'], '{"ensure": true}')
            # Revoke_blocked -> NOT reset into replay, remains revoke_blocked
            self.assertEqual(by_id[t_revoke_blocked]['status'], 'revoke_blocked')
            # Retained protocol awg -> untouched active
            self.assertEqual(by_id[t_retained]['status'], 'active')

            # Events emitted
            events = conn.execute(
                "SELECT config_id, action, status FROM backend_temporary_config_events WHERE node_key='n1' ORDER BY occurred_at"
            ).fetchall()
            event_map = {e['config_id']: e for e in events}
            self.assertEqual(event_map[t_queued]['status'], 'cancelled')
            self.assertEqual(event_map[t_active]['status'], 'revoking')
            self.assertEqual(event_map[t_blocked]['status'], 'revoking')

    def test_confirmed_revocations_allow_node_settings_cleanup(self):
        from backend.profiles import ProfileRepository
        ProfileRepository(self.db).ensure_account_profile(self.admin.id)
        self.assertEqual(self.create_node('n1').status_code, 201)
        self.apply_settings('n1', 1)

        with self.db.transaction() as conn:
            admin_profile = conn.execute(
                "SELECT id FROM backend_profiles WHERE owner_account_id=?", (self.admin.id,)
            ).fetchone()['id']
            materialize(conn, admin_profile)

        # Remove xray
        response = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'protocols': ['awg']}
        )
        self.assertEqual(response.status_code, 200)

        # Queue apply settings for revision 2
        queue_resp = self.client.post(
            '/api/v1/nodes/n1/apply-settings',
            headers=self.admin_headers(
                **{'Idempotency-Key': str(uuid4()), 'If-Match': '"2"'}
            )
        )
        self.assertEqual(queue_resp.status_code, 202)
        task_id = queue_resp.json()['id']

        driver_calls = []

        class MockDriver:
            def inspect_node(self, nk):
                return {'health_state': 'running', 'awg_config_present': True}

            def apply_node_settings(self, command_id, intent):
                driver_calls.append(command_id)
                return json.dumps({
                    'node_key': intent['node_key'],
                    'revision': intent['revision'],
                    'settings_sha256': _digest(intent)
                })

        worker = NodeSettingsExecutor(self.db, MockDriver())

        # While operation delete tasks are awaiting_executor, NodeSettingsExecutor MUST NOT dispatch
        self.assertFalse(worker.run_one())
        self.assertEqual(driver_calls, [])
        with self.db.connect() as conn:
            task = conn.execute("SELECT status FROM backend_node_settings_tasks WHERE id=?", (task_id,)).fetchone()
            self.assertEqual(task['status'], 'awaiting_executor')

        # Confirm revocations by marking all delete tasks succeeded
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE backend_operation_tasks SET status='succeeded' WHERE node_key='n1' AND protocol='xray'"
            )

        # Now revocations are confirmed: NodeSettingsExecutor runs and dispatches apply_node_settings
        self.assertTrue(worker.run_one())
        self.assertEqual(driver_calls, [task_id])
        with self.db.connect() as conn:
            task = conn.execute("SELECT status FROM backend_node_settings_tasks WHERE id=?", (task_id,)).fetchone()
            self.assertEqual(task['status'], 'succeeded')
            node = conn.execute("SELECT applied_revision, desired_revision FROM backend_nodes WHERE key='n1'").fetchone()
            self.assertEqual((node['desired_revision'], node['applied_revision']), (2, 2))

    def test_blocked_or_unknown_preserves_fence(self):
        from backend.profiles import ProfileRepository
        ProfileRepository(self.db).ensure_account_profile(self.admin.id)
        self.assertEqual(self.create_node('n1').status_code, 201)
        self.apply_settings('n1', 1)

        with self.db.transaction() as conn:
            admin_profile = conn.execute(
                "SELECT id FROM backend_profiles WHERE owner_account_id=?", (self.admin.id,)
            ).fetchone()['id']
            materialize(conn, admin_profile)

        # Remove xray
        self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'protocols': ['awg']}
        )
        queue_resp = self.client.post(
            '/api/v1/nodes/n1/apply-settings',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"2"'})
        )
        task_id = queue_resp.json()['id']

        # Task is blocked
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE backend_operation_tasks SET status='blocked' WHERE node_key='n1' AND protocol='xray'"
            )

        class MockDriver:
            def inspect_node(self, nk):
                return {'health_state': 'running', 'awg_config_present': True}

            def apply_node_settings(self, command_id, intent):
                raise AssertionError('settings cleanup must not run while revocations blocked')

        worker = NodeSettingsExecutor(self.db, MockDriver())
        self.assertFalse(worker.run_one())
        with self.db.connect() as conn:
            task = conn.execute("SELECT status FROM backend_node_settings_tasks WHERE id=?", (task_id,)).fetchone()
            self.assertEqual(task['status'], 'awaiting_executor')

    def test_retained_protocol_and_other_nodes_intact(self):
        self.assertEqual(self.create_node('n1', protocols=['awg', 'xray']).status_code, 201)
        self.apply_settings('n1', 1)
        self.assertEqual(self.create_node('n2', protocols=['xray'], settings={'public_host': 'n2.example.test', 'xray_tcp_port': 444}, transport='ssh', ssh_target='root@192.168.1.100').status_code, 201)
        self.apply_settings('n2', 1)

        provisioned = self.provision(102).json()['account']
        with self.db.transaction() as conn:
            member_profile = conn.execute(
                "SELECT id FROM backend_profiles WHERE owner_account_id=?", (provisioned['id'],)
            ).fetchone()['id']
            store(conn, member_profile, {
                'explicit_grants': [
                    {'node_key': 'n1', 'protocol': 'awg'},
                    {'node_key': 'n1', 'protocol': 'xray'},
                    {'node_key': 'n2', 'protocol': 'xray'}
                ],
                'rules': [],
                'exclusions': [],
            })
            materialize(conn, member_profile)

        # Remove xray only from n1
        response = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'protocols': ['awg']}
        )
        self.assertEqual(response.status_code, 200)

        with self.db.connect() as conn:
            grants = conn.execute(
                "SELECT node_key, protocol FROM backend_grants WHERE profile_id=?", (member_profile,)
            ).fetchall()
            grant_pairs = {(g['node_key'], g['protocol']) for g in grants}
            self.assertEqual(grant_pairs, {('n1', 'awg'), ('n2', 'xray')})

            # Check that only tasks for (n1, xray) were recorded
            latest_op = conn.execute(
                "SELECT id FROM backend_operations WHERE profile_id=? ORDER BY desired_revision DESC LIMIT 1",
                (member_profile,)
            ).fetchone()['id']
            tasks = conn.execute(
                "SELECT node_key, protocol, action FROM backend_operation_tasks WHERE operation_id=?",
                (latest_op,)
            ).fetchall()
            task_targets = [(t['node_key'], t['protocol'], t['action']) for t in tasks]
            self.assertEqual(task_targets, [('n1', 'xray', 'delete')])

    def test_idempotent_retry_and_adding_protocol_restores_admin_only_as_intended(self):
        from backend.profiles import ProfileRepository
        ProfileRepository(self.db).ensure_account_profile(self.admin.id)
        self.assertEqual(self.create_node('n1').status_code, 201)
        self.apply_settings('n1', 1)

        provisioned = self.provision(102).json()['account']
        with self.db.transaction() as conn:
            member_profile = conn.execute(
                "SELECT id FROM backend_profiles WHERE owner_account_id=?", (provisioned['id'],)
            ).fetchone()['id']
            store(conn, member_profile, {
                'explicit_grants': [{'node_key': 'n1', 'protocol': 'xray'}],
                'rules': [],
                'exclusions': [],
            })
            materialize(conn, member_profile)

        edit_key = str(uuid4())
        resp1 = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': edit_key, 'If-Match': '"1"'}),
            json={'protocols': ['awg']}
        )
        self.assertEqual(resp1.status_code, 200)

        # Idempotent retry returns identical result
        resp2 = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': edit_key, 'If-Match': '"1"'}),
            json={'protocols': ['awg']}
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp1.json(), resp2.json())

        # Complete cleanup and settings apply for revision 2
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_operation_tasks SET status='succeeded' WHERE node_key='n1'")
        self.apply_settings('n1', 2)

        # Now add xray back to n1
        add_resp = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"2"'}),
            json={'protocols': ['awg', 'xray']}
        )
        self.assertEqual(add_resp.status_code, 200)

        with self.db.connect() as conn:
            admin_profile = conn.execute(
                "SELECT id FROM backend_profiles WHERE owner_account_id=?", (self.admin.id,)
            ).fetchone()['id']
            admin_grants = {(g['node_key'], g['protocol']) for g in conn.execute(
                "SELECT node_key, protocol FROM backend_grants WHERE profile_id=?", (admin_profile,)
            ).fetchall()}
            # Admin automatically derives and gets (n1, 'xray') back
            self.assertIn(('n1', 'xray'), admin_grants)
            self.assertIn(('n1', 'awg'), admin_grants)

            member_grants = {(g['node_key'], g['protocol']) for g in conn.execute(
                "SELECT node_key, protocol FROM backend_grants WHERE profile_id=?", (member_profile,)
            ).fetchall()}
            # Member whose explicit grant was pruned does NOT get it restored
            self.assertNotIn(('n1', 'xray'), member_grants)

    def test_grants_manage_permission_required_when_revoking_grants(self):
        self.assertEqual(self.create_node('n1').status_code, 201)
        self.apply_settings('n1', 1)

        # Create an account with only 'nodes.manage' (without 'grants.manage')
        limited_actor_id = str(uuid4())
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO backend_accounts(id, role, status) VALUES (?, 'admin', 'approved')",
                (limited_actor_id,)
            )

        from backend.authorization import require_permission, Actor, Principal, PrincipalKind
        limited_actor = Actor(
            Principal('limited', PrincipalKind.ACCOUNT, frozenset({'nodes.manage', 'account.self.read'}), limited_actor_id),
            Account(limited_actor_id, 'admin', 'approved')
        )

        from backend.nodes import NodeService
        service = NodeService(self.db)
        with self.assertRaises(AccessDenied) as ctx:
            service.command(
                limited_actor,
                action='edit',
                node_key='n1',
                revision=1,
                command_key=str(uuid4()),
                values={'protocols': ['awg']}
            )
        self.assertEqual(ctx.exception.code, 'permission_denied')

    def test_integration_automatic_profile_and_admin_revokes_before_settings_cleanup(self):
        """Integration: actual IntentExecutor completes automatic profile/admin deletes before NodeSettingsExecutor cleanup."""
        ProfileRepository(self.db).ensure_account_profile(self.admin.id)
        created = self.create_node('n1', protocols=['awg', 'xray'])
        self.assertEqual(created.status_code, 201)
        self.apply_settings('n1', 1)

        provisioned = self.provision(102).json()['account']
        with self.db.transaction() as conn:
            member_profile = conn.execute(
                "SELECT id FROM backend_profiles WHERE owner_account_id=?", (provisioned['id'],)
            ).fetchone()['id']
            dev_id = str(uuid4())
            conn.execute(
                "INSERT INTO backend_devices(id, profile_id, display_name, runtime_name, status, created_at) "
                "VALUES (?, ?, 'Phone', 'dev_1', 'active', '2026-10-01')",
                (dev_id, member_profile)
            )
            store(conn, member_profile, {
                'explicit_grants': [
                    {'node_key': 'n1', 'protocol': 'awg'},
                    {'node_key': 'n1', 'protocol': 'xray'}
                ],
                'rules': [],
                'exclusions': [],
            })
            materialize(conn, member_profile)

        # Confirm grants exist for both admin and member
        with self.db.connect() as conn:
            admin_profile = conn.execute(
                "SELECT id FROM backend_profiles WHERE owner_account_id=?", (self.admin.id,)
            ).fetchone()['id']
            admin_grants = {(g['node_key'], g['protocol']) for g in conn.execute(
                "SELECT node_key, protocol FROM backend_grants WHERE profile_id=?", (admin_profile,)
            ).fetchall()}
            member_grants = {(g['node_key'], g['protocol']) for g in conn.execute(
                "SELECT node_key, protocol FROM backend_grants WHERE profile_id=?", (member_profile,)
            ).fetchall()}
            self.assertIn(('n1', 'xray'), admin_grants)
            self.assertIn(('n1', 'awg'), admin_grants)
            self.assertIn(('n1', 'xray'), member_grants)
            self.assertIn(('n1', 'awg'), member_grants)

        # Admin removes xray
        response = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'protocols': ['awg']}
        )
        self.assertEqual(response.status_code, 200, response.text)

        # Grants for xray revoked; delete tasks recorded for admin and member
        with self.db.connect() as conn:
            admin_grants_after = {(g['node_key'], g['protocol']) for g in conn.execute(
                "SELECT node_key, protocol FROM backend_grants WHERE profile_id=?", (admin_profile,)
            ).fetchall()}
            member_grants_after = {(g['node_key'], g['protocol']) for g in conn.execute(
                "SELECT node_key, protocol FROM backend_grants WHERE profile_id=?", (member_profile,)
            ).fetchall()}
            self.assertNotIn(('n1', 'xray'), admin_grants_after)
            self.assertIn(('n1', 'awg'), admin_grants_after)
            self.assertNotIn(('n1', 'xray'), member_grants_after)
            self.assertIn(('n1', 'awg'), member_grants_after)

            pending_deletes = conn.execute(
                "SELECT t.id, t.protocol, t.action, t.status FROM backend_operation_tasks t "
                "WHERE t.node_key='n1' AND t.protocol='xray' AND t.action='delete'"
            ).fetchall()
            self.assertGreaterEqual(len(pending_deletes), 2)
            for d in pending_deletes:
                self.assertEqual(d['status'], 'awaiting_executor')

        # Queue apply settings for revision 2
        queue_resp = self.client.post(
            '/api/v1/nodes/n1/apply-settings',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"2"'})
        )
        self.assertEqual(queue_resp.status_code, 202)
        settings_task_id = queue_resp.json()['id']

        driver = FakeUnifiedDriver()
        settings_worker = NodeSettingsExecutor(self.db, driver)
        intent_worker = IntentExecutor(self.db, driver)

        # Settings executor must NOT run while delete tasks are awaiting executor
        self.assertFalse(settings_worker.run_one())
        self.assertEqual([c for c in driver.calls if c[0] == 'apply_node_settings'], [])
        with self.db.connect() as conn:
            task = conn.execute(
                "SELECT status FROM backend_node_settings_tasks WHERE id=?", (settings_task_id,)
            ).fetchone()
            self.assertEqual(task['status'], 'awaiting_executor')

        # Run actual IntentExecutor to drain delete tasks (NO manual completion)
        executed_count = 0
        while intent_worker.run_one():
            executed_count += 1
        self.assertGreaterEqual(executed_count, 2)

        # Verify all xray deletes succeeded via driver RPC execution
        with self.db.connect() as conn:
            xray_tasks = conn.execute(
                "SELECT status, action FROM backend_operation_tasks WHERE node_key='n1' AND protocol='xray' AND action='delete'"
            ).fetchall()
            self.assertGreaterEqual(len(xray_tasks), 2)
            for xt in xray_tasks:
                self.assertEqual(xt['action'], 'delete')
                self.assertEqual(xt['status'], 'succeeded')

            old_ensures = conn.execute(
                "SELECT status FROM backend_operation_tasks WHERE node_key='n1' AND protocol='xray' AND action='ensure'"
            ).fetchall()
            for oe in old_ensures:
                self.assertEqual(oe['status'], 'superseded')

        executed_driver_deletes = [
            c for c in driver.calls
            if c[0] == 'execute' and c[2].get('action') == 'delete' and c[2].get('protocol') == 'xray'
        ]
        self.assertGreaterEqual(len(executed_driver_deletes), 2)

        # Now NodeSettingsExecutor can execute settings cleanup
        self.assertTrue(settings_worker.run_one())
        with self.db.connect() as conn:
            settings_task = conn.execute(
                "SELECT status FROM backend_node_settings_tasks WHERE id=?", (settings_task_id,)
            ).fetchone()
            self.assertEqual(settings_task['status'], 'succeeded')
            node = conn.execute(
                "SELECT applied_revision, desired_revision FROM backend_nodes WHERE key='n1'"
            ).fetchone()
            self.assertEqual((node['desired_revision'], node['applied_revision']), (2, 2))

        self.assertEqual(len([c for c in driver.calls if c[0] == 'apply_node_settings']), 1)

    def test_integration_retained_protocol_ensure_and_temporary_lease_held_until_successful_settings(self):
        """Integration: retained protocol queued ensure and temporary lease held across Save->Apply gap and pending settings."""
        ProfileRepository(self.db).ensure_account_profile(self.admin.id)
        created = self.create_node('n1', protocols=['awg', 'xray'])
        self.assertEqual(created.status_code, 201)
        self.apply_settings('n1', 1)

        provisioned = self.provision(102).json()['account']
        member_id = provisioned['id']
        with self.db.transaction() as conn:
            member_profile = conn.execute(
                "SELECT id FROM backend_profiles WHERE owner_account_id=?", (member_id,)
            ).fetchone()['id']
            dev_id = str(uuid4())
            conn.execute(
                "INSERT INTO backend_devices(id, profile_id, display_name, runtime_name, status, created_at) "
                "VALUES (?, ?, 'Dev', 'dev_1', 'active', '2026-10-01')",
                (dev_id, member_profile)
            )
            # Queued ensure for retained protocol awg
            op_id = str(uuid4())
            conn.execute(
                "INSERT INTO backend_operations(id, profile_id, actor_id, desired_revision, status, created_at) "
                "VALUES (?, ?, ?, 2, 'awaiting_executor', '2026-10-01')",
                (op_id, member_profile, self.admin.id)
            )
            awg_ensure_id = str(uuid4())
            conn.execute(
                """INSERT INTO backend_operation_tasks(id, operation_id, node_key, protocol, device_id, action, status, intent_json)
                   VALUES (?, ?, 'n1', 'awg', ?, 'ensure', 'awaiting_executor', '{"action": "ensure", "protocol": "awg"}')""",
                (awg_ensure_id, op_id, dev_id)
            )

            # Active temporary config for retained protocol awg
            t_active = str(uuid4())
            conn.execute(
                """INSERT INTO backend_temporary_configs(id, actor_id, principal_id, command_key, node_key, node_revision,
                   protocol, transport, duration_seconds, runtime_name, intent_json, status, created_at, expires_at)
                   VALUES (?, ?, 'p1', ?, 'n1', 1, 'awg', 'vpn', 43200, 'tmp_act', '{"lease_seconds": 43200, "protocol": "awg", "transport": "vpn"}', 'active', '2026-10-01', '2026-11-01T00:00:00+00:00')""",
                (t_active, self.admin.id, str(uuid4()))
            )

            # Queued temporary config created against the current applied revision
            t_queued = str(uuid4())
            conn.execute(
                """INSERT INTO backend_temporary_configs(id, actor_id, principal_id, command_key, node_key, node_revision,
                   protocol, transport, duration_seconds, runtime_name, intent_json, status, created_at)
                   VALUES (?, ?, 'p1', ?, 'n1', 1, 'awg', 'vpn', 43200, 'tmp_q', '{"lease_seconds": 43200, "protocol": "awg", "transport": "vpn", "action": "ensure"}', 'queued', '2026-10-01')""",
                (t_queued, self.admin.id, str(uuid4()))
            )

        # 1. SAVE: edit node to remove xray (Save->Apply gap: desired=2, applied=1, no apply-settings queued)
        patch_resp = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'protocols': ['awg']}
        )
        self.assertEqual(patch_resp.status_code, 200)

        driver = FakeUnifiedDriver()
        intent_worker = IntentExecutor(self.db, driver)
        settings_worker = NodeSettingsExecutor(self.db, driver)
        temp_service = TemporaryConfigService(self.db, driver)

        # Both retained leases move to the saved revision without reissuing.
        with self.db.connect() as conn:
            for config_id in (t_active, t_queued):
                lease = conn.execute('SELECT node_revision FROM backend_temporary_configs WHERE id=?', (config_id,)).fetchone()
                self.assertEqual(lease['node_revision'], 2)

        # In Save->Apply gap: active lease untouched
        with self.db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM backend_temporary_configs WHERE id=?", (t_active,)).fetchone()['status'],
                'active'
            )

        # In Save->Apply gap: queued temporary lease held
        self.assertFalse(temp_service.run_one())
        with self.db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM backend_temporary_configs WHERE id=?", (t_queued,)).fetchone()['status'],
                'queued'
            )

        # In Save->Apply gap: delete tasks for removed xray can run, but retained awg ensure must NOT run
        while intent_worker.run_one():
            pass
        with self.db.connect() as conn:
            awg_task = conn.execute(
                "SELECT status FROM backend_operation_tasks WHERE id=?", (awg_ensure_id,)
            ).fetchone()
            self.assertEqual(awg_task['status'], 'awaiting_executor')
            xray_tasks = conn.execute(
                "SELECT status FROM backend_operation_tasks WHERE node_key='n1' AND protocol='xray' AND action='delete'"
            ).fetchall()
            self.assertTrue(all(xt['status'] == 'succeeded' for xt in xray_tasks))

        # 2. APPLY: queue apply settings for revision 2
        queue_resp = self.client.post(
            '/api/v1/nodes/n1/apply-settings',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"2"'})
        )
        self.assertEqual(queue_resp.status_code, 202)
        settings_task_id = queue_resp.json()['id']

        # While settings are pending: awg ensure and queued lease still held
        self.assertFalse(intent_worker.run_one())
        self.assertFalse(temp_service.run_one())
        with self.db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM backend_operation_tasks WHERE id=?", (awg_ensure_id,)).fetchone()['status'],
                'awaiting_executor'
            )
            self.assertEqual(
                conn.execute("SELECT status FROM backend_temporary_configs WHERE id=?", (t_queued,)).fetchone()['status'],
                'queued'
            )

        # 3. Settings executor runs cleanup (xray revocations already succeeded)
        self.assertTrue(settings_worker.run_one())
        with self.db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM backend_node_settings_tasks WHERE id=?", (settings_task_id,)).fetchone()['status'],
                'succeeded'
            )
            node = conn.execute("SELECT applied_revision, desired_revision FROM backend_nodes WHERE key='n1'").fetchone()
            self.assertEqual((node['desired_revision'], node['applied_revision']), (2, 2))

        # 4. Now that settings are successfully applied, retained awg ensure and queued lease unblock
        self.assertTrue(intent_worker.run_one())
        with self.db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM backend_operation_tasks WHERE id=?", (awg_ensure_id,)).fetchone()['status'],
                'succeeded'
            )

        self.assertTrue(temp_service.run_one())
        with self.db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM backend_temporary_configs WHERE id=?", (t_queued,)).fetchone()['status'],
                'active'
            )
            self.assertEqual(
                conn.execute("SELECT status FROM backend_temporary_configs WHERE id=?", (t_active,)).fetchone()['status'],
                'active'
            )

    def test_integration_initial_agent_only_applied0_adding_first_service_restores_admin_grant_after_apply(self):
        """Integration: initial agent-only node (applied0) adding first service restores admin grant and enqueues ensure after apply."""
        ProfileRepository(self.db).ensure_account_profile(self.admin.id)
        with self.db.transaction() as conn:
            admin_profile = conn.execute(
                "SELECT id FROM backend_profiles WHERE owner_account_id=?", (self.admin.id,)
            ).fetchone()['id']
            conn.execute(
                "INSERT INTO backend_devices(id, profile_id, display_name, runtime_name, status, created_at) "
                "VALUES (?, ?, 'Laptop', 'dev_admin', 'active', '2026-10-01')",
                (str(uuid4()), admin_profile)
            )

        # Create agent-only node (protocols=[])
        create_resp = self.create_node(key='n1', protocols=[], settings={'public_host': 'n1.example.test'})
        self.assertEqual(create_resp.status_code, 201)

        with self.db.connect() as conn:
            node = conn.execute("SELECT applied_revision, desired_revision, enabled, protocols_json FROM backend_nodes WHERE key='n1'").fetchone()
            self.assertEqual(node['applied_revision'], 0)
            self.assertEqual(node['desired_revision'], 1)
            self.assertEqual(node['enabled'], 0)

            admin_profile = conn.execute(
                "SELECT id FROM backend_profiles WHERE owner_account_id=?", (self.admin.id,)
            ).fetchone()['id']
            grants = conn.execute(
                "SELECT node_key, protocol FROM backend_grants WHERE profile_id=?", (admin_profile,)
            ).fetchall()
            self.assertEqual(grants, [])

        # Add first service (awg)
        patch_resp = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'protocols': ['awg']}
        )
        self.assertEqual(patch_resp.status_code, 200)

        # Before apply, applied_revision is still 0, so admin grant is NOT yet materialized
        with self.db.connect() as conn:
            node = conn.execute("SELECT applied_revision, desired_revision FROM backend_nodes WHERE key='n1'").fetchone()
            self.assertEqual(node['applied_revision'], 0)
            self.assertEqual(node['desired_revision'], 2)
            grants = conn.execute(
                "SELECT node_key, protocol FROM backend_grants WHERE profile_id=?", (admin_profile,)
            ).fetchall()
            self.assertEqual(grants, [])

        # Queue apply settings for revision 2
        queue_resp = self.client.post(
            '/api/v1/nodes/n1/apply-settings',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"2"'})
        )
        self.assertEqual(queue_resp.status_code, 202)
        settings_task_id = queue_resp.json()['id']

        driver = FakeUnifiedDriver()
        settings_worker = NodeSettingsExecutor(self.db, driver)
        intent_worker = IntentExecutor(self.db, driver)

        # Apply settings via NodeSettingsExecutor
        self.assertTrue(settings_worker.run_one())
        with self.db.connect() as conn:
            node = conn.execute("SELECT applied_revision, desired_revision, enabled FROM backend_nodes WHERE key='n1'").fetchone()
            self.assertEqual((node['desired_revision'], node['applied_revision'], node['enabled']), (2, 2, 1))

            # Now applied_revision > 0: admin grant is restored in backend_grants
            grants = conn.execute(
                "SELECT node_key, protocol FROM backend_grants WHERE profile_id=?", (admin_profile,)
            ).fetchall()
            self.assertEqual([(g['node_key'], g['protocol']) for g in grants], [('n1', 'awg')])

            # Outbox ensure task created for admin
            task = conn.execute(
                """SELECT t.id, t.action, t.status, t.protocol FROM backend_operation_tasks t
                   JOIN backend_operations o ON o.id=t.operation_id
                   WHERE o.profile_id=? AND t.node_key='n1' AND t.protocol='awg'
                   ORDER BY o.desired_revision DESC LIMIT 1""",
                (admin_profile,)
            ).fetchone()
            self.assertIsNotNone(task)
            self.assertEqual(task['action'], 'ensure')
            self.assertEqual(task['status'], 'awaiting_executor')

        # Execute ensure task via IntentExecutor
        self.assertTrue(intent_worker.run_one())
        with self.db.connect() as conn:
            task_after = conn.execute(
                "SELECT status FROM backend_operation_tasks WHERE id=?", (task['id'],)
            ).fetchone()
            self.assertEqual(task_after['status'], 'succeeded')

        self.assertTrue(any(
            c[0] == 'execute' and c[2].get('action') == 'ensure' and c[2].get('protocol') == 'awg'
            for c in driver.calls
        ))

    def test_integration_global_unknown_retained_protocol_task_blocks_cleanup(self):
        """Integration: unknown (blocked) retained-protocol profile task blocks node settings cleanup."""
        ProfileRepository(self.db).ensure_account_profile(self.admin.id)
        created = self.create_node('n1', protocols=['awg', 'xray'])
        self.assertEqual(created.status_code, 201)
        self.apply_settings('n1', 1)

        provisioned = self.provision(102).json()['account']
        with self.db.transaction() as conn:
            member_profile = conn.execute(
                "SELECT id FROM backend_profiles WHERE owner_account_id=?", (provisioned['id'],)
            ).fetchone()['id']
            dev_id = str(uuid4())
            conn.execute(
                "INSERT INTO backend_devices(id, profile_id, display_name, runtime_name, status, created_at) "
                "VALUES (?, ?, 'Dev', 'dev_1', 'active', '2026-10-01')",
                (dev_id, member_profile)
            )
            # Retained protocol awg operation task in blocked state (uncertain outcome)
            op_id = str(uuid4())
            conn.execute(
                "INSERT INTO backend_operations(id, profile_id, actor_id, desired_revision, status, created_at) "
                "VALUES (?, ?, ?, 1, 'blocked', '2026-10-01')",
                (op_id, member_profile, self.admin.id)
            )
            blocked_task_id = str(uuid4())
            conn.execute(
                """INSERT INTO backend_operation_tasks(id, operation_id, node_key, protocol, device_id, action, status, intent_json)
                   VALUES (?, ?, 'n1', 'awg', ?, 'ensure', 'blocked', '{"action": "ensure", "protocol": "awg"}')""",
                (blocked_task_id, op_id, dev_id)
            )

        # Remove xray
        patch_resp = self.client.patch(
            '/api/v1/nodes/n1',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}),
            json={'protocols': ['awg']}
        )
        self.assertEqual(patch_resp.status_code, 200)

        # Queue apply settings for revision 2
        queue_resp = self.client.post(
            '/api/v1/nodes/n1/apply-settings',
            headers=self.admin_headers(**{'Idempotency-Key': str(uuid4()), 'If-Match': '"2"'})
        )
        self.assertEqual(queue_resp.status_code, 202)
        settings_task_id = queue_resp.json()['id']

        driver = FakeUnifiedDriver()
        intent_worker = IntentExecutor(self.db, driver)
        settings_worker = NodeSettingsExecutor(self.db, driver)

        # IntentExecutor cannot run other tasks on n1 because an operation task on n1 is blocked
        self.assertFalse(intent_worker.run_one())

        # Settings cleanup is blocked
        self.assertFalse(settings_worker.run_one())

        # Even if all xray delete tasks are marked succeeded, the blocked retained-protocol task STILL blocks cleanup
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE backend_operation_tasks SET status='succeeded' WHERE node_key='n1' AND protocol='xray'"
            )

        # NodeSettingsExecutor checks SELECT 1 FROM backend_operation_tasks WHERE node_key=? AND status IN ('running', 'blocked')
        self.assertFalse(settings_worker.run_one())
        self.assertEqual([c for c in driver.calls if c[0] == 'apply_node_settings'], [])
        with self.db.connect() as conn:
            task = conn.execute("SELECT status FROM backend_node_settings_tasks WHERE id=?", (settings_task_id,)).fetchone()
            self.assertEqual(task['status'], 'awaiting_executor')

        # Now resolve the blocked retained-protocol task
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_operation_tasks SET status='succeeded' WHERE id=?", (blocked_task_id,))
            conn.execute("UPDATE backend_operations SET status='succeeded' WHERE id=?", (op_id,))

        # With all blocked tasks on the node cleared, settings cleanup proceeds and succeeds
        self.assertTrue(settings_worker.run_one())
        with self.db.connect() as conn:
            task = conn.execute("SELECT status FROM backend_node_settings_tasks WHERE id=?", (settings_task_id,)).fetchone()
            self.assertEqual(task['status'], 'succeeded')
            node = conn.execute("SELECT applied_revision, desired_revision FROM backend_nodes WHERE key='n1'").fetchone()
            self.assertEqual((node['desired_revision'], node['applied_revision']), (2, 2))

        self.assertEqual(len([c for c in driver.calls if c[0] == 'apply_node_settings']), 1)


if __name__ == '__main__':
    unittest.main()
