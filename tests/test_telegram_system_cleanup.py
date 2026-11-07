import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from telegram_client.i18n import CATALOG
from telegram_client.routers import admin_system_cleanup as cleanup
from tests import test_telegram_client as fixture


class CleanupScreensTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture.TelegramFlowTests.setUp(self)
        self.job_id = str(uuid4())
        self.job = {
            "id": self.job_id,
            "status": "awaiting_shutdown",
            "phase": "shutdown",
            "items": [],
            "backup_id": None,
            "error_code": None,
        }
        self.backend = SimpleNamespace(
            system_cleanup_job=AsyncMock(return_value=self.job),
            system_cleanup_action=AsyncMock(),
        )

    async def test_shutdown_ack_requires_delivered_final_screen(self):
        self.query.data = "sc_shutdown:" + self.job_id
        with patch.object(
            cleanup, "render", AsyncMock(side_effect=RuntimeError("Telegram offline"))
        ):
            with self.assertRaises(RuntimeError):
                await cleanup.cleanup_action(
                    self.query, self.bot, self.backend, self.state
                )
        self.backend.system_cleanup_action.assert_not_awaited()
        with patch.object(cleanup, "render", AsyncMock()):
            await cleanup.cleanup_action(self.query, self.bot, self.backend, self.state)
        self.backend.system_cleanup_action.assert_awaited_once_with(
            123, self.job_id, "shutdown-ack",
            notification={"message_id": self.query.message.message_id, "locale": "en"}
        )

    async def test_shutdown_buttons_and_all_screens_are_localized(self):
        for locale in ("en", "ru"):
            self.state_data["locale"] = locale
            with patch.object(cleanup, "render", AsyncMock()) as draw:
                await cleanup.show_job(
                    123, 123, 77, self.job_id, self.bot, self.backend, self.state
                )
            rows = draw.call_args.args[3]
            self.assertEqual(
                [b.callback_data for b in rows[-1]],
                ["system_cleanup", "sc_shutdown:" + self.job_id],
            )
            self.assertNotIn("system_cleanup.", str(draw.call_args.args[2].lines))
        en = {key for key in CATALOG["en"] if key.startswith("system_cleanup.")}
        ru = {key for key in CATALOG["ru"] if key.startswith("system_cleanup.")}
        self.assertEqual(en, ru)

    async def test_running_cleanup_refreshes_but_terminal_status_stops(self):
        self.job.update(status='running', phase='nodes')
        with patch.object(cleanup, 'render', AsyncMock()), \
                patch.object(cleanup, 'schedule_refresh') as refresh:
            await cleanup.show_job(123, 123, 77, self.job_id, self.bot, self.backend, self.state)
            refresh.assert_called_once()
        self.job.update(status='succeeded', phase='complete')
        with patch.object(cleanup, 'render', AsyncMock()), \
                patch.object(cleanup, 'schedule_refresh') as refresh:
            await cleanup.show_job(123, 123, 77, self.job_id, self.bot, self.backend, self.state)
            refresh.assert_not_called()

    async def test_phrase_mismatch_does_not_submit_operation(self):
        self.state_data.update(
            locale="en",
            control_message_id=77,
            system_cleanup_draft={
                "key": str(uuid4()),
                "plan": {
                    "id": str(uuid4()),
                    "action": "reset",
                    "cleanup_nodes": False,
                    "confirmation_phrase": "RESET NODE PLANE",
                    "deployment": {
                        "base_dir": "/opt/node-plane",
                        "shared_dir": "/opt/node-plane/shared",
                    },
                },
            },
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            chat=SimpleNamespace(id=123, type="private"),
            text="yes",
            delete=AsyncMock(),
        )
        self.backend.system_cleanup_command = AsyncMock()
        with patch.object(cleanup, "render", AsyncMock()):
            await cleanup.cleanup_phrase(message, self.bot, self.backend, self.state)
        self.backend.system_cleanup_command.assert_not_awaited()

    async def test_cleanup_observer_renders_multiple_phases_without_manual_refresh(self):
        import asyncio
        from telegram_client.routers import common
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.memory import MemoryStorage
        from aiogram.fsm.storage.base import StorageKey
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=123, user_id=123))
        await state.update_data(locale='en', control_message_id=77)
        bot = SimpleNamespace(edit_message_text=AsyncMock(return_value=SimpleNamespace(rich_message=None)))
        phases = [('queued', 'backup'), ('running', 'nodes'), ('running', 'uninstall_prepare'), ('awaiting_shutdown', 'shutdown')]
        self.backend.system_cleanup_job.side_effect = [dict(self.job, status=status, phase=phase) for status, phase in phases]
        original = common.schedule_refresh
        def fast_refresh(state, refresh):
            original(state, refresh, interval=.001)
        try:
            with patch.object(cleanup, 'schedule_refresh', side_effect=fast_refresh):
                await cleanup.show_job(123, 123, 77, self.job_id, bot, self.backend, state)
                for _ in range(100):
                    await asyncio.sleep(.005)
                    if not common._REFRESH_TASKS:
                        break
            self.assertEqual(self.backend.system_cleanup_job.await_count, 4)
            self.assertEqual(bot.edit_message_text.await_count, 4)
            self.assertFalse(common._REFRESH_TASKS)
            self.backend.system_cleanup_action.assert_not_awaited()
        finally:
            await common.shutdown_refreshes()

    async def test_cleanup_recovers_after_render_error_without_repeating_cleanup(self):
        import asyncio
        from telegram_client.routers import common
        self.job.update(status='running', phase='nodes')
        finished = asyncio.Event()
        reads = 0
        async def read(*args):
            nonlocal reads
            reads += 1
            return dict(self.job, status='awaiting_shutdown' if reads >= 3 else 'running')
        async def draw(*args):
            if reads == 2:
                raise RuntimeError('temporary render failure')
            if reads >= 3:
                finished.set()
        self.backend.system_cleanup_job.side_effect = read
        original = common.schedule_refresh
        def fast_refresh(state, refresh):
            original(state, refresh, interval=.001)
        try:
            with patch.object(cleanup, 'schedule_refresh', side_effect=fast_refresh), patch.object(cleanup, 'render', side_effect=draw):
                await cleanup.show_job(123, 123, 77, self.job_id, self.bot, self.backend, self.state)
                await asyncio.wait_for(finished.wait(), 1)
            self.assertEqual(reads, 3)
            self.backend.system_cleanup_action.assert_not_awaited()
        finally:
            await common.shutdown_refreshes()
