from __future__ import annotations

from services.node_driver_client import NodeDriverClient
from services.node_driver_inprocess import InProcessNodeDriverClient

_driver: NodeDriverClient | None = None


def get_node_driver() -> NodeDriverClient:
    global _driver
    if _driver is None:
        _driver = InProcessNodeDriverClient()
    return _driver


def set_node_driver(driver: NodeDriverClient) -> None:
    global _driver
    _driver = driver
