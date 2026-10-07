"""Opaque bearer credentials. Only a token digest is retained in PostgreSQL."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
from uuid import uuid4

from .authorization import (
    AccessDenied, ADMIN_PERMISSIONS, APPROVED_PERMISSIONS, SELF_PERMISSIONS,
    Principal, PrincipalKind,
)

PERMISSIONS = SELF_PERMISSIONS | APPROVED_PERMISSIONS | ADMIN_PERMISSIONS
ADAPTER_SCOPES = PERMISSIONS | {'identity.telegram.resolve', 'delegate.telegram'}
TOKEN_PATTERN = re.compile(r'np_([a-f0-9]{32})\.([A-Za-z0-9_-]{43})\Z')


class CredentialService:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        with self.db.transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_credentials (
                id TEXT PRIMARY KEY,
                secret_hash TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('account', 'adapter', 'service')),
                account_id TEXT REFERENCES backend_accounts(id),
                scopes_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            )''')
        from .workstation_audit import WorkstationAudit
        WorkstationAudit(self.db).initialize_schema()

    def issue(self, kind: PrincipalKind, scopes: frozenset[str], *, account_id: str | None = None,
              ttl: timedelta = timedelta(days=30), now: datetime | None = None) -> tuple[str, str]:
        kind = PrincipalKind(kind)
        if not scopes or not scopes <= (ADAPTER_SCOPES if kind == PrincipalKind.ADAPTER else PERMISSIONS):
            raise ValueError('invalid credential scopes')
        if ttl <= timedelta(0) or ttl > timedelta(days=365):
            raise ValueError('credential lifetime must be within 365 days')
        if (kind == PrincipalKind.ACCOUNT) != bool(account_id):
            raise ValueError('only account credentials require account_id')
        timestamp = now or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ValueError('timestamp must have timezone')
        token_id = uuid4().hex
        token = f'np_{token_id}.{secrets.token_urlsafe(32)}'
        with self.db.transaction() as conn:
            if account_id:
                account = conn.execute('SELECT id FROM backend_accounts WHERE id = ?', (account_id,)).fetchone()
                if account is None:
                    raise ValueError('account does not exist')
            conn.execute('''INSERT INTO backend_credentials
                (id, secret_hash, kind, account_id, scopes_json, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)''', (
                    token_id, hashlib.sha256(token.encode()).hexdigest(), kind.value, account_id,
                    json.dumps(sorted(scopes)), timestamp.isoformat(), (timestamp + ttl).isoformat(),
                ))
        return token_id, token

    def authenticate(self, authorization: str | None, *, now: datetime | None = None) -> Principal:
        # Never include input credentials in an error or diagnostic string.
        if not isinstance(authorization, str) or not authorization.startswith('Bearer '):
            raise AccessDenied('invalid_credentials', 401)
        token = authorization[7:]
        match = TOKEN_PATTERN.fullmatch(token)
        if not match:
            raise AccessDenied('invalid_credentials', 401)
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_credentials WHERE id = ?', (match[1],)).fetchone()
        if row is None or not hmac.compare_digest(row['secret_hash'], hashlib.sha256(token.encode()).hexdigest()):
            raise AccessDenied('invalid_credentials', 401)
        timestamp = now or datetime.now(timezone.utc)
        if row['revoked_at'] is not None:
            raise AccessDenied('invalid_credentials', 401)
        try:
            expires_at = datetime.fromisoformat(row['expires_at'])
            kind = PrincipalKind(row['kind'])
            scopes = frozenset(json.loads(row['scopes_json']))
            if expires_at.tzinfo is None or not scopes or not scopes <= (ADAPTER_SCOPES if kind == PrincipalKind.ADAPTER else PERMISSIONS):
                raise ValueError('invalid credential record')
            if (kind == PrincipalKind.ACCOUNT) != bool(row['account_id']):
                raise ValueError('invalid credential record')
        except (TypeError, ValueError):
            raise AccessDenied('invalid_credentials', 401) from None
        if expires_at <= timestamp:
            raise AccessDenied('invalid_credentials', 401)
        return Principal(row['id'], kind, scopes, row['account_id'])

    def revoke(self, token_id: str) -> bool:
        with self.db.transaction() as conn:
            row = conn.execute('SELECT id FROM backend_credentials WHERE id = ?', (token_id,)).fetchone()
            if row is None:
                return False
            conn.execute('UPDATE backend_credentials SET revoked_at = ? WHERE id = ?',
                         (datetime.now(timezone.utc).isoformat(), token_id))
        return True
