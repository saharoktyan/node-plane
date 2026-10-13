"""Pure release version comparison without runtime or database initialization."""
import re

_SEMVER_RE = re.compile(r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)(?:-(?P<pre>[0-9A-Za-z.-]+))?$")


def _parse_semver(value: str) -> tuple[int, int, int, tuple[int, ...], str] | None:
    raw = str(value or "").strip()
    match = _SEMVER_RE.fullmatch(raw)
    if not match:
        return None
    pre = str(match.group("pre") or "")
    pre_parts: tuple[int, ...] = ()
    if pre.startswith("alpha."):
        suffix = pre.split(".", 1)[1]
        if suffix.isdigit():
            pre_parts = (0, int(suffix))
        else:
            pre_parts = (0, 0)
    elif pre:
        pre_parts = (1, 0)
    else:
        pre_parts = (2, 0)
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


