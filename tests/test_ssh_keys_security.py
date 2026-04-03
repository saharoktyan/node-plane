from __future__ import annotations

import importlib
import os
import stat
import sys
import tempfile
import unittest


TESTS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
APP_ROOT = os.path.join(REPO_ROOT, "app")
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class SSHKeysSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = self.tmpdir.name
        os.environ["NODE_PLANE_BASE_DIR"] = base
        os.environ["NODE_PLANE_SHARED_DIR"] = base
        os.environ["SSH_KEY"] = os.path.join(base, "ssh", "id_ed25519")

        import config
        import services.ssh_keys as ssh_keys

        self.config = importlib.reload(config)
        self.ssh_keys = importlib.reload(ssh_keys)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_existing_keypair_permissions_are_hardened(self) -> None:
        private_path = self.ssh_keys.get_ssh_private_key_path()
        public_path = self.ssh_keys.get_ssh_public_key_path()
        os.makedirs(os.path.dirname(private_path), exist_ok=True)
        with open(private_path, "w", encoding="utf-8") as fh:
            fh.write("PRIVATE")
        with open(public_path, "w", encoding="utf-8") as fh:
            fh.write("PUBLIC\n")
        os.chmod(private_path, 0o644)
        os.chmod(public_path, 0o666)

        ok, err = self.ssh_keys.ensure_ssh_keypair()

        self.assertTrue(ok, err)
        self.assertEqual(stat.S_IMODE(os.stat(private_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(public_path).st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
