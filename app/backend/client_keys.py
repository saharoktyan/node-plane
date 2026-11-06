"""Signed requests from clients with a key of their own.

Workstation through the relay and the bot on another node do not carry a
bearer token the relay could read and replay: each request is signed with
the client's Ed25519 key. The signature covers the method, the path with
its query, the body's SHA-256, the Telegram user the bot acts for (empty
otherwise), the time and a random nonce:

    Authorization: NP-Signature key=<id>, ts=<unix seconds>, nonce=<base64url>, sig=<base64url>

The controller checks the signature against the stored public key, refuses
a time more than a minute away and a nonce it has seen. A Workstation key
acts as its account (only while the account is staff); a bot key acts as
the Telegram adapter.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import re
from uuid import uuid4

from .authorization import AccessDenied, Principal, PrincipalKind

SCHEME = 'NP-Signature '
SKEW = timedelta(seconds=60)
# Nonces are kept a little longer than the window they can be used in.
KEEP_NONCES = timedelta(minutes=3)
# Last use is written at most this often per key.
TOUCH = timedelta(minutes=1)
KINDS = ('workstation', 'bot')
_FIELD = re.compile(r'\s*([a-z]+)=([A-Za-z0-9_-]+)\s*')
_ID = re.compile(r'[a-f0-9]{32}')
_NONCE = re.compile(r'[A-Za-z0-9_-]{16,64}')


def _decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def message(method, target, body, timestamp, nonce, delegated=''):
    """The bytes a client signs."""
    return '\n'.join(('np-sig-v1', method.upper(), target, hashlib.sha256(body).hexdigest(),
                      delegated or '', str(timestamp), nonce)).encode()


def signed(authorization):
    return isinstance(authorization, str) and authorization.startswith(SCHEME)


def parse(authorization):
    """The header's fields, or None when it is not a well-formed signature."""
    if not signed(authorization):
        return None
    fields = {}
    for part in authorization[len(SCHEME):].split(','):
        match = _FIELD.fullmatch(part)
        if match is None or match[1] in fields:
            return None
        fields[match[1]] = match[2]
    if set(fields) != {'key', 'ts', 'nonce', 'sig'} or not _ID.fullmatch(fields['key']) \
            or not fields['ts'].isdecimal() or len(fields['ts']) > 12 or not _NONCE.fullmatch(fields['nonce']):
        return None
    return fields


def public_key(value):
    """A raw Ed25519 public key in base64url, checked; the error otherwise."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        raw = _decode(value) if isinstance(value, str) and len(value) == 43 else b''
        Ed25519PublicKey.from_public_bytes(raw)
    except ValueError:
        raise AccessDenied('client_key_invalid', 422) from None
    return encode(raw)


def kind_of(conn, authorization):
    """The kind of the key a request names, unchecked: enough to take power
    away from a relayed request before its signature is verified."""
    fields = parse(authorization)
    if fields is None:
        return None
    row = conn.execute('SELECT kind FROM backend_client_keys WHERE id=? AND revoked_at IS NULL',
                       (fields['key'],)).fetchone()
    return row['kind'] if row else None


def register(conn, kind, key, *, name, account_id=None, now=None):
    """Stores a client's public key; its id is what the client names."""
    if kind not in KINDS or (kind == 'workstation') != bool(account_id):
        raise ValueError('invalid client key')
    key = public_key(key)
    if conn.execute('SELECT 1 FROM backend_client_keys WHERE public_key=?', (key,)).fetchone():
        raise AccessDenied('client_key_exists', 409)
    key_id = uuid4().hex
    conn.execute('''INSERT INTO backend_client_keys(id,kind,public_key,account_id,name,created_at)
        VALUES (?,?,?,?,?,?)''', (key_id, kind, key, account_id, name[:64],
                                  (now or datetime.now(timezone.utc)).isoformat()))
    return key_id


def revoke(conn, key_id, *, account_id=None, now=None):
    query = 'UPDATE backend_client_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL'
    values = [(now or datetime.now(timezone.utc)).isoformat(), key_id]
    if account_id is not None:
        query += ' AND account_id=?'
        values.append(account_id)
    if conn.execute(query, values).rowcount == 0:
        raise AccessDenied('resource_not_found', 404)


def revoke_all(conn, account_id, *, now=None):
    conn.execute('UPDATE backend_client_keys SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL',
                 ((now or datetime.now(timezone.utc)).isoformat(), account_id))


def public(row):
    return {'id': row['id'], 'kind': row['kind'], 'name': row['name'], 'created_at': row['created_at'],
            'last_used_at': row['last_used_at'], 'revoked_at': row['revoked_at']}


class ClientKeys:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        from db.migration_revisions.r0051_client_keys import create_schema
        with self.db.transaction() as conn:
            create_schema(conn)

    def authenticate(self, authorization, method, target, body, delegated=None, *, now=None):
        """The principal of a signed request, or 401."""
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from .credentials import ADAPTER_SCOPES, PERMISSIONS
        fields = parse(authorization)
        if fields is None:
            raise AccessDenied('invalid_credentials', 401)
        now = now or datetime.now(timezone.utc)
        if abs(now.timestamp() - int(fields['ts'])) > SKEW.total_seconds():
            raise AccessDenied('request_time_invalid', 401)
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_client_keys WHERE id=?', (fields['key'],)).fetchone()
        if row is None or row['revoked_at'] is not None:
            raise AccessDenied('invalid_credentials', 401)
        try:
            signature = _decode(fields['sig'])
            Ed25519PublicKey.from_public_bytes(_decode(row['public_key'])).verify(
                signature, message(method, target, body, fields['ts'], fields['nonce'], delegated))
        except (InvalidSignature, ValueError):
            raise AccessDenied('invalid_credentials', 401) from None
        with self.db.transaction() as conn:
            conn.execute('DELETE FROM backend_request_nonces WHERE seen_at<?', ((now - KEEP_NONCES).isoformat(),))
            if conn.execute('SELECT 1 FROM backend_request_nonces WHERE key_id=? AND nonce=?',
                            (row['id'], fields['nonce'])).fetchone():
                raise AccessDenied('request_replayed', 401)
            conn.execute('INSERT INTO backend_request_nonces(key_id,nonce,seen_at) VALUES (?,?,?)',
                         (row['id'], fields['nonce'], now.isoformat()))
            if row['last_used_at'] is None or datetime.fromisoformat(row['last_used_at']) <= now - TOUCH:
                conn.execute('UPDATE backend_client_keys SET last_used_at=? WHERE id=?', (now.isoformat(), row['id']))
            if row['kind'] == 'bot':
                return Principal(row['id'], PrincipalKind.ADAPTER, ADAPTER_SCOPES)
            # Workstation is for staff: a key stops working with the account's last staff role.
            account = conn.execute('SELECT role, status FROM backend_accounts WHERE id=?',
                                   (row['account_id'],)).fetchone()
        if account is None or (account['role'], account['status']) != ('admin', 'approved'):
            raise AccessDenied('invalid_credentials', 401)
        return Principal(row['id'], PrincipalKind.ACCOUNT, PERMISSIONS, row['account_id'])
