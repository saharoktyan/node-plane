from unittest import TestCase
from uuid import uuid4

from tests import test_backend_updates as fixture
from backend.recovery import overview


class RecoveryTests(TestCase):
    setUp = fixture.BackendUpdateTests.setUp
    service = fixture.BackendUpdateTests.service

    def test_queued_update_is_cancellable_but_uncertain_core_requires_evidence(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'stack', target_ref='v0.4.3-alpha.19', branch='dev')
        self.assertEqual(overview(self.db, self.actor)['items'][0]['actions'], ['cancel'])
        self.db.connection.execute("UPDATE backend_update_jobs SET status='blocked',result_json='{}'")
        self.assertEqual(overview(self.db, self.actor)['items'][0]['actions'], [])
        self.db.connection.execute("UPDATE backend_update_jobs SET result_json=?",
            ('{"phase":"core","unit_name":"test-update","secret":"never-show"}',))
        page = overview(self.db, self.actor)
        self.assertEqual(page['items'][0]['id'], job['id'])
        self.assertEqual(page['items'][0]['actions'], ['recheck'])
        self.assertNotIn('never-show', str(page))

    def test_pagination_handles_shrinking_list(self):
        self.service()
        for _ in range(12):
            identity = str(uuid4())
            self.db.connection.execute("INSERT INTO backend_update_jobs(id,actor_id,command_key,kind,intent_json,status,created_at) VALUES (?,?,?,'agents','{}','blocked','now')",
                (identity, self.admin.id, str(uuid4())))
        page = overview(self.db, self.actor, 10)
        self.assertEqual((page['total'], len(page['items']), page['offset']), (12, 2, 10))
        self.db.connection.execute("UPDATE backend_update_jobs SET status='succeeded'")
        page = overview(self.db, self.actor, 10)
        self.assertEqual((page['total'], page['offset'], page['items']), (0, 0, []))

    def test_started_child_prevents_cancel_even_if_parent_still_queued(self):
        service = self.service()
        job = service.queue(self.actor, str(uuid4()), 'stack', target_ref='v0.4.3-alpha.19', branch='dev')
        self.db.connection.execute("INSERT INTO backend_update_items VALUES (?,'node','{}','awaiting_executor',?,NULL)",
            (job['id'], str(uuid4())))
        self.assertEqual(overview(self.db, self.actor)['items'][0]['actions'], [])

    def test_another_update_gate_does_not_offer_recovery_for_unrelated_job(self):
        service = self.service()
        service.queue(self.actor, str(uuid4()), 'stack', target_ref='v0.4.3-alpha.19', branch='dev')
        self.db.connection.execute('UPDATE backend_controller_update_gate SET job_id=?', (str(uuid4()),))
        page = overview(self.db, self.actor)
        self.assertTrue(page['maintenance_active'])
        self.assertEqual(page['items'][0]['actions'], [])

    def test_inventory_requires_authorized_admin_and_includes_node_removal(self):
        self.service()
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}
        self.assertEqual(self.client.get('/api/v1/system/recovery').status_code, 401)
        self.register = lambda: self.client.post('/api/v1/integrations/telegram/identities/resolve',
            headers={**self.headers, 'Idempotency-Key': str(uuid4())}, json={'telegram_user_id': 202})
        self.register()
        self.assertEqual(self.client.get('/api/v1/system/recovery', headers={**self.headers,
            'X-Node-Plane-Telegram-User-ID': '202'}).status_code, 403)
        self.db.connection.execute("INSERT INTO backend_node_removals VALUES ('old-node',?,'blocked','node_cleanup_unavailable')", (self.admin.id,))
        response = self.client.get('/api/v1/system/recovery', headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['items'][0]['kind'], 'removal')
        self.assertEqual(response.json()['items'][0]['actions'], [])
