from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from config import POSTGRES_DSN


def _translate_query(query: str, params: object | None) -> str:
    if params is None:
        return query
    return query.replace("?", "%s")


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


class PostgresConnectionAdapter:
    backend_name = "postgres"

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, query: str, params: object | None = None) -> PostgresResult:
        cursor = self._conn.execute(_translate_query(query, params), params)
        return PostgresResult(cursor)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


class PostgresDB:
    backend_name = "postgres"

    def __init__(self, dsn: str | None = None) -> None:
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

    @contextmanager
    def connect(self) -> Iterator[PostgresConnectionAdapter]:
        conn = self._open()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[PostgresConnectionAdapter]:
        conn = self._open()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
