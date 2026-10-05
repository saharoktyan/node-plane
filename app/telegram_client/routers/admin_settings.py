"""Controller settings backed by available backend endpoints."""
from __future__ import annotations

import base64
import hashlib
from aiogram import Bot, F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from ..backend import BackendClient, BackendError
from ..screens import Screen, Section
from ..i18n import normalize_locale, tr
from .callbacks import AdminSettingsCallback, RequestPolicyCallback, UpdatesCallback
from .common import render

router = Router()


class RequestPolicyState(StatesGroup):
    waiting_for_gate_message = State()
    waiting_for_bot_title = State()


async def _locale(state: FSMContext) -> str:
    return normalize_locale((await state.get_data()).get('locale'))


def _friendly_error(locale: str, error: BackendError) -> str:
    if error.status == 503:
        return tr(locale, 'settings.error_unavailable')
    return tr(locale, 'settings.error_generic')


def _update_status(locale: str, value: str | None) -> str:
    status = str(value or 'never')
    if status not in {'never', 'available', 'up_to_date', 'running',
                      'succeeded', 'success', 'failed', 'error', 'unsupported', 'noop'}:
        status = 'unknown'
    return tr(locale, f'updates.status.{status}')


class SshKeyCallback(CallbackData, prefix='ssh_key'):
    pass


class UpdateActionCallback(CallbackData, prefix='upd_act'):
    action: str


@router.callback_query(AdminSettingsCallback.filter())
async def admin_settings_cb(query: CallbackQuery, bot: Bot,
                            backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(None)
    locale = await _locale(state)
    rows = [
        [InlineKeyboardButton(text=tr(locale, 'settings.admin.bot_title'), callback_data='bot_title_settings'),
         InlineKeyboardButton(text=tr(locale, 'settings.admin.request_policy'), callback_data=RequestPolicyCallback().pack())],
        [InlineKeyboardButton(text=tr(locale, 'settings.admin.updates'), callback_data=UpdatesCallback().pack()),
         InlineKeyboardButton(text=tr(locale, 'backups.title'), callback_data='backups')],
        [InlineKeyboardButton(text=tr(locale, 'alerts.title'), callback_data='alerts'),
         InlineKeyboardButton(text=tr(locale, 'traffic.title'), callback_data='traffic')],
        [InlineKeyboardButton(text=tr(locale, 'settings.admin.ssh_key'), callback_data=SshKeyCallback().pack())],
        [InlineKeyboardButton(text=tr(locale, 'system_cleanup.title'), callback_data='system_cleanup')],
        [InlineKeyboardButton(text=tr(locale, 'back'), callback_data='admin_menu')],
    ]
    sections = (
        Section(tr(locale, 'settings.rich.access'), rows=(tuple(rows[0]),)),
        Section(tr(locale, 'settings.rich.monitoring'), rows=(tuple(rows[2]),)),
        Section(tr(locale, 'settings.rich.maintenance'), rows=(tuple(rows[1]), tuple(rows[3]))),
        Section(tr(locale, 'settings.rich.danger'), collapsed=True, rows=(tuple(rows[4]),)))
    await render(bot, query.message.chat.id, Screen(tr(locale, 'settings.admin.title'),
        sections=sections, embedded_buttons=True, navigation=True), rows[-1:], state, query.message.message_id)


@router.callback_query(F.data == 'bot_title_settings')
async def bot_title_settings_cb(query: CallbackQuery, bot: Bot,
                                backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(None)
    locale = await _locale(state)
    try:
        title = (await backend.bot_title(query.from_user.id))['title']
        screen = Screen(tr(locale, 'bot_title.title'),
                        (tr(locale, 'bot_title.current', value=title),), embedded_buttons=True, navigation=True)
        rows = [[InlineKeyboardButton(text=tr(locale, 'bot_title.edit'), callback_data='bot_title_edit')],
                [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminSettingsCallback().pack())]]
    except BackendError as exc:
        screen = Screen(tr(locale, 'bot_title.title'), (_friendly_error(locale, exc),), embedded_buttons=True, navigation=True)
        rows = [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminSettingsCallback().pack())]]
    await render(bot, query.message.chat.id, screen, rows, state, query.message.message_id)


@router.callback_query(F.data == 'bot_title_edit')
async def bot_title_edit_cb(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await query.answer()
    locale = await _locale(state)
    await state.set_state(RequestPolicyState.waiting_for_bot_title)
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'bot_title.title'), (tr(locale, 'bot_title.prompt'),), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data='bot_title_settings')]],
        state, query.message.message_id)


@router.message(RequestPolicyState.waiting_for_bot_title, F.text)
async def bot_title_text(message: Message, bot: Bot, backend: BackendClient,
                         state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    locale = await _locale(state)
    value = (message.text or '').strip()
    message_id = (await state.get_data()).get('control_message_id')
    if not value or len(value) > 64:
        await render(bot, message.chat.id,
            Screen(tr(locale, 'bot_title.title'), (tr(locale, 'bot_title.invalid'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data='bot_title_settings')]],
            state, message_id)
        return
    await state.set_state(None)
    try:
        await backend.update_bot_title(message.from_user.id, value)
        await bot_title_settings_view(message.chat.id, message.from_user.id, message_id,
                                      bot, backend, state)
    except BackendError as exc:
        await render(bot, message.chat.id,
            Screen(tr(locale, 'bot_title.title'), (_friendly_error(locale, exc),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data='bot_title_settings')]],
            state, message_id)


async def bot_title_settings_view(chat_id: int, user_id: int, message_id: int,
                                 bot: Bot, backend: BackendClient,
                                 state: FSMContext) -> None:
    locale = await _locale(state)
    title = (await backend.bot_title(user_id))['title']
    rows = [[InlineKeyboardButton(text=tr(locale, 'bot_title.edit'), callback_data='bot_title_edit')],
            [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminSettingsCallback().pack())]]
    await render(bot, chat_id, Screen(tr(locale, 'bot_title.title'),
        (tr(locale, 'bot_title.current', value=title),), embedded_buttons=True, navigation=True), rows, state, message_id)


async def show_request_policy(chat_id: int, user_id: int, message_id: int,
                              bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    locale = await _locale(state)
    policy = await backend.access_request_policy(user_id)
    sections = (
        Section(tr(locale, 'settings.rich.requests'), rows=(policy_choices(locale, 'request_policy_enabled', policy['enabled']),)),
        Section(tr(locale, 'settings.rich.notifications'), rows=(policy_choices(locale, 'request_policy_notify', policy.get('notify_requests', True)),)),
        Section(tr(locale, 'settings.rich.gate_message'), (policy['gate_message'],),
            rows=((InlineKeyboardButton(text=tr(locale, 'request_policy.edit_message'), callback_data='request_policy_edit'),),)))
    await render(bot, chat_id, Screen(tr(locale, 'request_policy.title'),
        sections=sections, embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminSettingsCallback().pack())]], state, message_id)


def policy_choices(locale, prefix, selected):
    return tuple(InlineKeyboardButton(text=tr(locale, 'settings.rich.enable' if value else 'settings.rich.disable'),
        callback_data=f'{prefix}:{"on" if value else "off"}',
        style='primary' if selected == value else None) for value in (True, False))


@router.callback_query(F.data.in_({'request_policy_enabled:on', 'request_policy_enabled:off',
                                  'request_policy_notify:on', 'request_policy_notify:off'}))
async def request_policy_choice_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                                   state: FSMContext) -> None:
    await query.answer()
    locale = await _locale(state)
    prefix, value = query.data.split(':')
    field = 'enabled' if prefix == 'request_policy_enabled' else 'notify_requests'
    try:
        await backend.update_access_request_policy(query.from_user.id, {field: value == 'on'})
        await show_request_policy(query.message.chat.id, query.from_user.id, query.message.message_id,
                                  bot, backend, state)
    except BackendError as exc:
        await render(bot, query.message.chat.id, Screen(tr(locale, 'request_policy.title'),
            (_friendly_error(locale, exc),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=RequestPolicyCallback().pack())]],
            state, query.message.message_id)


@router.callback_query(RequestPolicyCallback.filter())
async def request_policy_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                            state: FSMContext) -> None:
    await query.answer()
    await state.set_state(None)
    try:
        await show_request_policy(query.message.chat.id, query.from_user.id,
            query.message.message_id, bot, backend, state)
    except BackendError as exc:
        locale = await _locale(state)
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'request_policy.title'), (_friendly_error(locale, exc),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminSettingsCallback().pack())]],
            state, query.message.message_id)


@router.callback_query(F.data == 'request_policy_toggle')
async def request_policy_toggle_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                                   state: FSMContext) -> None:
    await query.answer()
    try:
        current = await backend.access_request_policy(query.from_user.id)
        await backend.update_access_request_policy(query.from_user.id,
            {'enabled': not current['enabled']})
        await show_request_policy(query.message.chat.id, query.from_user.id,
            query.message.message_id, bot, backend, state)
    except BackendError as exc:
        locale = await _locale(state)
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'request_policy.title'), (_friendly_error(locale, exc),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=RequestPolicyCallback().pack())]],
            state, query.message.message_id)


@router.callback_query(F.data == 'request_policy_notify')
async def request_policy_notify_cb(query: CallbackQuery, bot: Bot,
                                   backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    try:
        current = await backend.access_request_policy(query.from_user.id)
        await backend.update_access_request_policy(query.from_user.id,
            {'notify_requests': not current.get('notify_requests', True)})
        await show_request_policy(query.message.chat.id, query.from_user.id,
            query.message.message_id, bot, backend, state)
    except BackendError as exc:
        locale = await _locale(state)
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'request_policy.title'), (_friendly_error(locale, exc),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=RequestPolicyCallback().pack())]],
            state, query.message.message_id)


@router.callback_query(F.data == 'request_policy_edit')
async def request_policy_edit_cb(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await query.answer()
    locale = await _locale(state)
    await state.set_state(RequestPolicyState.waiting_for_gate_message)
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'request_policy.edit_title'), (tr(locale, 'request_policy.edit_prompt'),), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=RequestPolicyCallback().pack())]],
        state, query.message.message_id)


@router.message(RequestPolicyState.waiting_for_gate_message, F.text)
async def request_policy_text(message: Message, bot: Bot, backend: BackendClient,
                              state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    locale = await _locale(state)
    value = (message.text or '').strip()
    message_id = (await state.get_data()).get('control_message_id')
    if not value or len(value) > 500:
        await render(bot, message.chat.id,
            Screen(tr(locale, 'request_policy.edit_title'), (tr(locale, 'request_policy.invalid'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=RequestPolicyCallback().pack())]],
            state, message_id)
        return
    await state.set_state(None)
    try:
        await backend.update_access_request_policy(message.from_user.id, {'gate_message': value})
        await show_request_policy(message.chat.id, message.from_user.id, message_id,
                                  bot, backend, state)
    except BackendError as exc:
        await render(bot, message.chat.id,
            Screen(tr(locale, 'request_policy.title'), (_friendly_error(locale, exc),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=RequestPolicyCallback().pack())]],
            state, message_id)


@router.callback_query(SshKeyCallback.filter())
async def ssh_key_cb(query: CallbackQuery, bot: Bot,
                     backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    locale = await _locale(state)
    rows = [[InlineKeyboardButton(text=tr(locale, 'settings.ssh.details'),
        callback_data='ssh_key_details')],
        [InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=AdminSettingsCallback().pack())]]
    try:
        response = await backend.request('GET', '/api/v1/system/ssh-key',
                                         telegram_user_id=query.from_user.id)
        public_key = response['public_key']
        fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(
            base64.b64decode(public_key.split()[1])).digest()).decode().rstrip('=')
        screen = Screen(tr(locale, 'settings.ssh.title'),
            (tr(locale, 'settings.ssh.ready'),
             tr(locale, 'settings.ssh.summary')),
            sections=(Section(tr(locale, 'settings.rich.fingerprint'), (fingerprint,)),),
            uri=response['public_key'], uri_title=tr(locale, 'settings.ssh.public_key'),
            files=(('node-plane.pub', (response['public_key'] + '\n').encode()),),
            files_title=tr(locale, 'settings.rich.download'), embedded_buttons=True, navigation=True)
    except BackendError as exc:
        rows = rows[1:]
        screen = Screen(tr(locale, 'settings.ssh.unavailable'),
            (_friendly_error(locale, exc),), embedded_buttons=True, navigation=True)
    await render(bot, query.message.chat.id, screen, rows, state,
                 query.message.message_id)


@router.callback_query(F.data == 'ssh_key_details')
async def ssh_key_details_cb(query: CallbackQuery, bot: Bot,
                             state: FSMContext) -> None:
    await query.answer()
    locale = await _locale(state)
    lines = tuple(tr(locale, f'settings.ssh.step{index}') for index in range(1, 5))
    rows = [[InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=SshKeyCallback().pack())]]
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'settings.ssh.guide_title'), lines, embedded_buttons=True, navigation=True),
        rows, state, query.message.message_id)


async def show_updates(query: CallbackQuery, bot: Bot, backend: BackendClient,
                       state: FSMContext) -> None:
    from .admin_updates import show_overview
    await show_overview(query, bot, backend, state)


async def show_update_branches(query: CallbackQuery, bot: Bot,
                               backend: BackendClient, state: FSMContext) -> None:
    locale = await _locale(state)
    overview = await backend.updates_overview(query.from_user.id)
    selected = overview.get('branch')
    rows = [[InlineKeyboardButton(text=branch,
        callback_data=UpdateActionCallback(action=f'branch_{branch}').pack(),
        style='primary' if selected == branch else None)]
        for branch in ('main', 'dev')]
    if selected == 'dev':
        rows.append([InlineKeyboardButton(text=track,
            callback_data=UpdateActionCallback(action=f'track_{track}').pack(),
            style='primary' if overview.get('dev_track') == track else None) for track in ('tag', 'head')])
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=UpdatesCallback().pack())])
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'updates.branch_title'),
            (tr(locale, 'updates.branch_hint'),)),
        rows, state, query.message.message_id)


@router.callback_query(UpdatesCallback.filter())
async def updates_menu_cb(query: CallbackQuery, bot: Bot,
                          backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    locale = await _locale(state)
    try:
        await show_updates(query, bot, backend, state)
    except BackendError as exc:
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'updates.unavailable'), (_friendly_error(locale, exc),)),
            [[InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data=AdminSettingsCallback().pack())]], state,
            query.message.message_id)


async def show_release_cleanup(query: CallbackQuery, bot: Bot,
                               backend: BackendClient, state: FSMContext,
                               result_status: str | None = None) -> None:
    locale = await _locale(state)
    overview = await backend.cleanup_overview(query.from_user.id)
    lines = [
        tr(locale, 'cleanup.mode', value=overview.get('install_mode') or '—'),
        tr(locale, 'cleanup.current', value=overview.get('current_target') or '—'),
        tr(locale, 'cleanup.total', count=overview.get('total_releases', 0)),
        tr(locale, 'cleanup.kept', count=overview.get('kept_releases', 0)),
        tr(locale, 'cleanup.removable', count=overview.get('removable_releases', 0)),
        tr(locale, 'cleanup.size', size=round(overview.get('removable_size_bytes', 0) / 1048576, 1)),
    ]
    if not overview.get('supported'):
        lines.append(tr(locale, 'cleanup.unsupported'))
    elif not overview.get('removable_releases'):
        lines.append(tr(locale, 'cleanup.nothing'))
    if result_status:
        lines.insert(0, tr(locale, 'cleanup.result',
                           status=_update_status(locale, result_status)))
    rows = []
    if overview.get('supported') and overview.get('removable_releases'):
        rows.append([InlineKeyboardButton(text=tr(locale, 'cleanup.run'),
            callback_data=UpdateActionCallback(action='cleanup_run').pack())])
    rows.extend([[InlineKeyboardButton(text=tr(locale, 'updates.refresh'),
        callback_data=UpdateActionCallback(action='cleanup_menu').pack())],
        [InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=UpdatesCallback().pack())]])
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'cleanup.title'), tuple(lines)),
        rows, state, query.message.message_id)


@router.callback_query(UpdateActionCallback.filter())
async def update_action_cb(query: CallbackQuery, callback_data: UpdateActionCallback,
                           bot: Bot, backend: BackendClient,
                           state: FSMContext) -> None:
    await query.answer()
    locale = await _locale(state)
    action = callback_data.action
    try:
        if action == 'check':
            await backend.check_updates(query.from_user.id)
            await show_updates(query, bot, backend, state)
        elif action == 'auto_check':
            overview = await backend.updates_overview(query.from_user.id)
            await backend.update_preferences(query.from_user.id,
                {'auto_check_enabled': not overview.get('auto_check_enabled', False)})
            await show_updates(query, bot, backend, state)
        elif action == 'branch_menu':
            await show_update_branches(query, bot, backend, state)
        elif action in {'branch_main', 'branch_dev'}:
            await backend.update_preferences(query.from_user.id,
                {'branch': action.removeprefix('branch_')})
            await show_update_branches(query, bot, backend, state)
        elif action in {'track_tag', 'track_head'}:
            await backend.update_preferences(query.from_user.id, {'dev_track': action.removeprefix('track_')})
            await show_update_branches(query, bot, backend, state)
        elif action == 'run':
            from .admin_updates import confirm_latest
            await confirm_latest(query, bot, backend, state)
        elif action == 'cleanup_menu':
            await show_release_cleanup(query, bot, backend, state)
        elif action == 'cleanup_run':
            overview = await backend.cleanup_overview(query.from_user.id)
            if not overview.get('supported') or not overview.get('removable_releases'):
                await show_release_cleanup(query, bot, backend, state)
                return
            result = await backend.run_cleanup(query.from_user.id)
            await show_release_cleanup(query, bot, backend, state,
                result_status=result.get('status'))
    except BackendError as exc:
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'updates.unavailable'), (_friendly_error(locale, exc),)),
            [[InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data=UpdatesCallback().pack())]],
            state, query.message.message_id)


@router.callback_query(F.data.in_({'traffic', 'traffic:on', 'traffic:off'}))
async def traffic_settings_cb(query: CallbackQuery, bot: Bot,
                              backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    locale = await _locale(state)
    sections = ()
    rows = [[InlineKeyboardButton(text=tr(locale, 'back'),
                                  callback_data=AdminSettingsCallback().pack())]]
    try:
        if query.data != 'traffic':
            policy = await backend.update_traffic_policy(query.from_user.id, query.data == 'traffic:on')
        else:
            policy = await backend.traffic_policy(query.from_user.id)
        enabled = policy['enabled']
        choices = policy_choices(locale, 'traffic', enabled)
        lines = (tr(locale, 'traffic.description'), tr(locale, 'traffic.collection_note'),
                 tr(locale, 'traffic.interval', minutes=policy['interval_minutes']))
        scan = policy.get('last_scan')
        if scan:
            lines += (tr(locale, 'traffic.scan', at=scan['at'][:16].replace('T', ' '),
                         checked=scan['profiles_checked'], unknown=scan['unknown']),)
        else:
            lines += (tr(locale, 'traffic.not_checked'),)
        sections = (Section(tr(locale, 'settings.rich.collection'), (lines[0], lines[1]), rows=(choices,)),
            Section(tr(locale, 'settings.rich.scan'), lines[2:]))
    except BackendError as exc:
        lines = (_friendly_error(locale, exc),)
    await render(bot, query.message.chat.id, Screen(tr(locale, 'traffic.title'),
        () if sections else lines, sections=sections, embedded_buttons=True, navigation=True),
                 rows, state, query.message.message_id)
