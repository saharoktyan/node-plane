import importlib.util
import json
from pathlib import Path
import tempfile
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


if __name__ == "__main__":
    unittest.main()
