"""Writing a text in several languages in the bot: tariff titles and
descriptions, announcements, the messages visitors see.

One screen for every such text. Each language the controller supports is a
line: its own text, or whose text it shows. The administrator writes the
languages they want; the first one written is the source, the only one
required. For a language without a text they choose whose to show, so
people with that language still get it. A caller registers a context — the
screen's title, where Back goes, the limits and what saving does — and
opens the editor with the current value.
"""
from dataclasses import dataclass
from typing import Awaitable, Callable

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section
from .common import render

router = Router()


class Editing(StatesGroup):
    text = State()


@dataclass(frozen=True)
class Context:
    title: str
    back: str
    max_length: int
    save: Callable[..., Awaitable[None]]
    multiline: bool = True
    # A text that may be removed altogether (a tariff's description).
    optional: bool = False


CONTEXTS: dict[str, Context] = {}


def register(name, context):
    CONTEXTS[name] = context


def language_name(locale, language):
    key = 'ltext.language.' + language
    name = tr(locale, key)
    return language.upper() if name == key else name


def shown(draft, language):
    """Whose text a language shows."""
    if language in draft['texts']:
        return language
    return draft['fallback'].get(language, draft['source'])


def value_of(draft):
    """The draft as the API takes it."""
    texts = {name: text for name, text in draft['texts'].items() if text}
    fallback = {name: target for name, target in draft['fallback'].items()
                if name not in texts and target in texts and target != draft['source']}
    return {'source': draft['source'], 'texts': texts, 'fallback': fallback}


async def _languages(backend, user_id):
    try:
        return (await backend.request('GET', '/api/v1/system/languages', telegram_user_id=user_id))['languages']
    except BackendError:
        return ['en', 'ru']


async def open_editor(chat_id, user_id, message_id, bot, backend, state, name, value=None):
    """Start editing; with no text yet, ask for the first one right away."""
    locale = normalize_locale((await state.get_data()).get('locale'))
    languages = await _languages(backend, user_id)
    draft = ({'source': value['source'], 'texts': dict(value['texts']), 'fallback': dict(value.get('fallback') or {})}
             if value else {'source': locale if locale in languages else languages[0], 'texts': {}, 'fallback': {}})
    await state.update_data(ltext_context=name, ltext_draft=draft, ltext_languages=languages,
                            ltext_message=message_id)
    if not draft['texts']:
        return await _prompt(chat_id, message_id, bot, state, draft['source'])
    await show(chat_id, message_id, bot, state)


async def show(chat_id, message_id, bot, state, notice=None):
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    context, draft, languages = CONTEXTS[data['ltext_context']], data['ltext_draft'], data['ltext_languages']
    await state.set_state(None)
    sections = []
    for language in languages:
        name = language_name(locale, language)
        if language in draft['texts']:
            head = tr(locale, 'ltext.source', name=name) if language == draft['source'] else name
            row = [InlineKeyboardButton(text=tr(locale, 'ltext.edit'), callback_data='lt:w:' + language)]
            if language != draft['source']:
                row += [InlineKeyboardButton(text=tr(locale, 'ltext.make_source'), callback_data='lt:s:' + language),
                        InlineKeyboardButton(text=tr(locale, 'ltext.remove'), callback_data='lt:d:' + language)]
            sections.append(Section(head, (draft['texts'][language],), rows=(tuple(row),)))
        else:
            other = language_name(locale, shown(draft, language))
            row = [InlineKeyboardButton(text=tr(locale, 'ltext.write'), callback_data='lt:w:' + language)]
            if len(draft['texts']) > 1:
                row.append(InlineKeyboardButton(text=tr(locale, 'ltext.show_other'), callback_data='lt:f:' + language))
            sections.append(Section(name, (tr(locale, 'ltext.shows', name=other),), rows=(tuple(row),)))
    rows = [[InlineKeyboardButton(text=tr(locale, 'ltext.save'), callback_data='lt:ok', style='primary')]]
    if context.optional:
        rows.append([InlineKeyboardButton(text=tr(locale, 'ltext.clear'), callback_data='lt:clear', style='danger')])
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data=context.back)])
    lines = ((notice,) if notice else ()) + (tr(locale, 'ltext.hint'),)
    await render(bot, chat_id, Screen(tr(locale, context.title), lines, sections=tuple(sections),
                 embedded_buttons=True, navigation=True), rows, state, message_id)


async def _prompt(chat_id, message_id, bot, state, language, notice=None):
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    context, draft = CONTEXTS[data['ltext_context']], data['ltext_draft']
    await state.set_state(Editing.text)
    await state.update_data(ltext_language=language)
    lines = ((notice,) if notice else ()) + (
        tr(locale, 'ltext.prompt', name=language_name(locale, language), limit=context.max_length),)
    current = draft['texts'].get(language)
    sections = (Section(tr(locale, 'ltext.current'), (current,)),) if current else ()
    back = 'lt:v' if draft['texts'] else context.back
    await render(bot, chat_id, Screen(tr(locale, context.title), lines, sections=sections, embedded_buttons=True,
                 navigation=True), [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=back)]],
                 state, message_id)


@router.callback_query(F.data.startswith('lt:'))
async def editor_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    data = await state.get_data()
    if data.get('ltext_context') not in CONTEXTS:
        return
    chat_id, message_id = query.message.chat.id, query.message.message_id
    draft, languages = data['ltext_draft'], data['ltext_languages']
    action, _, language = query.data.removeprefix('lt:').partition(':')
    if action == 'w' and language in languages:
        return await _prompt(chat_id, message_id, bot, state, language)
    if action == 's' and language in draft['texts']:
        draft['source'] = language
    elif action == 'd' and language in draft['texts'] and language != draft['source']:
        del draft['texts'][language]
        draft['fallback'] = {name: target for name, target in draft['fallback'].items() if target != language}
    elif action == 'f' and language in languages and language not in draft['texts']:
        # The next language with a text, around the list.
        written = [name for name in languages if name in draft['texts']]
        current = shown(draft, language)
        draft['fallback'][language] = written[(written.index(current) + 1) % len(written)]
    elif action in {'ok', 'clear'}:
        context = CONTEXTS[data['ltext_context']]
        try:
            await context.save(chat_id, query.from_user.id, message_id, bot, backend, state,
                               None if action == 'clear' else value_of(draft))
        except BackendError as exc:
            from .admin_settings import _friendly_error
            locale = normalize_locale(data.get('locale'))
            notice = (tr(locale, 'ltext.invalid') if exc.code in {'invalid_localized_text', 'invalid_input'}
                      else _friendly_error(locale, exc))
            await show(chat_id, message_id, bot, state, notice)
        return
    await state.update_data(ltext_draft=draft)
    await show(chat_id, message_id, bot, state)


@router.message(Editing.text, F.text)
async def text_message(message: Message, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    if message.chat.type != 'private' or not message.from_user:
        return
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    context, draft = CONTEXTS[data['ltext_context']], data['ltext_draft']
    language, message_id = data['ltext_language'], data['ltext_message']
    text = (message.text or '').strip()
    if not text or len(text) > context.max_length or (not context.multiline and '\n' in text):
        return await _prompt(message.chat.id, message_id, bot, state, language, tr(locale, 'ltext.invalid'))
    if not draft['texts']:
        draft['source'] = language
    draft['texts'][language] = text
    draft['fallback'].pop(language, None)
    await state.update_data(ltext_draft=draft)
    await show(message.chat.id, message_id, bot, state)
