"""AmneziaWG 3.1 profile generation and validation for node runtime scripts."""

from __future__ import annotations

import base64
import binascii
import os
import random
import re
import secrets
import sys
from pathlib import Path

LEGACY_FIELDS = ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5")
AWG3_FIELDS = ("HeaderProtectionKey", "ContentPaddingAddition", "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime", "KeepaliveTimeout", "MaxHandshakeAttempts", "RandomTrailers", "DisableCookies")
ALL_FIELDS = LEGACY_FIELDS + AWG3_FIELDS


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


def _range(value: str, maximum: int) -> tuple[int, int]:
    if not re.fullmatch(r"\d+(?:-\d+)?", value):
        raise ValueError(f"Invalid numeric range: {value!r}")
    parts = [int(part) for part in value.split("-", 1)]
    low, high = parts[0], parts[-1]
    if low > high or high > maximum:
        raise ValueError(f"Out-of-range value: {value!r}")
    return low, high


def _single(value: str, maximum: int) -> int:
    if not value.isdecimal() or int(value) > maximum:
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
    for key in ("S1", "S2", "S3", "S4"):
        size = _single(values[key], 65535)
        if version == "3.1" and size < 12:
            raise ValueError(f"Invalid {key} for header protection")
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
        for key in AWG3_FIELDS[1:7]:
            if values.get(key):
                _range(values[key], 65535)


def _i_values(preset: str) -> dict[str, str]:
    if preset == "dns":
        return {"I1": "<rc 2><b 0x01000001000000000000><r 32>", "I2": "<rc 2><b 0x01000001000000000000><r 64>", "I3": "<r 48>", "I4": "<r 80>", "I5": "<r 40>"}
    if preset == "chaos":
        return {"I1": f"<b 0x{secrets.token_hex(4)}><rc 4><r {random.randint(500, 900)}>", **{f"I{i}": f"<r {random.randint(100, 900)}>" for i in range(2, 6)}}
    if preset != "quic":
        raise ValueError(f"Unknown I1 preset: {preset}")
    return {"I1": "<b 0xc000000001><rc 8><r 900>", "I2": "<b 0x40><rc 4><r 100>", "I3": "<r 900>", "I4": "<r 100>", "I5": "<r 900>"}


def new_profile(preset: str) -> dict[str, str]:
    s = str(random.randint(16, 32))
    jmin = random.randint(40, 100)
    values = {
        "Jc": str(random.randint(3, 7)), "Jmin": str(jmin), "Jmax": str(random.randint(jmin + 40, 160)),
        **{f"S{i}": s for i in range(1, 5)},
        **{f"H{i}": str(i) for i in range(1, 5)},
        **_i_values(preset),
        "HeaderProtectionKey": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
        "ContentPaddingAddition": "16-64",
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
    if len(sys.argv) < 3 or sys.argv[1] not in ("init", "migrate", "regenerate", "validate"):
        raise SystemExit("Usage: awg_profile.py init|migrate|regenerate|validate PATH [PRESET]")
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
