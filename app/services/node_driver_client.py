from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass(frozen=True)
class DriverError:
    code: str
    summary: str
    detail: str = ""
    retryable: bool = False


@dataclass(frozen=True)
class DriverNodeCapabilities:
    supports_awg: bool
    supports_xray: bool
    supports_telemetry: bool
    supports_bootstrap: bool


@dataclass(frozen=True)
class DriverNodeHealth:
    connectivity: str
    last_seen_at: str = ""
    summary: str = ""


@dataclass(frozen=True)
class DriverNode:
    node_key: str
    transport: str
    version: str
    state: str
    title: str
    flag: str
    region: str
    public_host: str
    capabilities: DriverNodeCapabilities
    health: DriverNodeHealth
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DriverRuntimeStatus:
    state: str
    version: str
    commit: str
    expected_version: str
    expected_commit: str
    message: str


@dataclass(frozen=True)
class DriverRemoteProfileRecord:
    profile_name: str
    protocol_kind: str
    remote_id: str
    status: str
    node_key: str


@dataclass(frozen=True)
class DriverProfileUsage:
    profile_name: str
    protocol_kind: str
    rx_bytes: int
    tx_bytes: int
    total_bytes: int
    samples: int
    peers: int


@dataclass(frozen=True)
class DriverOperation:
    operation_id: str
    kind: str
    status: str
    node_key: str = ""
    profile_name: str = ""
    started_at: str = ""
    updated_at: str = ""
    finished_at: str = ""
    progress_message: str = ""
    result_json: str = ""
    error: Optional[DriverError] = None


class NodeDriverClient(Protocol):
    def get_node(self, node_key: str) -> Optional[DriverNode]:
        ...

    def list_nodes(self, include_disabled: bool = False) -> list[DriverNode]:
        ...

    def get_runtime_status(self, node_key: str) -> DriverRuntimeStatus:
        ...

    def list_nodes_needing_runtime_sync(self) -> list[DriverNode]:
        ...

    def sync_node_env(self, node_key: str) -> DriverOperation:
        ...

    def sync_runtime(self, node_key: str) -> DriverOperation:
        ...

    def sync_xray(self, node_key: str) -> DriverOperation:
        ...

    def probe_node(self, node_key: str) -> DriverOperation:
        ...

    def check_ports(self, node_key: str) -> DriverOperation:
        ...

    def open_ports(self, node_key: str) -> DriverOperation:
        ...

    def install_docker(self, node_key: str) -> DriverOperation:
        ...

    def bootstrap_node(self, node_key: str, preserve_config: bool = False) -> DriverOperation:
        ...

    def reinstall_node(self, node_key: str, preserve_config: bool = False) -> DriverOperation:
        ...

    def delete_runtime(self, node_key: str, preserve_config: bool = False) -> DriverOperation:
        ...

    def full_cleanup_node(self, node_key: str, remove_ssh_key: bool = False) -> DriverOperation:
        ...

    def ensure_profile_on_node(
        self,
        node_key: str,
        profile_name: str,
        protocol_kinds: list[str],
        *,
        xray_uuid: str = "",
        xray_short_id: str = "",
        awg_peer_name: str = "",
    ) -> DriverOperation:
        ...

    def delete_profile_from_node(self, node_key: str, profile_name: str, protocol_kinds: list[str]) -> DriverOperation:
        ...

    def reconcile_node(self, node_key: str) -> DriverOperation:
        ...

    def reconcile_profile(self, profile_name: str) -> DriverOperation:
        ...

    def get_operation(self, operation_id: str) -> Optional[DriverOperation]:
        ...

    def list_operations(
        self,
        *,
        node_key: str = "",
        profile_name: str = "",
        status: str = "",
        limit: int = 20,
    ) -> list[DriverOperation]:
        ...

    def get_profile_usage(self, profile_name: str, protocol_kind: str = "awg") -> DriverProfileUsage:
        ...

    def list_remote_profiles(self, node_key: str, protocol_kind: Optional[str] = None) -> list[DriverRemoteProfileRecord]:
        ...
