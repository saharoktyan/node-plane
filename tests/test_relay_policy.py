"""Through the web cabinet's relay the API grants no adapter powers and no
system operations; the link marks every relayed request."""
import unittest

from backend.authorization import PrincipalKind
from tests import test_backend_http


class RelayPolicyTests(unittest.TestCase):
    setUp = test_backend_http.BackendHTTPTests.setUp

    def account_headers(self):
        from backend.workstation_cli import SCOPES
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, SCOPES, account_id=self.admin.id)
        return {'Authorization': 'Bearer ' + token, 'X-Node-Plane-Via': 'relay'}

    def test_an_account_session_reaches_its_own_and_admin_routes(self):
        headers = self.account_headers()
        self.assertEqual(self.client.get('/api/v1/me', headers=headers).status_code, 200)
        self.assertEqual(self.client.get('/api/v1/nodes', headers=headers).status_code, 200)

    def test_adapter_credentials_and_delegation_are_refused(self):
        relayed = {**self.headers, 'X-Node-Plane-Via': 'relay'}
        response = self.client.get('/api/v1/me', headers={**relayed, 'X-Node-Plane-Telegram-User-ID': '101'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'not_available_through_relay')
        self.assertEqual(self.client.get('/api/v1/system/version', headers=relayed).status_code, 403)
        # The same request on the controller host still works.
        self.assertEqual(self.client.get('/api/v1/me', headers={
            **self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}).status_code, 200)

    def test_integration_and_system_routes_are_closed(self):
        headers = self.account_headers()
        for method, path in (('GET', '/api/v1/system/updates'), ('POST', '/api/v1/system/backups/jobs'),
                             ('GET', '/api/v1/system/recovery'), ('POST', '/api/v1/nodes/n1/agent-rollouts'),
                             ('POST', '/api/v1/nodes/n1/bootstrap'), ('GET', '/api/v1/agent-rollouts/x'),
                             ('POST', '/api/v1/integrations/telegram/alerts/claim'), ('GET', '/health/ready')):
            response = self.client.request(method, path, headers=headers)
            self.assertEqual(response.status_code, 403, path)
            self.assertEqual(response.json()['error']['code'], 'not_available_through_relay', path)

    def test_only_the_closed_routes_are_refused(self):
        from backend.relay_policy import refusal
        for path in ('/api/v1/system/version', '/api/v1/system/updates-history', '/api/v1/nodes/n1/overview',
                     '/api/v1/nodes/drain', '/api/v1/profiles/p1/devices', '/api/v1/system/traffic'):
            self.assertIsNone(refusal(path, {}, 'account'), path)
        self.assertEqual(refusal('/api/v1/nodes/n1/drain', {}, 'account'), 'not_available_through_relay')
        # The relay is not configured through itself.
        self.assertEqual(refusal('/api/v1/relay', {}, 'account'), 'not_available_through_relay')
