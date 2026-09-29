"""Controller settings backed by available backend endpoints."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from ..backend import BackendClient, BackendError
from ..screens import Screen
from ..i18n import normalize_locale, tr
from .callbacks import AdminSettingsCallback, RequestsCallback, RequestPolicyCallback, UpdatesCallback
from .common import render

router = Router()


class RequestPolicyState(StatesGroup):
    waiting_for_gate_message = State()


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
    locale = await _locale(state)
    rows = [
        [InlineKeyboardButton(text=tr(locale, 'settings.admin.requests'), callback_data=RequestsCallback().pack())],
        [InlineKeyboardButton(text=tr(locale, 'settings.admin.request_policy'), callback_data=RequestPolicyCallback().pack())],
        [InlineKeyboardButton(text=tr(locale, 'settings.admin.updates'), callback_data=UpdatesCallback().pack())],
        [InlineKeyboardButton(text=tr(locale, 'settings.admin.ssh_key'), callback_data=SshKeyCallback().pack())],
        [InlineKeyboardButton(text=tr(locale, 'back'), callback_data='admin_menu')],
    ]
    await render(bot, query.message.chat.id, Screen(tr(locale, 'settings.admin.title'),
        (tr(locale, 'settings.admin.description'),)), rows, state, query.message.message_id)


async def show_request_policy(chat_id: int, user_id: int, message_id: int,
                              bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    locale = await _locale(state)
    policy = await backend.access_request_policy(user_id)
    rows = [
        [InlineKeyboardButton(text=tr(locale, 'request_policy.toggle_on' if policy['enabled']
            else 'request_policy.toggle_off'), callback_data='request_policy_toggle')],
        [InlineKeyboardButton(text=tr(locale, 'request_policy.edit_message'), callback_data='request_policy_edit')],
        [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminSettingsCallback().pack())],
    ]
    await render(bot, chat_id, Screen(tr(locale, 'request_policy.title'),
        (tr(locale, 'request_policy.status', value=tr(locale, 'request_policy.enabled' if policy['enabled'] else 'request_policy.disabled')),
         tr(locale, 'request_policy.current_message', value=policy['gate_message']))),
        rows, state, message_id)


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
            Screen(tr(locale, 'request_policy.title'), (_friendly_error(locale, exc),)),
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
            Screen(tr(locale, 'request_policy.title'), (_friendly_error(locale, exc),)),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=RequestPolicyCallback().pack())]],
            state, query.message.message_id)


@router.callback_query(F.data == 'request_policy_edit')
async def request_policy_edit_cb(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await query.answer()
    locale = await _locale(state)
    await state.set_state(RequestPolicyState.waiting_for_gate_message)
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'request_policy.edit_title'), (tr(locale, 'request_policy.edit_prompt'),)),
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
            Screen(tr(locale, 'request_policy.edit_title'), (tr(locale, 'request_policy.invalid'),)),
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
            Screen(tr(locale, 'request_policy.title'), (_friendly_error(locale, exc),)),
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
        screen = Screen(tr(locale, 'settings.ssh.title'),
            (tr(locale, 'settings.ssh.ready'),
             tr(locale, 'settings.ssh.summary')),
            tr(locale, 'settings.ssh.public_key'), (response['public_key'],))
    except BackendError as exc:
        rows = rows[1:]
        screen = Screen(tr(locale, 'settings.ssh.unavailable'),
            (_friendly_error(locale, exc),))
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
        Screen(tr(locale, 'settings.ssh.guide_title'), lines),
        rows, state, query.message.message_id)


async def show_updates(query: CallbackQuery, bot: Bot, backend: BackendClient,
                       state: FSMContext) -> None:
    locale = await _locale(state)
    overview = await backend.updates_overview(query.from_user.id)
    status = overview.get('last_run_status') or 'never'
    lines = (
        tr(locale, 'updates.branch', value=overview.get('branch') or '—'),
        tr(locale, 'updates.track', value=overview.get('dev_track') or '—'),
        tr(locale, 'updates.install_mode', value=overview.get('install_mode') or '—'),
        tr(locale, 'updates.source', value=overview.get('source_dir') or '—'),
        tr(locale, 'updates.current', value=overview.get('current_label') or overview.get('current_version') or '—'),
        tr(locale, 'updates.available', value=overview.get('remote_label') or
           tr(locale, 'updates.not_checked')),
        tr(locale, 'updates.auto_check', value=tr(locale,
            'updates.enabled' if overview.get('auto_check_enabled') else 'updates.disabled')),
        tr(locale, 'updates.last_check', value=overview.get('last_checked_at') or '—'),
        tr(locale, 'updates.check_status', value=_update_status(locale, overview.get('last_status'))),
        tr(locale, 'updates.last_run', value=_update_status(locale, status)),
    )
    rows = [[InlineKeyboardButton(text=tr(locale, 'updates.check'),
        callback_data=UpdateActionCallback(action='check').pack())]]
    rows.append([InlineKeyboardButton(text=tr(locale, 'updates.choose_branch'),
        callback_data=UpdateActionCallback(action='branch_menu').pack()),
        InlineKeyboardButton(text=tr(locale, 'updates.toggle_auto'),
        callback_data=UpdateActionCallback(action='auto_check').pack())])
    if status == 'running':
        rows.insert(0, [InlineKeyboardButton(text=tr(locale, 'updates.refresh'),
            callback_data=UpdatesCallback().pack())])
    elif overview.get('update_supported') and overview.get('update_available'):
        rows.append([InlineKeyboardButton(text=tr(locale, 'updates.run'),
            callback_data=UpdateActionCallback(action='run').pack())])
    rows.append([InlineKeyboardButton(text=tr(locale, 'updates.cleanup'),
        callback_data=UpdateActionCallback(action='cleanup_menu').pack())])
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=AdminSettingsCallback().pack())])
    details = tuple(filter(None, (
        tr(locale, 'updates.error', value=overview['last_error']) if overview.get('last_error') else '',
        (overview.get('last_run_log_tail') or '')[-1000:])))
    await render(bot, query.message.chat.id, Screen(tr(locale, 'updates.title'), lines,
        tr(locale, 'updates.details') if details else None, details), rows, state,
        query.message.message_id)


async def show_update_branches(query: CallbackQuery, bot: Bot,
                               backend: BackendClient, state: FSMContext) -> None:
    locale = await _locale(state)
    overview = await backend.updates_overview(query.from_user.id)
    selected = overview.get('branch')
    rows = [[InlineKeyboardButton(text=f"{'✅ ' if selected == branch else ''}{branch}",
        callback_data=UpdateActionCallback(action=f'branch_{branch}').pack())]
        for branch in ('main', 'dev')]
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
        elif action == 'run':
            await backend.run_update(query.from_user.id)
            await show_updates(query, bot, backend, state)
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
