from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

from services.server_registry import RegisteredServer, get_server as get_registered_server, list_servers


@dataclass(frozen=True)
class ServerNode:
    key: str
    region: str
    title: str
    flag: str
    transport: str
    protocol_kinds: tuple[str, ...]
    public_host: str
    bootstrap_state: str


@dataclass(frozen=True)
class AccessMethod:
    code: str
    protocol_kind: str
    server_key: str
    label: str
    short_label: str
    getkey_payload: str
    supports_transports: bool = False

    @property
    def server(self) -> ServerNode:
        return get_server(self.server_key)


def _as_node(server: RegisteredServer) -> ServerNode:
    return ServerNode(
        key=server.key,
        region=server.region,
        title=server.title,
        flag=server.flag,
        transport=server.transport,
        protocol_kinds=server.protocol_kinds,
        public_host=server.public_host,
        bootstrap_state=server.bootstrap_state,
    )


def _xray_code(server_key: str) -> str:
    return "gx" if server_key == "de" else f"xray_{server_key}"


def _awg_code(server_key: str) -> str:
    if server_key == "de":
        return "ga"
    if server_key == "lv":
        return "la"
    return f"awg_{server_key}"


def _server_methods(server: RegisteredServer) -> list[AccessMethod]:
    methods: list[AccessMethod] = []
    if "xray" in server.protocol_kinds:
        methods.append(
            AccessMethod(
                code=_xray_code(server.key),
                protocol_kind="xray",
                server_key=server.key,
                label=f"{server.flag} Xray ({server.title})",
                short_label=f"{server.flag} Xray",
                getkey_payload=f"xray_{server.key}",
                supports_transports=True,
            )
        )
    if "awg" in server.protocol_kinds:
        methods.append(
            AccessMethod(
                code=_awg_code(server.key),
                protocol_kind="awg",
                server_key=server.key,
                label=f"{server.flag} AWG ({server.title})",
                short_label=f"{server.flag} AWG",
                getkey_payload=f"awg_{server.key}",
            )
        )
    return methods


def _all_servers() -> list[RegisteredServer]:
    return list_servers(include_disabled=False)


def _all_methods() -> list[AccessMethod]:
    methods: list[AccessMethod] = []
    for server in _all_servers():
        methods.extend(_server_methods(server))
    return methods


def get_server(server_key: str) -> ServerNode:
    server = get_registered_server(server_key)
    if not server or not server.enabled:
        raise KeyError(server_key)
    return _as_node(server)


def get_access_method(code: str) -> Optional[AccessMethod]:
    for method in _all_methods():
        if method.code == code:
            return method
    return None


def get_access_methods() -> List[AccessMethod]:
    return _all_methods()


def get_access_methods_for_codes(codes: Iterable[str]) -> List[AccessMethod]:
    items: List[AccessMethod] = []
    for code in codes:
        method = get_access_method(str(code))
        if method:
            items.append(method)
    return items


def get_access_methods_for_kind(kind: str) -> List[AccessMethod]:
    return [method for method in _all_methods() if method.protocol_kind == kind]


def get_access_method_by_getkey_payload(payload: str) -> Optional[AccessMethod]:
    for method in _all_methods():
        if method.getkey_payload == payload:
            return method
    return None


def get_awg_access_method_by_server_key(server_key: str) -> Optional[AccessMethod]:
    for method in get_access_methods_for_kind("awg"):
        if method.server_key == server_key:
            return method
    return None


def get_protocol_label(code: str, short: bool = False) -> str:
    method = get_access_method(code)
    if not method:
        return code
    return method.short_label if short else method.label


def get_awg_access_codes() -> tuple[str, ...]:
    return tuple(method.code for method in get_access_methods_for_kind("awg"))


def get_tracked_awg_server_keys() -> tuple[str, ...]:
    return tuple(sorted(method.server_key for method in get_access_methods_for_kind("awg")))
