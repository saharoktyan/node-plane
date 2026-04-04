from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

TESTS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
APP_ROOT = os.path.join(REPO_ROOT, "app")
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class ReleaseCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = self.tmpdir.name
        install_root = os.path.join(base, "node-plane")
        source_root = os.path.join(base, "node-plane-src")
        current_root = os.path.join(install_root, "current")
        releases_dir = os.path.join(install_root, "releases")
        os.makedirs(releases_dir, exist_ok=True)
        os.makedirs(source_root, exist_ok=True)
        os.environ["NODE_PLANE_BASE_DIR"] = install_root
        os.environ["NODE_PLANE_APP_DIR"] = current_root
        os.environ["NODE_PLANE_SHARED_DIR"] = os.path.join(install_root, "shared")
        os.environ["NODE_PLANE_SOURCE_DIR"] = source_root
        os.environ["NODE_PLANE_INSTALL_MODE"] = "simple"
        import config
        import services.release_cleanup as release_cleanup

        self.config = importlib.reload(config)
        self.release_cleanup = importlib.reload(release_cleanup)

        self.release_a = os.path.join(releases_dir, "r1")
        self.release_b = os.path.join(releases_dir, "r2")
        self.release_c = os.path.join(releases_dir, "r3")
        for idx, path in enumerate((self.release_a, self.release_b, self.release_c), start=1):
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "VERSION"), "w", encoding="utf-8") as fh:
                fh.write(f"0.0.{idx}\n")
            os.utime(path, (100 + idx, 100 + idx))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_overview_marks_only_old_releases_as_removable(self) -> None:
        overview = self.release_cleanup.get_release_cleanup_overview(keep_count=2)
        self.assertTrue(overview["supported"])
        self.assertEqual(overview["total_releases"], 3)
        self.assertEqual(overview["removable_releases"], 1)
        self.assertEqual(len(overview["removable"]), 1)

    def test_run_cleanup_returns_noop_when_nothing_to_remove(self) -> None:
        overview = self.release_cleanup.get_release_cleanup_overview(keep_count=5)
        self.assertEqual(overview["removable_releases"], 0)
        result = self.release_cleanup.run_release_cleanup(keep_count=5)
        self.assertEqual(result["status"], "noop")

    def test_run_cleanup_calls_script_with_install_env(self) -> None:
        script_path = os.path.join(self.config.SOURCE_ROOT, "scripts", "cleanup_releases.sh")
        os.makedirs(os.path.dirname(script_path), exist_ok=True)
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env bash\n")
        os.chmod(script_path, 0o755)
        proc = SimpleNamespace(returncode=0, stdout="Cleanup complete.\n", stderr="")
        with patch("services.release_cleanup.subprocess.run", return_value=proc) as mocked:
            result = self.release_cleanup.run_release_cleanup(keep_count=2)
        self.assertEqual(result["status"], "success")
        args, kwargs = mocked.call_args
        self.assertEqual(args[0], [script_path, "--keep", "2"])
        self.assertEqual(kwargs["env"]["NODE_PLANE_BASE_DIR"], self.config.INSTALL_ROOT)


if __name__ == "__main__":
    unittest.main()
