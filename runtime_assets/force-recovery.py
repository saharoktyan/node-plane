#!/usr/bin/env python3
"""Explicit, restart-fenced cancellation of one exact node command.

Never executes VPN scripts or claims the old mutation succeeded. Tickets and
retirement receipts survive interrupted HTTP requests and agent restarts.
"""
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
from uuid import UUID


def _checked(spec):
    if set(spec) != {'ticket_id', 'node_key', 'command_id', 'kind', 'intent'}:
        raise ValueError('force_invalid_request')
    for key in ('ticket_id', 'command_id'):
        if str(UUID(spec[key])) != spec[key]:
            raise ValueError('force_invalid_identity')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', spec['node_key']):
        raise ValueError('force_invalid_identity')
    kind, intent = spec['kind'], spec['intent']
    if kind not in {'settings', 'profile', 'node'} or not isinstance(intent, dict):
        raise ValueError('force_invalid_request')
    body = intent.get('intent') if kind == 'node' else intent
    if not isinstance(body, dict) or body.get('command_id') != spec['command_id']:
        raise ValueError('force_invalid_identity')
    if type(body.get('revision')) is not int or body['revision'] < 1:
        raise ValueError('force_invalid_revision')
    if kind != 'profile' and body.get('node_key') != spec['node_key']:
        raise ValueError('force_invalid_identity')
    if kind == 'profile':
        if (set(body) != {'command_id', 'protocol', 'runtime_name', 'revision', 'action', 'uuid', 'short_id'}
                or body['protocol'] not in {'awg', 'xray'} or body['action'] not in {'ensure', 'delete'}
                or not isinstance(body['runtime_name'], str)
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', body['runtime_name'])):
            raise ValueError('force_invalid_request')
    elif kind == 'settings':
        required = {'kind', 'node_key', 'command_id', 'revision', 'protocols', 'settings'}
        if (not required <= set(body) or set(body) - (required | {'protocol_plan'})
                or body.get('kind') != 'node_settings'):
            raise ValueError('force_invalid_request')
    elif (set(intent) != {'action', 'intent'} or intent['action'] not in {
            'bootstrap', 'reinstall_keep', 'reinstall_clean', 'cleanup_runtime',
            'install_docker', 'sync_runtime', 'check_ports', 'open_ports', 'sync_env', 'sync_xray',
            'regenerate_entropy', 'reconcile_access'}):
        raise ValueError('force_invalid_request')
    if kind != 'profile':
        if (not isinstance(body.get('protocols'), list) or len(body['protocols']) > 2
                or any(p not in {'awg', 'xray'} for p in body['protocols'])
                or len(set(body['protocols'])) != len(body['protocols'])
                or not isinstance(body.get('settings'), dict)):
            raise ValueError('force_invalid_request')
        if 'protocol_plan' in body:
            plan = body['protocol_plan']
            if (not isinstance(plan, dict) or set(plan) != {'remove_protocols'}
                    or not isinstance(plan['remove_protocols'], list)
                    or any(p not in {'awg', 'xray'} for p in plan['remove_protocols'])
                    or len(set(plan['remove_protocols'])) != len(plan['remove_protocols'])):
                raise ValueError('force_invalid_request')
    encoded = json.dumps(intent, sort_keys=True) if kind == 'node' else json.dumps(intent, sort_keys=True, separators=(',', ':'))
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    binding = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return body, fingerprint, binding


def checked(spec):
    try:
        return _checked(spec)
    except (TypeError, KeyError, AttributeError):
        raise ValueError('force_invalid_request') from None


def private_directory(path):
    path = Path(path)
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('force_ticket_store_untrusted')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('force_ticket_store_untrusted')
    return path


def publish_record(directory, path, value):
    binding = value['binding']
    # Publish only a complete fsynced ticket: a crash before publication must
    # not leave an empty permanent ticket that prevents future recovery.
    with tempfile.NamedTemporaryFile(mode='w', dir=directory, delete=False) as staged:
        json.dump(value, staged, sort_keys=True)
        staged.flush()
        os.fsync(staged.fileno())
        staged_path = Path(staged.name)
    try:
        try:
            os.link(staged_path, path, follow_symlinks=False)
        except FileExistsError:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as stream:
                value = json.load(stream)
            if value.get('binding') != binding:
                raise ValueError('force_ticket_conflict')
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        staged_path.unlink(missing_ok=True)
    return value


def prepare(spec, instance, directory, journal=None):
    _, fingerprint, binding = checked(spec)
    if not instance:
        raise ValueError('force_instance_missing')
    if journal is not None and Path(journal).exists():
        journal = Path(journal)
        if journal.is_symlink():
            raise ValueError('force_journal_untrusted')
        with closing(sqlite3.connect(journal.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='commands'").fetchone()
            row = conn.execute('SELECT fingerprint,status,response FROM commands WHERE id=?', (spec['command_id'],)).fetchone() if exists else None
            if row and row[0] != fingerprint:
                raise ValueError('force_command_conflict')
            if row and row[1] not in {'running', 'superseded', 'retired'}:
                raise ValueError('force_command_already_terminal')
    directory = private_directory(directory)
    path = directory / (spec['ticket_id'] + '.json')
    value = {'binding': binding, 'old_instance': instance}
    value = publish_record(directory, path, value)
    publish_record(directory, directory / ('command-' + spec['command_id'] + '.fence'),
                   {'binding': binding, 'command_id': spec['command_id'], 'node_key': spec['node_key']})
    return {'ticket_id': spec['ticket_id'], 'node_key': spec['node_key'],
            'restart_required': value['old_instance'] == instance}


def retire(spec, instance, directory, journal):
    body, fingerprint, binding = checked(spec)
    directory = private_directory(directory)
    fd = os.open(directory / (spec['ticket_id'] + '.json'), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        ticket = json.load(stream)
    if ticket.get('binding') != binding:
        raise ValueError('force_ticket_conflict')
    fd = os.open(directory / ('command-' + spec['command_id'] + '.fence'), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        fence = json.load(stream)
    if fence.get('binding') != binding:
        raise ValueError('force_ticket_conflict')
    if not instance or ticket['old_instance'] == instance:
        raise ValueError('force_restart_required')
    journal = Path(journal)
    # The same flock is inherited by every privileged mutation subprocess.
    # A new agent alone is insufficient if an old child still owns the lock.
    fd = os.open(str(journal) + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('force_execution_still_running') from None
        if Path(str(journal) + '.disabled').exists():
            raise ValueError('force_agent_removal_pending')
        if journal.is_symlink():
            raise ValueError('force_journal_untrusted')
        with closing(sqlite3.connect(journal, timeout=2)) as conn:
            os.chmod(journal, 0o600)
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, response TEXT, instance_id TEXT NOT NULL)')
            row = conn.execute('SELECT fingerprint,status,response FROM commands WHERE id=?', (spec['command_id'],)).fetchone()
            if row and row[0] != fingerprint:
                raise ValueError('force_command_conflict')
            terminal = 'retired' if spec['kind'] == 'node' else 'superseded'
            receipt = {'ticket_id': spec['ticket_id'], 'node_key': spec['node_key'],
                       'command_id': spec['command_id'], 'kind': spec['kind'],
                       'forced': True, 'outcome_unknown': True}
            if row and row[1] == terminal:
                old = json.loads(row[2] or '{}')
                if old != receipt:
                    raise ValueError('force_already_retired')
                return receipt
            if row and row[1] != 'running':
                raise ValueError('force_command_already_terminal')
            with conn:
                if row:
                    conn.execute('UPDATE commands SET status=?,response=? WHERE id=? AND status=?',
                                 (terminal, json.dumps(receipt, sort_keys=True), spec['command_id'], 'running'))
                else:
                    conn.execute('INSERT INTO commands VALUES (?,?,?,?,?)',
                                 (spec['command_id'], fingerprint, terminal, json.dumps(receipt, sort_keys=True), instance))
                if spec['kind'] == 'settings':
                    conn.execute('CREATE TABLE IF NOT EXISTS node_settings_fence (node_key TEXT PRIMARY KEY, revision INTEGER NOT NULL, command_id TEXT NOT NULL)')
                    conn.execute('INSERT INTO node_settings_fence VALUES (?,?,?) ON CONFLICT(node_key) DO UPDATE SET revision=excluded.revision,command_id=excluded.command_id WHERE excluded.revision>node_settings_fence.revision',
                                 (spec['node_key'], body['revision'], spec['command_id']))
                elif spec['kind'] == 'profile':
                    conn.execute('CREATE TABLE IF NOT EXISTS fences (protocol TEXT, profile TEXT, revision INTEGER NOT NULL, command_id TEXT NOT NULL, action TEXT, PRIMARY KEY(protocol,profile))')
                    if 'action' not in {r[1] for r in conn.execute('PRAGMA table_info(fences)')}:
                        conn.execute('ALTER TABLE fences ADD COLUMN action TEXT')
                    conn.execute('INSERT INTO fences VALUES (?,?,?,?,?) ON CONFLICT(protocol,profile) DO UPDATE SET revision=excluded.revision,command_id=excluded.command_id,action=excluded.action WHERE excluded.revision>fences.revision',
                                 (body['protocol'], body['runtime_name'], body['revision'], spec['command_id'], body['action']))
            return receipt


if __name__ == '__main__':
    try:
        os.umask(0o077)
        phase = sys.argv[1]
        value = sys.stdin.read(131073)
        if len(value) > 131072:
            raise ValueError('force_invalid_request')
        spec = json.loads(value)
        instance = os.environ.get('NODE_PLANE_AGENT_INSTANCE_ID', '')
        directory = '/etc/node-plane/force-recovery'
        result = prepare(spec, instance, directory, '/etc/node-plane/profile-intents.sqlite3') if phase == 'prepare' else retire(spec, instance, directory, '/etc/node-plane/profile-intents.sqlite3') if phase == 'retire' else None
        if result is None:
            raise ValueError('force_invalid_phase')
        print(json.dumps(result))
    except Exception as exc:
        # Never print request payloads, exception chains, keys or identities.
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r'force_[a-z_]{1,80}', str(exc)) else 'force_recovery_failed'
        raise SystemExit(code)
