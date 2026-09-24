import os
import pathlib
import subprocess
import tempfile
import unittest


RUNTIME_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "python_runtime.sh"


class PythonRuntimeSelectionTests(unittest.TestCase):
    def _fake_python(self, directory: pathlib.Path, name: str, version: str) -> pathlib.Path:
        path = directory / name
        path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def _select(self, fake_bin: pathlib.Path, override: str = "") -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
        if override:
            environment["NODE_PLANE_PYTHON_BIN"] = override
        else:
            environment.pop("NODE_PLANE_PYTHON_BIN", None)
        return subprocess.run(
            ["bash", "-c", 'source "$1"; select_python_runtime', "bash", str(RUNTIME_SCRIPT)],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_finds_python312_when_default_python_is_unsupported(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake_bin = pathlib.Path(temporary)
            preferred = self._fake_python(fake_bin, "python3.12", "3.12")
            self._fake_python(fake_bin, "python3", "3.14")
            result = self._select(fake_bin)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(preferred))

    def test_rejects_unsupported_explicit_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake_bin = pathlib.Path(temporary)
            self._fake_python(fake_bin, "python3.12", "3.12")
            unsupported = self._fake_python(fake_bin, "python3.14", "3.14")
            result = self._select(fake_bin, str(unsupported))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not Python 3.11/3.12", result.stderr)


if __name__ == "__main__":
    unittest.main()
