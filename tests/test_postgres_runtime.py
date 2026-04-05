from __future__ import annotations

import os
import subprocess
import tempfile
import unittest


class PostgresRuntimeScriptTests(unittest.TestCase):
    def test_ensure_portable_postgres_env_rewrites_legacy_shared_sqlite_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = os.path.join(tmpdir, ".env")
            with open(env_path, "w", encoding="utf-8") as fh:
                fh.write("DB_BACKEND=postgres\n")
                fh.write("POSTGRES_DSN=\n")
                fh.write("SQLITE_DB_PATH=/opt/node-plane/shared/data/bot.sqlite3\n")
                fh.write("NODE_PLANE_POSTGRES_DB=node_plane\n")
                fh.write("NODE_PLANE_POSTGRES_USER=node_plane\n")
                fh.write("NODE_PLANE_POSTGRES_PASSWORD=secret\n")
                fh.write("NODE_PLANE_POSTGRES_IMAGE=postgres:16-alpine\n")

            script = """
set -e
read_env_value() {
  local key="$1"
  local file="$2"
  if [[ ! -f "$file" ]]; then
    return 0
  fi
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}
set_env_value() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "$ENV_PATH"; then
    sed -i "s|^${key}=.*$|${key}=${value}|" "$ENV_PATH"
  else
    printf '%s=%s\\n' "$key" "$value" >> "$ENV_PATH"
  fi
}
source scripts/postgres_runtime.sh
ensure_portable_postgres_env "$ENV_PATH"
printf 'sqlite=%s\\n' "$(read_env_value SQLITE_DB_PATH "$ENV_PATH")"
printf 'dsn=%s\\n' "$(read_env_value POSTGRES_DSN "$ENV_PATH")"
"""
            proc = subprocess.run(
                ["/usr/bin/bash", "-lc", script],
                cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
                env={**os.environ, "ENV_PATH": env_path},
                text=True,
                capture_output=True,
                check=True,
            )

            self.assertIn("sqlite=/opt/node-plane/data/bot.sqlite3", proc.stdout)
            self.assertIn("dsn=postgresql://node_plane:secret@postgres:5432/node_plane", proc.stdout)


if __name__ == "__main__":
    unittest.main()
