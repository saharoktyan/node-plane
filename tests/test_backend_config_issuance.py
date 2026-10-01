import json
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.authorization import Actor, Principal, PrincipalKind
from backend.config_issuance import ConfigIssuanceService
from backend.http_api import create_app
from backend.operations import OperationRepository
from backend.profiles import ProfileRepository
from tests.test_backend_http import BackendHTTPTests


class BackendConfigIssuanceTests(unittest.TestCase):
    setUp = BackendHTTPTests.setUp

    def setup_ready_profile(self, protocol='xray'):
        profile_id = ProfileRepository(self.db).create_profile(runtime_name='alice',
            display_name='Alice', owner_account_id=self.admin.id)
        settings = {'public_host': 'node.example', 'awg_port': 51820,
                    'xray_sni': 'www.cloudflare.com',
                    'xray_tcp_port': 443, 'xray_xhttp_port': 8443, 'xray_xhttp_path': '/assets'}
        with self.db.transaction() as conn:
            conn.execute('''INSERT INTO backend_nodes (key, title, region, enabled,
                protocols_json, xray_transports_json, desired_revision, applied_revision, settings_json)
                VALUES (?, ?, ?, 1, ?, ?, 1, 1, ?)''',
                ('n1', 'Latvia #1', 'EU', json.dumps([protocol]), '["tcp","xhttp"]', json.dumps(settings)))
            conn.execute('INSERT INTO backend_grants(profile_id, node_key, protocol) VALUES (?, ?, ?)',
                         (profile_id, 'n1', protocol))
            actor = Actor(Principal('test', PrincipalKind.ACCOUNT,
                frozenset({'profiles.manage'}), self.admin.id), self.admin)
            OperationRepository.record(conn, actor, profile_id, set())
            conn.execute("UPDATE backend_operation_tasks SET status = 'succeeded', result_json = ?",
                (json.dumps({'wg_conf': '[Interface]\nPrivateKey = private\n'}) if protocol == 'awg' else None,))
            conn.execute("UPDATE backend_operations SET status = 'succeeded'")
        return profile_id

    def driver(self):
        class Driver:
            def inspect_node(self, node_key):
                return {'node_key': node_key, 'health_state': 'running',
                        'xray_config_present': True, 'awg_config_present': True}

            def inspect(self, intent):
                if intent['protocol'] == 'xray':
                    assert intent['xray']['short_id'] and intent['xray']['uuid']
                return {'disk_present': True, 'live_present': True,
                        'identity_matches': True, 'config_available': True}

            def read_xray_public(self, node_key):
                return {'sni': 'www.cloudflare.com', 'public_key': 'a' * 43,
                        'short_id': '0123456789abcdef', 'tcp_port': 443,
                        'xhttp_port': 8443, 'xhttp_path': '/assets',
                        'flow': 'xtls-rprx-vision', 'fingerprint': 'chrome'}

            def refresh_awg_config(self, node_key, wg_conf, profile_name):
                assert wg_conf.startswith('[Interface]')
                return {'wg_conf': '[Interface]\nPrivateKey = private\n\n[Peer]\nEndpoint = node.example:51820\n',
                        'vpn_key': 'vpn://encoded'}
        return Driver()

    def test_issuance_uses_confirmed_live_identity_and_rechecks_artifact(self):
        profile_id = self.setup_ready_profile()
        driver = self.driver()
        service = ConfigIssuanceService(self.db, driver)
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101',
                   'Idempotency-Key': str(uuid4())}
        with TestClient(create_app(self.db, node_driver=driver)) as client:
            path = f'/api/v1/profiles/{profile_id}/config-issuances'
            body = {'node_key': 'n1', 'protocol': 'xray', 'transport': 'xhttp'}
            queued = client.post(path, headers=headers, json=body)
            self.assertEqual(queued.status_code, 202, queued.text)
            self.assertEqual(client.post(path, headers=headers, json=body).json(), queued.json())
            task_id = queued.json()['id']
            self.assertEqual(client.get(f'/api/v1/config-issuances/{task_id}/artifact',
                headers=headers).status_code, 409)
            self.assertTrue(service.run_one())
            self.assertEqual(client.get(f'/api/v1/config-issuances/{task_id}',
                headers=headers).json()['status'], 'succeeded')
            artifact = client.get(f'/api/v1/config-issuances/{task_id}/artifact', headers=headers)
            self.assertEqual(artifact.status_code, 200, artifact.text)
            self.assertEqual(artifact.json()['filename'],
                             'VLESS - Latvia #1 - Alice - XHTTP.txt')
            self.assertIn('pbk=' + 'a' * 43, artifact.json()['content'])
            self.assertIn('type=xhttp', artifact.json()['content'])
            self.assertIn('Latvia%20%231', artifact.json()['content'])
            self.assertEqual(artifact.headers['Cache-Control'], 'no-store')
            self.db.connection.execute('UPDATE backend_nodes SET desired_revision = 2 WHERE key = ?', ('n1',))
            stale = client.get(f'/api/v1/config-issuances/{task_id}/artifact', headers=headers)
            self.assertNotEqual(stale.status_code, 200)

    def test_revoked_grant_blocks_existing_artifact(self):
        profile_id = self.setup_ready_profile()
        driver = self.driver()
        service = ConfigIssuanceService(self.db, driver)
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101',
                   'Idempotency-Key': str(uuid4())}
        with TestClient(create_app(self.db, node_driver=driver)) as client:
            queued = client.post(f'/api/v1/profiles/{profile_id}/config-issuances', headers=headers,
                json={'node_key': 'n1', 'protocol': 'xray', 'transport': 'tcp'})
            self.assertEqual(queued.status_code, 202, queued.text)
            self.assertTrue(service.run_one())
            task_id = queued.json()['id']
            self.db.connection.execute('DELETE FROM backend_grants WHERE profile_id = ?', (profile_id,))
            response = client.get(f'/api/v1/config-issuances/{task_id}/artifact', headers=headers)
            self.assertEqual(response.status_code, 403)
            self.assertNotIn('vless://', response.text)

    def test_awg_vpn_and_conf_are_refreshed_and_revoked(self):
        profile_id = self.setup_ready_profile('awg')
        driver = self.driver()
        service = ConfigIssuanceService(self.db, driver)
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}
        with TestClient(create_app(self.db, node_driver=driver)) as client:
            for extension in ('vpn', 'conf'):
                queued = client.post(f'/api/v1/profiles/{profile_id}/config-issuances',
                    headers={**headers, 'Idempotency-Key': str(uuid4())},
                    json={'node_key': 'n1', 'protocol': 'awg', 'transport': extension})
                self.assertEqual(queued.status_code, 202, queued.text)
                self.assertTrue(service.run_one())
                artifact = client.get(f'/api/v1/config-issuances/{queued.json()["id"]}/artifact',
                    headers=headers)
                self.assertEqual(artifact.status_code, 200, artifact.text)
                self.assertEqual(artifact.json()['filename'],
                                 f'AmneziaWG - Latvia #1 - Alice.{extension}')
                self.assertIn('vpn://' if extension == 'vpn' else '[Interface]',
                    artifact.json()['content'])
            self.db.connection.execute('DELETE FROM backend_grants WHERE profile_id = ?', (profile_id,))
            denied = client.get(f'/api/v1/config-issuances/{queued.json()["id"]}/artifact',
                headers=headers)
            self.assertEqual(denied.status_code, 403)
