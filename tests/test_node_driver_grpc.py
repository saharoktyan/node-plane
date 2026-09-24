from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

APP_ROOT = str(Path(__file__).resolve().parents[1] / "app")
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

from services.node_driver_client import DriverOperation
from services.node_driver_grpc import GrpcNodeDriverClient


class GrpcCommandIdentityTests(unittest.TestCase):
    CASES = [
        ("sync_node_env", "SyncNodeEnv", "node", ("node",)),
        ("probe_node", "ProbeNode", "node", ("node",)),
        ("check_ports", "CheckPorts", "node", ("node",)),
        ("open_ports", "OpenPorts", "node", ("node",)),
        ("install_docker", "InstallDocker", "node", ("node",)),
        ("bootstrap_node", "BootstrapNode", "runtime", ("node",)),
        ("reinstall_node", "ReinstallNode", "runtime", ("node",)),
        ("delete_runtime", "DeleteRuntime", "runtime", ("node",)),
        ("full_cleanup_node", "FullCleanupNode", "runtime", ("node",)),
        ("sync_runtime", "SyncRuntime", "runtime", ("node",)),
        ("sync_xray", "SyncXray", "runtime", ("node",)),
        ("reconcile_node", "ReconcileNode", "provisioning", ("node",)),
        ("reconcile_profile", "ReconcileProfile", "provisioning", ("alice",)),
        ("ensure_profile_on_node", "EnsureProfileOnNode", "provisioning", ("node", "alice", ["awg"])),
        ("delete_profile_from_node", "DeleteProfileFromNode", "provisioning", ("node", "alice", ["awg"])),
    ]

    def client_for(self, rpc_name: str, service: str):
        client = GrpcNodeDriverClient(target="unused:50051")
        client._ensure_client = Mock()
        client._types_pb2 = SimpleNamespace(ProfileSpec=SimpleNamespace, AwgSpec=SimpleNamespace, XraySpec=SimpleNamespace)
        setattr(client, f"_{service}_pb2", SimpleNamespace(**{rpc_name + "Request": SimpleNamespace}))
        rpc = Mock(return_value=SimpleNamespace(operation_id="existing-id"))
        setattr(client, f"_{service}_stub", SimpleNamespace(**{rpc_name: rpc}))
        client.get_operation = Mock(return_value=DriverOperation(operation_id="existing-id", kind="test", status="RUNNING"))
        return client, rpc

    def test_all_commands_forward_the_supplied_key_on_resubmission(self):
        for method, rpc_name, service, args in self.CASES:
            with self.subTest(method=method):
                client, rpc = self.client_for(rpc_name, service)
                for _ in range(2):
                    result = getattr(client, method)(*args, command_id="saved-command-1")
                    self.assertEqual(result.operation_id, "existing-id")
                    self.assertEqual(result.status, "RUNNING")
                self.assertEqual(rpc.call_count, 2)
                for call in rpc.call_args_list:
                    self.assertEqual(call.kwargs["metadata"], (("x-node-plane-command-id", "saved-command-1"),))

    def test_calls_without_key_do_not_invent_an_identity(self):
        client, rpc = self.client_for("InstallDocker", "node")
        client.install_docker("node")
        self.assertEqual(rpc.call_args.kwargs["metadata"], ())

    def test_transport_error_never_authorizes_automatic_reexecution(self):
        for key in (None, "saved-command-1"):
            with self.subTest(command_id=key):
                client, rpc = self.client_for("DeleteRuntime", "runtime")
                rpc.side_effect = TimeoutError("reply lost")
                result = client.delete_runtime("node", command_id=key)
                self.assertFalse(result.error.retryable)
                self.assertEqual(result.error.code, "grpc_error")
                self.assertEqual(result.operation_id, "")
                rpc.assert_called_once()
                client.get_operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
