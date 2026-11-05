import unittest

from backend.profiles import ProfileRepository
from tests import test_backend_http


class ServiceProfileTests(unittest.TestCase):
    setUp = test_backend_http.BackendHTTPTests.setUp

    def rows(self):
        return [dict(row) for row in self.db.connection.execute(
            'SELECT id, account_id, service FROM backend_service_profiles ORDER BY id').fetchall()]

    def test_a_vpn_profile_is_a_service_profile_of_its_account(self):
        from backend.service_profiles import services
        account = self.identities.resolve_telegram(301)
        with self.db.connect() as conn:
            self.assertEqual(services(conn, account.id), [])
        profile = ProfileRepository(self.db).ensure_account_profile(account.id)
        self.assertEqual(self.rows(), [{'id': profile, 'account_id': account.id, 'service': 'vpn'}])
        with self.db.connect() as conn:
            self.assertEqual(services(conn, account.id), ['vpn'])
            self.assertTrue(ProfileRepository.has_vpn_profile(conn, account.id))
        self.db.connection.execute(
            "INSERT INTO backend_profile_deletions(profile_id, requested_at) VALUES (?, 'now')", (profile,))
        with self.db.connect() as conn:
            self.assertEqual(services(conn, account.id), [])

    def test_existing_vpn_profiles_get_their_service_profiles(self):
        from backend.service_profiles import rebuild
        account = self.identities.resolve_telegram(302)
        profile = ProfileRepository(self.db).ensure_account_profile(account.id)
        self.db.connection.execute('DELETE FROM backend_service_profiles')
        with self.db.transaction() as conn:
            rebuild(conn)
            rebuild(conn)
        self.assertEqual(self.rows(), [{'id': profile, 'account_id': account.id, 'service': 'vpn'}])
