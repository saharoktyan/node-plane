"""Account details: the display name and language belong to the account.

They were read from the account's Telegram identity, which made Telegram the
only way to know who an account is. Accounts keep them now; the Telegram
details are a source when they are still empty.
"""

COLUMNS = (('display_name', 'TEXT'), ('locale', 'TEXT'),
           ('locale_selected', 'INTEGER NOT NULL DEFAULT 0'))


def add_columns(conn):
    if getattr(conn, 'backend_name', '') == 'postgres':
        for name, kind in COLUMNS:
            conn.execute(f'ALTER TABLE backend_accounts ADD COLUMN IF NOT EXISTS {name} {kind}')
        return
    existing = {row['name'] for row in conn.execute('PRAGMA table_info(backend_accounts)').fetchall()}
    for name, kind in COLUMNS:
        if name not in existing:
            conn.execute(f'ALTER TABLE backend_accounts ADD COLUMN {name} {kind}')


def backfill(conn, account_id=None):
    """Fill empty account details from the account's Telegram identity."""
    scope = 'AND id = ?' if account_id else ''
    params = (account_id,) if account_id else ()
    conn.execute(f'''UPDATE backend_accounts SET display_name = (
            SELECT COALESCE(NULLIF(TRIM(COALESCE(d.first_name, '') || ' ' || COALESCE(d.last_name, '')), ''),
                            '@' || d.username)
            FROM backend_external_identities i JOIN backend_telegram_identity_details d ON d.subject = i.subject
            WHERE i.account_id = backend_accounts.id AND i.provider = 'telegram'
            ORDER BY i.subject LIMIT 1)
        WHERE display_name IS NULL {scope}''', params)
    conn.execute(f'''UPDATE backend_accounts SET
            locale = (SELECT d.locale FROM backend_external_identities i
                JOIN backend_telegram_identity_details d ON d.subject = i.subject
                WHERE i.account_id = backend_accounts.id AND i.provider = 'telegram'
                ORDER BY i.subject LIMIT 1),
            locale_selected = COALESCE((SELECT d.locale_selected FROM backend_external_identities i
                JOIN backend_telegram_identity_details d ON d.subject = i.subject
                WHERE i.account_id = backend_accounts.id AND i.provider = 'telegram'
                ORDER BY i.subject LIMIT 1), 0)
        WHERE locale IS NULL {scope}''', params)


def upgrade(conn):
    add_columns(conn)
    backfill(conn)
