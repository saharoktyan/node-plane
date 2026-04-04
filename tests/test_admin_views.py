from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

TESTS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
APP_ROOT = os.path.join(REPO_ROOT, "app")
TMPDIR = tempfile.mkdtemp(prefix="node-plane-test-")
os.environ.setdefault("NODE_PLANE_BASE_DIR", TMPDIR)
os.environ.setdefault("SQLITE_DB_PATH", os.path.join(TMPDIR, "bot.sqlite3"))
os.environ.setdefault("SUBS_DB_PATH", os.path.join(TMPDIR, "subs.json"))
os.environ.setdefault("USERS_DB_PATH", os.path.join(TMPDIR, "users.json"))
os.environ.setdefault("WG_DB_PATH", os.path.join(TMPDIR, "wg_db.json"))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class InlineKeyboardButton:
    def __init__(self, text: str, callback_data: str):
        self.text = text
        self.callback_data = callback_data


class InlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


telegram_module = types.ModuleType("telegram")
telegram_module.Update = object
telegram_module.InlineKeyboardButton = InlineKeyboardButton
telegram_module.InlineKeyboardMarkup = InlineKeyboardMarkup
telegram_error_module = types.ModuleType("telegram.error")
telegram_error_module.BadRequest = Exception
telegram_error_module.RetryAfter = Exception
telegram_ext_module = types.ModuleType("telegram.ext")
telegram_ext_module.CallbackContext = object
sys.modules.setdefault("telegram", telegram_module)
sys.modules["telegram"] = telegram_module
sys.modules["telegram.error"] = telegram_error_module
sys.modules["telegram.ext"] = telegram_ext_module

from handlers import admin_server_wizard, user_profile
from services import ssh_keys
from ui import admin_views, user_views
from utils import keyboards


class AdminViewsTests(unittest.TestCase):
    def test_admin_menu_groups_operational_and_system_sections(self) -> None:
        markup = keyboards.kb_admin_menu(lang="en", updates_label="🆕 Updates")
        rows = markup.inline_keyboard
        self.assertEqual([button.callback_data for button in rows[0]], ["menu:admin_status", "menu:admin_requests"])
        self.assertEqual([button.callback_data for button in rows[1]], ["srv:menu", "cfg:start:edit"])
        self.assertEqual([button.callback_data for button in rows[2]], ["menu:admin_updates", "menu:admin_backups"])
        self.assertEqual([button.callback_data for button in rows[3]], ["menu:admin_settings"])
        self.assertEqual([button.callback_data for button in rows[4]], ["menu:admin_announce", "menu:sshkey"])

    def test_admin_updates_menu_prioritizes_check_and_update_actions(self) -> None:
        markup = keyboards.kb_admin_updates_menu(auto_check_enabled=True, update_supported=True, update_running=False, branch="dev", lang="en")
        rows = markup.inline_keyboard
        self.assertEqual([button.callback_data for button in rows[0]], ["menu:admin_updates_check", "menu:admin_updates_toggle_auto"])
        self.assertEqual([button.callback_data for button in rows[1]], ["menu:admin_updates_branch", "menu:admin_updates_versions:0"])
        self.assertEqual([button.callback_data for button in rows[2]], ["menu:admin_updates_run"])

    def test_admin_updates_menu_shows_runtime_sync_when_drift_exists(self) -> None:
        markup = keyboards.kb_admin_updates_menu(
            auto_check_enabled=True,
            update_supported=False,
            update_running=False,
            branch="dev",
            runtime_sync_available=True,
            lang="en",
        )
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("menu:admin_updates_runtime_sync", callbacks)

    def test_admin_backups_menu_groups_primary_actions(self) -> None:
        markup = keyboards.kb_admin_backups_menu(lang="en")
        rows = markup.inline_keyboard
        self.assertEqual([button.callback_data for button in rows[0]], ["menu:admin_backups_create", "menu:admin_backups_restore:0"])
        self.assertEqual([button.callback_data for button in rows[1]], ["menu:admin_backups_settings"])

    def test_backup_restore_page_uses_short_callback_tokens(self) -> None:
        fake_items = [
            {
                "name": "bot-2026-04-03T17-13-08-123456Z.sqlite3",
                "created_at": "2026-04-03T17:13:08Z",
                "trigger": "manual",
            }
        ]
        with patch.object(user_profile, "list_backups", return_value=fake_items):
            _text, markup = user_profile._render_admin_backups_restore_page("en", 0)
        callback = markup.inline_keyboard[0][0].callback_data
        self.assertTrue(callback.startswith("menu:admin_backups_pick:"))
        self.assertLessEqual(len(callback), 64)
        self.assertIn("2026-04-03 17:13 UTC", markup.inline_keyboard[0][0].text)
        self.assertNotIn("ago", markup.inline_keyboard[0][0].text)

    def test_admin_backups_settings_menu_marks_current_values(self) -> None:
        markup = keyboards.kb_admin_backups_settings_menu(enabled=True, interval_hours=12, keep_count=10, lang="en")
        rows = markup.inline_keyboard
        self.assertEqual([button.callback_data for button in rows[0]], ["menu:admin_backups_toggle"])
        self.assertIn(">⏱ 12 h<", [button.text for button in rows[1]])
        self.assertIn(">📚 Keep 10<", [button.text for button in rows[2]])

    def test_admin_settings_menu_groups_edit_and_toggle_actions(self) -> None:
        markup = keyboards.kb_admin_settings_menu(notify_enabled=True, telemetry_enabled=False, requests_enabled=True, lang="en")
        rows = markup.inline_keyboard
        self.assertEqual([button.callback_data for button in rows[0]], ["menu:admin_settings_bot_title", "menu:admin_settings_requests"])
        self.assertEqual([button.callback_data for button in rows[1]], ["menu:admin_settings_toggle_telemetry"])

    def test_admin_requests_settings_menu_groups_access_controls(self) -> None:
        markup = keyboards.kb_admin_requests_settings_menu(notify_enabled=True, requests_enabled=True, lang="en")
        rows = markup.inline_keyboard
        self.assertEqual([button.callback_data for button in rows[0]], ["menu:admin_settings_access_gate_message"])
        self.assertEqual([button.callback_data for button in rows[1]], ["menu:admin_settings_toggle_notify"])
        self.assertEqual([button.callback_data for button in rows[2]], ["menu:admin_settings_toggle_requests"])
        self.assertEqual([button.text for button in rows[1]], ["🔔 Request notifications: on"])
        self.assertEqual([button.text for button in rows[2]], ["📨 Access requests: on"])

    def test_request_card_text_uses_readable_sections(self) -> None:
        fake_users = {
            "42": {
                "username": "alice",
                "first_name": "Alice",
                "last_name": "Admin",
                "access_request_pending": True,
                "access_granted": False,
                "access_request_sent_at": "2026-04-02T00:00:00Z",
            }
        }
        with patch.object(user_profile.user_store, "read", return_value=fake_users):
            text, _markup = user_profile._render_request_card("42", "en")
        self.assertIn("*Requester*", text)
        self.assertIn("*Request state*", text)

    def test_requests_dashboard_hides_search_for_single_page(self) -> None:
        fake_users = {
            "42": {
                "username": "alice",
                "access_request_pending": True,
            }
        }
        with patch.object(user_profile.user_store, "read", return_value=fake_users):
            _text, markup = user_profile._render_requests_dashboard(["42"], 0, "en")
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertNotIn("menu:admin_requests_search", callbacks)

    def test_ssh_key_summary_uses_readable_sections(self) -> None:
        with patch.object(ssh_keys, "get_public_key", return_value=(True, "ssh-ed25519 AAAA test")):
            ok, text = ssh_keys.render_public_key_summary("en")
        self.assertTrue(ok)
        self.assertIn("🔐 *Bot SSH Key*", text)
        self.assertIn("*Status*", text)
        self.assertIn("*Path*", text)
        self.assertIn("*Next step*", text)

    def test_ssh_key_summary_escapes_markdown_sensitive_path(self) -> None:
        with patch.object(ssh_keys, "get_public_key", return_value=(True, "ssh-ed25519 AAAA test")), patch.object(
            ssh_keys, "get_ssh_public_key_path", return_value="/tmp/id_ed25519.pub"
        ):
            ok, text = ssh_keys.render_public_key_summary("en")
        self.assertTrue(ok)
        self.assertIn("/tmp/id\\_ed25519.pub", text)
        self.assertIn("`ssh-ed25519 AAAA test`", text)

    def test_advanced_menu_places_maintenance_after_protocol_sections(self) -> None:
        fake_server = SimpleNamespace(protocol_kinds=("xray", "awg"))
        with patch.object(admin_server_wizard, "get_server", return_value=fake_server):
            markup = admin_server_wizard._advanced_menu_markup("spb1", "en")
        rows = markup.inline_keyboard
        self.assertEqual([button.callback_data for button in rows[0]], ["srv:advsection:general:spb1", "srv:advsection:maintenance:spb1"])
        self.assertEqual([button.callback_data for button in rows[1]], ["srv:advsection:xray:spb1"])
        self.assertEqual([button.callback_data for button in rows[2]], ["srv:advsection:awg:spb1"])

    def test_advanced_menu_hides_protocol_sections_when_not_selected(self) -> None:
        fake_server = SimpleNamespace(protocol_kinds=("awg",))
        with patch.object(admin_server_wizard, "get_server", return_value=fake_server):
            markup = admin_server_wizard._advanced_menu_markup("spb1", "en")
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertNotIn("srv:advsection:xray:spb1", callbacks)
        self.assertIn("srv:advsection:awg:spb1", callbacks)

    def test_maintenance_section_groups_into_submenus(self) -> None:
        markup = admin_server_wizard._advanced_section_markup("spb1", "maintenance", "en")
        rows = markup.inline_keyboard
        self.assertEqual([button.callback_data for button in rows[0]], ["srv:action:metrics:spb1", "srv:advsection:maintenance_ports:spb1"])
        self.assertEqual([button.callback_data for button in rows[1]], ["srv:advsection:maintenance_runtime:spb1", "srv:advsection:maintenance_repair:spb1"])

    def test_maintenance_ports_section_groups_port_actions(self) -> None:
        markup = admin_server_wizard._advanced_section_markup("spb1", "maintenance_ports", "en")
        rows = markup.inline_keyboard
        self.assertEqual([button.callback_data for button in rows[0]], ["srv:action:checkports:spb1", "srv:action:openports:spb1"])
        self.assertEqual(rows[1][0].callback_data, "srv:advsection:maintenance:spb1")

    def test_maintenance_runtime_section_shows_sync_only_for_drift(self) -> None:
        with patch.object(admin_server_wizard, "get_server_runtime_state", return_value={"state": "unknown", "version": "", "commit": ""}):
            markup = admin_server_wizard._advanced_section_markup("spb1", "maintenance_runtime", "en")
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("srv:action:syncruntime:spb1", callbacks)

    def test_maintenance_runtime_section_hides_sync_when_current(self) -> None:
        with patch.object(admin_server_wizard, "get_server_runtime_state", return_value={"state": "up_to_date", "version": "0.3.1-alpha.1", "commit": "abc1234"}):
            markup = admin_server_wizard._advanced_section_markup("spb1", "maintenance_runtime", "en")
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertNotIn("srv:action:syncruntime:spb1", callbacks)
        self.assertEqual(callbacks, ["srv:advsection:maintenance:spb1"])

    def test_maintenance_repair_section_hides_xray_action_without_protocol(self) -> None:
        fake_server = SimpleNamespace(protocol_kinds=("awg",))
        with patch.object(admin_server_wizard, "get_server", return_value=fake_server):
            markup = admin_server_wizard._advanced_section_markup("spb1", "maintenance_repair", "en")
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("srv:action:syncenv:spb1", callbacks)
        self.assertIn("srv:action:reconcile:spb1", callbacks)
        self.assertNotIn("srv:action:syncxray:spb1", callbacks)

    def test_maintenance_repair_section_shows_xray_action_with_protocol(self) -> None:
        fake_server = SimpleNamespace(protocol_kinds=("xray",))
        with patch.object(admin_server_wizard, "get_server", return_value=fake_server):
            markup = admin_server_wizard._advanced_section_markup("spb1", "maintenance_repair", "en")
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("srv:action:syncxray:spb1", callbacks)

    def test_metrics_result_markup_returns_to_maintenance_screen(self) -> None:
        markup = admin_server_wizard._metrics_result_markup("spb1", "en")
        rows = markup.inline_keyboard
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0].callback_data, "srv:advsection:maintenance:spb1")

    def test_awg_entropy_result_markup_returns_to_awg_screen(self) -> None:
        markup = admin_server_wizard._awg_entropy_result_markup("spb1", "en")
        rows = markup.inline_keyboard
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0].callback_data, "srv:advsection:awg:spb1")

    def test_problem_server_card_opens_without_active_server_wizard(self) -> None:
        update = SimpleNamespace(
            callback_query=SimpleNamespace(message=SimpleNamespace(chat_id=1, message_id=2)),
            effective_user=None,
        )
        context = SimpleNamespace(user_data={})
        with patch.object(admin_server_wizard, "guard", return_value=True), patch.object(
            admin_server_wizard, "answer_cb"
        ), patch.object(
            admin_server_wizard, "_render_server_card"
        ) as render_card:
            admin_server_wizard.on_server_callback(update, context, "card:spb1")
        render_card.assert_called_once_with(context, "spb1")
        self.assertIn("server_wizard", context.user_data)

    def test_render_proto_keyboard_uses_save_label_in_edit_mode(self) -> None:
        fake_methods = [
            SimpleNamespace(code="gx", short_label="🇩🇪 Xray", server_key="de"),
            SimpleNamespace(code="ga", short_label="🇩🇪 AWG", server_key="de"),
        ]
        fake_server = SimpleNamespace(flag="🇩🇪", title="Germany")
        with patch.object(admin_views, "get_access_methods", return_value=fake_methods), patch.object(
            admin_views, "get_server", return_value=fake_server
        ):
            markup = admin_views.render_proto_keyboard(set(), lang="ru", editing=True)
        self.assertEqual(markup.inline_keyboard[-1][-1].text, "💾 Сохранить")
        self.assertEqual(markup.inline_keyboard[-1][-1].callback_data, "cfg:proto:done")

    def test_render_protocol_select_text_mentions_save_in_edit_mode(self) -> None:
        with patch.object(admin_views, "render_protocols_summary", return_value="summary"):
            text = admin_views.render_protocol_select_text("alice", {"gx"}, editing=True, lang="ru")
        self.assertIn("Сохранить", text)
        self.assertNotIn("Далее", text)

    def test_render_profile_card_compacts_fully_ready_provisioning(self) -> None:
        with patch.object(admin_views, "render_protocols_summary", return_value="• 🇱🇻 *Latvia*: AWG, Xray"), patch.object(
            admin_views, "render_profile_server_state_summary",
            return_value="• 🇱🇻 Latvia / AWG: ready\n• 🇱🇻 Latvia / Xray: ready",
        ):
            text, _markup = admin_views.render_profile_card("alice", {"awg_lv1", "xray_lv1"}, frozen=False, lang="en")
        self.assertIn("Provisioning:\n• All methods are ready", text)
        self.assertNotIn("Quick actions", text)

    def test_render_edit_menu_does_not_repeat_profile_summary(self) -> None:
        text, _markup = admin_views.render_edit_menu("alice", {"awg_lv1", "xray_lv1"}, frozen=False, lang="en")
        self.assertIn("✏️ Edit: `alice`", text)
        self.assertIn("• Status: *active*", text)
        self.assertNotIn("Access:", text)
        self.assertNotIn("Provisioning:", text)

    def test_render_getkey_server_menu_does_not_duplicate_methods_in_text(self) -> None:
        fake_methods = [
            SimpleNamespace(getkey_payload="xray:de", short_label="🇩🇪 Xray"),
            SimpleNamespace(getkey_payload="awg:de", short_label="🇩🇪 AWG"),
        ]
        fake_server = SimpleNamespace(flag="🇩🇪", title="Germany")
        with patch.object(user_views, "get_server", return_value=fake_server):
            text, items = user_views.render_server_menu("de", fake_methods, lang="en")
        self.assertEqual(text, "🇩🇪 Germany\n\nChoose a connection method.")
        self.assertEqual(items, [("xray:de", "Xray"), ("awg:de", "AWG")])

    def test_render_getkey_overview_does_not_duplicate_servers_in_text(self) -> None:
        fake_methods = [
            SimpleNamespace(server_key="spb", short_label="🇷🇺 AWG"),
            SimpleNamespace(server_key="spb", short_label="🇷🇺 Xray"),
        ]
        fake_server = SimpleNamespace(flag="🇷🇺", title="Saint-Petersburg")
        with patch.object(user_views, "get_server", return_value=fake_server):
            text, items = user_views.render_getkey_overview(fake_methods, lang="en")
        self.assertEqual(text, "🔑 *Get Key*\n\nChoose a server to connect.")
        self.assertEqual(items, [("spb", "🇷🇺 Saint-Petersburg · 2 methods")])

    def test_render_problem_servers_uses_server_key_placeholder_without_crashing(self) -> None:
        fake_server = SimpleNamespace(
            key="spb1",
            flag="🇷🇺",
            title="Saint-Petersburg",
            enabled=True,
            bootstrap_state="new",
            protocol_kinds=["xray"],
        )
        with patch.object(user_profile, "_problem_server_keys", return_value=["spb1"]), patch.object(
            user_profile, "list_servers", return_value=[fake_server]
        ):
            text, markup = user_profile._render_problem_servers("en")
        self.assertIn("Saint-Petersburg (spb1)", text)
        self.assertEqual(markup.inline_keyboard[0][0].text, "🇷🇺 Saint-Petersburg")

    def test_problem_servers_include_runtime_drift_reason(self) -> None:
        fake_server = SimpleNamespace(
            key="spb1",
            flag="🇷🇺",
            title="Saint-Petersburg",
            enabled=True,
            bootstrap_state="bootstrapped",
            protocol_kinds=["xray"],
        )
        with patch.object(user_profile, "_problem_server_keys", return_value=["spb1"]), patch.object(
            user_profile, "list_servers", return_value=[fake_server]
        ), patch.object(
            user_profile, "get_server_runtime_state", return_value={"state": "unknown"}
        ):
            text, _markup = user_profile._render_problem_servers("en")
        self.assertIn("runtime sync needed", text)

    def test_admin_status_shows_runtime_sync_button_when_drift_exists(self) -> None:
        with patch.object(user_profile, "_all_pending_request_ids", return_value=[]), patch.object(
            user_profile, "_problem_server_keys", return_value=[]
        ), patch.object(user_profile, "_runtime_drift_server_keys", return_value=["spb1"]):
            markup = user_profile._kb_admin_status("en")
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("menu:admin_runtime_sync_all", callbacks)

    def test_runtime_sync_confirm_lists_servers_and_action(self) -> None:
        fake_server = SimpleNamespace(key="spb1", flag="🇷🇺", title="Saint-Petersburg")
        with patch.object(user_profile, "get_servers_needing_runtime_sync", return_value=[fake_server]):
            text, markup = user_profile._render_runtime_sync_confirm("en")
        self.assertIn("Saint-Petersburg (spb1)", text)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "menu:admin_runtime_sync_run")

    def test_runtime_sync_confirm_can_return_to_updates(self) -> None:
        fake_server = SimpleNamespace(key="spb1", flag="🇷🇺", title="Saint-Petersburg")
        with patch.object(user_profile, "get_servers_needing_runtime_sync", return_value=[fake_server]):
            _text, markup = user_profile._render_runtime_sync_confirm("en", back_callback="menu:admin_updates")
        self.assertEqual(markup.inline_keyboard[1][0].callback_data, "menu:admin_updates")

    def test_maintenance_section_includes_full_cleanup(self) -> None:
        markup = admin_server_wizard._advanced_section_markup("spb1", "maintenance", "en")
        buttons = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("🧨 Full Cleanup", buttons)

    def test_maintenance_runtime_text_shows_runtime_state(self) -> None:
        fake_server = SimpleNamespace(flag="🇷🇺", title="Saint-Petersburg", key="spb1")
        with patch.object(admin_server_wizard, "get_server_runtime_state", return_value={"state": "unknown", "version": "", "commit": ""}):
            text = admin_server_wizard._advanced_section_text(fake_server, "maintenance_runtime", "en")
        self.assertIn("Runtime", text)
        self.assertIn("state: legacy/unknown", text)

    def test_advanced_section_for_port_fields_routes_back_to_protocol_sections(self) -> None:
        self.assertEqual(admin_server_wizard._advanced_section_for_field("xray_tcp_port"), "xray")
        self.assertEqual(admin_server_wizard._advanced_section_for_field("xray_xhttp_port"), "xray")
        self.assertEqual(admin_server_wizard._advanced_section_for_field("awg_port"), "awg")

    def test_server_card_omits_empty_next_step_section(self) -> None:
        fake_server = SimpleNamespace(
            key="spb1",
            flag="🇷🇺",
            title="Saint-Petersburg",
            enabled=True,
            bootstrap_state="bootstrapped",
            protocol_kinds=(),
            transport="ssh",
            public_host="example.com",
            xray_tcp_port=443,
            xray_xhttp_port=8443,
            awg_port=51820,
            awg_iface="wg0",
            notes="",
        )
        with patch.object(admin_server_wizard, "get_server_runtime_state", return_value={"state": "up_to_date"}), patch.object(
            admin_server_wizard, "_server_status", return_value=("✅", "ready")
        ), patch.object(
            admin_server_wizard, "_xray_status", return_value=("—", "disabled")
        ), patch.object(
            admin_server_wizard, "_awg_status", return_value=("—", "disabled")
        ), patch.object(
            admin_server_wizard, "_server_overall_status", return_value=("✅", "ready")
        ), patch.object(
            admin_server_wizard, "summarize_server_provisioning",
            return_value={"overall": "ok", "total": 0, "by_status": {"provisioned": 0, "failed": 0, "needs_attention": 0}},
        ):
            text = admin_server_wizard._server_card_text(fake_server, "en")
        self.assertNotIn("Next step", text)
        self.assertNotIn("nothing required", text)
        self.assertIn("Runtime", text)
        self.assertIn("Services", text)
        self.assertIn("Profiles", text)
        self.assertIn("No assigned profiles", text)

    def test_server_dashboard_text_is_summary_only(self) -> None:
        fake_servers = [
            SimpleNamespace(key="lv1", flag="🇱🇻", title="Latvia", enabled=True),
            SimpleNamespace(key="test", flag="🏳️", title="Test Server", enabled=True),
        ]
        with patch.object(admin_server_wizard, "_server_overall_status", side_effect=[("✅", "ready"), ("⚠️", "needs attention")]):
            text = admin_server_wizard._server_dashboard_text(fake_servers, "en")
        self.assertIn("Active: 2 / Total: 2", text)
        self.assertIn("Need attention: 1", text)
        self.assertNotIn("Latvia (lv1)", text)
        self.assertNotIn("Test Server (test)", text)

    def test_server_dashboard_buttons_use_quiet_ready_marker_and_problem_icons(self) -> None:
        fake_servers = [
            SimpleNamespace(key="lv1", flag="🇱🇻", title="Latvia"),
            SimpleNamespace(key="test", flag="🏳️", title="Test Server"),
        ]
        with patch.object(admin_server_wizard, "_server_overall_status", side_effect=[("✅", "ready"), ("⚠️", "needs attention")]), patch.object(
            admin_server_wizard,
            "summarize_server_provisioning",
            side_effect=[
                {"total": 0, "by_status": {"provisioned": 0, "failed": 0, "needs_attention": 0}},
                {"total": 4, "by_status": {"provisioned": 4, "failed": 0, "needs_attention": 0}},
            ],
        ):
            markup = admin_server_wizard._server_dashboard_markup(fake_servers, "en")
        labels = [row[0].text for row in markup.inline_keyboard[:2]]
        self.assertEqual(labels[0], "🇱🇻 Latvia ·")
        self.assertEqual(labels[1], "🏳️ Test Server ⚠️")

    def test_full_cleanup_menu_shows_ssh_key_option_for_ssh_servers(self) -> None:
        fake_server = SimpleNamespace(key="spb1", flag="🇷🇺", title="Saint-Petersburg", transport="ssh")
        markup = admin_server_wizard._full_cleanup_markup(fake_server, "en")
        buttons = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("Clean up node", buttons)
        self.assertIn("Clean up node + remove SSH key", buttons)

    def test_admin_settings_menu_includes_full_reset(self) -> None:
        from utils.keyboards import kb_admin_settings_menu

        markup = kb_admin_settings_menu(True, True, True, "en")
        buttons = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("🧨 Full Reset", buttons)
        self.assertIn("📨 Requests", buttons)


if __name__ == "__main__":
    unittest.main()
