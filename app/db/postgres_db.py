from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from typing import Any, Iterator

from config import POSTGRES_DSN

_WORKER_CONNECTIONS = ContextVar('worker_connections', default=False)
WORK_CHANNEL = 'node_plane_work'
DELIVERY_CHANNEL = 'node_plane_delivery'
_DELIVERY_TABLES = ('backend_alert_deliveries', 'backend_announcement_deliveries', 'backend_account_menu_refresh', 'backend_access_request_messages')
_QUEUE_TABLES = ('backend_operation_tasks', 'backend_node_jobs', 'backend_node_settings_tasks',
    'backend_agent_rollouts', 'backend_node_bootstraps', 'backend_node_removals',
    'backend_config_issuances', 'backend_temporary_configs', 'backend_update_jobs',
    'backend_backup_jobs', 'backend_system_cleanup_jobs')

@contextmanager
def worker_connections():
    """Reuse idle connections and suppress self-generated wakeups in the worker."""
    token = _WORKER_CONNECTIONS.set(True)
    try:
        yield
    finally:
        _WORKER_CONNECTIONS.reset(token)



def _translate_query(query: str, params: object | None) -> str:
    if params is None:
        return query
    
    # psycopg parses percent placeholders even inside SQL string literals.
    # Escape literal percent signs before introducing our positional %s markers.
    parts = query.replace('%', '%%').split("'")
    for i in range(0, len(parts), 2):
        parts[i] = parts[i].replace("?", "%s")
        
    return "'".join(parts)


class PostgresResult:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def fetchone(self) -> dict[str, Any] | None:
        row = self._cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row


class PostgresConnectionAdapter:
    backend_name = "postgres"

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._wake = False
        self._delivery_wake = False

    def execute(self, query: str, params: object | None = None) -> PostgresResult:
        cursor = self._conn.execute(_translate_query(query, params), params)
        sql = query.lstrip().lower()
        changed = (sql.startswith(('insert ', 'update ', 'delete '))
            and isinstance(cursor.rowcount, int) and cursor.rowcount > 0)
        if changed and any(table in sql for table in _DELIVERY_TABLES):
            self._delivery_wake = True
        if (changed and not _WORKER_CONNECTIONS.get()
            and any(table in sql for table in _QUEUE_TABLES)):
            self._wake = True
        return PostgresResult(cursor)

    def commit(self) -> None:
        if self._delivery_wake:
            self._conn.execute("SELECT pg_notify('node_plane_delivery', '')")
        if self._wake:
            self._conn.execute("SELECT pg_notify('node_plane_work', '')")
        self._conn.commit()
        self._wake = False
        self._delivery_wake = False

    def rollback(self) -> None:
        self._conn.rollback()
        self._wake = False
        self._delivery_wake = False

    def close(self) -> None:
        self._conn.close()


class PostgresDB:
    backend_name = "postgres"

    def __init__(self, dsn: str | None = None) -> None:
        self._idle = []
        self._pool_lock = Lock()
        self.reuse_connections = False
        self.max_idle = 2
        self._closed = False
        self.dsn = str(dsn or POSTGRES_DSN or "").strip()
        if not self.dsn:
            raise ValueError("POSTGRES_DSN is required when DB_BACKEND=postgres")

    def _open(self) -> PostgresConnectionAdapter:
        try:
            from psycopg import connect
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("psycopg is required for PostgreSQL support") from exc
        conn = connect(self.dsn, row_factory=dict_row)
        return PostgresConnectionAdapter(conn)

    def close(self):
        with self._pool_lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for conn in idle:
            conn.close()

    def _acquire(self):
        if self._closed:
            raise RuntimeError('Database connections are closed')
        if self.reuse_connections or _WORKER_CONNECTIONS.get():
            with self._pool_lock:
                while self._idle:
                    conn = self._idle.pop()
                    if not conn._conn.closed:
                        return conn
                    conn.close()
        return self._open()

    def _release(self, conn):
        if (self.reuse_connections or _WORKER_CONNECTIONS.get()) and not conn._conn.closed:
            with self._pool_lock:
                if not self._closed and len(self._idle) < self.max_idle:
                    self._idle.append(conn)
                    return
        conn.close()

    @contextmanager
    def connect(self) -> Iterator[PostgresConnectionAdapter]:
        conn = self._acquire()
        reusable = True
        try:
            yield conn
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                reusable = False
            raise
        finally:
            if reusable:
                self._release(conn)
            else:
                conn.close()

    transaction = connect
