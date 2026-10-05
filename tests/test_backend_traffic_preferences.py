"""Administrator-only traffic policy regression tests."""
import unittest

from tests import test_backend_http as fixture


class TrafficPreferencesTests(unittest.TestCase):
    def setUp(self):
        fixture.BackendHTTPTests.setUp(self)
        self.member = fixture.BackendHTTPTests.register(self, 102).json()
        self.db.connection.execute("UPDATE backend_accounts SET status='approved' WHERE id=?", (self.member['id'],))
        self.db.connection.commit()

    def headers_for(self, uid):
        return {**self.headers, 'X-Node-Plane-Telegram-User-ID': str(uid)}

    def test_default_off_and_global_enable_applies_to_members(self):
        member = self.client.get('/api/v1/me', headers=self.headers_for(102)).json()
        self.assertNotIn('traffic_consent', member)
        self.assertFalse(member['traffic_available'])
        result = self.client.patch('/api/v1/system/traffic/preferences',
            headers=self.headers_for(101), json={'enabled': True})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['collection_status'], 'ready')
        member = self.client.get('/api/v1/me', headers=self.headers_for(102)).json()
        self.assertTrue(member['traffic_available'])
        self.assertNotIn('traffic_consent', member)

    def test_member_consent_input_is_no_longer_supported(self):
        for value in (True, False):
            result = self.client.patch('/api/v1/me/preferences',
                headers=self.headers_for(102), json={'traffic_consent': value})
            self.assertEqual(result.status_code, 422)

    def test_schema_initialization_removes_obsolete_member_preferences(self):
        from backend.system_settings import SystemSettingsService
        for key in ('traffic_consent:' + self.member['id'], 'traffic_consent_generation:' + self.member['id']):
            self.db.connection.execute('INSERT INTO backend_system_settings VALUES (?,?)', (key, 'false'))
        self.db.connection.commit()
        SystemSettingsService(self.db).initialize_schema()
        rows = self.db.connection.execute("SELECT key FROM backend_system_settings WHERE key LIKE 'traffic_consent%'").fetchall()
        self.assertEqual(rows, [])

    def test_member_cannot_change_policy_and_payload_is_strict(self):
        self.assertEqual(self.client.patch('/api/v1/system/traffic/preferences',
            headers=self.headers_for(102), json={'enabled': True}).status_code, 403)
        for payload in ({}, {'enabled': 'yes'}, {'enabled': None}, {'enabled': True, 'account_id': self.member['id']}):
            self.assertEqual(self.client.patch('/api/v1/system/traffic/preferences',
                headers=self.headers_for(101), json=payload).status_code, 422)
        for payload in ({'traffic_consent': 1}, {'traffic_consent': None}, {'traffic_consent': True, 'account_id': self.member['id']}):
            self.assertEqual(self.client.patch('/api/v1/me/preferences',
                headers=self.headers_for(102), json=payload).status_code, 422)
