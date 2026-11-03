"""Traffic quota periods per profile and the anchor of their resets.

Periods are configuration state: they are backed up so a restore keeps the
open period and its usage. A blocked period suspends the profile's access,
so controllers that do not know this table must not read this database.
"""


def upgrade(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_quota_state (
        profile_id TEXT PRIMARY KEY REFERENCES backend_profiles(id) ON DELETE CASCADE,
        anchor_at TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_quota_periods (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES backend_profiles(id) ON DELETE CASCADE,
        period_start TEXT NOT NULL, period_end TEXT,
        uplink_bytes BIGINT NOT NULL DEFAULT 0, downlink_bytes BIGINT NOT NULL DEFAULT 0,
        warned_at TEXT, exhausted_at TEXT,
        blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0, 1)),
        opened_command TEXT UNIQUE,
        closed_at TEXT, close_reason TEXT CHECK(close_reason IN ('period_end', 'manual', 'quota_removed')),
        closed_by TEXT)''')
    # One open period per profile; a restarted worker cannot open a second.
    conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS backend_quota_periods_open
        ON backend_quota_periods(profile_id) WHERE closed_at IS NULL''')
    conn.execute('''CREATE INDEX IF NOT EXISTS backend_quota_periods_profile
        ON backend_quota_periods(profile_id, period_start)''')
