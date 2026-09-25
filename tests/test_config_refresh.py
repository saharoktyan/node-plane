from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.postgres_test_harness import configure_postgres_test_env

ROOT = Path(__file__).resolve().parent.parent
configure_postgres_test_env(tempfile.mkdtemp(prefix="node-plane-config-refresh-"))
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "runtime_assets"))
telegram = types.ModuleType("telegram")
telegram.Update = object
telegram.InlineKeyboardButton = object
telegram.InlineKeyboardMarkup = object
telegram_ext = types.ModuleType("telegram.ext")
telegram_ext.CallbackContext = object
telegram_error = types.ModuleType("telegram.error")
telegram_error.BadRequest = Exception
telegram_error.RetryAfter = Exception
qrcode = types.ModuleType("qrcode")
qrcode.make = lambda value: None
sys.modules.setdefault("telegram", telegram)
sys.modules.setdefault("telegram.ext", telegram_ext)
sys.modules.setdefault("telegram.error", telegram_error)
sys.modules.setdefault("qrcode", qrcode)

spec = importlib.util.spec_from_file_location("refresh_awg_config", ROOT / "runtime_assets" / "refresh-awg-config.py")
assert spec and spec.loader
refresh_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh_module)

from services import xray
from handlers import user_getkey
import awg_profile


class ConfigRefreshTests(unittest.TestCase):
    def test_awg_refresh_uses_node_and_profile_name(self) -> None:
        self.assertEqual(refresh_module.profile_description("Moscow #1 · alice"), "Moscow #1 · alice")
        self.assertEqual(refresh_module.profile_description(""), "AmneziaWG")

    def test_existing_awg_peer_gets_current_endpoint_and_entropy(self) -> None:
        old = """[Interface]
PrivateKey = client-secret
PublicKey = client-pub
Address = 10.8.1.2/32
I1 = old

[Peer]
PublicKey = old-server
PresharedKey = shared-secret
Endpoint = old.example:51820
AllowedIPs = 0.0.0.0/0
"""
        profile = awg_profile.new_profile("quic")
        current = "[Interface]\n" + "\n".join(f"{key} = {value}" for key, value in profile.items()) + "\n"
        updated = refresh_module.refresh(old, current, "new.example", "53000", "new-server")
        self.assertIn("PrivateKey = client-secret", updated)
        self.assertIn("PresharedKey = shared-secret", updated)
        self.assertIn("Address = 10.8.1.2/32", updated)
        self.assertIn(f"I1 = {profile['I1']}", updated)
        self.assertIn(f"HeaderProtectionKey = {profile['HeaderProtectionKey']}", updated)
        self.assertIn("PublicKey = new-server", updated)
        self.assertIn("Endpoint = new.example:53000", updated)
        self.assertNotIn("old.example", updated)

    def test_awg_refresh_rejects_unmigrated_server(self) -> None:
        with self.assertRaisesRegex(ValueError, "3.1"):
            refresh_module.refresh("[Interface]\nPrivateKey = old\n", "[Interface]\nJc = 4\n", "new.example", "51820", "server")

    def test_xray_link_uses_current_server_short_id(self) -> None:
        server = SimpleNamespace(
            key="node", xray_host="new.example", xray_xhttp_port=8443,
            xray_tcp_port=443, xray_sni="sni.example", xray_fp="firefox",
            xray_pbk="new-pbk", xray_short_id="current-sid", xray_sid="current-sid",
            xray_flow="xtls-rprx-vision", xray_xhttp_path_prefix="/assets", bootstrap_state="bootstrapped",
        )
        driver = SimpleNamespace(sync_xray=lambda node: SimpleNamespace(status="SUCCEEDED"))
        with patch.object(xray, "get_server", return_value=server), patch.object(xray, "get_server_link_status", return_value=(True, "ok")), patch.object(xray, "get_short_id_local", return_value="stale-sid"), patch("services.node_driver.get_node_driver", return_value=driver):
            link = xray.build_vless_link_transport("alice", "uuid", "tcp", "node")
        self.assertIn("sid=current-sid", link)
        self.assertIn("pbk=new-pbk", link)
        self.assertIn("@new.example:443", link)
        self.assertNotIn("stale-sid", link)

    def test_awg_issuance_refreshes_and_persists_existing_peer(self) -> None:
        old = {"config": "vpn://old", "wg_conf": "[Interface]\nPrivateKey = old\n"}
        driver = SimpleNamespace(refresh_awg_config=lambda node, conf, name: ("[Interface]\nPrivateKey = new\n", "vpn://new"))
        with patch.object(user_getkey, "get_awg_server", return_value=old), patch.object(user_getkey, "get_server", return_value=SimpleNamespace(bootstrap_state="bootstrapped")), patch.object(user_getkey, "get_node_driver", return_value=driver), patch.object(user_getkey, "update_awg_server") as save:
            result = user_getkey._current_awg_server("alice", "node")
        self.assertEqual(result["config"], "vpn://new")
        self.assertEqual(result["wg_conf"], "[Interface]\nPrivateKey = new\n")
        save.assert_called_once()

    def test_awg_issuance_blocks_unapplied_changes(self) -> None:
        with patch.object(user_getkey, "get_awg_server", return_value={"wg_conf": "old"}), patch.object(user_getkey, "get_server", return_value=SimpleNamespace(bootstrap_state="edited")), patch.object(user_getkey, "get_node_driver") as driver:
            with self.assertRaises(RuntimeError):
                user_getkey._current_awg_server("alice", "node")
        driver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
