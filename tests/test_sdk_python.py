"""The Python SDK speaks the contract's rules against a real HTTP server."""
import unittest

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from node_plane_sdk import ApiError, Client, collect


class SdkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = []

        async def echo(request):
            self.seen.append(dict(request.headers))
            return web.json_response({'ok': True, 'body': await request.json() if request.can_read_body else None},
                                     headers={'X-Request-ID': 'r1'})

        async def refused(request):
            return web.json_response({'error': {'code': 'revision_conflict', 'message': 'revision_conflict',
                                                'request_id': 'r2', 'details': {}}},
                                     status=412, headers={'X-Request-ID': 'r2'})

        async def not_json(request):
            return web.Response(text='<html>proxy error</html>', status=502)

        async def pages(request):
            cursor = request.query.get('cursor')
            if request.query.get('loop'):
                return web.json_response({'items': [1], 'next_cursor': 'again'})
            return web.json_response({'items': [1, 2], 'next_cursor': 'c2'} if cursor is None
                                     else {'items': [3], 'next_cursor': None})

        app = web.Application()
        app.router.add_route('*', '/echo', echo)
        app.router.add_get('/refused', refused)
        app.router.add_get('/not-json', not_json)
        app.router.add_get('/pages', pages)
        self.server = TestServer(app)
        await self.server.start_server()
        self.session = aiohttp.ClientSession()
        self.timings = []
        self.client = Client(self.session, str(self.server.make_url('/')), 'secret',
                             on_request=lambda *args: self.timings.append(args))

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()

    async def test_headers_follow_the_contract(self):
        result = await self.client.request('PATCH', '/echo', telegram_user_id=42, body={'a': 1},
                                           command=True, command_key='k1', revision=7)
        self.assertEqual(result, {'ok': True, 'body': {'a': 1}})
        headers = self.seen[0]
        self.assertEqual(headers['Authorization'], 'Bearer secret')
        self.assertEqual(headers['X-Node-Plane-Telegram-User-ID'], '42')
        self.assertEqual(headers['Idempotency-Key'], 'k1')
        self.assertEqual(headers['If-Match'], '"7"')
        await self.client.request('GET', '/echo')
        self.assertFalse({'Idempotency-Key', 'If-Match', 'X-Node-Plane-Telegram-User-ID'} & set(self.seen[1]))
        await self.client.request('POST', '/echo', command=True)
        self.assertTrue(self.seen[2]['Idempotency-Key'])
        self.assertEqual([timing[3] for timing in self.timings], ['r1', 'r1', 'r1'])

    async def test_errors_carry_the_contract_code(self):
        with self.assertRaises(ApiError) as refused:
            await self.client.request('GET', '/refused')
        self.assertEqual((refused.exception.code, refused.exception.status, refused.exception.request_id),
                         ('revision_conflict', 412, 'r2'))
        with self.assertRaises(ApiError) as unreadable:
            await self.client.request('GET', '/not-json')
        self.assertEqual((unreadable.exception.code, unreadable.exception.status), ('backend_unavailable', 503))
        await self.server.close()
        with self.assertRaises(ApiError) as down:
            await self.client.request('GET', '/echo')
        self.assertEqual(down.exception.status, 503)

    async def test_pages_are_collected_and_a_cursor_loop_stops(self):
        fetch = lambda cursor: self.client.request('GET', '/pages' + (f'?cursor={cursor}' if cursor else ''))
        self.assertEqual(await collect(fetch), [1, 2, 3])
        with self.assertRaises(ApiError) as loop:
            await collect(lambda cursor: self.client.request('GET', '/pages?loop=1'))
        self.assertEqual(loop.exception.code, 'invalid_pagination')


if __name__ == '__main__':
    unittest.main()
