"""Serialize new work against destructive controller maintenance admission."""

from .authorization import AccessDenied


def active(conn):
    return conn.execute("""SELECT id FROM backend_system_cleanup_jobs
        WHERE status IN ('queued','running','awaiting_shutdown','blocked') LIMIT 1""").fetchone()


def admit(conn):
    conn.execute("UPDATE backend_account_guard SET revision=revision+1 WHERE id=1")
    if active(conn):
        raise AccessDenied("system_cleanup_in_progress", 409)
