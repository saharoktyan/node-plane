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
        restart = Mock(return_value=True)
        value = change(self.db, actor, self.identities, path,
            {'action': 'telegram-admin-add', 'telegram_id': 102, 'command_id': str(uuid4())}, restart=restart)
        restart.assert_not_called()
        self.assertEqual(self.identities.get_account(member['id']).role, 'admin')
        self.assertIn('ADMIN_IDS=101,102', path.read_text())
        self.assertEqual(next(item for item in value['admins'] if item['telegram_user_id'] == '102')['username'], 'alice')
        change(self.db, actor, self.identities, path,
            {'action': 'telegram-admin-remove', 'telegram_id': 102, 'command_id': str(uuid4())}, restart=lambda: True)
        self.assertEqual(self.identities.get_account(member['id']).role, 'member')
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
        with patch('backend.telegram_configuration._write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                change(self.db, actor, self.identities, path,
                    {'action': 'telegram-admin-add', 'telegram_id': 102, 'command_id': str(uuid4())}, restart=lambda: True)
        self.assertEqual(self.identities.get_account(member['id']).role, 'member')
