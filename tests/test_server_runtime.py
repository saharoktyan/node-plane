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


class ServerRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = self.tmpdir.name
        os.environ["NODE_PLANE_BASE_DIR"] = base
        os.environ["NODE_PLANE_SHARED_DIR"] = base
        os.environ["SSH_KNOWN_HOSTS_PATH"] = os.path.join(base, "ssh", "known_hosts")
        os.environ["SSH_STRICT_HOST_KEY_CHECKING"] = "yes"

        import config
        import services.server_runtime as server_runtime

        self.config = importlib.reload(config)
        self.server_runtime = importlib.reload(server_runtime)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_ensure_known_host_scans_missing_host_key(self) -> None:
        server = SimpleNamespace(key="de1", ssh_host="37.233.84.218", ssh_target="root@37.233.84.218", ssh_port=22)

        def fake_run(args, capture_output=True, text=True):
            if args[:2] == ["ssh-keygen", "-F"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            if args[:2] == ["ssh-keyscan", "-T"]:
                return SimpleNamespace(returncode=0, stdout="37.233.84.218 ssh-ed25519 AAAA\n", stderr="")
            raise AssertionError(args)

        with patch.object(self.server_runtime.subprocess, "run", side_effect=fake_run):
            ok, err = self.server_runtime.ensure_known_host(server)

        self.assertTrue(ok, err)
        with open(self.config.SSH_KNOWN_HOSTS_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("ssh-ed25519", content)

    def test_ensure_known_host_skips_scan_when_entry_exists(self) -> None:
        server = SimpleNamespace(key="de1", ssh_host="37.233.84.218", ssh_target="root@37.233.84.218", ssh_port=22)

        def fake_run(args, capture_output=True, text=True):
            if args[:2] == ["ssh-keygen", "-F"]:
                return SimpleNamespace(returncode=0, stdout="found", stderr="")
            if args[:1] == ["ssh-keyscan"]:
                raise AssertionError("ssh-keyscan should not run when host key is already known")
            raise AssertionError(args)

        with patch.object(self.server_runtime.subprocess, "run", side_effect=fake_run):
            ok, err = self.server_runtime.ensure_known_host(server)

        self.assertTrue(ok, err)


if __name__ == "__main__":
    unittest.main()
