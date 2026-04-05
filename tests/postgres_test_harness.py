from __future__ import annotations

import os
import sqlite3
import sys
import types


def _sqlite_path_from_dsn(dsn: str) -> str:
    raw = str(dsn or "").strip()
    if not raw:
        raise ValueError("POSTGRES_DSN is required for fake postgres test harness")
    if raw.startswith("sqlite://"):
        return raw.removeprefix("sqlite://")
    return raw


def install_fake_psycopg() -> None:
    existing = sys.modules.get("psycopg")
    if existing is not None and getattr(existing, "__fake__", False):
        return

    psycopg_module = types.ModuleType("psycopg")
    psycopg_module.__fake__ = True
    rows_module = types.ModuleType("psycopg.rows")
    rows_module.dict_row = object()

    class _FakePsycopgConnection:
        def __init__(self, dsn: str, autocommit: bool = False) -> None:
            path = _sqlite_path_from_dsn(dsn)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._conn = sqlite3.connect(path)
            self._conn.row_factory = sqlite3.Row
            if autocommit:
                self._conn.isolation_level = None

        def execute(self, query: str, params=None):
            sql = str(query).replace("%s", "?")
            if params is None:
                return self._conn.execute(sql)
            return self._conn.execute(sql, params)

        def commit(self) -> None:
            self._conn.commit()

        def rollback(self) -> None:
            self._conn.rollback()

        def close(self) -> None:
            self._conn.close()

    def connect(dsn: str, row_factory=None, autocommit: bool = False):
        _ = row_factory
        return _FakePsycopgConnection(dsn, autocommit=autocommit)

    psycopg_module.connect = connect
    psycopg_module.rows = rows_module
    sys.modules["psycopg"] = psycopg_module
    sys.modules["psycopg.rows"] = rows_module


def configure_postgres_test_env(base_dir: str, db_name: str = "bot.pg.sqlite3") -> str:
    os.environ["NODE_PLANE_BASE_DIR"] = base_dir
    os.environ["DB_BACKEND"] = "postgres"
    os.environ["POSTGRES_DSN"] = os.path.join(base_dir, db_name)
    os.environ.setdefault("SQLITE_DB_PATH", os.path.join(base_dir, "bot.sqlite3"))
    os.environ.setdefault("SUBS_DB_PATH", os.path.join(base_dir, "subs.json"))
    os.environ.setdefault("USERS_DB_PATH", os.path.join(base_dir, "users.json"))
    os.environ.setdefault("WG_DB_PATH", os.path.join(base_dir, "wg_db.json"))
    install_fake_psycopg()
    return os.environ["POSTGRES_DSN"]
