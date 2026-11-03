"""Tariffs: named sets of profile limits, an access term and nodes.

A profile's tariff is a policy layer between the installation and the
profile, so controllers that do not know these tables must not read this
database: they would resolve limits without it.
"""


def upgrade(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_tariffs (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        canonical TEXT NOT NULL UNIQUE,
        duration_days INTEGER CHECK(duration_days IS NULL OR duration_days BETWEEN 1 AND 3650),
        access_json TEXT,
        policies_json TEXT NOT NULL DEFAULT '{}',
        revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_profile_tariffs (
        profile_id TEXT PRIMARY KEY REFERENCES backend_profiles(id) ON DELETE CASCADE,
        tariff_id TEXT NOT NULL REFERENCES backend_tariffs(id),
        assigned_at TEXT NOT NULL)''')
    conn.execute('''CREATE INDEX IF NOT EXISTS backend_profile_tariffs_tariff
        ON backend_profile_tariffs(tariff_id)''')
