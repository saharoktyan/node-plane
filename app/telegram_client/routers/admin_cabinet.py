"""The web cabinet as a system service: where it is served from and how far
that is applied. Served by the relay's node it shares the relay's domain;
served by the controller it needs the address it listens on and the address
people open it at. Callbacks carry the way in (`cab:{action}:{where}`, where
is 'v' for Services or 's' for Settings), so Back returns there."""
import ipaddress

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen
from .common import render

router = Router()

CABINET = '/api/v1/web-cabinet'
DEFAULT_LISTEN = '0.0.0.0:8090'
# Typed fields by their callback letter.
FIELDS = {'l': 'listen', 'u': 'public_url'}
ERRORS = {'cabinet_build_missing', 'cabinet_needs_relay', 'cabinet_mode_change', 'cabinet_removing',
          'cabinet_listen_invalid', 'cabinet_url_invalid', 'cabinet_apply_failed', 'cabinet_files_invalid',
          'cabinet_files_too_large', 'agent_unavailable'}


class CabinetState(StatesGroup):
    waiting_for_value = State()


def callback(action, where):
    return f'cab:{action}:{where}'


def parse(data):
    _, action, where = (data.split(':', 2) + ['', ''])[:3]
    return action, where if where in {'v', 's'} else 'v'


def back_callback(where):
    return 'admin_settings' if where == 's' else 'services'


def check(field, value):
    """The typed value as the controller takes it, or the error key."""
    value = value.strip()
    if field == 'listen':
        host, _, port = value.rpartition(':')
        try:
            ipaddress.ip_address(host.strip('[]'))
        except ValueError:
            return None, 'cabinet.invalid.listen'
        if not port.isdigit() or not 0 < int(port) < 65536 or int(port) == 8080:
            return None, 'cabinet.invalid.listen'
        return value, None
    if not value.lower().startswith(('https://', 'http://')) or ' ' in value:
        return None, 'cabinet.invalid.url'
    return value, None


def error_text(locale, exc):
    from .admin_settings import _friendly_error
    return tr(locale, 'cabinet.error.' + exc.code) if exc.code in ERRORS else _friendly_error(locale, exc)


async def show(chat_id, user_id, message_id, where, bot, backend, state, notice=None, confirm=False):
    locale = normalize_locale((await state.get_data()).get('locale'))
    await state.set_state(None)
    await state.update_data(cabinet_draft=None, cabinet_input=None)
    back = [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=back_callback(where))]
    lines, rows = [notice] if notice else [], []
    try:
        answer = await backend.request('GET', CABINET, telegram_user_id=user_id)
    except BackendError as exc:
        lines.append(error_text(locale, exc))
        rows.append([InlineKeyboardButton(text=tr(locale, 'cabinet.refresh'), callback_data=callback('v', where))])
        return await render(bot, chat_id, Screen(tr(locale, 'cabinet.title'), tuple(lines), embedded_buttons=True,
                            navigation=True), rows + [back], state, message_id)
    cabinet = answer.get('cabinet')
    if not answer.get('available'):
        lines.append(tr(locale, 'cabinet.no_build'))
    if cabinet is None:
        lines.append(tr(locale, 'cabinet.none'))
        if answer.get('available'):
            if answer.get('relay_node_key'):
                rows.append([InlineKeyboardButton(text=tr(locale, 'cabinet.on_relay'), callback_data=callback('r', where),
                                                  style='primary')])
            else:
                lines.append(tr(locale, 'cabinet.relay_first'))
                rows.append([InlineKeyboardButton(text=tr(locale, 'relay.title'), callback_data='relay:v:' + where)])
            rows.append([InlineKeyboardButton(text=tr(locale, 'cabinet.on_controller'), callback_data=callback('e', where))])
    else:
        lines.append(tr(locale, 'cabinet.address', value=cabinet.get('url') or '—'))
        lines.append(tr(locale, 'cabinet.where.relay') if cabinet['mode'] == 'relay'
                     else tr(locale, 'cabinet.where.local', value=cabinet.get('listen') or '—'))
        lines.append(tr(locale, 'cabinet.status', value=tr(locale, 'relay.status.' + cabinet['status'])))
        if cabinet.get('applied_version') and cabinet['applied_version'] != cabinet.get('version'):
            lines.append(tr(locale, 'cabinet.version_behind', value=cabinet['applied_version']))
        if cabinet.get('error_code'):
            lines.append(tr(locale, 'relay.last_error', value=cabinet['error_code']))
        if cabinet['mode'] == 'local':
            lines.append(tr(locale, 'cabinet.local_warning'))
        if confirm:
            lines.append(tr(locale, 'cabinet.remove_confirm'))
            rows.append([InlineKeyboardButton(text=tr(locale, 'cabinet.remove'), callback_data=callback('rmy', where),
                                              style='danger'),
                         InlineKeyboardButton(text=tr(locale, 'cancel'), callback_data=callback('v', where))])
        elif cabinet['status'] != 'removing':
            if cabinet['mode'] == 'local':
                rows.append([InlineKeyboardButton(text=tr(locale, 'cabinet.change'), callback_data=callback('e', where))])
            rows.append([InlineKeyboardButton(text=tr(locale, 'cabinet.remove'), callback_data=callback('rm', where),
                                              style='danger')])
    rows.append([InlineKeyboardButton(text=tr(locale, 'cabinet.refresh'), callback_data=callback('v', where))])
    await render(bot, chat_id, Screen(tr(locale, 'cabinet.title'), tuple(lines), embedded_buttons=True, navigation=True),
                 rows + [back], state, message_id)


async def show_draft(chat_id, message_id, where, bot, state, notice=None):
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    draft = data['cabinet_draft']
    lines = ([notice] if notice else []) + [tr(locale, 'cabinet.local_intro'), tr(locale, 'cabinet.local_warning')]
    rows = [[InlineKeyboardButton(text=tr(locale, 'cabinet.field.' + field, value=draft[field] or '—'),
                                  callback_data=callback('f' + letter, where))] for letter, field in FIELDS.items()]
    rows.append([InlineKeyboardButton(text=tr(locale, 'cabinet.save'), callback_data=callback('save', where),
                                      style='primary')])
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data=callback('v', where))])
    await render(bot, chat_id, Screen(tr(locale, 'cabinet.local_title'), tuple(lines), embedded_buttons=True,
                 navigation=True), rows, state, message_id)


async def save(user_id, body, backend):
    await backend.request('PUT', CABINET, telegram_user_id=user_id, command=True, body=body)


@router.callback_query(F.data.startswith('cab:'))
async def cabinet_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    action, where = parse(query.data)
    chat_id, user_id, message_id = query.message.chat.id, query.from_user.id, query.message.message_id
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    if action in {'v', 'rm'}:
        return await show(chat_id, user_id, message_id, where, bot, backend, state, confirm=action == 'rm')
    if action in {'rmy', 'r'}:
        try:
            if action == 'rmy':
                await backend.request('DELETE', CABINET, telegram_user_id=user_id, command=True)
            else:
                await save(user_id, {'mode': 'relay'}, backend)
        except BackendError as exc:
            return await show(chat_id, user_id, message_id, where, bot, backend, state, error_text(locale, exc))
        return await show(chat_id, user_id, message_id, where, bot, backend, state,
                          tr(locale, 'cabinet.removing' if action == 'rmy' else 'cabinet.saved'))
    draft = data.get('cabinet_draft')
    if action == 'e':
        try:
            cabinet = (await backend.request('GET', CABINET, telegram_user_id=user_id)).get('cabinet')
        except BackendError as exc:
            return await show(chat_id, user_id, message_id, where, bot, backend, state, error_text(locale, exc))
        local = cabinet if cabinet and cabinet['mode'] == 'local' else {}
        draft = {'listen': local.get('listen') or DEFAULT_LISTEN, 'public_url': local.get('public_url') or ''}
        await state.update_data(cabinet_draft=draft)
        return await show_draft(chat_id, message_id, where, bot, state)
    if draft is None:
        # A stale button after the draft was dropped.
        return await show(chat_id, user_id, message_id, where, bot, backend, state)
    if action.startswith('f') and action[1:] in FIELDS:
        field = FIELDS[action[1:]]
        await state.set_state(CabinetState.waiting_for_value)
        await state.update_data(cabinet_input={'field': field, 'where': where, 'message_id': message_id})
        return await render(bot, chat_id, Screen(tr(locale, 'cabinet.field_title.' + field),
            (tr(locale, 'cabinet.prompt.' + field),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=callback('d', where))]], state, message_id)
    if action == 'save':
        if not draft['public_url']:
            return await show_draft(chat_id, message_id, where, bot, state, tr(locale, 'cabinet.missing.url'))
        try:
            await save(user_id, {'mode': 'local', **draft}, backend)
        except BackendError as exc:
            return await show_draft(chat_id, message_id, where, bot, state, error_text(locale, exc))
        return await show(chat_id, user_id, message_id, where, bot, backend, state, tr(locale, 'cabinet.saved'))
    await state.set_state(None)
    await show_draft(chat_id, message_id, where, bot, state)


@router.message(CabinetState.waiting_for_value, F.text)
async def cabinet_value(message: Message, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    request, draft = data.get('cabinet_input') or {}, data.get('cabinet_draft')
    if draft is None or not request:
        await state.set_state(None)
        return
    where, message_id, field = request['where'], request['message_id'], request['field']
    value, error = check(field, message.text or '')
    if error:
        return await render(bot, message.chat.id, Screen(tr(locale, 'cabinet.field_title.' + field),
            (tr(locale, error), tr(locale, 'cabinet.prompt.' + field)), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=callback('d', where))]], state, message_id)
    draft[field] = value
    await state.set_state(None)
    await state.update_data(cabinet_draft=draft, cabinet_input=None)
    await show_draft(message.chat.id, message_id, where, bot, state)
