from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol


class DatabaseBackend(Protocol):
    def connect(self) -> AbstractContextManager[Any]:
        ...

    def transaction(self) -> AbstractContextManager[Any]:
        ...
