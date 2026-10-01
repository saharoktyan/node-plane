"""Owned-path validation and uninstall script review; scripts are never executed."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.authorization import AccessDenied
from backend.installation_maintenance import InstallationMaintenance, owned_path


class InstallationMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "installation"
        self.shared = self.base / "shared"
        self.shared.mkdir(parents=True)
        (self.base / "releases" / "test").mkdir(parents=True)
        (self.base / "current").symlink_to(self.base / "releases" / "test")
        (self.shared / ".env").write_text(
            "BOT_TOKEN=keep\nNODE_AGENT_TARGETS=lv1=example:50061\n"
        )
        marker = {
            "format": "node-plane-systemd-v1",
            "base_dir": str(self.base),
            "shared_dir": str(self.shared),
            "postgres_container": None,
        }
        (self.base / ".node-plane-installation.json").write_text(json.dumps(marker))
        self.units = Path(self.temp.name) / "units"
        self.units.mkdir()
        self.host = InstallationMaintenance(
            staging_root=self.temp.name, units_root=self.units
        )
        for mocker in (
            patch.dict(
                os.environ,
                {
                    "NODE_PLANE_BASE_DIR": str(self.base),
                    "NODE_PLANE_SHARED_DIR": str(self.shared),
                    "NODE_PLANE_APP_DIR": str(self.base / "current"),
                },
            ),
            patch("backend.installation_maintenance.os.geteuid", return_value=0),
            patch(
                "backend.installation_maintenance.shutil.which", return_value="/fake"
            ),
        ):
            mocker.start()
            self.addCleanup(mocker.stop)

    def test_refuses_broad_paths_and_missing_marker(self):
        for path in ("/", "/opt", "/home/user", "/usr/local", "relative"):
            with self.assertRaises(AccessDenied):
                owned_path(path)
        (self.base / ".node-plane-installation.json").unlink()
        with self.assertRaises(AccessDenied):
            self.host.deployment()

    def test_reset_removes_managed_keys_and_old_backups_only(self):
        for name in ("ssh", "driver-agent-tls", "backups/backend"):
            (self.shared / name).mkdir(parents=True)
        (self.shared / "ssh/key").write_text("private")
        (self.shared / "backups/backend/keep.json").write_text("{}")
        (self.shared / "backups/backend/old.json").write_text("{}")
        with patch("backend.installation_maintenance.subprocess.run") as run:
            self.host.clear_local(self.host.deployment(), "keep")
            run.assert_called_once()
        self.assertFalse((self.shared / "ssh").exists())
        self.assertTrue((self.shared / "backups/backend/keep.json").exists())
        self.assertFalse((self.shared / "backups/backend/old.json").exists())
        self.assertIn("BOT_TOKEN=keep", (self.shared / ".env").read_text())
        self.assertIn("NODE_AGENT_TARGETS=\n", (self.shared / ".env").read_text())

    def test_symlink_rejected_before_any_deletion(self):
        (self.shared / "ssh").mkdir()
        (self.shared / "backups").symlink_to(self.units)
        with self.assertRaises(AccessDenied):
            self.host.clear_local(self.host.deployment(), "keep")
        self.assertTrue((self.shared / "ssh").exists())

    def test_prepared_uninstall_is_scoped_idempotent_and_abortable(self):
        job_id = str(uuid4())
        plan = self.host.prepare_uninstall(self.host.deployment(), job_id)
        self.assertEqual(
            plan, self.host.prepare_uninstall(self.host.deployment(), job_id)
        )
        script = Path(plan["script"])
        subprocess.run(["bash", "-n", str(script)], check=True)
        text = script.read_text()
        self.assertIn("node-plane-backend-worker.timer", text)
        self.assertNotIn("prune", text)
        self.assertNotIn("SOURCE_ROOT", text)
        self.assertEqual(script.stat().st_mode & 0o777, 0o700)
        self.host.discard_uninstall(plan)
        self.assertFalse(script.exists())

    def test_repurposed_unit_prevents_removal(self):
        (self.units / "node-plane-backend.service").write_text("ExecStart=/other/app")
        with self.assertRaises(AccessDenied):
            self.host.prepare_uninstall(self.host.deployment(), str(uuid4()))
