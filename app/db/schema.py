"""PostgreSQL runtime schema entry point."""
from .postgres_schema import ensure_schema

__all__ = ["ensure_schema"]
