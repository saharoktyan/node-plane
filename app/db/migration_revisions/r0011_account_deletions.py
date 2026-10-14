"""One account per profile; inaccessible receipts survive account deletion."""
import json
from datetime import datetime, timezone
from uuid import uuid4


def create_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS backend_account_deletions (
        account_id TEXT PRIMARY KEY REFERENCES backend_accounts(id),
        telegram_json TEXT NOT NULL, requested_at TEXT NOT NULL, completed_at TEXT
    )""")


def upgrade(conn):
    create_schema(conn)
    timestamp = datetime.now(timezone.utc).isoformat()
    # Preserve profile IDs, runtime keys, devices and grants from older installations.
    # Unowned/extra legacy profiles become accounts without a Telegram login.
    owners = set()
    for row in conn.execute('SELECT id,owner_account_id FROM backend_profiles ORDER BY id').fetchall():
        owner = row['owner_account_id']
        if owner is None or owner in owners:
            owner = str(uuid4())
            conn.execute("INSERT INTO backend_accounts(id,status) VALUES (?,'approved')", (owner,))
            conn.execute('UPDATE backend_profiles SET owner_account_id=? WHERE id=?', (owner,row['id']))
        owners.add(owner)
    for row in conn.execute("""SELECT a.id,a.role,i.subject,d.username FROM backend_accounts a
        LEFT JOIN backend_external_identities i ON i.account_id=a.id AND i.provider='telegram'
        LEFT JOIN backend_telegram_identity_details d ON d.subject=i.subject
        WHERE NOT EXISTS (SELECT 1 FROM backend_profiles p WHERE p.owner_account_id=a.id)""").fetchall():
        profile = str(uuid4())
        fallback = ('Admin' if row['role']=='admin' else 'User') + ' ' + (row['subject'] or row['id'])
        conn.execute("""INSERT INTO backend_profiles(id,runtime_name,display_name,owner_account_id,created_at)
            VALUES (?,?,?,?,?)""", (profile,'account_'+profile.replace('-',''),row['username'] or fallback,row['id'],timestamp))
    # Complete legacy deletions through the same worker path, without reviving accounts.
    for row in conn.execute("""SELECT a.id,a.role,a.status,a.revision,i.subject,d.username,d.first_name,d.last_name,
        d.language_code,d.locale,d.locale_selected FROM backend_accounts a
        JOIN backend_profiles p ON p.owner_account_id=a.id
        JOIN backend_profile_deletions pd ON pd.profile_id=p.id
        LEFT JOIN backend_external_identities i ON i.account_id=a.id AND i.provider='telegram'
        LEFT JOIN backend_telegram_identity_details d ON d.subject=i.subject""").fetchall():
        saved = dict(row)
        saved['telegram_user_id'] = int(saved.pop('subject')) if row['subject'] else None
        conn.execute('INSERT INTO backend_account_deletions(account_id,telegram_json,requested_at) VALUES (?,?,?)', (row['id'],json.dumps(saved),timestamp))
        conn.execute("UPDATE backend_accounts SET status='disabled',revision=revision+1 WHERE id=?", (row['id'],))
        conn.execute('UPDATE backend_credentials SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL', (timestamp,row['id']))
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_backend_profile_single_owner ON backend_profiles(owner_account_id)')
