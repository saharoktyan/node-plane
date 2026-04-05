from __future__ import annotations

import importlib
import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.postgres_test_harness import configure_postgres_test_env

TESTS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
APP_ROOT = os.path.join(REPO_ROOT, "app")
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class _InlineKeyboardButton:
    def __init__(self, text: str, callback_data: str):
        self.text = text
        self.callback_data = callback_data


class _InlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


telegram_module = types.ModuleType("telegram")
telegram_module.Update = object
telegram_module.InlineKeyboardButton = _InlineKeyboardButton
telegram_module.InlineKeyboardMarkup = _InlineKeyboardMarkup
telegram_error_module = types.ModuleType("telegram.error")
telegram_error_module.BadRequest = Exception
telegram_error_module.RetryAfter = Exception
telegram_ext_module = types.ModuleType("telegram.ext")
telegram_ext_module.CallbackContext = object
sys.modules.setdefault("telegram", telegram_module)
sys.modules["telegram"] = telegram_module
sys.modules["telegram.error"] = telegram_error_module
sys.modules["telegram.ext"] = telegram_ext_module


class _FakeMessage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.chat_id = 501
        self.message_id = 777

    def reply_text(self, text: str, **kwargs):
        self.calls.append((text, kwargs))


class FirstStartLanguageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = self.tmpdir.name
        configure_postgres_test_env(base)
        os.environ["ADMIN_IDS"] = ""

        import config
        import services.app_settings as app_settings
        import services.profile_state as profile_state
        import handlers.user_common as user_common
        import handlers.user_profile as user_profile

        self.config = importlib.reload(config)
        self.app_settings = importlib.reload(app_settings)
        self.profile_state = importlib.reload(profile_state)
        self.user_common = importlib.reload(user_common)
        self.user_profile = importlib.reload(user_profile)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_first_start_shows_language_then_continues_to_access_gate(self) -> None:
        message = _FakeMessage()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=501, username="newbie", first_name="New", last_name="User", language_code="en"),
            effective_chat=SimpleNamespace(id=501),
            effective_message=message,
        )
        context = SimpleNamespace(user_data={})

        self.user_common.start_cmd(update, context)

        self.assertTrue(context.user_data.get("start_language_gate_pending"))
        self.assertEqual(len(message.calls), 1)
        self.assertIn("Language", message.calls[0][0])
        self.assertIn("Язык", message.calls[0][0])
        buttons = message.calls[0][1]["reply_markup"].inline_keyboard
        self.assertEqual(buttons[0][0].text, "Русский")
        self.assertEqual(buttons[1][0].text, "English")

        callback_update = SimpleNamespace(
            effective_user=update.effective_user,
            effective_chat=update.effective_chat,
            callback_query=SimpleNamespace(
                message=message,
                data="menu:setlangstart:en",
                answer=lambda *args, **kwargs: None,
            ),
        )

        with patch.object(self.user_common, "kb_main_menu", return_value="main-menu-en"), patch.object(
            self.user_profile, "safe_edit_message"
        ) as safe_edit, patch.object(self.user_profile, "answer_cb"):
            self.user_profile.on_menu_callback(callback_update, context, "setlangstart:en")

        self.assertFalse(context.user_data.get("start_language_gate_pending"))
        safe_edit.assert_called_once()
        self.assertIn("You do not have access to the bot yet", safe_edit.call_args.args[2])
        self.assertEqual(safe_edit.call_args.kwargs["reply_markup"], "main-menu-en")

    def test_first_start_language_selection_continues_without_pending_flag(self) -> None:
        message = _FakeMessage()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=502, username="newbie2", first_name="New", last_name="User", language_code="en"),
            effective_chat=SimpleNamespace(id=502),
            effective_message=message,
        )
        context = SimpleNamespace(user_data={})

        self.user_common.start_cmd(update, context)
        context.user_data.clear()

        callback_update = SimpleNamespace(
            effective_user=update.effective_user,
            effective_chat=update.effective_chat,
            callback_query=SimpleNamespace(
                message=message,
                data="menu:setlangstart:en",
                answer=lambda *args, **kwargs: None,
            ),
        )

        with patch.object(self.user_common, "kb_main_menu", return_value="main-menu-en"), patch.object(
            self.user_profile, "safe_edit_message"
        ) as safe_edit, patch.object(self.user_profile, "answer_cb"):
            self.user_profile.on_menu_callback(callback_update, context, "setlangstart:en")

        safe_edit.assert_called_once()
        self.assertIn("You do not have access to the bot yet", safe_edit.call_args.args[2])
        self.assertEqual(safe_edit.call_args.kwargs["reply_markup"], "main-menu-en")

    def test_settings_language_selection_stays_on_language_screen(self) -> None:
        user_id = 503
        self.profile_state.user_store.upsert_user(
            user_id,
            chat_id=503,
            username="languser",
            locale="ru",
            locale_explicitly_selected=True,
        )
        context = SimpleNamespace(user_data={})
        callback_update = SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id, username="languser", first_name="Lang", last_name="User", language_code="ru"),
            effective_chat=SimpleNamespace(id=503),
            callback_query=SimpleNamespace(
                message=_FakeMessage(),
                data="menu:setlang:en",
                answer=lambda *args, **kwargs: None,
            ),
        )

        with patch.object(self.user_profile, "safe_edit_message") as safe_edit, patch.object(self.user_profile, "answer_cb"):
            self.user_profile.on_menu_callback(callback_update, context, "setlang:en")

        safe_edit.assert_called_once()
        self.assertEqual(safe_edit.call_args.args[2], "🌐 Language\n\nChoose the interface language.")


if __name__ == "__main__":
    unittest.main()
