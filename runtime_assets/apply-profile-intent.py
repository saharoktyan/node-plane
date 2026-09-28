#!/usr/bin/env python3
"""Durable agent command identity and revision fence for backend intents.

An unfinished or failed journal entry fails closed: external mutation may still
have an unknown outcome. Higher revisions require a completed predecessor.
No mutation is launched for duplicate, stale or conflicting commands.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from uuid import UUID


def validate(intent):
    expected = {'command_id', 'protocol', 'runtime_name', 'revision', 'action', 'uuid', 'short_id'}
    if set(intent) != expected or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', intent['command_id']):
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
            prior = connection.execute('SELECT fingerprint, status, response FROM commands WHERE id = ?', (intent['command_id'],)).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise ValueError('command identity conflict')
                if prior[1] == 'succeeded':
                    return json.loads(prior[2])
                raise ValueError('previous command did not complete successfully')
            # The node-wide lock serializes mutations. A crashed predecessor may
            # have orphaned an external operation: do not advance any fence.
            if connection.execute("SELECT 1 FROM commands WHERE status = 'running' LIMIT 1").fetchone():
                raise ValueError('unfinished execution requires operator reconciliation')
            target = (intent['protocol'], intent['runtime_name'])
            fence = connection.execute('SELECT revision FROM fences WHERE protocol = ? AND profile = ?', target).fetchone()
            if fence and intent['revision'] <= fence[0]:
                raise ValueError('stale or conflicting revision')
            instance_id = os.environ.get('NODE_PLANE_AGENT_INSTANCE_ID', 'standalone')
            with connection:
                connection.execute("INSERT INTO commands(id, fingerprint, status, instance_id) VALUES (?, ?, 'running', ?)", (intent['command_id'], fingerprint, instance_id))
                connection.execute('INSERT INTO fences(protocol, profile, revision, command_id, action) VALUES (?, ?, ?, ?, ?) ON CONFLICT(protocol, profile) DO UPDATE SET revision = excluded.revision, command_id = excluded.command_id, action = excluded.action', (*target, intent['revision'], intent['command_id'], intent['action']))
            try:
                response = runner(intent, lock.fileno())
                if not isinstance(response, dict) or set(response) != {'summary', 'payload_json'} or any(not isinstance(v, str) for v in response.values()):
                    raise ValueError('invalid runtime result')
            except Exception:
                # Keep running durable: a transport/script failure cannot prove
                # an external Docker/API operation stopped. Fence remains closed.
                raise
            with connection:
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
        return json.loads(row[2])
    finally:
        connection.close()


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


def prepare_decommission(path, command_id):
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
            connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
            try:
                if 'action' not in {row[1] for row in connection.execute('PRAGMA table_info(fences)')}:
                    raise ValueError('profile journal needs a fresh delete revision')
                if connection.execute("SELECT 1 FROM commands WHERE status = 'running' LIMIT 1").fetchone():
                    raise ValueError('unfinished profile command blocks decommission')
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
        if len(sys.argv) == 3 and sys.argv[1] == 'resolve':
            result = resolve_interrupted(json.loads(sys.argv[2]), '/etc/node-plane/profile-intents.sqlite3', inspect_for_repair)
            print(json.dumps(result))
            sys.exit(0)
        if len(sys.argv) == 3 and sys.argv[1] == 'prepare-decommission':
            result = prepare_decommission('/etc/node-plane/profile-intents.sqlite3', sys.argv[2])
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
