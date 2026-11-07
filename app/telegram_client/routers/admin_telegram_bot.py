"""Where the Telegram bot runs: on the controller or on a node with an agent.
On a node it reaches the controller through the relay, so the controller
needs no access to Telegram. Callbacks carry the way in
(`tgb:{action}:{where}`, where is 'v' for Services or 's' for Settings), so
Back returns there."""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen
from .admin_relay import candidates, node_title
from .common import render

router = Router()

PLACEMENT = '/api/v1/telegram-bot/placement'
ERRORS = {'telegram_disabled', 'telegram_bot_needs_relay', 'telegram_bot_node_without_agent', 'telegram_bot_removing',
          'telegram_bot_node_change', 'telegram_bot_local_start_failed', 'telegram_bot_apply_failed',
          'bot_install_failed', 'bot_package_uv_missing', 'bot_package_requirements_failed', 'bot_build_missing',
          'bot_files_too_large', 'agent_unavailable'}


def callback(action, where):
    return f'tgb:{action}:{where}'


def parse(data):
    _, action, where = (data.split(':', 2) + ['', ''])[:3]
    return action, where if where in {'v', 's'} else 'v'


def back_callback(where):
    return 'admin_settings' if where == 's' else 'services'


def error_text(locale, exc):
    from .admin_settings import _friendly_error
    return tr(locale, 'tgbot.error.' + exc.code) if exc.code in ERRORS else _friendly_error(locale, exc)


async def show(chat_id, user_id, message_id, where, bot, backend, state, notice=None, confirm=False):
    locale = normalize_locale((await state.get_data()).get('locale'))
    back = [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=back_callback(where))]
    lines, rows = [notice] if notice else [], []
    try:
        answer = await backend.request('GET', PLACEMENT, telegram_user_id=user_id)
    except BackendError as exc:
        lines.append(error_text(locale, exc))
        answer = None
    if answer is not None:
        placement = answer.get('placement')
        if placement is None:
            lines.append(tr(locale, 'tgbot.on_controller'))
            lines.append(tr(locale, 'tgbot.intro'))
            if not answer.get('relay_node_key'):
                lines.append(tr(locale, 'tgbot.relay_first'))
                rows.append([InlineKeyboardButton(text=tr(locale, 'relay.title'), callback_data='relay:v:' + where)])
            else:
                rows.append([InlineKeyboardButton(text=tr(locale, 'tgbot.move'), callback_data=callback('n', where),
                                                  style='primary')])
        else:
            title = await node_title(user_id, placement['node_key'], backend, state)
            lines.append(tr(locale, 'tgbot.on_node', value=title))
            lines.append(tr(locale, 'cabinet.status', value=tr(locale, 'relay.status.' + placement['status'])))
            if placement.get('applied_version') and placement['applied_version'] != placement.get('version'):
                lines.append(tr(locale, 'cabinet.version_behind', value=placement['applied_version']))
            if placement.get('error_code'):
                lines.append(tr(locale, 'relay.last_error', value=placement['error_code']))
            if confirm:
                lines.append(tr(locale, 'tgbot.back_confirm'))
                rows.append([InlineKeyboardButton(text=tr(locale, 'tgbot.back'), callback_data=callback('rmy', where),
                                                  style='danger'),
                             InlineKeyboardButton(text=tr(locale, 'cancel'), callback_data=callback('v', where))])
            elif placement['status'] != 'removing':
                if placement['status'] == 'failed':
                    rows.append([InlineKeyboardButton(text=tr(locale, 'tgbot.retry'),
                                                      callback_data=callback('again', where))])
                rows.append([InlineKeyboardButton(text=tr(locale, 'tgbot.back'), callback_data=callback('rm', where))])
    rows.append([InlineKeyboardButton(text=tr(locale, 'cabinet.refresh'), callback_data=callback('v', where))])
    await render(bot, chat_id, Screen(tr(locale, 'tgbot.title'), tuple(lines), embedded_buttons=True, navigation=True),
                 rows + [back], state, message_id)


async def show_nodes(chat_id, user_id, message_id, where, bot, backend, state):
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        nodes = await candidates(user_id, backend)
    except BackendError as exc:
        return await show(chat_id, user_id, message_id, where, bot, backend, state, error_text(locale, exc))
    await state.update_data(tgbot_nodes=nodes)
    rows = [[InlineKeyboardButton(text=title, callback_data=callback(f'p{index}', where))]
            for index, (_, title) in enumerate(nodes)]
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data=callback('v', where))])
    lines = (tr(locale, 'tgbot.pick'), tr(locale, 'tgbot.pick_hint')) if nodes else (tr(locale, 'relay.no_nodes'),)
    await render(bot, chat_id, Screen(tr(locale, 'tgbot.move'), lines, embedded_buttons=True, navigation=True),
                 rows, state, message_id)


@router.callback_query(F.data.startswith('tgb:'))
async def telegram_bot_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    action, where = parse(query.data)
    chat_id, user_id, message_id = query.message.chat.id, query.from_user.id, query.message.message_id
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    if action in {'v', 'rm'}:
        return await show(chat_id, user_id, message_id, where, bot, backend, state, confirm=action == 'rm')
    if action == 'n':
        return await show_nodes(chat_id, user_id, message_id, where, bot, backend, state)
    node_key = None
    if action == 'again':
        try:
            node_key = ((await backend.request('GET', PLACEMENT, telegram_user_id=user_id)).get('placement')
                        or {}).get('node_key')
        except BackendError as exc:
            return await show(chat_id, user_id, message_id, where, bot, backend, state, error_text(locale, exc))
    elif action.startswith('p') and action[1:].isdigit():
        nodes = data.get('tgbot_nodes') or []
        if int(action[1:]) < len(nodes):
            node_key = nodes[int(action[1:])][0]
    if action == 'rmy':
        try:
            await backend.request('DELETE', PLACEMENT, telegram_user_id=user_id, command=True)
        except BackendError as exc:
            return await show(chat_id, user_id, message_id, where, bot, backend, state, error_text(locale, exc))
        return await show(chat_id, user_id, message_id, where, bot, backend, state, tr(locale, 'tgbot.returning'))
    if node_key is None:
        # A stale button.
        return await show(chat_id, user_id, message_id, where, bot, backend, state)
    try:
        await backend.request('PUT', PLACEMENT, telegram_user_id=user_id, command=True, body={'node_key': node_key})
    except BackendError as exc:
        return await show(chat_id, user_id, message_id, where, bot, backend, state, error_text(locale, exc))
    await show(chat_id, user_id, message_id, where, bot, backend, state, tr(locale, 'tgbot.moving'))
