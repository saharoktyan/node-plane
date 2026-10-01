import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram_client.commands import COMMANDS, register_commands
from telegram_client.backend import BackendError
from telegram_client.routers import user
from tests import test_telegram_client as fixture


class CommandsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture.TelegramFlowTests.setUp(self)
        self.message = SimpleNamespace(from_user=SimpleNamespace(id=123,language_code='en',username='alice'),chat=SimpleNamespace(id=123,type='private'),delete=AsyncMock())
        self.backend = SimpleNamespace()

    async def test_menu_contains_only_agreed_commands_in_both_locales(self):
        bot = SimpleNamespace(set_my_commands=AsyncMock())
        await register_commands(bot)
        self.assertEqual(COMMANDS, ('start','help','id','version','status'))
        for call in bot.set_my_commands.call_args_list:
            commands = call.args[0]
            self.assertEqual(tuple(c.command for c in commands), COMMANDS)
            self.assertTrue(all(c.description and not c.description.startswith('command.') for c in commands))
        self.assertEqual([c.kwargs['language_code'] for c in bot.set_my_commands.call_args_list], ['', 'en','ru'])

    async def test_help_exits_wizard_and_lists_supported_commands(self):
        await self.state.set_state('wizard:input')
        self.state_data['issuance_poll_token'] = 'old'
        with patch.object(user,'render',AsyncMock()) as draw:
            await user.help_cmd(self.message,self.bot,self.backend,self.state)
        self.assertIsNone(self.current_state)
        self.assertIsNone(self.state_data['issuance_poll_token'])
        text = draw.call_args.args[2].plain()
        for command in COMMANDS:
            self.assertIn('/'+command, text)
        self.assertNotIn('/getkey', text)

    async def test_status_reuses_backend_screen_and_denial_is_friendly(self):
        with patch.object(user,'show_admin_status',AsyncMock()) as show:
            await user.status_cmd(self.message,self.bot,self.backend,self.state)
            show.assert_awaited_once_with(123,123,None,self.bot,self.backend,self.state)
        with patch.object(user,'show_admin_status',AsyncMock(side_effect=BackendError('permission_denied',403))), patch.object(user,'render',AsyncMock()) as draw:
            await user.status_cmd(self.message,self.bot,self.backend,self.state)
        self.assertIn('only to administrators', draw.call_args.args[2].plain())
        self.assertNotIn('permission_denied', draw.call_args.args[2].plain())
