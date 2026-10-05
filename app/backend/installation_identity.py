"""Stable controller identity follows the database through upgrades/backups."""
from uuid import UUID, uuid4


def controller_identity(db):
    with db.transaction() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS backend_installation_identity (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL)''')
        conn.execute('''INSERT INTO backend_installation_identity VALUES (1, ?)
            ON CONFLICT(singleton) DO NOTHING''', (str(uuid4()),))
        value = conn.execute('SELECT id FROM backend_installation_identity WHERE singleton=1').fetchone()['id']
    if str(UUID(value)) != value:
        raise ValueError('invalid controller installation identity')
    return value
