from __future__ import annotations

from collections import defaultdict
from typing import List, Sequence

from domain.servers import AccessMethod, get_access_methods_for_codes, get_server
from i18n import t


def format_server_access(name: str, allowed_codes: List[str], awg_server_keys: Sequence[str], lang: str = "ru") -> str:
    grouped = defaultdict(list)
    for method in get_access_methods_for_codes(allowed_codes):
        grouped[method.server_key].append(method)

    if not grouped:
        return t(lang, "common.none")

    lines = []
    for server_key, methods in grouped.items():
        server = get_server(server_key)
        method_labels = [method.short_label.split(" ", 1)[1] for method in methods]
        lines.append(f"• {server.flag} *{server.title}*: " + ", ".join(method_labels))
    return "\n".join(lines)


def render_getkey_overview(methods: List[AccessMethod], lang: str = "ru") -> tuple[str, List[tuple[str, str]]]:
    grouped = defaultdict(list)
    for method in methods:
        grouped[method.server_key].append(method)

    server_items: List[tuple[str, str]] = []
    for server_key, server_methods in grouped.items():
        server = get_server(server_key)
        server_items.append((server_key, f"{server.flag} {server.title} · {t(lang, 'ui.getkey.methods_count', count=len(server_methods))}"))

    return f"{t(lang, 'getkey.title')}\n\n{t(lang, 'ui.getkey.choose_server')}", server_items


def render_server_menu(server_key: str, methods: List[AccessMethod], lang: str = "ru") -> tuple[str, List[tuple[str, str]]]:
    server = get_server(server_key)
    items = [(method.getkey_payload, method.short_label.split(" ", 1)[1]) for method in methods]
    text = f"{server.flag} {server.title}\n\n{t(lang, 'ui.server.choose_method')}"
    return text, items
