from __future__ import annotations

import json
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
    DriverRuntimeStatus,
    NodeDriverClient,
)

if TYPE_CHECKING:
    from services.server_registry import RegisteredServer


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _operation(
    kind: str,
    *,
    node_key: str = "",
    profile_name: str = "",
    status: str,
    message: str,
    result_json: str = "",
    error: DriverError | None = None,
) -> DriverOperation:
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
        result_json=result_json,
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
        flag=server.flag,
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

    def get_runtime_status(self, node_key: str) -> DriverRuntimeStatus:
        from services.server_bootstrap import get_server_runtime_state

        status = get_server_runtime_state(node_key)
        return DriverRuntimeStatus(
            state=str(status.get("state") or ""),
            version=str(status.get("version") or ""),
            commit=str(status.get("commit") or ""),
            expected_version=str(status.get("expected_version") or ""),
            expected_commit=str(status.get("expected_commit") or ""),
            message=str(status.get("message") or ""),
        )

    def list_nodes_needing_runtime_sync(self) -> list[DriverNode]:
        from services.server_bootstrap import get_servers_needing_runtime_sync

        return [_node_from_server(server) for server in get_servers_needing_runtime_sync()]

    def sync_node_env(self, node_key: str) -> DriverOperation:
        from services.server_bootstrap import sync_server_node_env

        code, out = sync_server_node_env(node_key)
        if code != 0:
            return _operation(
                "sync_node_env",
                node_key=node_key,
                status="FAILED",
                message=out,
                error=DriverError(code="sync_node_env_failed", summary=f"Node env sync failed for {node_key}", detail=out),
            )
        return _operation("sync_node_env", node_key=node_key, status="SUCCEEDED", message=out)

    def sync_runtime(self, node_key: str) -> DriverOperation:
        from services.server_bootstrap import sync_server_runtime

        code, out = sync_server_runtime(node_key)
        if code != 0:
            return _operation(
                "sync_runtime",
                node_key=node_key,
                status="FAILED",
                message=out,
                error=DriverError(code="sync_runtime_failed", summary=f"Runtime sync failed for {node_key}", detail=out),
            )
        return _operation("sync_runtime", node_key=node_key, status="SUCCEEDED", message=out)

    def sync_xray(self, node_key: str) -> DriverOperation:
        from services.server_bootstrap import sync_xray_server_settings

        code, out = sync_xray_server_settings(node_key)
        if code != 0:
            return _operation(
                "sync_xray",
                node_key=node_key,
                status="FAILED",
                message=out,
                error=DriverError(code="sync_xray_failed", summary=f"Xray sync failed for {node_key}", detail=out),
            )
        return _operation("sync_xray", node_key=node_key, status="SUCCEEDED", message=out)

    def probe_node(self, node_key: str) -> DriverOperation:
        from services.server_bootstrap import probe_server

        code, out = probe_server(node_key)
        if code != 0:
            return _operation(
                "probe_node",
                node_key=node_key,
                status="FAILED",
                message=out,
                error=DriverError(code="probe_node_failed", summary=f"Probe failed for {node_key}", detail=out),
            )
        return _operation("probe_node", node_key=node_key, status="SUCCEEDED", message=out)

    def check_ports(self, node_key: str) -> DriverOperation:
        from services.server_bootstrap import check_server_ports

        code, out = check_server_ports(node_key)
        if code != 0:
            return _operation(
                "check_ports",
                node_key=node_key,
                status="FAILED",
                message=out,
                error=DriverError(code="check_ports_failed", summary=f"Port check failed for {node_key}", detail=out),
            )
        return _operation("check_ports", node_key=node_key, status="SUCCEEDED", message=out)

    def open_ports(self, node_key: str) -> DriverOperation:
        from services.server_bootstrap import open_server_ports

        code, out = open_server_ports(node_key)
        if code != 0:
            return _operation(
                "open_ports",
                node_key=node_key,
                status="FAILED",
                message=out,
                error=DriverError(code="open_ports_failed", summary=f"Open ports failed for {node_key}", detail=out),
            )
        return _operation("open_ports", node_key=node_key, status="SUCCEEDED", message=out)

    def install_docker(self, node_key: str) -> DriverOperation:
        from services.server_bootstrap import install_server_docker

        code, out = install_server_docker(node_key)
        if code != 0:
            return _operation(
                "install_docker",
                node_key=node_key,
                status="FAILED",
                message=out,
                error=DriverError(code="install_docker_failed", summary=f"Docker install failed for {node_key}", detail=out),
            )
        return _operation("install_docker", node_key=node_key, status="SUCCEEDED", message=out)

    def bootstrap_node(self, node_key: str, preserve_config: bool = False) -> DriverOperation:
        from services.server_bootstrap import bootstrap_server

        code, out = bootstrap_server(node_key, preserve_config=preserve_config)
        if code != 0:
            return _operation(
                "bootstrap_node",
                node_key=node_key,
                status="FAILED",
                message=out,
                error=DriverError(code="bootstrap_node_failed", summary=f"Bootstrap failed for {node_key}", detail=out),
            )
        return _operation("bootstrap_node", node_key=node_key, status="SUCCEEDED", message=out)

    def reinstall_node(self, node_key: str, preserve_config: bool = False) -> DriverOperation:
        from services.server_bootstrap import reinstall_server

        code, out = reinstall_server(node_key, preserve_config=preserve_config)
        if code != 0:
            return _operation(
                "reinstall_node",
                node_key=node_key,
                status="FAILED",
                message=out,
                error=DriverError(code="reinstall_node_failed", summary=f"Reinstall failed for {node_key}", detail=out),
            )
        return _operation("reinstall_node", node_key=node_key, status="SUCCEEDED", message=out)

    def delete_runtime(self, node_key: str, preserve_config: bool = False) -> DriverOperation:
        from services.server_bootstrap import delete_server_runtime

        code, out = delete_server_runtime(node_key, preserve_config=preserve_config)
        if code != 0:
            return _operation(
                "delete_runtime",
                node_key=node_key,
                status="FAILED",
                message=out,
                error=DriverError(code="delete_runtime_failed", summary=f"Delete runtime failed for {node_key}", detail=out),
            )
        return _operation("delete_runtime", node_key=node_key, status="SUCCEEDED", message=out)

    def full_cleanup_node(self, node_key: str, remove_ssh_key: bool = False) -> DriverOperation:
        from services.server_bootstrap import full_cleanup_server

        code, out = full_cleanup_server(node_key, remove_ssh_key=remove_ssh_key)
        if code != 0:
            return _operation(
                "full_cleanup_node",
                node_key=node_key,
                status="FAILED",
                message=out,
                error=DriverError(code="full_cleanup_node_failed", summary=f"Full cleanup failed for {node_key}", detail=out),
            )
        return _operation("full_cleanup_node", node_key=node_key, status="SUCCEEDED", message=out)

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
        from services import awg as awg_svc
        from services import xray as xray_svc
        from services.provisioning_state import upsert_profile_server_state

        lines: list[str] = []
        failed = False
        awg_result_json = ""
        if "xray" in protocol_kinds:
            code, out, ensured_uuid, ensured_short_id = xray_svc.ensure_user(
                profile_name,
                node_key,
                uuid_value=xray_uuid or None,
                short_id_value=xray_short_id or None,
            )
            if code == 0:
                upsert_profile_server_state(profile_name, node_key, "xray", status="provisioned", remote_id=ensured_uuid or xray_uuid, last_error=None)
            else:
                failed = True
                upsert_profile_server_state(profile_name, node_key, "xray", status="failed", remote_id=xray_uuid or None, last_error=out)
            lines.append(f"xray: {out}")
            if ensured_short_id:
                lines.append(f"xray_short_id: {ensured_short_id}")
        if "awg" in protocol_kinds:
            code, conf, out = awg_svc.create_awg_user(node_key, awg_peer_name or profile_name)
            lines.append(f"awg: {out}")
            if code == 0:
                upsert_profile_server_state(profile_name, node_key, "awg", status="provisioned", last_error=None)
                payload = json.dumps(
                    {
                        "vpn_uri": str(conf or "").strip(),
                        "wg_conf": str(out[out.find("[Interface]") :].strip()) if "[Interface]" in out else "",
                    },
                    ensure_ascii=True,
                )
                awg_result_json = payload
            else:
                failed = True
                upsert_profile_server_state(profile_name, node_key, "awg", status="failed", last_error=out)
                awg_result_json = ""
        if not lines:
            failed = True
            lines.append("no supported protocol kinds requested")
            awg_result_json = ""
        return _operation(
            "ensure_profile_on_node",
            node_key=node_key,
            profile_name=profile_name,
            status="FAILED" if failed else "SUCCEEDED",
            message="\n".join(lines),
            result_json=awg_result_json if not failed else "",
        )

    def delete_profile_from_node(self, node_key: str, profile_name: str, protocol_kinds: list[str]) -> DriverOperation:
        from services import awg as awg_svc
        from services import xray as xray_svc
        from services.provisioning_state import delete_profile_server_state

        lines: list[str] = []
        failed = False
        if "xray" in protocol_kinds:
            code, out = xray_svc.delete_user(profile_name, node_key)
            if code == 0:
                delete_profile_server_state(profile_name, node_key, "xray")
            else:
                failed = True
            lines.append(f"xray: {out}")
        if "awg" in protocol_kinds:
            code, out = awg_svc.delete_awg_user(node_key, profile_name)
            if code == 0:
                delete_profile_server_state(profile_name, node_key, "awg")
            else:
                failed = True
            lines.append(f"awg: {out}")
        if not lines:
            failed = True
            lines.append("no supported protocol kinds requested")
        return _operation(
            "delete_profile_from_node",
            node_key=node_key,
            profile_name=profile_name,
            status="FAILED" if failed else "SUCCEEDED",
            message="\n".join(lines),
        )

    def reconcile_node(self, node_key: str) -> DriverOperation:
        from services.reconcile_state import reconcile_server_state

        code, out = reconcile_server_state(node_key)
        if code != 0:
            return _operation("reconcile_node", node_key=node_key, status="FAILED", message=out)
        return _operation("reconcile_node", node_key=node_key, status="SUCCEEDED", message=out)

    def reconcile_profile(self, profile_name: str) -> DriverOperation:
        from services.reconcile_state import reconcile_profile_state

        code, out = reconcile_profile_state(profile_name)
        if code != 0:
            return _operation("reconcile_profile", profile_name=profile_name, status="FAILED", message=out)
        return _operation("reconcile_profile", profile_name=profile_name, status="SUCCEEDED", message=out)

    def get_operation(self, operation_id: str) -> Optional[DriverOperation]:
        return None

    def list_operations(
        self,
        *,
        node_key: str = "",
        profile_name: str = "",
        status: str = "",
        limit: int = 20,
    ) -> list[DriverOperation]:
        return []

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
        from services.node_driver_remote import list_remote_awg_profiles, list_remote_xray_profiles
        from services.server_registry import get_server

        server = get_server(node_key)
        if not server:
            raise KeyError(node_key)

        kinds = [protocol_kind] if protocol_kind else list(server.protocol_kinds)
        items: list[DriverRemoteProfileRecord] = []

        for kind in kinds:
            if kind == "xray":
                code, records, out = list_remote_xray_profiles(node_key)
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
                code, names, out = list_remote_awg_profiles(node_key)
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
                    for name in sorted(names)
                )

        return items
