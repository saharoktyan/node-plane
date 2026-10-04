"""Revision-bound Xray config issuance; the URI is built only after live checks."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import logging
import re
from urllib.parse import quote
from uuid import UUID, uuid4

from .authorization import AccessDenied, Account, Actor, ProfileResource, require_profile


def _command_key(value):
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise AccessDenied('invalid_idempotency_key', 422) from None


def _profile_resource(row):
    return ProfileResource(row['id'], row['owner_account_id'], bool(row['frozen']),
        datetime.fromisoformat(row['expires_at']) if row['expires_at'] else None)


class ConfigIssuanceService:
    def __init__(self, db, driver=None):
        self.db, self.driver = db, driver

    def initialize_schema(self):
        with self.db.transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_config_issuances (
                id TEXT PRIMARY KEY, actor_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
                command_key TEXT NOT NULL, profile_id TEXT NOT NULL REFERENCES backend_profiles(id),
                node_key TEXT NOT NULL, protocol TEXT NOT NULL, transport TEXT NOT NULL,
                profile_revision INTEGER NOT NULL, node_revision INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_executor', 'running', 'blocked', 'succeeded', 'superseded')),
                result_json TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                UNIQUE(actor_account_id, command_key)
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_backend_config_issuances_work ON backend_config_issuances(status, id)')

    @staticmethod
    def public(row):
        return {key: row[key] for key in ('id', 'profile_id', 'node_key', 'protocol',
            'transport', 'status', 'expires_at')}

    @staticmethod
    def _current(conn, profile_id, node_key, protocol, transport):
        if conn.execute("SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running') LIMIT 1").fetchone():
            raise AccessDenied('restore_in_progress',409)
        profile = conn.execute('SELECT * FROM backend_profiles WHERE id = ?', (profile_id,)).fetchone()
        if profile is None:
            raise AccessDenied('resource_not_found', 404)
        node = conn.execute('SELECT * FROM backend_nodes WHERE key = ?', (node_key,)).fetchone()
        grant = conn.execute('''SELECT 1 FROM backend_grants WHERE profile_id = ? AND node_key = ?
            AND protocol = ?''', (profile_id, node_key, protocol)).fetchone()
        if grant is None:
            raise AccessDenied('grant_revoked')
        if profile['frozen']:
            raise AccessDenied('profile_frozen')
        if profile['expires_at'] and datetime.fromisoformat(profile['expires_at']) <= datetime.now(timezone.utc):
            raise AccessDenied('profile_expired')
        if node is None or not node['enabled'] or node['desired_revision'] != node['applied_revision']:
            raise AccessDenied('node_settings_pending', 409)
        if conn.execute("SELECT 1 FROM backend_node_jobs WHERE node_key = ? AND status IN ('awaiting_executor', 'running', 'blocked')", (node_key,)).fetchone():
            raise AccessDenied('node_settings_pending', 409)
        if protocol not in json.loads(node['protocols_json']) or (
                protocol == 'xray' and transport not in json.loads(node['xray_transports_json'])):
            raise AccessDenied('config_not_supported', 422)
        if conn.execute('SELECT 1 FROM backend_node_drains WHERE node_key = ?', (node_key,)).fetchone():
            raise AccessDenied('node_draining', 409)
        if conn.execute('''SELECT 1 FROM backend_node_settings_tasks WHERE node_key = ?
            AND status IN ('awaiting_executor', 'running', 'blocked')''', (node_key,)).fetchone():
            raise AccessDenied('node_settings_pending', 409)
        latest = conn.execute('''SELECT t.action, t.status, t.result_json, o.desired_revision FROM backend_operation_tasks t
            JOIN backend_operations o ON o.id = t.operation_id
            WHERE o.profile_id = ? AND t.node_key = ? AND t.protocol = ?
            ORDER BY o.desired_revision DESC, o.created_at DESC LIMIT 1''',
            (profile_id, node_key, protocol)).fetchone()
        if latest is None or latest['desired_revision'] != profile['desired_revision'] or (
                latest['action'], latest['status']) != ('ensure', 'succeeded'):
            raise AccessDenied('profile_not_synced', 409)
        identity = conn.execute('SELECT xray_uuid, xray_short_id FROM backend_profile_identities WHERE profile_id = ?',
            (profile_id,)).fetchone()
        if identity is None:
            raise AccessDenied('profile_not_synced', 409)
        if protocol == 'awg':
            try:
                if not json.loads(latest['result_json'])['wg_conf'].startswith('[Interface]'):
                    raise ValueError()
            except (TypeError, ValueError, KeyError, AttributeError):
                raise AccessDenied('profile_not_synced', 409) from None
        return profile, node, identity, latest

    def request(self, actor, profile_id, node_key, protocol, transport, command_key):
        key = _command_key(command_key)
        if (protocol, transport) not in {('xray', 'tcp'), ('xray', 'xhttp'),
                                          ('awg', 'vpn'), ('awg', 'conf')}:
            raise AccessDenied('config_not_supported', 422)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            account = conn.execute('''UPDATE backend_accounts SET role = role WHERE id = ?
                RETURNING role, status''', (actor.account.id,)).fetchone()
            if account is None:
                raise AccessDenied('permission_denied')
            current_actor = Actor(actor.principal, Account(actor.account.id, account['role'], account['status']))
            profile = conn.execute('SELECT * FROM backend_profiles WHERE id = ?', (profile_id,)).fetchone()
            if profile is None or (profile['owner_account_id'] != actor.account.id and account['role'] != 'admin'):
                raise AccessDenied('resource_not_found', 404)
            grant = conn.execute('''SELECT 1 FROM backend_grants WHERE profile_id = ? AND node_key = ?
                AND protocol = ?''', (profile_id, node_key, protocol)).fetchone()
            require_profile(current_actor, _profile_resource(profile), action='issue_config',
                administrative=profile['owner_account_id'] != actor.account.id, grant_active=grant is not None)
            previous = conn.execute('''SELECT * FROM backend_config_issuances
                WHERE actor_account_id = ? AND command_key = ?''', (actor.account.id, key)).fetchone()
            if previous is not None:
                if (previous['profile_id'], previous['node_key'], previous['protocol'], previous['transport']) != (
                        profile_id, node_key, protocol, transport):
                    raise AccessDenied('idempotency_conflict', 409)
                return self.public(previous)
            _, node, _, _ = self._current(conn, profile_id, node_key, protocol, transport)
            now = datetime.now(timezone.utc)
            issuance_id = str(uuid4())
            conn.execute('''INSERT INTO backend_config_issuances (id, actor_account_id, command_key,
                profile_id, node_key, protocol, transport, profile_revision, node_revision,
                status, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_executor', ?, ?)''',
                (issuance_id, actor.account.id, key, profile_id, node_key, protocol, transport,
                 profile['desired_revision'], node['desired_revision'], now.isoformat(),
                 (now + timedelta(minutes=15)).isoformat()))
            row = conn.execute('SELECT * FROM backend_config_issuances WHERE id = ?', (issuance_id,)).fetchone()
            return self.public(row)

    def _authorized(self, conn, actor, issuance_id):
        row = conn.execute('SELECT * FROM backend_config_issuances WHERE id = ?', (issuance_id,)).fetchone()
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        profile = conn.execute('SELECT * FROM backend_profiles WHERE id = ?', (row['profile_id'],)).fetchone()
        if profile is None or (profile['owner_account_id'] != actor.account.id and actor.account.role != 'admin'):
            raise AccessDenied('resource_not_found', 404)
        grant = conn.execute('''SELECT 1 FROM backend_grants WHERE profile_id = ? AND node_key = ?
            AND protocol = ?''', (row['profile_id'], row['node_key'], row['protocol'])).fetchone()
        require_profile(actor, _profile_resource(profile), action='read_config',
            administrative=profile['owner_account_id'] != actor.account.id, grant_active=grant is not None)
        return row

    def get(self, actor, issuance_id):
        with self.db.connect() as conn:
            return self.public(self._authorized(conn, actor, issuance_id))

    @contextmanager
    def _driver(self):
        if self.driver is not None:
            with nullcontext(self.driver) as driver:
                yield driver
        else:
            from .driver_transport import GrpcIntentDriver, local_channel
            with local_channel('127.0.0.1:50051') as channel:
                yield GrpcIntentDriver(channel)

    @staticmethod
    def _live(driver, profile, node, identity, latest, protocol):
        node_key = node['key']
        observation = driver.inspect_node(node_key)
        config_present = 'xray_config_present' if protocol == 'xray' else 'awg_config_present'
        if (observation['health_state'] != 'running' or not observation[config_present]):
            raise ValueError('protocol runtime is not ready')
        intent = {'node_key': node_key, 'runtime_name': profile['runtime_name'],
                  'protocol': protocol, 'action': 'ensure', 'desired_revision': profile['desired_revision']}
        if protocol == 'xray':
            intent['xray'] = {'uuid': identity['xray_uuid'], 'short_id': identity['xray_short_id']}
        inspection = driver.inspect(intent)
        if not all(inspection[key] for key in ('disk_present', 'live_present',
                                              'identity_matches', 'config_available')):
            raise ValueError('profile is not live')
        if protocol == 'awg':
            stored = json.loads(latest['result_json'])['wg_conf']
            refreshed = driver.refresh_awg_config(node_key, stored,
                f'{node["title"]} AmneziaWG · {profile["display_name"]}')
            settings = json.loads(node['settings_json'])
            expected_endpoint = settings.get('awg_public_host', settings['public_host'])
            expected_port = settings.get('awg_port', 51820)
            if f'Endpoint = {expected_endpoint}:{expected_port}' not in refreshed['wg_conf'].splitlines():
                raise ValueError('AWG endpoint differs from applied node settings')
            digest = sha256((refreshed['wg_conf'] + '\0' + refreshed['vpn_key']).encode()).hexdigest()
            return {'sha256': digest}, refreshed
        metadata = driver.read_xray_public(node_key)
        settings = json.loads(node['settings_json'])
        if (metadata['sni'] != settings['xray_sni'] or
            metadata['tcp_port'] != settings['xray_tcp_port'] or
            metadata['xhttp_port'] != settings['xray_xhttp_port'] or
            metadata['xhttp_path'] != settings['xray_xhttp_path'] or
            metadata['flow'] != 'xtls-rprx-vision' or metadata['fingerprint'] != 'chrome'):
            raise ValueError('Xray runtime differs from applied node settings')
        return metadata, None

    def run_one(self, issuance_id=None):
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM backend_config_issuances WHERE status = 'awaiting_executor'" +
                (" AND id = ?" if issuance_id else " ORDER BY id LIMIT 1"),
                (issuance_id,) if issuance_id else ()).fetchone()
            if row is None:
                return False
            claimed = conn.execute("UPDATE backend_config_issuances SET status = 'running' WHERE id = ? AND status = 'awaiting_executor' RETURNING id", (row['id'],)).fetchone()
            if claimed is None:
                return False
        try:
            if datetime.fromisoformat(row['expires_at']) <= datetime.now(timezone.utc):
                raise AccessDenied('config_expired', 410)
            with self.db.connect() as conn:
                profile, node, identity, latest = self._current(conn, row['profile_id'], row['node_key'],
                    row['protocol'], row['transport'])
                if (profile['desired_revision'] != row['profile_revision'] or
                    node['desired_revision'] != row['node_revision']):
                    raise AccessDenied('config_stale', 409)
            with self._driver() as driver:
                metadata, _ = self._live(driver, profile, node, identity, latest, row['protocol'])
            with self.db.transaction() as conn:
                profile, node, _, _ = self._current(conn, row['profile_id'], row['node_key'],
                    row['protocol'], row['transport'])
                if (profile['desired_revision'] != row['profile_revision'] or
                    node['desired_revision'] != row['node_revision']):
                    raise AccessDenied('config_stale', 409)
                conn.execute("UPDATE backend_config_issuances SET status = 'succeeded', result_json = ? WHERE id = ? AND status = 'running'",
                    (json.dumps(metadata, sort_keys=True), row['id']))
        except AccessDenied:
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_config_issuances SET status = 'superseded' WHERE id = ? AND status = 'running'",
                    (row['id'],))
        except Exception as exc:
            # Never log configs, keys, or the remote exception text.
            logging.getLogger(__name__).warning('Config issuance failed: id=%s protocol=%s node=%s error=%s',
                row['id'], row['protocol'], row['node_key'], type(exc).__name__)
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_config_issuances SET status = 'blocked' WHERE id = ? AND status = 'running'",
                    (row['id'],))
        return True

    def recover(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_config_issuances SET status = 'blocked' WHERE status = 'running'")

    def artifact(self, actor, issuance_id):
        with self.db.connect() as conn:
            row = self._authorized(conn, actor, issuance_id)
            if row['status'] != 'succeeded':
                raise AccessDenied('config_not_ready', 409)
            if datetime.fromisoformat(row['expires_at']) <= datetime.now(timezone.utc):
                raise AccessDenied('config_expired', 410)
            profile, node, identity, latest = self._current(conn, row['profile_id'], row['node_key'],
                row['protocol'], row['transport'])
            if (profile['desired_revision'] != row['profile_revision'] or
                node['desired_revision'] != row['node_revision']):
                raise AccessDenied('config_stale', 409)
        try:
            with self._driver() as driver:
                metadata, refreshed = self._live(driver, profile, node, identity, latest, row['protocol'])
        except Exception:
            raise AccessDenied('node_agent_unavailable', 503) from None
        if metadata != json.loads(row['result_json']):
            raise AccessDenied('config_stale', 409)
        # Recheck after the remote read; a grant or revision may have changed.
        with self.db.connect() as conn:
            self._authorized(conn, actor, issuance_id)
            current_profile, current_node, _, _ = self._current(conn, row['profile_id'], row['node_key'],
                row['protocol'], row['transport'])
            if (current_profile['desired_revision'] != row['profile_revision'] or
                current_node['desired_revision'] != row['node_revision']):
                raise AccessDenied('config_stale', 409)
        if row['protocol'] == 'awg':
            extension = row['transport']
            safe_title = (re.sub(r'[^\w .()#-]+', '', node['title']).strip(' .')[:64]
                          or row['node_key'])
            safe_profile = (re.sub(r'[^\w .()#-]+', '', profile['display_name']).strip(' .')[:64]
                            or 'Profile')
            return {'filename': f'AmneziaWG - {safe_title} - {safe_profile}.{extension}',
                    'display_name': f'{node["title"]} AmneziaWG · {profile["display_name"]}',
                    'media_type': 'text/plain',
                    'content': refreshed['vpn_key'] if extension == 'vpn' else refreshed['wg_conf'],
                    'files': [{'filename': f'AmneziaWG - {safe_title} - {safe_profile}.{ext}',
                               'content': refreshed[key]} for ext, key in
                              (('vpn', 'vpn_key'), ('conf', 'wg_conf'))]}
        settings = json.loads(node['settings_json'])
        host = settings.get('xray_host', settings['public_host'])
        port = metadata['tcp_port'] if row['transport'] == 'tcp' else metadata['xhttp_port']
        params = (f'encryption=none&security=reality&sni={quote(metadata["sni"], safe="")}'
                  f'&fp=chrome&pbk={metadata["public_key"]}&sid={metadata["short_id"]}')
        if row['transport'] == 'tcp':
            params += '&type=tcp&flow=xtls-rprx-vision'
        else:
            params += (f'&type=xhttp&path={quote(metadata["xhttp_path"], safe="")}&mode=auto'
                       '&extra=%7B%22xmux%22%3A%7B%22maxConcurrency%22%3A%2216-32%22%7D%7D')
        display_name = f'{node["title"]} VLESS {row["transport"].upper()} · {profile["display_name"]}'
        label = quote(display_name, safe='')
        uri = f'vless://{identity["xray_uuid"]}@{host}:{port}?{params}#{label}'
        safe_title = (re.sub(r'[^\w .()#-]+', '', node['title']).strip(' .')[:64]
                      or row['node_key'])
        safe_profile = (re.sub(r'[^\w .()#-]+', '', profile['display_name']).strip(' .')[:64]
                        or 'Profile')
        return {'filename': f'VLESS - {safe_title} - {safe_profile} - {row["transport"].upper()}.txt',
                'media_type': 'text/uri-list', 'content': uri, 'display_name': display_name}
