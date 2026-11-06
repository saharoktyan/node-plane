"""The bot's editor of texts in several languages: the source, other
languages, whose text a language without its own shows, removing a text."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from telegram_client.backend import BackendError
from telegram_client.routers import localized_editor as editor
from tests import test_telegram_admin_profiles_rich as fixture


class EditorTests(IsolatedAsyncioTestCase):
    setUp = fixture.ProfileRichTests.setUp

    def start(self, languages=('en', 'ru'), optional=False, multiline=True, fail=None):
        self.saved = []

        async def save(chat_id, user_id, message_id, bot, backend, state, value):
            if fail:
                raise BackendError(fail, 422)
            self.saved.append(value)
        editor.register('test', editor.Context('tariff.describe', 'back_here', 20, save,
                                               multiline=multiline, optional=optional))
        self.backend = SimpleNamespace(request=AsyncMock(return_value={'languages': list(languages)}))

    async def open(self, value=None):
        with patch.object(editor, 'render', new_callable=AsyncMock) as draw:
            await editor.open_editor(123, 123, 77, self.bot, self.backend, self.state, 'test', value)
        return draw.call_args.args[2:4]

    async def press(self, data):
        self.query.data = data
        with patch.object(editor, 'render', new_callable=AsyncMock) as draw:
            await editor.editor_cb(self.query, self.bot, self.backend, self.state)
        return draw.call_args.args[2:4] if draw.call_args else None

    async def send(self, text):
        message = SimpleNamespace(chat=SimpleNamespace(id=123, type='private'), from_user=SimpleNamespace(id=123),
                                  text=text, delete=AsyncMock())
        with patch.object(editor, 'render', new_callable=AsyncMock) as draw:
            await editor.text_message(message, self.bot, self.backend, self.state)
        return draw.call_args.args[2:4]

    async def test_a_new_text_starts_in_the_admins_language_and_other_languages_show_it(self):
        self.start()
        screen, rows = await self.open()
        # Nothing written yet: the prompt for the admin's own language, Back leaves.
        self.assertIn('English', screen.plain())
        self.assertEqual(rows, [[rows[0][0]]])
        self.assertEqual(rows[0][0].callback_data, 'back_here')
        screen, rows = await self.send(' Month ')
        self.assertIn('Shows the English text.', screen.plain())
        await self.press('lt:ok')
        self.assertEqual(self.saved, [{'source': 'en', 'texts': {'en': 'Month'}, 'fallback': {}}])

    async def test_the_source_moves_and_a_removed_language_drops_its_fallback(self):
        self.start(languages=('en', 'ru', 'de'))
        await self.open({'source': 'en', 'texts': {'en': 'Month', 'ru': 'Месяц'}, 'fallback': {}})
        # German readers get the Russian text instead of the source.
        screen, _ = await self.press('lt:f:de')
        self.assertIn('Shows the Russian text.', screen.plain())
        await self.press('lt:s:ru')
        await self.press('lt:ok')
        self.assertEqual(self.saved[-1], {'source': 'ru', 'texts': {'en': 'Month', 'ru': 'Месяц'}, 'fallback': {}})
        await self.press('lt:s:en')
        await self.press('lt:d:ru')
        await self.press('lt:ok')
        self.assertEqual(self.saved[-1], {'source': 'en', 'texts': {'en': 'Month'}, 'fallback': {}})
        # The source cannot be removed.
        await self.press('lt:d:en')
        self.assertIn('en', self.data['ltext_draft']['texts'])

    async def test_a_chosen_fallback_other_than_the_source_is_kept(self):
        self.start(languages=('en', 'ru', 'de'))
        await self.open({'source': 'en', 'texts': {'en': 'Month', 'ru': 'Месяц'}, 'fallback': {}})
        await self.press('lt:f:de')
        await self.press('lt:ok')
        self.assertEqual(self.saved[-1]['fallback'], {'de': 'ru'})

    async def test_a_text_too_long_or_on_two_lines_is_asked_again(self):
        self.start(multiline=False)
        await self.open()
        screen, _ = await self.send('x' * 21)
        self.assertIn('The text is empty or too long.', screen.plain())
        screen, _ = await self.send('one\ntwo')
        self.assertIn('The text is empty or too long.', screen.plain())
        self.assertEqual(self.data['ltext_draft']['texts'], {})

    async def test_an_optional_text_is_removed_and_a_refusal_stays_on_the_editor(self):
        self.start(optional=True)
        _, rows = await self.open({'source': 'en', 'texts': {'en': 'Fast'}, 'fallback': {}})
        self.assertIn('lt:clear', [button.callback_data for row in rows for button in row])
        await self.press('lt:clear')
        self.assertEqual(self.saved, [None])
        self.start(fail='invalid_localized_text')
        await self.open({'source': 'en', 'texts': {'en': 'Fast'}, 'fallback': {}})
        screen, _ = await self.press('lt:ok')
        self.assertIn('The text is empty or too long.', screen.plain())

    async def test_presses_without_an_open_editor_do_nothing(self):
        self.start()
        self.assertIsNone(await self.press('lt:ok'))
