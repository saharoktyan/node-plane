import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_release_in_container.sh"


class ReleaseContainerTests(unittest.TestCase):
    def test_container_runs_as_checkout_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake_docker = pathlib.Path(temporary) / "docker"
            fake_docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_LOG"\n', encoding="utf-8")
            fake_docker.chmod(0o755)
            log = pathlib.Path(temporary) / "docker.log"
            env = os.environ.copy()
            env["PATH"] = f"{temporary}:{env['PATH']}"
            env["DOCKER_LOG"] = str(log)
            result = subprocess.run(
                [str(BUILD_SCRIPT), "v0.4.1-alpha.10"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            command = log.read_text(encoding="utf-8")
            owner = ROOT.stat()
            self.assertIn(f"--user {owner.st_uid}:{owner.st_gid}", command)
            self.assertIn("--env GIT_CONFIG_VALUE_0=/work", command)


if __name__ == "__main__":
    unittest.main()
