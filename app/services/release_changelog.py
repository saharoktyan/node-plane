"""Read change sections without installer commands or release asset tables."""
import re


def version_changes(markdown, tag):
    version = re.escape(tag.removeprefix('v'))
    heading = rf'^##[ \t]+(?:\[v?{version}\]|v?{version})(?:[ \t]+[^\n]*)?$'
    match = re.search(heading, markdown, re.M)
    if not match:
        return ''
    rest = markdown[match.end():]
    return re.split(r'^##[ \t]+', rest, maxsplit=1, flags=re.M)[0].strip()


def release_changes(markdown):
    match = re.search(r"^##[ \t]+(?:Changes|What's Changed|Changelog|Release notes)[ \t]*$", markdown, re.M | re.I)
    if not match:
        return ''
    return re.split(r'^##[ \t]+', markdown[match.end():], maxsplit=1, flags=re.M)[0].strip()
