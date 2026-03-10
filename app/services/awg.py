# app/services/awg.py
from __future__ import annotations

import logging
import re
import shlex
from typing import Dict, List, Tuple

from services.server_registry import get_server
from services.server_runtime import run_server_command
from utils.security import validate_profile_name, validate_safe_token, validate_server_key

log = logging.getLogger("awg")


def create_awg_user(server_key: str, name: str) -> Tuple[int, str, str]:
    try:
        server_key = validate_server_key(server_key)
        name = validate_profile_name(name)
    except ValueError as exc:
        return 1, "", str(exc)
    server = get_server(server_key)
    if not server:
        return 1, "", f"Unknown server: {server_key}"
    code, out = run_server_command(server, f"/opt/node-plane-runtime/awg-add-user.sh {shlex.quote(name)}", timeout=120)
    if code != 0 and _extract_wg_conf(out or ""):
        log.warning("AWG create server=%s name=%s recovered from rc=%s because config was extracted", server_key, name, code)
        return 0, out, out
    log.info("AWG create server=%s name=%s rc=%s", server_key, name, code)
    return code, out, out


def delete_awg_user(server_key: str, name: str) -> Tuple[int, str]:
    try:
        server_key = validate_server_key(server_key)
        name = validate_profile_name(name)
    except ValueError as exc:
        return 1, str(exc)
    server = get_server(server_key)
    if not server:
        return 1, f"Unknown server: {server_key}"
    code, out = run_server_command(server, f"/opt/node-plane-runtime/awg-del-user.sh {shlex.quote(name)}", timeout=120)
    log.info("AWG delete server=%s name=%s rc=%s", server_key, name, code)
    return code, out


_WG_CONF_RE = re.compile(r"(\[Interface\][\s\S]*?\n\[Peer\][\s\S]*?)(?:\n=+|\Z)")


def _parse_wg_sections(text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current: str | None = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().lower()
            sections.setdefault(current, {})
            continue
        if current and "=" in line:
            key, value = line.split("=", 1)
            sections[current][key.strip().lower()] = value.strip()
    return sections


def _extract_wg_conf(text: str) -> str | None:
    if not text:
        return None
    m = _WG_CONF_RE.search(text)
    if not m:
        return None
    conf = m.group(1).strip()
    return conf.replace("\r\n", "\n").replace("\r", "\n")


def extract_client_public_key(wg_conf: str) -> str | None:
    sections = _parse_wg_sections(wg_conf)
    value = sections.get("interface", {}).get("publickey")
    return value or None


def list_awg_peer_transfers(server_key: str) -> Tuple[int, List[Dict[str, int | str]], str]:
    server = get_server(server_key)
    if not server:
        return 1, [], f"Unknown server: {server_key}"
    iface = validate_safe_token(server.awg_iface, field_name="awg_iface")
    cmd = (
        "source /etc/node-plane/node.env && "
        'CONTAINER="${AWG_CONTAINER_NAME:-amnezia-awg}" && '
        f'IFACE={shlex.quote(iface)} && '
        'if docker info >/dev/null 2>&1; then DOCKER="docker"; '
        'elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then DOCKER="sudo docker"; '
        'else echo "Docker is not available for this user." >&2; exit 1; fi && '
        '$DOCKER exec -i "$CONTAINER" sh -lc "wg show $IFACE transfer"'
    )
    code, out = run_server_command(server, cmd, timeout=60)
    if code != 0:
        return code, [], out

    records: List[Dict[str, int | str]] = []
    for raw_line in (out or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        peer_key = parts[0].strip()
        try:
            rx_bytes = int(parts[1])
            tx_bytes = int(parts[2])
        except ValueError:
            continue
        records.append({"peer_key": peer_key, "rx_bytes_total": rx_bytes, "tx_bytes_total": tx_bytes})
    return 0, records, out
