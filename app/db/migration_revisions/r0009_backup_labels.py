"""Optional names for manual backup commands without changing older journals."""


def upgrade(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS backend_backup_labels (
        job_id TEXT PRIMARY KEY REFERENCES backend_backup_jobs(id) ON DELETE CASCADE,
        name TEXT NOT NULL)""")
