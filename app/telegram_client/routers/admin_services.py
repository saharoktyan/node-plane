"""The installation's services in the bot: system ones (the web cabinet's
relay, the Telegram bot), built-in VPN and, later, services from manifests,
each with its kind, state and instances, as `GET /api/v1/services` reports."""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..backend import BackendClient
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section, Table
from .common import render

router = Router()

KINDS = ('system', 'builtin', 'user')


def entry_visible(permissions):
    """Staff who see more than VPN: system settings or a type of service."""
    return permissions is None or 'settings.manage' in permissions or any(
        permission.startswith('services.') for permission in permissions)


def _name(locale, item):
    key = 'services.name.' + item['service']
    name = tr(locale, key)
    return item['service'] if name == key else name


def _state(locale, item):
    if item['state']:
        key = 'services.state.' + item['state']
        value = tr(locale, key)
        return item['state'] if value == key else value
    return '—'


async def show_services(query, bot, backend, state):
    locale = normalize_locale((await state.get_data()).get('locale'))
    items = (await backend.request('GET', '/api/v1/services', telegram_user_id=query.from_user.id))['items']
    head = (tr(locale, 'services.column.service'), tr(locale, 'services.column.state'),
            tr(locale, 'services.column.instances'))
    sections = []
    for kind in KINDS:
        rows = tuple((_name(locale, item), _state(locale, item),
                      '—' if item['instances'] is None else str(item['instances']))
                     for item in items if item['kind'] == kind)
        if rows:
            sections.append(Section(tr(locale, 'services.kind.' + kind), (tr(locale, 'services.kind_hint.' + kind),),
                                    tables=(Table(head, rows),)))
    buttons = []
    if any(item['service'] == 'relay' for item in items):
        buttons.append([InlineKeyboardButton(text=tr(locale, 'relay.title'), callback_data='relay:v:v')])
    buttons.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data='admin_menu')])
    await render(bot, query.message.chat.id, Screen(tr(locale, 'services.title'), sections=tuple(sections),
                 embedded_buttons=True, navigation=True), buttons, state, query.message.message_id)


@router.callback_query(F.data == 'services')
async def services_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await show_services(query, bot, backend, state)
