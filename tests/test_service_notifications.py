"""Service notification kinds, recipient modes and Inbox visibility."""
from datetime import datetime, timezone
import unittest
from uuid import uuid4

from backend import service_notifications
from backend.authorization import resolve_actor
from backend.service_notifications import Kind, queue
from tests import test_backend_announcements as fixture


class ServiceNotificationTests(unittest.TestCase):
    def setUp(self):
        fixture.AnnouncementTests.setUp(self)
        saved = dict(service_notifications.KINDS)
        self.addCleanup(lambda: (service_notifications.KINDS.clear(), service_notifications.KINDS.update(saved)))
        self.relevant = True
        service_notifications.KINDS['test_event'] = Kind(lambda conn, system, delivery: self.relevant,
                                                         lambda payload, title: f"Event {payload['count']}")

    headers_for = fixture.AnnouncementTests.headers_for

    def event(self, profile_id=None, dedup_key='test:1', count=2):
        with self.db.transaction() as conn:
            return queue(conn, kind='test_event', profile_id=profile_id or self.profile_id,
                         dedup_key=dedup_key, timestamp=datetime.now(timezone.utc), payload={'count': count})

    def prefer(self, mode, user=102):
        response = self.client.patch('/api/v1/me/preferences', headers=self.headers_for(user),
                                     json={'service_notifications': mode})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['service_notifications'], mode)

    def member_actor(self):
        return resolve_actor(self.transport, self.identities, telegram_user_id=102)

    def test_modes_control_sound_and_off_keeps_the_event_in_the_inbox(self):
        self.assertEqual(self.client.get('/api/v1/me', headers=self.headers_for(102)).json()['service_notifications'], 'on')
        # Announcement sound is independent of service notification sound.
        self.client.patch('/api/v1/me/preferences', headers=self.headers_for(102), json={'announcement_silent': True})
        for number, (mode, silent) in enumerate((('on', False), ('silent', True))):
            self.prefer(mode)
            self.event(dedup_key=f'test:{number}')
            key = str(uuid4())
            delivery = self.service.claim(self.transport, key)
            self.assertEqual((delivery['notification_kind'], delivery['sender_name'], delivery['silent']),
                             ('test_event', 'System', silent))
            self.assertEqual(delivery['payload'], {'count': 2})
            self.service.acknowledge(self.transport, delivery['id'], key, 'sent')
        self.prefer('off')
        announcement = self.event(dedup_key='test:off')
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))
        inbox = self.service.inbox(self.member_actor())
        self.assertEqual(inbox['total'], 3)
        muted = next(item for item in inbox['items'] if item['announcement_id'] == announcement)
        self.assertIsNone(muted['viewed_at'])
        self.assertEqual(self.service.inbox_item(self.member_actor(), muted['id'])['text'], 'Event 2')
        self.service.dismiss_inbox_item(self.member_actor(), muted['id'])
        self.assertEqual(self.service.inbox(self.member_actor())['total'], 2)
        # Muted events are not counted as administrator announcement jobs.
        self.assertIsNone(self.service.latest(self.actor)['last_job'])

    def test_dedup_key_admits_one_event_and_stale_events_are_skipped(self):
        first = self.event()
        self.assertIsNotNone(first)
        self.assertIsNone(self.event(count=3))
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_system_notifications').fetchone()[0], 1)
        self.relevant = False
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))
        self.assertEqual(self.db.connection.execute(
            'SELECT status FROM backend_announcement_deliveries WHERE announcement_id=?', (first,)).fetchone()[0], 'skipped')
        self.relevant = True
        # Off-mode events are still checked for relevance before reaching the Inbox.
        self.prefer('off')
        stale = self.event(dedup_key='test:stale')
        self.relevant = False
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))
        self.assertEqual(self.db.connection.execute(
            'SELECT status FROM backend_announcement_deliveries WHERE announcement_id=?', (stale,)).fetchone()[0], 'skipped')

    def test_administrators_receive_events_for_their_own_profiles(self):
        from backend.profiles import ProfileRepository
        profile = ProfileRepository(self.db).create_profile(runtime_name='admin_profile', display_name='Admin',
                                                            owner_account_id=self.admin.id)
        self.event(profile_id=profile, dedup_key='test:admin')
        delivery = self.service.claim(self.transport, str(uuid4()))
        self.assertEqual(delivery['telegram_user_id'], 101)

    def test_unknown_kinds_and_invalid_modes_are_rejected(self):
        self.event()
        service_notifications.KINDS.pop('test_event')
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))
        for body in ({'service_notifications': 'loud'}, {'service_notifications': None}):
            self.assertEqual(self.client.patch('/api/v1/me/preferences', headers=self.headers_for(102),
                                               json=body).status_code, 422)


if __name__ == '__main__':
    unittest.main()
