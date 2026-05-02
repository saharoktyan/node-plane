from __future__ import annotations

from importlib import import_module
from typing import Optional

from config import NODE_DRIVER_GRPC_TARGET, NODE_DRIVER_GRPC_TIMEOUT_SECONDS
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


class GrpcNodeDriverClient(NodeDriverClient):
    def __init__(self, target: str | None = None, timeout_seconds: int | None = None) -> None:
        self.target = (target or NODE_DRIVER_GRPC_TARGET).strip() or NODE_DRIVER_GRPC_TARGET
        self.timeout_seconds = int(timeout_seconds or NODE_DRIVER_GRPC_TIMEOUT_SECONDS)
        self._grpc = None
        self._channel = None
        self._stubs_ready = False
        self._types_pb2 = None
        self._node_pb2 = None
        self._node_pb2_grpc = None
        self._provisioning_pb2 = None
        self._provisioning_pb2_grpc = None
        self._runtime_pb2 = None
        self._runtime_pb2_grpc = None
        self._telemetry_pb2 = None
        self._telemetry_pb2_grpc = None
        self._operation_pb2 = None
        self._operation_pb2_grpc = None
        self._node_stub = None
        self._provisioning_stub = None
        self._runtime_stub = None
        self._telemetry_stub = None
        self._operation_stub = None

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
            self._load_stubs()

    def _load_stubs(self) -> None:
        try:
            self._types_pb2 = import_module("driver.v1.types_pb2")
            self._node_pb2 = import_module("driver.v1.node_service_pb2")
            self._node_pb2_grpc = import_module("driver.v1.node_service_pb2_grpc")
            self._provisioning_pb2 = import_module("driver.v1.provisioning_service_pb2")
            self._provisioning_pb2_grpc = import_module("driver.v1.provisioning_service_pb2_grpc")
            self._runtime_pb2 = import_module("driver.v1.runtime_service_pb2")
            self._runtime_pb2_grpc = import_module("driver.v1.runtime_service_pb2_grpc")
            self._telemetry_pb2 = import_module("driver.v1.telemetry_service_pb2")
            self._telemetry_pb2_grpc = import_module("driver.v1.telemetry_service_pb2_grpc")
            self._operation_pb2 = import_module("driver.v1.operation_service_pb2")
            self._operation_pb2_grpc = import_module("driver.v1.operation_service_pb2_grpc")
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Generated protobuf modules are missing. "
                "Run scripts/gen_driver_proto.sh after installing requirements-dev.txt."
            ) from exc

        self._node_stub = self._node_pb2_grpc.NodeServiceStub(self._channel)
        self._provisioning_stub = self._provisioning_pb2_grpc.ProvisioningServiceStub(self._channel)
        self._runtime_stub = self._runtime_pb2_grpc.RuntimeServiceStub(self._channel)
        self._telemetry_stub = self._telemetry_pb2_grpc.TelemetryServiceStub(self._channel)
        self._operation_stub = self._operation_pb2_grpc.OperationServiceStub(self._channel)
        self._stubs_ready = True

    def _rpc_error_text(self, exc: Exception) -> str:
        code_fn = getattr(exc, "code", None)
        details_fn = getattr(exc, "details", None)
        code = str(code_fn()) if callable(code_fn) else exc.__class__.__name__
        details = str(details_fn()) if callable(details_fn) else str(exc)
        return f"{code}: {details}".strip()

    def _operation_from_pb(self, operation) -> DriverOperation:
        error = None
        if getattr(operation, "error", None) and getattr(operation.error, "code", ""):
            error = DriverError(
                code=str(operation.error.code),
                summary=str(operation.error.summary),
                detail=str(operation.error.detail),
                retryable=bool(operation.error.retryable),
            )
        return DriverOperation(
            operation_id=str(getattr(operation, "operation_id", "")),
            kind=str(getattr(operation, "kind", "")),
            status=str(getattr(operation, "status", "")),
            node_key=str(getattr(operation, "node_key", "")),
            profile_name=str(getattr(operation, "profile_name", "")),
            started_at=str(getattr(operation, "started_at", "")),
            updated_at=str(getattr(operation, "updated_at", "")),
            finished_at=str(getattr(operation, "finished_at", "")),
            progress_message=str(getattr(operation, "progress_message", "")),
            result_json=str(getattr(operation, "result_json", "")),
            error=error,
        )

    def _start_operation(self, kind: str, response, *, node_key: str = "", profile_name: str = "") -> DriverOperation:
        return DriverOperation(
            operation_id=str(getattr(response, "operation_id", "")),
            kind=kind,
            status="PENDING",
            node_key=node_key,
            profile_name=profile_name,
        )

    def _failed_operation(self, kind: str, exc: Exception, *, node_key: str = "", profile_name: str = "") -> DriverOperation:
        detail = self._rpc_error_text(exc)
        return DriverOperation(
            operation_id="",
            kind=kind,
            status="FAILED",
            node_key=node_key,
            profile_name=profile_name,
            progress_message=detail,
            error=DriverError(code="grpc_error", summary=f"{kind} RPC failed", detail=detail, retryable=True),
        )

    def _query_error(self, kind: str, exc: Exception) -> RuntimeError:
        return RuntimeError(f"{kind} RPC failed: {self._rpc_error_text(exc)}")

    def _node_from_pb(self, item) -> DriverNode:
        caps = getattr(item, "capabilities", None)
        health = getattr(item, "health", None)
        return DriverNode(
            node_key=str(getattr(item, "node_key", "")),
            transport=str(getattr(item, "transport", "")),
            version=str(getattr(item, "version", "")),
            state=str(getattr(item, "state", "")),
            title=str(getattr(item, "title", "")),
            flag=str(getattr(item, "flag", "")),
            region=str(getattr(item, "region", "")),
            public_host=str(getattr(item, "public_host", "")),
            capabilities=DriverNodeCapabilities(
                supports_awg=bool(getattr(caps, "supports_awg", False)),
                supports_xray=bool(getattr(caps, "supports_xray", False)),
                supports_telemetry=bool(getattr(caps, "supports_telemetry", False)),
                supports_bootstrap=bool(getattr(caps, "supports_bootstrap", False)),
            ),
            health=DriverNodeHealth(
                connectivity=str(getattr(health, "connectivity", "")),
                last_seen_at=str(getattr(health, "last_seen_at", "")),
                summary=str(getattr(health, "summary", "")),
            ),
            metadata={},
        )

    def get_node(self, node_key: str) -> Optional[DriverNode]:
        self._ensure_client()
        try:
            response = self._node_stub.GetNode(
                self._node_pb2.GetNodeRequest(node_key=node_key),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            raise self._query_error("get_node", exc) from exc
        if not getattr(response, "node_key", ""):
            return None
        return self._node_from_pb(response)

    def list_nodes(self, include_disabled: bool = False) -> list[DriverNode]:
        self._ensure_client()
        try:
            response = self._node_stub.ListNodes(
                self._node_pb2.ListNodesRequest(include_disabled=include_disabled),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            raise self._query_error("list_nodes", exc) from exc
        return [self._node_from_pb(item) for item in getattr(response, "items", [])]

    def get_runtime_status(self, node_key: str) -> DriverRuntimeStatus:
        self._ensure_client()
        try:
            response = self._runtime_stub.GetRuntimeStatus(
                self._runtime_pb2.GetRuntimeStatusRequest(node_key=node_key),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            raise self._query_error("get_runtime_status", exc) from exc
        runtime = getattr(response, "runtime", None)
        if runtime is None:
            return DriverRuntimeStatus(
                state="unknown",
                version="",
                commit="",
                expected_version="",
                expected_commit="",
                message="runtime service returned no runtime payload",
            )
        return DriverRuntimeStatus(
            state=str(getattr(runtime, "state", "")),
            version=str(getattr(runtime, "version", "")),
            commit=str(getattr(runtime, "commit", "")),
            expected_version=str(getattr(runtime, "expected_version", "")),
            expected_commit=str(getattr(runtime, "expected_commit", "")),
            message=str(getattr(runtime, "message", "")),
        )

    def list_nodes_needing_runtime_sync(self) -> list[DriverNode]:
        self._ensure_client()
        try:
            response = self._runtime_stub.ListNodesNeedingRuntimeSync(
                self._runtime_pb2.ListNodesNeedingRuntimeSyncRequest(),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            raise self._query_error("list_nodes_needing_runtime_sync", exc) from exc
        return [self._node_from_pb(item) for item in getattr(response, "items", [])]

    def sync_node_env(self, node_key: str) -> DriverOperation:
        self._ensure_client()
        try:
            response = self._node_stub.SyncNodeEnv(
                self._node_pb2.SyncNodeEnvRequest(node_key=node_key),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return self._failed_operation("sync_node_env", exc, node_key=node_key)
        operation = self._start_operation("sync_node_env", response, node_key=node_key)
        if operation.operation_id:
            fetched = self.get_operation(operation.operation_id)
            if fetched is not None:
                return fetched
        return operation

    def sync_runtime(self, node_key: str) -> DriverOperation:
        self._ensure_client()
        try:
            response = self._runtime_stub.SyncRuntime(
                self._runtime_pb2.SyncRuntimeRequest(node_key=node_key),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return self._failed_operation("sync_runtime", exc, node_key=node_key)
        operation = self._start_operation("sync_runtime", response, node_key=node_key)
        if operation.operation_id:
            fetched = self.get_operation(operation.operation_id)
            if fetched is not None:
                return fetched
        return operation

    def sync_xray(self, node_key: str) -> DriverOperation:
        self._ensure_client()
        try:
            response = self._runtime_stub.SyncXray(
                self._runtime_pb2.SyncXrayRequest(node_key=node_key),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return self._failed_operation("sync_xray", exc, node_key=node_key)
        operation = self._start_operation("sync_xray", response, node_key=node_key)
        if operation.operation_id:
            fetched = self.get_operation(operation.operation_id)
            if fetched is not None:
                return fetched
        return operation

    def probe_node(self, node_key: str) -> DriverOperation:
        self._ensure_client()
        try:
            response = self._node_stub.ProbeNode(
                self._node_pb2.ProbeNodeRequest(node_key=node_key),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return self._failed_operation("probe_node", exc, node_key=node_key)
        operation = self._start_operation("probe_node", response, node_key=node_key)
        if operation.operation_id:
            fetched = self.get_operation(operation.operation_id)
            if fetched is not None:
                return fetched
        return operation

    def check_ports(self, node_key: str) -> DriverOperation:
        self._ensure_client()
        try:
            response = self._node_stub.CheckPorts(
                self._node_pb2.CheckPortsRequest(node_key=node_key),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return self._failed_operation("check_ports", exc, node_key=node_key)
        operation = self._start_operation("check_ports", response, node_key=node_key)
        if operation.operation_id:
            fetched = self.get_operation(operation.operation_id)
            if fetched is not None:
                return fetched
        return operation

    def open_ports(self, node_key: str) -> DriverOperation:
        self._ensure_client()
        try:
            response = self._node_stub.OpenPorts(
                self._node_pb2.OpenPortsRequest(node_key=node_key),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return self._failed_operation("open_ports", exc, node_key=node_key)
        operation = self._start_operation("open_ports", response, node_key=node_key)
        if operation.operation_id:
            fetched = self.get_operation(operation.operation_id)
            if fetched is not None:
                return fetched
        return operation

    def install_docker(self, node_key: str) -> DriverOperation:
        self._ensure_client()
        try:
            response = self._node_stub.InstallDocker(
                self._node_pb2.InstallDockerRequest(node_key=node_key),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return self._failed_operation("install_docker", exc, node_key=node_key)
        operation = self._start_operation("install_docker", response, node_key=node_key)
        if operation.operation_id:
            fetched = self.get_operation(operation.operation_id)
            if fetched is not None:
                return fetched
        return operation

    def bootstrap_node(self, node_key: str, preserve_config: bool = False) -> DriverOperation:
        self._ensure_client()
        try:
            response = self._runtime_stub.BootstrapNode(
                self._runtime_pb2.BootstrapNodeRequest(node_key=node_key, preserve_config=preserve_config),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return self._failed_operation("bootstrap_node", exc, node_key=node_key)
        operation = self._start_operation("bootstrap_node", response, node_key=node_key)
        if operation.operation_id:
            fetched = self.get_operation(operation.operation_id)
            if fetched is not None:
                return fetched
        return operation

    def reinstall_node(self, node_key: str, preserve_config: bool = False) -> DriverOperation:
        self._ensure_client()
        try:
            response = self._runtime_stub.ReinstallNode(
                self._runtime_pb2.ReinstallNodeRequest(node_key=node_key, preserve_config=preserve_config),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return self._failed_operation("reinstall_node", exc, node_key=node_key)
        operation = self._start_operation("reinstall_node", response, node_key=node_key)
        if operation.operation_id:
            fetched = self.get_operation(operation.operation_id)
            if fetched is not None:
                return fetched
        return operation

    def delete_runtime(self, node_key: str, preserve_config: bool = False) -> DriverOperation:
        self._ensure_client()
        try:
            response = self._runtime_stub.DeleteRuntime(
                self._runtime_pb2.DeleteRuntimeRequest(node_key=node_key, preserve_config=preserve_config),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return self._failed_operation("delete_runtime", exc, node_key=node_key)
        operation = self._start_operation("delete_runtime", response, node_key=node_key)
        if operation.operation_id:
            fetched = self.get_operation(operation.operation_id)
            if fetched is not None:
                return fetched
        return operation

    def full_cleanup_node(self, node_key: str, remove_ssh_key: bool = False) -> DriverOperation:
        self._ensure_client()
        try:
            response = self._runtime_stub.FullCleanupNode(
                self._runtime_pb2.FullCleanupNodeRequest(node_key=node_key, remove_ssh_key=remove_ssh_key),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return self._failed_operation("full_cleanup_node", exc, node_key=node_key)
        operation = self._start_operation("full_cleanup_node", response, node_key=node_key)
        if operation.operation_id:
            fetched = self.get_operation(operation.operation_id)
            if fetched is not None:
                return fetched
        return operation

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
        self._ensure_client()
        profile = self._types_pb2.ProfileSpec(
            profile_name=profile_name,
            protocol_kinds=protocol_kinds,
            awg=self._types_pb2.AwgSpec(profile_name=profile_name, peer_name=awg_peer_name or profile_name),
            xray=self._types_pb2.XraySpec(profile_name=profile_name, uuid=xray_uuid, short_id=xray_short_id),
        )
        try:
            response = self._provisioning_stub.EnsureProfileOnNode(
                self._provisioning_pb2.EnsureProfileOnNodeRequest(node_key=node_key, profile=profile),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return self._failed_operation("ensure_profile_on_node", exc, node_key=node_key, profile_name=profile_name)
        operation = self._start_operation("ensure_profile_on_node", response, node_key=node_key, profile_name=profile_name)
        if operation.operation_id:
            fetched = self.get_operation(operation.operation_id)
            if fetched is not None:
                return fetched
        return operation

    def delete_profile_from_node(self, node_key: str, profile_name: str, protocol_kinds: list[str]) -> DriverOperation:
        self._ensure_client()
        try:
            response = self._provisioning_stub.DeleteProfileFromNode(
                self._provisioning_pb2.DeleteProfileFromNodeRequest(
                    node_key=node_key,
                    profile_name=profile_name,
                    protocol_kinds=protocol_kinds,
                ),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return self._failed_operation("delete_profile_from_node", exc, node_key=node_key, profile_name=profile_name)
        operation = self._start_operation("delete_profile_from_node", response, node_key=node_key, profile_name=profile_name)
        if operation.operation_id:
            fetched = self.get_operation(operation.operation_id)
            if fetched is not None:
                return fetched
        return operation

    def reconcile_node(self, node_key: str) -> DriverOperation:
        self._ensure_client()
        try:
            response = self._provisioning_stub.ReconcileNode(
                self._provisioning_pb2.ReconcileNodeRequest(node_key=node_key),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return self._failed_operation("reconcile_node", exc, node_key=node_key)
        operation = self._start_operation("reconcile_node", response, node_key=node_key)
        if operation.operation_id:
            fetched = self.get_operation(operation.operation_id)
            if fetched is not None:
                return fetched
        return operation

    def reconcile_profile(self, profile_name: str) -> DriverOperation:
        self._ensure_client()
        try:
            response = self._provisioning_stub.ReconcileProfile(
                self._provisioning_pb2.ReconcileProfileRequest(profile_name=profile_name),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return self._failed_operation("reconcile_profile", exc, profile_name=profile_name)
        operation = self._start_operation("reconcile_profile", response, profile_name=profile_name)
        if operation.operation_id:
            fetched = self.get_operation(operation.operation_id)
            if fetched is not None:
                return fetched
        return operation

    def get_operation(self, operation_id: str) -> Optional[DriverOperation]:
        self._ensure_client()
        try:
            response = self._operation_stub.GetOperation(
                self._operation_pb2.GetOperationRequest(operation_id=operation_id),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            code_fn = getattr(exc, "code", None)
            if callable(code_fn):
                try:
                    if str(code_fn()) == "StatusCode.NOT_FOUND":
                        return None
                except Exception:
                    pass
            raise self._query_error("get_operation", exc) from exc
        return self._operation_from_pb(response)

    def list_operations(
        self,
        *,
        node_key: str = "",
        profile_name: str = "",
        status: str = "",
        limit: int = 20,
    ) -> list[DriverOperation]:
        self._ensure_client()
        try:
            response = self._operation_stub.ListOperations(
                self._operation_pb2.ListOperationsRequest(
                    node_key=node_key,
                    profile_name=profile_name,
                    status=status,
                    limit=max(1, int(limit)),
                ),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            raise self._query_error("list_operations", exc) from exc
        return [self._operation_from_pb(item) for item in getattr(response, "items", [])]

    def get_profile_usage(self, profile_name: str, protocol_kind: str = "awg") -> DriverProfileUsage:
        self._ensure_client()
        try:
            response = self._telemetry_stub.GetProfileUsage(
                self._telemetry_pb2.GetProfileUsageRequest(
                    profile_name=profile_name,
                    protocol_kind=protocol_kind,
                    period="month",
                ),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            raise self._query_error("get_profile_usage", exc) from exc
        usage = getattr(response, "usage", None)
        if usage is None:
            raise RuntimeError("GetProfileUsage returned no usage payload")
        return DriverProfileUsage(
            profile_name=str(getattr(usage, "profile_name", profile_name)),
            protocol_kind=str(getattr(usage, "protocol_kind", protocol_kind)),
            rx_bytes=int(getattr(usage, "rx_bytes", 0)),
            tx_bytes=int(getattr(usage, "tx_bytes", 0)),
            total_bytes=int(getattr(usage, "total_bytes", 0)),
            samples=int(getattr(usage, "samples", 0)),
            peers=int(getattr(usage, "peers", 0)),
        )

    def list_remote_profiles(self, node_key: str, protocol_kind: Optional[str] = None) -> list[DriverRemoteProfileRecord]:
        self._ensure_client()
        try:
            response = self._provisioning_stub.ListRemoteProfiles(
                self._provisioning_pb2.ListRemoteProfilesRequest(
                    node_key=node_key,
                    protocol_kind=str(protocol_kind or ""),
                ),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            raise self._query_error("list_remote_profiles", exc) from exc
        return [
            DriverRemoteProfileRecord(
                profile_name=str(getattr(item, "profile_name", "")),
                protocol_kind=str(getattr(item, "protocol_kind", "")),
                remote_id=str(getattr(item, "remote_id", "")),
                status=str(getattr(item, "status", "")),
                node_key=node_key,
            )
            for item in getattr(response, "items", [])
        ]
