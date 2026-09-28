from __future__ import annotations

from uuid import uuid4
import unittest

import grpc
from fastapi.testclient import TestClient
from backend.http_api import create_app

from tests.test_backend_http import BackendHTTPTests


class BackendNodeTests(unittest.TestCase):
    setUp = BackendHTTPTests.setUp
    register = BackendHTTPTests.register

    def admin_headers(self, **extra):
        return {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101', **extra}

    def create(self, body=None, key=None):
        body = body or {'key': 'lv1', 'title': 'Latvia #1', 'region': 'Latvia',
                        'protocols': ['xray', 'awg'], 'xray_transports': ['tcp', 'xhttp'],
                        'settings': {'public_host': 'lv1.example.test', 'xray_tcp_port': 443}}
        return self.client.post('/api/v1/nodes', headers=self.admin_headers(**{
            'Idempotency-Key': key or str(uuid4())}), json=body)

    def edit(self, revision, body, key=None):
        return self.client.patch('/api/v1/nodes/lv1', headers=self.admin_headers(**{
            'Idempotency-Key': key or str(uuid4()), 'If-Match': f'"{revision}"'}), json=body)

    def test_create_read_and_update_desired_node_without_claiming_runtime(self):
        created = self.create()
        self.assertEqual(created.status_code, 201, created.text)
        node = created.json()
        self.assertFalse(node['enabled'])
        self.assertEqual((node['desired_revision'], node['applied_revision']), (1, 0))
        self.assertEqual(node['settings'], {'public_host': 'lv1.example.test', 'xray_tcp_port': 443})
        self.assertEqual(created.headers['ETag'], '"1"')
        listed = self.client.get('/api/v1/nodes', headers=self.admin_headers())
        self.assertEqual(listed.json()['items'], [node])
        self.assertEqual(self.client.get('/api/v1/nodes/lv1', headers=self.admin_headers()).json(), node)
        changed = self.edit(1, {'title': 'Latvia #2', 'settings': {'public_host': 'new.example.test'}})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()['settings'], {'public_host': 'new.example.test'})
        self.assertEqual(changed.json()['desired_revision'], 2)
        self.assertEqual(changed.json()['applied_revision'], 0)
        self.assertFalse(changed.json()['enabled'])
        self.assertEqual(self.edit(1, {'title': 'stale'}).status_code, 412)

    def test_idempotency_key_and_retirement_fence(self):
        key = str(uuid4())
        first = self.create(key=key)
        self.assertEqual(self.create(key=key).json(), first.json())
        conflict = self.create(body={'key': 'other', 'title': 'Other', 'region': 'EU', 'protocols': []}, key=key)
        self.assertEqual(conflict.json()['error']['code'], 'idempotency_conflict')
        self.assertEqual(self.create().json()['error']['code'], 'node_key_conflict')
        edit_key = str(uuid4())
        changed = self.edit(1, {'title': 'New'}, key=edit_key)
        self.assertEqual(self.edit(1, {'title': 'New'}, key=edit_key).json(), changed.json())
        self.assertEqual(self.edit(1, {'title': 'Different'}, key=edit_key).status_code, 409)
        with self.db.transaction() as conn:
            conn.execute('''INSERT INTO backend_node_retirements
                (node_key, actor_id, mode, reason, unfinished_tasks, retired_at)
                VALUES (?, ?, 'registry_only', 'retired by test', 0, '2026-09-28')''', ('old', self.admin.id))
        response = self.create(body={'key': 'old', 'title': 'Old', 'region': 'EU', 'protocols': []})
        self.assertEqual(response.json()['error']['code'], 'node_key_retired')

    def test_access_validation_and_active_grant_guard(self):
        member = self.register(102).json()
        self.db.connection.execute("UPDATE backend_accounts SET status = 'approved' WHERE id = ?", (member['id'],))
        member_headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '102'}
        self.assertEqual(self.client.get('/api/v1/nodes', headers=member_headers).status_code, 403)
        self.assertEqual(self.client.post('/api/v1/nodes', headers={**member_headers,
            'Idempotency-Key': str(uuid4())}, json={'key': 'foo', 'title': 'Foo', 'region': 'EU', 'protocols': []}).status_code, 403)
        for body in ({'key': '../bad', 'title': 'Bad', 'region': 'EU', 'protocols': []},
                     {'key': 'bad', 'title': 'Bad', 'region': 'EU', 'protocols': ['awg'],
                      'settings': {'awg_port': 70000}},
                     {'key': 'bad', 'title': 'Bad', 'region': 'EU', 'protocols': ['awg'],
                      'xray_transports': ['tcp']}):
            self.assertEqual(self.create(body=body).status_code, 422)
        self.assertEqual(self.create().status_code, 201)
        profile_id = self.db.connection.execute('''INSERT INTO backend_profiles
            (id, runtime_name, display_name) VALUES (?, ?, ?) RETURNING id''',
            (str(uuid4()), 'test', 'Test')).fetchone()['id']
        self.db.connection.execute('INSERT INTO backend_grants(profile_id, node_key, protocol) VALUES (?, ?, ?)',
                                   (profile_id, 'lv1', 'awg'))
        response = self.edit(1, {'protocols': ['xray']})
        self.assertEqual(response.json()['error']['code'], 'node_protocol_in_use')
        self.assertEqual(self.client.get('/api/v1/nodes/lv1', headers=self.admin_headers()).json()['protocols'], ['awg', 'xray'])
        self.assertEqual(self.edit(1, {'settings': {'awg_port': None}}).status_code, 422)
        self.assertEqual(self.client.patch('/api/v1/nodes/lv1', headers=self.admin_headers(**{
            'Idempotency-Key': str(uuid4())}), json={'title': 'No revision'}).status_code, 428)

    def test_runtime_inspection_is_read_only_and_never_acknowledges_settings(self):
        self.assertEqual(self.create().status_code, 201)

        class Driver:
            calls = []

            def inspect_node(self, node_key):
                self.calls.append(node_key)
                return {'node_key': node_key, 'health_state': 'running',
                        'runtime_version': '0.4.1', 'runtime_commit': 'abc123',
                        'xray_config_present': True, 'awg_config_present': True}

        driver = Driver()
        with TestClient(create_app(self.db, node_driver=driver)) as client:
            response = client.get('/api/v1/nodes/lv1/runtime', headers=self.admin_headers())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(driver.calls, ['lv1'])
            self.assertFalse(response.json()['settings_verified'])
            self.assertEqual((response.json()['desired_revision'], response.json()['applied_revision']), (1, 0))
            self.assertEqual(client.get('/api/v1/nodes/other/runtime', headers=self.admin_headers()).status_code, 404)
            member = self.register(102).json()
            self.db.connection.execute("UPDATE backend_accounts SET status = 'approved' WHERE id = ?", (member['id'],))
            denied = client.get('/api/v1/nodes/lv1/runtime', headers={**self.headers,
                'X-Node-Plane-Telegram-User-ID': '102'})
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(driver.calls, ['lv1'])

    def test_runtime_inspection_redacts_agent_failure(self):
        self.assertEqual(self.create().status_code, 201)

        class Unavailable(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.UNAVAILABLE

            def details(self):
                return 'secret agent address'

        class Driver:
            def inspect_node(self, node_key):
                raise Unavailable()

        with TestClient(create_app(self.db, node_driver=Driver())) as client:
            response = client.get('/api/v1/nodes/lv1/runtime', headers=self.admin_headers())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'node_agent_unavailable')
        self.assertNotIn('secret agent address', response.text)
