import os

from .schema import ensure_schema
from .postgres_db import PostgresDB
from .types import DatabaseBackend


def get_db() -> DatabaseBackend:
    import config

    backend = str(config.DB_BACKEND or os.getenv("DB_BACKEND", "postgres")).strip().lower() or "postgres"
    if backend == "sqlite":
        raise ValueError(
            "DB_BACKEND=sqlite is supported only as a legacy migration source; runtime requires DB_BACKEND=postgres"
        )
    postgres_dsn = os.getenv("POSTGRES_DSN", "").strip() or str(config.POSTGRES_DSN or "").strip()
    return PostgresDB(postgres_dsn)


__all__ = ["DatabaseBackend", "PostgresDB", "ensure_schema", "get_db"]
