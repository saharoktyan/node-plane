import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from telegram_client.routers import common, admin_node_tools, admin_nodes, admin_updates


class AutoRefreshTests(IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await common.shutdown_refreshes()

    async def test_refresh_observes_once_and_navigation_cancels_inflight_read(self):
        state = SimpleNamespace()
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def read():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        common.schedule_refresh(state, read, interval=0)
        await asyncio.wait_for(started.wait(), 1)
        common.cancel_refresh(state)
        await asyncio.wait_for(cancelled.wait(), 1)
        self.assertFalse(common._REFRESH_TASKS)

    async def test_navigation_can_cancel_observer_during_its_own_render(self):
        state=SimpleNamespace()
        rendering,cancelled=asyncio.Event(),asyncio.Event()
        async def refresh():
            common.cancel_refresh(state)  # render starts this way
            rendering.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        common.schedule_refresh(state,refresh,interval=0)
        await asyncio.wait_for(rendering.wait(),1)
        common.cancel_refresh(state)
        await asyncio.wait_for(cancelled.wait(),1)

    async def test_only_newest_observer_runs_and_panels_are_independent(self):
        first, second = SimpleNamespace(), SimpleNamespace()
        obsolete, active, other = AsyncMock(), AsyncMock(), AsyncMock()
        common.schedule_refresh(first, obsolete, interval=0)
        common.schedule_refresh(first, active, interval=0)
        common.schedule_refresh(second, other, interval=0)
        await asyncio.sleep(.01)
        obsolete.assert_not_awaited()
        active.assert_awaited_once()
        other.assert_awaited_once()

    async def test_notification_does_not_cancel_main_panel_observer(self):
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.memory import MemoryStorage
        from aiogram.fsm.storage.base import StorageKey
        storage=MemoryStorage()
        main=FSMContext(storage=storage,key=StorageKey(bot_id=1,chat_id=123,user_id=123))
        await main.update_data(locale='en')
        common.schedule_refresh(main,AsyncMock(),interval=30)
        event=SimpleNamespace(callback_query=SimpleNamespace(data='notification_review:r',
            message=SimpleNamespace(message_id=99),from_user=SimpleNamespace(language_code='en')))
        handler=AsyncMock()
        await common.NotificationStateMiddleware()(handler,event,{'state':main})
        self.assertIn(common._refresh_key(main),common._REFRESH_TASKS)
        self.assertNotEqual(handler.call_args.args[1]['state'].key,main.key)

    async def test_progress_read_renders_completed_job_without_replaying_action(self):
        state=SimpleNamespace(get_data=AsyncMock(return_value={'locale':'en'}),update_data=AsyncMock())
        backend=SimpleNamespace(node_job=AsyncMock(return_value={'id':'job','node_key':'lv1','action':'bootstrap','status':'succeeded'}))
        bot=SimpleNamespace()
        with patch.object(admin_node_tools,'render',new_callable=AsyncMock) as draw, patch.object(admin_node_tools,'schedule_refresh') as schedule:
            await admin_node_tools.show_job(123,123,77,{'id':'job','node_key':'lv1','action':'bootstrap','status':'running'},bot,state,backend)
            refresh=schedule.call_args.args[1]
            await refresh()
        backend.node_job.assert_awaited_once_with(123,'job')
        self.assertEqual(schedule.call_count,1)
        self.assertIn('installed',draw.call_args.args[2].plain().lower())
