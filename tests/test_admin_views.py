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

    def test_admin_backups_menu_groups_primary_actions(self) -> None:
        markup = keyboards.kb_admin_backups_menu(lang="en")
        rows = markup.inline_keyboard
        self.assertEqual([button.callback_data for button in rows[0]], ["menu:admin_backups_create", "menu:admin_backups_restore:0"])
        self.assertEqual([button.callback_data for button in rows[1]], ["menu:admin_backups_settings"])

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
        markup = admin_server_wizard._advanced_menu_markup("spb1", "en")
        rows = markup.inline_keyboard
        self.assertEqual([button.callback_data for button in rows[0]], ["srv:advsection:general:spb1", "srv:advsection:xray:spb1"])
        self.assertEqual([button.callback_data for button in rows[1]], ["srv:advsection:awg:spb1", "srv:advsection:maintenance:spb1"])

    def test_maintenance_section_groups_safe_actions_before_sync(self) -> None:
        markup = admin_server_wizard._advanced_section_markup("spb1", "maintenance", "en")
        rows = markup.inline_keyboard
        self.assertEqual([button.callback_data for button in rows[0]], ["srv:action:metrics:spb1", "srv:action:checkports:spb1"])
        self.assertEqual([button.callback_data for button in rows[1]], ["srv:action:openports:spb1", "srv:action:reconcile:spb1"])
        self.assertEqual([button.callback_data for button in rows[2]], ["srv:action:syncenv:spb1", "srv:action:syncxray:spb1"])

    def test_metrics_result_markup_returns_to_maintenance_screen(self) -> None:
        markup = admin_server_wizard._metrics_result_markup("spb1", "en")
        rows = markup.inline_keyboard
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0].callback_data, "srv:advsection:maintenance:spb1")

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

    def test_maintenance_section_includes_full_cleanup(self) -> None:
        markup = admin_server_wizard._advanced_section_markup("spb1", "maintenance", "en")
        buttons = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("🧨 Full Cleanup", buttons)

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
