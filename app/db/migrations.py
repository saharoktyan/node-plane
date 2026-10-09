"""Explicit PostgreSQL migrations; application rollback never downgrades the DB."""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from .migration_revisions import r0001_baseline, r0002_node_traffic

LOCK_KEY = 0x4E504C414E454442  # NPLANEDB; shared by every controller/schema.
LEDGER_DDL = '''CREATE TABLE IF NOT EXISTS backend_schema_revisions (
    revision INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL,
    minimum_reader INTEGER NOT NULL, applied_at TEXT NOT NULL)'''


class MigrationError(RuntimeError):
    """Sanitized deployment failure, safe to display without credentials or SQL."""


@dataclass(frozen=True)
class Revision:
    number: int
    name: str
    module: object
    minimum_reader: int

    @property
    def checksum(self):
        # Hash frozen source, including data transformations, not live repositories.
        return sha256(Path(self.module.__file__).read_bytes()).hexdigest()


REVISIONS = (
    Revision(1, 'baseline', r0001_baseline, 1),
    Revision(2, 'node_traffic', r0002_node_traffic, 1),
)


def history_exists(conn):
    # Repository unit fixtures use SQLite; production adapters are PostgreSQL.
    if getattr(conn, 'backend_name', '') != 'postgres':
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='backend_schema_revisions'").fetchone() is not None
    return conn.execute("SELECT to_regclass('backend_schema_revisions') AS name").fetchone()['name'] is not None


def _history(conn):
    return [dict(row) for row in conn.execute(
        'SELECT revision,name,checksum,minimum_reader,applied_at FROM backend_schema_revisions ORDER BY revision').fetchall()]


def _validate(rows, revisions, *, writing=False):
    expected = {r.number:r for r in revisions}
    if [r.number for r in revisions] != list(range(1, len(revisions)+1)):
        raise MigrationError('migration_registry_invalid')
    for index, row in enumerate(rows, 1):
        if row['revision'] != index or not 1 <= row['minimum_reader'] <= row['revision']:
            raise MigrationError('migration_history_invalid')
        known = expected.get(index)
        if known and (row['name'], row['checksum'], row['minimum_reader']) != (
                known.name,known.checksum,known.minimum_reader):
            raise MigrationError('migration_history_mismatch')
    if writing and len(rows) > len(revisions):
        raise MigrationError('database_newer_than_migrator')
    if rows and max(r['minimum_reader'] for r in rows) > len(revisions):
        raise MigrationError('database_reader_incompatible')


def schema_status(db, *, revisions=REVISIONS):
    """Read-only status. Future compatible additive revisions allow app rollback."""
    with db.connect() as conn:
        rows = _history(conn) if history_exists(conn) else []
    _validate(rows,revisions)
    head = rows[-1]['revision'] if rows else 0
    return {'current_revision':head, 'required_revision':len(revisions),
        'ready':head >= len(revisions), 'revisions':rows}


def check_schema(db, *, revisions=REVISIONS):
    result = schema_status(db,revisions=revisions)
    if not result['ready']:
        raise MigrationError('database_migration_required')
    return result


def migrate(db, *, revisions=REVISIONS):
    """Apply the pending batch and journal atomically under a DB-wide lock."""
    applied = []
    current = 0
    try:
        with db.transaction() as conn:
            conn.execute("SET LOCAL lock_timeout = '30s'")
            conn.execute('SELECT pg_advisory_xact_lock(?)', (LOCK_KEY,))
            conn.execute(LEDGER_DDL)
            rows = _history(conn)
            _validate(rows,revisions,writing=True)
            current = len(rows)
            # Serialize data backfills against already running backend workers.
            if conn.execute("SELECT to_regclass('backend_account_guard') AS name").fetchone()['name']:
                conn.execute('UPDATE backend_account_guard SET revision=revision WHERE id=1')
            for revision in revisions[current:]:
                revision.module.upgrade(conn)
                conn.execute('''INSERT INTO backend_schema_revisions
                    (revision,name,checksum,minimum_reader,applied_at) VALUES (?,?,?,?,?)''',
                    (revision.number,revision.name,revision.checksum,revision.minimum_reader,
                     datetime.now(timezone.utc).isoformat()))
                applied.append(revision.number)
    except MigrationError:
        raise
    except Exception as error:
        failed = current + len(applied) + 1
        raise MigrationError(f'database_migration_failed: revision={failed}; transaction rolled back') from error
    return {'current_revision':len(revisions), 'applied':applied}
