from __future__ import annotations

import os
import subprocess
import tempfile
import unittest


class PostgresRuntimeScriptTests(unittest.TestCase):
    def _run_shell(self, script: str, env_vars: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        base_env = os.environ.copy()
        if env_vars:
            base_env.update(env_vars)
        return subprocess.run(
            ["/usr/bin/bash", "-c", script],
            cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
            env=base_env,
            text=True,
            capture_output=True,
            check=True,
            timeout=15,
        )

    def test_load_postgres_runtime_config_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = os.path.join(tmpdir, ".env")
            with open(env_path, "w", encoding="utf-8") as fh:
                fh.write("# empty configuration\n")

            script = """
set -e
read_env_value() {
  local key="$1"
  local file="$2"
  if [[ ! -f "$file" ]]; then return 0; fi
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}
source scripts/postgres_runtime.sh
load_postgres_runtime_config "$ENV_PATH" "$PWD"
printf 'backend=%s\\n' "$POSTGRES_RUNTIME_DB_BACKEND"
printf 'db=%s\\n' "$POSTGRES_RUNTIME_DB_NAME"
printf 'user=%s\\n' "$POSTGRES_RUNTIME_DB_USER"
printf 'port=%s\\n' "$POSTGRES_RUNTIME_PORT"
printf 'container=%s\\n' "$POSTGRES_RUNTIME_CONTAINER"
printf 'image=%s\\n' "$POSTGRES_RUNTIME_IMAGE"
"""
            proc = self._run_shell(script, {"ENV_PATH": env_path})
            self.assertIn("backend=postgres", proc.stdout)
            self.assertIn("db=node_plane", proc.stdout)
            self.assertIn("user=node_plane", proc.stdout)
            self.assertIn("port=55432", proc.stdout)
            self.assertIn("container=node-plane-postgres", proc.stdout)
            self.assertIn("image=postgres:16-alpine", proc.stdout)

    def test_load_postgres_runtime_config_custom_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = os.path.join(tmpdir, ".env")
            with open(env_path, "w", encoding="utf-8") as fh:
                fh.write("DB_BACKEND=postgres\n")
                fh.write("POSTGRES_DSN=postgresql://custom_u:custom_p@127.0.0.1:55433/custom_db\n")
                fh.write("NODE_PLANE_POSTGRES_DB=custom_db\n")
                fh.write("NODE_PLANE_POSTGRES_USER=custom_u\n")
                fh.write("NODE_PLANE_POSTGRES_PASSWORD=custom_p\n")
                fh.write("NODE_PLANE_POSTGRES_PORT=55433\n")
                fh.write("NODE_PLANE_POSTGRES_CONTAINER=custom-pg\n")
                fh.write("NODE_PLANE_POSTGRES_IMAGE=postgres:16.4\n")

            script = """
set -e
read_env_value() {
  local key="$1"
  local file="$2"
  if [[ ! -f "$file" ]]; then return 0; fi
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}
source scripts/postgres_runtime.sh
load_postgres_runtime_config "$ENV_PATH" "$PWD"
printf 'db=%s\\n' "$POSTGRES_RUNTIME_DB_NAME"
printf 'user=%s\\n' "$POSTGRES_RUNTIME_DB_USER"
printf 'port=%s\\n' "$POSTGRES_RUNTIME_PORT"
printf 'container=%s\\n' "$POSTGRES_RUNTIME_CONTAINER"
printf 'image=%s\\n' "$POSTGRES_RUNTIME_IMAGE"
"""
            proc = self._run_shell(script, {"ENV_PATH": env_path})
            self.assertIn("db=custom_db", proc.stdout)
            self.assertIn("user=custom_u", proc.stdout)
            self.assertIn("port=55433", proc.stdout)
            self.assertIn("container=custom-pg", proc.stdout)
            self.assertIn("image=postgres:16.4", proc.stdout)

    def test_dsn_uses_managed_local_postgres(self) -> None:
        script = """
set -e
source scripts/postgres_runtime.sh
POSTGRES_RUNTIME_PORT="55432"
POSTGRES_RUNTIME_DB_NAME="node_plane"

if dsn_uses_managed_local_postgres "postgresql://user:pass@127.0.0.1:55432/node_plane"; then
  printf 'local=1\\n'
else
  printf 'local=0\\n'
fi

if dsn_uses_managed_local_postgres "postgresql://user:pass@remote-host:55432/node_plane"; then
  printf 'remote=1\\n'
else
  printf 'remote=0\\n'
fi

if dsn_uses_managed_local_postgres "postgresql://user:pass@127.0.0.1:5432/node_plane"; then
  printf 'wrong_port=1\\n'
else
  printf 'wrong_port=0\\n'
fi

if dsn_uses_managed_local_postgres ""; then
  printf 'empty=1\\n'
else
  printf 'empty=0\\n'
fi
"""
        proc = self._run_shell(script)
        self.assertIn("local=1", proc.stdout)
        self.assertIn("remote=0", proc.stdout)
        self.assertIn("wrong_port=0", proc.stdout)
        self.assertIn("empty=0", proc.stdout)

    def test_env_set_updates_and_creates_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = os.path.join(tmpdir, "subdir", ".env")

            script = """
set -e
source scripts/postgres_runtime.sh
env_set "$ENV_PATH" "FOO" "bar"
env_set "$ENV_PATH" "KEY" "initial"
env_set "$ENV_PATH" "KEY" "updated"
"""
            self._run_shell(script, {"ENV_PATH": env_path})
            with open(env_path, "r", encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("FOO=bar", content)
            self.assertIn("KEY=updated", content)
            self.assertNotIn("KEY=initial", content)

    def test_auto_provision_simple_postgres_skips_external_dsn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = os.path.join(tmpdir, ".env")
            with open(env_path, "w", encoding="utf-8") as fh:
                fh.write("POSTGRES_DSN=postgresql://remote_user:secret@database.internal:5432/prod_db\n")

            script = """
set -e
read_env_value() {
  local key="$1"
  local file="$2"
  if [[ ! -f "$file" ]]; then return 0; fi
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}
source scripts/postgres_runtime.sh
docker_is_usable() {
  echo "docker_is_usable should not be called" >&2
  exit 2
}
auto_provision_simple_postgres "$ENV_PATH" "$SHARED_DIR" 0
printf 'status=success\\n'
"""
            proc = self._run_shell(script, {"ENV_PATH": env_path, "SHARED_DIR": tmpdir})
            self.assertIn("status=success", proc.stdout)

    def test_auto_provision_simple_postgres_docker_unavailable_soft_return(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = os.path.join(tmpdir, ".env")
            with open(env_path, "w", encoding="utf-8") as fh:
                fh.write("# no dsn\n")

            script = """
set -e
read_env_value() {
  local key="$1"
  local file="$2"
  if [[ ! -f "$file" ]]; then return 0; fi
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}
source scripts/postgres_runtime.sh
docker_is_usable() {
  return 1
}
install_docker_if_missing() {
  echo "install_docker_if_missing should not be called" >&2
  exit 2
}
auto_provision_simple_postgres "$ENV_PATH" "$SHARED_DIR" 0
printf 'status=soft_skip\\n'
"""
            proc = self._run_shell(script, {"ENV_PATH": env_path, "SHARED_DIR": tmpdir})
            self.assertIn("status=soft_skip", proc.stdout)


if __name__ == "__main__":
    unittest.main()
