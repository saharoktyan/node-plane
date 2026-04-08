from __future__ import annotations

from typing import Optional

from config import NODE_DRIVER_GRPC_TARGET, NODE_DRIVER_GRPC_TIMEOUT_SECONDS
from services.node_driver_client import (
    DriverNode,
    DriverOperation,
    DriverProfileUsage,
    DriverRemoteProfileRecord,
    DriverRuntimeStatus,
    NodeDriverClient,
)


class GrpcNodeDriverClient(NodeDriverClient):
    def __init__(self, target: str | None = None, timeout_seconds: int | None = None) -> None:
        self.target = (target or NODE_DRIVER_GRPC_TARGET).strip() or NODE_DRIVER_GRPC_TARGET
        self.timeout_seconds = int(timeout_seconds or NODE_DRIVER_GRPC_TIMEOUT_SECONDS)
        self._grpc = None
        self._channel = None
        self._stubs_ready = False

    def _ensure_client(self) -> None:
        if self._grpc is None:
            try:
                import grpc  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "gRPC backend requested but 'grpcio' is not installed. "
                    "Install gRPC dependencies or switch NODE_DRIVER_BACKEND=inprocess."
                ) from exc
            self._grpc = grpc
        if self._channel is None:
            self._channel = self._grpc.insecure_channel(self.target)
        if not self._stubs_ready:
            raise NotImplementedError(
                "GrpcNodeDriverClient is a scaffold. "
                "Generate Python stubs from proto/driver/v1 and wire RPC mappings before enabling NODE_DRIVER_BACKEND=grpc."
            )

    def get_node(self, node_key: str) -> Optional[DriverNode]:
        self._ensure_client()
        raise AssertionError("unreachable")

    def list_nodes(self, include_disabled: bool = False) -> list[DriverNode]:
        self._ensure_client()
        raise AssertionError("unreachable")

    def get_runtime_status(self, node_key: str) -> DriverRuntimeStatus:
        self._ensure_client()
        raise AssertionError("unreachable")

    def list_nodes_needing_runtime_sync(self) -> list[DriverNode]:
        self._ensure_client()
        raise AssertionError("unreachable")

    def sync_node_env(self, node_key: str) -> DriverOperation:
        self._ensure_client()
        raise AssertionError("unreachable")

    def sync_runtime(self, node_key: str) -> DriverOperation:
        self._ensure_client()
        raise AssertionError("unreachable")

    def sync_xray(self, node_key: str) -> DriverOperation:
        self._ensure_client()
        raise AssertionError("unreachable")

    def probe_node(self, node_key: str) -> DriverOperation:
        self._ensure_client()
        raise AssertionError("unreachable")

    def check_ports(self, node_key: str) -> DriverOperation:
        self._ensure_client()
        raise AssertionError("unreachable")

    def open_ports(self, node_key: str) -> DriverOperation:
        self._ensure_client()
        raise AssertionError("unreachable")

    def install_docker(self, node_key: str) -> DriverOperation:
        self._ensure_client()
        raise AssertionError("unreachable")

    def bootstrap_node(self, node_key: str, preserve_config: bool = False) -> DriverOperation:
        self._ensure_client()
        raise AssertionError("unreachable")

    def reinstall_node(self, node_key: str, preserve_config: bool = False) -> DriverOperation:
        self._ensure_client()
        raise AssertionError("unreachable")

    def delete_runtime(self, node_key: str, preserve_config: bool = False) -> DriverOperation:
        self._ensure_client()
        raise AssertionError("unreachable")

    def full_cleanup_node(self, node_key: str, remove_ssh_key: bool = False) -> DriverOperation:
        self._ensure_client()
        raise AssertionError("unreachable")

    def reconcile_node(self, node_key: str) -> DriverOperation:
        self._ensure_client()
        raise AssertionError("unreachable")

    def reconcile_profile(self, profile_name: str) -> DriverOperation:
        self._ensure_client()
        raise AssertionError("unreachable")

    def get_profile_usage(self, profile_name: str, protocol_kind: str = "awg") -> DriverProfileUsage:
        self._ensure_client()
        raise AssertionError("unreachable")

    def list_remote_profiles(self, node_key: str, protocol_kind: Optional[str] = None) -> list[DriverRemoteProfileRecord]:
        self._ensure_client()
        raise AssertionError("unreachable")
