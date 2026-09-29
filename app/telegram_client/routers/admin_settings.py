"""Controller settings backed by available backend endpoints."""
from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..backend import BackendClient, BackendError
from ..screens import Screen
from .callbacks import AdminSettingsCallback, RequestsCallback, UpdatesCallback
from .common import render

router = Router()


class SshKeyCallback(CallbackData, prefix='ssh_key'):
    pass


class UpdateActionCallback(CallbackData, prefix='upd_act'):
    action: str


@router.callback_query(AdminSettingsCallback.filter())
async def admin_settings_cb(query: CallbackQuery, bot: Bot,
                            backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    rows = [
        [InlineKeyboardButton(text='🎫 Заявки на доступ', callback_data=RequestsCallback().pack())],
        [InlineKeyboardButton(text='Обновления', callback_data=UpdatesCallback().pack())],
        [InlineKeyboardButton(text='🔐 SSH-ключ', callback_data=SshKeyCallback().pack())],
        [InlineKeyboardButton(text='🔙 Назад', callback_data='admin_menu')],
    ]
    await render(bot, query.message.chat.id, Screen('Настройки системы',
        ('Управление контроллером.',)), rows, state, query.message.message_id)


@router.callback_query(SshKeyCallback.filter())
async def ssh_key_cb(query: CallbackQuery, bot: Bot,
                     backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    rows = [[InlineKeyboardButton(text='🔙 Назад',
        callback_data=AdminSettingsCallback().pack())]]
    try:
        response = await backend.request('GET', '/api/v1/system/ssh-key',
                                         telegram_user_id=query.from_user.id)
        screen = Screen('SSH-ключ контроллера',
            ('Добавьте этот публичный ключ в authorized_keys целевого VPS.',),
            'Публичный ключ', (response['public_key'],))
    except BackendError as exc:
        screen = Screen('SSH-ключ недоступен', (f'Причина: {exc.code}',))
    await render(bot, query.message.chat.id, screen, rows, state,
                 query.message.message_id)


async def show_updates(query: CallbackQuery, bot: Bot, backend: BackendClient,
                       state: FSMContext) -> None:
    overview = await backend.updates_overview(query.from_user.id)
    status = overview.get('last_run_status') or 'never'
    lines = (
        f"Ветка: {overview.get('branch') or 'неизвестна'}",
        f"Текущая версия: {overview.get('current_label') or overview.get('current_version') or '-'}",
        f"Доступная версия: {overview.get('remote_label') or 'не проверена'}",
        f'Последнее обновление: {status}',
    )
    rows = [[InlineKeyboardButton(text='🔍 Проверить обновления',
        callback_data=UpdateActionCallback(action='check').pack())]]
    if status == 'running':
        rows.insert(0, [InlineKeyboardButton(text='🔄 Обновить статус',
            callback_data=UpdatesCallback().pack())])
    elif overview.get('update_supported') and overview.get('update_available'):
        rows.append([InlineKeyboardButton(text='🚀 Обновить систему',
            callback_data=UpdateActionCallback(action='run').pack())])
    rows.append([InlineKeyboardButton(text='🧹 Очистить старые релизы',
        callback_data=UpdateActionCallback(action='cleanup_menu').pack())])
    rows.append([InlineKeyboardButton(text='🔙 Назад',
        callback_data=AdminSettingsCallback().pack())])
    details = tuple(filter(None, (overview.get('last_error'),
        (overview.get('last_run_log_tail') or '')[-1000:])))
    await render(bot, query.message.chat.id, Screen('Обновления', lines,
        'Последние сообщения' if details else None, details), rows, state,
        query.message.message_id)


@router.callback_query(UpdatesCallback.filter())
async def updates_menu_cb(query: CallbackQuery, bot: Bot,
                          backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    try:
        await show_updates(query, bot, backend, state)
    except BackendError as exc:
        await render(bot, query.message.chat.id,
            Screen('Обновления недоступны', (f'Причина: {exc.code}',)),
            [[InlineKeyboardButton(text='🔙 Назад',
                callback_data=AdminSettingsCallback().pack())]], state,
            query.message.message_id)


@router.callback_query(UpdateActionCallback.filter())
async def update_action_cb(query: CallbackQuery, callback_data: UpdateActionCallback,
                           bot: Bot, backend: BackendClient,
                           state: FSMContext) -> None:
    await query.answer()
    action = callback_data.action
    try:
        if action == 'check':
            await backend.check_updates(query.from_user.id)
            await show_updates(query, bot, backend, state)
        elif action == 'run':
            await backend.run_update(query.from_user.id)
            await show_updates(query, bot, backend, state)
        elif action == 'cleanup_menu':
            overview = await backend.cleanup_overview(query.from_user.id)
            rows = [[InlineKeyboardButton(text='🧹 Запустить очистку',
                callback_data=UpdateActionCallback(action='cleanup_run').pack())],
                [InlineKeyboardButton(text='🔙 Назад', callback_data=UpdatesCallback().pack())]]
            await render(bot, query.message.chat.id, Screen('Очистка релизов',
                (f"Статус: {overview.get('last_run_status', 'never')}",),
                'Последние сообщения' if overview.get('last_run_log_tail') else None,
                ((overview.get('last_run_log_tail') or '')[-1000:],)
                if overview.get('last_run_log_tail') else ()),
                rows, state, query.message.message_id)
        elif action == 'cleanup_run':
            await backend.run_cleanup(query.from_user.id)
            await render(bot, query.message.chat.id, Screen('Очистка релизов',
                ('Задача запущена. Обновите статус через меню очистки.',)),
                [[InlineKeyboardButton(text='🔄 Проверить статус',
                    callback_data=UpdateActionCallback(action='cleanup_menu').pack())]],
                state, query.message.message_id)
    except BackendError as exc:
        await render(bot, query.message.chat.id, Screen('Действие недоступно',
            (f'Причина: {exc.code}',)),
            [[InlineKeyboardButton(text='🔙 Назад', callback_data=UpdatesCallback().pack())]],
            state, query.message.message_id)
