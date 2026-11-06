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
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected', 'disabled')),
                revision INTEGER NOT NULL DEFAULT 1
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_account_guard (
                id INTEGER PRIMARY KEY CHECK(id = 1), revision INTEGER NOT NULL
            )""")
            conn.execute("""INSERT INTO backend_account_guard(id, revision) VALUES (1, 1)
                ON CONFLICT(id) DO NOTHING""")
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_external_identities (
                provider TEXT NOT NULL,
                subject TEXT NOT NULL,
                account_id TEXT NOT NULL REFERENCES backend_accounts(id),
                PRIMARY KEY(provider, subject)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_telegram_identity_details (
                subject TEXT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                language_code TEXT,
                locale TEXT,
                locale_selected INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )""")

            conn.execute("""CREATE TABLE IF NOT EXISTS backend_identity_commands (
                principal_id TEXT NOT NULL, command_key TEXT NOT NULL,
                telegram_subject TEXT NOT NULL, account_id TEXT REFERENCES backend_accounts(id),
                PRIMARY KEY(principal_id, command_key)
            )""")

        with self.db.transaction() as conn:
            from db.migration_revisions.r0011_account_deletions import create_schema
            create_schema(conn)
            from db.migration_revisions.r0007_account_menu_refresh import upgrade
            upgrade(conn)
            from db.migration_revisions.r0028_account_details import upgrade
            upgrade(conn)
            from db.migration_revisions.r0029_account_channels import upgrade
            upgrade(conn)
            from db.migration_revisions.r0035_roles import upgrade
            upgrade(conn)
            from db.migration_revisions.r0040_logins import upgrade
            upgrade(conn)
            from db.migration_revisions.r0041_sessions import create_schema as sessions_schema
            sessions_schema(conn)
            from db.migration_revisions.r0042_passkeys import create_schema as passkeys_schema
            passkeys_schema(conn)
            from db.migration_revisions.r0043_invites import create_schema as invites_schema
            invites_schema(conn)
            from db.migration_revisions.r0044_registrations import create_schema as registrations_schema
            registrations_schema(conn)
            from db.migration_revisions.r0045_telegram_sign_in import create_schema as telegram_sign_in_schema
            telegram_sign_in_schema(conn)

        from .system_cleanup import SystemCleanupService
        SystemCleanupService(self.db).initialize_schema()
        from .backups import BackupService
        BackupService(self.db).initialize_schema()
        from .recovery import initialize_schema
        initialize_schema(self.db)

    @staticmethod
    def _account(row, conn=None) -> Account | None:
        if row is None:
            return None
        return Account(row['id'], row['role'], row['status'], account_roles(conn, row) if conn else None)

    def get_account(self, account_id: str) -> Account | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT id, role, status FROM backend_accounts WHERE id = ?", (account_id,)).fetchone()
            return self._account(row, conn)

    def create_account(self, *, status='approved') -> Account:
        """Create an account without requiring any external identity provider."""
        if status != 'approved':
            raise ValueError('invalid account status')
        account_id = str(uuid4())
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            conn.execute('INSERT INTO backend_accounts(id, status) VALUES (?, ?)',
                         (account_id, status))
        return Account(account_id, 'member', status)

    def link_telegram(self, account_id: str, user_id: int) -> Account:
        """Attach Telegram as an optional login to an existing account."""
        from .authorization import validate_telegram_id
        validate_telegram_id(user_id)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            row = conn.execute('SELECT id, role, status FROM backend_accounts WHERE id = ?',
                               (account_id,)).fetchone()
            if row is None:
                raise AccessDenied('resource_not_found', 404)
            existing = conn.execute('''SELECT account_id FROM backend_external_identities
                WHERE provider = 'telegram' AND subject = ?''', (str(user_id),)).fetchone()
            if existing is not None:
                if existing['account_id'] != account_id:
                    raise AccessDenied('identity_already_linked', 409)
            else:
                conn.execute('''INSERT INTO backend_external_identities(provider, subject, account_id)
                    VALUES ('telegram', ?, ?)''', (str(user_id), account_id))
                from .account_channels import attach
                attach(conn, account_id, 'telegram', user_id)
            return self._account(row, conn)

    def find_account(self, provider: str, subject: str) -> Account | None:
        """The account a sign-in identity of any provider belongs to."""
        with self.db.connect() as conn:
            row = conn.execute("""SELECT a.id, a.role, a.status FROM backend_accounts a
                JOIN backend_external_identities i ON i.account_id = a.id
                WHERE i.provider = ? AND i.subject = ?""", (provider, subject)).fetchone()
            return self._account(row, conn)

    def find_telegram_account(self, user_id: int) -> Account | None:
        return self.find_account('telegram', str(user_id))

    def account_details(self, account_id: str) -> dict:
        """The account's own name and language, with its Telegram details if it has them."""
        with self.db.connect() as conn:
            row = conn.execute('''SELECT a.display_name, a.locale, a.locale_selected,
                d.username, d.first_name, d.last_name, d.language_code
                FROM backend_accounts a
                LEFT JOIN backend_external_identities i ON i.account_id = a.id AND i.provider = 'telegram'
                LEFT JOIN backend_telegram_identity_details d ON d.subject = i.subject
                WHERE a.id = ? ORDER BY i.subject LIMIT 1''', (account_id,)).fetchone()
        if row is None:
            return {}
        result = {key: row[key] for key in row.keys() if row[key] is not None}
        result['locale_selected'] = bool(row['locale_selected'])
        return result

    def _resolve(self, conn, user_id: int) -> Account:
        candidate = str(uuid4())
        conn.execute("INSERT INTO backend_accounts(id, status) VALUES (?, 'approved')", (candidate,))
        conn.execute("""INSERT INTO backend_external_identities(provider, subject, account_id)
            VALUES ('telegram', ?, ?) ON CONFLICT(provider, subject) DO NOTHING""", (str(user_id), candidate))
        row = conn.execute("""SELECT a.id, a.role, a.status FROM backend_accounts a
            JOIN backend_external_identities i ON i.account_id = a.id
            WHERE i.provider = 'telegram' AND i.subject = ?""", (str(user_id),)).fetchone()
        if row['id'] != candidate:
            conn.execute("DELETE FROM backend_accounts WHERE id = ?", (candidate,))
        else:
            from db.migration_revisions.r0028_account_details import backfill
            backfill(conn, candidate)
            from .account_channels import attach
            attach(conn, candidate, 'telegram', user_id)
        return self._account(row, conn)

    def resolve_telegram(self, user_id: int) -> Account:
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            return self._resolve(conn, user_id)

    def update_telegram_details(self, user_id: int, *, username=None, first_name=None,
                                last_name=None, language_code=None) -> None:
        """Keep display metadata separate from authentication identity and roles."""
        from datetime import datetime, timezone
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            conn.execute('''INSERT INTO backend_telegram_identity_details
                (subject, username, first_name, last_name, language_code, locale, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(subject) DO UPDATE SET
                username = excluded.username, first_name = excluded.first_name,
                last_name = excluded.last_name, language_code = excluded.language_code,
                locale = COALESCE(backend_telegram_identity_details.locale, excluded.locale),
                updated_at = excluded.updated_at''',
                (str(user_id), username, first_name, last_name, language_code,
                 language_code[:2].lower() if (language_code or '')[:2].lower() in {'ru', 'en'} else 'en',
                 datetime.now(timezone.utc).isoformat()))
            linked = conn.execute('''SELECT account_id FROM backend_external_identities
                WHERE provider = 'telegram' AND subject = ?''', (str(user_id),)).fetchone()
            if linked is not None:
                from db.migration_revisions.r0028_account_details import backfill
                backfill(conn, linked['account_id'])

    def set_locale_for_account(self, account_id: str, locale: str) -> None:
        """The language is the account's; a linked Telegram identity keeps the same choice."""
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            if conn.execute('''UPDATE backend_accounts SET locale = ?, locale_selected = 1
                    WHERE id = ?''', (locale, account_id)).rowcount == 0:
                raise AccessDenied('resource_not_found', 404)
            row = conn.execute('''SELECT subject FROM backend_external_identities
                WHERE account_id = ? AND provider = 'telegram' LIMIT 1''', (account_id,)).fetchone()
            if row is None:
                return
            conn.execute('''INSERT INTO backend_telegram_identity_details
                (subject, locale, locale_selected, updated_at)
                VALUES (?, ?, 1, CURRENT_TIMESTAMP) ON CONFLICT(subject) DO UPDATE SET
                locale = excluded.locale, locale_selected = 1, updated_at = excluded.updated_at''',
                (row['subject'], locale))

    def resolve_telegram_command(self, principal_id: str, command_key: str, user_id: int) -> Account:
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
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
                return self._account(row, conn)
            account = self._resolve(conn, user_id)
            conn.execute("""UPDATE backend_identity_commands SET account_id = ?
                WHERE principal_id = ? AND command_key = ?""", (account.id, principal_id, command_key))
            return account


def account_roles(conn, row):
    """The installation-wide roles of an account row (`id`, `role`).

    Roles count only while the mirror says `admin`, so a demotion written to
    `role` takes them all. An administrator without assignments was made by
    an older release or written directly; `role` then decides (None)."""
    if row['role'] != 'admin':
        return frozenset()
    roles = frozenset(r['role_id'] for r in conn.execute(
        """SELECT role_id FROM backend_role_assignments
        WHERE account_id = ? AND scope_kind = 'installation' AND scope_id = ''""", (row['id'],)).fetchall())
    return None if not roles and row['role'] == 'admin' else roles


def load_account(conn, account_id):
    """The account with its roles, read in this transaction."""
    row = conn.execute('SELECT id, role, status FROM backend_accounts WHERE id = ?', (account_id,)).fetchone()
    return SQLIdentityRepository._account(row, conn)


def actor_account(db, account_id, row):
    """An Account for work done on behalf of `account_id`, whose locked row
    (`role`, `status`) the caller read; the roles are read here."""
    with db.connect() as conn:
        roles = account_roles(conn, {'id': account_id, 'role': row['role']})
    return Account(account_id, row['role'], row['status'], roles)
