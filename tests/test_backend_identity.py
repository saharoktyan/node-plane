from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from backend.authorization import (
    AccessDenied, Principal, PrincipalKind, ProfileResource, require_permission,
    require_profile, resolve_actor,
)
from backend.identity import IdentityService
from backend.identity_repository import SQLIdentityRepository
from backend.profiles import ProfileRepository


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(':memory:', check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys = ON')

    @contextmanager
    def connect(self):
        yield self.connection

    @contextmanager
    def transaction(self):
        with self.connection:
            yield self.connection


class BackendIdentityTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.addCleanup(self.db.connection.close)
        self.repository = SQLIdentityRepository(self.db)
        self.repository.initialize_schema()
        self.service = IdentityService(self.repository)
        self.scopes = frozenset({'identity.telegram.resolve', 'delegate.telegram', 'account.self.read',
                                'profiles.self.read', 'configs.self.issue', 'configs.self.read',
                                'profiles.manage', 'configs.manage.issue', 'nodes.execute'})
        self.adapter = Principal('telegram', PrincipalKind.ADAPTER, self.scopes)
        self.alice = self.service.resolve_telegram(self.adapter, 101)
        self.bob = self.service.resolve_telegram(self.adapter, 102)

    def approve(self, account, role='member'):
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET status = 'approved', role = ? WHERE id = ?", (role, account.id))
        return resolve_actor(self.adapter, self.repository, telegram_user_id=101)

    def denied(self, code, call):
        with self.assertRaises(AccessDenied) as error:
            call()
        self.assertEqual(error.exception.code, code)

    def test_registration_is_pending_member_and_repeat_has_same_id(self):
        self.assertEqual((self.alice.role, self.alice.status), ('member', 'pending'))
        self.assertEqual(self.service.resolve_telegram(self.adapter, 101), self.alice)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_accounts').fetchone()[0], 2)

    def test_profile_owner_exists_without_telegram_and_can_link_later(self):
        account = self.repository.create_account()
        profiles = ProfileRepository(self.db)
        profiles.initialize_schema()
        profile_id = profiles.create_profile(runtime_name='cli_user',
            display_name='CLI user', owner_account_id=account.id)
        self.assertIsNone(self.repository.find_telegram_account(303))
        direct = Principal('cli', PrincipalKind.ACCOUNT, self.scopes, account.id)
        self.assertEqual(resolve_actor(direct, self.repository).account.id, account.id)
        self.assertEqual(profiles.get(profile_id)['owner_account_id'], account.id)
        self.repository.link_telegram(account.id, 303)
        self.repository.link_telegram(account.id, 303)
        self.assertEqual(resolve_actor(self.adapter, self.repository,
            telegram_user_id=303).account.id, account.id)
        self.denied('identity_already_linked',
            lambda: self.repository.link_telegram(self.alice.id, 303))

    def test_account_credentials_cannot_delegate_or_register(self):
        principal = Principal('account', PrincipalKind.ACCOUNT, self.scopes, self.alice.id)
        self.denied('delegation_not_allowed', lambda: resolve_actor(principal, self.repository, telegram_user_id=102))
        self.denied('permission_denied', lambda: self.service.resolve_telegram(principal, 103))

    def test_adapter_needs_explicit_scopes_and_known_identity(self):
        principal = Principal('limited', PrincipalKind.ADAPTER, frozenset())
        self.denied('delegation_not_allowed', lambda: resolve_actor(principal, self.repository, telegram_user_id=101))
        self.denied('permission_denied', lambda: self.service.resolve_telegram(principal, 103))
        self.denied('identity_not_registered', lambda: resolve_actor(self.adapter, self.repository, telegram_user_id=103))

    def test_invalid_telegram_ids_are_rejected(self):
        for value in (None, True, '101', 0, -1, 2**63):
            with self.subTest(value=value):
                self.denied('invalid_telegram_identity', lambda: self.service.resolve_telegram(self.adapter, value))

    def test_pending_account_can_read_status_but_not_config(self):
        self.assertEqual(self.service.me(self.adapter, telegram_user_id=101), self.alice)
        actor = resolve_actor(self.adapter, self.repository, telegram_user_id=101)
        self.denied('permission_denied', lambda: require_profile(actor, ProfileResource('p', self.alice.id), action='issue_config', grant_active=True))

    def test_member_cannot_read_other_profile_or_execute_admin_action(self):
        actor = self.approve(self.alice)
        self.denied('resource_not_found', lambda: require_profile(actor, ProfileResource('p', self.bob.id)))
        self.denied('permission_denied', lambda: require_permission(actor, 'nodes.execute'))
        self.denied('permission_denied', lambda: require_profile(actor, ProfileResource('p', self.bob.id), administrative=True))
        require_profile(actor, ProfileResource('p', self.alice.id))

    def test_role_is_not_enough_without_principal_scope(self):
        self.approve(self.alice, 'admin')
        principal = Principal('limited', PrincipalKind.ACCOUNT, frozenset({'account.self.read'}), self.alice.id)
        actor = resolve_actor(principal, self.repository)
        self.denied('permission_denied', lambda: require_permission(actor, 'nodes.execute'))

    def test_admin_config_access_is_explicit_and_does_not_bypass_freeze(self):
        actor = self.approve(self.alice, 'admin')
        profile = ProfileResource('p', self.bob.id)
        self.denied('resource_not_found', lambda: require_profile(actor, profile, action='issue_config', grant_active=True))
        require_profile(actor, profile, action='issue_config', administrative=True, grant_active=True)
        frozen = ProfileResource('p', self.bob.id, frozen=True)
        self.denied('profile_frozen', lambda: require_profile(actor, frozen, action='issue_config', administrative=True, grant_active=True))

    def test_grant_expiry_and_artifact_read_are_checked(self):
        actor = self.approve(self.alice)
        profile = ProfileResource('p', self.alice.id)
        self.denied('grant_revoked', lambda: require_profile(actor, profile, action='read_config'))
        expired = ProfileResource('p', self.alice.id, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        self.denied('profile_expired', lambda: require_profile(actor, expired, action='read_config', grant_active=True))
        frozen = ProfileResource('p', self.alice.id, frozen=True)
        self.denied('profile_frozen', lambda: require_profile(actor, frozen, action='read_config', grant_active=True))

    def test_account_revocation_is_visible_on_next_request(self):
        self.approve(self.alice, 'admin')
        self.assertEqual(self.service.me(self.adapter, telegram_user_id=101).role, 'admin')
        self.db.connection.execute("UPDATE backend_accounts SET status = 'disabled' WHERE id = ?", (self.alice.id,))
        self.denied('account_disabled', lambda: self.service.me(self.adapter, telegram_user_id=101))

    def test_service_cannot_impersonate_telegram_user(self):
        principal = Principal('scheduler', PrincipalKind.SERVICE, self.scopes)
        self.denied('human_actor_required', lambda: resolve_actor(principal, self.repository, telegram_user_id=101))
