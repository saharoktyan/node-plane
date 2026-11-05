from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from backend.authorization import AccessDenied, Principal, PrincipalKind, resolve_actor
from backend.telegram_configuration import change, overview
from backend.workstation_cli import SCOPES
from tests.test_backend_http import BackendHTTPTests


class TelegramConfigurationTests(unittest.TestCase):
    setUp = BackendHTTPTests.setUp
    register = BackendHTTPTests.register

    def fixture(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / '.env'
        path.write_text('BOT_TOKEN=123456:old_secret_value_0123456789\nADMIN_IDS=101\nOTHER=value\n')
        path.chmod(0o600)
        actor = resolve_actor(Principal('npw', PrincipalKind.ACCOUNT, SCOPES, self.admin.id), self.identities)
        return actor, path

    def test_secret_is_verified_saved_privately_and_never_returned(self):
        actor, path = self.fixture()
        token = '123456:new_secret_value_0123456789'
        verify = lambda value: self.assertEqual(value, token)
        value = change(self.db, actor, self.identities, path,
            {'action': 'telegram-token', 'token': token}, verify=verify, restart=lambda: True)
        self.assertIn('BOT_TOKEN=' + token, path.read_text())
        self.assertNotIn(token, str(value))
        self.assertTrue(value['token_configured'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_token_keeps_previous_configuration(self):
        actor, path = self.fixture()
        original = path.read_text()
        def reject(_):
            raise AccessDenied('invalid_bot_token', 422)
        with self.assertRaises(AccessDenied):
            change(self.db, actor, self.identities, path,
                {'action': 'telegram-token', 'token': 'invalid'}, verify=reject, restart=lambda: True)
        self.assertEqual(path.read_text(), original)

    def test_add_remove_admin_updates_roles_and_bootstrap_list(self):
        actor, path = self.fixture()
        member = self.register(102, username='alice').json()
        self.assertFalse(member['account_created'])
        req = self.client.post('/api/v1/me/access-requests',
            headers={**self.headers, 'X-Node-Plane-Telegram-User-ID': '102', 'Idempotency-Key': str(uuid4())}).json()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_telegram_menu_refresh(subject, revision) VALUES ('102', 1)")
        restart = Mock(return_value=True)
        value = change(self.db, actor, self.identities, path,
            {'action': 'telegram-admin-add', 'telegram_id': 102, 'command_id': str(uuid4())}, restart=restart)
        restart.assert_not_called()
        admin_entry = next(item for item in value['admins'] if item['telegram_user_id'] == '102')
        account_id = admin_entry['id']
        self.assertEqual(self.identities.get_account(account_id).role, 'admin')
        self.assertIn('ADMIN_IDS=101,102', path.read_text())
        self.assertEqual(admin_entry['username'], 'alice')
        with self.db.connect() as conn:
            req_row = conn.execute("SELECT status, account_id FROM backend_access_requests WHERE id=?", (req['id'],)).fetchone()
            self.assertEqual(req_row['status'], 'approved')
            self.assertEqual(req_row['account_id'], account_id)
            self.assertIsNone(conn.execute("SELECT * FROM backend_telegram_menu_refresh WHERE subject='102'").fetchone())
        change(self.db, actor, self.identities, path,
            {'action': 'telegram-admin-remove', 'telegram_id': 102, 'command_id': str(uuid4())}, restart=lambda: True)
        self.assertEqual(self.identities.get_account(account_id).role, 'member')
        self.assertIn('ADMIN_IDS=101\n', path.read_text())

    def test_self_removal_and_symlink_configuration_are_rejected(self):
        actor, path = self.fixture()
        original = path.read_text()
        with self.assertRaises(AccessDenied):
            change(self.db, actor, self.identities, path,
                {'action': 'telegram-admin-remove', 'telegram_id': 101, 'command_id': str(uuid4())}, restart=lambda: True)
        self.assertEqual(path.read_text(), original)
        link = path.parent / 'link'
        link.symlink_to(path)
        with self.assertRaises(AccessDenied):
            overview(self.db, actor, link)

    def test_failed_env_write_rolls_back_admin_promotion(self):
        actor, path = self.fixture()
        member = self.register(102).json()
        self.assertFalse(member['account_created'])
        self.assertIsNone(self.identities.find_telegram_account(102))
        with patch('backend.telegram_configuration._write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                change(self.db, actor, self.identities, path,
                    {'action': 'telegram-admin-add', 'telegram_id': 102, 'command_id': str(uuid4())}, restart=lambda: True)
        self.assertIsNone(self.identities.find_telegram_account(102))

        # Existing account role untouched on rollback
        existing = self.identities.create_account(status='approved')
        self.identities.link_telegram(existing.id, 103)
        self.assertEqual(self.identities.get_account(existing.id).role, 'member')
        with patch('backend.telegram_configuration._write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                change(self.db, actor, self.identities, path,
                    {'action': 'telegram-admin-add', 'telegram_id': 103, 'command_id': str(uuid4())}, restart=lambda: True)
        self.assertEqual(self.identities.get_account(existing.id).role, 'member')


class TelegramBotSwitchTests(unittest.TestCase):
    """Turning the bot off and on again; the host side is a fake."""
    setUp = BackendHTTPTests.setUp
    fixture = TelegramConfigurationTests.fixture

    def system(self, works=True):
        return Mock(install=Mock(return_value=works), remove=Mock(return_value=works))

    def adapter_tokens(self):
        return self.db.connection.execute("""SELECT COUNT(*) FROM backend_credentials
            WHERE kind='adapter' AND revoked_at IS NULL""").fetchone()[0]

    def test_disable_then_enable_keeps_accounts_and_replaces_the_bot_credential(self):
        from backend.telegram_configuration import switch
        actor, path = self.fixture()
        token_file = path.parent / 'data/telegram-adapter.token'
        token_file.parent.mkdir()
        token_file.write_text('old\n')
        self.assertGreater(self.adapter_tokens(), 0)
        system = self.system()
        value = switch(self.db, actor, path, {'action': 'telegram-disable'}, system=system)
        system.remove.assert_called_once()
        self.assertFalse(value['enabled'])
        self.assertFalse(value['restart_pending'])
        self.assertEqual(self.adapter_tokens(), 0)
        self.assertFalse(token_file.exists())
        self.assertIn('BOT_TOKEN=\n', path.read_text())
        self.assertIn('OTHER=value', path.read_text())
        self.assertIsNotNone(self.identities.find_telegram_account(101))
        with self.assertRaises(AccessDenied) as refused:
            change(self.db, actor, self.identities, path, {'action': 'telegram-token',
                   'token': '123456:new_secret_value_0123456789'}, verify=lambda _: None, restart=lambda: True)
        self.assertEqual(refused.exception.code, 'telegram_disabled')

        token = '123456:new_secret_value_0123456789'
        value = switch(self.db, actor, path, {'action': 'telegram-enable', 'token': token},
                       verify=lambda value: self.assertEqual(value, token), system=system)
        system.install.assert_called_once()
        self.assertTrue(value['enabled'])
        self.assertNotIn(token, str(value))
        self.assertIn('BOT_TOKEN=' + token, path.read_text())
        self.assertIn('NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE=' + str(token_file), path.read_text())
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.adapter_tokens(), 1)
        secret = token_file.read_text().strip()
        self.assertEqual(self.credentials.authenticate('Bearer ' + secret).kind, PrincipalKind.ADAPTER)

    def test_service_that_does_not_start_is_reported(self):
        from backend.telegram_configuration import switch
        actor, path = self.fixture()
        value = switch(self.db, actor, path, {'action': 'telegram-enable',
                       'token': '123456:new_secret_value_0123456789'}, verify=lambda _: None,
                       system=self.system(works=False))
        self.assertTrue(value['enabled'])
        self.assertTrue(value['restart_pending'])

    def test_rejected_token_changes_nothing(self):
        from backend.telegram_configuration import switch
        actor, path = self.fixture()
        switch(self.db, actor, path, {'action': 'telegram-disable'}, system=self.system())
        original = path.read_text()
        def reject(_):
            raise AccessDenied('bot_token_verification_failed', 422)
        system = self.system()
        with self.assertRaises(AccessDenied):
            switch(self.db, actor, path, {'action': 'telegram-enable', 'token': 'x'}, verify=reject, system=system)
        system.install.assert_not_called()
        self.assertEqual(path.read_text(), original)
        self.assertFalse(overview(self.db, actor, path)['enabled'])


class TelegramSwitchRequestTests(unittest.TestCase):
    def test_enable_needs_a_token_and_disable_takes_nothing_else(self):
        from backend.workstation_cli import WorkstationError, validate_request
        base = {'version': 1, 'session_id': str(uuid4())}
        validate_request({**base, 'action': 'telegram-enable', 'token': '123456:new_secret_value_0123456789'})
        validate_request({**base, 'action': 'telegram-disable'})
        for request in ({**base, 'action': 'telegram-enable'},
                        {**base, 'action': 'telegram-enable', 'token': 'bad'},
                        {**base, 'action': 'telegram-disable', 'token': '123456:new_secret_value_0123456789'}):
            with self.subTest(request=request), self.assertRaises(WorkstationError):
                validate_request(request)
