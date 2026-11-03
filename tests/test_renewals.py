"""Renewal requests: the owner asks, an administrator extends or declines."""
from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from tests import test_connection_limits as fixture
from tests import test_tariffs


class RenewalTests(fixture.Fixture, unittest.TestCase):
    revision = test_tariffs.TariffTests.revision
    tariff = test_tariffs.TariffTests.tariff
    assign = test_tariffs.TariffTests.assign

    def setUp(self):
        super().setUp()
        created = self.client.post('/api/v1/accounts', headers=self.headers_for(), json={'identity': '987654'})
        self.assertEqual(created.status_code, 201, created.text)
        self.profile = created.json()['profile']['id']
        self.member = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '987654'}

    def ask(self, key=None):
        return self.client.post(f'/api/v1/me/profiles/{self.profile}/renewal',
                                headers={**self.member, 'Idempotency-Key': key or str(uuid4())})

    def approve(self, request_id, **body):
        return self.client.post(f'/api/v1/renewal-requests/{request_id}/approve',
                                headers=self.headers_for(self.revision()), json=body)

    def notifications(self):
        return [row[0] for row in self.db.connection.execute(
            'SELECT kind FROM backend_system_notifications ORDER BY rowid').fetchall()]

    def test_owner_asks_once_and_approval_extends_after_the_remaining_term(self):
        # Access without a term needs no renewal.
        self.assertEqual(self.ask().json()['error']['code'], 'renewal_not_needed')
        ends = datetime.now(timezone.utc) + timedelta(days=5)
        self.db.connection.execute('UPDATE backend_profiles SET expires_at=? WHERE id=?', (ends.isoformat(), self.profile))
        key = str(uuid4())
        first = self.ask(key)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(self.ask(key).json(), first.json())
        # A second request returns the pending one.
        self.assertEqual(self.ask().json()['id'], first.json()['id'])
        listed = self.client.get('/api/v1/renewal-requests', headers=self.headers_for()).json()
        self.assertEqual((listed['total'], listed['items'][0]['profile_revision']), (1, self.revision()))
        # Without a tariff term the administrator chooses the days.
        self.assertEqual(self.approve(first.json()['id']).json()['error']['code'], 'renewal_days_required')
        approved = self.approve(first.json()['id'], days=30)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(datetime.fromisoformat(approved.json()['profile']['expires_at']), ends + timedelta(days=30))
        self.assertIsNotNone(approved.json()['operation_id'])
        summary = self.client.get(f'/api/v1/me/profiles/{self.profile}/summary', headers=self.member).json()
        self.assertEqual((summary['renewal']['status'], summary['renewal']['days']), ('approved', 30))
        self.assertEqual(self.notifications(), ['renewal_approved'])
        self.assertEqual(self.approve(first.json()['id'], days=30).json()['error']['code'], 'renewal_decided')
        self.assertEqual(self.client.post(f"/api/v1/renewal-requests/{first.json()['id']}/reject",
                                          headers=self.headers_for()).json()['error']['code'], 'renewal_decided')

    def test_expired_access_renews_from_now_with_the_tariff_term_or_is_declined(self):
        self.db.connection.execute('UPDATE backend_profiles SET expires_at=? WHERE id=?',
            ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(), self.profile))
        tariff = self.tariff(duration_days=7)
        self.assertEqual(self.assign(tariff['id']).status_code, 200)
        request = self.ask().json()
        approved = self.approve(request['id'])
        self.assertEqual(approved.status_code, 200, approved.text)
        expires = datetime.fromisoformat(approved.json()['profile']['expires_at'])
        self.assertGreater(expires, datetime.now(timezone.utc) + timedelta(days=6))
        declined = self.ask().json()
        self.assertEqual(declined['status'], 'pending')
        rejected = self.client.post(f"/api/v1/renewal-requests/{declined['id']}/reject", headers=self.headers_for())
        self.assertEqual(rejected.json()['status'], 'rejected')
        self.assertEqual(self.notifications(), ['renewal_approved', 'renewal_rejected'])
        # Members cannot decide.
        other = self.ask().json()
        denied = self.client.post(f"/api/v1/renewal-requests/{other['id']}/reject", headers=self.member)
        self.assertEqual(denied.status_code, 403)
        # Another member cannot ask for this profile.
        self.client.post('/api/v1/accounts', headers=self.headers_for(), json={'identity': '555'})
        stranger = self.client.post(f'/api/v1/me/profiles/{self.profile}/renewal',
            headers={**self.headers, 'X-Node-Plane-Telegram-User-ID': '555', 'Idempotency-Key': str(uuid4())})
        self.assertEqual(stranger.status_code, 404)


if __name__ == '__main__':
    unittest.main()
