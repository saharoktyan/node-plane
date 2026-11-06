"""Telegram in the web cabinet: Login Widget and Mini App sign-in, and moving a Telegram between accounts."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4
import unittest

from backend import sessions
from backend import telegram_sign_in as telegram
from backend.authorization import AccessDenied, PrincipalKind
from tests import test_backend_http as http_fixture

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
TOKEN = '123456:' + 'A' * 35


def widget(user_id, now=NOW, token=TOKEN, **fields):
    data = {'id': user_id, 'first_name': 'Sahar', 'username': 'saharoktyan', 'auth_date': int(now.timestamp()), **fields}
    check = '\n'.join(f'{key}={data[key]}' for key in sorted(data))
    data['hash'] = hmac.new(hashlib.sha256(token.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    return data


def init_data(user_id, now=NOW, token=TOKEN):
    fields = {'user': json.dumps({'id': user_id, 'username': 'saharoktyan'}), 'auth_date': str(int(now.timestamp())),
              'query_id': 'AAE'}
    check = '\n'.join(f'{key}={fields[key]}' for key in sorted(fields))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class TelegramSignInTests(unittest.TestCase):
    setUp_http = http_fixture.BackendHTTPTests.setUp
    register_identity = http_fixture.BackendHTTPTests.register
    provision = http_fixture.BackendHTTPTests.provision

    def setUp(self):
        self.setUp_http()
        token = patch.object(telegram, 'bot_token', return_value=TOKEN)
        token.start()
        self.addCleanup(token.stop)

    def code(self, call, *args, **kwargs):
        with self.assertRaises(AccessDenied) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def cabinet_account(self, login='anna'):
        account_id = str(uuid4())
        from backend import logins
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_accounts(id, status) VALUES (?, 'approved')", (account_id,))
            logins.claim(conn, account_id, login, 'first', NOW)
        return account_id

    def owner(self, user_id):
        account = self.identities.find_telegram_account(user_id)
        return account.id if account else None

    def test_signatures_freshness_and_single_use(self):
        self.register_identity(202)
        member = self.provision(202).json()['account']['id']
        sign = lambda kind, data, now=NOW: telegram.sign_in(self.db, kind, data, device='x', source='s', now=now)
        result = sign('login', widget(202))
        self.assertEqual(self.credentials.authenticate('Bearer ' + result['access_token'], now=NOW).account_id, member)
        sign('webapp', init_data(202))
        # Used once.
        self.assertEqual(self.code(sign, 'webapp', init_data(202)), 'telegram_auth_invalid')
        self.assertEqual(self.code(sign, 'login', widget(202, token='999:' + 'B' * 35)), 'telegram_auth_invalid')
        tampered = widget(202)
        tampered['id'] = 101
        self.assertEqual(self.code(sign, 'login', tampered), 'telegram_auth_invalid')
        self.assertEqual(self.code(sign, 'login', widget(202, now=NOW - timedelta(minutes=11))),
                         'telegram_auth_invalid')
        # The Mini App's data lives longer.
        sign('webapp', init_data(202, now=NOW - timedelta(minutes=50)))
        self.assertEqual(self.code(sign, 'login', widget(303)), 'telegram_not_registered')
        with self.db.connect() as conn:
            self.assertEqual({s['method'] for s in sessions.listed(conn, member, now=NOW)}, {'telegram'})
        with patch.object(telegram, 'bot_token', return_value=None):
            self.assertEqual(self.code(sign, 'login', widget(202, auth_date=1)), 'telegram_disabled')

    def test_a_free_telegram_links_and_an_empty_account_moves(self):
        anna = self.cabinet_account()
        preview = telegram.link_preview(self.db, anna, kind='login', data=widget(404), source='s', now=NOW)
        self.assertEqual((preview['state'], preview['username']), ('free', 'saharoktyan'))
        account = telegram.link_confirm(self.db, anna, preview['link_id'], now=NOW)
        self.assertEqual(account['telegram_user_id'], 404)
        self.assertEqual(self.owner(404), anna)
        # An account visiting the bot without access moves to the cabinet account by a code.
        self.register_identity(505)
        empty = self.identities.resolve_telegram(505).id
        bea = self.cabinet_account('bea')
        code = telegram.link_code(self.db, '505', now=NOW)['code']
        self.assertEqual(self.code(telegram.link_preview, self.db, bea, code='WRONG-ONE', source='s', now=NOW),
                         'telegram_code_invalid')
        preview = telegram.link_preview(self.db, bea, code=code.lower(), source='s', now=NOW)
        self.assertEqual(preview['state'], 'other_empty')
        # The code is used up.
        self.assertEqual(self.code(telegram.link_preview, self.db, bea, code=code, source='s', now=NOW),
                         'telegram_code_invalid')
        telegram.link_confirm(self.db, bea, preview['link_id'], now=NOW)
        self.assertEqual(self.owner(505), bea)
        with self.db.connect() as conn:
            self.assertIsNotNone(conn.execute('SELECT completed_at FROM backend_account_deletions WHERE account_id=?',
                                              (empty,)).fetchone()['completed_at'])

    def test_an_account_with_access_keeps_its_telegram(self):
        self.register_identity(606)
        self.provision(606)
        cara = self.cabinet_account('cara')
        self.assertEqual(self.code(telegram.link_code, self.db, '606', now=NOW), 'telegram_account_has_access')
        preview = telegram.link_preview(self.db, cara, kind='webapp', data=init_data(606), source='s', now=NOW)
        self.assertEqual(preview['state'], 'other_with_access')
        self.assertEqual(self.code(telegram.link_confirm, self.db, cara, preview['link_id'], now=NOW),
                         'telegram_account_has_access')
        self.assertNotEqual(self.owner(606), cara)
        # An account links one Telegram.
        own = telegram.link_preview(self.db, cara, kind='login', data=widget(707), source='s', now=NOW)
        telegram.link_confirm(self.db, cara, own['link_id'], now=NOW)
        other = telegram.link_preview(self.db, cara, kind='login', data=widget(808), source='s', now=NOW)
        self.assertEqual(other['state'], 'account_has_telegram')
        # A link is the account's own.
        dora = self.cabinet_account('dora')
        self.assertEqual(self.code(telegram.link_confirm, self.db, dora, other['link_id'], now=NOW),
                         'telegram_link_invalid')

    def test_api(self):
        self.register_identity(909)
        member = self.provision(909).json()['account']['id']
        response = self.client.post('/api/v1/sessions/telegram', json={'kind': 'webapp', 'data': init_data(909, now=datetime.now(timezone.utc))})
        self.assertEqual(response.status_code, 201, response.text)
        anna = self.cabinet_account()
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, frozenset({'account.self.read',
                                          'account.self.preferences.write'}), account_id=anna)
        own = {'Authorization': 'Bearer ' + token}
        self.register_identity(910)
        bot = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '910'}
        code = self.client.post('/api/v1/me/telegram/link-code', headers=bot)
        self.assertEqual(code.status_code, 201, code.text)
        # The bot does not confirm; the cabinet account does.
        member_bot = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '909'}
        response = self.client.post('/api/v1/me/telegram/link/preview', headers=member_bot,
                                    json={'code': code.json()['code']})
        self.assertEqual(response.json()['error']['code'], 'telegram_link_not_through_adapter')
        preview = self.client.post('/api/v1/me/telegram/link/preview', headers=own, json={'code': code.json()['code']})
        self.assertEqual(preview.json()['state'], 'free', preview.text)
        response = self.client.post('/api/v1/me/telegram/link', headers=own, json={'link_id': preview.json()['link_id']})
        self.assertEqual(response.json()['telegram_user_id'], 910, response.text)
        response = self.client.post('/api/v1/me/telegram/link-code', headers=member_bot)
        self.assertEqual(response.json()['error']['code'], 'telegram_account_has_access')
        self.assertIsNotNone(member)


if __name__ == '__main__':
    unittest.main()
