"""Signing in by password, passkey and invite against disposable PostgreSQL only."""
from datetime import datetime, timezone
import os
from unittest.mock import patch
import unittest

from backend import invites, passkeys, sessions
from backend.authorization import AccessDenied, Principal, PrincipalKind, resolve_actor
from backend.credentials import PERMISSIONS, CredentialService
from backend.identity_repository import SQLIdentityRepository
from tests import test_backups_postgres as fixture
from tests.test_backend_passkeys import RP, Authenticator

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class SignInPostgresTests(unittest.TestCase):
    setUp = fixture.BackupsPostgresTests.setUp
    drop_schema = fixture.BackupsPostgresTests.drop_schema

    def test_an_invited_account_signs_in_by_password_and_passkey(self):
        CredentialService(self.db).initialize_schema()
        admin = resolve_actor(Principal('test', PrincipalKind.ACCOUNT, PERMISSIONS, account_id=self.admin.id),
                              SQLIdentityRepository(self.db))
        invites.set_bot_username(self.db, admin, 'np_test_bot')
        token = invites.create(self.db, admin, vpn=True, now=NOW)['token']
        account = invites.accept(self.db, token, login='Dana', password='correct horse battery',
                                 source='10.0.0.1', now=NOW)['account']
        with self.assertRaises(AccessDenied):
            sessions.sign_in(self.db, 'dana', 'wrong password', device='x', source='10.0.0.1', now=NOW)
        tokens = sessions.sign_in(self.db, 'DANA', 'correct horse battery', device='x', source='10.0.0.1', now=NOW)
        sessions.refresh(self.db, tokens['refresh_token'], now=NOW)
        with patch.object(passkeys, 'relying_party', return_value=RP):
            device = Authenticator()
            started = passkeys.registration_options(self.db, account['id'], now=NOW)
            passkeys.register(self.db, account['id'], started['challenge_id'], device.create(started['options']),
                              now=NOW)
            started = passkeys.sign_in_options(self.db, source='10.0.0.1', now=NOW)
            passkeys.sign_in(self.db, started['challenge_id'], device.get(started['options']), device='Phone',
                             source='10.0.0.1', now=NOW)
        with self.db.connect() as conn:
            methods = sorted(s['method'] for s in sessions.listed(conn, account['id'], now=NOW))
        self.assertEqual(methods, ['invite', 'passkey', 'password'])

    def test_registration_lists_apart_and_cleans_up(self):
        from datetime import timedelta
        from backend import registrations
        from backend.accounts import AccountService
        from tests.test_backend_registrations import solve
        CredentialService(self.db).initialize_schema()
        admin = resolve_actor(Principal('test', PrincipalKind.ACCOUNT, PERMISSIONS, account_id=self.admin.id),
                              SQLIdentityRepository(self.db))
        registrations.update(self.db, admin, open=True, cleanup_days=7)
        with patch.object(registrations, 'POW_BITS', 8):
            puzzle = registrations.challenge(self.db, source='s', now=NOW)
            account = registrations.register(self.db, puzzle['challenge_id'], solve(puzzle), login='kim',
                                             source='s', now=NOW)['account']
        apart = AccountService(self.db).list(admin, limit=100, access='without')['items']
        self.assertEqual([item['id'] for item in apart], [account['id']])
        self.assertNotIn(account['id'], [item['id'] for item in AccountService(self.db).list(admin, limit=100,
                                                                                             access='with')['items']])
        self.assertTrue(registrations.RegistrationCleanup(self.db).run_one(now=NOW + timedelta(days=8)))

    def test_telegram_signs_in_and_moves_from_an_empty_account(self):
        from backend import logins, telegram_sign_in as telegram
        from tests.test_backend_telegram_sign_in import NOW as SIGNED, TOKEN, init_data, widget
        CredentialService(self.db).initialize_schema()
        identities = SQLIdentityRepository(self.db)
        identities.resolve_telegram(505)
        anna = identities.create_account().id
        with self.db.transaction() as conn:
            logins.claim(conn, anna, 'anna', 'first', SIGNED)
        with patch.object(telegram, 'bot_token', return_value=TOKEN):
            preview = telegram.link_preview(self.db, anna, kind='webapp', data=init_data(505), source='s', now=SIGNED)
            self.assertEqual(preview['state'], 'other_empty')
            telegram.link_confirm(self.db, anna, preview['link_id'], now=SIGNED)
            telegram.sign_in(self.db, 'login', widget(505), device='x', source='s', now=SIGNED)
        self.assertEqual(identities.find_telegram_account(505).id, anna)
