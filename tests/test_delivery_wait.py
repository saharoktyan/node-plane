import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, patch

from backend.delivery_wakeup import DeliveryWakeup
from telegram_client.announcement_delivery import delivery_loop


class WakeupTests(unittest.TestCase):
    def test_signal_between_queue_check_and_wait_is_not_lost(self):
        wake = DeliveryWakeup('unused')
        generation = wake.snapshot()
        wake.notify()
        self.assertTrue(wake.wait(generation, 0))
        self.assertFalse(wake.wait(wake.snapshot(), 0))

    def test_signal_wakes_all_waiters(self):
        wake = DeliveryWakeup('unused')
        snapshot = wake.snapshot()
        threads = [threading.Thread(target=lambda: wake.wait(snapshot, 2)) for _ in range(3)]
        for thread in threads:
            thread.start()
        wake.notify()
        for thread in threads:
            thread.join(1)
            self.assertFalse(thread.is_alive())


class DeliveryLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_queues_wait_instead_of_sleep_and_cancellation_propagates(self):
        backend = AsyncMock()
        backend.wait_deliveries.side_effect = asyncio.CancelledError
        with patch('telegram_client.announcement_delivery.deliver_one', new=AsyncMock(return_value=False)), \
             patch('telegram_client.access_decision_delivery.deliver_one', new=AsyncMock(return_value=False)), \
             patch('telegram_client.alert_delivery.deliver_one', new=AsyncMock(return_value=False)), \
             patch('telegram_client.announcement_delivery.asyncio.sleep', new=AsyncMock()) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await delivery_loop(None, backend)
        backend.wait_deliveries.assert_awaited_once()
        sleep.assert_not_awaited()

    async def test_failed_wait_uses_periodic_fallback(self):
        backend = AsyncMock()
        backend.wait_deliveries.side_effect = OSError('API restart')
        with patch('telegram_client.announcement_delivery.deliver_one', new=AsyncMock(return_value=False)), \
             patch('telegram_client.access_decision_delivery.deliver_one', new=AsyncMock(return_value=False)), \
             patch('telegram_client.alert_delivery.deliver_one', new=AsyncMock(return_value=False)), \
             patch('telegram_client.announcement_delivery.asyncio.sleep', new=AsyncMock(side_effect=asyncio.CancelledError)) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await delivery_loop(None, backend)
        sleep.assert_awaited_once_with(5)
