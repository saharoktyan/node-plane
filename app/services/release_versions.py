"""Pure release version comparison without runtime or database initialization."""
import re

_SEMVER_RE = re.compile(r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)(?:-(?P<pre>[0-9A-Za-z.-]+))?$")


def _parse_semver(value: str) -> tuple[int, int, int, tuple[tuple[int, int | str], ...], str] | None:
    raw = str(value or "").strip()
    match = _SEMVER_RE.fullmatch(raw)
    if not match:
        return None
    pre = str(match.group("pre") or "")
    # Numeric identifiers compare numerically, textual identifiers lexically;
    # a stable release sorts after every prerelease of the same base version.
    pre_parts = ((2, 0),)
    if pre:
        parts = pre.split(".")
        if any(not part or (part.isdigit() and len(part) > 1 and part[0] == "0") for part in parts):
            return None
        pre_parts = tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)
    return (int(match.group("major")), int(match.group("minor")), int(match.group("patch")), pre_parts, raw)


def _compare_versions(left: str, right: str) -> int:
    left_parsed = _parse_semver(left)
    right_parsed = _parse_semver(right)
    if not left_parsed and not right_parsed:
        return 0
    if not left_parsed:
        return -1
    if not right_parsed:
        return 1
    left_key = left_parsed[:4]
    right_key = right_parsed[:4]
    if left_key < right_key:
        return -1
    if left_key > right_key:
        return 1
    return 0


