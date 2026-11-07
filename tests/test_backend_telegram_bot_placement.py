"""The Telegram bot on a node: moved there and back by the worker, its key
pinned, the controller's own bot stopped and started again."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import grpc

from backend import bot_package, relay_policy, telegram_bot_placement
from backend.account_channels import enabled, set_enabled
from backend.authorization import AccessDenied
from backend.client_keys import ClientKeys
from backend.telegram_bot_placement import TelegramBotPlacementService
from tests import test_backend_relays as relays
from tests.test_backend_relays import Failure

ROOT = Path(__file__).resolve().parents[1]


class Driver(relays.Driver):
    def __init__(self):
        super().__init__()
        self.seed = Ed25519PrivateKey.generate()

    def bot_key(self, node_key):
        self.calls.append(('bot-key', node_key))
        return {'public_key': self.seed.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}

    def apply_bot(self, node_key, root, version, **settings):
        self.calls.append(('bot', node_key, root, version, settings))
        if self.failure:
            raise self.failure
        return {'version': version, 'active': True}

    def remove_bot(self, node_key):
        self.calls.append(('bot-remove', node_key))
        if self.failure:
            raise self.failure
        return {}


class Local:
    """The controller's own bot."""

    def __init__(self):
        self.running = True

    def install(self):
        self.running = True
        return True

    def remove(self):
        self.running = False
        return True


class PlacementTests(unittest.TestCase):
    def setUp(self):
        relays.RelayTests.setUp(self)
        self.driver = Driver()
        self.relays.driver = self.driver
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.env = Path(temporary.name) / '.env'
        self.env.write_text('BOT_TOKEN=123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ\n')
        self.package = Path(temporary.name) / 'package'
        self.local = Local()
        self.bot = TelegramBotPlacementService(self.db, self.driver, local=self.local, env_path=self.env,
                                               package=lambda version: self.package)
        self.bot.initialize_schema()
        with self.db.transaction() as conn:
            set_enabled(conn, enabled(conn) | {'telegram'})

    def relay(self):
        self.relays.configure(self.actor, 'n1', {'domain': 'cab.example.com', 'tls': 'proxy', 'port': 9443})
        self.relays.run_one()

    def refused(self, code, work):
        with self.assertRaises(AccessDenied) as raised:
            work()
        self.assertEqual(raised.exception.code, code)

    def keys(self):
        with self.db.connect() as conn:
            return [tuple(row) for row in conn.execute(
                "SELECT id, revoked_at IS NULL FROM backend_client_keys WHERE kind='bot' ORDER BY created_at")]

    def test_moved_to_a_node_and_back(self):
        self.refused('telegram_bot_needs_relay', lambda: self.bot.place(self.actor, 'n1'))
        self.relay()
        self.refused('resource_not_found', lambda: self.bot.place(self.actor, 'missing'))
        answer = self.bot.place(self.actor, 'n1')
        self.assertEqual((answer['placement']['state'], answer['relay_node_key']), ('changes_pending', 'n1'))
        self.assertTrue(self.bot.run_one())
        key_id = self.keys()[0][0]
        self.assertEqual(self.driver.calls[-2:], [
            ('bot-key', 'n1'),
            ('bot', 'n1', str(self.package), telegram_bot_placement.APP_SEMVER,
             {'token': '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'api_url': 'https://cab.example.com',
              'relay_ca': '', 'key_id': key_id})])
        self.assertFalse(self.local.running)
        placement = self.bot.get(self.actor)['placement']
        self.assertEqual((placement['status'], placement['applied_version']),
                         ('active', telegram_bot_placement.APP_SEMVER))
        # The relay and the node stay while the bot uses them; it cannot be
        # turned off or moved elsewhere from there.
        self.refused('relay_serves_telegram_bot', lambda: self.relays.remove(self.actor))
        self.refused('telegram_bot_node_change', lambda: self.bot.place(self.actor, 'n2'))
        from backend.telegram_bot_setup import disable
        self.refused('telegram_bot_on_node', lambda: disable(self.db, self.env, read=None, write=None,
                                                             system=self.local))
        # A new release on the worker's start: the same key again.
        self.bot.recover()
        self.bot.run_one()
        self.assertEqual(self.keys(), [(key_id, 1)])
        self.bot.remove(self.actor)
        self.bot.run_one()
        self.assertEqual(self.driver.calls[-1], ('bot-remove', 'n1'))
        self.assertTrue(self.local.running)
        self.assertEqual(self.keys(), [(key_id, 0)])
        self.assertIsNone(self.bot.get(self.actor)['placement'])
        self.relays.remove(self.actor)

    def test_failures_are_kept_or_retried(self):
        self.relay()
        self.bot.place(self.actor, 'n1')
        self.driver.failure = Failure(grpc.StatusCode.UNAVAILABLE)
        self.bot.run_one()
        placement = self.bot.get(self.actor)['placement']
        self.assertEqual((placement['status'], placement['error_code']), ('applying', 'agent_unavailable'))
        self.assertTrue(self.local.running)
        # A failed install leaves the controller's bot running.
        self.bot.place(self.actor, 'n1')
        self.driver.failure = Failure(grpc.StatusCode.FAILED_PRECONDITION, 'bot_install_failed: no network')
        self.bot.run_one()
        placement = self.bot.get(self.actor)['placement']
        self.assertEqual((placement['status'], placement['error_code']), ('failed', 'bot_install_failed'))
        self.assertTrue(self.local.running)
        # Without a token the node gets nothing.
        self.env.write_text('BOT_TOKEN=\n')
        self.driver.failure = None
        self.bot.place(self.actor, 'n1')
        self.bot.run_one()
        self.assertEqual(self.bot.get(self.actor)['placement']['error_code'], 'telegram_disabled')

    def test_a_new_token_goes_to_the_node(self):
        self.relay()
        self.bot.place(self.actor, 'n1')
        self.bot.run_one()
        with self.db.transaction() as conn:
            self.assertTrue(telegram_bot_placement.reapply(conn))
        self.assertEqual(self.bot.get(self.actor)['placement']['state'], 'changes_pending')

    def test_the_bots_signed_requests_cross_the_relay(self):
        self.assertIsNone(relay_policy.refusal('/api/v1/integrations/telegram/deliveries/wait',
                                               {'x-node-plane-telegram-user-id': '1'}, 'bot'))
        self.assertEqual(relay_policy.refusal('/api/v1/telegram-bot/placement', {}, 'workstation'),
                         'not_available_through_relay')


class SignedClientTests(unittest.IsolatedAsyncioTestCase):
    """The bot's client signs what it sends, as the controller checks it."""

    async def test_the_signature_covers_what_is_sent(self):
        from telegram_client.backend import BackendClient, Signer
        seed = os.urandom(32)
        signer = Signer('0' * 32, seed)
        seen = {}

        class Response:
            status = 200
            headers = {}

            async def json(self):
                return {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *error):
                return False

        class Session:
            def request(self, method, url, **options):
                seen.update(method=method, url=url, **options)
                return Response()

        with self.assertRaises(ValueError):
            BackendClient(Session(), 'http://cab.example.com', signer=signer)
        client = BackendClient(Session(), 'https://cab.example.com', signer=signer)
        await client.request('POST', '/api/v1/me/preferences?x=%2F', telegram_user_id=7, body={'a': 1})
        self.assertEqual(str(seen['url']), 'https://cab.example.com/api/v1/me/preferences?x=%2F')
        fields = dict(part.strip().split('=', 1) for part in
                      seen['headers']['Authorization'][len('NP-Signature '):].split(','))
        from backend import client_keys
        public = Ed25519PrivateKey.from_private_bytes(seed).public_key()
        public.verify(client_keys._decode(fields['sig']), client_keys.message(
            'POST', '/api/v1/me/preferences?x=%2F', seen['data'], fields['ts'], fields['nonce'], '7'))
        self.assertEqual(seen['data'], b'{"a": 1}')
        self.assertNotIn('Bearer', seen['headers']['Authorization'])


class PackageTests(unittest.TestCase):
    def test_the_package_holds_what_the_bot_imports(self):
        uv = subprocess.run(['which', 'uv'], capture_output=True, text=True).stdout.strip()
        if not uv:
            self.skipTest('uv is not installed')
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
                'NODE_PLANE_BOT_PACKAGE_DIR': temporary, 'NODE_PLANE_UV_BIN': uv}), \
                patch.object(bot_package, 'APP_ROOT', str(ROOT)):
            (Path(temporary) / '0.0.1').mkdir()
            root = bot_package.build('9.9.9')
            self.assertEqual([p.name for p in Path(temporary).iterdir()], ['9.9.9'])
            requirements = (root / 'requirements.txt').read_text()
            self.assertIn('aiogram==', requirements)
            self.assertIn('cryptography==', requirements)
            self.assertNotIn('fastapi', requirements)
            self.assertEqual(zlib.decompress((root / 'uv.zlib').read_bytes()), Path(uv).read_bytes())
            # Every module of the bot imports from the package alone.
            check = ('import sys, pkgutil, importlib; sys.path.insert(0, sys.argv[1]); import telegram_client\n'
                     'for m in pkgutil.walk_packages(telegram_client.__path__, "telegram_client."):\n'
                     '    importlib.import_module(m.name)\n'
                     'assert all(not (getattr(m, "__file__", "") or "").startswith(sys.argv[2])'
                     ' for m in list(sys.modules.values()))')
            result = subprocess.run([sys.executable, '-c', check, str(root / 'app'), str(ROOT / 'app')],
                                    cwd=temporary, capture_output=True, text=True,
                                    env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'})
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])


if __name__ == '__main__':
    unittest.main()
