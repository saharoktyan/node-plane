from __future__ import annotations

import os
import sys
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tests.postgres_test_harness import configure_postgres_test_env

APP_ROOT = str(Path(__file__).resolve().parents[1] / "app")
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
configure_postgres_test_env(tempfile.mkdtemp(prefix="node-plane-driver-commands-"))

from db import ensure_schema, get_db
from services.driver_commands import execute_server_command
from services.node_driver_client import DriverOperation
from services.node_driver_grpc import GrpcNodeDriverClient


class FakeGrpcDriver(GrpcNodeDriverClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self.operations: dict[str, DriverOperation] = {}
        self.lose_first_reply = False

    def bootstrap_node(self, node_key: str, preserve_config: bool = False, *, command_id: str | None = None) -> DriverOperation:
        assert command_id
        self.calls.append((command_id, node_key, preserve_config))
        operation = self.operations.setdefault(command_id, DriverOperation(
            operation_id="op-" + command_id,
            kind="bootstrap_node",
            status="SUCCEEDED",
            node_key=node_key,
            progress_message="done",
        ))
        if self.lose_first_reply and len(self.calls) == 1:
            return DriverOperation(
                operation_id="", kind="bootstrap_node", status="FAILED", node_key=node_key,
                progress_message="reply lost",
            )
        return operation

    def get_operation(self, operation_id: str) -> DriverOperation | None:
        return next((operation for operation in self.operations.values() if operation.operation_id == operation_id), None)


class DriverCommandJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.previous_dsn = os.environ.get("POSTGRES_DSN")
        configure_postgres_test_env(self.tempdir.name)

    def tearDown(self) -> None:
        if self.previous_dsn is None:
            os.environ.pop("POSTGRES_DSN", None)
        else:
            os.environ["POSTGRES_DSN"] = self.previous_dsn
        self.tempdir.cleanup()

    def row(self, source_ref: str):
        with get_db().connect() as conn:
            return conn.execute(
                "SELECT * FROM driver_commands WHERE source_ref = ?", (source_ref,)
            ).fetchone()

    def test_lost_reply_reuses_persisted_key_after_service_restart(self) -> None:
        first_driver = FakeGrpcDriver()
        first_driver.lose_first_reply = True
        first = execute_server_command(first_driver, "telegram:101", "bootstrap_node", "node", preserve_config=True)
        self.assertEqual(first.operation_id, "")
        saved = self.row("telegram:101")
        self.assertEqual(saved["operation_id"], None)
        self.assertEqual(first_driver.calls[0][0], saved["command_id"])

        second_driver = FakeGrpcDriver()
        second_driver.operations = first_driver.operations
        second = execute_server_command(second_driver, "telegram:101", "bootstrap_node", "node", preserve_config=True)
        self.assertEqual(second.operation_id, "op-" + saved["command_id"])
        self.assertEqual(second_driver.calls[0][0], saved["command_id"])
        self.assertEqual(self.row("telegram:101")["operation_id"], second.operation_id)

        cached = execute_server_command(second_driver, "telegram:101", "bootstrap_node", "node", preserve_config=True)
        self.assertEqual(cached.operation_id, second.operation_id)
        self.assertEqual(len(second_driver.calls), 1)

    def test_changed_parameters_are_rejected_before_driver_dispatch(self) -> None:
        driver = FakeGrpcDriver()
        execute_server_command(driver, "telegram:102", "bootstrap_node", "node", preserve_config=True)
        with self.assertRaisesRegex(ValueError, "different parameters"):
            execute_server_command(driver, "telegram:102", "bootstrap_node", "node", preserve_config=False)
        with self.assertRaisesRegex(ValueError, "different parameters"):
            execute_server_command(driver, "telegram:102", "bootstrap_node", "other", preserve_config=True)
        self.assertEqual(len(driver.calls), 1)

    def test_known_operation_missing_from_driver_blocks_reexecution(self) -> None:
        driver = FakeGrpcDriver()
        execute_server_command(driver, "telegram:103", "bootstrap_node", "node")
        driver.operations.clear()
        with self.assertRaisesRegex(RuntimeError, "operation is missing"):
            execute_server_command(driver, "telegram:103", "bootstrap_node", "node")
        self.assertEqual(len(driver.calls), 1)

    def test_awg_entropy_regeneration_is_not_repeated_for_same_update(self) -> None:
        class EntropyDriver(FakeGrpcDriver):
            def regenerate_awg_entropy(self, node_key: str, *, command_id: str | None = None) -> DriverOperation:
                assert command_id
                self.calls.append((command_id, node_key, False))
                operation = DriverOperation(
                    operation_id="op-" + command_id, kind="regenerate_awg_entropy",
                    status="SUCCEEDED", node_key=node_key,
                )
                self.operations[command_id] = operation
                return operation

        driver = EntropyDriver()
        first = execute_server_command(driver, "telegram:105", "regenerate_awg_entropy", "node")
        second = execute_server_command(driver, "telegram:105", "regenerate_awg_entropy", "node")
        self.assertEqual(first.operation_id, second.operation_id)
        self.assertEqual(len(driver.calls), 1)

    def test_concurrent_redelivery_uses_one_persisted_key(self) -> None:
        with get_db().transaction() as conn:
            ensure_schema(conn)
        driver = FakeGrpcDriver()
        barrier = threading.Barrier(2)

        def submit() -> DriverOperation:
            barrier.wait()
            return execute_server_command(driver, "telegram:104", "bootstrap_node", "node")

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(first.operation_id, second.operation_id)
        self.assertEqual({call[0] for call in driver.calls}, {self.row("telegram:104")["command_id"]})

    def test_inprocess_driver_keeps_existing_call_shape(self) -> None:
        class LocalDriver:
            def bootstrap_node(self, node_key: str, preserve_config: bool = False) -> DriverOperation:
                return DriverOperation(operation_id="local", kind="bootstrap_node", status="SUCCEEDED", node_key=node_key)

        result = execute_server_command(LocalDriver(), "", "bootstrap_node", "node")
        self.assertEqual(result.operation_id, "local")


if __name__ == "__main__":
    unittest.main()
