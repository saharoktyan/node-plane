"""Open registration in the bot: turned on and off in system settings, with the
removal of registered accounts left empty; accounts without access are a
separate list under Accounts."""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section
from .common import render

router = Router()

CLEANUP = (None, 7, 30, 90)


def _choice(text, callback, selected):
    return InlineKeyboardButton(text=text, callback_data=callback, style='primary' if selected else None)


async def show_settings(query, bot, backend, state, notice=None):
    locale = normalize_locale((await state.get_data()).get('locale'))
    value = await backend.request('GET', '/api/v1/system/registration', telegram_user_id=query.from_user.id)
    sections = (
        Section(tr(locale, 'registration.open'), (tr(locale, 'registration.open_hint'),), rows=((
            _choice(tr(locale, 'registration.on'), 'reg:open:on', value['open']),
            _choice(tr(locale, 'registration.off'), 'reg:open:off', not value['open'])),)),
        Section(tr(locale, 'registration.cleanup'), (tr(locale, 'registration.cleanup_hint'),), rows=(tuple(
            _choice(tr(locale, 'registration.cleanup_off') if days is None
                    else tr(locale, 'registration.cleanup_days', days=days),
                    f"reg:cleanup:{days or 'off'}", value['cleanup_days'] == days)
            for days in CLEANUP),)),
    )
    await render(bot, query.message.chat.id, Screen(tr(locale, 'registration.title'),
                 (notice,) if notice else (), sections=sections, embedded_buttons=True, navigation=True),
                 [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data='admin_settings')]],
                 state, query.message.message_id)


async def show_without_access(query, bot, backend, state):
    locale = normalize_locale((await state.get_data()).get('locale'))
    page = await backend.request('GET', '/api/v1/accounts?limit=100&access=without',
                                 telegram_user_id=query.from_user.id)
    from .admin_profiles import AccountCallback
    rows = [[InlineKeyboardButton(
        text=account.get('login') or str(account.get('telegram_user_id') or account['id'][:8]),
        callback_data=AccountCallback(account_id=account['id']).pack())] for account in page['items']]
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data='accounts')])
    await render(bot, query.message.chat.id, Screen(tr(locale, 'registration.without_access'),
                 (tr(locale, 'registration.without_access_hint' if page['items'] else 'registration.without_access_empty'),),
                 embedded_buttons=True, navigation=True), rows, state, query.message.message_id)


@router.callback_query(F.data.startswith('reg:'))
async def registration_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    parts = query.data.split(':')
    if parts[1:2] == ['without']:
        return await show_without_access(query, bot, backend, state)
    body = None
    if parts[1:2] == ['open'] and len(parts) == 3:
        body = {'open': parts[2] == 'on'}
    elif parts[1:2] == ['cleanup'] and len(parts) == 3:
        body = {'cleanup_days': None if parts[2] == 'off' else int(parts[2])} \
            if parts[2] == 'off' or parts[2].isdigit() else None
    notice = None
    if body is not None:
        try:
            await backend.request('PATCH', '/api/v1/system/registration', telegram_user_id=query.from_user.id,
                                  command=True, body=body)
        except BackendError as exc:
            from .admin_settings import _friendly_error
            notice = _friendly_error(normalize_locale((await state.get_data()).get('locale')), exc)
    await show_settings(query, bot, backend, state, notice)
