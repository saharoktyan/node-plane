"""Open registration: accounts made in the web cabinet without an invite.

`backend_registrations` marks such accounts, with the address they came
from, for the per-address limit and for removing the ones left empty.
`backend_registration_challenges` holds the proof-of-work puzzles handed out;
each is solved once.
"""


def create_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_registrations (
        account_id TEXT PRIMARY KEY REFERENCES backend_accounts(id) ON DELETE CASCADE,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS backend_registrations_source ON backend_registrations(source, created_at)')
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_registration_challenges (
        id TEXT PRIMARY KEY,
        challenge TEXT NOT NULL,
        bits INTEGER NOT NULL,
        expires_at TEXT NOT NULL)''')


def upgrade(conn):
    create_schema(conn)
