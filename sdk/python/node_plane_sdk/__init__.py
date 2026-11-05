"""Python client of the Node Plane API (`api/openapi.json`).

The contract's rules live here once for every Python client: bearer
authentication, acting for a Telegram identity, `Idempotency-Key` for
commands, `If-Match` revisions, error responses and cursor pages.
"""
from .client import Client
from .errors import ApiError
from .pagination import collect

__all__ = ['ApiError', 'Client', 'collect']
