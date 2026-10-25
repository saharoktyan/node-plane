from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest

from tests import test_backend_http
from backend.authorization import PrincipalKind
from backend.profiles import ProfileRepository


class BackendProfileTests(unittest.TestCase):
    setUp = test_backend_http.BackendHTTPTests.setUp

    def prepare(self):
        self.repo = ProfileRepository(self.db)
        self.member = self.identities.resolve_telegram(102)
        self.other = self.identities.resolve_telegram(103)
        self.db.connection.execute("UPDATE backend_accounts SET status = 'approved' WHERE id = ?", (self.member.id,))
        self.headers['X-Node-Plane-Telegram-User-ID'] = '102'
        self.profile = self.repo.create_profile(runtime_name='alice', display_name='Alice', owner_account_id=self.member.id)
        self.foreign = self.repo.create_profile(runtime_name='bob', display_name='Bob', owner_account_id=self.other.id)
        self.unowned = self.repo.create_profile(runtime_name='unowned', display_name='Unowned')
        # Historical trusted profiles can have no owner; pending filtering must preserve them.
        self.db.connection.execute('UPDATE backend_profiles SET owner_account_id = NULL WHERE id = ?', (self.unowned,))

    def node(self, key, *, enabled=1, protocols=('awg', 'xray')):
        self.db.connection.execute('''INSERT INTO backend_nodes(key, title, region, flag, enabled, protocols_json, xray_transports_json, desired_revision, applied_revision)
            VALUES (?, ?, 'test', '', ?, ?, ?, 1, 1)''', (key, key, enabled, json.dumps(protocols), json.dumps(['tcp', 'xhttp'])))

    def test_pending_node_settings_and_draining_nodes_are_hidden(self):
        self.prepare()
        for key in ('ready', 'pending', 'draining'):
            self.node(key)
            self.grant(self.profile, key)
        self.db.connection.execute("UPDATE backend_nodes SET desired_revision = 2 WHERE key = 'pending'")
        self.db.connection.execute('INSERT INTO backend_node_drains(node_key, actor_id, operation_ids_json, started_at) VALUES (?, ?, ?, ?)',
            ('draining', self.admin.id, '[]', datetime.now(timezone.utc).isoformat()))
        response = self.get(f'/api/v1/profiles/{self.profile}/nodes')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([node['key'] for node in response.json()['items']], ['ready'])

    def grant(self, profile, node, kind='awg'):
        self.db.connection.execute('INSERT INTO backend_grants(profile_id, node_key, protocol) VALUES (?, ?, ?)', (profile, node, kind))

    def get(self, path, **params):
        return self.client.get(path, headers=self.headers, params=params)

    def test_member_reads_only_own_profile_and_safe_fields(self):
        self.prepare()
        response = self.get('/api/v1/me/profiles')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([p['id'] for p in response.json()['items']], [self.profile])
        item = self.get(f'/api/v1/profiles/{self.profile}').json()
        self.assertNotIn('runtime_name', item)
        for profile_id in (self.foreign, self.unowned):
            self.assertEqual(self.get(f'/api/v1/profiles/{profile_id}').status_code, 404)

    def test_admin_and_scope_checks(self):
        self.prepare()
        self.headers['X-Node-Plane-Telegram-User-ID'] = '101'
        self.assertEqual(self.get(f'/api/v1/profiles/{self.foreign}').status_code, 200)
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, frozenset({'account.self.read'}), account_id=self.admin.id)
        self.assertEqual(self.client.get(f'/api/v1/profiles/{self.foreign}', headers={'Authorization': 'Bearer ' + token}).status_code, 403)

    def test_admin_profile_list_and_grants_are_authorized(self):
        self.prepare()
        self.node('n1')
        self.grant(self.profile, 'n1', 'awg')
        self.assertEqual(self.get('/api/v1/profiles').status_code, 403)
        self.assertEqual(self.get(f'/api/v1/profiles/{self.foreign}/grants').status_code, 404)
        self.assertEqual(self.get(f'/api/v1/profiles/{self.profile}/grants').json()['items'],
                         [{'node_key': 'n1', 'protocol': 'awg'}])
        self.headers['X-Node-Plane-Telegram-User-ID'] = '101'
        page = self.get('/api/v1/profiles').json()
        self.assertEqual({item['id'] for item in page['items']},
                         {self.profile, self.unowned})
        self.assertNotIn(self.foreign, {item['id'] for item in page['items']})
        self.db.connection.execute("UPDATE backend_accounts SET status = 'approved' WHERE id = ?", (self.other.id,))
        approved_page = self.get('/api/v1/profiles').json()
        self.assertEqual({item['id'] for item in approved_page['items']},
                         {self.profile, self.foreign, self.unowned})

    def test_admin_profile_pagination_and_search_filters_pending_before_limit(self):
        self.prepare()
        self.headers['X-Node-Plane-Telegram-User-ID'] = '101'
        user_app = self.identities.resolve_telegram(201)
        self.db.connection.execute("UPDATE backend_accounts SET status = 'approved' WHERE id = ?", (user_app.id,))
        p_approved = self.repo.create_profile(runtime_name='p_approved', display_name='SearchMatch Approved', owner_account_id=user_app.id)

        user_dis = self.identities.resolve_telegram(202)
        self.db.connection.execute("UPDATE backend_accounts SET status = 'disabled' WHERE id = ?", (user_dis.id,))
        p_disabled = self.repo.create_profile(runtime_name='p_disabled', display_name='SearchMatch Disabled', owner_account_id=user_dis.id)

        user_pen1 = self.identities.resolve_telegram(203)
        p_pending1 = self.repo.create_profile(runtime_name='p_pending1', display_name='SearchMatch Pending1', owner_account_id=user_pen1.id)

        user_pen2 = self.identities.resolve_telegram(204)
        p_pending2 = self.repo.create_profile(runtime_name='p_pending2', display_name='SearchMatch Pending2', owner_account_id=user_pen2.id)

        seen = []
        cursor = None
        while True:
            params = {'limit': 1}
            if cursor:
                params['cursor'] = cursor
            page = self.get('/api/v1/profiles', **params).json()
            self.assertGreater(len(page['items']), 0, 'Pagination returned an empty page!')
            self.assertNotIn(page['items'][0]['id'], seen, 'Pagination repeated an item')
            seen.extend(item['id'] for item in page['items'])
            cursor = page.get('next_cursor')
            if not cursor:
                break

        self.assertNotIn(self.foreign, seen)
        self.assertNotIn(p_pending1, seen)
        self.assertNotIn(p_pending2, seen)

        expected = {self.profile, self.unowned, p_approved, p_disabled}
        self.assertEqual(set(seen), expected)

        search_seen = []
        search_cursor = None
        while True:
            params = {'limit': 1, 'search': 'SearchMatch'}
            if search_cursor:
                params['cursor'] = search_cursor
            page = self.get('/api/v1/profiles', **params).json()
            self.assertGreater(len(page['items']), 0, 'Search pagination returned an empty page!')
            self.assertNotIn(page['items'][0]['id'], search_seen, 'Search pagination repeated an item')
            search_seen.extend(item['id'] for item in page['items'])
            search_cursor = page.get('next_cursor')
            if not search_cursor:
                break

        self.assertEqual(set(search_seen), {p_approved, p_disabled})
        self.assertNotIn(p_pending1, search_seen)
        self.assertNotIn(p_pending2, search_seen)

        pending_only = self.get('/api/v1/profiles', search='Pending1').json()
        self.assertEqual(pending_only['items'], [])

    def test_pagination_filters_before_limit(self):
        self.prepare()
        second = self.repo.create_profile(runtime_name='alice2', display_name='Alice 2', owner_account_id=self.member.id)
        first = self.get('/api/v1/me/profiles', limit=1).json()
        self.assertEqual(len(first['items']), 1)
        self.assertIsNotNone(first['next_cursor'])
        next_page = self.get('/api/v1/me/profiles', limit=1, cursor=first['next_cursor']).json()
        self.assertEqual({first['items'][0]['id'], next_page['items'][0]['id']}, {self.profile, second})
        self.assertIsNone(next_page['next_cursor'])
        for cursor in ('not-base64', 'e30='):
            self.assertEqual(self.get('/api/v1/me/profiles', cursor=cursor).status_code, 422)
        self.assertEqual(self.get('/api/v1/me/profiles', limit=101).status_code, 422)

    def test_nodes_include_only_active_owned_grants_and_supported_protocols(self):
        self.prepare()
        for key in ('active', 'other', 'disabled', 'unassigned'):
            self.node(key, enabled=0 if key == 'disabled' else 1)
        self.grant(self.profile, 'active', 'xray')
        self.grant(self.profile, 'disabled')
        self.grant(self.foreign, 'other')
        self.node('unsupported', protocols=('xray',))
        self.grant(self.profile, 'unsupported', 'awg')
        response = self.get('/api/v1/me/nodes')
        self.assertEqual(response.status_code, 200, response.text)
        nodes = response.json()['items']
        self.assertEqual([node['key'] for node in nodes], ['active'])
        self.assertEqual(nodes[0]['protocols'], [{'kind': 'xray', 'transports': ['tcp', 'xhttp']}])
        self.assertEqual(set(nodes[0]), {'key', 'title', 'region', 'flag', 'protocols'})
        self.db.connection.execute('DELETE FROM backend_grants WHERE profile_id = ?', (self.profile,))
        self.assertEqual(self.get('/api/v1/me/nodes').json()['items'], [])

    def test_frozen_expired_and_pending_cannot_gain_node_access(self):
        self.prepare()
        self.node('active')
        self.grant(self.profile, 'active')
        self.db.connection.execute('UPDATE backend_profiles SET frozen = 1 WHERE id = ?', (self.profile,))
        self.assertEqual(self.get('/api/v1/me/nodes').json()['items'], [])
        expiry = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.db.connection.execute('UPDATE backend_profiles SET frozen = 0, expires_at = ? WHERE id = ?', (expiry, self.profile))
        self.assertEqual(self.get('/api/v1/me/nodes').json()['items'], [])
        self.db.connection.execute("UPDATE backend_accounts SET status = 'pending' WHERE id = ?", (self.member.id,))
        self.assertEqual(self.get('/api/v1/me/profiles').status_code, 403)
        self.assertEqual(self.get('/api/v1/me/nodes').status_code, 403)

    def test_profile_nodes_only_expose_its_grants_and_awg_formats(self):
        self.prepare()
        other = self.repo.create_profile(runtime_name='alice2', display_name='Alice 2',
                                         owner_account_id=self.member.id)
        self.node('awg-node', protocols=('awg',))
        self.node('other-node', protocols=('awg',))
        self.grant(self.profile, 'awg-node', 'awg')
        self.grant(other, 'other-node', 'awg')
        page = self.get(f'/api/v1/profiles/{self.profile}/nodes')
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual([item['key'] for item in page.json()['items']], ['awg-node'])
        self.assertEqual(page.json()['items'][0]['protocols'],
                         [{'kind': 'awg', 'transports': ['vpn', 'conf']}])
        self.assertEqual(self.get(f'/api/v1/profiles/{self.foreign}/nodes').status_code, 404)

    def test_node_pagination_and_cross_kind_cursor(self):
        self.prepare()
        for key in ('a', 'b'):
            self.node(key)
            self.grant(self.profile, key)
        first = self.get('/api/v1/me/nodes', limit=1).json()
        self.assertEqual(first['items'][0]['key'], 'a')
        second = self.get('/api/v1/me/nodes', limit=1, cursor=first['next_cursor']).json()
        self.assertEqual(second['items'][0]['key'], 'b')
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(self.get('/api/v1/me/profiles', cursor=first['next_cursor']).status_code, 422)

    def test_local_profile_creation_validates_owner_and_name(self):
        self.prepare()
        for name in ('../bad', 'bad name', '', 'x' * 65):
            with self.assertRaises(ValueError):
                self.repo.create_profile(runtime_name=name, display_name='Test')
        with self.assertRaises(ValueError):
            self.repo.create_profile(runtime_name='valid', display_name='Test', owner_account_id='unknown')
