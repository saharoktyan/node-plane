"""Persist a caller's command identity before invoking the Rust driver."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from db import ensure_schema, get_db
from services.node_driver_client import DriverOperation, NodeDriverClient
from services.node_driver_grpc import GrpcNodeDriverClient


_SERVER_ACTIONS = frozenset({
    "bootstrap_node", "reinstall_node", "delete_runtime", "full_cleanup_node",
    "probe_node", "check_ports", "open_ports", "install_docker",
    "sync_node_env", "sync_runtime", "sync_xray", "reconcile_node",
})
_OPTION_FIELDS = {
    "bootstrap_node": "preserve_config",
    "reinstall_node": "preserve_config",
    "delete_runtime": "preserve_config",
    "full_cleanup_node": "remove_ssh_key",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _claim_command(source_ref: str, kind: str, request_json: str) -> tuple[str, str | None]:
    command_id = uuid4().hex
    timestamp = _now()
    db = get_db()
    with db.transaction() as conn:
        ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO driver_commands(
                source_ref, command_id, kind, request_json, operation_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT(source_ref) DO NOTHING
            """,
            (source_ref, command_id, kind, request_json, timestamp, timestamp),
        )
        row = conn.execute(
            "SELECT command_id, kind, request_json, operation_id FROM driver_commands WHERE source_ref = ?",
            (source_ref,),
        ).fetchone()
        if not row or row["kind"] != kind or row["request_json"] != request_json:
            raise ValueError("command source was already used with different parameters")
        return str(row["command_id"]), str(row["operation_id"]) if row["operation_id"] else None


def _record_operation(source_ref: str, command_id: str, operation_id: str) -> None:
    db = get_db()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT operation_id FROM driver_commands WHERE source_ref = ? AND command_id = ?",
            (source_ref, command_id),
        ).fetchone()
        if not row:
            raise RuntimeError("saved command identity disappeared before recording its result")
        if row["operation_id"] and str(row["operation_id"]) != operation_id:
            raise RuntimeError("driver returned a different operation ID for the same command")
        conn.execute(
            "UPDATE driver_commands SET operation_id = ?, updated_at = ? WHERE source_ref = ? AND command_id = ?",
            (operation_id, _now(), source_ref, command_id),
        )


def execute_server_command(
    driver: NodeDriverClient,
    source_ref: str,
    kind: str,
    node_key: str,
    **options: bool,
) -> DriverOperation:
    """Run one node action; a repeated source uses the same persisted command key.

    The in-process backend retains its existing behavior during migration.
    """
    if kind not in _SERVER_ACTIONS or not node_key:
        raise ValueError("unsupported driver action or missing node key")
    option_name = _OPTION_FIELDS.get(kind)
    if options and (option_name is None or set(options) != {option_name}):
        raise ValueError("unsupported driver action parameters")
    if option_name is not None:
        options = {option_name: bool(options.get(option_name, False))}
    method = getattr(driver, kind)
    if not isinstance(driver, GrpcNodeDriverClient):
        return method(node_key, **options)
    if not source_ref:
        raise ValueError("source_ref is required for a gRPC driver command")

    request_json = json.dumps(
        {"node_key": node_key, "options": options},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    command_id, known_operation_id = _claim_command(source_ref, kind, request_json)
    if known_operation_id:
        known = driver.get_operation(known_operation_id)
        if known is None:
            raise RuntimeError("saved driver operation is missing; inspect the node before retrying")
        return known

    operation = method(node_key, command_id=command_id, **options)
    if operation.operation_id:
        _record_operation(source_ref, command_id, operation.operation_id)
    return operation
