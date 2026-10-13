"""AmneziaWG 3.1 profile generation and validation for node runtime scripts."""

from __future__ import annotations

import base64
import binascii
import os
import re
import secrets
import sys
from pathlib import Path

LEGACY_FIELDS = ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5")
AWG3_FIELDS = ("HeaderProtectionKey", "ContentPaddingAddition", "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime", "KeepaliveTimeout", "MaxHandshakeAttempts", "RandomTrailers", "DisableCookies")
ALL_FIELDS = LEGACY_FIELDS + AWG3_FIELDS
TIMING_FIELDS = AWG3_FIELDS[2:7]
# The pinned engine uses 12-byte header nonces and a 1700-byte iOS receive
# buffer. Bound server padding for clients on every supported platform.
PADDING_LIMITS = {"S1": 1700 - 148, "S2": 1700 - 92, "S3": 1700 - 64,
                  "S4": 32}  # NP policy: limit overhead on every data packet.
GENERATED_HEADER_MAX = 2**31 - 1  # Also fits signed client configuration editors.


def _random_int(low: int, high: int) -> int:
    return low + secrets.randbelow(high - low + 1)


def new_private_key() -> str:
    """Generate a clamped X25519 private key in WireGuard's base64 format."""
    key = bytearray(secrets.token_bytes(32))
    key[0] &= 248
    key[31] = (key[31] & 127) | 64
    return base64.b64encode(key).decode("ascii")


def interface_values(config: str) -> dict[str, str]:
    section = ""
    values: dict[str, str] = {}
    for raw in config.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line.lower()
        elif section == "[interface]" and "=" in line and not line.startswith("#"):
            key, value = (part.strip() for part in line.split("=", 1))
            values[key] = value
    return values


def protocol_version(values: dict[str, str]) -> str:
    if values.get("HeaderProtectionKey"):
        if values.get("RandomTrailers", "").lower() not in ("on", "off") or values.get("DisableCookies", "").lower() not in ("on", "off"):
            raise ValueError("Incomplete AWG 3.1 profile")
        return "3.1"
    if any(values.get(key) for key in AWG3_FIELDS):
        raise ValueError("AWG 3.x fields without HeaderProtectionKey")
    return "2"


def _range(value: str, maximum: int, minimum: int = 0) -> tuple[int, int]:
    if not re.fullmatch(r"[0-9]+(?:-[0-9]+)?", value):
        raise ValueError(f"Invalid numeric range: {value!r}")
    parts = [int(part) for part in value.split("-", 1)]
    low, high = parts[0], parts[-1]
    if low < minimum or low > high or high > maximum:
        raise ValueError(f"Out-of-range value: {value!r}")
    return low, high


def _single(value: str, maximum: int) -> int:
    if not re.fullmatch(r"[0-9]+", value) or int(value) > maximum:
        raise ValueError(f"Invalid integer: {value!r}")
    return int(value)


def validate(values: dict[str, str], *, require_31: bool = True) -> None:
    version = protocol_version(values)
    if require_31 and version != "3.1":
        raise ValueError("AWG 3.1 profile is required")
    for key in LEGACY_FIELDS:
        if not values.get(key):
            raise ValueError(f"Missing AWG field: {key}")
    if _single(values["Jmin"], 65535) > _single(values["Jmax"], 65535):
        raise ValueError("Jmin exceeds Jmax")
    _single(values["Jc"], 65535)
    for key, maximum in PADDING_LIMITS.items():
        size = _single(values[key], maximum)
        if version == "3.1" and size < 12:
            raise ValueError(f"Invalid {key} for header protection")
    if int(values["S1"]) + 56 == int(values["S2"]):
        raise ValueError("S1 and S2 produce equal handshake packet sizes")
    ranges = [_range(values[key], 4294967295) for key in ("H1", "H2", "H3", "H4")]
    for index, (low, high) in enumerate(ranges):
        if any(low <= other_high and other_low <= high for other_low, other_high in ranges[:index]):
            raise ValueError("Overlapping H1-H4 ranges")
    for key in ("I1", "I2", "I3", "I4", "I5"):
        expression = values[key]
        if not re.fullmatch(r"(?:<(?:b 0x[0-9a-fA-F]+|[r](?: \d+)|r[cd] \d+|t)>)+", expression):
            raise ValueError(f"Invalid {key} CPS expression")
        if any(len(hex_value) % 2 for hex_value in re.findall(r"<b 0x([0-9a-fA-F]+)>", expression)):
            raise ValueError(f"{key} contains odd-length hex data")
        for tag, length in re.findall(r"<(r|rc|rd) (\d+)>", expression):
            if int(length) > 1000:
                raise ValueError(f"{key} {tag} length exceeds 1000")
    if version == "3.1":
        try:
            key_bytes = base64.b64decode(values["HeaderProtectionKey"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid HeaderProtectionKey") from exc
        if len(key_bytes) != 32:
            raise ValueError("HeaderProtectionKey must contain 32 bytes")
        if values.get("ContentPaddingAddition"):
            _range(values["ContentPaddingAddition"], 65535)
        for key in TIMING_FIELDS:
            if values.get(key):
                _range(values[key], 65535, minimum=1)
        # Missing timing fields retain the pinned engine's 120s/180s defaults.
        rekey_high = _range(values.get("RekeyAfterTime") or "120", 65535, 1)[1]
        reject_low = _range(values.get("RejectAfterTime") or "180", 65535, 1)[0]
        if rekey_high >= reject_low:
            raise ValueError("RekeyAfterTime must finish before RejectAfterTime")


def _i_values(preset: str) -> dict[str, str]:
    if preset == "dns":
        return {"I1": "<rc 2><b 0x01000001000000000000><r 32>", "I2": "<rc 2><b 0x01000001000000000000><r 64>", "I3": "<r 48>", "I4": "<r 80>", "I5": "<r 40>"}
    if preset == "chaos":
        return {"I1": f"<b 0x{secrets.token_hex(4)}><rc 4><r {_random_int(500, 900)}>", **{f"I{i}": f"<r {_random_int(100, 900)}>" for i in range(2, 6)}}
    if preset != "quic":
        raise ValueError(f"Unknown I1 preset: {preset}")
    return {"I1": "<b 0xc000000001><rc 8><r 900>", "I2": "<b 0x40><rc 4><r 100>", "I3": "<r 900>", "I4": "<r 100>", "I5": "<r 900>"}


def new_profile(preset: str) -> dict[str, str]:
    s1, s2 = _random_int(16, 128), _random_int(16, 128)
    while s1 + 56 == s2:
        s2 = _random_int(16, 128)
    jmin = _random_int(40, 100)
    # Separate bands guarantee distinct single-value headers without retries.
    # Avoid wide H ranges with RandomTrailers: padded data can be mistaken for
    # handshake packets by the engine's receive classifier.
    band = (GENERATED_HEADER_MAX - 4) // 4
    headers = {f"H{i + 1}": str(_random_int(5 + i * band, 4 + (i + 1) * band))
               for i in range(4)}
    padding_low = _random_int(8, 24)
    rekey_low, rekey_high = _random_int(100, 115), _random_int(125, 145)
    values = {
        "Jc": str(_random_int(3, 7)), "Jmin": str(jmin), "Jmax": str(_random_int(jmin + 40, 250)),
        "S1": str(s1), "S2": str(s2), "S3": str(_random_int(12, 48)),
        "S4": str(_random_int(12, 32)),
        **headers,
        **_i_values(preset),
        "HeaderProtectionKey": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
        "ContentPaddingAddition": f"{padding_low}-{_random_int(padding_low + 8, 64)}",
        "RekeyAfterTime": f"{rekey_low}-{rekey_high}",
        "RejectAfterTime": f"{_random_int(180, 200)}-{_random_int(220, 260)}",
        "RekeyTimeout": f"{_random_int(3, 5)}-{_random_int(6, 8)}",
        "KeepaliveTimeout": f"{_random_int(8, 10)}-{_random_int(12, 18)}",
        "MaxHandshakeAttempts": f"{_random_int(16, 20)}-{_random_int(24, 32)}",
        "RandomTrailers": "on", "DisableCookies": "off",
    }
    validate(values)
    return values


def replace_interface_values(config: str, values: dict[str, str]) -> str:
    lines = config.splitlines()
    output: list[str] = []
    section = ""
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section == "[interface]":
                output.extend(f"{key} = {value}" for key, value in values.items() if key not in seen and value)
            section = stripped.lower()
        if section == "[interface]" and "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in values:
                if key not in seen and values[key]:
                    output.append(f"{key} = {values[key]}")
                seen.add(key)
                continue
        output.append(line)
    if section == "[interface]":
        output.extend(f"{key} = {value}" for key, value in values.items() if key not in seen and value)
    return "\n".join(output).rstrip() + "\n"


def migrate(config: str, preset: str) -> str:
    current = interface_values(config)
    if protocol_version(current) == "3.1":
        validate(current)
        return config
    profile = new_profile(preset)
    # Preserve the selected CPS pattern where valid, replacing unsupported tags only.
    for key in ("I1", "I2", "I3", "I4", "I5"):
        if current.get(key):
            profile[key] = re.sub(r"<(r|rc|rd) (\d+)>", lambda m: f"<{m.group(1)} {min(int(m.group(2)), 1000)}>", current[key])
    validate(profile)
    return replace_interface_values(config, profile)


def main() -> None:
    if sys.argv[1:] == ["genkey"]:
        print(new_private_key())
        return
    if len(sys.argv) < 3 or sys.argv[1] not in ("init", "migrate", "regenerate", "validate", "apply"):
        raise SystemExit("Usage: awg_profile.py genkey | init|migrate|regenerate|validate PATH [PRESET] | apply PATH PARAMETERS_JSON")
    action, path = sys.argv[1], Path(sys.argv[2])
    preset = sys.argv[3] if len(sys.argv) > 3 else "quic"
    if action == "init":
        for key, value in new_profile(preset).items():
            print(f"{key} = {value}")
        return
    original = path.read_text(encoding="utf-8")
    if action == "validate":
        validate(interface_values(original))
        return
    if action == 'apply':
        import json
        values = json.loads(Path(sys.argv[3]).read_text())
        if not isinstance(values, dict) or set(values) - set(ALL_FIELDS) or any(
                not isinstance(value, str) or not value or len(value) > 4096
                or any(ord(c) < 32 or ord(c) > 126 for c in value) for value in values.values()):
            raise ValueError('invalid manual AWG parameters')
        validate(values)
        updated = replace_interface_values(original, {key: values.get(key, '') for key in ALL_FIELDS})
    else:
        updated = migrate(original, preset) if action == "migrate" else replace_interface_values(original, new_profile(preset))
    if updated != original:
        backup = path.with_name(path.name + ".pre-awg31")
        if not backup.exists():
            backup.write_text(original, encoding="utf-8")
            backup.chmod(0o600)
        temporary = path.with_name(path.name + ".new")
        temporary.write_text(updated, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
        print("AWG profile upgraded; existing client configs must be reimported")


if __name__ == "__main__":
    main()
