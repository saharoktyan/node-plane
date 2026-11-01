"""Regression tests for release publisher validation."""
import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / 'app') not in sys.path:
    sys.path.insert(0, str(ROOT / 'app'))

import scripts.validate_release as validate_release
from services.release_versions import CHANNEL_BRANCHES, parse_release_metadata


def create_release_tree(
    root: Path,
    *,
    version: str = "0.5.0",
    channel: str = "stable",
    pyproject_version: str | None = None,
    cargo_version: str | None = None,
) -> None:
    (root / "VERSION").write_text(f"{version}\n")
    (root / "RELEASE_CHANNEL").write_text(f"{channel}\n")
    py_ver = pyproject_version if pyproject_version is not None else version
    (root / "pyproject.toml").write_text(f'[project]\nversion = "{py_ver}"\n')
    cargo_ver = cargo_version if cargo_version is not None else version
    cargo_path = root / "rust" / "node-plane-cli" / "Cargo.toml"
    cargo_path.parent.mkdir(parents=True, exist_ok=True)
    cargo_path.write_text(f'[package]\nversion = "{cargo_ver}"\n')


def init_git_repo(root: Path, branch: str = "main") -> str:
    subprocess.run(["git", "init", "-b", branch, str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Validation Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "validation@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "Initial commit"], check=True, capture_output=True)
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


class ReleaseValidationTests(unittest.TestCase):
    def setUp(self):
        self.commit = "a" * 40

    def test_valid_dev_alpha_releases(self):
        for tag, version in [("v0.5.0-alpha.1", "0.5.0-alpha.1"), ("v1.0.0-alpha.12", "1.0.0-alpha.12")]:
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                create_release_tree(root, version=version, channel="dev")
                channel = validate_release.validate(root, tag, self.commit)
                self.assertEqual(channel, "dev")

    def test_valid_beta_and_rc_releases(self):
        cases = [
            ("v0.5.0-beta.1", "0.5.0-beta.1"),
            ("v0.5.0-beta.12", "0.5.0-beta.12"),
            ("v0.5.0-rc.1", "0.5.0-rc.1"),
            ("v1.2.0-rc.5", "1.2.0-rc.5"),
        ]
        for tag, version in cases:
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                create_release_tree(root, version=version, channel="beta")
                channel = validate_release.validate(root, tag, self.commit)
                self.assertEqual(channel, "beta")

    def test_valid_stable_releases(self):
        for tag, version in [("v0.5.0", "0.5.0"), ("v1.0.0", "1.0.0"), ("v2.14.3", "2.14.3")]:
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                create_release_tree(root, version=version, channel="stable")
                channel = validate_release.validate(root, tag, self.commit)
                self.assertEqual(channel, "stable")

    def test_reject_wrong_release_channel(self):
        mismatches = [
            ("v0.5.0-alpha.1", "0.5.0-alpha.1", "beta"),
            ("v0.5.0-alpha.1", "0.5.0-alpha.1", "stable"),
            ("v0.5.0-beta.1", "0.5.0-beta.1", "dev"),
            ("v0.5.0-beta.1", "0.5.0-beta.1", "stable"),
            ("v0.5.0-rc.1", "0.5.0-rc.1", "stable"),
            ("v0.5.0", "0.5.0", "dev"),
            ("v0.5.0", "0.5.0", "beta"),
        ]
        for tag, version, wrong_channel in mismatches:
            with self.subTest(tag=tag, wrong_channel=wrong_channel), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                create_release_tree(root, version=version, channel=wrong_channel)
                with self.assertRaises(ValueError) as cm:
                    validate_release.validate(root, tag, self.commit)
                self.assertEqual(str(cm.exception), "RELEASE_CHANNEL must match the tag stage")

    def test_reject_version_file_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            create_release_tree(root, version="0.5.1", channel="stable")
            with self.assertRaises(ValueError) as cm:
                validate_release.validate(root, "v0.5.0", self.commit)
            self.assertEqual(str(cm.exception), "Release tag must match VERSION")

    def test_reject_pyproject_version_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            create_release_tree(root, version="0.5.0", channel="stable", pyproject_version="0.5.1")
            with self.assertRaises(ValueError) as cm:
                validate_release.validate(root, "v0.5.0", self.commit)
            self.assertEqual(str(cm.exception), "pyproject.toml version must match VERSION")

    def test_reject_cargo_version_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            create_release_tree(root, version="0.5.0", channel="stable", cargo_version="0.5.1")
            with self.assertRaises(ValueError) as cm:
                validate_release.validate(root, "v0.5.0", self.commit)
            self.assertEqual(str(cm.exception), "rust/node-plane-cli/Cargo.toml version must match VERSION")

    def test_reject_invalid_full_commit(self):
        invalid_commits = [
            "a" * 39,
            "a" * 41,
            "1234567",
            "",
            "g" * 40,
            "A" * 40,
            " " + "a" * 39,
            "a" * 40 + "\n",
            "a" * 20 + " " + "a" * 19,
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            create_release_tree(root, version="0.5.0", channel="stable")
            for invalid in invalid_commits:
                with self.subTest(commit=invalid), self.assertRaises(ValueError) as cm:
                    validate_release.validate(root, "v0.5.0", invalid)
                self.assertEqual(str(cm.exception), "A full release commit is required")

    def test_reject_archive_and_unsupported_tags(self):
        unsupported_tags = [
            "legacy/v1.0.0-beta.11",
            "0.5.0",
            "v01.0.0",
            "v0.5.0-beta.01",
            "v0.5.0-preview.1",
            "v0.5.0-beta.1+build",
            "v0.5.0-nightly.1",
            "HEAD",
            "stable",
            "refs/tags/v0.5.0",
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            create_release_tree(root, version="0.5.0", channel="stable")
            for tag in unsupported_tags:
                with self.subTest(tag=tag), self.assertRaises(ValueError) as cm:
                    validate_release.validate(root, tag, self.commit)
                self.assertEqual(str(cm.exception), "Invalid release tag")

    def test_check_branch_rejects_checkout_commit_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            create_release_tree(root, version="0.5.0", channel="stable")
            actual_commit = init_git_repo(root, branch="main")
            subprocess.run(["git", "-C", str(root), "update-ref", "refs/remotes/origin/main", actual_commit], check=True)

            mismatched_commit = "0" * 40
            self.assertNotEqual(actual_commit, mismatched_commit)
            with self.assertRaises(ValueError) as cm:
                validate_release.validate(root, "v0.5.0", mismatched_commit, check_branch=True)
            self.assertEqual(str(cm.exception), "Release commit must match the checkout")

    def test_check_branch_rejects_commits_not_reachable_from_origin_ref(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            create_release_tree(root, version="0.5.0", channel="stable")
            c1 = init_git_repo(root, branch="main")

            # 1. Missing remote ref: origin/main does not exist at all
            with self.assertRaises(subprocess.CalledProcessError):
                validate_release.validate(root, "v0.5.0", c1, check_branch=True)

            # 2. Remote ref exists but commit is not reachable (c2 committed locally, origin/main stays at c1)
            subprocess.run(["git", "-C", str(root), "update-ref", "refs/remotes/origin/main", c1], check=True)
            (root / "extra.txt").write_text("local advance\n")
            subprocess.run(["git", "-C", str(root), "add", "extra.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "advance local HEAD"], check=True, capture_output=True)
            c2 = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()

            with self.assertRaises(subprocess.CalledProcessError):
                validate_release.validate(root, "v0.5.0", c2, check_branch=True)

            # 3. Diverged branch: commit on feature branch is not an ancestor of origin/main
            subprocess.run(["git", "-C", str(root), "checkout", "-b", "feature", c1], check=True, capture_output=True)
            (root / "feature.txt").write_text("divergent work\n")
            subprocess.run(["git", "-C", str(root), "add", "feature.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "feature commit"], check=True, capture_output=True)
            c_feature = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()

            with self.assertRaises(subprocess.CalledProcessError):
                validate_release.validate(root, "v0.5.0", c_feature, check_branch=True)

            # 4. Cross-channel ref mismatch: c1 is in origin/main, but release tag is dev (which checks origin/dev)
            subprocess.run(["git", "-C", str(root), "checkout", c1], check=True, capture_output=True)
            create_release_tree(root, version="0.5.0-alpha.1", channel="dev")
            subprocess.run(["git", "-C", str(root), "commit", "-am", "dev release"], check=True, capture_output=True)
            c_dev = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
            # origin/main has c_dev, but origin/dev does not exist
            subprocess.run(["git", "-C", str(root), "update-ref", "refs/remotes/origin/main", c_dev], check=True)

            with self.assertRaises(subprocess.CalledProcessError):
                validate_release.validate(root, "v0.5.0-alpha.1", c_dev, check_branch=True)

    def test_check_branch_accepts_correct_ancestry_across_channels(self):
        scenarios = [
            ("dev", "v0.5.0-alpha.1", "0.5.0-alpha.1", "dev"),
            ("beta", "v0.5.0-beta.2", "0.5.0-beta.2", "beta"),
            ("beta", "v0.5.0-rc.1", "0.5.0-rc.1", "beta"),
            ("stable", "v0.5.0", "0.5.0", "main"),
        ]
        for channel, tag, version, branch_name in scenarios:
            with self.subTest(channel=channel, tag=tag), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                create_release_tree(root, version=version, channel=channel)
                commit_sha = init_git_repo(root, branch=branch_name)
                remote_ref = f"refs/remotes/origin/{CHANNEL_BRANCHES[channel]}"
                subprocess.run(["git", "-C", str(root), "update-ref", remote_ref, commit_sha], check=True)

                res = validate_release.validate(root, tag, commit_sha, check_branch=True)
                self.assertEqual(res, channel)

    def test_check_branch_accepts_commit_as_strict_ancestor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            create_release_tree(root, version="0.5.0", channel="stable")
            c1 = init_git_repo(root, branch="main")

            (root / "subsequent.txt").write_text("commit 2\n")
            subprocess.run(["git", "-C", str(root), "add", "subsequent.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "second commit"], check=True, capture_output=True)
            c2 = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()

            # origin/main has advanced to c2
            subprocess.run(["git", "-C", str(root), "update-ref", "refs/remotes/origin/main", c2], check=True)

            # Checkout c1 (detached HEAD)
            subprocess.run(["git", "-C", str(root), "checkout", c1], check=True, capture_output=True)
            self.assertEqual(
                subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
                c1,
            )

            # c1 is an ancestor of origin/main (c2), and HEAD matches c1
            res = validate_release.validate(root, "v0.5.0", c1, check_branch=True)
            self.assertEqual(res, "stable")

    def test_cli_metadata_generation_and_verification(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            create_release_tree(root, version="0.5.0", channel="stable")
            meta_file = root / "RELEASE_METADATA.txt"

            args = ["validate_release.py", "--tag", "v0.5.0", "--commit", self.commit, "--metadata", str(meta_file)]
            with patch.object(validate_release, "ROOT", root), patch("sys.argv", args):
                validate_release.main()

            self.assertTrue(meta_file.exists())
            raw_metadata = meta_file.read_bytes()
            parsed = parse_release_metadata(raw_metadata, tag="v0.5.0", channel="stable", commit=self.commit)
            self.assertEqual(parsed["format"], "2")
            self.assertEqual(parsed["channel"], "stable")
            self.assertEqual(parsed["tag"], "v0.5.0")
            self.assertEqual(parsed["version"], "0.5.0")
            self.assertEqual(parsed["commit"], self.commit)
            self.assertIn("built_at", parsed)

            # Verify with CLI --verify-metadata
            verify_args = [
                "validate_release.py",
                "--tag",
                "v0.5.0",
                "--commit",
                self.commit,
                "--verify-metadata",
                str(meta_file),
            ]
            with patch.object(validate_release, "ROOT", root), patch("sys.argv", verify_args):
                validate_release.main()

    def test_cli_verify_metadata_rejects_corrupted_data(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            create_release_tree(root, version="0.5.0", channel="stable")
            meta_file = root / "RELEASE_METADATA.txt"
            # Metadata has mismatched commit
            mismatched_commit = "b" * 40
            meta_file.write_text(
                f"format=2\nchannel=stable\ntag=v0.5.0\nversion=0.5.0\ncommit={mismatched_commit}\n"
            )

            args = [
                "validate_release.py",
                "--tag",
                "v0.5.0",
                "--commit",
                self.commit,
                "--verify-metadata",
                str(meta_file),
            ]
            stderr = io.StringIO()
            with patch.object(validate_release, "ROOT", root), patch("sys.argv", args), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as cm:
                    validate_release.main()
                self.assertEqual(cm.exception.code, 1)
            self.assertIn("Release validation failed: Release metadata does not match channel, tag or commit", stderr.getvalue())

    def test_cli_check_local_branch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            create_release_tree(root, version="0.5.0-alpha.1", channel="dev")
            commit_sha = init_git_repo(root, branch="dev")

            # Correct branch: dev branch matches dev channel
            success_args = [
                "validate_release.py",
                "--tag",
                "v0.5.0-alpha.1",
                "--commit",
                commit_sha,
                "--check-local-branch",
            ]
            with patch.object(validate_release, "ROOT", root), patch("sys.argv", success_args):
                validate_release.main()

            # Mismatched branch: stable release expects main, but branch is dev
            create_release_tree(root, version="0.5.0", channel="stable")
            fail_args = [
                "validate_release.py",
                "--tag",
                "v0.5.0",
                "--commit",
                commit_sha,
                "--check-local-branch",
            ]
            stderr = io.StringIO()
            with patch.object(validate_release, "ROOT", root), patch("sys.argv", fail_args), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as cm:
                    validate_release.main()
                self.assertEqual(cm.exception.code, 1)
            self.assertIn("Release validation failed: Release must be prepared on its matching source branch", stderr.getvalue())

    def test_cli_exits_with_error_on_validation_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            create_release_tree(root, version="0.5.1", channel="stable")
            args = ["validate_release.py", "--tag", "v0.5.0", "--commit", self.commit]
            stderr = io.StringIO()
            with patch.object(validate_release, "ROOT", root), patch("sys.argv", args), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as cm:
                    validate_release.main()
                self.assertEqual(cm.exception.code, 1)
            self.assertIn("Release validation failed: Release tag must match VERSION", stderr.getvalue())

    def test_cli_isolated_script_execution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            create_release_tree(root, version="0.5.0", channel="stable")
            (root / "scripts").mkdir()
            shutil.copyfile(ROOT / "scripts" / "validate_release.py", root / "scripts" / "validate_release.py")
            (root / "app" / "services").mkdir(parents=True)
            (root / "app" / "services" / "__init__.py").touch()
            shutil.copyfile(ROOT / "app" / "services" / "release_versions.py", root / "app" / "services" / "release_versions.py")

            meta_file = root / "METADATA.txt"
            res = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts" / "validate_release.py"),
                    "--tag",
                    "v0.5.0",
                    "--commit",
                    self.commit,
                    "--metadata",
                    str(meta_file),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=f"CLI failed with stderr: {res.stderr}")
            self.assertTrue(meta_file.exists())
            self.assertIn(f"commit={self.commit}", meta_file.read_text())


if __name__ == "__main__":
    unittest.main()
