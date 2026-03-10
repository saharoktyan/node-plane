# app/storage/json_store.py
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from filelock import FileLock


@dataclass(frozen=True)
class JsonStore:
    path: str

    def _lock_path(self) -> str:
        return self.path + ".lock"

    def read(self) -> Dict[str, Any]:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        lock = FileLock(self._lock_path())
        with lock:
            if not os.path.exists(self.path):
                return {}
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except Exception:
                # corrupted or partial; do not crash bot
                return {}

    def write(self, data: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        lock = FileLock(self._lock_path())
        with lock:
            tmp_fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=os.path.dirname(self.path))
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.path)  # atomic
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

    def update(self, mutator: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        data = self.read()
        new_data = mutator(data)
        if not isinstance(new_data, dict):
            raise ValueError("JsonStore.update mutator must return dict")
        self.write(new_data)
        return new_data
