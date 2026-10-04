from __future__ import annotations

import unittest
from uuid import uuid4

from tests.test_backend_identity import Database
from backend.access_requests import AccessRequestService
from backend.system_settings import SystemSettingsService
from backend.admin_cli import bootstrap_admin
from backend.authorization import AccessDenied, Principal, PrincipalKind, resolve_actor
from backend.identity_repository import SQLIdentityRepository


class AccessRequestServiceTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.addCleanup(self.db.connection.close)
        self.identities = SQLIdentityRepository(self.db)
        self.identities.initialize_schema()
        from backend.profiles import ProfileRepository
        ProfileRepository(self.db).initialize_schema()
        self.requests = AccessRequestService(self.db)
        self.requests.initialize_schema()
        self.system_settings = SystemSettingsService(self.db)
        self.system_settings.initialize_schema()
        self.admin = bootstrap_admin(self.identities, 101)
        self.member = self.identities.resolve_telegram(102)
        scopes = frozenset({'delegate.telegram', 'account.self.read', 'access_requests.self.create',
                            'access_requests.self.read', 'access_requests.manage',
                            'settings.manage'})
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
        pending = self.requests.list_pending(self.admin_actor)['items']
        self.assertEqual(len(pending), 1)
        self.assertEqual({key: pending[0][key] for key in item}, item)
        self.assertEqual(pending[0]['telegram_user_id'], 102)
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

    def test_access_request_policy_is_public_read_and_admin_write(self):
        self.assertEqual(self.requests.policy(self.member_actor), {
            'enabled': True,
            'gate_message': 'Authorization is required to use this bot.',
            'notify_requests': None,
        })
        updated = self.requests.update_policy(self.admin_actor, enabled=False,
            gate_message='Contact the administrator.', notify_requests=False)
        self.assertEqual(updated, {
            'enabled': False,
            'gate_message': 'Contact the administrator.',
            'notify_requests': False,
        })
        self.assertEqual(self.requests.policy(self.member_actor), {
            'enabled': False,
            'gate_message': 'Contact the administrator.',
            'notify_requests': None,
        })
        self.assertFalse(self.requests.policy(self.admin_actor)['notify_requests'])
        with self.assertRaises(AccessDenied) as error:
            self.requests.create(self.member_actor, str(uuid4()))
        self.assertEqual(error.exception.code, 'access_requests_disabled')
        with self.assertRaises(AccessDenied):
            self.requests.update_policy(self.member_actor, enabled=True)

    def test_bot_title_is_backend_owned_and_admin_managed(self):
        self.assertEqual(self.system_settings.bot_title(self.member_actor),
                         {'title': 'Node Plane'})
        self.assertEqual(self.system_settings.update_bot_title(
            self.admin_actor, 'My VPN'), {'title': 'My VPN'})
        self.assertEqual(self.system_settings.bot_title(self.member_actor),
                         {'title': 'My VPN'})
        with self.assertRaises(AccessDenied):
            self.system_settings.update_bot_title(self.member_actor, 'Member edit')
        with self.assertRaises(AccessDenied):
            self.system_settings.update_bot_title(self.admin_actor, ' ')

    def test_pending_requests_search_and_cursor_pagination(self):
        actors = [self.member_actor]
        for telegram_id, username, first_name in (
                (103, 'alex_one', 'Alex'), (104, 'sam', 'Sam')):
            account = self.identities.resolve_telegram(telegram_id)
            self.identities.update_telegram_details(telegram_id,
                username=username, first_name=first_name, language_code='en')
            actors.append(resolve_actor(
                self.admin_actor.principal, self.identities,
                telegram_user_id=telegram_id))
        for actor in actors:
            self.requests.create(actor, str(uuid4()))
        first = self.requests.list_pending(self.admin_actor, limit=1)
        second = self.requests.list_pending(self.admin_actor, limit=1,
                                            cursor=first['next_cursor'])
        third = self.requests.list_pending(self.admin_actor, limit=1,
                                           cursor=second['next_cursor'])
        self.assertEqual(len({first['items'][0]['id'], second['items'][0]['id'],
                              third['items'][0]['id']}), 3)
        found = self.requests.list_pending(self.admin_actor, limit=1,
                                           search='ALEX_')
        self.assertEqual([item['username'] for item in found['items']], ['alex_one'])
        self.assertIsNone(found['next_cursor'])
        self.assertEqual([page['pending_total'] for page in (first, second, third, found)], [3, 3, 3, 3])
        with self.assertRaises(AccessDenied) as error:
            self.requests.list_pending(self.admin_actor, cursor=first['next_cursor'],
                                       search='different search')
        self.assertEqual(error.exception.code, 'invalid_cursor')
