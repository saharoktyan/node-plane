from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

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

from services import xray, profile_state
from handlers import user_getkey
import awg_profile


class ConfigRefreshTests(unittest.TestCase):
    def test_awg_file_formats_use_current_config(self) -> None:
        for extension, expected in (("vpn", "vpn://fresh"), ("conf", "current-conf")):
            with self.subTest(extension=extension):
                update = SimpleNamespace(effective_chat=SimpleNamespace(id=1), effective_user=SimpleNamespace(id=2))
                context = SimpleNamespace(bot=Mock(), user_data={})
                context.bot.send_document.return_value = SimpleNamespace(message_id=3)
                with patch.object(user_getkey, "answer_cb"), patch.object(user_getkey, "_is_admin", return_value=False), patch.object(user_getkey, "get_locale_for_update", return_value="en"), patch.object(user_getkey, "_delete_all_getkey_artifacts"), patch.object(user_getkey, "_resolve_profile_name", return_value="alice"), patch.object(user_getkey, "get_access_method_by_getkey_payload", return_value=None), patch.object(user_getkey, "get_profile_access_status", return_value={"frozen": False, "active": True}), patch.object(user_getkey, "get_allowed_protocols", return_value=[]), patch.object(user_getkey, "get_access_methods_for_codes", return_value=[SimpleNamespace(protocol_kind="awg", server_key="node")]), patch.object(user_getkey, "get_awg_access_method_by_server_key", return_value=None), patch.object(user_getkey, "_current_awg_server", return_value={"config": "vpn://fresh", "wg_conf": "current-conf"}), patch.object(user_getkey, "get_server", return_value=SimpleNamespace(title="Moscow #1")), patch.object(user_getkey, "kb_getkey_attachment_back"), patch.object(user_getkey, "safe_delete_update_message"):
                    user_getkey.on_getkey_callback(update, context, f"awg_{extension}:node")
                document = context.bot.send_document.call_args.kwargs["document"]
                self.assertEqual(document.getvalue().decode(), expected)
                self.assertEqual(document.name, f"AmneziaWG Moscow #1 - alice.{extension}")

    def test_old_awg_button_cannot_reenable_revoked_or_frozen_access(self):
        for frozen in (True, False):
            with self.subTest(frozen=frozen):
                update = SimpleNamespace(effective_chat=SimpleNamespace(id=1), effective_user=SimpleNamespace(id=2))
                context = SimpleNamespace(bot=Mock(), user_data={})
                with patch.object(user_getkey, "answer_cb"), patch.object(user_getkey, "_is_admin", return_value=False), patch.object(user_getkey, "get_locale_for_update", return_value="en"), patch.object(user_getkey, "_resolve_profile_name", return_value="alice"), patch.object(user_getkey, "get_profile_access_status", return_value={"frozen": frozen, "text": "Frozen"}), patch.object(user_getkey, "get_allowed_protocols", return_value=[]), patch.object(user_getkey, "get_access_methods_for_codes", return_value=[]), patch.object(user_getkey, "_delete_all_getkey_artifacts"), patch.object(user_getkey, "kb_getkey_servers"), patch.object(user_getkey, "safe_edit_message"), patch.object(user_getkey, "_current_awg_server") as issue:
                    user_getkey.on_getkey_callback(update, context, "awg_vpn:node")
                issue.assert_not_called()
                context.bot.send_document.assert_not_called()

    def test_freeze_revokes_both_protocols_and_reports_partial_failure(self):
        methods = [SimpleNamespace(server_key="node", protocol_kind=kind, label=kind) for kind in ("xray", "awg")]
        driver = Mock()
        driver.delete_profile_from_node.side_effect = [SimpleNamespace(status="SUCCEEDED", progress_message=""), SimpleNamespace(status="FAILED", progress_message="offline")]
        with patch.object(profile_state, "get_profile", return_value={"uuid": "stable-id"}), patch.object(profile_state, "get_allowed_protocols", return_value=[]), patch("domain.servers.get_access_methods_for_codes", return_value=methods), patch("services.node_driver.get_node_driver", return_value=driver), patch("services.provisioning_state.upsert_profile_server_state") as state:
            errors = profile_state._sync_profile_frozen_access("alice", frozen=True)
        self.assertEqual(errors, ["awg"])
        self.assertEqual(driver.delete_profile_from_node.call_count, 2)
        self.assertFalse(state.call_args_list[0].kwargs["desired_enabled"])
        self.assertEqual(state.call_args_list[1].kwargs["status"], "failed")

    def test_unfreeze_restores_existing_uuid_for_both_protocols(self):
        methods = [SimpleNamespace(server_key="node", protocol_kind=kind, label=kind) for kind in ("xray", "awg")]
        driver = Mock()
        driver.ensure_profile_on_node.return_value = SimpleNamespace(status="SUCCEEDED", progress_message="")
        with patch.object(profile_state, "get_profile", return_value={"uuid": "stable-id"}), patch.object(profile_state, "get_allowed_protocols", return_value=[]), patch("domain.servers.get_access_methods_for_codes", return_value=methods), patch("services.node_driver.get_node_driver", return_value=driver), patch("services.provisioning_state.upsert_profile_server_state"):
            self.assertEqual(profile_state._sync_profile_frozen_access("alice", frozen=False), [])
        self.assertEqual(driver.ensure_profile_on_node.call_count, 2)
        self.assertEqual(driver.ensure_profile_on_node.call_args_list[0].kwargs["xray_uuid"], "stable-id")

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
            key="node", title="Moscow #1", xray_host="new.example", xray_xhttp_port=8443,
            xray_tcp_port=443, xray_sni="sni.example", xray_fp="firefox",
            xray_pbk="new-pbk", xray_short_id="current-sid", xray_sid="current-sid",
            xray_flow="xtls-rprx-vision", xray_xhttp_path_prefix="/assets", bootstrap_state="bootstrapped",
        )
        driver = SimpleNamespace(sync_xray=lambda node: SimpleNamespace(status="SUCCEEDED"), ensure_profile_on_node=Mock(return_value=SimpleNamespace(status="SUCCEEDED")))
        with patch.object(xray, "get_server", return_value=server), patch.object(xray, "get_server_link_status", return_value=(True, "ok")), patch.object(xray, "get_short_id_local", return_value="stale-sid"), patch("services.node_driver.get_node_driver", return_value=driver):
            link = xray.build_vless_link_transport("alice", "uuid", "tcp", "node")
        self.assertIn("sid=current-sid", link)
        self.assertIn("pbk=new-pbk", link)
        self.assertIn("@new.example:443", link)
        self.assertNotIn("stale-sid", link)

    def test_xhttp_link_imports_client_transport_settings(self) -> None:
        server = SimpleNamespace(
            key="node", xray_host="new.example", xray_xhttp_port=8443,
            xray_sni="sni.example", xray_fp="chrome", xray_pbk="public-key",
            xray_short_id="current-sid", xray_sid="current-sid",
            xray_xhttp_path_prefix="/assets/xhttp", bootstrap_state="bootstrapped",
            title="Moscow #1",
        )
        driver = SimpleNamespace(sync_xray=lambda node: SimpleNamespace(status="SUCCEEDED"), ensure_profile_on_node=Mock(return_value=SimpleNamespace(status="SUCCEEDED")))
        with patch.object(xray, "get_server", return_value=server), patch.object(xray, "get_server_link_status", return_value=(True, "ok")), patch("services.node_driver.get_node_driver", return_value=driver):
            link = xray.build_vless_link_transport("alice", "uuid", "xhttp", "node")
        params = parse_qs(urlsplit(link).query)
        self.assertEqual(params["mode"], ["auto"])
        self.assertEqual(params["path"], ["/assets/xhttp"])
        self.assertEqual(json.loads(params["extra"][0]), {"xmux": {"maxConcurrency": "16-32"}})
        self.assertNotIn("allowInsecure", params)

    def test_awg_issuance_refreshes_and_persists_existing_peer(self) -> None:
        old = {"config": "vpn://old", "wg_conf": "[Interface]\nPrivateKey = old\n"}
        driver = SimpleNamespace(refresh_awg_config=lambda node, conf, name: ("[Interface]\nPrivateKey = new\n", "vpn://new"), ensure_profile_on_node=Mock(return_value=SimpleNamespace(status="SUCCEEDED", result_json=json.dumps({"wg_conf": old["wg_conf"]}))))
        with patch.object(user_getkey, "get_awg_server", return_value=old), patch.object(user_getkey, "get_server", return_value=SimpleNamespace(title="Moscow #1", bootstrap_state="bootstrapped")), patch.object(user_getkey, "get_node_driver", return_value=driver), patch.object(user_getkey, "update_awg_server") as save:
            result = user_getkey._current_awg_server("alice", "node")
        self.assertEqual(result["config"], "vpn://new")
        self.assertEqual(result["wg_conf"], "[Interface]\nPrivateKey = new\n")
        save.assert_called_once()

    def test_awg_issuance_blocks_unapplied_changes(self) -> None:
        with patch.object(user_getkey, "get_awg_server", return_value={"wg_conf": "old"}), patch.object(user_getkey, "get_server", return_value=SimpleNamespace(bootstrap_state="edited")), patch.object(user_getkey, "get_node_driver") as driver:
            with self.assertRaises(RuntimeError):
                user_getkey._current_awg_server("alice", "node")
        driver.assert_not_called()

    def test_awg_after_clean_reinstall_uses_recreated_peer_credentials(self) -> None:
        driver = SimpleNamespace(
            ensure_profile_on_node=Mock(return_value=SimpleNamespace(
                status="SUCCEEDED", result_json=json.dumps({"wg_conf": "new-peer-conf"}))),
            refresh_awg_config=Mock(return_value=("refreshed-new-peer", "vpn://fresh")),
        )
        with patch.object(user_getkey, "get_awg_server", return_value={"wg_conf": "deleted-peer-conf"}), \
             patch.object(user_getkey, "get_server", return_value=SimpleNamespace(title="Moscow", bootstrap_state="bootstrapped")), \
             patch.object(user_getkey, "get_node_driver", return_value=driver), \
             patch.object(user_getkey, "update_awg_server") as save:
            result = user_getkey._current_awg_server("alice", "node")
        driver.ensure_profile_on_node.assert_called_once_with("node", "alice", ["awg"])
        driver.refresh_awg_config.assert_called_once_with("node", "new-peer-conf", "Moscow · alice")
        self.assertEqual(result["wg_conf"], "refreshed-new-peer")
        save.assert_called_once()

    def test_xray_issuance_blocks_when_profile_cannot_be_restored(self) -> None:
        driver = SimpleNamespace(
            ensure_profile_on_node=Mock(return_value=SimpleNamespace(status="FAILED")),
            sync_xray=Mock(),
        )
        with patch.object(xray, "get_server", return_value=SimpleNamespace(bootstrap_state="bootstrapped")), \
             patch("services.node_driver.get_node_driver", return_value=driver):
            with self.assertRaises(ValueError):
                xray.build_vless_link_transport("alice", "uuid", "tcp", "node")
        driver.ensure_profile_on_node.assert_called_once_with("node", "alice", ["xray"], xray_uuid="uuid")
        driver.sync_xray.assert_not_called()


if __name__ == "__main__":
    unittest.main()
