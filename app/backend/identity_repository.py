"""Additive identity storage on an injected Node Plane database.

Schema creation is an explicit deployment action, not an import side effect.
Existing telegram_users/profile bindings are not migrated by registration.
"""
from __future__ import annotations

from uuid import uuid4

from .authorization import Account, AccessDenied


class SQLIdentityRepository:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self) -> None:
        with self.db.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_accounts (
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('member', 'admin')),
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected', 'disabled'))
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_external_identities (
                provider TEXT NOT NULL,
                subject TEXT NOT NULL,
                account_id TEXT NOT NULL REFERENCES backend_accounts(id),
                PRIMARY KEY(provider, subject)
            )""")

            conn.execute("""CREATE TABLE IF NOT EXISTS backend_identity_commands (
                principal_id TEXT NOT NULL, command_key TEXT NOT NULL,
                telegram_subject TEXT NOT NULL, account_id TEXT REFERENCES backend_accounts(id),
                PRIMARY KEY(principal_id, command_key)
            )""")

    @staticmethod
    def _account(row) -> Account | None:
        return Account(row['id'], row['role'], row['status']) if row is not None else None

    def get_account(self, account_id: str) -> Account | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT id, role, status FROM backend_accounts WHERE id = ?", (account_id,)).fetchone()
        return self._account(row)

    def find_telegram_account(self, user_id: int) -> Account | None:
        with self.db.connect() as conn:
            row = conn.execute("""SELECT a.id, a.role, a.status FROM backend_accounts a
                JOIN backend_external_identities i ON i.account_id = a.id
                WHERE i.provider = 'telegram' AND i.subject = ?""", (str(user_id),)).fetchone()
        return self._account(row)

    def _resolve(self, conn, user_id: int) -> Account:
        candidate = str(uuid4())
        conn.execute("INSERT INTO backend_accounts(id) VALUES (?)", (candidate,))
        conn.execute("""INSERT INTO backend_external_identities(provider, subject, account_id)
            VALUES ('telegram', ?, ?) ON CONFLICT(provider, subject) DO NOTHING""", (str(user_id), candidate))
        row = conn.execute("""SELECT a.id, a.role, a.status FROM backend_accounts a
            JOIN backend_external_identities i ON i.account_id = a.id
            WHERE i.provider = 'telegram' AND i.subject = ?""", (str(user_id),)).fetchone()
        if row['id'] != candidate:
            conn.execute("DELETE FROM backend_accounts WHERE id = ?", (candidate,))
        return self._account(row)

    def resolve_telegram(self, user_id: int) -> Account:
        with self.db.transaction() as conn:
            return self._resolve(conn, user_id)

    def resolve_telegram_command(self, principal_id: str, command_key: str, user_id: int) -> Account:
        with self.db.transaction() as conn:
            conn.execute("""INSERT INTO backend_identity_commands(principal_id, command_key, telegram_subject)
                VALUES (?, ?, ?) ON CONFLICT(principal_id, command_key) DO NOTHING""",
                (principal_id, command_key, str(user_id)))
            command = conn.execute("""SELECT telegram_subject, account_id FROM backend_identity_commands
                WHERE principal_id = ? AND command_key = ?""", (principal_id, command_key)).fetchone()
            if command['telegram_subject'] != str(user_id):
                raise AccessDenied('idempotency_conflict', 409)
            if command['account_id'] is not None:
                row = conn.execute('SELECT id, role, status FROM backend_accounts WHERE id = ?',
                                   (command['account_id'],)).fetchone()
                return self._account(row)
            account = self._resolve(conn, user_id)
            conn.execute("""UPDATE backend_identity_commands SET account_id = ?
                WHERE principal_id = ? AND command_key = ?""", (account.id, principal_id, command_key))
            return account
