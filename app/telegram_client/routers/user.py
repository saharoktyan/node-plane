"""Member screens. Every data read and mutation goes through the backend API."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
import secrets
import time

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message
import qrcode
from qrcode.exceptions import DataOverflowError

from ..backend import BackendClient, BackendError
from ..screens import Screen
from .callbacks import HomeCallback
from .common import render

router = Router()


@dataclass(frozen=True)
class Action:
    owner_id: int
    name: str
    args: tuple[str, ...]
    expires_at: float


actions: dict[str, Action] = {}


def button(owner_id: int, label: str, name: str, *args: str) -> InlineKeyboardButton:
    now = time.monotonic()
    if len(actions) > 1000:
        for stale in [key for key, action in actions.items() if action.expires_at < now]:
            actions.pop(stale, None)
    token = secrets.token_urlsafe(10)
    actions[token] = Action(owner_id, name, args, now + 900)
    return InlineKeyboardButton(text=label, callback_data='u:' + token)


@router.message(CommandStart())
async def start_cmd(message: Message, bot: Bot, backend: BackendClient,
                    state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await backend.resolve(message.from_user.id)
        await show_home(message.chat.id, message.from_user.id, bot, backend, state)
    except BackendError:
        await render(bot, message.chat.id,
            Screen('Node Plane', ('Сервис временно недоступен. Повторите /start позже.',)),
            [], state)


@router.callback_query(HomeCallback.filter())
async def home_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                  state: FSMContext) -> None:
    await query.answer()
    if query.message:
        await state.clear()
        await show_home(query.message.chat.id, query.from_user.id, bot, backend,
                        state, query.message.message_id)


async def show_home(chat_id: int, user_id: int, bot: Bot, backend: BackendClient,
                    state: FSMContext, message_id: int | None = None) -> None:
    account = await backend.me(user_id)
    rows: list[list[InlineKeyboardButton]] = []
    if account['status'] != 'approved':
        requests = await backend.request('GET', '/api/v1/me/access-requests?limit=25',
                                         telegram_user_id=user_id)
        pending = any(item['status'] == 'pending' for item in requests['items'])
        if not pending:
            rows.append([button(user_id, '🚀 Запросить доступ', 'request_access')])
        lines = ('Заявка ожидает решения администратора.',) if pending else (
            'Запросите доступ, чтобы получать VPN-конфиги.',)
    else:
        rows.append([button(user_id, '🔑 Получить конфиг', 'profiles')])
        rows.append([button(user_id, '👤 Мой аккаунт', 'account_info')])
        lines = ('Выберите действие.',)
    if account['role'] == 'admin' and account['status'] == 'approved':
        rows.append([button(user_id, '👑 Админ-панель', 'admin_menu')])
    await render(bot, chat_id, Screen('Node Plane', lines), rows, state, message_id)


async def show_profiles(chat_id: int, user_id: int, message_id: int, bot: Bot,
                        backend: BackendClient, state: FSMContext) -> None:
    page = await backend.profiles(user_id)
    rows = [[button(user_id, item['display_name'], 'profile', item['id'])]
            for item in page['items']]
    rows.append([button(user_id, '🔙 Назад', 'home')])
    await render(bot, chat_id, Screen('Мои профили',
        ('Выберите профиль.',) if page['items'] else ('Профилей пока нет.',)),
        rows, state, message_id)


async def show_profile(chat_id: int, user_id: int, message_id: int,
                       profile_id: str, bot: Bot, backend: BackendClient,
                       state: FSMContext) -> None:
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}',
                                    telegram_user_id=user_id)
    page = await backend.profile_nodes(user_id, profile_id)
    rows = [[button(user_id, f"{node['flag']} {node['title']}".strip(),
                    'node', profile_id, node['key'])] for node in page['items']]
    rows.append([button(user_id, '🔙 Назад', 'profiles')])
    status = 'Заморожен' if profile['frozen'] else 'Активен'
    await render(bot, chat_id, Screen(profile['display_name'],
        (f'Статус: {status}', 'Выберите сервер.') if page['items'] else
        (f'Статус: {status}', 'Доступных серверов пока нет.')),
        rows, state, message_id)


async def show_node(chat_id: int, user_id: int, message_id: int,
                    profile_id: str, node_key: str, bot: Bot,
                    backend: BackendClient, state: FSMContext) -> None:
    page = await backend.profile_nodes(user_id, profile_id)
    node = next((item for item in page['items'] if item['key'] == node_key), None)
    if node is None:
        await show_profile(chat_id, user_id, message_id, profile_id, bot, backend, state)
        return
    rows = [[button(user_id, f"{protocol['kind'].upper()} · {transport.upper()}",
                    'issue', profile_id, node_key, protocol['kind'], transport)]
            for protocol in node['protocols'] for transport in protocol['transports']]
    rows.append([button(user_id, '🔙 Назад', 'profile', profile_id)])
    await render(bot, chat_id, Screen(node['title'],
        (f"Регион: {node['region']}", 'Выберите формат конфига.')),
        rows, state, message_id)


async def show_issuance(chat_id: int, user_id: int, message_id: int,
                        issuance_id: str, bot: Bot, backend: BackendClient,
                        state: FSMContext) -> None:
    result = await backend.issuance(user_id, issuance_id)
    profile_id, node_key = result['profile_id'], result['node_key']
    rows = [[button(user_id, '🔙 Назад', 'node', profile_id, node_key)]]
    if result['status'] == 'succeeded':
        artifact = await backend.artifact(user_id, issuance_id)
        content = artifact['content']
        filename = artifact['filename'] or f"{result['protocol']}-{node_key}.txt"
        await bot.send_document(chat_id, BufferedInputFile(content.encode(), filename))
        if len(content.encode()) <= 2500:
            rows.insert(0, [button(user_id, 'Показать QR', 'qr', issuance_id)])
        details = (content,) if result['protocol'] == 'xray' else ()
        await render(bot, chat_id, Screen('Конфиг готов',
            ('Импортируйте прикреплённый файл в VPN-клиент.',),
            'VLESS-ссылка' if details else None, details), rows, state, message_id)
    elif result['status'] in {'blocked', 'superseded', 'failed'}:
        await render(bot, chat_id, Screen('Конфиг недоступен',
            ('Проверьте доступ и состояние ноды, затем запросите новый конфиг.',)),
            rows, state, message_id)
    else:
        rows.insert(0, [button(user_id, 'Обновить', 'issuance', issuance_id)])
        await render(bot, chat_id, Screen('Конфиг готовится',
            ('Backend ещё обрабатывает запрос.',)), rows, state, message_id)


async def issue(chat_id: int, user_id: int, message_id: int, profile_id: str,
                node_key: str, protocol: str, transport: str, bot: Bot,
                backend: BackendClient, state: FSMContext) -> None:
    queued = await backend.issue(user_id, profile_id, node_key, protocol, transport)
    await render(bot, chat_id, Screen('Подготовка конфига',
        ('Проверяем доступ и состояние ноды…',)),
        [[button(user_id, '🔙 Назад', 'node', profile_id, node_key)]], state, message_id)
    for _ in range(15):
        result = await backend.issuance(user_id, queued['id'])
        if result['status'] in {'succeeded', 'blocked', 'superseded', 'failed'}:
            break
        await asyncio.sleep(1)
    await show_issuance(chat_id, user_id, message_id, queued['id'], bot, backend, state)


async def show_qr(chat_id: int, user_id: int, message_id: int, issuance_id: str,
                  bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    result = await backend.issuance(user_id, issuance_id)
    artifact = await backend.artifact(user_id, issuance_id)
    content = artifact['content']
    rows = [[button(user_id, '🔙 Назад', 'node', result['profile_id'], result['node_key'])]]
    if len(content.encode()) > 2500:
        await render(bot, chat_id, Screen('QR недоступен',
            ('Конфиг слишком длинный для QR. Используйте файл.',)), rows, state, message_id)
        return
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L,
                         box_size=6, border=4)
    code.add_data(content)
    try:
        code.make(fit=True)
    except DataOverflowError:
        await render(bot, chat_id, Screen('QR недоступен',
            ('Конфиг слишком длинный для QR. Используйте файл.',)), rows, state, message_id)
        return
    image = BytesIO()
    code.make_image(fill_color='black', back_color='white').save(image, format='PNG')
    await bot.send_photo(chat_id, BufferedInputFile(image.getvalue(), 'config.png'))
    await render(bot, chat_id, Screen('QR готов', ('Отсканируйте отправленное изображение.',)),
                 rows, state, message_id)


@router.callback_query(F.data.startswith('u:'))
async def user_action_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                         state: FSMContext) -> None:
    if query.message is None or query.from_user is None or query.message.chat.type != 'private':
        return
    token = (query.data or '')[2:]
    action = actions.get(token)
    if action is None or action.owner_id != query.from_user.id or action.expires_at < time.monotonic():
        await query.answer('Экран устарел. Отправьте /start.', show_alert=True)
        return
    actions.pop(token, None)
    await query.answer()
    chat_id, user_id, message_id = query.message.chat.id, query.from_user.id, query.message.message_id
    try:
        if action.name == 'home':
            await show_home(chat_id, user_id, bot, backend, state, message_id)
        elif action.name == 'profiles':
            await show_profiles(chat_id, user_id, message_id, bot, backend, state)
        elif action.name == 'profile':
            await show_profile(chat_id, user_id, message_id, action.args[0], bot, backend, state)
        elif action.name == 'node':
            await show_node(chat_id, user_id, message_id, *action.args, bot, backend, state)
        elif action.name == 'issue':
            await issue(chat_id, user_id, message_id, *action.args, bot, backend, state)
        elif action.name == 'issuance':
            await show_issuance(chat_id, user_id, message_id, action.args[0], bot, backend, state)
        elif action.name == 'qr':
            await show_qr(chat_id, user_id, message_id, action.args[0], bot, backend, state)
        elif action.name == 'account_info':
            account = await backend.me(user_id)
            await render(bot, chat_id, Screen('Мой аккаунт',
                (f"ID: {account['id']}", f"Статус: {account['status']}")),
                [[button(user_id, '🔙 Назад', 'home')]], state, message_id)
        elif action.name == 'request_access':
            request = await backend.request_access(user_id)
            await show_home(chat_id, user_id, bot, backend, state, message_id)
            from .admin_requests import notify_admins
            await notify_admins(bot, backend, request['id'])
        elif action.name == 'admin_menu':
            await show_admin_menu(chat_id, user_id, message_id, bot, state)
    except BackendError as exc:
        await render(bot, chat_id, Screen('Действие недоступно',
            (f'Причина: {exc.code}',)), [[button(user_id, 'Главная', 'home')]],
            state, message_id)


async def show_admin_menu(chat_id: int, user_id: int, message_id: int,
                          bot: Bot, state: FSMContext) -> None:
    from .callbacks import (AccountsCallback, AdminNodesCallback,
                            AdminProfilesCallback, AdminSettingsCallback, RequestsCallback)
    rows = [
        [InlineKeyboardButton(text='🎫 Заявки', callback_data=RequestsCallback().pack())],
        [InlineKeyboardButton(text='🖥 Серверы', callback_data=AdminNodesCallback().pack()),
         InlineKeyboardButton(text='👥 Профили', callback_data=AdminProfilesCallback().pack())],
        [InlineKeyboardButton(text='👤 Аккаунты', callback_data=AccountsCallback().pack())],
        [InlineKeyboardButton(text='⚙️ Настройки', callback_data=AdminSettingsCallback().pack())],
        [button(user_id, '🔙 Назад', 'home')],
    ]
    await render(bot, chat_id, Screen('Админ-панель', ('Управление Node Plane.',)),
                 rows, state, message_id)


@router.callback_query(F.data == 'admin_menu')
async def admin_menu_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                        state: FSMContext) -> None:
    await query.answer()
    account = await backend.me(query.from_user.id)
    if account['role'] != 'admin' or account['status'] != 'approved':
        return
    await show_admin_menu(query.message.chat.id, query.from_user.id,
                          query.message.message_id, bot, state)
