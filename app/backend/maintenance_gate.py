"""Serialize new work against destructive controller maintenance admission."""

from .authorization import AccessDenied


def active(conn):
    cleanup = conn.execute("""SELECT id FROM backend_system_cleanup_jobs
        WHERE status IN ('queued','running','awaiting_shutdown','blocked') LIMIT 1""").fetchone()
    return cleanup or conn.execute('SELECT job_id FROM backend_controller_update_gate WHERE id=1').fetchone()


def admit(conn, *, allow_restore=False):
    conn.execute("UPDATE backend_account_guard SET revision=revision+1 WHERE id=1")
    if active(conn):
        updating = conn.execute('SELECT job_id FROM backend_controller_update_gate WHERE id=1').fetchone()
        raise AccessDenied('update_pending' if updating else 'system_cleanup_in_progress', 409)
    if not allow_restore and conn.execute("""SELECT id FROM backend_backup_jobs
        WHERE action='restore' AND status IN ('awaiting_executor','running') AND COALESCE(phase,'') != 'reconnecting' LIMIT 1""").fetchone():
        raise AccessDenied('restore_in_progress', 409)


def core_upgrade(conn):
    """A core-only upgrade pauses all previously queued node dispatch."""
    return conn.execute("""SELECT 1 FROM backend_controller_update_gate g
        JOIN backend_update_jobs j ON j.id=g.job_id WHERE j.kind='version' LIMIT 1""").fetchone() is not None
