"""Trusted local bootstrap for the workstation assistant, never a public API.

Read one versioned JSON request from stdin and write one JSON response to stdout.
The authenticate response contains a bearer secret; callers must not log it.
Run through an SSH root session or passwordless sudo with the installed venv.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
import stat
import sys
from uuid import UUID, uuid4

from backend.authorization import (
    AccessDenied, PrincipalKind, require_permission, resolve_actor, validate_telegram_id,
)
from backend.credentials import CredentialService, TOKEN_PATTERN
from backend.identity_repository import SQLIdentityRepository


VERSION = 1
LEGACY_SCOPES = frozenset({'maintenance.manage', 'settings.manage'})
SCOPES = LEGACY_SCOPES | {'nodes.manage'}
LIFETIME = timedelta(hours=1)
MAX_REQUEST_BYTES = 16_384
MAX_SESSION_BYTES = 16_384


class WorkstationError(Exception):
    def __init__(self, code: str, *, choices: list[dict] | None = None):
        super().__init__(code)
        self.code = code
        self.choices = choices


def _uuid(value) -> str:
    if not isinstance(value, str):
        raise WorkstationError('invalid_request')
    try:
        parsed = str(UUID(value))
    except ValueError:
        raise WorkstationError('invalid_request') from None
    if parsed != value:
        raise WorkstationError('invalid_request')
    return parsed


def validate_request(request) -> dict:
    if not isinstance(request, dict) or type(request.get('version')) is not int or request['version'] != VERSION:
        raise WorkstationError('invalid_request')
    action = request.get('action')
    allowed = {'version', 'action'}
    if action in {'authenticate', 'revoke', 'lookup-update', 'lookup-node', 'audit-enrollment', 'revoke-access', 'restore-access'}:
        _uuid(request.get('session_id'))
        allowed.add('session_id')
    elif action != 'list':
        raise WorkstationError('invalid_request')
    if action == 'audit-enrollment':
        _uuid(request.get('command_id'))
        allowed |= {'command_id', 'target', 'key_fingerprint', 'outcome'}
        if (not isinstance(request.get('target'), str)
                or len(request['target']) > 340
                or not re.fullmatch(r'[A-Za-z0-9_.:@\[\]-]+', request['target'])
                or not isinstance(request.get('key_fingerprint'), str)
                or not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', request['key_fingerprint'])
                or not isinstance(request.get('outcome'), str)
                or request['outcome'] not in {'admitted', 'succeeded', 'unconfirmed'}):
            raise WorkstationError('invalid_request')
    elif action in {'lookup-update', 'lookup-node'}:
        _uuid(request.get('command_id'))
        allowed.add('command_id')
    elif action in {'authenticate', 'revoke-access', 'restore-access'}:
        allowed |= {'account_id', 'telegram_id', 'attribution'}
        if 'attribution' in request:
            from backend.workstation_audit import WorkstationAudit
            try:
                WorkstationAudit.validate_context(request['attribution'])
            except ValueError:
                raise WorkstationError('invalid_request') from None
        if 'account_id' in request and 'telegram_id' in request:
            raise WorkstationError('invalid_request')
        if 'account_id' in request:
            _uuid(request['account_id'])
        if 'telegram_id' in request:
            try:
                validate_telegram_id(request['telegram_id'])
            except AccessDenied:
                raise WorkstationError('invalid_request') from None
    if request.keys() - allowed:
        raise WorkstationError('invalid_request')
    return request


class SessionStore:
    """Root-private files and an exclusive local lock preserve retry identity."""

    def __init__(self, directory: Path):
        self.directory = directory.absolute()
        self.owner_uid = os.geteuid()
        # The root inode can be mapped to a different UID inside a user namespace.
        # On the installed host this is UID 0; unit fixtures run unprivileged.
        self.root_uid = Path('/').stat().st_uid

    def _check_file(self, fd: int) -> None:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise WorkstationError('unsafe_session_storage')

    def _directory(self) -> int:
        # Reject symlinks and writable ancestors before creating a secret file.
        # A root-owned sticky /tmp ancestor is safe for private test directories.
        for path in reversed((self.directory, *self.directory.parents)):
            if not path.exists() and not path.is_symlink():
                path.mkdir(mode=0o700)
            info = path.lstat()
            sticky_root = info.st_uid == self.root_uid and bool(info.st_mode & stat.S_ISVTX)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {self.root_uid, self.owner_uid}
                    or (info.st_mode & 0o022 and not sticky_root)):
                raise WorkstationError('unsafe_session_storage')
        if stat.S_IMODE(self.directory.stat().st_mode) != 0o700:
            raise WorkstationError('unsafe_session_storage')
        return os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    @contextmanager
    def locked(self):
        directory_fd = self._directory()
        lock_fd = None
        try:
            lock_fd = os.open('.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                              0o600, dir_fd=directory_fd)
            self._check_file(lock_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield directory_fd
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            os.close(directory_fd)

    def read(self, directory_fd: int, session_id: str) -> dict | None:
        try:
            fd = os.open(session_id + '.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        try:
            self._check_file(fd)
            with os.fdopen(fd, 'r', encoding='utf-8') as stream:
                fd = None
                encoded = stream.read(MAX_SESSION_BYTES + 1)
            if len(encoded) > MAX_SESSION_BYTES:
                raise WorkstationError('session_state_invalid')
            value = json.loads(encoded)
            if not isinstance(value, dict) or value.get('version') != VERSION or value.get('session_id') != session_id:
                raise WorkstationError('session_state_invalid')
            _uuid(value.get('account_id'))
            token = value.get('token')
            match = TOKEN_PATTERN.fullmatch(token) if isinstance(token, str) else None
            if match is None or value.get('credential_id') != match[1] or value.get('scopes') not in (sorted(SCOPES), sorted(LEGACY_SCOPES)):
                raise WorkstationError('session_state_invalid')
            expires_at = datetime.fromisoformat(value['expires_at'])
            if expires_at.tzinfo is None or type(value.get('revoked', False)) is not bool:
                raise WorkstationError('session_state_invalid')
            return value
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise WorkstationError('session_state_invalid') from None
        finally:
            if fd is not None:
                os.close(fd)

    def write(self, directory_fd: int, session_id: str, value: dict) -> None:
        temporary = '.' + uuid4().hex + '.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(value, stream, separators=(',', ':'))
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, session_id + '.json', src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


class WorkstationService:
    def __init__(self, db, state_directory: Path):
        self.db = db
        self.identities = SQLIdentityRepository(db)
        self.credentials = CredentialService(db)
        self.store = SessionStore(state_directory)
        from backend.workstation_audit import WorkstationAudit
        self.audit = WorkstationAudit(db)
        self.audit.initialize_schema()
        # This helper is embedded in the workstation. Older installed audit
        # modules must still permit the workstation to upgrade the controller.
        with db.transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_workstation_keys (
                fingerprint TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                registered_at TEXT NOT NULL, revoked_at TEXT)''')

    def _audit_credential(self, result, request):
        if 'attribution' not in request:
            return
        label = next((choice['label'] for choice in self.choices() if choice['account_id'] == result['account_id']), result['account_id'])
        if self.audit.bind(result['credential_id'], result['session_id'], result['account_id'], label, request['attribution']):
            self.audit.record(self.audit.context(result['credential_id']), 'credential', 'issued')

    def _revoke_credential(self, credential_id):
        revoked = self.credentials.revoke(credential_id)
        context = self.audit.context(credential_id)
        if context:
            self.audit.record(context, 'credential', 'revoked')
        return revoked

    def choices(self) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("""SELECT a.id, d.username, d.first_name, d.last_name, i.subject
                FROM backend_accounts a LEFT JOIN backend_external_identities i
                ON i.account_id = a.id AND i.provider = 'telegram'
                LEFT JOIN backend_telegram_identity_details d ON d.subject = i.subject
                WHERE a.role = 'admin' AND a.status = 'approved' ORDER BY a.id, i.subject""").fetchall()
        choices = {}
        for row in rows:
            label = str(row['subject'] or row['id'])
            if row['username']:
                label += ' · @' + row['username']
            choices.setdefault(row['id'], {'account_id': row['id'],
                'label': label[:200]})
        return list(choices.values())

    def _account(self, request: dict, previous: dict | None):
        context = request.get('attribution')
        if context is None:
            raise WorkstationError('workstation_attribution_required')
        fingerprint = context['device_fingerprint']
        with self.db.connect() as conn:
            binding = conn.execute('SELECT * FROM backend_workstation_keys WHERE fingerprint=?', (fingerprint,)).fetchone()
        if binding is not None and binding['revoked_at'] is not None:
            raise WorkstationError('workstation_access_revoked')
        if 'account_id' in request:
            account = self.identities.get_account(request['account_id'])
        elif 'telegram_id' in request:
            account = self.identities.find_telegram_account(request['telegram_id'])
        elif binding is not None:
            account = self.identities.get_account(binding['account_id'])
        elif previous is not None:
            account = self.identities.get_account(previous['account_id'])
        else:
            choices = self.choices()
            if not choices:
                raise WorkstationError('approved_admin_required')
            raise WorkstationError('admin_selection_required', choices=choices)
        if account is None or account.role != 'admin' or account.status != 'approved':
            raise WorkstationError('approved_admin_required')
        if previous is not None and account.id != previous['account_id']:
            raise WorkstationError('session_account_conflict')
        if binding is not None and account.id != binding['account_id']:
            raise WorkstationError('workstation_account_conflict')
        if previous is not None:
            old_context = self.audit.context(previous['credential_id'])
            if old_context is not None and old_context['device_fingerprint'] != fingerprint:
                raise WorkstationError('workstation_attribution_conflict')
        if binding is None:
            with self.db.transaction() as conn:
                conn.execute('INSERT INTO backend_workstation_keys VALUES (?,?,?,NULL)',
                    (fingerprint, account.id, datetime.now(timezone.utc).isoformat()))
            label = next(choice['label'] for choice in self.choices() if choice['account_id'] == account.id)
            self.audit.record({'credential_id': None, 'session_id': request['session_id'],
                'account_id': account.id, 'account_label': label, **context},
                'privileged SSH workstation registration', 'issued')
        return account

    def _owns_credential(self, previous: dict) -> bool:
        with self.db.connect() as conn:
            row = conn.execute('SELECT secret_hash, account_id, kind, scopes_json FROM backend_credentials WHERE id = ?',
                               (previous['credential_id'],)).fetchone()
        return bool(row is not None and row['account_id'] == previous['account_id']
                    and row['kind'] == PrincipalKind.ACCOUNT.value
                    and frozenset(json.loads(row['scopes_json'])) == frozenset(previous['scopes'])
                    and hmac.compare_digest(row['secret_hash'], hashlib.sha256(previous['token'].encode()).hexdigest()))

    def handle(self, request: dict, *, now: datetime | None = None) -> dict:
        request = validate_request(request)
        if request['action'] == 'list':
            return {'version': VERSION, 'ok': True, 'accounts': self.choices()}
        session_id = request['session_id']
        with self.store.locked() as directory_fd:
            previous = self.store.read(directory_fd, session_id)
            if request['action'] in {'revoke-access', 'restore-access'}:
                context = request.get('attribution')
                if context is None:
                    raise WorkstationError('workstation_attribution_required')
                with self.db.transaction() as conn:
                    binding = conn.execute('SELECT * FROM backend_workstation_keys WHERE fingerprint=?',
                        (context['device_fingerprint'],)).fetchone()
                    if binding is None:
                        raise WorkstationError('workstation_key_not_registered')
                    if request['action'] == 'restore-access':
                        if request.get('account_id') != binding['account_id']:
                            raise WorkstationError('workstation_account_conflict')
                        account = self.identities.get_account(binding['account_id'])
                        if account is None or account.role != 'admin' or account.status != 'approved':
                            raise WorkstationError('approved_admin_required')
                    conn.execute('UPDATE backend_workstation_keys SET revoked_at=? WHERE fingerprint=?',
                        (None if request['action'] == 'restore-access' else datetime.now(timezone.utc).isoformat(),
                         context['device_fingerprint']))
                    # Restoring a key never revives old bearer tokens.
                    conn.execute('''UPDATE backend_credentials SET revoked_at=? WHERE id IN
                        (SELECT credential_id FROM backend_workstation_context WHERE device_fingerprint=?)''',
                        (datetime.now(timezone.utc).isoformat(), context['device_fingerprint']))
                self.audit.record({'credential_id': None, 'session_id': session_id,
                    'account_id': binding['account_id'], 'account_label': binding['account_id'], **context},
                    'privileged SSH workstation ' + request['action'], 'completed')
                return {'version': VERSION, 'ok': True}
            if request['action'] == 'revoke':
                revoked = False
                if previous is not None:
                    if self._owns_credential(previous):
                        if not previous.get('revoked'):
                            revoked = self._revoke_credential(previous['credential_id'])
                        else:
                            revoked = self.credentials.revoke(previous['credential_id'])
                    previous['revoked'] = True
                    self.store.write(directory_fd, session_id, previous)
                return {'version': VERSION, 'ok': True, 'session_id': session_id, 'revoked': revoked}
            if previous is not None and previous.get('revoked'):
                raise WorkstationError('session_revoked')
            if request['action'] in {'lookup-update', 'lookup-node', 'audit-enrollment'}:
                if previous is None:
                    raise WorkstationError('session_not_found')
                try:
                    principal = self.credentials.authenticate('Bearer ' + previous['token'], now=now)
                    require_permission(resolve_actor(principal, self.identities), 'settings.manage')
                except AccessDenied:
                    raise WorkstationError('session_reauthentication_required') from None
                if (principal.kind != PrincipalKind.ACCOUNT or principal.account_id != previous['account_id']
                        or principal.scopes != SCOPES):
                    raise WorkstationError('session_state_invalid')
                if request['action'] == 'lookup-node':
                    with self.db.connect() as conn:
                        matches = []
                        for table, kind in (('backend_node_jobs', 'node-jobs'), ('backend_agent_rollouts', 'agent-rollouts')):
                            row = conn.execute(f'SELECT id,node_key,status FROM {table} WHERE actor_id=? AND command_key=?',
                                (principal.account_id, request['command_id'])).fetchone()
                            if row:
                                matches.append({**dict(row), 'kind': kind})
                    if len(matches) > 1:
                        raise WorkstationError('node_command_conflict')
                    return {'version': VERSION, 'ok': True, 'job': matches[0] if matches else None}
                if request['action'] == 'audit-enrollment':
                    context = self.audit.context(principal.id)
                    if context is None:
                        raise WorkstationError('workstation_attribution_required')
                    self.audit.record_enrollment(context, request['command_id'], request['target'],
                        request['key_fingerprint'], request['outcome'])
                    return {'version': VERSION, 'ok': True}
                with self.db.connect() as conn:
                    row = conn.execute('''SELECT id, kind, intent_json FROM backend_update_jobs
                        WHERE actor_id = ? AND command_key = ?''',
                        (principal.account_id, request['command_id'])).fetchone()
                job = None
                if row is not None:
                    intent = json.loads(row['intent_json'])
                    if (not isinstance(intent, dict) or intent.keys() != {'target_ref', 'branch'}
                            or row['kind'] not in {'version', 'stack', 'agents', 'runtimes'}):
                        raise WorkstationError('update_state_invalid')
                    job = {'id': row['id'], 'kind': row['kind'], 'target_ref': intent['target_ref'],
                           'branch': intent['branch']}
                return {'version': VERSION, 'ok': True, 'session_id': session_id, 'job': job}
            account = self._account(request, previous)
            timestamp = now or datetime.now(timezone.utc)
            if previous is not None:
                try:
                    principal = self.credentials.authenticate('Bearer ' + previous['token'], now=timestamp)
                except AccessDenied:
                    pass
                else:
                    if principal.account_id == account.id and principal.kind == PrincipalKind.ACCOUNT and principal.scopes == SCOPES:
                        try:
                            self._audit_credential(previous, request)
                        except Exception:
                            raise WorkstationError('workstation_attribution_conflict') from None
                        return {'ok': True, **{k: v for k, v in previous.items() if k != 'revoked'}}
                if self._owns_credential(previous):
                    self._revoke_credential(previous['credential_id'])
            credential_id, token = self.credentials.issue(PrincipalKind.ACCOUNT, SCOPES,
                account_id=account.id, ttl=LIFETIME, now=timestamp)
            result = {'version': VERSION, 'session_id': session_id, 'credential_id': credential_id,
                      'token': token, 'account_id': account.id,
                      'expires_at': (timestamp + LIFETIME).isoformat(), 'scopes': sorted(SCOPES)}
            try:
                self._audit_credential(result, request)
                self.store.write(directory_fd, session_id, result)
            except Exception:
                # An ordinary storage failure must not leave an untracked live token.
                self._revoke_credential(credential_id)
                raise
            return {'ok': True, **result}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkstationError('invalid_request')
        result[key] = value
    return result


def main(stdin=None, stdout=None) -> int:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    try:
        if os.geteuid() != 0:
            raise WorkstationError('root_required')
        encoded = stdin.read(MAX_REQUEST_BYTES + 1)
        if len(encoded.encode('utf-8')) > MAX_REQUEST_BYTES:
            raise WorkstationError('invalid_request')
        try:
            request = validate_request(json.loads(encoded, object_pairs_hook=_object))
        except (json.JSONDecodeError, UnicodeError):
            raise WorkstationError('invalid_request') from None
        from db import get_db
        db = get_db()
        import config
        service = WorkstationService(db, Path(config.SHARED_ROOT) / 'data' / 'workstation-sessions')
        response = service.handle(request)
    except WorkstationError as error:
        detail = {'code': error.code}
        if error.choices is not None:
            detail['choices'] = error.choices
        response = {'version': VERSION, 'ok': False, 'error': detail}
    except Exception:
        # DB failures may contain DSNs; filesystem failures may include secret
        # payload fragments. No exception text or traceback crosses this boundary.
        response = {'version': VERSION, 'ok': False, 'error': {'code': 'workstation_unavailable'}}
    json.dump(response, stdout, separators=(',', ':'))
    stdout.write('\n')
    stdout.flush()
    return 0 if response['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
