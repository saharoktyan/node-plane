from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from telegram_client.routers import admin_temporary_configs as screens
from telegram_client.backend import BackendError
from tests import test_telegram_client as fixtures

class TemporaryScreensTests(IsolatedAsyncioTestCase):
    setUp = fixtures.TelegramFlowTests.setUp

    def node(self, protocols=('awg',), transports=()):
        return dict(key='lv1',title='Latvia #1',region='Europe',flag='🇱🇻',enabled=True,
                    desired_revision=1,applied_revision=1,protocols=list(protocols),xray_transports=list(transports))

    async def click(self, action, backend):
        self.query.data='temporary:'+action
        with patch.object(screens,'render',new_callable=AsyncMock,return_value=True) as draw:
            await screens.temporary_cb(self.query,self.bot,backend,self.state)
        return draw

    async def test_awg_skips_single_protocol_and_back_returns_to_server(self):
        backend=SimpleNamespace(request=AsyncMock(return_value={'items':[self.node()],'next_cursor':None}))
        await self.click('new',backend)
        draw=await self.click('node:0',backend)
        self.assertEqual(self.state_data['temporary_step'],'duration')
        rows=draw.call_args.args[3]
        self.assertEqual(rows[-1][0].callback_data,'temporary:server')
        await self.click('duration:43200',backend)
        draft=self.state_data['temporary_draft']
        self.assertEqual((draft['protocol'],draft['transport'],draft['duration_seconds']),('awg','vpn',43200))
        await self.click('duration',backend)
        self.assertEqual(self.state_data['temporary_step'],'duration')

    async def test_two_transports_show_choice_and_vless_notice_on_confirmation(self):
        backend=SimpleNamespace(request=AsyncMock(return_value={'items':[self.node(('xray',),('tcp','xhttp'))],'next_cursor':None}))
        await self.click('new',backend)
        await self.click('node:0',backend)
        self.assertEqual(self.state_data['temporary_step'],'transport')
        await self.click('transport:xhttp',backend)
        draw=await self.click('duration:259200',backend)
        screen,rows=draw.call_args.args[2:4]
        self.assertIn('Existing connections',screen.plain())
        self.assertEqual(rows[0][0].style,'primary')
        key=self.state_data['temporary_draft']['command_key']
        await self.click('duration',backend)
        await self.click('duration:86400',backend)
        self.assertEqual(self.state_data['temporary_draft']['command_key'],key)

    async def test_card_retains_files_and_ios_message_does_not_replace_card(self):
        identity=str(uuid4())
        card=dict(id=identity,node_key='lv1',protocol='xray',transport='tcp',status='active',expires_at='2026-10-12T12:00:00+00:00')
        artifact=dict(content='vless://secret',files=[dict(filename='Temporary VLESS.txt',content='vless://secret')])
        backend=SimpleNamespace(request=AsyncMock(side_effect=[card,artifact,artifact]))
        draw=await self.click('show:'+identity,backend)
        screen,rows=draw.call_args.args[2:4]
        self.assertTrue(screen.files)
        self.assertEqual(screen.uri_rows[0][0].callback_data,'temporary:plain:'+identity)
        self.assertIn('Existing connections',screen.plain())
        screen.rich(rows)
        self.bot.send_message=AsyncMock()
        draw=await self.click('plain:'+identity,backend)
        draw.assert_not_awaited()
        self.bot.send_message.assert_awaited_once()
        self.assertIsNone(self.bot.send_message.call_args.kwargs['parse_mode'])
        self.assertEqual(self.bot.send_message.call_args.args[1],'vless://secret')

    async def test_temporary_qr_matches_regular_payload_and_media_layout(self):
        from telegram_client.routers.user import config_qr, qr_payload
        for protocol, transport, uri in (('awg','vpn','vpn://secret'), ('xray','xhttp','vless://secret')):
            identity=str(uuid4())
            card=dict(id=identity,node_key='lv1',protocol=protocol,transport=transport,status='active')
            artifact=dict(content=uri,files=[dict(filename='config.txt',content=uri)])
            backend=SimpleNamespace(request=AsyncMock(side_effect=[card,artifact]))
            draw=await self.click('show:'+identity,backend)
            screen,rows=draw.call_args.args[2:4]
            self.assertEqual(screen.qr,config_qr(qr_payload(protocol,transport,uri)))
            self.assertTrue(screen.qr.startswith(b'\x89PNG'))
            self.assertTrue(screen.details_lines)
            self.assertTrue(screen.uri_collapsed)
            self.assertFalse(any(b.callback_data=='temporary:show:'+identity for row in rows for b in row))
            screen.rich(rows)

    async def test_fallback_sends_qr_photo_and_files(self):
        identity=str(uuid4())
        item=dict(id=identity,node_key='lv1',protocol='awg',transport='vpn',status='active')
        artifact=dict(content='vpn://secret',files=[dict(filename='config.txt',content='vpn://secret')])
        backend=SimpleNamespace(request=AsyncMock(side_effect=[item,artifact]))
        self.bot.send_photo=AsyncMock()
        self.bot.send_document=AsyncMock()
        with patch.object(screens,'render',new_callable=AsyncMock,return_value=False):
            await screens.card(self.query,self.bot,backend,self.state,identity,show=True)
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_document.assert_awaited_once()

    async def test_oversized_uri_keeps_files_without_qr(self):
        identity=str(uuid4())
        item=dict(id=identity,node_key='lv1',protocol='xray',transport='tcp',status='active')
        uri='vless://'+ 'a'*2600
        backend=SimpleNamespace(request=AsyncMock(side_effect=[item,dict(content=uri,files=[dict(filename='config.txt',content=uri)])]))
        draw=await self.click('show:'+identity,backend)
        screen=draw.call_args.args[2]
        self.assertIsNone(screen.qr)
        self.assertTrue(screen.files)
        self.assertEqual(screen.uri,uri)

    async def test_revocation_needs_confirmation_and_is_red(self):
        backend=SimpleNamespace(request=AsyncMock())
        identity=str(uuid4())
        draw=await self.click('confirm_revoke:'+identity,backend)
        self.assertEqual(draw.call_args.args[3][0][0].style,'danger')
        backend.request.assert_not_awaited()

    async def test_unknown_issue_keeps_key_and_does_not_repeat_request(self):
        backend=SimpleNamespace(request=AsyncMock(side_effect=BackendError('backend_unavailable',503)))
        key=str(uuid4())
        self.state_data['temporary_draft']=dict(command_key=key,node=self.node(),protocol='awg',transport='vpn',duration_seconds=86400)
        await self.click('issue',backend)
        backend.request.assert_awaited_once()
        self.assertEqual(self.state_data['temporary_draft']['command_key'],key)
        self.assertEqual(backend.request.call_args.kwargs['command_key'],key)
