from __future__ import annotations

from uuid import uuid4
import unittest

from tests.test_backend_http import BackendHTTPTests


class BackendAccessRequestTests(unittest.TestCase):
    setUp = BackendHTTPTests.setUp
    register = BackendHTTPTests.register
    def member_headers(self, telegram_id):
        return {**self.headers, 'X-Node-Plane-Telegram-User-ID': str(telegram_id)}

    def request_access(self, telegram_id, key=None):
        return self.client.post('/api/v1/me/access-requests',
            headers={**self.member_headers(telegram_id), 'Idempotency-Key': key or str(uuid4())})

    def decision(self, request_id, decision, key=None):
        return self.client.post(f'/api/v1/access-requests/{request_id}/decision',
            headers={**self.member_headers(101), 'Idempotency-Key': key or str(uuid4())},
            json={'decision': decision})

    def test_decision_edits_all_registered_notices_and_refreshes_requester(self):
        account = self.register(102).json()
        request_id = self.request_access(102).json()['id']
        root = '/api/v1/integrations/telegram/access-decisions'
        for chat_id, message_id in ((101, 8), (103, 9)):
            response = self.client.post(f'{root}/{request_id}/messages', headers=self.headers,
                json={'chat_id': chat_id, 'message_id': message_id, 'locale': 'en'})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.client.post(root + '/claim', headers=self.headers).json()['delivery'])
        self.assertEqual(self.decision(request_id, 'approve').status_code, 200)
        seen = []
        for _ in range(2):
            item = self.client.post(root + '/claim', headers=self.headers).json()['delivery']
            self.assertEqual(item['decision'], 'approved')
            self.assertEqual(item['surface'], 'telegram')
            self.assertEqual(item['decider']['telegram_user_id'], 101)
            seen.append((item['chat_id'], item['message_id']))
            response = self.client.post(f"{root}/{request_id}/ack?chat_id={item['chat_id']}&message_id={item['message_id']}",
                headers=self.headers, json={})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(seen, [(101, 8), (103, 9)])
        self.assertIsNone(self.client.post(root + '/claim', headers=self.headers).json()['delivery'])
        refresh = self.client.post('/api/v1/integrations/telegram/menu-refresh/claim', headers=self.headers).json()['delivery']
        self.assertEqual(refresh['account']['id'], account['id'])
        self.assertEqual(refresh['account']['status'], 'approved')

    def test_notice_recipients_follow_roles_instead_of_bootstrap_env_ids(self):
        account = self.register(102, username='new_admin').json()
        details = self.client.get('/api/v1/accounts/' + account['id'], headers=self.member_headers(101)).json()
        root = '/api/v1/integrations/telegram/access-decisions/recipients'
        self.assertEqual(self.client.get(root, headers=self.headers).json()['items'], [101])
        response = self.client.patch('/api/v1/accounts/' + account['id'],
            headers={**self.member_headers(101), 'If-Match': f'"{details["revision"]}"', 'Idempotency-Key': str(uuid4())},
            json={'role': 'admin', 'status': 'approved'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get(root, headers=self.headers).json()['items'], [101, 102])

    def test_request_approval_is_atomic_and_does_not_grant_vpn_access(self):
        account = self.register(102).json()
        key = str(uuid4())
        created = self.request_access(102, key)
        self.assertEqual(created.status_code, 201, created.text)
        request_id = created.json()['id']
        self.assertEqual(self.request_access(102, key).json()['id'], request_id)
        self.assertEqual(self.request_access(102).json()['error']['code'], 'request_already_pending')
        self.assertEqual(self.client.get('/api/v1/access-requests', headers=self.member_headers(102)).status_code, 403)
        pending = self.client.get('/api/v1/access-requests', headers=self.member_headers(101)).json()['items']
        self.assertEqual([item['id'] for item in pending], [request_id])
        decision_key = str(uuid4())
        approved = self.decision(request_id, 'approve', decision_key)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(self.decision(request_id, 'approve', decision_key).status_code, 200)
        self.assertEqual(self.decision(request_id, 'reject').json()['error']['code'], 'request_already_decided')
        self.assertEqual(self.client.get('/api/v1/me', headers=self.member_headers(102)).json()['status'], 'approved')
        profiles = self.client.get('/api/v1/me/profiles', headers=self.member_headers(102)).json()['items']
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]['display_name'], 'User 102')
        self.assertEqual(self.client.get('/api/v1/me/nodes', headers=self.member_headers(102)).json()['items'], [])
        self.assertEqual(self.client.get('/api/v1/access-requests', headers=self.member_headers(101)).json()['items'], [])
        self.assertEqual(self.client.get('/api/v1/me/access-requests', headers=self.member_headers(102)).json()['items'][0]['account_id'], account['id'])

    def test_rejection_allows_new_request_but_old_decision_cannot_be_reopened(self):
        self.register(102)
        first = self.request_access(102).json()['id']
        self.assertEqual(self.decision(first, 'reject').status_code, 200)
        self.assertEqual(self.client.get('/api/v1/me', headers=self.member_headers(102)).json()['status'], 'rejected')
        second = self.request_access(102).json()['id']
        self.assertNotEqual(first, second)
        self.assertEqual(self.decision(first, 'approve').json()['error']['code'], 'request_already_decided')
        self.assertEqual(self.decision(second, 'approve').status_code, 200)

    def test_invalid_requests_do_not_mutate_account(self):
        self.register(102)
        self.assertEqual(self.request_access(101).json()['error']['code'], 'request_not_allowed')
        self.assertEqual(self.client.post('/api/v1/me/access-requests', headers={**self.member_headers(102),
            'Idempotency-Key': 'invalid'}).status_code, 422)
        request_id = self.request_access(102).json()['id']
        self.assertEqual(self.decision(request_id, 'grant').status_code, 422)
        self.assertEqual(self.client.get('/api/v1/me', headers=self.member_headers(102)).json()['status'], 'pending')
        self.assertEqual(self.client.post(f'/api/v1/access-requests/{request_id}/decision',
            headers={**self.member_headers(102), 'Idempotency-Key': str(uuid4())},
            json={'decision': 'approve'}).status_code, 403)
