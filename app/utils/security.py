from __future__ import annotations

import re
import shlex
from typing import Iterable, Sequence


PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SERVER_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$")
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/@:+-]{1,255}$")
XRAY_PATH_PREFIX_RE = re.compile(r"^/[A-Za-z0-9._/-]{0,127}$")
XRAY_SHORT_ID_RE = re.compile(r"^[a-f0-9]{1,32}$")
XRAY_PUBLIC_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.IGNORECASE)
VPN_URI_RE = re.compile(r"vpn://[A-Za-z0-9+/=_-]+")
VLESS_URI_RE = re.compile(r"vless://[^\s`]+")


def validate_profile_name(value: str) -> str:
    normalized = str(value or "").strip().lstrip("@")
    if not PROFILE_NAME_RE.fullmatch(normalized):
        raise ValueError("Profile name must match [A-Za-z0-9._-] and be at most 64 characters")
    return normalized


def validate_server_key(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not SERVER_KEY_RE.fullmatch(normalized):
        raise ValueError("Server key must match [a-z0-9_-] and start with a letter or digit")
    return normalized


def validate_host(value: str, *, allow_empty: bool = False) -> str:
    normalized = str(value or "").strip()
    if not normalized and allow_empty:
        return ""
    if not HOST_RE.fullmatch(normalized):
        raise ValueError("Host contains unsupported characters")
    return normalized


def validate_safe_token(value: str, *, field_name: str = "value", allow_empty: bool = False) -> str:
    normalized = str(value or "").strip()
    if not normalized and allow_empty:
        return ""
    if not SAFE_TOKEN_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} contains unsupported characters")
    return normalized


def validate_safe_path(value: str, *, field_name: str = "path") -> str:
    normalized = str(value or "").strip()
    if not SAFE_PATH_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be an absolute safe path without spaces")
    return normalized


def validate_port(value: object, *, field_name: str = "port") -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"{field_name} must be between 1 and 65535")
    return port


def validate_protocol_kinds(values: Sequence[str] | Iterable[str]) -> list[str]:
    allowed = []
    for raw in values:
        item = str(raw).strip().lower()
        if item not in {"xray", "awg"}:
            raise ValueError("Unsupported protocol kind")
        if item not in allowed:
            allowed.append(item)
    return allowed


def validate_server_field(field: str, value: object) -> object:
    if field == "protocol_kinds":
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("protocol_kinds must be a list")
        return validate_protocol_kinds(value)
    if field in {"enabled"}:
        return bool(value)
    if field in {"ssh_port", "xray_tcp_port", "xray_xhttp_port", "awg_port"}:
        return validate_port(value, field_name=field)
    if field in {"transport"}:
        normalized = str(value or "").strip().lower()
        if normalized not in {"local", "ssh"}:
            raise ValueError("transport must be local or ssh")
        return normalized
    if field in {"key"}:
        return validate_server_key(str(value))
    if field in {"public_host", "xray_host", "xray_sni", "awg_public_host"}:
        return validate_host(str(value), allow_empty=True)
    if field in {"ssh_host"}:
        host = str(value or "").strip()
        if not host:
            return ""
        if "@" in host:
            user, _, hostname = host.rpartition("@")
            validate_safe_token(user, field_name="ssh user")
            validate_host(hostname)
            return f"{user}@{hostname}"
        return validate_host(host)
    if field in {"ssh_user"}:
        return validate_safe_token(str(value), field_name=field, allow_empty=True)
    if field in {"xray_service_name", "awg_iface", "xray_fp", "xray_flow", "awg_i1_preset", "bootstrap_state"}:
        return validate_safe_token(str(value), field_name=field)
    if field in {"xray_config_path", "awg_config_path", "ssh_key_path"}:
        return validate_safe_path(str(value), field_name=field)
    if field in {"xray_short_id", "xray_sid"}:
        normalized = str(value or "").strip().lower()
        if normalized and not XRAY_SHORT_ID_RE.fullmatch(normalized):
            raise ValueError(f"{field} must be lowercase hex")
        return normalized
    if field in {"xray_pbk"}:
        normalized = str(value or "").strip()
        if normalized and not XRAY_PUBLIC_KEY_RE.fullmatch(normalized):
            raise ValueError("xray_pbk contains unsupported characters")
        return normalized
    if field in {"xray_xhttp_path_prefix"}:
        normalized = str(value or "").strip()
        if not XRAY_PATH_PREFIX_RE.fullmatch(normalized):
            raise ValueError("xray_xhttp_path_prefix must start with / and contain only letters, digits, ., _, -, /")
        return normalized
    if field in {"title", "region", "notes", "flag"}:
        normalized = str(value or "").strip()
        if "\n" in normalized or "\r" in normalized:
            raise ValueError(f"{field} must be a single line")
        return normalized
    raise ValueError(f"Unsupported field: {field}")


def shell_env_assignment(name: str, value: object) -> str:
    return f"{name}={shlex.quote(str(value))}"


def escape_markdown(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("[", "\\[")
    )


def redact_sensitive_text(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""

    patterns = [
        (VPN_URI_RE, "vpn://[REDACTED]"),
        (VLESS_URI_RE, "vless://[REDACTED]"),
        (UUID_RE, "[REDACTED_UUID]"),
        (re.compile(r'(?i)("?(?:privatekey|publickey|presharedkey|xray_pbk|xray_sid|xray_short_id|uuid)"?\s*[:=]\s*")[^"\n]+(")'), r"\1[REDACTED]\2"),
        (re.compile(r"(?im)^(\s*(?:PrivateKey|PublicKey|PresharedKey)\s*=\s*).+$"), r"\1[REDACTED]"),
        (re.compile(r'(?i)([?&](?:pbk|sid)=)[^&#\s]+'), r"\1[REDACTED]"),
    ]
    for pattern, repl in patterns:
        text = pattern.sub(repl, text)
    return text
