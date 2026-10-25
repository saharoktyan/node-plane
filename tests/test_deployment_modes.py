from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DeploymentModesTests(unittest.TestCase):
    def test_help_for_cli_scripts_advertises_no_portable_mode(self):
        for script in ("install.sh", "update.sh", "healthcheck.sh"):
            with self.subTest(script=script):
                result = subprocess.run(
                    ["bash", str(ROOT / "scripts" / script), "--help"],
                    capture_output=True, text=True, timeout=5
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("simple", result.stdout)
                # Word-bounded check: do not assert absence of "portable" as substring because "importable" contains it
                match = re.search(r"\bportable\b", result.stdout, re.IGNORECASE)
                self.assertIsNone(match, f"{script} --help advertises portable mode: {match}")

    def test_unsupported_mode_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            temp_dir = Path(directory)
            shutil.copytree(ROOT / "scripts", temp_dir / "scripts")

            # healthcheck.sh rejects unsupported mode
            res_hc = subprocess.run(
                ["bash", str(temp_dir / "scripts" / "healthcheck.sh"), "--mode", "unsupported_xyz"],
                capture_output=True, text=True, timeout=5, cwd=temp_dir
            )
            self.assertNotEqual(res_hc.returncode, 0)
            self.assertIn("Unsupported mode", res_hc.stderr)

            # install.sh rejects unsupported mode before mutation
            res_in = subprocess.run(
                ["bash", str(temp_dir / "scripts" / "install.sh"), "--mode", "unsupported_xyz"],
                capture_output=True, text=True, timeout=5, cwd=temp_dir
            )
            self.assertNotEqual(res_in.returncode, 0)
            self.assertIn("Unsupported mode", res_in.stderr)

            # update.sh rejects unsupported mode before mutation
            res_up = subprocess.run(
                ["bash", str(temp_dir / "scripts" / "update.sh"), "--mode", "unsupported_xyz"],
                capture_output=True, text=True, timeout=5, cwd=temp_dir
            )
            self.assertNotEqual(res_up.returncode, 0)
            self.assertIn("Unsupported mode", res_up.stderr)
