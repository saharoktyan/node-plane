#!/usr/bin/env python3
"""Generate or verify deterministic THIRD_PARTY_LICENSES for locked Rust crate graphs.

Collects third-party license information across Node Plane's Rust manifests using
cargo-about 0.9.2, preserves packaged legal files and native notices, incorporates
verified upstream supplement files, deduplicates identical license texts by SHA-256,
and maintains full crate-to-component graph mappings without leaking local paths or
timestamps.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Sequence

REQUIRED_CARGO_ABOUT_VERSION = "0.9.2"
OWN_CRATES = {"node-plane-cli", "node-plane-driver", "node-plane-agent"}
SERVER_TARGET = "x86_64-unknown-linux-gnu"

LEGAL_PREFIXES = ("license", "licence", "copying", "notice", "copyright")
EXCLUDE_DIR_NAMES = {"test", "tests", "testdata", "example", "examples", ".git", ".github", "target"}
NON_LEGAL_EXTS = {
    ".rs", ".c", ".h", ".cpp", ".cc", ".cxx", ".py", ".go", ".java",
    ".sh", ".toml", ".json", ".yaml", ".yml", ".css", ".js",
    ".o", ".a", ".so", ".dylib", ".dll", ".exe", ".png", ".jpg", ".svg",
}


@dataclass(frozen=True)
class ManifestSpec:
    component: str
    manifest_path: Path
    target: str | None = None


def normalize_text(text: str) -> str:
    """Normalize line endings to standard Unix newlines and strip trailing whitespace."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def check_cargo_about_version(cargo_about_bin: str) -> None:
    """Ensure cargo-about is installed and matches the exact required version 0.9.2."""
    try:
        proc = subprocess.run(
            [cargo_about_bin, "--version"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        raise RuntimeError(f"cargo-about binary not found at '{cargo_about_bin}'") from None
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Failed to execute cargo-about ({cargo_about_bin}): {exc.stderr.strip()}"
        ) from exc

    output = proc.stdout.strip()
    match = re.fullmatch(r"cargo-about\s+([0-9]+\.[0-9]+\.[0-9]+)", output)
    version = match.group(1) if match else output
    if version != REQUIRED_CARGO_ABOUT_VERSION:
        raise RuntimeError(
            f"cargo-about exact version {REQUIRED_CARGO_ABOUT_VERSION} is required, but found '{version}'"
        )


def run_cargo_fetch(cargo_bin: str, manifest_path: Path) -> None:
    """Fetch locked dependencies for a manifest."""
    subprocess.run(
        [cargo_bin, "fetch", "--locked", "--manifest-path", str(manifest_path)],
        check=True,
    )


def run_cargo_about_generate(
    cargo_about_bin: str,
    config_path: Path,
    manifest_path: Path,
    target: str | None = None,
) -> dict:
    """Run cargo-about generate in JSON mode for a given manifest and optional target."""
    cmd = [
        cargo_about_bin,
        "generate",
        "-c",
        str(config_path),
        "-m",
        str(manifest_path),
        "--locked",
        "--offline",
        "--fail",
        "--format",
        "json",
    ]
    if target:
        cmd.extend(["--target", target])

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"cargo-about failed for {manifest_path} (exit code {proc.returncode}):\n{proc.stderr.strip()}"
        )
    return json.loads(proc.stdout)


def scan_packaged_legal_files(
    pkg_dir: Path, explicit_license_file: str | None = None
) -> list[tuple[str, Path]]:
    """Scan a crate package directory recursively for bundled legal files."""
    pkg_dir = pkg_dir.resolve()
    found: list[tuple[str, Path]] = []
    seen: set[Path] = set()

    if explicit_license_file:
        exp_path = (pkg_dir / explicit_license_file).resolve()
        if not exp_path.is_relative_to(pkg_dir):
            raise ValueError("Explicit legal file escapes package directory")
        if exp_path.is_file():
            found.append((str(explicit_license_file), exp_path))
            seen.add(exp_path)

    if not pkg_dir.is_dir():
        return found

    for root, dirs, files in os.walk(pkg_dir):
        dirs[:] = sorted([
            d for d in dirs
            if d.lower() not in EXCLUDE_DIR_NAMES and not d.startswith(".")
        ])
        for f in sorted(files):
            if f.lower().startswith(LEGAL_PREFIXES):
                ext = Path(f).suffix.lower()
                if ext not in NON_LEGAL_EXTS:
                    fp = (Path(root) / f).resolve()
                    if fp.is_file() and fp not in seen:
                        try:
                            rel = fp.relative_to(pkg_dir.resolve())
                            found.append((str(rel), fp))
                        except ValueError:
                            raise ValueError("Legal file escapes package directory") from None
                        seen.add(fp)
    return found


def load_upstream_manifest(manifest_path: Path) -> dict[str, list[dict[str, str]]]:
    """Load and validate the upstream license supplement manifest."""
    if not manifest_path.is_file():
        return {}

    manifest_dir = manifest_path.parent.resolve()
    with open(manifest_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, list):
        raise ValueError(f"Upstream manifest at {manifest_path} must be a JSON list")

    by_crate: dict[str, list[dict[str, str]]] = {}
    for entry in raw:
        crate_key = entry.get("crate")
        if not crate_key:
            raise ValueError(f"Entry in {manifest_path} missing 'crate' attribute")
        if crate_key in by_crate:
            raise ValueError(f"Duplicate upstream supplement entry: {crate_key}")
        files_spec = entry.get("files", [])
        validated_files = []
        for f_spec in files_spec:
            rel_path = f_spec.get("path")
            expected_sha = f_spec.get("sha256")
            if not rel_path or not expected_sha:
                raise ValueError(f"File spec in {crate_key} missing 'path' or 'sha256'")

            target_path = (manifest_dir / rel_path).resolve()
            if not target_path.is_relative_to(manifest_dir):
                raise ValueError(
                    f"Path containment violation for {crate_key}: path '{rel_path}' escapes manifest directory"
                )
            if not target_path.is_file():
                raise FileNotFoundError(
                    f"Upstream license file missing for {crate_key}: {target_path}"
                )

            data = target_path.read_bytes()
            actual_sha = hashlib.sha256(data).hexdigest()
            if actual_sha != expected_sha:
                raise ValueError(
                    f"Hash mismatch for {rel_path} ({crate_key}): expected {expected_sha}, got {actual_sha}"
                )
            validated_files.append({
                "path": rel_path,
                "url": f_spec.get("url", ""),
                "sha256": expected_sha,
                "text": normalize_text(data.decode("utf-8")),
            })
        by_crate[crate_key] = validated_files
    return by_crate


def collect_licenses(
    manifest_outputs: Sequence[tuple[str, dict]],
    upstream_supplements: dict[str, list[dict[str, str]]],
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Aggregate crate metadata and deduplicate license texts by SHA-256."""
    crates_info: dict[str, dict] = {}
    texts_by_hash: dict[str, dict] = {}

    for component_name, data in manifest_outputs:
        for c_entry in data.get("crates", []):
            pkg = c_entry.get("package", {})
            name = pkg.get("name", "")
            version = pkg.get("version", "")
            source = pkg.get("source")

            if source is None:
                if name in OWN_CRATES:
                    continue
                raise RuntimeError(
                    f"Unknown local dependency without source: {name}@{version}"
                )

            crate_key = f"{name}@{version}"
            if crate_key not in crates_info:
                crates_info[crate_key] = {
                    "name": name,
                    "version": version,
                    "spdx": pkg.get("license") or "UNKNOWN",
                    "source": source,
                    "manifest_path": pkg.get("manifest_path"),
                    "license_file": pkg.get("license_file"),
                    "components": set(),
                }
            if crates_info[crate_key]["source"] != source:
                raise ValueError(f"Conflicting sources for {crate_key}")
            crates_info[crate_key]["components"].add(component_name)

        # Collect cargo-about selected texts
        for lic in data.get("licenses", []):
            text = normalize_text(lic.get("text", ""))
            if not text or not text.strip():
                continue
            sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if sha256 not in texts_by_hash:
                texts_by_hash[sha256] = {"text": text, "crates": set()}
            for u in lic.get("used_by", []):
                cr = u.get("crate", {})
                cr_name = cr.get("name")
                cr_ver = cr.get("version")
                if cr_name and cr_name not in OWN_CRATES:
                    texts_by_hash[sha256]["crates"].add(f"{cr_name}@{cr_ver}")

    # Process packaged legal files and upstream supplements for each external crate
    for crate_key, info in sorted(crates_info.items()):
        manifest_path_str = info.get("manifest_path")
        packaged_files = []
        if manifest_path_str:
            pkg_dir = Path(manifest_path_str).parent
            packaged_files = scan_packaged_legal_files(pkg_dir, info.get("license_file"))

        has_packaged = len(packaged_files) > 0
        has_upstream = crate_key in upstream_supplements and len(upstream_supplements[crate_key]) > 0

        if not has_packaged and not has_upstream:
            raise RuntimeError(
                f"Crate '{crate_key}' has no packaged legal files and no upstream supplement manifest entry"
            )

        for _rel, fpath in packaged_files:
            try:
                data = fpath.read_bytes()
            except OSError as err:
                raise RuntimeError(f"Failed to read packaged legal file {fpath}: {err}") from err
            text = normalize_text(data.decode("utf-8"))
            sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if sha256 not in texts_by_hash:
                texts_by_hash[sha256] = {"text": text, "crates": set()}
            texts_by_hash[sha256]["crates"].add(crate_key)

        if has_upstream:
            for item in upstream_supplements[crate_key]:
                text = item["text"]
                sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if sha256 not in texts_by_hash:
                    texts_by_hash[sha256] = {"text": text, "crates": set()}
                texts_by_hash[sha256]["crates"].add(crate_key)

    return crates_info, texts_by_hash


def render_third_party_licenses(
    crates_info: dict[str, dict],
    texts_by_hash: dict[str, dict],
    stdlib_text: str | None = None,
) -> str:
    """Format THIRD_PARTY_LICENSES content deterministically."""
    lines: list[str] = [
        "=" * 80,
        "Node Plane Third-Party Software Notices and Licenses",
        "=" * 80,
        "",
        "This document contains third-party software notices and license texts for",
        "Rust dependencies compiled into Node Plane binaries (node-plane-cli,",
        "node-plane-driver, node-plane-agent), including a conservative superset of",
        "build dependencies and supported target platforms.",
        "",
        "The root LICENSE file covers Node Plane itself. These third-party notices",
        "apply to the compiled Rust binaries and repository distribution; they do not",
        "cover Python packages or host system libraries downloaded and installed",
        "separately by the runtime environment.",
        "",
    ]

    if stdlib_text and stdlib_text.strip():
        lines.extend([
            "=" * 80,
            "Rust Standard Library (Rust 1.90.0)",
            "Source: Pinned Rust toolchain distribution (1.90.0)",
            "=" * 80,
            "",
            stdlib_text.strip(),
            "",
        ])

    lines.extend([
        "=" * 80,
        "Third-Party Crate Dependency Index & Graph Component Mapping",
        "=" * 80,
        "",
    ])

    for crate_key in sorted(crates_info.keys()):
        info = crates_info[crate_key]
        comp_str = ", ".join(sorted(info["components"]))
        lines.extend([
            f"- {crate_key}",
            f"  Declared SPDX: {info['spdx']}",
            f"  Components: {comp_str}",
            f"  Source: {info['source']}",
            f"  Source package: https://crates.io/crates/{info['name']}/{info['version']}",
        ])
    lines.append("")

    lines.extend([
        "=" * 80,
        "Third-Party License Notices & Verbatim Texts",
        "=" * 80,
        "",
    ])

    sorted_hashes = sorted(
        texts_by_hash.keys(),
        key=lambda h: (sorted(texts_by_hash[h]["crates"])[0] if texts_by_hash[h]["crates"] else "", h),
    )

    for sha256 in sorted_hashes:
        entry = texts_by_hash[sha256]
        using_crates = sorted(entry["crates"])
        lines.extend([
            "-" * 80,
            f"License Notice (SHA-256: {sha256})",
            "Used by:",
        ])
        for ckey in using_crates:
            cinfo = crates_info.get(ckey)
            if cinfo:
                comp_str = ", ".join(sorted(cinfo["components"]))
                lines.append(f"  - {ckey} [Components: {comp_str}] [SPDX: {cinfo['spdx']}]")
            else:
                lines.append(f"  - {ckey}")
        lines.extend([
            "-" * 80,
            "",
            entry["text"],
            "",
        ])

    rendered = "\n".join(lines)
    if not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


def generate_licenses(
    root_dir: Path,
    manifest_specs: Sequence[ManifestSpec],
    about_toml: Path,
    upstream_manifest: Path,
    stdlib_license: Path | None,
    cargo_about_bin: str,
    cargo_bin: str,
    offline: bool = False,
) -> str:
    """Generate the full third-party licenses document from manifests."""
    check_cargo_about_version(cargo_about_bin)

    manifest_outputs: list[tuple[str, dict]] = []
    for spec in manifest_specs:
        manifest_path = (root_dir / spec.manifest_path).resolve()
        if not manifest_path.is_file():
            raise FileNotFoundError(f"Manifest not found: {manifest_path}")

        if not offline:
            run_cargo_fetch(cargo_bin, manifest_path)

        data = run_cargo_about_generate(
            cargo_about_bin=cargo_about_bin,
            config_path=about_toml.resolve(),
            manifest_path=manifest_path,
            target=spec.target,
        )
        manifest_outputs.append((spec.component, data))

    supplements = load_upstream_manifest(upstream_manifest.resolve())

    crates_info, texts_by_hash = collect_licenses(manifest_outputs, supplements)

    stdlib_text = None
    if stdlib_license and stdlib_license.is_file():
        stdlib_text = normalize_text(stdlib_license.read_text(encoding="utf-8"))

    return render_third_party_licenses(crates_info, texts_by_hash, stdlib_text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate or verify deterministic THIRD_PARTY_LICENSES for locked Rust crate graphs."
    )
    parser.add_argument(
        "--cargo-about",
        default=os.environ.get("CARGO_ABOUT", "cargo-about"),
        help="Path to cargo-about binary (default: CARGO_ABOUT env or 'cargo-about')",
    )
    parser.add_argument(
        "--cargo",
        default=os.environ.get("CARGO", "cargo"),
        help="Path to cargo binary (default: CARGO env or 'cargo')",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip cargo fetch before generating licenses",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check that committed output matches generated licenses in memory without modifying disk",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Path to output THIRD_PARTY_LICENSES file (default: <root>/THIRD_PARTY_LICENSES)",
    )
    parser.add_argument(
        "--about-toml",
        type=Path,
        default=None,
        help="Path to about.toml config file (default: <root>/about.toml)",
    )
    parser.add_argument(
        "--upstream-manifest",
        type=Path,
        default=None,
        help="Path to upstream supplement manifest (default: <root>/licenses/upstream/manifest.json)",
    )
    parser.add_argument(
        "--stdlib-license",
        type=Path,
        default=None,
        help="Path to stdlib license file (default: <root>/licenses/rust-stdlib-1.90.0.txt)",
    )

    args = parser.parse_args(argv)

    root_dir = Path(__file__).resolve().parents[1]
    about_toml = args.about_toml or (root_dir / "about.toml")
    upstream_manifest = args.upstream_manifest or (root_dir / "licenses/upstream/manifest.json")
    stdlib_license = args.stdlib_license or (root_dir / "licenses/rust-stdlib-1.90.0.txt")
    output_path = args.output or (root_dir / "THIRD_PARTY_LICENSES")

    manifest_specs = [
        ManifestSpec(
            component="node-plane-cli",
            manifest_path=Path("rust/node-plane-cli/Cargo.toml"),
            target=None,
        ),
        ManifestSpec(
            component="node-plane-driver",
            manifest_path=Path("rust/node-driver/Cargo.toml"),
            target=SERVER_TARGET,
        ),
        ManifestSpec(
            component="node-plane-agent",
            manifest_path=Path("rust/node-agent/Cargo.toml"),
            target=SERVER_TARGET,
        ),
    ]

    try:
        # Standard-library notices belong to the pinned toolchain too. Updating
        # Rust must refresh this document instead of silently retaining old text.
        toolchain = (root_dir / "rust-toolchain.toml").read_text(encoding="utf-8")
        if not re.search(r'^channel\s*=\s*"1\.90\.0"\s*$', toolchain, re.MULTILINE):
            raise ValueError("Pinned Rust changed; refresh the bundled standard-library notices and generator version")
        if not stdlib_license.is_file():
            raise FileNotFoundError("Required Rust standard-library notices are missing")
        generated_text = generate_licenses(
            root_dir=root_dir,
            manifest_specs=manifest_specs,
            about_toml=about_toml,
            upstream_manifest=upstream_manifest,
            stdlib_license=stdlib_license,
            cargo_about_bin=args.cargo_about,
            cargo_bin=args.cargo,
            offline=args.offline,
        )
    except Exception as exc:
        print(f"Error generating third-party licenses: {exc}", file=sys.stderr)
        return 1

    if args.check:
        if not output_path.is_file():
            print(
                f"Error: License file '{output_path}' does not exist. Run scripts/generate_third_party_licenses.py to generate it.",
                file=sys.stderr,
            )
            return 1
        committed_text = output_path.read_bytes().decode("utf-8")
        if committed_text != generated_text:
            diff = "".join(
                difflib.unified_diff(
                    committed_text.splitlines(keepends=True),
                    generated_text.splitlines(keepends=True),
                    fromfile=f"committed:{output_path.name}",
                    tofile=f"generated:{output_path.name}",
                    n=3,
                )
            )
            print(
                f"Error: License file '{output_path}' is stale or out of date.\n{diff}",
                file=sys.stderr,
            )
            return 1
        print(f"Third-party licenses are up to date: {output_path}")
        return 0

    output_path.write_text(generated_text, encoding="utf-8")
    print(f"Successfully generated third-party licenses at {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
