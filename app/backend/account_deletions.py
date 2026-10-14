"""Account deletion is finalized only after all profile revocations settle.

Rows remain as inaccessible receipts for foreign-keyed operation/audit history.
External identities are released only after successful runtime cleanup.
"""
import json
from datetime import datetime, timezone


def begin(conn, account_id):
    from .accounts import AccountService
    account = AccountService.public(AccountService._read(conn, account_id))
    timestamp = datetime.now(timezone.utc).isoformat()
    conn.execute("""INSERT INTO backend_account_deletions(account_id,telegram_json,requested_at)
        VALUES (?,?,?) ON CONFLICT(account_id) DO NOTHING""",
        (account_id, json.dumps(account), timestamp))
    conn.execute("UPDATE backend_accounts SET status='disabled',revision=revision+1 WHERE id=?", (account_id,))
    conn.execute("UPDATE backend_credentials SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL", (timestamp,account_id))


def settle(conn):
    rows = conn.execute("""SELECT d.account_id FROM backend_account_deletions d
        WHERE d.completed_at IS NULL AND NOT EXISTS (
            SELECT 1 FROM backend_profiles p
            LEFT JOIN backend_profile_deletions pd ON pd.profile_id=p.id
            LEFT JOIN backend_operations o ON o.id=pd.operation_id
            WHERE p.owner_account_id=d.account_id
            AND (pd.profile_id IS NULL OR o.status IS NULL OR o.status NOT IN ('succeeded','no_targets')))
        ORDER BY d.account_id""").fetchall()
    timestamp = datetime.now(timezone.utc).isoformat()
    for row in rows:
        account_id = row['account_id']
        # Concurrent identity resolution locks this account before using its profile.
        conn.execute('UPDATE backend_accounts SET revision=revision+1 WHERE id=?', (account_id,))
        conn.execute('DELETE FROM backend_external_identities WHERE account_id=?', (account_id,))
        conn.execute('UPDATE backend_identity_commands SET account_id=NULL WHERE account_id=?', (account_id,))
        conn.execute("UPDATE backend_devices SET status='retired' WHERE profile_id IN (SELECT id FROM backend_profiles WHERE owner_account_id=?)", (account_id,))
        conn.execute('UPDATE backend_account_deletions SET completed_at=? WHERE account_id=? AND completed_at IS NULL', (timestamp,account_id))
        revision = conn.execute('SELECT revision FROM backend_accounts WHERE id=?', (account_id,)).fetchone()['revision']
        conn.execute("""INSERT INTO backend_account_menu_refresh(account_id,revision) VALUES (?,?)
            ON CONFLICT(account_id) DO UPDATE SET revision=excluded.revision""", (account_id,revision))
