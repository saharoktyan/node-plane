#!/usr/bin/env python3
"""Durable agent command identity and revision fence for backend intents.

An unfinished or failed journal entry fails closed: external mutation may still
have an unknown outcome. Higher revisions require a completed predecessor.
No mutation is launched for duplicate, stale or conflicting commands.
"""
import fcntl
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from uuid import UUID


def validate(intent):
    expected = {'command_id', 'protocol', 'runtime_name', 'revision', 'action', 'uuid', 'short_id'}
    if set(intent) not in (expected, expected | {'lease_seconds'}) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', intent['command_id']):
        raise ValueError('invalid command identity')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', intent['runtime_name']):
        raise ValueError('invalid runtime name')
    if type(intent['revision']) is not int or intent['revision'] <= 0:
        raise ValueError('invalid revision')
    if intent['protocol'] not in ('awg', 'xray') or intent['action'] not in ('ensure', 'delete'):
        raise ValueError('invalid intent')
    if intent['protocol'] == 'xray' and intent['action'] == 'ensure':
        import uuid
        uuid.UUID(intent['uuid'])
        if not re.fullmatch(r'[0-9a-fA-F]{16}', intent['short_id']):
            raise ValueError('invalid xray identity')
    elif intent['uuid'] or intent['short_id']:
        raise ValueError('unexpected identity')
    if 'lease_seconds' in intent and (type(intent['lease_seconds']) is not int
            or intent['lease_seconds'] != 86400 or intent['action'] != 'ensure'
            or not re.fullmatch(r'tmp_[0-9a-f]{32}', intent['runtime_name'])):
        raise ValueError('invalid temporary lease')


def lease_schema(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS temporary_leases (
        protocol TEXT NOT NULL, profile TEXT NOT NULL, command_id TEXT NOT NULL,
        expires_at TEXT NOT NULL, status TEXT NOT NULL
            CHECK(status IN ('provisioning','active','revoking','revoked','expired')),
        PRIMARY KEY(protocol, profile))''')


def lease_current(connection, intent, now=None):
    lease = connection.execute('SELECT expires_at,status FROM temporary_leases WHERE protocol=? AND profile=?',
                               (intent['protocol'], intent['runtime_name'])).fetchone()
    if intent['action'] == 'ensure' and lease:
        if (lease[1] != 'active' or datetime.fromisoformat(lease[0]) <= (now or datetime.now(timezone.utc))):
            raise ValueError('temporary lease is unavailable')
        if 'lease_seconds' not in intent:
            raise ValueError('temporary identity cannot become permanent')
    return lease


def apply(intent, path, runner):
    validate(intent)
    encoded = json.dumps(intent, sort_keys=True, separators=(',', ':'))
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if Path(str(path) + '.disabled').exists():
            raise ValueError('agent removal is in progress')
        connection = sqlite3.connect(path)
        try:
            os.chmod(path, 0o600)
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, response TEXT, instance_id TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS fences (protocol TEXT, profile TEXT, revision INTEGER NOT NULL, command_id TEXT NOT NULL, action TEXT, PRIMARY KEY(protocol, profile))')
            if 'action' not in {row[1] for row in connection.execute('PRAGMA table_info(fences)')}:
                connection.execute('ALTER TABLE fences ADD COLUMN action TEXT')
            lease_schema(connection)
            prior = connection.execute('SELECT fingerprint, status, response FROM commands WHERE id = ?', (intent['command_id'],)).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise ValueError('command identity conflict')
                if prior[1] == 'succeeded':
                    lease_current(connection, intent)
                    return json.loads(prior[2])
                raise ValueError('previous command did not complete successfully')
            # The node-wide lock serializes mutations. A crashed predecessor may
            # have orphaned an external operation: do not advance any fence.
            if connection.execute("SELECT 1 FROM commands WHERE status = 'running' LIMIT 1").fetchone():
                raise ValueError('unfinished execution requires operator reconciliation')
            target = (intent['protocol'], intent['runtime_name'])
            lease = lease_current(connection, intent)
            if lease and intent['action'] == 'ensure':
                raise ValueError('temporary lease cannot be renewed')
            fence = connection.execute('SELECT revision FROM fences WHERE protocol = ? AND profile = ?', target).fetchone()
            if 'lease_seconds' in intent and fence:
                raise ValueError('temporary lease requires an unused identity')
            if fence and intent['revision'] <= fence[0]:
                raise ValueError('stale or conflicting revision')
            instance_id = os.environ.get('NODE_PLANE_AGENT_INSTANCE_ID', 'standalone')
            with connection:
                connection.execute("INSERT INTO commands(id, fingerprint, status, instance_id) VALUES (?, ?, 'running', ?)", (intent['command_id'], fingerprint, instance_id))
                connection.execute('INSERT INTO fences(protocol, profile, revision, command_id, action) VALUES (?, ?, ?, ?, ?) ON CONFLICT(protocol, profile) DO UPDATE SET revision = excluded.revision, command_id = excluded.command_id, action = excluded.action', (*target, intent['revision'], intent['command_id'], intent['action']))
                if 'lease_seconds' in intent:
                    # Persist a conservative deadline before any external change.
                    # A crash after creating the peer still leaves revocation work.
                    expires = datetime.now(timezone.utc) + timedelta(seconds=intent['lease_seconds'])
                    connection.execute('''INSERT INTO temporary_leases VALUES (?,?,?,?,'provisioning')''',
                                       (*target, intent['command_id'], expires.isoformat()))
            try:
                response = runner(intent, lock.fileno())
                if not isinstance(response, dict) or set(response) != {'summary', 'payload_json'} or any(not isinstance(v, str) for v in response.values()):
                    raise ValueError('invalid runtime result')
            except Exception:
                # Keep running durable: a transport/script failure cannot prove
                # an external Docker/API operation stopped. Fence remains closed.
                raise
            with connection:
                if 'lease_seconds' in intent:
                    expires = datetime.now(timezone.utc) + timedelta(seconds=intent['lease_seconds'])
                    payload = json.loads(response['payload_json']) if response['payload_json'] else {}
                    if not isinstance(payload, dict):
                        raise ValueError('invalid lease result')
                    response = dict(response, payload_json=json.dumps(dict(payload, expires_at=expires.isoformat()), sort_keys=True))
                    connection.execute("UPDATE temporary_leases SET status='active',expires_at=? WHERE protocol=? AND profile=?",
                                       (expires.isoformat(), *target))
                elif intent['action'] == 'delete' and lease:
                    connection.execute("UPDATE temporary_leases SET status='revoked' WHERE protocol=? AND profile=?", target)
                connection.execute("UPDATE commands SET status = 'succeeded', response = ? WHERE id = ?", (json.dumps(response), intent['command_id']))
            return response
        finally:
            connection.close()


def run(intent, lock_fd):
    root = Path(__file__).parent
    protocol, action = intent['protocol'], intent['action']
    script = 'xray-add-user-existing.sh' if protocol == 'xray' and action == 'ensure' else f'{protocol}-{"add" if action == "ensure" else "del"}-user.sh'
    args = [intent['runtime_name']]
    if protocol == 'xray' and action == 'ensure':
        args += [intent['uuid'], intent['short_id']]
    output = subprocess.run([str(root / script), *args], check=True, capture_output=True, text=True, pass_fds=(lock_fd,))
    # Keep the existing AWG output format for extraction by the agent.
    return {'summary': output.stdout.strip(), 'payload_json': ''}


def lookup(intent, path):
    """Read-only recovery; verify the entire command payload before returning it."""
    validate(intent)
    fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        row = connection.execute('SELECT fingerprint, status, response FROM commands WHERE id = ?', (intent['command_id'],)).fetchone()
        if not row or row[0] != fingerprint or row[1] != 'succeeded':
            raise ValueError('command success is not confirmed')
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='temporary_leases'").fetchone():
            lease_current(connection, intent)
        return json.loads(row[2])
    finally:
        connection.close()


def expire_leases(path, runner, now=None):
    """Local revocation independent of the controller and agent RPC service.

    Only idempotent deletes are retried. Never replay an interrupted ensure.
    The shared lock also waits for orphaned mutation subprocesses to stop.
    """
    path = Path(path)
    if not path.exists():
        return {'expired': 0, 'pending': 0}
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('expiry requires an aware UTC clock')
    with open(str(path) + '.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Decommission writes this fence only after all known peers are removed.
        # Cleanup may already have deleted the protocol scripts and containers.
        if Path(str(path) + '.disabled').exists():
            return {'expired': 0, 'pending': 0}
        with closing(sqlite3.connect(path)) as connection:
            connection.execute('PRAGMA synchronous=FULL')
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='temporary_leases'").fetchone():
                return {'expired': 0, 'pending': 0}
            expired = pending = 0
            rows = connection.execute('''SELECT protocol,profile,command_id,expires_at,status
                FROM temporary_leases WHERE status IN ('provisioning','active','revoking')
                ORDER BY expires_at,protocol,profile''').fetchall()
            for protocol, profile, command_id, deadline, status in rows:
                if status != 'revoking' and datetime.fromisoformat(deadline) > now:
                    continue
                # Reserve the identity permanently, including after a failed delete.
                with connection:
                    connection.execute("UPDATE temporary_leases SET status='revoking' WHERE protocol=? AND profile=?", (protocol, profile))
                fence = connection.execute('SELECT revision FROM fences WHERE protocol=? AND profile=?', (protocol, profile)).fetchone()
                intent = {'command_id': 'expiry:' + hashlib.sha256(command_id.encode()).hexdigest(),
                          'protocol': protocol, 'runtime_name': profile, 'revision': fence[0] + 1,
                          'action': 'delete', 'uuid': '', 'short_id': ''}
                validate(intent)
                try:
                    response = runner(intent, lock.fileno())
                    if (not isinstance(response, dict) or set(response) != {'summary', 'payload_json'}
                            or any(not isinstance(value, str) for value in response.values())):
                        raise ValueError('invalid expiry result')
                except Exception:
                    pending += 1
                    continue
                fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                with connection:
                    connection.execute('''INSERT INTO commands VALUES (?,?,'succeeded',?,?)
                        ON CONFLICT(id) DO NOTHING''', (intent['command_id'], fingerprint, json.dumps(response),
                            os.environ.get('NODE_PLANE_AGENT_INSTANCE_ID', 'standalone')))
                    connection.execute("UPDATE fences SET revision=?,command_id=?,action='delete' WHERE protocol=? AND profile=?",
                                       (intent['revision'], intent['command_id'], protocol, profile))
                    connection.execute("UPDATE commands SET status='superseded' WHERE id=? AND status='running'", (command_id,))
                    connection.execute("UPDATE temporary_leases SET status='expired' WHERE protocol=? AND profile=?", (protocol, profile))
                expired += 1
            return {'expired': expired, 'pending': pending}


def validate_node_settings(intent):
    if not isinstance(intent, dict) or set(intent) != {'kind', 'node_key', 'command_id', 'revision', 'protocols', 'settings'}:
        raise ValueError('invalid node settings intent')
    if intent['kind'] != 'node_settings' or not isinstance(intent['node_key'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', intent['node_key']):
        raise ValueError('invalid node key')
    if not isinstance(intent['command_id'], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', intent['command_id']):
        raise ValueError('invalid command identity')
    if type(intent['revision']) is not int or intent['revision'] <= 0:
        raise ValueError('invalid revision')
    protocols = intent['protocols']
    if (not isinstance(protocols, list) or not protocols or len(protocols) > 2
            or len(set(protocols)) != len(protocols) or set(protocols) - {'awg', 'xray'}):
        raise ValueError('invalid protocols')
    settings = intent['settings']
    fields = {'public_host', 'xray_host', 'xray_sni', 'xray_tcp_port', 'xray_xhttp_port',
              'xray_xhttp_path', 'awg_public_host', 'awg_port',
              'xray_fingerprint', 'awg_interface', 'awg_i1_preset', 'awg_port_mode'}
    if not isinstance(settings, dict) or set(settings) - fields:
        raise ValueError('invalid settings')
    required = {'public_host'}
    if 'xray' in protocols:
        required |= {'xray_sni', 'xray_tcp_port', 'xray_xhttp_port', 'xray_xhttp_path'}
    if 'awg' in protocols:
        required.add('awg_port')
    if not required <= set(settings):
        raise ValueError('node settings are incomplete')
    for field, value in settings.items():
        if field.endswith('_port'):
            if type(value) is not int or not 1 <= value <= 65535:
                raise ValueError('invalid port')
        elif (not isinstance(value, str) or not value or len(value) > 255
              or any(ord(char) < 33 or ord(char) > 126 for char in value)):
            raise ValueError('invalid text setting')
        if field in {'public_host', 'xray_host', 'xray_sni', 'awg_public_host'} and (
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]', value)
                or '..' in value):
            raise ValueError('invalid host setting')
        if field == 'xray_xhttp_path' and not re.fullmatch(r'/[A-Za-z0-9/_~.%+-]*', value):
            raise ValueError('invalid Xray path')
        if field == 'awg_interface' and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,14}', value):
            raise ValueError('invalid AWG interface')
        if field == 'awg_i1_preset' and value not in {'quic', 'dns', 'chaos'}:
            raise ValueError('invalid AWG preset')
        if field == 'awg_port_mode' and value not in {'auto', 'manual'}:
            raise ValueError('invalid AWG port mode')
        if field == 'xray_fingerprint' and value not in {'chrome', 'firefox', 'safari', 'ios', 'android', 'edge', 'random', 'randomized'}:
            raise ValueError('invalid fingerprint')
    if 'xray' in protocols and (settings['xray_tcp_port'] == settings['xray_xhttp_port']
                                or not settings['xray_xhttp_path'].startswith('/')):
        raise ValueError('invalid Xray ports or path')


def node_settings_digest(intent):
    value = {'protocols': sorted(intent['protocols']), 'settings': intent['settings']}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def apply_node_settings(intent, path, runner):
    """Journal one node-wide settings mutation under the profile command lock."""
    validate_node_settings(intent)
    fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if Path(str(path) + '.disabled').exists():
            raise ValueError('agent removal is in progress')
        connection = sqlite3.connect(path)
        try:
            os.chmod(path, 0o600)
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, response TEXT, instance_id TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS fences (protocol TEXT, profile TEXT, revision INTEGER NOT NULL, command_id TEXT NOT NULL, action TEXT, PRIMARY KEY(protocol, profile))')
            connection.execute('CREATE TABLE IF NOT EXISTS node_settings_fence (node_key TEXT PRIMARY KEY, revision INTEGER NOT NULL, command_id TEXT NOT NULL)')
            prior = connection.execute('SELECT fingerprint, status, response FROM commands WHERE id = ?', (intent['command_id'],)).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise ValueError('command identity conflict')
                if prior[1] == 'succeeded':
                    return json.loads(prior[2])
                raise ValueError('previous command did not complete successfully')
            if connection.execute("SELECT 1 FROM commands WHERE status = 'running' LIMIT 1").fetchone():
                raise ValueError('unfinished execution requires operator reconciliation')
            fence = connection.execute('SELECT revision FROM node_settings_fence WHERE node_key = ?', (intent['node_key'],)).fetchone()
            if fence and intent['revision'] <= fence[0]:
                raise ValueError('stale or conflicting revision')
            instance_id = os.environ.get('NODE_PLANE_AGENT_INSTANCE_ID', 'standalone')
            with connection:
                connection.execute("INSERT INTO commands(id, fingerprint, status, instance_id) VALUES (?, ?, 'running', ?)",
                                   (intent['command_id'], fingerprint, instance_id))
                connection.execute('''INSERT INTO node_settings_fence(node_key, revision, command_id)
                    VALUES (?, ?, ?) ON CONFLICT(node_key) DO UPDATE SET revision = excluded.revision,
                    command_id = excluded.command_id''', (intent['node_key'], intent['revision'], intent['command_id']))
            response = runner(intent, lock.fileno())
            if (not isinstance(response, dict) or set(response) != {'summary', 'payload_json'}
                    or any(not isinstance(value, str) for value in response.values())):
                raise ValueError('invalid node settings result')
            payload = json.loads(response['payload_json'])
            effective = intent
            if isinstance(payload, dict) and 'awg_port' in payload:
                spec = importlib.util.spec_from_file_location('awg_ports', Path(__file__).parent / 'awg_ports.py')
                ports = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(ports)
                settings = intent['settings']
                if ('awg' not in intent['protocols'] or settings.get('awg_port_mode') != 'auto'
                        or type(payload['awg_port']) is not int
                        or payload['awg_port'] not in ports.candidates(settings.get('awg_i1_preset', 'quic'), settings['awg_port'])):
                    raise ValueError('invalid selected AWG port')
                effective = dict(intent, settings=dict(settings, awg_port=payload['awg_port']))
            expected = {'node_key': intent['node_key'], 'revision': intent['revision'],
                        'settings_sha256': node_settings_digest(effective)}
            if isinstance(payload, dict) and 'awg_port' in payload:
                expected['awg_port'] = payload['awg_port']
            if payload != expected:
                raise ValueError('node settings verification mismatch')
            with connection:
                connection.execute("UPDATE commands SET status = 'succeeded', response = ? WHERE id = ?",
                                   (json.dumps(response), intent['command_id']))
            return response
        finally:
            connection.close()


def lookup_node_settings(intent, path):
    validate_node_settings(intent)
    fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        if len(sys.argv) == 2 and sys.argv[1] == 'expire-leases':
            result = expire_leases('/etc/node-plane/profile-intents.sqlite3', run)
            print(json.dumps(result))
            sys.exit(1 if result['pending'] else 0)
        row = connection.execute('SELECT fingerprint, status, response FROM commands WHERE id = ?', (intent['command_id'],)).fetchone()
        if not row or row[0] != fingerprint or row[1] != 'succeeded':
            raise ValueError('node settings success is not confirmed')
        return json.loads(row[2])
    finally:
        connection.close()


def resolve_node_settings(intent, path, inspector):
    """Retire an interrupted settings command after the old agent has stopped.

    This never reports application as successful. A new revision must verify
    the complete runtime state before the backend enables the node again.
    """
    validate_node_settings(intent)
    path = Path(path)
    fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with open(str(path) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if Path(str(path) + '.disabled').exists():
            raise ValueError('agent removal is in progress')
        connection = sqlite3.connect(path)
        try:
            row = connection.execute('SELECT fingerprint, status, response, instance_id FROM commands WHERE id = ?',
                                     (intent['command_id'],)).fetchone()
            if not row or row[0] != fingerprint:
                raise ValueError('command identity mismatch')
            if row[1] == 'superseded':
                return json.loads(row[2])
            if row[1] != 'running':
                raise ValueError('command is not interrupted')
            current_instance = os.environ.get('NODE_PLANE_AGENT_INSTANCE_ID', 'standalone')
            if not current_instance or row[3] == current_instance:
                raise ValueError('agent service restart is required before repair')
            observation = inspector(intent)
            if set(observation) != {'config_matches', 'containers_running'} or any(
                    type(value) is not bool for value in observation.values()):
                raise ValueError('live inspection unavailable')
            record = {'observation': observation, 'resolved_at': datetime.now(timezone.utc).isoformat()}
            with connection:
                changed = connection.execute("UPDATE commands SET status = 'superseded', response = ? WHERE id = ? AND status = 'running'",
                                             (json.dumps(record, sort_keys=True), intent['command_id']))
                if changed.rowcount != 1:
                    raise ValueError('command changed during repair')
            return record
        finally:
            connection.close()


def verify_node_settings_config(intent, xray_path, awg_path):
    settings = intent['settings']
    if 'xray' in intent['protocols']:
        data = json.loads(Path(xray_path).read_text())
        inbounds = {item.get('tag'): item for item in data.get('inbounds', [])}
        for tag, port in (('reality-tcp', settings['xray_tcp_port']),
                          ('reality-xhttp', settings['xray_xhttp_port'])):
            inbound = inbounds[tag]
            reality = inbound['streamSettings']['realitySettings']
            if (inbound['port'] != port or reality['serverNames'] != [settings['xray_sni']]
                    or reality['dest'] != settings['xray_sni'] + ':443'):
                raise ValueError('Xray settings verification failed')
        if inbounds['reality-xhttp']['streamSettings']['xhttpSettings']['path'] != settings['xray_xhttp_path']:
            raise ValueError('Xray path verification failed')
    if 'awg' in intent['protocols']:
        awg_config = Path(awg_path).read_text()
        if not re.search(r'(?m)^ListenPort\s*=\s*' + str(settings['awg_port']) + r'\s*$', awg_config):
            raise ValueError('AWG port verification failed')


def inspect_node_settings_for_repair(intent):
    env_path = Path('/etc/node-plane/node.env')
    paths = subprocess.run(['bash', '-c', 'source "$1"; printf "%s\n%s\n%s\n%s\n" "${XRAY_CONFIG:-/opt/node-plane-runtime/xray/config.json}" "${AWG_CONFIG:-/opt/node-plane-runtime/amnezia-awg/data/wg0.conf}" "${XRAY_CONTAINER_NAME:-xray}" "${AWG_CONTAINER_NAME:-amnezia-awg}"',
                            'node-settings-paths', str(env_path)], check=True, capture_output=True,
                           text=True, timeout=30).stdout.splitlines()
    if len(paths) != 4:
        raise ValueError('invalid node environment')
    try:
        verify_node_settings_config(intent, paths[0], paths[1])
        matches = True
    except (OSError, ValueError, KeyError, TypeError):
        matches = False
    containers = ([paths[2]] if 'xray' in intent['protocols'] else []) + ([paths[3]] if 'awg' in intent['protocols'] else [])
    running = all(subprocess.run(['docker', 'inspect', '-f', '{{.State.Running}}', name],
                                 capture_output=True, text=True, timeout=30).stdout.strip() == 'true'
                  for name in containers)
    return {'config_matches': matches, 'containers_running': running}


def run_node_settings(intent, lock_fd, *, env_path=Path('/etc/node-plane/node.env'), root=Path(__file__).parent):
    """Apply to an already bootstrapped node; no implicit install or key reset."""
    settings = intent['settings']
    env_path = Path(env_path)
    root = Path(root)
    if not env_path.is_file() or not (root / 'apply-node-settings.sh').is_file():
        raise ValueError('node runtime is not bootstrapped')
    paths = subprocess.run(['bash', '-c', 'source "$1"; printf "%s\n%s\n%s\n%s\n" "${XRAY_CONFIG:-/opt/node-plane-runtime/xray/config.json}" "${AWG_CONFIG:-/opt/node-plane-runtime/amnezia-awg/data/wg0.conf}" "${XRAY_CONTAINER_NAME:-xray}" "${AWG_CONTAINER_NAME:-amnezia-awg}"',
                            'node-settings-paths', str(env_path)], check=True, capture_output=True, text=True).stdout.splitlines()
    if len(paths) != 4:
        raise ValueError('invalid node environment')
    xray_path, awg_path = map(Path, paths[:2])
    selected_port = None
    if 'awg' in intent['protocols'] and settings.get('awg_port_mode') == 'auto':
        spec = importlib.util.spec_from_file_location('awg_ports', root / 'awg_ports.py')
        ports = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ports)
        selected_port = ports.select_port(settings.get('awg_i1_preset', 'quic'),
            settings['awg_port'], awg_path, paths[3])
        settings = dict(settings, awg_port=selected_port)
        intent = dict(intent, settings=settings)
    # Existing environment remains intact. Replace only backend-owned public
    # settings, using the same shell quoting as the legacy env renderer.
    replacements = {
        'SERVER_KEY': intent['node_key'],
        'AWG_SERVER_IP': settings.get('awg_public_host', settings['public_host']),
        'AWG_SERVER_PORT': str(settings.get('awg_port', 51820)),
        'AWG_IFACE': settings.get('awg_interface', 'wg0'),
        'AWG_I1_PRESET': settings.get('awg_i1_preset', 'quic'),
        'XRAY_FP': settings.get('xray_fingerprint', 'chrome'),
    }
    new_awg_path = awg_path.with_name(settings.get('awg_interface', 'wg0') + '.conf')
    if awg_path != new_awg_path and awg_path.exists():
        # Preserve server and peer identities when changing interface names.
        new_awg_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(awg_path, new_awg_path)
    awg_path = new_awg_path
    replacements['AWG_CONFIG'] = str(awg_path)
    original = env_path.read_text()
    lines = [line for line in original.splitlines() if not any(
        re.match(r'^\s*' + name + r'=', line) for name in replacements)]
    lines += [f'{name}={shlex.quote(value)}' for name, value in replacements.items()]
    with tempfile.NamedTemporaryFile(mode='w', dir=env_path.parent, delete=False) as staged:
        staged.write('\n'.join(lines) + '\n')
        staged.flush()
        os.fsync(staged.fileno())
        staged_path = Path(staged.name)
    staged_path.chmod(0o600)
    os.replace(staged_path, env_path)
    directory = os.open(env_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    # A freshly prepared agent has the runtime scripts and node.env but no
    # protocol configs. Initialize only missing configs under this same durable
    # command identity; existing keys must never be regenerated on retry.
    if 'xray' in intent['protocols']:
        if not xray_path.exists():
            subprocess.run([str(root / 'init-xray.sh'), str(xray_path), settings['public_host'],
                            settings['xray_sni'], str(settings['xray_tcp_port']),
                            str(settings['xray_xhttp_port']), settings['xray_xhttp_path'],
                            'xtls-rprx-vision', 'ghcr.io/xtls/xray-core:26.3.27'],
                           check=True, capture_output=True, text=True, pass_fds=(lock_fd,))
        json.loads(xray_path.read_text())
    if 'awg' in intent['protocols']:
        if not awg_path.exists():
            subprocess.run([str(root / 'init-awg.sh')], check=True, capture_output=True,
                           text=True, pass_fds=(lock_fd,))
        awg_path.read_text()
    rules = []
    if 'xray' in intent['protocols']:
        rules += [f"{settings['xray_tcp_port']}/tcp", f"{settings['xray_xhttp_port']}/tcp"]
    if 'awg' in intent['protocols']:
        rules.append(f"{settings['awg_port']}/udp")
    if shutil.which('ufw'):
        for rule in rules:
            subprocess.run(['ufw', 'allow', rule], check=True, capture_output=True, text=True,
                           pass_fds=(lock_fd,))
        subprocess.run(['ufw', 'reload'], check=True, capture_output=True, text=True,
                       pass_fds=(lock_fd,))
    args = [str(root / 'apply-node-settings.sh'),
            str(xray_path), settings.get('xray_sni', ''),
            str(settings.get('xray_tcp_port', 443)), str(settings.get('xray_xhttp_port', 8443)),
            settings.get('xray_xhttp_path', '/assets'),
            str('xray' in intent['protocols']).lower(), str('awg' in intent['protocols']).lower()]
    subprocess.run(args, check=True, capture_output=True, text=True, pass_fds=(lock_fd,))
    # The apply script only reports success after protocol config edits and
    # deployment. Read the resulting files independently before acknowledgment.
    verify_node_settings_config(intent, xray_path, awg_path)
    for container in ([paths[2]] if 'xray' in intent['protocols'] else []) + ([paths[3]] if 'awg' in intent['protocols'] else []):
        state = subprocess.run(['docker', 'inspect', '-f', '{{.State.Running}}', container],
                               check=True, capture_output=True, text=True).stdout.strip()
        if state != 'true':
            raise ValueError('managed protocol container is not running')
    payload = {'node_key': intent['node_key'], 'revision': intent['revision'],
               'settings_sha256': node_settings_digest(intent)}
    if selected_port is not None:
        payload['awg_port'] = selected_port
    return {'summary': 'node settings applied and verified', 'payload_json': json.dumps(payload, sort_keys=True)}


def _decommission_id(command_id):
    try:
        if str(UUID(command_id)) != command_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid decommission command identity') from None
    return command_id


def verify_decommission(path, command_id):
    """Read a durable decommission fence; the caller must hold the node lock."""
    _decommission_id(command_id)
    marker = Path(str(path) + '.disabled')
    try:
        record = json.loads(marker.read_text())
    except (OSError, ValueError):
        raise ValueError('decommission fence missing or invalid') from None
    if record != {'kind': 'decommission', 'command_id': command_id}:
        raise ValueError('decommission command identity conflict')
    return record


def revoke_journal_profiles(path, command_id, runner, lock_fd):
    """Durably revoke historical targets under the existing node-wide lock."""
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('PRAGMA synchronous=FULL')
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'commands' not in tables:
            raise ValueError('invalid command journal; decommission refused')
        if connection.execute("SELECT 1 FROM commands WHERE status = 'running' LIMIT 1").fetchone():
            raise ValueError('unfinished profile command blocks decommission')
        if 'fences' not in tables:
            return
        if 'action' not in {row[1] for row in connection.execute('PRAGMA table_info(fences)')}:
            connection.execute('ALTER TABLE fences ADD COLUMN action TEXT')
        targets = connection.execute('''SELECT f.protocol, f.profile, f.revision, f.action, c.status
            FROM fences f LEFT JOIN commands c ON c.id=f.command_id
            ORDER BY f.protocol, f.profile''').fetchall()
        intents = []
        for protocol, profile, revision, action, status in targets:
            if action == 'delete' and status == 'succeeded':
                continue
            if status != 'succeeded':
                raise ValueError('unconfirmed historical profile blocks decommission')
            intent = {'command_id': f'{command_id}:{protocol}:{profile}',
                      'protocol': protocol, 'runtime_name': profile,
                      'revision': revision + 1, 'action': 'delete', 'uuid': '', 'short_id': ''}
            validate(intent)
            intents.append(intent)
        # Validate every candidate before the first mutation.
        connection.commit()
        for intent in intents:
            encoded = json.dumps(intent, sort_keys=True, separators=(',', ':'))
            fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
            with connection:
                connection.execute("""INSERT INTO commands(id, fingerprint, status, response, instance_id)
                    VALUES (?, ?, 'running', NULL, ?)""",
                    (intent['command_id'], fingerprint,
                     os.environ.get('NODE_PLANE_AGENT_INSTANCE_ID', 'standalone')))
                connection.execute('''UPDATE fences SET revision=?, command_id=?, action='delete'
                    WHERE protocol=? AND profile=?''', (intent['revision'], intent['command_id'],
                                                        intent['protocol'], intent['runtime_name']))
            # Any failure leaves a durable running command, never a guessed success.
            response = runner(intent, lock_fd)
            if (not isinstance(response, dict) or set(response) != {'summary', 'payload_json'}
                    or any(not isinstance(value, str) for value in response.values())):
                raise ValueError('invalid runtime result')
            with connection:
                connection.execute("UPDATE commands SET status='succeeded', response=? WHERE id=?",
                                   (json.dumps(response), intent['command_id']))


def prepare_decommission(path, command_id, runner=None):
    """Fence all mutations only after every known profile is durably deleted.

    A pre-upgrade journal without an action column must receive a new delete
    intent before decommission. This prevents guessing from old fence records.
    """
    _decommission_id(command_id)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = Path(str(path) + '.disabled')
        if marker.exists():
            return verify_decommission(path, command_id)
        if path.exists():
            if runner is not None:
                revoke_journal_profiles(path, command_id, runner, lock.fileno())
            connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
            try:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if 'commands' not in tables:
                    raise ValueError('invalid command journal; decommission refused')
                if connection.execute("SELECT 1 FROM commands WHERE status = 'running' LIMIT 1").fetchone():
                    raise ValueError('unfinished profile command blocks decommission')
                # Docker/bootstrap uses the shared commands journal before any
                # profile has existed. Absence of profile fences is not a pending
                # revocation. Old nonempty fences still require a fresh delete.
                if 'fences' in tables and connection.execute('SELECT 1 FROM fences LIMIT 1').fetchone():
                    if 'action' not in {row[1] for row in connection.execute('PRAGMA table_info(fences)')}:
                        raise ValueError('profile journal needs a fresh delete revision')
                    if connection.execute('''SELECT 1 FROM fences f LEFT JOIN commands c ON c.id = f.command_id
                        WHERE f.action IS NULL OR f.action != 'delete' OR c.status IS NULL OR c.status != 'succeeded'
                        LIMIT 1''').fetchone():
                        raise ValueError('managed profiles remain on the node')
            finally:
                connection.close()
        record = {'kind': 'decommission', 'command_id': command_id}
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(record, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return record


def resolve_interrupted(intent, path, inspector):
    """After an agent restart, observe live state and retire an orphaned command.

    The operator must first restart the agent service to quiesce its old process
    group. We never infer resolution from disk config alone, nor re-run the old
    mutation. The next backend revision performs the actual repair.
    """
    validate(intent)
    path = Path(path)
    fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with open(str(path) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if Path(str(path) + '.disabled').exists():
            raise ValueError('agent removal is in progress')
        connection = sqlite3.connect(path)
        try:
            row = connection.execute('SELECT fingerprint, status, response, instance_id FROM commands WHERE id = ?', (intent['command_id'],)).fetchone()
            if not row or row[0] != fingerprint:
                raise ValueError('command identity mismatch')
            if row[1] == 'superseded':
                return json.loads(row[2])
            if row[1] != 'running':
                raise ValueError('command is not interrupted')
            current_instance = os.environ.get('NODE_PLANE_AGENT_INSTANCE_ID', 'standalone')
            if not current_instance or row[3] == current_instance:
                raise ValueError('agent service restart is required before repair')
            observation = inspector(intent)
            required = {'disk_present', 'live_present', 'identity_matches', 'config_available'}
            if set(observation) != required or any(type(value) is not bool for value in observation.values()):
                raise ValueError('live inspection unavailable')
            record = {'observation': observation, 'resolved_at': datetime.now(timezone.utc).isoformat()}
            with connection:
                changed = connection.execute("UPDATE commands SET status = 'superseded', response = ? WHERE id = ? AND status = 'running'",
                                             (json.dumps(record, sort_keys=True), intent['command_id']))
                if changed.rowcount != 1:
                    raise ValueError('command changed during repair')
            return record
        finally:
            connection.close()


def inspect_for_repair(intent):
    arguments = [intent['protocol'], intent['runtime_name']]
    if intent['protocol'] == 'xray':
        # Deletion intents omit XraySpec. For repair, compare presence without
        # claiming an identity match; the new revision will enforce desired state.
        arguments.append(intent['uuid'])
    result = subprocess.run([str(Path(__file__).with_name('inspect-profile.sh')), *arguments],
                            check=True, capture_output=True, text=True, timeout=30)
    return json.loads(result.stdout)


def guard_legacy(path, protocol=None, profile=None):
    """Refuse old mutation paths once the explicit backend owns the target.

    No journal means a legacy-only node. A present but unreadable journal is
    fail-closed, since it may contain a fence we cannot safely inspect.
    """
    path = Path(path)
    if Path(str(path) + '.disabled').exists():
        raise ValueError('agent removal is in progress')
    if not path.exists():
        return
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        if protocol is None:
            owned = connection.execute('SELECT 1 FROM fences LIMIT 1').fetchone()
            if (not owned and connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'node_settings_fence'").fetchone()):
                owned = connection.execute('SELECT 1 FROM node_settings_fence LIMIT 1').fetchone()
        else:
            if (protocol not in {'awg', 'xray'} or not isinstance(profile, str)
                    or not profile or len(profile) > 128 or '\0' in profile):
                raise ValueError('invalid legacy target')
            owned = connection.execute('SELECT 1 FROM fences WHERE protocol = ? AND profile = ?', (protocol, profile)).fetchone()
        if owned:
            raise ValueError('target is managed by the explicit backend')
    finally:
        connection.close()


def run_legacy(path, scope, profile, script, args):
    """Serialize an old mutation with explicit commands before checking fences."""
    profile_scripts = {
        'xray-add-user-existing.sh': 'xray', 'xray-del-user.sh': 'xray',
        'awg-add-user.sh': 'awg', 'awg-del-user.sh': 'awg',
    }
    maintenance_scripts = {
        'init-xray.sh', 'deploy-xray.sh', 'init-awg.sh', 'deploy-awg.sh',
        'apply-node-settings.sh', 'regenerate-awg-entropy.sh', 'sync-xray.sh',
    }
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError('invalid runtime arguments')
    if scope == 'all':
        if script not in maintenance_scripts or profile:
            raise ValueError('invalid maintenance command')
    elif (profile_scripts.get(script) != scope or not isinstance(profile, str)
          or not profile or len(profile) > 128 or '\0' in profile):
        raise ValueError('invalid profile command')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        guard_legacy(path, None if scope == 'all' else scope,
                     None if scope == 'all' else profile)
        result = subprocess.run([str(Path(__file__).parent / script), *args],
                                check=True, capture_output=True, text=True,
                                pass_fds=(lock.fileno(),))
        return result.stdout.strip()


if __name__ == '__main__':
    os.umask(0o077)
    try:
        if len(sys.argv) == 3 and sys.argv[1] == 'apply-node-settings':
            result = apply_node_settings(json.loads(sys.argv[2]), '/etc/node-plane/profile-intents.sqlite3', run_node_settings)
            print(json.dumps(result))
            sys.exit(0)
        if len(sys.argv) == 3 and sys.argv[1] == 'lookup-node-settings':
            result = lookup_node_settings(json.loads(sys.argv[2]), '/etc/node-plane/profile-intents.sqlite3')
            print(json.dumps(result))
            sys.exit(0)
        if len(sys.argv) == 3 and sys.argv[1] == 'resolve-node-settings':
            result = resolve_node_settings(json.loads(sys.argv[2]), '/etc/node-plane/profile-intents.sqlite3', inspect_node_settings_for_repair)
            print(json.dumps(result))
            sys.exit(0)
        if len(sys.argv) == 3 and sys.argv[1] == 'resolve':
            result = resolve_interrupted(json.loads(sys.argv[2]), '/etc/node-plane/profile-intents.sqlite3', inspect_for_repair)
            print(json.dumps(result))
            sys.exit(0)
        if len(sys.argv) == 3 and sys.argv[1] == 'prepare-decommission':
            result = prepare_decommission('/etc/node-plane/profile-intents.sqlite3', sys.argv[2], run)
            print(json.dumps(result))
            sys.exit(0)
        if len(sys.argv) == 3 and sys.argv[1] == 'verify-decommission':
            result = verify_decommission('/etc/node-plane/profile-intents.sqlite3', sys.argv[2])
            print(json.dumps(result))
            sys.exit(0)
        if len(sys.argv) >= 5 and sys.argv[1] == 'run-legacy':
            result = run_legacy('/etc/node-plane/profile-intents.sqlite3',
                                sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:])
            print(result)
            sys.exit(0)
        if len(sys.argv) == 2 and sys.argv[1] == 'guard-maintenance':
            guard_legacy('/etc/node-plane/profile-intents.sqlite3')
            result = {'status': 'allowed'}
        elif len(sys.argv) == 4 and sys.argv[1] == 'guard-legacy':
            guard_legacy('/etc/node-plane/profile-intents.sqlite3', sys.argv[2], sys.argv[3])
            result = {'status': 'allowed'}
        elif len(sys.argv) == 3 and sys.argv[1] == 'lookup':
            result = lookup(json.loads(sys.argv[2]), '/etc/node-plane/profile-intents.sqlite3')
        elif len(sys.argv) == 2:
            result = apply(json.loads(sys.argv[1]), '/etc/node-plane/profile-intents.sqlite3', run)
        else:
            raise ValueError('invalid arguments')
        print(json.dumps(result))
    except Exception:
        raise SystemExit('profile intent could not be applied; reconciliation required')
