from __future__ import annotations

from uuid import uuid4
import unittest

from tests.test_backend_http import BackendHTTPTests


class BackendAccessRequestTests(unittest.TestCase):
    setUp = BackendHTTPTests.setUp
    register = BackendHTTPTests.register
    provision = BackendHTTPTests.provision
    def member_headers(self, telegram_id):
        return {**self.headers, 'X-Node-Plane-Telegram-User-ID': str(telegram_id)}

    def request_access(self, telegram_id, key=None):
        return self.client.post('/api/v1/me/access-requests',
            headers={**self.member_headers(telegram_id), 'Idempotency-Key': key or str(uuid4())})

    def decision(self, request_id, decision, key=None):
        return self.client.post(f'/api/v1/access-requests/{request_id}/decision',
            headers={**self.member_headers(101), 'Idempotency-Key': key or str(uuid4())},
            json={'decision': decision})

    def test_workstation_creation_provisions_one_account_and_profile_and_retries_safely(self):
        headers={**self.member_headers(101),'Idempotency-Key':str(uuid4())}
        created=self.client.post('/api/v1/accounts',headers=headers,json={'identity':'987654'})
        self.assertEqual(created.status_code,201,created.text)
        result=created.json()
        self.assertTrue(result['created'])
        self.assertEqual(result['account']['telegram_user_id'],987654)
        self.assertEqual(result['account']['status'],'approved')
        self.assertEqual(result['profile']['owner_account_id'],result['account']['id'])
        self.assertEqual(self.client.post('/api/v1/accounts',headers=headers,json={'identity':'987654'}).json(),result)
        reused=self.client.post('/api/v1/accounts',headers={**headers,'Idempotency-Key':str(uuid4())},json={'identity':'987654'})
        self.assertEqual(reused.json()['profile']['id'],result['profile']['id'])
        self.assertFalse(reused.json()['created'])
        conflict=self.client.post('/api/v1/accounts',headers=headers,json={'identity':'987655'})
        self.assertEqual(conflict.status_code,409)
        self.assertEqual(self.client.post('/api/v1/accounts',headers=self.member_headers(101),json={'identity':'987654'}).status_code,422)

    def test_profile_provisioning_resolves_known_username_and_requires_id_for_unknown(self):
        account=self.register(102,username='known_user').json()
        self.assertEqual(account['id'], 'telegram:102')
        key=str(uuid4())
        response=self.client.post('/api/v1/accounts',headers={**self.member_headers(101),'Idempotency-Key':key},json={'identity':'@KNOWN_USER'})
        self.assertEqual(response.status_code,201,response.text)
        self.assertEqual(response.json()['account']['telegram_user_id'],102)
        self.assertEqual(response.json()['account']['username'],'known_user')
        response=self.client.post('/api/v1/accounts',headers={**self.member_headers(101),'Idempotency-Key':str(uuid4())},json={'identity':'@unknown_user'})
        self.assertEqual(response.status_code,422)
        self.assertEqual(response.json()['error']['code'],'telegram_id_required')
        self.register(104)
        self.assertEqual(self.client.post('/api/v1/accounts',headers={**self.member_headers(104),'Idempotency-Key':str(uuid4())},json={'identity':'987654'}).status_code,401)
        self.assertEqual(self.client.post('/api/v1/accounts',headers={**self.member_headers(102),'Idempotency-Key':str(uuid4())},json={'identity':'987654'}).status_code,403)

    def test_only_the_owner_can_rename_an_administrator_profile(self):
        from backend.profiles import ProfileRepository
        self.register(102, username='other_admin')
        account=self.provision(102).json()['account']
        self.db.connection.execute("UPDATE backend_accounts SET role='admin',status='approved' WHERE id=?",(account['id'],))
        self.db.connection.commit()
        profile=ProfileRepository(self.db).owned(account['id'],after='',limit=10)[0]
        headers={**self.member_headers(101),'Idempotency-Key':str(uuid4()),'If-Match':'"'+str(profile['desired_revision'])+'"'}
        denied=self.client.patch('/api/v1/profiles/'+profile['id'],headers=headers,json={'display_name':'Other name'})
        self.assertEqual(denied.status_code,403,denied.text)
        self.assertEqual(denied.json()['error']['code'],'admin_name_owner_only')
        allowed=self.client.patch('/api/v1/profiles/'+profile['id'],headers={**headers,**self.member_headers(102)},json={'display_name':'My name'})
        self.assertEqual(allowed.status_code,200,allowed.text)

    def test_decision_edits_all_registered_notices_and_refreshes_requester(self):
        self.register(102)
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
        self.assertEqual(refresh['account']['telegram_user_id'], 102)
        self.assertEqual(refresh['account']['status'], 'approved')

    def test_notice_recipients_follow_roles_instead_of_bootstrap_env_ids(self):
        self.register(102, username='new_admin')
        account = self.provision(102).json()['account']
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
        self.assertEqual(account['id'], 'telegram:102')
        key = str(uuid4())
        created = self.request_access(102, key)
        self.assertEqual(created.status_code, 201, created.text)
        request_id = created.json()['id']
        self.assertEqual(self.request_access(102, key).json()['id'], request_id)
        self.assertEqual(self.request_access(102).json()['error']['code'], 'request_already_pending')
        self.assertEqual(self.client.get('/api/v1/access-requests', headers=self.member_headers(102)).status_code, 401)
        pending = self.client.get('/api/v1/access-requests', headers=self.member_headers(101)).json()['items']
        self.assertEqual([item['id'] for item in pending], [request_id])
        decision_key = str(uuid4())
        approved = self.decision(request_id, 'approve', decision_key)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(self.decision(request_id, 'approve', decision_key).status_code, 200)
        self.assertEqual(self.decision(request_id, 'reject').json()['error']['code'], 'request_already_decided')
        me = self.client.get('/api/v1/me', headers=self.member_headers(102)).json()
        self.assertEqual(me['status'], 'approved')
        self.assertEqual(self.client.get('/api/v1/access-requests', headers=self.member_headers(102)).status_code, 403)
        profiles = self.client.get('/api/v1/me/profiles', headers=self.member_headers(102)).json()['items']
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]['display_name'], 'User 102')
        self.assertEqual(self.client.get('/api/v1/me/nodes', headers=self.member_headers(102)).json()['items'], [])
        self.assertEqual(self.client.get('/api/v1/access-requests', headers=self.member_headers(101)).json()['items'], [])
        self.assertEqual(self.client.get('/api/v1/me/access-requests', headers=self.member_headers(102)).json()['items'][0]['account_id'], me['id'])

    def test_rejection_allows_new_request_but_old_decision_cannot_be_reopened(self):
        self.register(102)
        first = self.request_access(102).json()['id']
        self.assertEqual(self.decision(first, 'reject').status_code, 200)
        me = self.client.get('/api/v1/me', headers=self.member_headers(102)).json()
        self.assertEqual(me['status'], 'pending')
        self.assertFalse(me['account_created'])
        self.assertEqual(self.client.get('/api/v1/me/access-requests', headers=self.member_headers(102)).json()['items'][0]['status'], 'rejected')
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
            json={'decision': 'approve'}).status_code, 401)
        self.provision(103)
        self.assertEqual(self.client.post(f'/api/v1/access-requests/{request_id}/decision',
            headers={**self.member_headers(103), 'Idempotency-Key': str(uuid4())},
            json={'decision': 'approve'}).status_code, 403)
