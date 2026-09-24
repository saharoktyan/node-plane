"""AWG client config parsing used by the Telegram adapter."""

from __future__ import annotations

import re


_WG_CONF_RE = re.compile(r"(\[Interface\][\s\S]*?\n\[Peer\][\s\S]*?)(?:\n=+|\Z)")


def _parse_wg_sections(text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current: str | None = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().lower()
            sections.setdefault(current, {})
        elif current and "=" in line:
            key, value = line.split("=", 1)
            sections[current][key.strip().lower()] = value.strip()
    return sections


def _extract_wg_conf(text: str) -> str | None:
    if not text:
        return None
    match = _WG_CONF_RE.search(text)
    if not match:
        return None
    return match.group(1).strip().replace("\r\n", "\n").replace("\r", "\n")


def extract_client_public_key(wg_conf: str) -> str | None:
    return _parse_wg_sections(wg_conf).get("interface", {}).get("publickey") or None
