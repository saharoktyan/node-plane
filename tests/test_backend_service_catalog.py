"""The services in one shape: the catalog with access and system services, an account's service profiles."""
import unittest
from uuid import uuid4

from backend.authorization import PrincipalKind
from tests import test_backend_http as http_fixture


class ServiceCatalogTests(unittest.TestCase):
    setUp = http_fixture.BackendHTTPTests.setUp
    register = http_fixture.BackendHTTPTests.register
    provision = http_fixture.BackendHTTPTests.provision

    def bot(self, user_id):
        return {**self.headers, 'X-Node-Plane-Telegram-User-ID': str(user_id)}

    def admin_headers(self):
        from backend.workstation_cli import SCOPES
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, SCOPES, account_id=self.admin.id)
        return {'Authorization': 'Bearer ' + token}

    def test_an_account_without_access_sees_vpn_to_request(self):
        account = str(uuid4())
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_accounts(id, status) VALUES (?, 'approved')", (account,))
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, frozenset({'account.self.read'}), account_id=account)
        own = {'Authorization': 'Bearer ' + token}
        items = self.client.get('/api/v1/services', headers=own).json()['items']
        self.assertEqual(items, [{'service': 'vpn', 'kind': 'builtin', 'profiles': True,
                                  'instance_types': ['xray', 'awg'], 'access': 'requestable', 'state': None,
                                  'instances': None}])
        self.assertEqual(self.client.get('/api/v1/me/services', headers=own).json(), {'items': []})

    def test_a_vpn_profile_with_its_status_and_actions(self):
        self.register(202)
        profile = self.provision(202).json()['profile']['id']
        self.assertEqual(self.client.get('/api/v1/services', headers=self.bot(202)).json()['items'][0]['access'],
                         'active')
        items = self.client.get('/api/v1/me/services', headers=self.bot(202)).json()['items']
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['id'], items[0]['service'], items[0]['kind'], items[0]['status']),
                         (profile, 'vpn', 'builtin', 'active'))
        self.assertEqual(items[0]['actions'], ['devices', 'configs'])
        self.assertEqual(items[0]['summary']['devices'], 0)
        self.assertIsNone(items[0]['summary']['quota_limit_bytes'])
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_profiles SET frozen=1, expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                         (profile,))
        item = self.client.get('/api/v1/me/services', headers=self.bot(202)).json()['items'][0]
        self.assertEqual((item['status'], item['actions']), ('frozen', ['devices', 'renewal']))

    def test_a_vpn_profile_tells_its_open_quota_period(self):
        from backend.vpn_policies import VpnPolicies
        self.register(203)
        profile = self.provision(203).json()['profile']['id']
        VpnPolicies(self.db).initialize_schema()
        with self.db.transaction() as conn:
            conn.execute('''INSERT INTO backend_vpn_policies(id, subject_kind, profile_id, key, value_json, updated_at)
                VALUES ('q1', 'profile', ?, 'quota.bytes', ?, '2026-11-06T00:00:00+00:00')''', (profile, str(10 ** 11)))
        from backend.quotas import QuotaService
        QuotaService(self.db).scheduled()
        summary = self.client.get('/api/v1/me/services', headers=self.bot(203)).json()['items'][0]['summary']
        self.assertEqual((summary['quota_limit_bytes'], summary['quota_used_bytes']), (10 ** 11, 0))
        self.assertTrue(summary['quota_reset_at'])

    def test_staff_see_system_services_and_instances(self):
        items = {item['service']: item for item in
                 self.client.get('/api/v1/services', headers=self.admin_headers()).json()['items']}
        self.assertEqual(set(items), {'vpn', 'relay', 'telegram_bot'})
        self.assertEqual((items['vpn']['access'], items['vpn']['instances']), ('active', 0))
        self.assertEqual((items['relay']['kind'], items['relay']['state'], items['relay']['instances']),
                         ('system', 'off', 0))
        self.assertIn(items['telegram_bot']['state'], {'active', 'off'})
        self.assertIsNone(items['telegram_bot']['access'])


if __name__ == '__main__':
    unittest.main()
