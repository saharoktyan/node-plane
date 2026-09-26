from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.postgres_test_harness import configure_postgres_test_env

APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)


class ForgetServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        configure_postgres_test_env(self.tmpdir.name)
        import services.profile_state as profile_state
        import services.server_registry as server_registry

        self.profile_state = importlib.reload(profile_state)
        self.registry = importlib.reload(server_registry)
        for key in ("gone", "keep"):
            self.registry.upsert_server(
                key=key, region="test", title=key, flag="🏳️", transport="ssh",
                protocol_kinds=("xray", "awg"), public_host="127.0.0.1",
                ssh_host="127.0.0.1", ssh_user="root",
            )
        self.profile_state.profile_store.write({
            "alice": {
                "type": "none", "protocols": ["xray_gone", "awg_gone", "xray_keep"],
                "uuid": "some-uuid", "xray": {
                    "enabled": True, "server_short_ids": {"gone": "a1", "keep": "b2"},
                },
            },
        })
        with self.registry._db.transaction() as conn:
            conn.execute(
                "INSERT INTO profile_server_state(profile_name, server_key, protocol_kind) VALUES (?, ?, ?)",
                ("alice", "gone", "xray"),
            )
            conn.execute(
                "INSERT INTO awg_server_configs(profile_name, server_key, config_text) VALUES (?, ?, ?)",
                ("alice", "gone", "secret-config"),
            )
            conn.execute(
                "INSERT INTO traffic_samples(profile_name, server_key, protocol_kind, remote_id, sampled_at) VALUES (?, ?, ?, ?, ?)",
                ("alice", "gone", "awg", "peer", "2026-01-01"),
            )
            conn.execute(
                "CREATE TABLE alert_state(alert_key TEXT PRIMARY KEY, server_key TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO alert_state(alert_key, server_key) VALUES (?, ?)", ("gone:down", "gone"))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_removes_only_deleted_node_certificates_and_target(self) -> None:
        root = Path(self.tmpdir.name)
        tls = root / "driver-agent-tls"
        for key in ("gone", "keep"):
            (tls / "nodes" / key).mkdir(parents=True)
            (tls / "nodes" / key / "server.key").write_text("private")
        (tls / "ca.key").write_text("shared-private")
        env = root / ".env"
        env.write_text("NODE_AGENT_TARGETS=gone=host:50061,keep=other:50061\nOTHER=value\n")
        with patch.dict(os.environ, {"NODE_PLANE_SHARED_DIR": str(root)}):
            self.registry._remove_node_credentials("gone")
        self.assertFalse((tls / "nodes" / "gone").exists())
        self.assertTrue((tls / "nodes" / "keep" / "server.key").exists())
        self.assertTrue((tls / "ca.key").exists())
        self.assertEqual(env.read_text(), "NODE_AGENT_TARGETS=keep=other:50061\nOTHER=value\n")

    def test_forget_offline_server_removes_only_its_controller_state(self) -> None:
        # An old AWG grant can remain after the node's protocol list changes.
        self.registry.update_server_fields("gone", protocol_kinds=("xray",))
        self.assertTrue(self.registry.forget_server("gone"))
        self.assertIsNone(self.registry.get_server("gone"))
        self.assertIsNotNone(self.registry.get_server("keep"))
        self.assertFalse(self.registry.forget_server("gone"))
        profile = self.profile_state.get_profile("alice")
        self.assertEqual(profile["protocols"], ["xray_keep"])
        self.assertEqual(profile["xray"]["server_short_ids"], {"keep": "b2"})
        with self.registry._db.connect() as conn:
            for table in ("profile_server_state", "awg_server_configs", "traffic_samples", "alert_state"):
                row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE server_key = ?", ("gone",)).fetchone()
                self.assertEqual(row["n"], 0, table)


if __name__ == "__main__":
    unittest.main()
