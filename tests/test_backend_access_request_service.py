from __future__ import annotations

import unittest
from uuid import uuid4

from tests.test_backend_identity import Database
from backend.access_requests import AccessRequestService
from backend.system_settings import SystemSettingsService
from backend.admin_cli import bootstrap_admin
from backend.authorization import AccessDenied, Principal, PrincipalKind, resolve_actor
from backend.identity_repository import SQLIdentityRepository



def text(value, source='en', **others):
    return {'source': source, 'texts': {source: value, **others}}

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
        self.assertEqual(self.identities.get_account(self.member.id).status, 'approved')
        second = self.requests.create(self.member_actor, str(uuid4()))
        self.assertNotEqual(first['id'], second['id'])
        self.requests.decide(self.admin_actor, second['id'], 'approve', str(uuid4()))
        with self.assertRaises(AccessDenied):
            self.requests.decide(self.admin_actor, first['id'], 'approve', str(uuid4()))

    def test_account_requests_vpn_only_while_it_has_no_vpn_profile(self):
        from backend.profiles import ProfileRepository
        first = self.requests.create(self.member_actor, str(uuid4()))
        self.requests.decide(self.admin_actor, first['id'], 'approve', str(uuid4()))
        with self.db.connect() as conn:
            self.assertTrue(ProfileRepository.has_vpn_profile(conn, self.member.id))
        with self.assertRaises(AccessDenied) as error:
            self.requests.create(self.member_actor, str(uuid4()))
        self.assertEqual(error.exception.code, 'request_not_allowed')
        # Deleting the last profile leaves the account; it may ask again.
        profile = self.db.connection.execute('SELECT id FROM backend_profiles WHERE owner_account_id=?',
                                             (self.member.id,)).fetchone()['id']
        self.db.connection.execute("INSERT INTO backend_profile_deletions(profile_id, requested_at) VALUES (?, 'now')",
                                   (profile,))
        self.assertEqual(self.identities.get_account(self.member.id).status, 'approved')
        again = self.requests.create(self.member_actor, str(uuid4()))
        self.assertEqual(again['status'], 'pending')

    def test_administrators_do_not_request_vpn_access(self):
        with self.assertRaises(AccessDenied) as error:
            self.requests.create(self.admin_actor, str(uuid4()))
        self.assertEqual(error.exception.code, 'request_not_allowed')

    def test_access_request_policy_is_public_read_and_admin_write(self):
        self.assertEqual(self.requests.policy(self.member_actor), {
            'enabled': True,
            'telegram': True,
            'gate_message': 'Authorization is required to use this bot.',
            'gate_messages': None,
            'notify_requests': None,
            'welcome_message': 'Request access to use this bot.',
            'welcome_messages': None,
        })
        updated = self.requests.update_policy(self.admin_actor, enabled=False,
            gate_messages=text('Contact the administrator.'), notify_requests=False)
        self.assertEqual((updated['enabled'], updated['gate_message'], updated['notify_requests']),
                         (False, 'Contact the administrator.', False))
        self.assertEqual(updated['gate_messages']['texts'], {'en': 'Contact the administrator.'})
        policy = self.requests.policy(self.member_actor)
        self.assertEqual((policy['enabled'], policy['gate_message'], policy['gate_messages']),
                         (False, 'Contact the administrator.', None))
        self.assertFalse(self.requests.policy(self.admin_actor)['notify_requests'])
        with self.assertRaises(AccessDenied) as error:
            self.requests.create(self.member_actor, str(uuid4()))
        self.assertEqual(error.exception.code, 'access_requests_disabled')
        with self.assertRaises(AccessDenied):
            self.requests.update_policy(self.member_actor, enabled=True)

    def test_messages_in_several_languages_persist_across_mode_switches(self):
        updated = self.requests.update_policy(self.admin_actor, welcome_messages=text(
            'Welcome to our private bot!', ru='Добро пожаловать!'))
        self.assertEqual(updated['welcome_message'], 'Welcome to our private bot!')
        self.assertEqual(updated['gate_message'], 'Authorization is required to use this bot.')
        disabled = self.requests.update_policy(self.admin_actor, enabled=False,
                                               gate_messages=text('Registration closed.'))
        self.assertEqual((disabled['gate_message'], disabled['welcome_message']),
                         ('Registration closed.', 'Welcome to our private bot!'))
        enabled = self.requests.update_policy(self.admin_actor, enabled=True)
        self.assertEqual(enabled['gate_message'], 'Registration closed.')
        # A Russian reader gets the Russian text; the gate message has only English.
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET locale='ru' WHERE id=?", (self.member_actor.account.id,))
        policy = self.requests.policy(self.member_actor)
        self.assertEqual((policy['welcome_message'], policy['gate_message']),
                         ('Добро пожаловать!', 'Registration closed.'))
        for value in (text('   '), text('a' * 501)):
            with self.assertRaises(AccessDenied) as ctx:
                self.requests.update_policy(self.admin_actor, welcome_messages=value)
            self.assertEqual(ctx.exception.code, 'invalid_localized_text')
        with self.assertRaises(AccessDenied):
            self.requests.update_policy(self.member_actor, welcome_messages=text('Hacked'))

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
        self.assertEqual(found['items'][0]['display_name'], 'Alex')
        self.db.connection.execute("UPDATE backend_accounts SET display_name='Sam Account' WHERE id=?",
                                   (self.identities.find_telegram_account(104).id,))
        named = self.requests.list_pending(self.admin_actor, limit=5, search='sam account')
        self.assertEqual([item['username'] for item in named['items']], ['sam'])
        self.assertIsNone(found['next_cursor'])
        self.assertEqual([page['pending_total'] for page in (first, second, third, found)], [3, 3, 3, 3])
        with self.assertRaises(AccessDenied) as error:
            self.requests.list_pending(self.admin_actor, cursor=first['next_cursor'],
                                       search='different search')
        self.assertEqual(error.exception.code, 'invalid_cursor')
