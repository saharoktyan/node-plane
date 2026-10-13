import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from backend.http_api import create_app
from tests import test_backend_http as fixture
from tests import test_backend_config_issuance as issuance_fixture


class BackendOnboardingTests(unittest.TestCase):
    setUp = fixture.BackendHTTPTests.setUp

    def headers_for_admin(self):
        return {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}

    def test_empty_installation_and_accepting_defaults_are_real_operations(self):
        progress = self.client.get('/api/v1/system/onboarding', headers=self.headers_for_admin())
        self.assertEqual(progress.status_code, 200, progress.text)
        self.assertEqual(progress.json()['next_step'], 'defaults')
        self.assertTrue(all(not step['complete'] for step in progress.json()['steps']))
        accepted = self.client.post('/api/v1/system/onboarding/defaults', headers=self.headers_for_admin())
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()['next_step'], 'node')
        self.assertTrue(accepted.json()['steps'][0]['complete'])

    def test_resume_skips_applied_node_and_granted_profile_but_requires_issuance(self):
        profile = issuance_fixture.BackendConfigIssuanceTests.setup_ready_profile(self)
        driver = issuance_fixture.BackendConfigIssuanceTests.driver(self)
        headers = self.headers_for_admin()
        with TestClient(create_app(self.db, node_driver=driver)) as client:
            client.post('/api/v1/system/onboarding/defaults', headers=headers)
            progress = client.get('/api/v1/system/onboarding', headers=headers).json()
            self.assertEqual(progress['next_step'], 'config')
            self.assertEqual(progress['node_key'], 'n1')
            self.assertEqual(progress['profile_id'], profile)
            result = client.post(f'/api/v1/profiles/{profile}/config-issuances?wait=true',
                headers={**headers, 'Idempotency-Key': str(uuid4())},
                json={'node_key': 'n1', 'protocol': 'xray', 'transport': 'tcp'})
            self.assertEqual(result.json()['status'], 'succeeded', result.text)
            self.assertIsNone(client.get('/api/v1/system/onboarding', headers=headers).json()['next_step'])
            self.db.connection.execute("UPDATE backend_nodes SET desired_revision=2 WHERE key='n1'")
            self.db.connection.commit()
            self.assertEqual(client.get('/api/v1/system/onboarding', headers=headers).json()['next_step'], 'protocols')

    def test_done_refuses_incomplete_guide(self):
        for client in ('telegram', 'workstation'):
            response = self.client.post('/api/v1/system/onboarding/complete/' + client, headers=self.headers_for_admin())
            self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(self.client.get('/api/v1/me', headers=self.headers_for_admin()).json()['onboarding_completed'])

    def test_dismiss_is_permanent_even_without_nodes_and_after_restart(self):
        headers = self.headers_for_admin()
        for client in ('telegram', 'workstation'):
            result = self.client.post('/api/v1/system/onboarding/dismiss/' + client, headers=headers)
            self.assertEqual(result.status_code, 200, result.text)
        with TestClient(create_app(self.db)) as restarted:
            value = restarted.get('/api/v1/system/onboarding', headers=headers).json()
            self.assertTrue(value['completed'])
            self.assertTrue(value['workstation_completed'])
            self.assertNotIn('access', [step['key'] for step in value['steps']])
        self.assertEqual(self.client.post('/api/v1/system/onboarding/dismiss/other', headers=headers).status_code, 422)

    def check_protocol_completion(self, protocol, transport):
        profile = issuance_fixture.BackendConfigIssuanceTests.setup_ready_profile(self, protocol)
        driver = issuance_fixture.BackendConfigIssuanceTests.driver(self)
        headers = self.headers_for_admin()
        with TestClient(create_app(self.db, node_driver=driver)) as client:
            client.post('/api/v1/system/onboarding/defaults', headers=headers)
            workstation = client.post('/api/v1/system/onboarding/complete/workstation', headers=headers)
            self.assertEqual(workstation.status_code, 200, workstation.text)
            self.assertTrue(workstation.json()['workstation_completed'])
            self.assertFalse(workstation.json()['completed'])
            self.assertEqual(client.post('/api/v1/system/onboarding/complete/telegram', headers=headers).status_code, 409)
            response = client.post(f'/api/v1/profiles/{profile}/config-issuances?wait=true',
                headers={**headers, 'Idempotency-Key': str(uuid4())},
                json={'node_key':'n1', 'protocol': protocol, 'transport': transport})
            self.assertEqual(response.json()['status'], 'succeeded', response.text)
            # Completion is remembered without a Done callback, including
            # when setup later becomes incomplete after removal or revision changes.
            observed = client.get('/api/v1/system/onboarding', headers=headers).json()
            self.assertTrue(observed['completed'])
            self.assertTrue(observed['workstation_completed'])
            done = client.post('/api/v1/system/onboarding/complete/telegram', headers=headers)
            self.assertEqual(done.status_code, 200, done.text)
            self.assertTrue(done.json()['completed'])
            self.assertTrue(client.get('/api/v1/me', headers=headers).json()['onboarding_completed'])
            self.db.connection.execute("UPDATE backend_nodes SET desired_revision=2 WHERE key='n1'")
            self.db.connection.commit()
        with TestClient(create_app(self.db, node_driver=driver)) as restarted:
            self.assertTrue(restarted.get('/api/v1/system/onboarding', headers=headers).json()['completed'])
            self.assertEqual(restarted.post('/api/v1/system/onboarding/complete/telegram', headers=headers).status_code, 200)

    def test_vless_issuance_without_device_completes_telegram_guide(self):
        self.check_protocol_completion('xray', 'tcp')

    def test_awg_issuance_with_device_completes_telegram_guide(self):
        self.check_protocol_completion('awg', 'conf')
