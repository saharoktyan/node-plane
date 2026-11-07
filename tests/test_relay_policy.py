"""Through the web cabinet's relay the API grants no adapter powers and no
health or integration routes; the link marks every relayed request."""
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

    def test_integration_and_health_routes_are_closed(self):
        headers = self.account_headers()
        for method, path in (('POST', '/api/v1/integrations/telegram/alerts/claim'), ('GET', '/health/ready')):
            response = self.client.request(method, path, headers=headers)
            self.assertEqual(response.status_code, 403, path)
            self.assertEqual(response.json()['error']['code'], 'not_available_through_relay', path)

    def test_system_routes_are_open_to_staff(self):
        headers = self.account_headers()
        for path in ('/api/v1/system/recovery', '/api/v1/system/backups/catalog', '/api/v1/relay'):
            response = self.client.get(path, headers=headers)
            self.assertNotEqual(response.json().get('error', {}).get('code'), 'not_available_through_relay', path)

    def test_only_the_closed_routes_are_refused(self):
        from backend.relay_policy import refusal
        for path in ('/api/v1/system/version', '/api/v1/system/updates', '/api/v1/nodes/n1/drain', '/api/v1/relay',
                     '/api/v1/telegram-bot/placement', '/api/v1/system/cleanup/jobs', '/api/v1/profiles/p1/devices'):
            self.assertIsNone(refusal(path, {}, 'account'), path)
        for path in ('/api/v1/integrations/telegram/deliveries/wait', '/health/live'):
            self.assertEqual(refusal(path, {}, 'account'), 'not_available_through_relay', path)
        # The bot's signed requests are never refused; an adapter token always is.
        self.assertIsNone(refusal('/api/v1/integrations/x', {}, 'bot'))
        self.assertEqual(refusal('/api/v1/me', {}, 'adapter'), 'not_available_through_relay')
        # A Workstation linked through the relay has no session to confirm dangerous actions with.
        for path in ('/api/v1/system/updates', '/api/v1/relay', '/api/v1/nodes/n1/remove-step'):
            self.assertEqual(refusal(path, {}, 'workstation'), 'not_available_through_relay', path)
        self.assertIsNone(refusal('/api/v1/nodes', {}, 'workstation'))


if __name__ == '__main__':
    unittest.main()
