from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock

from telegram_client.i18n import tr
from telegram_client.screens import Screen
from telegram_client.routers.common import render, media_blocks


class TranslationTests(TestCase):
    def test_node_key_placeholder_does_not_conflict_with_translation_key(self):
        self.assertEqual(tr('en', 'nodes.maintenance.node', key='lv1'), 'Node: lv1')
        self.assertEqual(tr('ru', 'nodes.maintenance.node', key='lv1'), 'Нода: lv1')


class MediaRetentionTests(IsolatedAsyncioTestCase):
    async def test_uri_show_and_hide_reuse_all_documents_and_nested_qr(self):
        data = {'control_message_id': 12}
        async def update(**values):
            data.update(values)
        state = SimpleNamespace(get_data=AsyncMock(side_effect=lambda: dict(data)),
                                update_data=AsyncMock(side_effect=update))
        returned = SimpleNamespace(rich_message=SimpleNamespace(blocks=[
            SimpleNamespace(type='details', blocks=[SimpleNamespace(type='photo',
                photo=[SimpleNamespace(file_id='qr-id')])]),
            SimpleNamespace(type='document', document=SimpleNamespace(file_id='vpn-id')),
            SimpleNamespace(type='document', document=SimpleNamespace(file_id='conf-id'))]))
        bot = SimpleNamespace(edit_message_text=AsyncMock(return_value=returned))
        def screen(uri=None):
            return Screen('Config', uri=uri, qr=b'qr', qr_title='QR',
                          files=(('test.vpn', b'vpn'), ('test.conf', b'conf')))
        await render(bot, 1, screen(), [], state)
        for uri in ('vpn://long-configuration', None):
            await render(bot, 1, screen(uri), [], state)
            content = bot.edit_message_text.call_args.kwargs['rich_message']
            self.assertEqual([getattr(block, block.type).media for block in media_blocks(content)],
                             ['qr-id', 'vpn-id', 'conf-id'])
