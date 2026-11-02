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


class XrayUserApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        self.config = {
            "api": {"services": ["StatsService", "HandlerService"]},
            "inbounds": [
                {"tag": tag, "port": port, "settings": {"clients": []}}
                for tag, port in (("reality-tcp", 443), ("reality-xhttp", 8443))
            ],
        }
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        self.live = {"reality-tcp": {}, "reality-xhttp": {}}
        self.calls = []
        self.uuid = "11111111-1111-4111-8111-111111111111"

    def api_users(self, _container, tag):
        return self.live[tag].copy()

    def api_add(self, _container, inbound, client, _directory):
        self.calls.append(("add", inbound["tag"]))
        self.live[inbound["tag"]][client["email"]] = {
            "id": client["id"], "flow": client.get("flow", "")
        }

    def api_remove(self, _container, tag, name):
        self.calls.append(("remove", tag))
        del self.live[tag][name]

    def invoke(self, action, uuid=None):
        with patch.object(MODULE, "ensure_api", side_effect=lambda _p, cfg, _c: cfg), \
             patch.object(MODULE, "users", side_effect=self.api_users), \
             patch.object(MODULE, "add_live", side_effect=self.api_add), \
             patch.object(MODULE, "remove_live", side_effect=self.api_remove):
            MODULE.run(action, self.path, "xray", "reality-tcp", "reality-xhttp", "alice", uuid)

    def test_add_persists_both_transports_without_restart(self):
        self.invoke("add", self.uuid)
        config = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.calls, [("add", "reality-tcp"), ("add", "reality-xhttp")])
        self.assertEqual(config["inbounds"][0]["settings"]["clients"][0]["flow"], "xtls-rprx-vision")
        self.assertNotIn("flow", config["inbounds"][1]["settings"]["clients"][0])

    def test_retry_repairs_partial_live_add(self):
        self.invoke("add", self.uuid)
        self.live["reality-xhttp"].clear()
        self.calls.clear()
        self.invoke("add", self.uuid)
        self.assertEqual(self.calls, [("add", "reality-xhttp")])
        self.assertEqual(self.live["reality-xhttp"]["alice"]["id"], self.uuid)

    def test_delete_rewrites_file_and_revokes_live_users(self):
        self.invoke("add", self.uuid)
        self.calls.clear()
        self.invoke("delete")
        self.assertEqual(self.calls, [("remove", "reality-tcp"), ("remove", "reality-xhttp")])
        config = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertTrue(all(not item["settings"]["clients"] for item in config["inbounds"]))

    def test_retry_replaces_stale_live_uuid_even_when_file_is_current(self):
        self.invoke("add", self.uuid)
        self.live["reality-tcp"]["alice"]["id"] = "22222222-2222-4222-8222-222222222222"
        self.calls.clear()
        self.invoke("add", self.uuid)
        self.assertEqual(self.calls, [("remove", "reality-tcp"), ("add", "reality-tcp")])
        self.assertEqual(self.live["reality-tcp"]["alice"]["id"], self.uuid)

    def test_failed_second_inbound_keeps_retriable_config(self):
        def failing_add(container, inbound, client, directory):
            if inbound["tag"] == "reality-xhttp":
                raise RuntimeError("API unavailable")
            self.api_add(container, inbound, client, directory)

        with patch.object(MODULE, "ensure_api", side_effect=lambda _p, cfg, _c: cfg), \
             patch.object(MODULE, "users", side_effect=self.api_users), \
             patch.object(MODULE, "add_live", side_effect=failing_add):
            with self.assertRaisesRegex(RuntimeError, "API unavailable"):
                MODULE.run("add", self.path, "xray", "reality-tcp", "reality-xhttp", "alice", self.uuid)
        self.assertTrue(all(item["settings"]["clients"] for item in json.loads(self.path.read_text())["inbounds"]))
        self.invoke("add", self.uuid)
        self.assertEqual(self.live["reality-xhttp"]["alice"]["id"], self.uuid)

    def test_existing_file_mount_is_redeployed_once_with_handler_service(self):
        self.config["api"]["services"] = ["StatsService"]
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        with patch.object(MODULE, "docker", return_value="/etc/xray/config.json\n"), \
             patch.object(MODULE.subprocess, "run") as deploy:
            MODULE.ensure_api(self.path, self.config, "xray")
        self.assertIn("HandlerService", json.loads(self.path.read_text())["api"]["services"])
        deploy.assert_called_once_with(["/opt/node-plane-runtime/deploy-xray.sh"], check=True)

    def test_partial_delete_can_be_retried(self):
        self.invoke("add", self.uuid)

        def failing_remove(container, tag, name):
            if tag == "reality-xhttp":
                raise RuntimeError("API unavailable")
            self.api_remove(container, tag, name)

        with patch.object(MODULE, "ensure_api", side_effect=lambda _p, cfg, _c: cfg), \
             patch.object(MODULE, "users", side_effect=self.api_users), \
             patch.object(MODULE, "remove_live", side_effect=failing_remove):
            with self.assertRaisesRegex(RuntimeError, "API unavailable"):
                MODULE.run("delete", self.path, "xray", "reality-tcp", "reality-xhttp", "alice")
        self.assertEqual(self.live["reality-tcp"], {})
        self.assertFalse(json.loads(self.path.read_text())["inbounds"][0]["settings"]["clients"])
        self.invoke("delete")
        self.assertEqual(self.live["reality-xhttp"], {})


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

    def test_agent_transport_reads_and_changes_users_without_docker(self):
        calls, run = self.run_agent(json.dumps({"users": [
            {"email": "alice", "account": {"id": "1", "flow": "xtls-rprx-vision"}}]}))
        with patch.object(MODULE, "agent", return_value="/agent"), run, \
             patch.dict(os.environ, {"NODE_PLANE_XRAY_API": "127.0.0.1:1234"}), \
             patch.object(MODULE, "docker", side_effect=AssertionError("docker used")):
            self.assertEqual(MODULE.users("xray", "reality-tcp"),
                             {"alice": {"id": "1", "flow": "xtls-rprx-vision"}})
            MODULE.add_live("xray", {"tag": "reality-xhttp"}, {"email": "alice", "id": "1"}, self.root)
            MODULE.remove_live("xray", "reality-tcp", "alice")
        self.assertEqual(calls, [
            ["/agent", "xray-api", "--server=127.0.0.1:1234", "inbound-users", "--tag=reality-tcp"],
            ["/agent", "xray-api", "--server=127.0.0.1:1234", "add-user", "--tag=reality-xhttp",
             "--email=alice", "--uuid=1", "--flow="],
            ["/agent", "xray-api", "--server=127.0.0.1:1234", "remove-user", "--tag=reality-tcp",
             "--email=alice"],
        ])

    def test_agent_transport_failure_is_an_error_not_absence(self):
        _, run = self.run_agent("", returncode=3, stderr="Xray API unavailable: refused")
        with patch.object(MODULE, "agent", return_value="/agent"), run:
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                MODULE.users("xray", "reality-tcp")


if __name__ == "__main__":
    unittest.main()
