"""The per-node BitTorrent override in the bot."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.routers import admin_node_policies as policies
from tests import test_telegram_admin_profiles_rich as fixture


class NodeBittorrentTests(IsolatedAsyncioTestCase):
    setUp = fixture.ProfileRichTests.setUp

    def listing(self):
        own = [{'key': 'xray.bittorrent', 'node_key': 'n1', 'protocol': None, 'value': value, 'revision': revision}
               for value, revision in [self.own] if value]
        return {'keys': [{'key': 'xray.bittorrent', 'value': 'block', 'default': 'allow', 'effective': self.global_,
                          'revision': 1}], 'values': own}

    async def request(self, method, path, **kwargs):
        if method == 'PATCH':
            change = kwargs['body']['changes'][0]
            self.patches.append((change['node_key'], change['value'], change['revision']))
            self.own = (change['value'], change['revision'] + 1)
        return self.listing()

    async def press(self, data):
        self.query.data = data
        with patch.object(policies, 'render', new_callable=AsyncMock) as draw:
            await policies.node_bittorrent_cb(self.query, self.bot, self.backend, self.state)
        return draw.call_args.args[2]

    async def test_override_shows_its_source_and_inherit_removes_it(self):
        self.own, self.global_, self.patches = (None, 0), 'block', []
        self.backend = SimpleNamespace(request=AsyncMock(side_effect=self.request))
        node = {'key': 'n1', 'protocols': ['xray']}
        section = policies.settings_section('en', node, self.listing())[0]
        self.assertEqual(section.lines, ('BitTorrent over VLESS: Block · installation setting',))
        self.assertEqual(policies.settings_section('en', {'key': 'a1', 'protocols': ['awg']}, self.listing()), ())
        screen = await self.press('nodebt:n1')
        self.assertEqual([b.callback_data for row in screen.sections[0].rows for b in row if b.style == 'primary'],
                         ['nodebt:n1:inherit'])
        screen = await self.press('nodebt:n1:allow')
        self.assertEqual(screen.sections[0].lines, ('BitTorrent over VLESS: Allow · this node',))
        await self.press('nodebt:n1:allow')
        await self.press('nodebt:n1:maybe')
        await self.press('nodebt:n1:inherit')
        self.assertEqual(self.patches, [('n1', 'allow', 0), ('n1', None, 1)])
