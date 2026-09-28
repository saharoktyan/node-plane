from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
import tempfile
import unittest

from tests.test_backend_identity import Database
from backend.admin_cli import bootstrap_admin, write_secret
from backend.authorization import AccessDenied, PrincipalKind, resolve_actor, require_permission
from backend.credentials import ADAPTER_SCOPES, CredentialService
from backend.identity_repository import SQLIdentityRepository


class BackendCredentialTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.addCleanup(self.db.connection.close)
        self.identities = SQLIdentityRepository(self.db)
        self.identities.initialize_schema()
        self.credentials = CredentialService(self.db)
        self.credentials.initialize_schema()
        self.now = datetime.now(timezone.utc)

    def token(self):
        return self.credentials.issue(PrincipalKind.ADAPTER, ADAPTER_SCOPES, now=self.now)

    def unauthorized(self, value, **kwargs):
        with self.assertRaises(AccessDenied) as caught:
            self.credentials.authenticate(value, **kwargs)
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(caught.exception.code, 'invalid_credentials')

    def test_bearer_verifies_and_storage_contains_no_secret(self):
        token_id, token = self.token()
        principal = self.credentials.authenticate('Bearer ' + token)
        self.assertEqual(principal.id, token_id)
        self.assertEqual(principal.scopes, ADAPTER_SCOPES)
        row = self.db.connection.execute('SELECT * FROM backend_credentials').fetchone()
        self.assertNotIn(token, str(dict(row)))
        self.assertNotIn(token.split('.')[1], str(dict(row)))

    def test_invalid_headers_and_tampered_secret(self):
        _, token = self.token()
        for value in (None, '', token, 'Basic ' + token, 'Bearer ' + token + '\n', 'Bearer ' + token[:-1] + '!'):
            with self.subTest(header_type=type(value).__name__):
                self.unauthorized(value)
        replacement = 'A' if token[-1] != 'A' else 'B'
        self.unauthorized('Bearer ' + token[:-1] + replacement)

    def test_expiration_boundary_and_revocation(self):
        token_id, token = self.token()
        self.unauthorized('Bearer ' + token, now=self.now + timedelta(days=30))
        self.assertTrue(self.credentials.revoke(token_id))
        self.assertTrue(self.credentials.revoke(token_id))
        self.unauthorized('Bearer ' + token)
        self.assertFalse(self.credentials.revoke('missing'))

    def test_account_token_cannot_obtain_adapter_scope(self):
        account = self.identities.resolve_telegram(101)
        with self.assertRaises(ValueError):
            self.credentials.issue(PrincipalKind.ACCOUNT, ADAPTER_SCOPES, account_id=account.id)
        with self.assertRaises(ValueError):
            self.credentials.issue(PrincipalKind.ACCOUNT, frozenset({'account.self.read'}), account_id='missing')
        with self.assertRaises(ValueError):
            self.credentials.issue(PrincipalKind.ADAPTER, ADAPTER_SCOPES, account_id=account.id)

    def test_local_bootstrap_and_authenticated_delegation(self):
        account = bootstrap_admin(self.identities, 101)
        self.assertEqual((account.role, account.status), ('admin', 'approved'))
        self.assertEqual(bootstrap_admin(self.identities, 101).id, account.id)
        _, token = self.token()
        principal = self.credentials.authenticate('Bearer ' + token)
        actor = resolve_actor(principal, self.identities, telegram_user_id=101)
        require_permission(actor, 'nodes.execute')
        self.assertEqual(actor.account.id, account.id)
        unknown = self.identities.resolve_telegram(102)
        self.assertEqual((unknown.role, unknown.status), ('member', 'pending'))

    def test_role_revocation_is_not_cached_in_token(self):
        account = bootstrap_admin(self.identities, 101)
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, frozenset({'nodes.execute'}), account_id=account.id)
        principal = self.credentials.authenticate('Bearer ' + token)
        require_permission(resolve_actor(principal, self.identities), 'nodes.execute')
        self.db.connection.execute("UPDATE backend_accounts SET role = 'member' WHERE id = ?", (account.id,))
        with self.assertRaises(AccessDenied):
            require_permission(resolve_actor(principal, self.identities), 'nodes.execute')

    def test_secret_file_permissions_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'token'
            write_secret(path, 'secret')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                write_secret(path, 'replacement')
            self.assertEqual(path.read_text(), 'secret\n')
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(FileExistsError):
                write_secret(link, 'replacement')

    def test_invalid_lifetime_or_scope_is_rejected_before_insertion(self):
        for ttl in (timedelta(0), timedelta(days=366)):
            with self.assertRaises(ValueError):
                self.credentials.issue(PrincipalKind.ADAPTER, ADAPTER_SCOPES, ttl=ttl)
        with self.assertRaises(ValueError):
            self.credentials.issue(PrincipalKind.ADAPTER, frozenset({'unknown'}))
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_credentials').fetchone()[0], 0)
