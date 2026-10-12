import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from telegram_client.timings import ActionTimings, CURRENT, TelegramTimings, record, route
from time import perf_counter


class TimingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_actions_are_isolated_and_do_not_log_payloads(self):
        seen = []
        async def handler(event, data):
            seen.append(CURRENT.get().id)
            await asyncio.sleep(0)
            record('backend', perf_counter(), 'GET /api/v1/me')
        event = SimpleNamespace(callback_query=SimpleNamespace(data='profile:private-payload'))
        with patch('telegram_client.timings.enabled', return_value=True), self.assertLogs('telegram_client.timings', 'INFO') as logs:
            await asyncio.gather(*(ActionTimings()(handler, event, {}) for _ in range(2)))
        self.assertEqual(len(set(seen)), 2)
        self.assertIsNone(CURRENT.get())
        self.assertNotIn('private-payload', '\n'.join(logs.output))
        self.assertEqual(sum('stage=action' in line for line in logs.output), 2)

    async def test_failure_is_measured_without_changing_exception(self):
        async def handler(event, data):
            raise ValueError('private-input')
        with patch('telegram_client.timings.enabled', return_value=True), self.assertLogs('telegram_client.timings', 'INFO') as logs:
            with self.assertRaises(ValueError):
                await ActionTimings()(handler, SimpleNamespace(callback_query=None), {})
        self.assertIn('outcome=error', logs.output[0])
        self.assertNotIn('private-input', logs.output[0])
        self.assertIsNone(CURRENT.get())

    async def test_polling_is_excluded_and_send_is_measured(self):
        async def request(bot, method):
            return 'ok'
        middleware = TelegramTimings()
        with patch('telegram_client.timings.enabled', return_value=True), patch('telegram_client.timings.record') as metric:
            await middleware(request, None, type('GetUpdates', (), {})())
            metric.assert_not_called()
            self.assertEqual(await middleware(request, None, type('SendMessage', (), {})()), 'ok')
            self.assertEqual(metric.call_args.args[2], 'SendMessage')

    def test_route_omits_query_and_uuid(self):
        self.assertEqual(route('/api/v1/profiles/12345678-1234-1234-1234-123456789abc?secret=value'),
                         '/api/v1/profiles/:id')
