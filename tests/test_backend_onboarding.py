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
