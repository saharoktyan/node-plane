"""AmneziaWG 3.1 profile validation for the controller's manual parameters.

The node agent generates and checks profiles in Rust
(`rust/node-agent/src/protocols/awg/profile.rs`); both decide the cases in
`tests/fixtures/awg_profile_cases.json` the same way.
"""

from __future__ import annotations

import base64
import binascii
import re

LEGACY_FIELDS = ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5")
AWG3_FIELDS = ("HeaderProtectionKey", "ContentPaddingAddition", "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime", "KeepaliveTimeout", "MaxHandshakeAttempts", "RandomTrailers", "DisableCookies")
ALL_FIELDS = LEGACY_FIELDS + AWG3_FIELDS
TIMING_FIELDS = AWG3_FIELDS[2:7]
# The pinned engine uses 12-byte header nonces and a 1700-byte iOS receive
# buffer. Bound server padding for clients on every supported platform.
PADDING_LIMITS = {"S1": 1700 - 148, "S2": 1700 - 92, "S3": 1700 - 64,
                  "S4": 32}  # NP policy: limit overhead on every data packet.


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
