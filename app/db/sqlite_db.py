from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator


class SQLiteDB:
    def __init__(self, path: str) -> None:
        self.path = path

    def _harden_permissions(self) -> None:
        dir_path = os.path.dirname(self.path)
        os.makedirs(dir_path, mode=0o700, exist_ok=True)
        try:
            os.chmod(dir_path, 0o700)
        except OSError:
            pass
        for suffix in ("", "-wal", "-shm"):
            candidate = f"{self.path}{suffix}"
            if not os.path.exists(candidate):
                continue
            try:
                os.chmod(candidate, 0o600)
            except OSError:
                pass

    def _open(self) -> sqlite3.Connection:
        self._harden_permissions()
        conn = sqlite3.connect(self.path, timeout=5.0)
        self._harden_permissions()
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        self._harden_permissions()
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
