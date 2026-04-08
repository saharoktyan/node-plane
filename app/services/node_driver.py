from __future__ import annotations

from config import NODE_DRIVER_BACKEND, NODE_DRIVER_GRPC_TARGET, NODE_DRIVER_GRPC_TIMEOUT_SECONDS
from services.node_driver_client import NodeDriverClient
from services.node_driver_grpc import GrpcNodeDriverClient
from services.node_driver_inprocess import InProcessNodeDriverClient

_driver: NodeDriverClient | None = None


def get_node_driver() -> NodeDriverClient:
    global _driver
    if _driver is None:
        if NODE_DRIVER_BACKEND == "grpc":
            _driver = GrpcNodeDriverClient(
                target=NODE_DRIVER_GRPC_TARGET,
                timeout_seconds=NODE_DRIVER_GRPC_TIMEOUT_SECONDS,
            )
        else:
            _driver = InProcessNodeDriverClient()
    return _driver


def set_node_driver(driver: NodeDriverClient) -> None:
    global _driver
    _driver = driver
