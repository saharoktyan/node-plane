from __future__ import annotations

from uuid import uuid4
import unittest

from tests.test_backend_identity import Database
from backend.admin_cli import bootstrap_admin
from backend.authorization import PrincipalKind
from backend.credentials import CredentialService, ADAPTER_SCOPES
from backend.identity_repository import SQLIdentityRepository
from backend.profiles import ProfileRepository
from backend.profile_commands import ProfileCommands
from backend.http_api import create_app
from backend.access_requests import AccessRequestService
from backend.accounts import AccountService
from backend.nodes import NodeService
from backend.node_settings import NodeSettingsService
from backend.config_issuance import ConfigIssuanceService
from backend.agent_rollout import AgentRolloutService
from backend.temporary_configs import TemporaryConfigService
from db.migrations import LEDGER_DDL, REVISIONS
from fastapi.testclient import TestClient


class PreapprovalVisitorTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.addCleanup(self.db.connection.close)
        self.identities = SQLIdentityRepository(self.db)
        self.identities.initialize_schema()
        self.credentials = CredentialService(self.db)
        self.credentials.initialize_schema()
        ProfileRepository(self.db).initialize_schema()
        ProfileCommands(self.db).initialize_schema()
        AccessRequestService(self.db).initialize_schema()
        AccountService(self.db).initialize_schema()
        NodeService(self.db).initialize_schema()
        NodeSettingsService(self.db).initialize_schema()
        ConfigIssuanceService(self.db).initialize_schema()
        AgentRolloutService(self.db).initialize_schema()
        TemporaryConfigService(self.db).initialize_schema()
        with self.db.transaction() as conn:
            conn.execute(LEDGER_DDL)
            for revision in REVISIONS:
                conn.execute('INSERT INTO backend_schema_revisions VALUES (?,?,?,?,?)',
                    (revision.number, revision.name, revision.checksum, revision.minimum_reader, '2026-10-09'))
        self.admin = bootstrap_admin(self.identities, 101)
        self.token_id, self.token = self.credentials.issue(PrincipalKind.ADAPTER, ADAPTER_SCOPES)
        self.headers = {'Authorization': 'Bearer ' + self.token}
        self.client = TestClient(create_app(self.db))
        self.addCleanup(self.client.close)

    def headers_for(self, telegram_id=None):
        h = dict(self.headers)
        if telegram_id is not None:
            h['X-Node-Plane-Telegram-User-ID'] = str(telegram_id)
        return h

    def register(self, user_id, key=None, **fields):
        return self.client.post('/api/v1/integrations/telegram/identities/resolve',
            headers={**self.headers, 'Idempotency-Key': key or str(uuid4())},
            json={'telegram_user_id': user_id, **fields})

    def provision(self, identity, key=None):
        from backend.workstation_cli import SCOPES
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, SCOPES, account_id=self.admin.id)
        return self.client.post('/api/v1/accounts',
            headers={'Authorization': 'Bearer ' + token, 'Idempotency-Key': key or str(uuid4())},
            json={'identity': str(identity)})

    def test_real_http_no_account_profile_growth_during_visitor_lifecycle(self):
        def get_counts():
            with self.db.connect() as conn:
                a = conn.execute("SELECT COUNT(*) FROM backend_accounts WHERE id != ?", (self.admin.id,)).fetchone()[0]
                p = conn.execute("SELECT COUNT(*) FROM backend_profiles WHERE owner_account_id != ?", (self.admin.id,)).fetchone()[0]
                i = conn.execute("SELECT COUNT(*) FROM backend_external_identities WHERE account_id != ?", (self.admin.id,)).fetchone()[0]
            return a, p, i

        self.assertEqual(get_counts(), (0, 0, 0))

        # 1. Initial observe
        res = self.register(201, username='guest_user', language_code='en')
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body['id'], 'telegram:201')
        self.assertEqual(body['role'], 'member')
        self.assertEqual(body['status'], 'pending')
        self.assertFalse(body['account_created'])
        self.assertEqual(get_counts(), (0, 0, 0))

        me0 = self.client.get('/api/v1/me', headers=self.headers_for(201)).json()
        self.assertEqual(me0['locale'], 'en')
        self.assertFalse(me0['locale_selected'])
        self.assertFalse(me0['account_created'])

        # 2. Repeated observe
        res2 = self.register(201, username='guest_updated', language_code='ru')
        self.assertEqual(res2.status_code, 200, res2.text)
        self.assertFalse(res2.json()['account_created'])
        self.assertEqual(res2.json()['id'], 'telegram:201')
        self.assertEqual(get_counts(), (0, 0, 0))

        # 3. Locale selection
        res_loc = self.client.patch('/api/v1/me/preferences', headers=self.headers_for(201), json={'locale': 'ru'})
        self.assertEqual(res_loc.status_code, 200, res_loc.text)
        self.assertEqual(res_loc.json()['locale'], 'ru')
        self.assertTrue(res_loc.json()['locale_selected'])
        self.assertFalse(res_loc.json()['account_created'])
        self.assertEqual(get_counts(), (0, 0, 0))

        # 4. Access request submission
        req_key = str(uuid4())
        res_req = self.client.post('/api/v1/me/access-requests',
            headers={**self.headers_for(201), 'Idempotency-Key': req_key})
        self.assertEqual(res_req.status_code, 201, res_req.text)
        req_body = res_req.json()
        self.assertEqual(req_body['status'], 'pending')
        self.assertIsNone(req_body['account_id'])
        self.assertEqual(get_counts(), (0, 0, 0))

        # Check pending list as admin
        pending = self.client.get('/api/v1/access-requests', headers=self.headers_for(101)).json()['items']
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['telegram_user_id'], 201)
        self.assertEqual(pending[0]['id'], req_body['id'])

        # 5. Access request rejection
        res_rej = self.client.post(f"/api/v1/access-requests/{req_body['id']}/decision",
            headers={**self.headers_for(101), 'Idempotency-Key': str(uuid4())}, json={'decision': 'reject'})
        self.assertEqual(res_rej.status_code, 200, res_rej.text)
        self.assertEqual(res_rej.json()['status'], 'rejected')
        self.assertIsNone(res_rej.json()['account_id'])
        self.assertEqual(get_counts(), (0, 0, 0))

        # Check me remains visitor
        me = self.client.get('/api/v1/me', headers=self.headers_for(201)).json()
        self.assertEqual(me['id'], 'telegram:201')
        self.assertFalse(me['account_created'])
        self.assertEqual(me['status'], 'pending')
        self.assertEqual(get_counts(), (0, 0, 0))

    def test_denial_of_guest_privileged_endpoints_spoofed_account_and_delegation(self):
        self.register(202)
        # Privileged endpoints requiring strict Actor return 401 for guests
        for path in ('/api/v1/profiles', '/api/v1/accounts', '/api/v1/nodes',
                     '/api/v1/regions', '/api/v1/me/profiles', '/api/v1/me/nodes'):
            resp = self.client.get(path, headers=self.headers_for(202))
            self.assertEqual(resp.status_code, 401, f"{path} should be 401 for guest")
            self.assertEqual(resp.json()['error']['code'], 'identity_not_registered')

        # Guest cannot toggle announcements (only real accounts)
        silent_attempt = self.client.patch('/api/v1/me/preferences',
            headers=self.headers_for(202), json={'announcement_silent': True})
        self.assertEqual(silent_attempt.status_code, 403)
        self.assertEqual(silent_attempt.json()['error']['code'], 'permission_denied')

        # Account token cannot delegate telegram user header or call resolve
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, frozenset({'account.self.read'}), account_id=self.admin.id)
        acc_headers = {'Authorization': 'Bearer ' + token, 'X-Node-Plane-Telegram-User-ID': '202'}
        self.assertEqual(self.client.get('/api/v1/me', headers=acc_headers).status_code, 403)
        self.assertEqual(self.client.post('/api/v1/integrations/telegram/identities/resolve',
            headers={'Authorization': 'Bearer ' + token, 'Idempotency-Key': str(uuid4())},
            json={'telegram_user_id': 202}).status_code, 403)
        # Cannot pass delegation header to resolve
        self.assertEqual(self.client.post('/api/v1/integrations/telegram/identities/resolve',
            headers={**self.headers_for(202), 'Idempotency-Key': str(uuid4())},
            json={'telegram_user_id': 202}).status_code, 422)

        # Adapter token missing delegate.telegram cannot act as delegate
        _, narrow_token = self.credentials.issue(PrincipalKind.ADAPTER, frozenset({'account.self.read'}))
        narrow_headers = {'Authorization': 'Bearer ' + narrow_token, 'X-Node-Plane-Telegram-User-ID': '202'}
        self.assertEqual(self.client.get('/api/v1/me', headers=narrow_headers).status_code, 403)

        # Failed registration scope checks must precede metadata writes.
        _, registration_only = self.credentials.issue(PrincipalKind.ADAPTER, frozenset({'identity.telegram.resolve'}))
        denied_registration = self.client.post('/api/v1/integrations/telegram/identities/resolve',
            headers={'Authorization': 'Bearer ' + registration_only, 'Idempotency-Key': str(uuid4())},
            json={'telegram_user_id': 210})
        self.assertEqual(denied_registration.status_code, 403)
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT subject FROM backend_telegram_identity_details WHERE subject='210'").fetchone())

    def test_disabled_real_account_cannot_fallback(self):
        prov = self.provision(203).json()
        account_id = prov['account']['id']
        rev = prov['account']['revision']

        # Admin disables the account
        res_dis = self.client.patch(f"/api/v1/accounts/{account_id}",
            headers={**self.headers_for(101), 'If-Match': f'"{rev}"', 'Idempotency-Key': str(uuid4())},
            json={'status': 'disabled'})
        self.assertEqual(res_dis.status_code, 200, res_dis.text)
        self.assertEqual(res_dis.json()['status'], 'disabled')

        # Observe returns disabled account, not visitor
        obs = self.register(203).json()
        self.assertEqual(obs['id'], account_id)
        self.assertEqual(obs['status'], 'disabled')

        # me denied with account_disabled, cannot fall back to visitor
        me_resp = self.client.get('/api/v1/me', headers=self.headers_for(203))
        self.assertEqual(me_resp.status_code, 403)
        self.assertEqual(me_resp.json()['error']['code'], 'account_disabled')

        # access request submission denied
        req_resp = self.client.post('/api/v1/me/access-requests',
            headers={**self.headers_for(203), 'Idempotency-Key': str(uuid4())})
        self.assertEqual(req_resp.status_code, 403)
        self.assertEqual(req_resp.json()['error']['code'], 'account_disabled')

    def test_request_scopes_and_disabled_request_policy(self):
        self.register(204)
        pol = self.client.get('/api/v1/system/access-requests', headers=self.headers_for(204))
        self.assertEqual(pol.status_code, 200, pol.text)
        self.assertTrue(pol.json()['enabled'])
        self.assertIsNone(pol.json()['notify_requests'])

        # Admin disables access requests
        patch_pol = self.client.patch('/api/v1/system/access-requests',
            headers=self.headers_for(101),
            json={'enabled': False, 'gate_message': 'Gate closed', 'welcome_message': 'Closed welcome'})
        self.assertEqual(patch_pol.status_code, 200, patch_pol.text)

        # Visitor sees disabled policy
        pol2 = self.client.get('/api/v1/system/access-requests', headers=self.headers_for(204)).json()
        self.assertFalse(pol2['enabled'])
        self.assertEqual(pol2['gate_message'], 'Gate closed')
        self.assertEqual(pol2['welcome_message'], 'Closed welcome')

        # Submitting request while policy is disabled is rejected
        res = self.client.post('/api/v1/me/access-requests',
            headers={**self.headers_for(204), 'Idempotency-Key': str(uuid4())})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()['error']['code'], 'access_requests_disabled')

        # Visitor scopes strictly restricted
        me = self.client.get('/api/v1/me', headers=self.headers_for(204)).json()
        expected_scopes = {'account.self.read', 'account.self.preferences.write',
                           'access_requests.self.read', 'access_requests.self.create'}
        self.assertEqual(set(me['permissions']), expected_scopes)

    def test_approve_and_replay_exactly_once(self):
        self.register(205)
        req = self.client.post('/api/v1/me/access-requests',
            headers={**self.headers_for(205), 'Idempotency-Key': str(uuid4())}).json()
        req_id = req['id']

        dec_key = str(uuid4())
        app1 = self.client.post(f'/api/v1/access-requests/{req_id}/decision',
            headers={**self.headers_for(101), 'Idempotency-Key': dec_key}, json={'decision': 'approve'})
        self.assertEqual(app1.status_code, 200, app1.text)
        app1_data = app1.json()
        self.assertEqual(app1_data['status'], 'approved')
        account_id = app1_data['account_id']
        self.assertIsNotNone(account_id)
        self.assertNotEqual(account_id, 'telegram:205')

        # Verify DB state atomically created
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM backend_accounts WHERE id=?", (account_id,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM backend_profiles WHERE owner_account_id=?", (account_id,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM backend_external_identities WHERE subject='205' AND account_id=?", (account_id,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM backend_telegram_menu_refresh WHERE subject='205'").fetchone()[0], 0)

        # Replay same decision key returns identical result without duplicates
        app2 = self.client.post(f'/api/v1/access-requests/{req_id}/decision',
            headers={**self.headers_for(101), 'Idempotency-Key': dec_key}, json={'decision': 'approve'})
        self.assertEqual(app2.status_code, 200, app2.text)
        self.assertEqual(app2.json(), app1_data)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM backend_accounts WHERE id=?", (account_id,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM backend_profiles WHERE owner_account_id=?", (account_id,)).fetchone()[0], 1)

        # New key or different decision on decided request rejected with 409
        diff_key = self.client.post(f'/api/v1/access-requests/{req_id}/decision',
            headers={**self.headers_for(101), 'Idempotency-Key': str(uuid4())}, json={'decision': 'approve'})
        self.assertEqual(diff_key.status_code, 409)
        self.assertEqual(diff_key.json()['error']['code'], 'request_already_decided')

        diff_dec = self.client.post(f'/api/v1/access-requests/{req_id}/decision',
            headers={**self.headers_for(101), 'Idempotency-Key': str(uuid4())}, json={'decision': 'reject'})
        self.assertEqual(diff_dec.status_code, 409)
        self.assertEqual(diff_dec.json()['error']['code'], 'request_already_decided')

    def test_fresh_request_after_rejection(self):
        self.register(206)
        req1 = self.client.post('/api/v1/me/access-requests',
            headers={**self.headers_for(206), 'Idempotency-Key': str(uuid4())}).json()
        req1_id = req1['id']

        rej = self.client.post(f'/api/v1/access-requests/{req1_id}/decision',
            headers={**self.headers_for(101), 'Idempotency-Key': str(uuid4())}, json={'decision': 'reject'})
        self.assertEqual(rej.status_code, 200, rej.text)
        self.assertEqual(rej.json()['status'], 'rejected')

        # Submitting fresh request is allowed after rejection
        req2 = self.client.post('/api/v1/me/access-requests',
            headers={**self.headers_for(206), 'Idempotency-Key': str(uuid4())})
        self.assertEqual(req2.status_code, 201, req2.text)
        req2_id = req2.json()['id']
        self.assertNotEqual(req2_id, req1_id)
        self.assertEqual(req2.json()['status'], 'pending')

        # Old decision cannot be reopened
        reopen = self.client.post(f'/api/v1/access-requests/{req1_id}/decision',
            headers={**self.headers_for(101), 'Idempotency-Key': str(uuid4())}, json={'decision': 'approve'})
        self.assertEqual(reopen.status_code, 409)
        self.assertEqual(reopen.json()['error']['code'], 'request_already_decided')

        # New request can be approved
        app = self.client.post(f'/api/v1/access-requests/{req2_id}/decision',
            headers={**self.headers_for(101), 'Idempotency-Key': str(uuid4())}, json={'decision': 'approve'})
        self.assertEqual(app.status_code, 200, app.text)
        self.assertEqual(app.json()['status'], 'approved')
        self.assertIsNotNone(app.json()['account_id'])

    def test_manual_admin_post_accounts_consumes_pending_guest_request_without_duplicates(self):
        self.register(207, username='charlie')
        req = self.client.post('/api/v1/me/access-requests',
            headers={**self.headers_for(207), 'Idempotency-Key': str(uuid4())}).json()
        req_id = req['id']
        pending_before = self.client.get('/api/v1/access-requests', headers=self.headers_for(101)).json()['items']
        self.assertIn(req_id, [item['id'] for item in pending_before])

        prov_key = str(uuid4())
        prov = self.provision('@charlie', key=prov_key)
        self.assertEqual(prov.status_code, 201, prov.text)
        prov_data = prov.json()
        self.assertTrue(prov_data['created'])
        account_id = prov_data['account']['id']
        self.assertEqual(prov_data['account']['telegram_user_id'], 207)

        # Pending guest request was automatically approved
        pending_after = self.client.get('/api/v1/access-requests', headers=self.headers_for(101)).json()['items']
        self.assertNotIn(req_id, [item['id'] for item in pending_after])
        with self.db.connect() as conn:
            req_row = conn.execute("SELECT * FROM backend_access_requests WHERE id=?", (req_id,)).fetchone()
            self.assertEqual(req_row['status'], 'approved')
            self.assertEqual(req_row['account_id'], account_id)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM backend_access_requests WHERE telegram_subject='207'").fetchone()[0], 1)

        # Replay returns identical result
        prov_replay = self.provision('@charlie', key=prov_key)
        self.assertEqual(prov_replay.status_code, 201)
        self.assertEqual(prov_replay.json(), prov_data)

        # New key returns created False without duplicates
        prov_repeat = self.provision('@charlie')
        self.assertEqual(prov_repeat.status_code, 201)
        self.assertFalse(prov_repeat.json()['created'])
        self.assertEqual(prov_repeat.json()['account']['id'], account_id)

    def test_delete_then_background_menu_refresh_never_resolves_and_new_request_gets_new_identity(self):
        self.register(208)
        prov = self.provision(208).json()
        acc_1 = prov['account']['id']
        repo = ProfileRepository(self.db)
        prof_1 = repo.owned(acc_1, after='', limit=10)[0]['id']

        # Delete profile and finalize account deletion
        del_prof = self.client.delete(f'/api/v1/profiles/{prof_1}',
            headers={**self.headers_for(101), 'If-Match': '"1"', 'Idempotency-Key': str(uuid4())})
        self.assertEqual(del_prof.status_code, 200, del_prof.text)
        self.assertIsNone(self.identities.find_telegram_account(208))

        # Menu refresh delivers deleted account
        claim = self.client.post('/api/v1/integrations/telegram/menu-refresh/claim', headers=self.headers).json()['delivery']
        self.assertEqual(claim['account']['id'], acc_1)
        self.assertTrue(claim['account']['deleted'])

        # Background refresh executes me (never resolve) which returns visitor
        me_guest = self.client.get('/api/v1/me', headers=self.headers_for(208)).json()
        self.assertEqual(me_guest['id'], 'telegram:208')
        self.assertFalse(me_guest['account_created'])
        self.assertEqual(me_guest['status'], 'pending')
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT account_id FROM backend_external_identities WHERE subject='208'").fetchone())

        # Acknowledge deletion menu refresh
        self.client.post(f"/api/v1/integrations/telegram/menu-refresh/{acc_1}/ack?revision={claim['revision']}", headers=self.headers)

        # User explicitly submits new access request
        req = self.client.post('/api/v1/me/access-requests',
            headers={**self.headers_for(208), 'Idempotency-Key': str(uuid4())}).json()
        req_id = req['id']

        # Admin approves new request
        app = self.client.post(f'/api/v1/access-requests/{req_id}/decision',
            headers={**self.headers_for(101), 'Idempotency-Key': str(uuid4())}, json={'decision': 'approve'}).json()
        acc_2 = app['account_id']
        self.assertNotEqual(acc_2, acc_1)
        self.assertNotEqual(acc_2, 'telegram:208')

        me_approved = self.client.get('/api/v1/me', headers=self.headers_for(208)).json()
        self.assertEqual(me_approved['id'], acc_2)
        self.assertEqual(me_approved['status'], 'approved')

    def test_guest_rejection_refresh_queue_ackrevision_protected(self):
        self.register(209)
        req = self.client.post('/api/v1/me/access-requests',
            headers={**self.headers_for(209), 'Idempotency-Key': str(uuid4())}).json()
        self.client.post(f"/api/v1/access-requests/{req['id']}/decision",
            headers={**self.headers_for(101), 'Idempotency-Key': str(uuid4())}, json={'decision': 'reject'})

        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM backend_telegram_menu_refresh WHERE subject='209'").fetchone()
            self.assertEqual(row['revision'], 1)

        # Revision bumped to 2
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_telegram_menu_refresh SET revision=2 WHERE subject='209'")

        claim = self.client.post('/api/v1/integrations/telegram/menu-refresh/claim', headers=self.headers).json()['delivery']
        self.assertEqual(claim['visitor_subject'], '209')
        self.assertEqual(claim['revision'], 2)
        self.assertEqual(claim['account']['id'], 'telegram:209')

        # Acknowledging obsolete revision 1 does NOT delete the entry
        ack_old = self.client.post('/api/v1/integrations/telegram/visitor-menu-refresh/209/ack?revision=1', headers=self.headers)
        self.assertEqual(ack_old.status_code, 200)
        with self.db.connect() as conn:
            self.assertIsNotNone(conn.execute("SELECT * FROM backend_telegram_menu_refresh WHERE subject='209'").fetchone())

        # Next claim still returns delivery
        claim2 = self.client.post('/api/v1/integrations/telegram/menu-refresh/claim', headers=self.headers).json()['delivery']
        self.assertEqual(claim2['visitor_subject'], '209')
        self.assertEqual(claim2['revision'], 2)

        # Acknowledging matching revision 2 deletes the entry
        ack_cur = self.client.post('/api/v1/integrations/telegram/visitor-menu-refresh/209/ack?revision=2', headers=self.headers)
        self.assertEqual(ack_cur.status_code, 200)
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT * FROM backend_telegram_menu_refresh WHERE subject='209'").fetchone())

        # Queue is now empty
        self.assertIsNone(self.client.post('/api/v1/integrations/telegram/menu-refresh/claim', headers=self.headers).json()['delivery'])

        # Ack rejects client delegation header
        ack_invalid = self.client.post('/api/v1/integrations/telegram/visitor-menu-refresh/209/ack?revision=1',
            headers=self.headers_for(101))
        self.assertEqual(ack_invalid.status_code, 422)
