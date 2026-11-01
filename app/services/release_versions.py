"""Pure release version comparison without runtime or database initialization."""
import re

CHANNEL_BRANCHES = {"dev": "dev", "beta": "beta", "stable": "main"}
_RELEASE_TAG_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(alpha|beta|rc)\.(0|[1-9][0-9]*))?$")


def release_channel(tag: str) -> str:
    """Return the release lane; arbitrary refs and legacy/ tags are invalid."""
    match = _RELEASE_TAG_RE.fullmatch(tag)
    if not match:
        raise ValueError("Invalid release tag")
    stage = match[4]
    return "dev" if stage == "alpha" else "beta" if stage in {"beta", "rc"} else "stable"


def branch_channel(branch: str) -> str:
    for channel, candidate in CHANNEL_BRANCHES.items():
        if branch == candidate:
            return channel
    raise ValueError("Update branch must be dev, beta or main")


def parse_release_metadata(data: bytes, *, tag: str, channel: str, commit: str | None = None) -> dict[str, str]:
    """Require explicit membership, excluding releases predating channel isolation."""
    if len(data) > 4096:
        raise ValueError("Release metadata exceeds size limit")
    fields = {}
    for line in data.decode("utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key or key in fields:
            raise ValueError("Malformed or duplicate release metadata field")
        fields[key] = value
    if (fields.get("format") != "2" or fields.get("tag") != tag
            or fields.get("version") != tag.removeprefix("v")
            or fields.get("channel") != channel or release_channel(tag) != channel
            or not re.fullmatch(r"[0-9a-f]{40}", fields.get("commit", ""))
            or (commit is not None and fields["commit"] != commit)):
        raise ValueError("Release metadata does not match channel, tag or commit")
    return fields

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


