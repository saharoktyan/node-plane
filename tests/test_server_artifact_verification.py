import gzip
import hashlib
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SETUP_SCRIPT = REPO_ROOT / "scripts" / "setup_driver_agents.sh"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_server_artifacts.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import verify_server_artifacts


def create_tar_gz(path, entries):
    """Helper to build tar.gz archives with specific TarInfo attributes and payloads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        for item in entries:
            name = item["name"]
            payload = item.get("payload", b"#!/bin/sh\nexit 0\n")
            tarinfo = tarfile.TarInfo(name)
            tarinfo.size = len(payload)
            tarinfo.mode = item.get("mode", 0o755)
            tarinfo.type = item.get("type", tarfile.REGTYPE)
            if "linkname" in item:
                tarinfo.linkname = item["linkname"]
            archive.addfile(tarinfo, io.BytesIO(payload) if tarinfo.isreg() else None)


def file_sha256(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


class ServerArtifactVerificationUnitTests(unittest.TestCase):
    """Unit tests for verify_server_artifacts.py CLI and library functions."""

    def test_positive_suffixed_layout_with_legal_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            archive_path = tmp / "driver.tar.gz"
            out_bin = tmp / "extracted" / "node-plane-driver"
            binary_payload = b"#!/bin/sh\necho driver-ok\nexit 0\n"
            create_tar_gz(
                archive_path,
                [
                    {"name": "node-plane-driver-linux-amd64", "payload": binary_payload, "mode": 0o755},
                    {"name": "LICENSE", "payload": b"MIT License\n", "mode": 0o644},
                    {"name": "THIRD_PARTY_LICENSES", "payload": b"Third party legal\n", "mode": 0o644},
                ],
            )
            digest = file_sha256(archive_path)
            sums_path = tmp / "SHA256SUMS.txt"
            sums_path.write_text(f"{digest}  node-plane-driver-linux-amd64.tar.gz\n", encoding="utf-8")

            # Test parse-checksum CLI
            res_parse = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "parse-checksum",
                    "--sums-file",
                    str(sums_path),
                    "--asset-name",
                    "node-plane-driver-linux-amd64.tar.gz",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            self.assertEqual(res_parse.stdout.strip(), digest)

            # Test verify-and-extract CLI
            res_extract = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "verify-and-extract",
                    "--component",
                    "driver",
                    "--archive",
                    str(archive_path),
                    "--expected-sha256",
                    digest,
                    "--output",
                    str(out_bin),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            self.assertTrue(out_bin.is_file())
            self.assertEqual(out_bin.read_bytes(), binary_payload)
            self.assertTrue(out_bin.stat().st_mode & stat.S_IXUSR)
            # Ensure LICENSE files were not extracted to destination
            self.assertFalse((tmp / "extracted" / "LICENSE").exists())

    def test_positive_unsuffixed_layout_with_legal_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            archive_path = tmp / "agent.tar.gz"
            out_bin = tmp / "stage" / "node-plane-agent"
            binary_payload = b"#!/bin/sh\necho agent-ok\nexit 0\n"
            create_tar_gz(
                archive_path,
                [
                    {"name": "node-plane-agent", "payload": binary_payload, "mode": 0o755},
                    {"name": "LICENSE", "payload": b"Apache-2.0\n", "mode": 0o644},
                ],
            )
            digest = file_sha256(archive_path)
            res = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "verify-and-extract",
                    "--component",
                    "agent",
                    "--archive",
                    str(archive_path),
                    "--expected-sha256",
                    digest,
                    "--output",
                    str(out_bin),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            self.assertEqual(res.returncode, 0)
            self.assertTrue(out_bin.is_file())
            self.assertEqual(out_bin.read_bytes(), binary_payload)

    def test_wrong_checksum_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            archive_path = tmp / "driver.tar.gz"
            out_bin = tmp / "out" / "bin"
            create_tar_gz(
                archive_path,
                [{"name": "node-plane-driver-linux-amd64", "payload": b"data", "mode": 0o755}],
            )
            wrong_digest = "0" * 64
            res = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "verify-and-extract",
                    "--component",
                    "driver",
                    "--archive",
                    str(archive_path),
                    "--expected-sha256",
                    wrong_digest,
                    "--output",
                    str(out_bin),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("digest mismatch", res.stderr)
            self.assertFalse(out_bin.exists())

    def test_missing_sums_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            sums_path = tmp / "SHA256SUMS.txt"
            sums_path.write_text(f"{'a' * 64}  other-asset.tar.gz\n", encoding="utf-8")
            res = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "parse-checksum",
                    "--sums-file",
                    str(sums_path),
                    "--asset-name",
                    "node-plane-driver-linux-amd64.tar.gz",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Missing SHA-256 entry", res.stderr)

    def test_duplicate_sums_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            sums_path = tmp / "SHA256SUMS.txt"
            sums_path.write_text(
                f"{'a' * 64}  node-plane-driver-linux-amd64.tar.gz\n"
                f"{'b' * 64}  node-plane-driver-linux-amd64.tar.gz\n",
                encoding="utf-8",
            )
            res = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "parse-checksum",
                    "--sums-file",
                    str(sums_path),
                    "--asset-name",
                    "node-plane-driver-linux-amd64.tar.gz",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Duplicate SHA-256 entry", res.stderr)

    def test_malformed_sums_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            # Test non-hex checksum
            bad_sums = tmp / "bad.txt"
            bad_sums.write_text(f"{'g' * 64}  node-plane-driver-linux-amd64.tar.gz\n", encoding="utf-8")
            res = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "parse-checksum",
                    "--sums-file",
                    str(bad_sums),
                    "--asset-name",
                    "node-plane-driver-linux-amd64.tar.gz",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Malformed checksum entry", res.stderr)

            # Test HTML error page
            html_sums = tmp / "html.txt"
            html_sums.write_text("<!DOCTYPE html><html><body>404 Not Found</body></html>\n", encoding="utf-8")
            res_html = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "parse-checksum",
                    "--sums-file",
                    str(html_sums),
                    "--asset-name",
                    "node-plane-driver-linux-amd64.tar.gz",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(res_html.returncode, 0)
            self.assertIn("Malformed checksum entry", res_html.stderr)

    def test_malformed_archive_format_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            bad_archive = tmp / "driver.tar.gz"
            bad_archive.write_bytes(b"\x7fELF\x02\x01\x01\x00not a gzip archive")
            digest = file_sha256(bad_archive)
            out_bin = tmp / "out" / "bin"
            res = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "verify-and-extract",
                    "--component",
                    "driver",
                    "--archive",
                    str(bad_archive),
                    "--expected-sha256",
                    digest,
                    "--output",
                    str(out_bin),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Invalid tar.gz", res.stderr)
            self.assertFalse(out_bin.exists())

    def test_wrong_binary_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            archive_path = tmp / "driver.tar.gz"
            out_bin = tmp / "out" / "bin"
            create_tar_gz(
                archive_path,
                [{"name": "node-plane-wrong", "payload": b"echo wrong\n", "mode": 0o755}],
            )
            digest = file_sha256(archive_path)
            res = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "verify-and-extract",
                    "--component",
                    "driver",
                    "--archive",
                    str(archive_path),
                    "--expected-sha256",
                    digest,
                    "--output",
                    str(out_bin),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Unexpected entry in server archive: node-plane-wrong", res.stderr)
            self.assertFalse(out_bin.exists())

    def test_both_binary_aliases_rejected_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            archive_path = tmp / "driver.tar.gz"
            out_bin = tmp / "out" / "bin"
            create_tar_gz(
                archive_path,
                [
                    {"name": "node-plane-driver-linux-amd64", "payload": b"bin1", "mode": 0o755},
                    {"name": "node-plane-driver", "payload": b"bin2", "mode": 0o755},
                ],
            )
            digest = file_sha256(archive_path)
            res = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "verify-and-extract",
                    "--component",
                    "driver",
                    "--archive",
                    str(archive_path),
                    "--expected-sha256",
                    digest,
                    "--output",
                    str(out_bin),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Both binary aliases present", res.stderr)
            self.assertFalse(out_bin.exists())

    def test_duplicate_member_names_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            archive_path = tmp / "dup.tar.gz"
            out_bin = tmp / "out" / "bin"
            with tarfile.open(archive_path, "w:gz") as archive:
                for _ in range(2):
                    info = tarfile.TarInfo("node-plane-driver-linux-amd64")
                    info.size = 10
                    info.mode = 0o755
                    archive.addfile(info, io.BytesIO(b"1234567890"))
            digest = file_sha256(archive_path)
            res = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "verify-and-extract",
                    "--component",
                    "driver",
                    "--archive",
                    str(archive_path),
                    "--expected-sha256",
                    digest,
                    "--output",
                    str(out_bin),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Duplicate archive entry rejected", res.stderr)
            self.assertFalse(out_bin.exists())

    def test_path_traversal_and_absolute_and_nested_rejected(self):
        for bad_path, expected_err in [
            ("/usr/local/bin/node-plane-driver", "Absolute path"),
            ("../node-plane-driver", "Path traversal"),
            ("sub/node-plane-driver", "Nested or unsafe path"),
        ]:
            with self.subTest(bad_path=bad_path):
                with tempfile.TemporaryDirectory() as temp_dir:
                    tmp = pathlib.Path(temp_dir)
                    archive_path = tmp / "bad.tar.gz"
                    out_bin = tmp / "out" / "bin"
                    create_tar_gz(
                        archive_path,
                        [{"name": bad_path, "payload": b"code", "mode": 0o755}],
                    )
                    digest = file_sha256(archive_path)
                    res = subprocess.run(
                        [
                            sys.executable,
                            str(VERIFY_SCRIPT),
                            "verify-and-extract",
                            "--component",
                            "driver",
                            "--archive",
                            str(archive_path),
                            "--expected-sha256",
                            digest,
                            "--output",
                            str(out_bin),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertNotEqual(res.returncode, 0)
                    self.assertIn(expected_err, res.stderr)
                    self.assertFalse(out_bin.exists())

    def test_symlinks_and_hardlinks_rejected(self):
        for link_type, type_name in [(tarfile.SYMTYPE, "symlink"), (tarfile.LNKTYPE, "hardlink")]:
            with self.subTest(type_name=type_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    tmp = pathlib.Path(temp_dir)
                    archive_path = tmp / "link.tar.gz"
                    out_bin = tmp / "out" / "bin"
                    create_tar_gz(
                        archive_path,
                        [
                            {
                                "name": "node-plane-driver-linux-amd64",
                                "type": link_type,
                                "linkname": "/etc/passwd",
                                "payload": b"",
                                "mode": 0o777,
                            }
                        ],
                    )
                    digest = file_sha256(archive_path)
                    res = subprocess.run(
                        [
                            sys.executable,
                            str(VERIFY_SCRIPT),
                            "verify-and-extract",
                            "--component",
                            "driver",
                            "--archive",
                            str(archive_path),
                            "--expected-sha256",
                            digest,
                            "--output",
                            str(out_bin),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertNotEqual(res.returncode, 0)
                    self.assertIn("not a regular file", res.stderr)
                    self.assertFalse(out_bin.exists())

    def test_non_executable_binary_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            archive_path = tmp / "nonexec.tar.gz"
            out_bin = tmp / "out" / "bin"
            create_tar_gz(
                archive_path,
                [{"name": "node-plane-driver-linux-amd64", "payload": b"code", "mode": 0o644}],
            )
            digest = file_sha256(archive_path)
            res = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "verify-and-extract",
                    "--component",
                    "driver",
                    "--archive",
                    str(archive_path),
                    "--expected-sha256",
                    digest,
                    "--output",
                    str(out_bin),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("not executable", res.stderr)
            self.assertFalse(out_bin.exists())

    def test_empty_binary_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            archive_path = tmp / "empty.tar.gz"
            out_bin = tmp / "out" / "bin"
            create_tar_gz(
                archive_path,
                [{"name": "node-plane-driver-linux-amd64", "payload": b"", "mode": 0o755}],
            )
            digest = file_sha256(archive_path)
            res = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "verify-and-extract",
                    "--component",
                    "driver",
                    "--archive",
                    str(archive_path),
                    "--expected-sha256",
                    digest,
                    "--output",
                    str(out_bin),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("empty", res.stderr)
            self.assertFalse(out_bin.exists())

    def test_oversize_archive_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            archive_path = tmp / "large.tar.gz"
            out_bin = tmp / "out" / "bin"
            large_size = 64 * 1024
            with tarfile.open(archive_path, "w:gz") as archive:
                info = tarfile.TarInfo("node-plane-driver-linux-amd64")
                info.size = large_size
                info.mode = 0o755
                archive.addfile(info, io.BytesIO(b"\x00" * large_size))

            digest = file_sha256(archive_path)
            with patch.object(verify_server_artifacts, 'MAX_ARCHIVE_SIZE', archive_path.stat().st_size - 1):
                with self.assertRaisesRegex(ValueError, 'Archive size exceeds'):
                    verify_server_artifacts.verify_server_archive('driver', archive_path, digest, out_bin)
            with patch.object(verify_server_artifacts, 'MAX_PAYLOAD_SIZE', large_size - 1):
                with self.assertRaisesRegex(ValueError, 'exceeds size limit'):
                    verify_server_artifacts.verify_server_archive('driver', archive_path, digest, out_bin)
            self.assertFalse(out_bin.exists())

    def test_decompression_bomb_exceeding_max_tar_size_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            archive_path = tmp / "bomb.tar.gz"
            out_bin = tmp / "out" / "bin"

            archive_path.write_bytes(gzip.compress(b'\0' * 8192))
            digest = file_sha256(archive_path)
            with patch.object(verify_server_artifacts, 'MAX_TAR_SIZE', 4096), \
                    patch.object(verify_server_artifacts.tarfile, 'open', side_effect=AssertionError('tar parser reached')):
                with self.assertRaisesRegex(ValueError, 'Expanded tar stream'):
                    verify_server_artifacts.verify_server_archive('driver', archive_path, digest, out_bin)
            self.assertFalse(out_bin.exists())

    def test_max_entries_exceeded_rejected(self):
        max_entries = verify_server_artifacts.MAX_ENTRIES
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            archive_path = tmp / "too_many_entries.tar.gz"
            out_bin = tmp / "out" / "bin"

            entries = [
                {"name": "node-plane-driver-linux-amd64", "payload": b"#!/bin/sh\nexit 0\n", "mode": 0o755}
            ]
            for i in range(max_entries):
                entries.append({"name": f"extra_file_{i}", "payload": b"data\n", "mode": 0o644})

            create_tar_gz(archive_path, entries)
            digest = file_sha256(archive_path)

            res = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "verify-and-extract",
                    "--component",
                    "driver",
                    "--archive",
                    str(archive_path),
                    "--expected-sha256",
                    digest,
                    "--output",
                    str(out_bin),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("too many entries", res.stderr)
            self.assertFalse(out_bin.exists())

    def test_atomic_temp_extraction_does_not_follow_predictable_symlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            out_bin = tmp / "extracted" / "node-plane-driver"
            out_bin.parent.mkdir(parents=True, exist_ok=True)

            decoy = tmp / "sensitive_decoy.txt"
            decoy_content = "ORIGINAL_DECOY_CONTENT_DO_NOT_OVERWRITE"
            decoy.write_text(decoy_content, encoding="utf-8")

            # Pre-plant a symlink at the old predictable temp path .<bin>.tmp.<pid>
            predictable_tmp = out_bin.with_name(f".{out_bin.name}.tmp.{os.getpid()}")
            predictable_tmp.symlink_to(decoy)

            archive_path = tmp / "driver.tar.gz"
            binary_payload = b"#!/bin/sh\necho secure-extracted\nexit 0\n"
            create_tar_gz(
                archive_path,
                [{"name": "node-plane-driver-linux-amd64", "payload": binary_payload, "mode": 0o755}],
            )
            digest = file_sha256(archive_path)

            # Direct function verification within this process where os.getpid() matches the preplanted symlink
            result = verify_server_artifacts.verify_server_archive(
                "driver", str(archive_path), digest, output_path=str(out_bin)
            )
            self.assertEqual(result["sha256"], digest)
            self.assertTrue(out_bin.is_file())
            self.assertEqual(out_bin.read_bytes(), binary_payload)
            # Decoy must NOT have been overwritten
            self.assertEqual(decoy.read_text(encoding="utf-8"), decoy_content)


class ServerArtifactSetupIntegrationTests(unittest.TestCase):
    """Integration security tests invoking scripts/setup_driver_agents.sh."""

    def test_http_custom_url_denial(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            app = tmp / "current"
            shared = tmp / "shared"
            fake_bin = tmp / "bin"
            for d in (app, shared, fake_bin):
                d.mkdir(parents=True)
            (shared / ".env").write_text("BOT_TOKEN=test\n")
            res = subprocess.run(
                [str(SETUP_SCRIPT), "--dry-run", "--bin-source", "release"],
                env={
                    **os.environ,
                    "NODE_PLANE_APP_DIR": str(app),
                    "NODE_PLANE_SHARED_DIR": str(shared),
                    "NODE_PLANE_DRIVER_BIN_URL": "http://insecure.example.test/driver.tar.gz",
                    "NODE_PLANE_DRIVER_SHA256": "0" * 64,
                },
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("HTTPS required", res.stderr)

    def test_custom_url_without_sha256_denial(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            app = tmp / "current"
            shared = tmp / "shared"
            for d in (app, shared):
                d.mkdir(parents=True)
            (shared / ".env").write_text("BOT_TOKEN=test\n")
            res = subprocess.run(
                [str(SETUP_SCRIPT), "--dry-run", "--bin-source", "release"],
                env={
                    **os.environ,
                    "NODE_PLANE_APP_DIR": str(app),
                    "NODE_PLANE_SHARED_DIR": str(shared),
                    "NODE_PLANE_DRIVER_BIN_URL": "https://secure.example.test/driver.tar.gz",
                    # NODE_PLANE_DRIVER_SHA256 missing
                },
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("requires matching explicit NODE_PLANE_DRIVER_SHA256", res.stderr)

    def test_insecure_download_url_rejected_even_with_mocked_curl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            app = tmp / "current"
            shared = tmp / "shared"
            fake_bin = tmp / "bin"
            for d in (app, shared, fake_bin):
                d.mkdir(parents=True)
            (shared / ".env").write_text("BOT_TOKEN=test\n")

            # Mock curl that blindly succeeds and touches output if called
            mock_curl = fake_bin / "curl"
            mock_curl.write_text(
                "#!/bin/sh\n"
                "out=''\n"
                "for arg in \"$@\"; do\n"
                "  if [ \"$previous\" = '-o' ]; then out=\"$arg\"; fi\n"
                "  previous=\"$arg\"\n"
                "done\n"
                "if [ -n \"$out\" ]; then touch \"$out\"; fi\n"
                "exit 0\n"
            )
            mock_curl.chmod(0o755)

            res = subprocess.run(
                [str(SETUP_SCRIPT), "--skip-driver", "--skip-agents", "--bin-source", "release"],
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "NODE_PLANE_APP_DIR": str(app),
                    "NODE_PLANE_SHARED_DIR": str(shared),
                    "NODE_PLANE_DRIVER_BIN_URL": "http://evil.example.test/driver.tar.gz",
                    "NODE_PLANE_DRIVER_SHA256": "1" * 64,
                },
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("HTTPS required", res.stderr)

    def test_installer_rejection_without_sudo_ssh_install(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = pathlib.Path(temp_dir)
            app = tmp / "current"
            shared = tmp / "shared"
            fake_bin = tmp / "bin"
            for d in (app, shared, fake_bin):
                d.mkdir(parents=True)
            (shared / ".env").write_text("BOT_TOKEN=test\n")

            # Create bad driver archive
            driver_archive = tmp / "driver.tar.gz"
            agent_archive = tmp / "agent.tar.gz"
            create_tar_gz(
                driver_archive,
                [{"name": "node-plane-driver-linux-amd64", "payload": b"driver", "mode": 0o755}],
            )
            create_tar_gz(
                agent_archive,
                [{"name": "node-plane-agent-linux-amd64", "payload": b"agent", "mode": 0o755}],
            )

            # Checksums file has WRONG checksum for driver
            sums_file = tmp / "SHA256SUMS.txt"
            sums_file.write_text(
                f"{'0' * 64}  node-plane-driver-linux-amd64.tar.gz\n"
                f"{file_sha256(agent_archive)}  node-plane-agent-linux-amd64.tar.gz\n",
                encoding="utf-8",
            )

            # Mock curl to return the archives and sums
            curl = fake_bin / "curl"
            curl.write_text(
                f"""#!/bin/sh
out=''
for arg in "$@"; do
  if [ "$previous" = '-o' ]; then out="$arg"; fi
  previous="$arg"
done
case "$*" in
  *SHA256SUMS.txt*) cp "{sums_file}" "$out"; exit 0;;
  *driver*) cp "{driver_archive}" "$out"; exit 0;;
  *agent*) cp "{agent_archive}" "$out"; exit 0;;
  *) exit 1;;
esac
""",
                encoding="utf-8",
            )
            curl.chmod(0o755)

            # Sudo, ssh, and scp fail loudly with exit code 99 if invoked!
            for cmd in ("sudo", "ssh", "scp"):
                p = fake_bin / cmd
                p.write_text(
                    f"#!/bin/sh\necho 'FORBIDDEN PRIVILEGED CALL: {cmd}' >&2\nexit 99\n",
                    encoding="utf-8",
                )
                p.chmod(0o755)

            res = subprocess.run(
                [str(SETUP_SCRIPT), "--bin-source", "release"],
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "NODE_PLANE_APP_DIR": str(app),
                    "NODE_PLANE_SHARED_DIR": str(shared),
                },
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertNotEqual(res.returncode, 99)
            self.assertNotIn("FORBIDDEN PRIVILEGED CALL", res.stderr)
            self.assertIn("RELEASE_BINARY_UNAVAILABLE", res.stderr)


if __name__ == "__main__":
    unittest.main()
