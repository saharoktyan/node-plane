"""Profile administration backed by the standalone backend API."""
from __future__ import annotations

from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from ..backend import BackendClient, BackendError
from ..screens import Screen
from .callbacks import (AccountsCallback, AccountCallback, NewProfileCallback,
    AdminProfilesCallback, AdminProfileCallback, GrantNodesCallback,
    GrantProtocolsCallback, AddGrantCallback, RemoveGrantCallback,
    ToggleFreezeCallback)
from .common import render
from .states import ProfileDraftState

router = Router()


class ProfileSearchState(StatesGroup):
    waiting_for_query = State()


@router.callback_query(AccountsCallback.filter())
async def accounts_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                      state: FSMContext) -> None:
    await query.answer()
    await state.clear()
    page = await backend.accounts(query.from_user.id)
    rows = [[InlineKeyboardButton(
        text=f"{account.get('telegram_user_id') or account['id'][:8]} · {account['status']}",
        callback_data=AccountCallback(account_id=account['id']).pack())]
        for account in page['items']]
    rows.append([InlineKeyboardButton(text='🔙 Назад', callback_data='admin_menu')])
    await render(bot, query.message.chat.id, Screen('Аккаунты',
        ('Выберите владельца VPN-профиля.',) if page['items'] else ('Аккаунтов пока нет.',)),
        rows, state, query.message.message_id)


@router.callback_query(AccountCallback.filter())
async def account_cb(query: CallbackQuery, callback_data: AccountCallback,
                     bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    account = await backend.request('GET', f'/api/v1/accounts/{callback_data.account_id}',
                                    telegram_user_id=query.from_user.id)
    rows = [[InlineKeyboardButton(text='Создать VPN-профиль',
        callback_data=NewProfileCallback(account_id=account['id']).pack())],
        [InlineKeyboardButton(text='🔙 Назад', callback_data=AccountsCallback().pack())]]
    await render(bot, query.message.chat.id,
        Screen(f"Аккаунт {account.get('telegram_user_id') or account['id'][:8]}",
               (f"Статус: {account['status']}", f"Роль: {account['role']}")),
        rows, state, query.message.message_id)


@router.callback_query(NewProfileCallback.filter())
async def new_profile_cb(query: CallbackQuery, callback_data: NewProfileCallback,
                         bot: Bot, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(ProfileDraftState.waiting_for_name)
    await state.update_data(profile_account_id=callback_data.account_id,
                            profile_command_key=str(uuid4()))
    await render(bot, query.message.chat.id,
        Screen('Новый профиль', ('Отправьте название профиля сообщением.',)),
        [[InlineKeyboardButton(text='Отмена', callback_data=AccountsCallback().pack())]],
        state, query.message.message_id)


@router.message(ProfileDraftState.waiting_for_name, F.text)
async def process_profile_name(message: Message, bot: Bot, backend: BackendClient,
                               state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    account_id = data.get('profile_account_id')
    message_id = data.get('control_message_id')
    name = message.text.strip()
    rows = [[InlineKeyboardButton(text='Отмена', callback_data=AccountsCallback().pack())]]
    if not account_id or not name or len(name) > 128:
        await render(bot, message.chat.id, Screen('Название профиля',
            ('Введите от 1 до 128 символов.',)), rows, state, message_id)
        return
    try:
        result = await backend.create_profile(message.from_user.id, account_id, name,
                                              data['profile_command_key'])
    except BackendError as exc:
        await render(bot, message.chat.id, Screen('Не удалось создать профиль',
            (f'Причина: {exc.code}', 'Повторите то же название или отмените действие.')),
            rows, state, message_id)
        return
    await state.clear()
    await show_admin_profile(message.chat.id, message.from_user.id, message_id,
                             result['profile']['id'], bot, backend, state)


@router.callback_query(AdminProfilesCallback.filter())
async def admin_profiles_cb(query: CallbackQuery, bot: Bot,
                            backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await state.clear()
    page = await backend.admin_profiles(query.from_user.id)
    rows = [[InlineKeyboardButton(text='➕ Создать профиль', callback_data=AccountsCallback().pack())],
            [InlineKeyboardButton(text='🔍 Найти профиль', callback_data='search_profile')]]
    rows.extend([[InlineKeyboardButton(text=item['display_name'],
        callback_data=AdminProfileCallback(profile_id=item['id']).pack())]
        for item in page['items']])
    rows.append([InlineKeyboardButton(text='🔙 Назад', callback_data='admin_menu')])
    await render(bot, query.message.chat.id, Screen('Профили',
        ('Управление профилями пользователей.',) if page['items'] else
        ('Пока нет зарегистрированных профилей.',)), rows, state,
        query.message.message_id)


@router.callback_query(F.data == 'search_profile')
async def search_profile_cb(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(ProfileSearchState.waiting_for_query)
    await render(bot, query.message.chat.id, Screen('Поиск профиля',
        ('Отправьте ID профиля или часть его названия.',)),
        [[InlineKeyboardButton(text='Отмена', callback_data=AdminProfilesCallback().pack())]],
        state, query.message.message_id)


@router.message(ProfileSearchState.waiting_for_query, F.text)
async def process_profile_search(message: Message, bot: Bot,
                                 backend: BackendClient, state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    text = message.text.strip().lower()
    data = await state.get_data()
    message_id = data.get('control_message_id')
    await state.clear()
    page = await backend.admin_profiles(message.from_user.id)
    matches = [item for item in page['items'] if text in item['id'].lower()
               or text in item['display_name'].lower()]
    rows = [[InlineKeyboardButton(text=item['display_name'],
        callback_data=AdminProfileCallback(profile_id=item['id']).pack())]
        for item in matches]
    rows.append([InlineKeyboardButton(text='🔙 Назад', callback_data=AdminProfilesCallback().pack())])
    await render(bot, message.chat.id, Screen('Результаты поиска',
        (f'Найдено: {len(matches)}',) if matches else ('Совпадений нет.',)),
        rows, state, message_id)


@router.callback_query(AdminProfileCallback.filter())
async def admin_profile_cb(query: CallbackQuery, callback_data: AdminProfileCallback,
                           bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await show_admin_profile(query.message.chat.id, query.from_user.id,
                             query.message.message_id, callback_data.profile_id,
                             bot, backend, state)


async def show_admin_profile(chat_id: int, user_id: int, message_id: int,
                             profile_id: str, bot: Bot, backend: BackendClient,
                             state: FSMContext) -> None:
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}',
                                    telegram_user_id=user_id)
    rows = [[InlineKeyboardButton(text='🔑 Доступы',
        callback_data=GrantNodesCallback(profile_id=profile_id).pack())],
        [InlineKeyboardButton(text='Разморозить' if profile['frozen'] else 'Заморозить',
        callback_data=ToggleFreezeCallback(profile_id=profile_id).pack())],
        [InlineKeyboardButton(text='🔙 Назад', callback_data=AdminProfilesCallback().pack())]]
    await render(bot, chat_id, Screen('Управление профилем',
        (f"Название: {profile['display_name']}", f"ID: {profile['id']}",
         f"Статус: {'заморожен' if profile['frozen'] else 'активен'}")),
        rows, state, message_id)


@router.callback_query(GrantNodesCallback.filter())
async def grant_nodes_cb(query: CallbackQuery, callback_data: GrantNodesCallback,
                         bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await show_grant_nodes(query.message.chat.id, query.from_user.id,
                           query.message.message_id, callback_data.profile_id,
                           bot, backend, state)


async def show_grant_nodes(chat_id: int, user_id: int, message_id: int,
                           profile_id: str, bot: Bot, backend: BackendClient,
                           state: FSMContext) -> None:
    nodes = await backend.admin_nodes(user_id)
    grants = await backend.profile_grants(user_id, profile_id)
    granted = {item['node_key'] for item in grants['items']}
    rows = [[InlineKeyboardButton(
        text=f"{'✅' if node['key'] in granted else '○'} {node['title']}",
        callback_data=GrantProtocolsCallback(profile_id=profile_id,
                                             node_key=node['key']).pack())]
        for node in nodes['items']]
    rows.append([InlineKeyboardButton(text='🔙 Назад',
        callback_data=AdminProfileCallback(profile_id=profile_id).pack())])
    await render(bot, chat_id, Screen('Доступ к серверам',
        ('Выберите сервер, затем протоколы доступа.',)), rows, state, message_id)


@router.callback_query(GrantProtocolsCallback.filter())
async def grant_protocols_cb(query: CallbackQuery, callback_data: GrantProtocolsCallback,
                             bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await show_grant_protocols(query.message.chat.id, query.from_user.id,
        query.message.message_id, callback_data.profile_id, callback_data.node_key,
        bot, backend, state)


async def show_grant_protocols(chat_id: int, user_id: int, message_id: int,
                               profile_id: str, node_key: str, bot: Bot,
                               backend: BackendClient, state: FSMContext) -> None:
    grants = await backend.profile_grants(user_id, profile_id)
    enabled = {item['protocol'] for item in grants['items'] if item['node_key'] == node_key}
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}',
                                 telegram_user_id=user_id)
    rows = []
    for protocol in node['protocols']:
        callback = (RemoveGrantCallback if protocol in enabled else AddGrantCallback)(
            profile_id=profile_id, node_key=node_key, protocol=protocol)
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if protocol in enabled else '○'} {protocol.upper()}",
            callback_data=callback.pack())])
    rows.append([InlineKeyboardButton(text='🔙 Назад',
        callback_data=GrantNodesCallback(profile_id=profile_id).pack())])
    await render(bot, chat_id, Screen('Доступ к протоколам',
        (f"Сервер: {node['title']}", 'Выберите протоколы для профиля.')),
        rows, state, message_id)


async def change_grant(query: CallbackQuery, profile_id: str, node_key: str,
                       protocol: str, add: bool, bot: Bot,
                       backend: BackendClient, state: FSMContext) -> None:
    user_id = query.from_user.id
    page = await backend.profile_grants(user_id, profile_id)
    grants = [dict(item) for item in page['items']]
    target = {'node_key': node_key, 'protocol': protocol}
    if add and target not in grants:
        grants.append(target)
    elif not add:
        grants = [item for item in grants if item != target]
    profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}',
                                    telegram_user_id=user_id)
    await backend.replace_grants(user_id, profile_id, profile['desired_revision'], grants)
    await show_grant_protocols(query.message.chat.id, user_id,
        query.message.message_id, profile_id, node_key, bot, backend, state)


@router.callback_query(AddGrantCallback.filter())
async def add_grant_cb(query: CallbackQuery, callback_data: AddGrantCallback,
                       bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await change_grant(query, callback_data.profile_id, callback_data.node_key,
        callback_data.protocol, True, bot, backend, state)


@router.callback_query(RemoveGrantCallback.filter())
async def remove_grant_cb(query: CallbackQuery, callback_data: RemoveGrantCallback,
                          bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await change_grant(query, callback_data.profile_id, callback_data.node_key,
        callback_data.protocol, False, bot, backend, state)


@router.callback_query(ToggleFreezeCallback.filter())
async def toggle_freeze_cb(query: CallbackQuery, callback_data: ToggleFreezeCallback,
                           bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    profile = await backend.request('GET', f'/api/v1/profiles/{callback_data.profile_id}',
                                    telegram_user_id=query.from_user.id)
    await backend.edit_profile(query.from_user.id, callback_data.profile_id,
        profile['desired_revision'], {'frozen': not profile['frozen']})
    await show_admin_profile(query.message.chat.id, query.from_user.id,
        query.message.message_id, callback_data.profile_id, bot, backend, state)
