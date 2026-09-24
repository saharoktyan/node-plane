from __future__ import annotations

import importlib
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from tests.postgres_test_harness import configure_postgres_test_env

TESTS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
APP_ROOT = os.path.join(REPO_ROOT, "app")
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

telegram_module = types.ModuleType("telegram")
telegram_module.Update = object
sys.modules.setdefault("telegram", telegram_module)
sys.modules["telegram"] = telegram_module


class SystemResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = self.tmpdir.name
        configure_postgres_test_env(base)
        os.environ["NODE_PLANE_APP_DIR"] = base
        os.environ["NODE_PLANE_SHARED_DIR"] = base
        os.environ["SQLITE_DB_PATH"] = os.path.join(base, "bot.sqlite3")

        import config
        import services.app_settings as app_settings
        import services.backups as backups
        import services.profile_state as profile_state
        import services.server_registry as server_registry
        import services.system_reset as system_reset

        self.config = importlib.reload(config)
        self.app_settings = importlib.reload(app_settings)
        self.backups = importlib.reload(backups)
        self.profile_state = importlib.reload(profile_state)
        self.server_registry = importlib.reload(server_registry)
        self.system_reset = importlib.reload(system_reset)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_factory_reset_clears_local_state_and_ssh_material(self) -> None:
        os.makedirs(self.config.SSH_DIR, exist_ok=True)
        with open(os.path.join(self.config.SSH_DIR, "id_ed25519"), "w", encoding="utf-8") as fh:
            fh.write("secret")
        backup_dir = self.backups.get_backup_dir()
        with open(os.path.join(backup_dir, "bot-test.sqlite3"), "w", encoding="utf-8") as fh:
            fh.write("backup")

        self.server_registry.upsert_server(
            key="spb1",
            region="ru",
            title="Saint-Petersburg",
            flag="🇷🇺",
            transport="local",
            protocol_kinds=("xray",),
            public_host="127.0.0.1",
        )
        self.profile_state.profile_store.write(
            {
                "alice": {
                    "type": "none",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "protocols": ["spb1-xray"],
                }
            }
        )
        self.profile_state.user_store.upsert_user(1, username="alice", profile_name="alice", access_granted=True)
        self.app_settings.set_menu_title("Test Title")
        with self.system_reset._db.transaction() as conn:
            conn.execute(
                """INSERT INTO driver_commands(source_ref, command_id, kind, request_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                ("telegram:1", "saved-1", "bootstrap_node", "{}", "2026-04-01T00:00:00Z", "2026-04-01T00:00:00Z"),
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_state (
                    alert_key TEXT PRIMARY KEY,
                    server_key TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    active INTEGER NOT NULL DEFAULT 0,
                    hit_streak INTEGER NOT NULL DEFAULT 0,
                    clear_streak INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL DEFAULT '',
                    last_sent_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                INSERT INTO alert_state(
                    alert_key, server_key, alert_type, severity, payload_json,
                    active, hit_streak, clear_streak, first_seen_at, last_seen_at, last_sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("spb1:disk_low", "spb1", "disk_low", "warn", "{}", 1, 1, 0, "2026-04-01T00:00:00Z", "2026-04-01T00:00:00Z", ""),
            )

        rc, out = self.system_reset.run_factory_reset(cleanup_nodes=False, stop_local_runtime=False)

        self.assertEqual(rc, 0)
        self.assertIn("Node Plane state removed.", out)
        self.assertEqual(self.server_registry.list_servers(include_disabled=True), [])
        self.assertEqual(self.profile_state.profile_store.read(), {})
        self.assertEqual(self.profile_state.user_store.read(), {})
        self.assertEqual(os.listdir(self.config.SSH_DIR), [])
        self.assertEqual(os.listdir(backup_dir), [])
        self.assertEqual(self.app_settings.get_menu_title(), self.config.MENU_TITLE)
        with self.system_reset._db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM alert_state").fetchone()
            self.assertEqual(int(row["c"]), 0)
            row = conn.execute("SELECT COUNT(*) AS c FROM driver_commands").fetchone()
            self.assertEqual(int(row["c"]), 0)

    def test_factory_reset_requests_remote_key_cleanup_for_ssh_nodes(self) -> None:
        self.server_registry.upsert_server(
            key="nl1",
            region="nl",
            title="Netherlands",
            flag="🇳🇱",
            transport="ssh",
            protocol_kinds=("xray",),
            public_host="1.2.3.4",
            ssh_host="1.2.3.4",
            ssh_user="root",
        )
        calls = []

        def fake_cleanup(server_key: str, remove_ssh_key: bool = False):
            calls.append((server_key, remove_ssh_key))
            return 0, "ok"

        self.system_reset.full_cleanup_server = fake_cleanup  # type: ignore[attr-defined]
        rc, out = self.system_reset.run_factory_reset(cleanup_nodes=True, stop_local_runtime=False)

        self.assertEqual(rc, 0)
        self.assertIn(("nl1", True), calls)
        self.assertIn("bot SSH key removal requested for SSH nodes", out)

    def test_factory_reset_with_nodes_cleans_local_managed_runtime(self) -> None:
        with patch.object(self.system_reset, "_cleanup_local_managed_runtime", return_value=(0, "local managed runtime removed")) as mocked:
            rc, out = self.system_reset.run_factory_reset(cleanup_nodes=True, stop_local_runtime=False)
        self.assertEqual(rc, 0)
        mocked.assert_called_once()
        self.assertIn("local managed runtime removed", out)

    def test_grpc_factory_reset_cleans_registered_node_through_driver(self) -> None:
        from services.node_driver_client import DriverOperation
        from services.node_driver_grpc import GrpcNodeDriverClient

        self.server_registry.upsert_server(
            key="nl1", region="nl", title="Netherlands", flag="NL", transport="ssh",
            protocol_kinds=("xray",), public_host="1.2.3.4", ssh_host="1.2.3.4", ssh_user="root",
        )
        self.server_registry.upsert_server(
            key="local1", region="local", title="Local", flag="", transport="local",
            protocol_kinds=("awg",), public_host="127.0.0.1",
        )
        seen = []

        class FakeDriver(GrpcNodeDriverClient):
            def __init__(self):
                pass

            def full_cleanup_node(self, node_key, remove_ssh_key=False, *, command_id=None):
                with self_db.connect() as conn:
                    row = conn.execute("SELECT command_id FROM driver_commands WHERE source_ref = ?", (f"telegram:111:cleanup:{node_key}",)).fetchone()
                seen.append((node_key, remove_ssh_key, command_id, row["command_id"] if row else None))
                return DriverOperation(operation_id="op-cleanup", kind="full_cleanup_node", status="SUCCEEDED", node_key=node_key)

        self_db = self.system_reset._db
        with patch.object(self.system_reset, "get_node_driver", return_value=FakeDriver()), patch.object(
            self.system_reset, "full_cleanup_server"
        ) as legacy, patch.object(self.system_reset, "_cleanup_local_managed_runtime") as local_cleanup:
            rc, out = self.system_reset.run_factory_reset(cleanup_nodes=True, source_ref="telegram:111")
        self.assertEqual(rc, 0)
        self.assertEqual({entry[:2] for entry in seen}, {("nl1", True), ("local1", False)})
        self.assertTrue(all(entry[2] == entry[3] for entry in seen))
        legacy.assert_not_called()
        local_cleanup.assert_not_called()
        self.assertIn("node-agent", out)

    def test_grpc_reset_still_cleans_unregistered_host_runtime(self) -> None:
        from services.node_driver_grpc import GrpcNodeDriverClient

        class FakeDriver(GrpcNodeDriverClient):
            def __init__(self):
                pass

        with patch.object(self.system_reset, "get_node_driver", return_value=FakeDriver()), patch.object(
            self.system_reset, "_cleanup_local_managed_runtime", return_value=(0, "orphan runtime removed")
        ) as local_cleanup:
            rc, out = self.system_reset.run_factory_reset(cleanup_nodes=True, source_ref="telegram:113")
        self.assertEqual(rc, 0)
        local_cleanup.assert_called_once()
        self.assertIn("orphan runtime removed", out)

    def test_grpc_full_remove_requires_successful_node_cleanup_before_uninstall(self) -> None:
        from services.node_driver_client import DriverOperation
        from services.node_driver_grpc import GrpcNodeDriverClient

        self.server_registry.upsert_server(
            key="nl1", region="nl", title="Netherlands", flag="NL", transport="ssh",
            protocol_kinds=("xray",), public_host="1.2.3.4", ssh_host="1.2.3.4", ssh_user="root",
        )

        class FakeDriver(GrpcNodeDriverClient):
            def __init__(self):
                pass

            def full_cleanup_node(self, node_key, remove_ssh_key=False, *, command_id=None):
                return DriverOperation(
                    operation_id="op-failed", kind="full_cleanup_node", status="FAILED",
                    node_key=node_key, progress_message="agent unreachable",
                )

        with patch.object(self.system_reset, "get_node_driver", return_value=FakeDriver()), patch.object(
            self.system_reset, "schedule_full_uninstall"
        ) as uninstall:
            rc, out = self.system_reset.run_full_remove(cleanup_nodes=True, source_ref="telegram:114")
        self.assertEqual(rc, 1)
        self.assertIn("agent unreachable", out)
        uninstall.assert_not_called()

    def test_failed_grpc_cleanup_blocks_reset_and_preserves_registry(self) -> None:
        from services.node_driver_client import DriverOperation
        from services.node_driver_grpc import GrpcNodeDriverClient

        self.server_registry.upsert_server(
            key="nl1", region="nl", title="Netherlands", flag="NL", transport="ssh",
            protocol_kinds=("xray",), public_host="1.2.3.4", ssh_host="1.2.3.4", ssh_user="root",
        )
        calls = []

        class FakeDriver(GrpcNodeDriverClient):
            def __init__(self):
                pass

            def full_cleanup_node(self, node_key, remove_ssh_key=False, *, command_id=None):
                calls.append(command_id)
                return DriverOperation(
                    operation_id="op-failed", kind="full_cleanup_node", status="FAILED",
                    node_key=node_key, progress_message="agent unreachable",
                )

            def get_operation(self, operation_id):
                return DriverOperation(
                    operation_id=operation_id, kind="full_cleanup_node", status="FAILED",
                    node_key="nl1", progress_message="agent unreachable",
                )

        with patch.object(self.system_reset, "get_node_driver", return_value=FakeDriver()):
            rc, out = self.system_reset.run_factory_reset(cleanup_nodes=True, source_ref="telegram:112")
            second_rc, _ = self.system_reset.run_factory_reset(cleanup_nodes=True, source_ref="telegram:112")
        self.assertEqual((rc, second_rc), (1, 1))
        self.assertIn("agent unreachable", out)
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(self.server_registry.get_server("nl1"))

    def test_schedule_full_uninstall_spawns_detached_cleanup(self) -> None:
        with patch.object(self.system_reset.shutil, "which", return_value=None), patch.object(self.system_reset.subprocess, "Popen") as mocked:
            rc, out = self.system_reset.schedule_full_uninstall()
        self.assertEqual(rc, 0)
        self.assertIn("Node Plane removal scheduled.", out)
        mocked.assert_called_once()

    def test_schedule_full_uninstall_prefers_systemd_run(self) -> None:
        with patch.object(self.system_reset.shutil, "which", return_value="/usr/bin/systemd-run"), patch.object(
            self.system_reset.subprocess, "run"
        ) as mocked_run:
            rc, out = self.system_reset.schedule_full_uninstall()
        self.assertEqual(rc, 0)
        self.assertIn("Node Plane removal scheduled.", out)
        mocked_run.assert_called_once()
        args = mocked_run.call_args.args[0]
        self.assertIn("systemd-run", args)
        self.assertIn("--no-block", args)

    def test_full_uninstall_script_removes_targets_before_killing_process(self) -> None:
        script = self.system_reset._build_full_uninstall_script(12345, ["/opt/node-plane", "/opt/node-plane-src"])
        self.assertIn("rm -rf -- '/opt/node-plane' >/dev/null 2>&1 || true", script)
        self.assertIn("rm -rf -- '/opt/node-plane-src' >/dev/null 2>&1 || true", script)
        self.assertLess(script.index("rm -rf -- '/opt/node-plane'"), script.index("kill 12345"))
        self.assertLess(script.index("rm -rf -- '/opt/node-plane-src'"), script.index("kill 12345"))

    def test_full_uninstall_script_removes_managed_images(self) -> None:
        script = self.system_reset._build_full_uninstall_script(12345, ["/opt/node-plane"])
        self.assertIn("docker rmi -f 'node-plane-amnezia-awg:0.2.16' >/dev/null 2>&1 || true", script)
        self.assertIn("docker rmi -f 'amneziavpn/amneziawg-go:0.2.16' >/dev/null 2>&1 || true", script)
        self.assertIn("docker image prune -af >/dev/null 2>&1 || true", script)

    def test_full_uninstall_script_runs_compose_down_when_compose_file_exists(self) -> None:
        script = self.system_reset._build_full_uninstall_script(12345, ["/opt/node-plane"])
        self.assertIn("docker compose -f ", script)
        self.assertIn(" down -v --remove-orphans >/dev/null 2>&1 || true", script)
        self.assertLess(script.index("docker compose -f "), script.index("docker rm -f node-plane"))

    def test_uninstall_targets_include_install_root(self) -> None:
        self.assertIn(os.path.abspath(self.tmpdir.name), self.system_reset._uninstall_targets())

    def test_run_full_remove_with_nodes_runs_node_cleanup_before_uninstall(self) -> None:
        self.server_registry.upsert_server(
            key="nl1",
            region="nl",
            title="Netherlands",
            flag="🇳🇱",
            transport="ssh",
            protocol_kinds=("xray",),
            public_host="1.2.3.4",
            ssh_host="1.2.3.4",
            ssh_user="root",
        )
        calls = []

        def fake_cleanup(server_key: str, remove_ssh_key: bool = False):
            calls.append((server_key, remove_ssh_key))
            return 0, "ok"

        with patch.object(self.system_reset, "full_cleanup_server", side_effect=fake_cleanup), patch.object(
            self.system_reset, "schedule_full_uninstall", return_value=(0, "Node Plane removal scheduled.")
        ):
            rc, out = self.system_reset.run_full_remove(cleanup_nodes=True)

        self.assertEqual(rc, 0)
        self.assertIn(("nl1", True), calls)
        self.assertIn("managed runtimes cleaned up on registered nodes", out)


if __name__ == "__main__":
    unittest.main()
