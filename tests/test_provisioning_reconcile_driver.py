from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.postgres_test_harness import configure_postgres_test_env

TESTS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
APP_ROOT = os.path.join(REPO_ROOT, "app")
TMPDIR = tempfile.mkdtemp(prefix="node-plane-test-")
configure_postgres_test_env(TMPDIR)
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services import provisioning_state
from services.node_driver_client import DriverRemoteProfileRecord


class ProvisioningReconcileDriverTests(unittest.TestCase):
    def test_reconcile_xray_marks_provisioned_when_remote_matches_uuid(self) -> None:
        upserts: list[tuple[str, str, str, str, str | None, str | None]] = []

        fake_driver = SimpleNamespace(
            list_remote_profiles=lambda server_key, protocol_kind=None: [
                DriverRemoteProfileRecord(
                    profile_name="alice",
                    protocol_kind="xray",
                    remote_id="uuid-1",
                    status="observed",
                    node_key=server_key,
                )
            ]
        )

        with patch("services.node_driver.get_node_driver", return_value=fake_driver), patch(
            "services.profile_state.profile_store.read",
            return_value={"alice": {"uuid": "uuid-1", "protocols": ["ga"]}},
        ), patch(
            "domain.servers.get_access_methods_for_codes",
            return_value=[SimpleNamespace(protocol_kind="xray", server_key="de")],
        ), patch.object(
            provisioning_state, "list_server_provisioning_states", return_value=[]
        ), patch.object(
            provisioning_state, "upsert_profile_server_state", side_effect=lambda *args, **kwargs: upserts.append(
                (
                    str(args[0]),
                    str(args[1]),
                    str(args[2]),
                    str(kwargs.get("status") or ""),
                    kwargs.get("remote_id"),
                    kwargs.get("last_error"),
                )
            )
        ):
            code, out = provisioning_state.reconcile_xray_server_state("de")

        self.assertEqual(code, 0)
        self.assertIn("ready: 1", out)
        self.assertTrue(any(item[0] == "alice" and item[3] == "provisioned" for item in upserts))

    def test_reconcile_xray_marks_failed_when_remote_missing(self) -> None:
        upserts: list[tuple[str, str, str, str, str | None, str | None]] = []

        fake_driver = SimpleNamespace(list_remote_profiles=lambda server_key, protocol_kind=None: [])

        with patch("services.node_driver.get_node_driver", return_value=fake_driver), patch(
            "services.profile_state.profile_store.read",
            return_value={"alice": {"uuid": "uuid-1", "protocols": ["ga"]}},
        ), patch(
            "domain.servers.get_access_methods_for_codes",
            return_value=[SimpleNamespace(protocol_kind="xray", server_key="de")],
        ), patch.object(
            provisioning_state, "list_server_provisioning_states", return_value=[]
        ), patch.object(
            provisioning_state, "upsert_profile_server_state", side_effect=lambda *args, **kwargs: upserts.append(
                (
                    str(args[0]),
                    str(args[1]),
                    str(args[2]),
                    str(kwargs.get("status") or ""),
                    kwargs.get("remote_id"),
                    kwargs.get("last_error"),
                )
            )
        ):
            code, out = provisioning_state.reconcile_xray_server_state("de")

        self.assertEqual(code, 0)
        self.assertIn("failed: 1", out)
        self.assertTrue(any(item[0] == "alice" and item[3] == "failed" for item in upserts))

    def test_reconcile_awg_marks_provisioned_when_remote_present(self) -> None:
        upserts: list[tuple[str, str, str, str, str | None, str | None]] = []

        fake_driver = SimpleNamespace(
            list_remote_profiles=lambda server_key, protocol_kind=None: [
                DriverRemoteProfileRecord(
                    profile_name="alice",
                    protocol_kind="awg",
                    remote_id="alice",
                    status="observed",
                    node_key=server_key,
                )
            ]
        )

        with patch("services.node_driver.get_node_driver", return_value=fake_driver), patch(
            "services.profile_state.profile_store.read",
            return_value={"alice": {"protocols": ["ga"]}},
        ), patch(
            "domain.servers.get_access_methods_for_codes",
            return_value=[SimpleNamespace(protocol_kind="awg", server_key="de")],
        ), patch.object(
            provisioning_state, "list_server_provisioning_states", return_value=[]
        ), patch.object(
            provisioning_state, "upsert_profile_server_state", side_effect=lambda *args, **kwargs: upserts.append(
                (
                    str(args[0]),
                    str(args[1]),
                    str(args[2]),
                    str(kwargs.get("status") or ""),
                    kwargs.get("remote_id"),
                    kwargs.get("last_error"),
                )
            )
        ):
            code, out = provisioning_state.reconcile_awg_server_state("de")

        self.assertEqual(code, 0)
        self.assertIn("ready: 1", out)
        self.assertTrue(any(item[0] == "alice" and item[3] == "provisioned" for item in upserts))


if __name__ == "__main__":
    unittest.main()
