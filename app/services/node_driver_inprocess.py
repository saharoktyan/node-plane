from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional
from uuid import uuid4

from services.node_driver_client import (
    DriverError,
    DriverNode,
    DriverNodeCapabilities,
    DriverNodeHealth,
    DriverOperation,
    DriverProfileUsage,
    DriverRemoteProfileRecord,
    NodeDriverClient,
)

if TYPE_CHECKING:
    from services.server_registry import RegisteredServer


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _operation(kind: str, *, node_key: str = "", profile_name: str = "", status: str, message: str, error: DriverError | None = None) -> DriverOperation:
    now = _now_iso()
    return DriverOperation(
        operation_id=str(uuid4()),
        kind=kind,
        status=status,
        node_key=node_key,
        profile_name=profile_name,
        started_at=now,
        updated_at=now,
        finished_at=now,
        progress_message=message,
        error=error,
    )


def _node_health(server: RegisteredServer) -> DriverNodeHealth:
    if not server.enabled:
        return DriverNodeHealth(connectivity="disabled", summary="server disabled in registry")
    if server.bootstrap_state == "bootstrapped":
        return DriverNodeHealth(connectivity="ready", summary="bootstrapped")
    return DriverNodeHealth(connectivity="degraded", summary=server.bootstrap_state or "unknown")


def _node_from_server(server: RegisteredServer) -> DriverNode:
    return DriverNode(
        node_key=server.key,
        transport=server.transport,
        version="",
        state=server.bootstrap_state,
        title=server.title,
        region=server.region,
        public_host=server.public_host,
        capabilities=DriverNodeCapabilities(
            supports_awg="awg" in server.protocol_kinds,
            supports_xray="xray" in server.protocol_kinds,
            supports_telemetry=True,
            supports_bootstrap=True,
        ),
        health=_node_health(server),
        metadata=asdict(server),
    )


def _parse_awg_profile_names(config_text: str) -> set[str]:
    names: set[str] = set()
    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("#"):
            continue
        name = line.lstrip("#").strip()
        if name:
            names.add(name)
    return names


class InProcessNodeDriverClient(NodeDriverClient):
    def get_node(self, node_key: str) -> Optional[DriverNode]:
        from services.server_registry import get_server

        server = get_server(node_key)
        if not server:
            return None
        return _node_from_server(server)

    def list_nodes(self, include_disabled: bool = False) -> list[DriverNode]:
        from services.server_registry import list_servers

        return [_node_from_server(server) for server in list_servers(include_disabled=include_disabled)]

    def reconcile_node(self, node_key: str) -> DriverOperation:
        from services.provisioning_state import reconcile_server_state

        code, out = reconcile_server_state(node_key)
        if code != 0:
            return _operation(
                "reconcile_node",
                node_key=node_key,
                status="FAILED",
                message=out,
                error=DriverError(code="reconcile_failed", summary=f"Node reconcile failed for {node_key}", detail=out),
            )
        return _operation("reconcile_node", node_key=node_key, status="SUCCEEDED", message=out)

    def reconcile_profile(self, profile_name: str) -> DriverOperation:
        from services.provisioning_state import reconcile_profile_state

        code, out = reconcile_profile_state(profile_name)
        if code != 0:
            return _operation(
                "reconcile_profile",
                profile_name=profile_name,
                status="FAILED",
                message=out,
                error=DriverError(code="reconcile_failed", summary=f"Profile reconcile failed for {profile_name}", detail=out),
            )
        return _operation("reconcile_profile", profile_name=profile_name, status="SUCCEEDED", message=out)

    def get_profile_usage(self, profile_name: str, protocol_kind: str = "awg") -> DriverProfileUsage:
        from services.traffic_usage import get_profile_monthly_usage

        usage = get_profile_monthly_usage(profile_name, protocol_kind)
        return DriverProfileUsage(
            profile_name=profile_name,
            protocol_kind=protocol_kind,
            rx_bytes=int(usage["rx_bytes"]),
            tx_bytes=int(usage["tx_bytes"]),
            total_bytes=int(usage["total_bytes"]),
            samples=int(usage["samples"]),
            peers=int(usage["peers"]),
        )

    def list_remote_profiles(self, node_key: str, protocol_kind: Optional[str] = None) -> list[DriverRemoteProfileRecord]:
        from services.server_registry import get_server
        from services.server_runtime import run_server_command
        from services.xray import list_user_records

        server = get_server(node_key)
        if not server:
            raise KeyError(node_key)

        kinds = [protocol_kind] if protocol_kind else list(server.protocol_kinds)
        items: list[DriverRemoteProfileRecord] = []

        for kind in kinds:
            if kind == "xray":
                code, records, out = list_user_records(node_key)
                if code != 0:
                    raise RuntimeError(out)
                items.extend(
                    DriverRemoteProfileRecord(
                        profile_name=str(record.get("name") or ""),
                        protocol_kind="xray",
                        remote_id=str(record.get("uuid") or ""),
                        status="observed",
                        node_key=node_key,
                    )
                    for record in records
                    if record.get("name")
                )
            elif kind == "awg":
                code, out = run_server_command(server, f"cat {server.awg_config_path}", timeout=60)
                if code != 0:
                    raise RuntimeError(out)
                items.extend(
                    DriverRemoteProfileRecord(
                        profile_name=name,
                        protocol_kind="awg",
                        remote_id=name,
                        status="observed",
                        node_key=node_key,
                    )
                    for name in sorted(_parse_awg_profile_names(out))
                )

        return items
