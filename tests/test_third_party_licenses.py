"""Tests for scripts/generate_third_party_licenses.py."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

# Import generator script through importlib with sys.modules registration for dataclasses
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_third_party_licenses.py"
_spec = importlib.util.spec_from_file_location("generate_third_party_licenses", _SCRIPT_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load module from {_SCRIPT_PATH}")
gen = importlib.util.module_from_spec(_spec)
sys.modules["generate_third_party_licenses"] = gen
_spec.loader.exec_module(gen)


class TestThirdPartyLicenses(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_collect_packaged_license_and_notice_preserves_copyright(self) -> None:
        """Collect packaged LICENSE + NOTICE preserving copyright notices."""
        pkg_dir = self.root / "pkg_with_notices"
        pkg_dir.mkdir()
        (pkg_dir / "Cargo.toml").write_text("[package]\nname = 'licensed-crate'\n", encoding="utf-8")
        (pkg_dir / "LICENSE").write_text("Copyright (c) 2026 Alice <alice@example.com>\nMIT\n", encoding="utf-8")
        (pkg_dir / "NOTICE").write_text("Notice: Copyright (c) 2026 Bob Corporation\n", encoding="utf-8")

        manifest_outputs = [("node-plane-cli", {
            "crates": [{"package": {
                "name": "licensed-crate", "version": "1.0.0", "source": "registry+https://crates.io",
                "manifest_path": str(pkg_dir / "Cargo.toml"), "license": "MIT",
            }}],
            "licenses": [],
        })]

        crates_info, texts_by_hash = gen.collect_licenses(manifest_outputs, {})

        self.assertIn("licensed-crate@1.0.0", crates_info)
        collected_texts = [entry["text"] for entry in texts_by_hash.values()]
        self.assertTrue(any("Copyright (c) 2026 Alice" in text for text in collected_texts))
        self.assertTrue(any("Notice: Copyright (c) 2026 Bob" in text for text in collected_texts))
        for entry in texts_by_hash.values():
            self.assertIn("licensed-crate@1.0.0", entry["crates"])

    def test_graph_union_component_mappings_and_dedup(self) -> None:
        """Graph union component mappings across manifests and license dedup by SHA-256."""
        pkg_a = self.root / "crate_a"
        pkg_a.mkdir()
        (pkg_a / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
        (pkg_a / "LICENSE").write_text("Identical Shared License 2026\n", encoding="utf-8")

        pkg_b = self.root / "crate_b"
        pkg_b.mkdir()
        (pkg_b / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
        (pkg_b / "LICENSE").write_text("Identical Shared License 2026\n", encoding="utf-8")

        manifest_outputs = [
            ("node-plane-cli", {"crates": [{"package": {
                "name": "crate-shared", "version": "1.0.0", "source": "registry",
                "manifest_path": str(pkg_a / "Cargo.toml"),
            }}], "licenses": []}),
            ("node-plane-driver", {"crates": [
                {"package": {"name": "crate-shared", "version": "1.0.0", "source": "registry", "manifest_path": str(pkg_a / "Cargo.toml")}},
                {"package": {"name": "crate-b", "version": "2.0.0", "source": "registry", "manifest_path": str(pkg_b / "Cargo.toml")}},
            ], "licenses": []}),
        ]

        crates_info, texts_by_hash = gen.collect_licenses(manifest_outputs, {})

        # Component set union across CLI and Driver
        self.assertEqual(crates_info["crate-shared@1.0.0"]["components"], {"node-plane-cli", "node-plane-driver"})
        self.assertEqual(crates_info["crate-b@2.0.0"]["components"], {"node-plane-driver"})

        # Deduplication of identical text to a single SHA-256 entry referenced by both crates
        self.assertEqual(len(texts_by_hash), 1)
        entry = next(iter(texts_by_hash.values()))
        self.assertEqual(entry["crates"], {"crate-shared@1.0.0", "crate-b@2.0.0"})

    def test_missing_packaged_files_and_supplement_refuses(self) -> None:
        """Missing packaged legal files and absent supplement refuses with error."""
        pkg_dir = self.root / "empty_crate"
        pkg_dir.mkdir()
        (pkg_dir / "Cargo.toml").write_text("[package]\n", encoding="utf-8")

        manifest_outputs = [("node-plane-cli", {"crates": [{"package": {
            "name": "orphan-crate", "version": "0.1.0", "source": "registry",
            "manifest_path": str(pkg_dir / "Cargo.toml"),
        }}], "licenses": []})]

        with self.assertRaisesRegex(RuntimeError, "no packaged legal files and no upstream supplement"):
            gen.collect_licenses(manifest_outputs, {})

    def test_upstream_manifest_sha_mismatch_and_path_escape_refuses(self) -> None:
        """Upstream manifest SHA mismatch and path escape attempt refuse."""
        upstream_dir = self.root / "upstream"
        upstream_dir.mkdir()
        valid_file = upstream_dir / "supplement.txt"
        valid_file.write_text("license content", encoding="utf-8")

        # Path escape containment violation
        escape_spec = [{"crate": "c@1.0", "files": [{"path": "../outside.txt", "sha256": "abc"}]}]
        escape_file = upstream_dir / "escape.json"
        escape_file.write_text(json.dumps(escape_spec), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "escapes manifest directory"):
            gen.load_upstream_manifest(escape_file)

        # Hash mismatch violation
        mismatch_spec = [{"crate": "c@1.0", "files": [{"path": "supplement.txt", "sha256": "bad-hash"}]}]
        mismatch_file = upstream_dir / "mismatch.json"
        mismatch_file.write_text(json.dumps(mismatch_spec), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Hash mismatch"):
            gen.load_upstream_manifest(mismatch_file)

    def test_explicit_license_file_outside_package_refuses(self) -> None:
        """Explicit license_file escaping package directory refuses."""
        pkg_dir = self.root / "pkg"
        pkg_dir.mkdir()
        with self.assertRaisesRegex(ValueError, "Explicit legal file escapes package directory"):
            gen.scan_packaged_legal_files(pkg_dir, explicit_license_file="../../outside.txt")

    def vendored_tree(self) -> Path:
        tree = self.root / "proto" / "vendor" / "lib"
        (tree / "app").mkdir(parents=True)
        (tree / "LICENSE").write_text("Mozilla Public License Version 2.0\n", encoding="utf-8")
        (tree / "README.md").write_text("Vendored copy\n", encoding="utf-8")
        (tree / "app" / "a.proto").write_text("syntax = 'proto3';\n", encoding="utf-8")
        (tree / "b.proto").write_text("syntax = 'proto3';\n", encoding="utf-8")
        return tree

    def vendored_manifest(self, **changes: object) -> Path:
        entry = {"name": "Lib protobuf", "version": "v1", "spdx": "MPL-2.0",
                 "source": "https://example.com/lib", "path": "proto/vendor/lib",
                 "license": "LICENSE", "components": ["node-plane-agent"], **changes}
        path = self.root / "vendored.json"
        path.write_text(json.dumps([entry]), encoding="utf-8")
        return path

    def test_vendored_sources_list_every_file_and_carry_their_license(self) -> None:
        """Vendored files are listed individually and their license text is included."""
        self.vendored_tree()
        vendored = gen.load_vendored_manifest(self.vendored_manifest(), self.root)
        self.assertEqual(vendored[0]["files"], ["app/a.proto", "b.proto"])
        texts = {"h": {"text": vendored[0]["text"], "crates": {gen.vendored_key(vendored[0])}}}
        rendered = gen.render_third_party_licenses({}, texts, None, vendored)
        self.assertIn("Vendored Third-Party Source Files", rendered)
        self.assertIn("  Repository path: proto/vendor/lib\n  Files:\n    app/a.proto\n    b.proto\n", rendered)
        self.assertIn("  - Lib protobuf v1 [Components: node-plane-agent] [SPDX: MPL-2.0]", rendered)
        self.assertIn("Mozilla Public License Version 2.0", rendered)
        self.assertNotIn(str(self.root), rendered)
        # Without vendored sources the document keeps its previous shape.
        self.assertNotIn("Vendored", gen.render_third_party_licenses({}, {}, None))

    def test_vendored_paths_must_stay_in_the_repository_and_have_a_license(self) -> None:
        """Escaping paths and missing licenses refuse."""
        self.vendored_tree()
        with self.assertRaisesRegex(ValueError, "directory in the repository"):
            gen.load_vendored_manifest(self.vendored_manifest(path="../outside"), self.root)
        with self.assertRaisesRegex(FileNotFoundError, "license file missing"):
            gen.load_vendored_manifest(self.vendored_manifest(license="../../../LICENSE"), self.root)
        with self.assertRaisesRegex(FileNotFoundError, "license file missing"):
            gen.load_vendored_manifest(self.vendored_manifest(license="COPYING"), self.root)
        manifest = self.vendored_manifest()
        manifest.write_text(json.dumps([{"name": "x"}]), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing"):
            gen.load_vendored_manifest(manifest, self.root)

    @patch.object(gen, "generate_licenses", return_value="EXPECTED THIRD PARTY LICENSES\n")
    def test_cli_check_crlf_and_stale_file_refuses(self, _mock_gen: unittest.mock.MagicMock) -> None:
        """CLI --check CRLF compares correctly and stale file refuses."""
        out_file = self.root / "THIRD_PARTY_LICENSES"

        def run_cli() -> tuple[int, str, str]:
            out_buf, err_buf = io.StringIO(), io.StringIO()
            with patch("sys.stdout", out_buf), patch("sys.stderr", err_buf):
                code = gen.main(["--check", "--output", str(out_file)])
            return code, out_buf.getvalue(), err_buf.getvalue()

        # Matching text succeeds
        out_file.write_bytes(b"EXPECTED THIRD PARTY LICENSES\n")
        code, out, _ = run_cli()
        self.assertEqual(code, 0)
        self.assertIn("up to date", out)

        # CRLF difference is detected without normalization and refused
        out_file.write_bytes(b"EXPECTED THIRD PARTY LICENSES\r\n")
        code, _, err = run_cli()
        self.assertEqual(code, 1)
        self.assertIn("stale or out of date", err)

        # Matching original CRLF bytes must also succeed.
        _mock_gen.return_value = "EXPECTED THIRD PARTY LICENSES\r\n"
        code, _, _ = run_cli()
        self.assertEqual(code, 0)

        # Stale content refuses
        out_file.write_bytes(b"STALE PREVIOUS CONTENT\n")
        code, _, err = run_cli()
        self.assertEqual(code, 1)
        self.assertIn("stale or out of date", err)


if __name__ == "__main__":
    unittest.main()
