import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "runtime_assets" / "xray-user-api.py"
SPEC = importlib.util.spec_from_file_location("xray_user_api", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class XrayApiTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def executable(self, name, body):
        path = self.root / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return str(path)

    def test_probe_accepts_an_agent_that_serves_xray_api(self):
        path = self.executable("new-agent", """[ "$1 $2" = "xray-api capabilities" ] && echo '{"xray_api": 1}'""")
        self.assertEqual(MODULE._probe(path), path)

    def test_probe_makes_an_older_agent_exit_before_it_serves(self):
        # Older agents ignore arguments and start their server: the probe must
        # give them a missing config and an unparsable listen address.
        path = self.executable("old-agent", """[ "$NODE_AGENT_LISTEN_ADDR" = probe ] && [ ! -e "$NODE_AGENT_CONFIG_PATH" ] && exit 1
sleep 30""")
        started = time.monotonic()
        self.assertIsNone(MODULE._probe(path))
        self.assertLess(time.monotonic() - started, 3)

    def test_missing_agent_keeps_the_container_cli(self):
        self.assertIsNone(MODULE._probe(str(self.root / "absent")))
        with patch.dict(os.environ, {"NODE_PLANE_AGENT_BIN": str(self.root / "absent")}):
            self.assertIsNone(MODULE.agent())

    def run_agent(self, stdout, returncode=0, stderr=""):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, returncode, stdout, stderr)
        return calls, patch.object(MODULE.subprocess, "run", side_effect=run)

    def test_agent_transport_reads_users_without_docker(self):
        calls, run = self.run_agent(json.dumps({"users": [
            {"email": "alice", "account": {"id": "1", "flow": "xtls-rprx-vision"}}]}))
        with patch.object(MODULE, "agent", return_value="/agent"), run, \
             patch.dict(os.environ, {"NODE_PLANE_XRAY_API": "127.0.0.1:1234"}), \
             patch.object(MODULE, "docker", side_effect=AssertionError("docker used")):
            self.assertEqual(MODULE.users("xray", "reality-tcp"),
                             {"alice": {"id": "1", "flow": "xtls-rprx-vision"}})
        self.assertEqual(calls, [
            ["/agent", "xray-api", "--server=127.0.0.1:1234", "inbound-users", "--tag=reality-tcp"]])

    def test_agent_transport_failure_is_an_error_not_absence(self):
        _, run = self.run_agent("", returncode=3, stderr="Xray API unavailable: refused")
        with patch.object(MODULE, "agent", return_value="/agent"), run:
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                MODULE.users("xray", "reality-tcp")


if __name__ == "__main__":
    unittest.main()
