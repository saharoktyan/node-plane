#!/usr/bin/env python3
"""Verify and extract node-plane server release artifacts (stdlib only)."""
import argparse
from contextlib import ExitStack
import gzip
import hashlib
import hmac
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tempfile

MAX_METADATA_SIZE = 1 * 1024 * 1024  # 1 MiB
MAX_ARCHIVE_SIZE = 256 * 1024 * 1024  # 256 MiB
MAX_PAYLOAD_SIZE = 256 * 1024 * 1024  # 256 MiB
MAX_TAR_SIZE = MAX_PAYLOAD_SIZE + 1024 * 1024  # payload plus bounded tar metadata
MAX_ENTRIES = 16
ALLOWED_COMPONENTS = ("driver", "agent")
LEGAL_FILES = {"LICENSE", "THIRD_PARTY_LICENSES"}
HEX64_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
SUMS_LINE_PATTERN = re.compile(r"^([0-9a-fA-F]{64})\s+[*]?(\S+)$")


def parse_checksum(sums_path, asset_name):
    """Parse exactly one strict 64-hex checksum entry for asset_name from sums_path."""
    path = Path(sums_path)
    if not path.is_file():
        raise ValueError(f"Checksum file not found or not a regular file: {sums_path}")
    size = path.stat().st_size
    if size > MAX_METADATA_SIZE:
        raise ValueError(f"Checksum file exceeds maximum size limit ({size} > {MAX_METADATA_SIZE})")
    if not asset_name or "/" in asset_name or "\\" in asset_name:
        raise ValueError(f"Invalid asset name: {asset_name}")

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Checksum file is not valid UTF-8 text: {exc}") from exc

    matches = []
    has_valid_line = False
    for line_num, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        match = SUMS_LINE_PATTERN.fullmatch(line)
        if not match:
            raise ValueError(f"Malformed checksum entry on line {line_num} in {sums_path}: {line!r}")
        has_valid_line = True
        digest, filename = match.groups()
        if filename == asset_name:
            matches.append(digest.lower())

    if not has_valid_line:
        raise ValueError(f"Checksum file contains no valid entries: {sums_path}")

    if len(matches) == 0:
        raise ValueError(f"Missing SHA-256 entry for asset '{asset_name}' in {sums_path}")
    if len(matches) > 1:
        raise ValueError(f"Duplicate SHA-256 entry for asset '{asset_name}' in {sums_path} ({len(matches)} matches)")

    return matches[0]


def verify_server_archive(component, archive_path, expected_sha256, output_path=None):
    """Inspect safe layout and checksum before extracting only the expected regular binary."""
    if component not in ALLOWED_COMPONENTS:
        raise ValueError(f"Unsupported component '{component}'; allowed: {ALLOWED_COMPONENTS}")

    if not expected_sha256 or not HEX64_PATTERN.fullmatch(expected_sha256):
        raise ValueError(f"Invalid expected SHA-256 format: {expected_sha256!r}")
    expected_hex = expected_sha256.lower()

    archive_file = Path(archive_path)
    if not archive_file.is_file():
        raise ValueError(f"Archive file not found or not a regular file: {archive_path}")

    archive_size = archive_file.stat().st_size
    if archive_size == 0:
        raise ValueError(f"Archive file is empty: {archive_path}")
    if archive_size > MAX_ARCHIVE_SIZE:
        raise ValueError(f"Archive size exceeds maximum allowed limit ({archive_size} > {MAX_ARCHIVE_SIZE})")

    # Compute SHA-256 before opening or extracting
    hasher = hashlib.sha256()
    with open(archive_file, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    actual_hex = hasher.hexdigest().lower()

    if not hmac.compare_digest(actual_hex, expected_hex):
        raise ValueError(
            f"SHA-256 digest mismatch for {archive_path}: expected {expected_hex}, got {actual_hex}"
        )

    # Bound expansion before tar parsing, including PAX/long-name headers which
    # tarfile consumes internally before exposing a member to the caller.
    with ExitStack() as resources:
        data = resources.enter_context(tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024))
        expanded = 0
        try:
            with gzip.open(archive_file, 'rb') as compressed:
                while True:
                    chunk = compressed.read(min(65536, MAX_TAR_SIZE - expanded + 1))
                    if not chunk:
                        break
                    expanded += len(chunk)
                    if expanded > MAX_TAR_SIZE:
                        raise ValueError('Expanded tar stream exceeds size limit')
                    data.write(chunk)
            data.seek(0)
            archive = resources.enter_context(tarfile.open(fileobj=data, mode='r:'))
        except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError) as exc:
            raise ValueError(f"Invalid tar.gz server archive: {exc}") from exc

        members = []
        for member in archive:
            if len(members) >= MAX_ENTRIES:
                raise ValueError('Server archive contains too many entries')
            members.append(member)

        if not members:
            raise ValueError(f"Server archive contains no entries: {archive_path}")
        if len(members) > MAX_ENTRIES:
            raise ValueError(f"Server archive contains too many entries ({len(members)} > {MAX_ENTRIES})")

        alias_suffixed = f"node-plane-{component}-linux-amd64"
        alias_unsuffixed = f"node-plane-{component}"
        allowed_names = {alias_suffixed, alias_unsuffixed} | LEGAL_FILES

        seen_names = set()
        found_binary_aliases = set()
        binary_member = None
        total_payload_size = 0

        for member in members:
            name = member.name
            if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE}:
                raise ValueError(
                    f"Archive member '{name}' is not a regular file "
                    f"(symlinks, hardlinks, dirs, devices, FIFOs rejected)"
                )

            if name.startswith("/") or name.startswith("\\") or re.match(r"^[A-Za-z]:", name):
                raise ValueError(f"Absolute path in archive rejected: {name}")

            posix_path = PurePosixPath(name)
            if ".." in posix_path.parts or ".." in name:
                raise ValueError(f"Path traversal in archive rejected: {name}")

            if len(posix_path.parts) != 1 or str(posix_path) != name or "\x00" in name:
                raise ValueError(f"Nested or unsafe path in archive rejected: {name}")

            if name in seen_names:
                raise ValueError(f"Duplicate archive entry rejected: {name}")
            seen_names.add(name)

            if name not in allowed_names:
                raise ValueError(f"Unexpected entry in server archive: {name}")

            if name in (alias_suffixed, alias_unsuffixed):
                found_binary_aliases.add(name)
                binary_member = member

            if member.size < 0 or member.size > MAX_PAYLOAD_SIZE:
                raise ValueError(f"Archive entry '{name}' exceeds size limit: {member.size}")
            total_payload_size += member.size
            if total_payload_size > MAX_PAYLOAD_SIZE:
                raise ValueError(
                    f"Total unpacked payload exceeds limit ({total_payload_size} > {MAX_PAYLOAD_SIZE})"
                )

        if len(found_binary_aliases) == 0:
            raise ValueError(
                f"Missing required binary for {component}: expected {alias_suffixed} or {alias_unsuffixed}"
            )
        if len(found_binary_aliases) > 1:
            raise ValueError(f"Both binary aliases present in archive rejected: {sorted(found_binary_aliases)}")

        assert binary_member is not None
        if binary_member.size == 0:
            raise ValueError(f"Binary member '{binary_member.name}' is empty")

        if not (binary_member.mode & 0o111):
            raise ValueError(
                f"Binary member '{binary_member.name}' is not executable (mode: {oct(binary_member.mode)})"
            )

        if output_path is not None:
            out_file = Path(output_path)
            out_file.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=out_file.parent, prefix=f'.{out_file.name}.')
            out_tmp = Path(temporary)
            try:
                with os.fdopen(fd, 'wb') as dst:
                    extracted_file = archive.extractfile(binary_member)
                    if extracted_file is None:
                        raise ValueError(f"Could not extract member: {binary_member.name}")
                    bytes_copied = 0
                    with extracted_file as src:
                        while True:
                            buf = src.read(65536)
                            if not buf:
                                break
                            bytes_copied += len(buf)
                            if bytes_copied > MAX_PAYLOAD_SIZE or bytes_copied > binary_member.size:
                                raise ValueError("Extracted size exceeded archive member declaration")
                            dst.write(buf)
                if bytes_copied != binary_member.size:
                    raise ValueError(
                        f"Extracted size mismatch: expected {binary_member.size}, got {bytes_copied}"
                    )
                os.chmod(out_tmp, 0o755)
                out_tmp.replace(out_file)
            finally:
                if out_tmp.exists():
                    out_tmp.unlink()

    return {
        "component": component,
        "binary_name": binary_member.name,
        "sha256": actual_hex,
        "output_path": str(output_path) if output_path else None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify and extract node-plane server release artifacts.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse_cmd = subparsers.add_parser("parse-checksum", help="Parse checksum from SHA256SUMS.txt")
    parse_cmd.add_argument("--sums-file", required=True, help="Path to SHA256SUMS.txt")
    parse_cmd.add_argument("--asset-name", required=True, help="Asset filename to look up")

    extract_cmd = subparsers.add_parser("verify-and-extract", help="Verify archive and extract binary")
    extract_cmd.add_argument("--component", choices=ALLOWED_COMPONENTS, required=True, help="Target component")
    extract_cmd.add_argument("--archive", required=True, help="Path to tar.gz archive")
    extract_cmd.add_argument("--output", required=True, help="Destination path for extracted binary")
    group = extract_cmd.add_mutually_exclusive_group(required=True)
    group.add_argument("--expected-sha256", help="Expected 64-hex SHA-256 digest")
    group.add_argument("--sums-file", help="Path to SHA256SUMS.txt")
    extract_cmd.add_argument("--asset-name", help="Asset filename (required with --sums-file)")

    verify_cmd = subparsers.add_parser("verify-archive", help="Verify archive layout and checksum without extracting")
    verify_cmd.add_argument("--component", choices=ALLOWED_COMPONENTS, required=True, help="Target component")
    verify_cmd.add_argument("--archive", required=True, help="Path to tar.gz archive")
    vgroup = verify_cmd.add_mutually_exclusive_group(required=True)
    vgroup.add_argument("--expected-sha256", help="Expected 64-hex SHA-256 digest")
    vgroup.add_argument("--sums-file", help="Path to SHA256SUMS.txt")
    verify_cmd.add_argument("--asset-name", help="Asset filename (required with --sums-file)")

    args = parser.parse_args(argv)

    try:
        if args.command == "parse-checksum":
            digest = parse_checksum(args.sums_file, args.asset_name)
            sys.stdout.write(f"{digest}\n")
            return 0
        elif args.command == "verify-and-extract":
            expected = args.expected_sha256
            if not expected:
                if not args.asset_name:
                    parser.error("--asset-name is required when using --sums-file")
                expected = parse_checksum(args.sums_file, args.asset_name)
            verify_server_archive(args.component, args.archive, expected, output_path=args.output)
            return 0
        elif args.command == "verify-archive":
            expected = args.expected_sha256
            if not expected:
                if not args.asset_name:
                    parser.error("--asset-name is required when using --sums-file")
                expected = parse_checksum(args.sums_file, args.asset_name)
            verify_server_archive(args.component, args.archive, expected, output_path=None)
            return 0
    except Exception as exc:
        sys.stderr.write(f"Verification error: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
