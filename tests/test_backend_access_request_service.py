from __future__ import annotations

import unittest
from uuid import uuid4

from tests.test_backend_identity import Database
from backend.access_requests import AccessRequestService
from backend.admin_cli import bootstrap_admin
from backend.authorization import AccessDenied, Principal, PrincipalKind, resolve_actor
from backend.identity_repository import SQLIdentityRepository


class AccessRequestServiceTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.addCleanup(self.db.connection.close)
        self.identities = SQLIdentityRepository(self.db)
        self.identities.initialize_schema()
        self.requests = AccessRequestService(self.db)
        self.requests.initialize_schema()
        self.admin = bootstrap_admin(self.identities, 101)
        self.member = self.identities.resolve_telegram(102)
        scopes = frozenset({'delegate.telegram', 'access_requests.self.create',
                            'access_requests.self.read', 'access_requests.manage'})
        principal = Principal('adapter', PrincipalKind.ADAPTER, scopes)
        self.admin_actor = resolve_actor(principal, self.identities, telegram_user_id=101)
        self.member_actor = resolve_actor(principal, self.identities, telegram_user_id=102)

    def test_approval_replay_and_access_isolation(self):
        key = str(uuid4())
        item = self.requests.create(self.member_actor, key)
        self.assertEqual(item['status'], 'pending')
        self.assertEqual(item, self.requests.create(self.member_actor, key))
        with self.assertRaises(AccessDenied) as error:
            self.requests.create(self.member_actor, str(uuid4()))
        self.assertEqual(error.exception.code, 'request_already_pending')
        with self.assertRaises(AccessDenied):
            self.requests.list_pending(self.member_actor)
        self.assertEqual(self.requests.list_pending(self.admin_actor)['items'], [item])
        decision_key = str(uuid4())
        decided = self.requests.decide(self.admin_actor, item['id'], 'approve', decision_key)
        self.assertEqual(decided['status'], 'approved')
        self.assertEqual(decided, self.requests.decide(self.admin_actor, item['id'], 'approve', decision_key))
        with self.assertRaises(AccessDenied) as error:
            self.requests.decide(self.admin_actor, item['id'], 'reject', str(uuid4()))
        self.assertEqual(error.exception.code, 'request_already_decided')
        self.assertEqual(self.identities.get_account(self.member.id).status, 'approved')
        self.assertEqual(self.requests.list_pending(self.admin_actor)['items'], [])
        self.assertEqual(self.requests.list_own(self.member_actor)['items'], [decided])

    def test_rejected_account_can_reapply_without_reopening_old_request(self):
        first = self.requests.create(self.member_actor, str(uuid4()))
        self.requests.decide(self.admin_actor, first['id'], 'reject', str(uuid4()))
        self.assertEqual(self.identities.get_account(self.member.id).status, 'rejected')
        second = self.requests.create(self.member_actor, str(uuid4()))
        self.assertNotEqual(first['id'], second['id'])
        self.requests.decide(self.admin_actor, second['id'], 'approve', str(uuid4()))
        with self.assertRaises(AccessDenied):
            self.requests.decide(self.admin_actor, first['id'], 'approve', str(uuid4()))
