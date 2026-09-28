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
        self.assertEqual(self.client.get('/api/v1/me/profiles', headers=self.member_headers(102)).json()['items'], [])
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
