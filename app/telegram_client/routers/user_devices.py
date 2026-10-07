"""Named AWG devices; identity and authorization belong to the backend."""
from uuid import uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.state import State, StatesGroup

from ..backend import BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section
from . import user
from .common import render

router = Router()


class DeviceName(StatesGroup):
    waiting = State()


async def locale_for(state):
    return normalize_locale((await state.get_data()).get('locale'))


async def devices(backend, user_id, profile_id):
    return (await backend.request('GET', f'/api/v1/profiles/{profile_id}/devices',
        telegram_user_id=user_id))['items']


async def show_profiles(chat_id, user_id, message_id, bot, backend, state):
    profiles = (await backend.profiles(user_id))['items']
    if len(profiles) == 1:
        await show_list(chat_id, user_id, message_id, profiles[0]['id'], bot, backend, state)
        return
    locale = await locale_for(state)
    rows = [[user.button(user_id, p['display_name'], 'device_list', p['id'])] for p in profiles]
    rows.append([user.button(user_id, tr(locale, 'back'), 'home')])
    await render(bot, chat_id, Screen(tr(locale, 'devices.title'),
        () if profiles else (tr(locale, 'profiles.empty'),), embedded_buttons=True,
        navigation=True), rows, state, message_id)


async def show_list(chat_id, user_id, message_id, profile_id, bot, backend, state, page=0):
    locale = await locale_for(state)
    items = [d for d in await devices(backend, user_id, profile_id) if d['status'] != 'retired']
    items.sort(key=lambda d: (d['display_name'].casefold(), d['id']))
    pages = max(1, (len(items) + 9) // 10)
    page = max(0, min(page, pages - 1))
    rows = [[user.button(user_id, d['display_name'] +
        (' · ' + tr(locale, 'devices.deleting') if d['status'] == 'deleting' else ''),
        'device_card', profile_id, d['id'])] for d in items[page * 10:(page + 1) * 10]]
    rows.extend(user.server_pagination(user_id, locale, page, pages, 'device_page', profile_id))
    rows.append([user.button(user_id, tr(locale, 'devices.add'), 'device_create', profile_id)
        .model_copy(update={'style': 'primary'})])
    rows.append([user.button(user_id, tr(locale, 'back'), 'home')])
    await render(bot, chat_id, Screen(tr(locale, 'devices.title'),
        (tr(locale, 'devices.description'),) if items else (tr(locale, 'devices.empty'),),
        embedded_buttons=True, navigation=True), rows, state, message_id)


async def get_device(backend, user_id, profile_id, device_id):
    item = next((d for d in await devices(backend, user_id, profile_id) if d['id'] == device_id), None)
    if item is None:
        raise BackendError('resource_not_found', 404)
    return item


async def show_card(chat_id, user_id, message_id, profile_id, device_id, bot, backend, state):
    locale = await locale_for(state)
    item = await get_device(backend, user_id, profile_id, device_id)
    rows = []
    if item['status'] == 'active':
        rows.append([user.button(user_id, tr(locale, 'devices.rename'), 'device_rename', profile_id, device_id),
            user.button(user_id, tr(locale, 'devices.delete'), 'device_delete', profile_id, device_id)
                .model_copy(update={'style': 'danger'})])
    elif item['status'] == 'deleting':
        rows.append([user.button(user_id, tr(locale, 'devices.refresh'), 'device_card', profile_id, device_id)])
    rows.append([user.button(user_id, tr(locale, 'back'), 'device_list', profile_id)])
    await render(bot, chat_id, Screen(item['display_name'],
        (tr(locale, 'devices.status.' + item['status']),),
        sections=(Section(tr(locale, 'devices.access'), (tr(locale, 'devices.access_text'),)),)
            if item['status'] == 'active' else (),
        embedded_buttons=True, navigation=True), rows, state, message_id)


async def show_picker(chat_id, user_id, message_id, profile_id, node_key, bot, backend, state, *, force=False, page=0):
    locale = await locale_for(state)
    items = [d for d in await devices(backend, user_id, profile_id) if d['status'] == 'active']
    items.sort(key=lambda d: (d['display_name'].casefold(), d['id']))
    if len(items) == 1 and not force:
        await select_device(chat_id, user_id, message_id, profile_id, node_key,
            items[0]['id'], bot, backend, state)
        return
    pages = max(1, (len(items) + 9) // 10)
    page = max(0, min(page, pages - 1))
    rows = [[user.button(user_id, d['display_name'], 'device_pick', profile_id, node_key, d['id'])]
        for d in items[page * 10:(page + 1) * 10]]
    rows.extend(user.server_pagination(user_id, locale, page, pages, 'device_picker_page', profile_id, node_key))
    rows.append([user.button(user_id, tr(locale, 'devices.add'), 'device_create', profile_id, node_key)
        .model_copy(update={'style': 'primary'})])
    rows.append([user.button(user_id, tr(locale, 'back'), 'profile', profile_id)])
    await render(bot, chat_id, Screen(tr(locale, 'devices.choose'),
        (tr(locale, 'devices.empty'),) if not items else (), embedded_buttons=True,
        navigation=True), rows, state, message_id)


async def select_device(chat_id, user_id, message_id, profile_id, node_key, device_id, bot, backend, state):
    try:
        await user.issue(chat_id, user_id, message_id, profile_id, node_key, 'awg', 'vpn',
            bot, backend, state, device_id=device_id)
    except BackendError as error:
        if error.code != 'profile_not_synced':
            raise
        locale = await locale_for(state)
        await render(bot, chat_id, Screen(tr(locale, 'devices.preparing'),
            (tr(locale, 'devices.preparing_text'),), embedded_buttons=True, navigation=True),
            [[user.button(user_id, tr(locale, 'devices.refresh'), 'device_pick', profile_id, node_key, device_id)],
             [user.button(user_id, tr(locale, 'back'), 'device_picker', profile_id, node_key)]], state, message_id)


async def prompt_name(chat_id, user_id, message_id, profile_id, device_id, bot, backend, state, *, node_key=None):
    if device_id:
        item = await get_device(backend, user_id, profile_id, device_id)
        revision = item['revision']
        if item['status'] != 'active':
            raise BackendError('device_unavailable', 409)
    else:
        profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}', telegram_user_id=user_id)
        revision = profile['desired_revision']
    await state.update_data(device_name_draft={'profile_id': profile_id, 'device_id': device_id,
        'revision': revision, 'owner': user_id, 'message_id': message_id, 'node_key': node_key})
    await state.set_state(DeviceName.waiting)
    locale = await locale_for(state)
    await render(bot, chat_id, Screen(tr(locale, 'devices.rename' if device_id else 'devices.add'),
        (tr(locale, 'devices.name_prompt'),), embedded_buttons=True, navigation=True),
        [[user.button(user_id, tr(locale, 'back'), 'device_card' if device_id else ('device_picker' if node_key else 'device_list'),
            profile_id, *((device_id,) if device_id else ((node_key,) if node_key else ())))]], state, message_id)


@router.message(DeviceName.waiting, F.text, ~F.text.startswith('/'))
async def name_message(message, bot, backend, state):
    draft = (await state.get_data()).get('device_name_draft')
    if not draft or not message.from_user or message.chat.type != 'private' or draft['owner'] != message.from_user.id:
        return
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    # Retrying the same submitted name retains its command identity; a changed
    # name is a distinct command even if the earlier result was uncertain.
    if draft.get('submitted_name') != message.text:
        draft.update(submitted_name=message.text, command_key=str(uuid4()))
        await state.update_data(device_name_draft=draft)
    profile_id, device_id = draft['profile_id'], draft['device_id']
    path = f'/api/v1/profiles/{profile_id}/devices' + (f'/{device_id}' if device_id else '')
    try:
        result = await backend.request('PATCH' if device_id else 'POST', path,
            telegram_user_id=message.from_user.id, body={'display_name': message.text},
            command=True, command_key=draft['command_key'], revision=draft['revision'])
    except BackendError as error:
        locale = await locale_for(state)
        key = {'device_name_conflict': 'devices.name_conflict', 'invalid_device_name': 'devices.name_invalid',
            'revision_conflict': 'devices.changed'}.get(error.code, 'action.error.retry')
        await render(bot, message.chat.id, Screen(tr(locale, 'devices.add' if not device_id else 'devices.rename'),
            (tr(locale, key), tr(locale, 'devices.name_prompt')), embedded_buttons=True, navigation=True),
            [[user.button(message.from_user.id, tr(locale, 'back'), 'device_list', profile_id)]],
            state, draft['message_id'])
        if error.code == 'revision_conflict' or error.status in {401, 403, 404}:
            await state.set_state(None)
        return
    await state.set_state(None)
    await state.update_data(device_name_draft=None)
    if draft.get('node_key'):
        await show_picker(message.chat.id, message.from_user.id, draft['message_id'], profile_id,
            draft['node_key'], bot, backend, state)
        return
    await show_card(message.chat.id, message.from_user.id, draft['message_id'], profile_id,
        result['device']['id'], bot, backend, state)


async def handle_action(action, query, bot, backend, state):
    chat_id, owner, message_id = query.message.chat.id, query.from_user.id, query.message.message_id
    args = action.args
    if action.name == 'device_profiles':
        await show_profiles(chat_id, owner, message_id, bot, backend, state)
    elif action.name in {'device_list', 'device_page'}:
        await show_list(chat_id, owner, message_id, args[0], bot, backend, state,
            int(args[1]) if action.name == 'device_page' else 0)
    elif action.name == 'device_card':
        await show_card(chat_id, owner, message_id, *args, bot, backend, state)
    elif action.name in {'device_create', 'device_rename'}:
        await prompt_name(chat_id, owner, message_id, args[0],
            args[1] if action.name == 'device_rename' else None, bot, backend, state,
            node_key=args[1] if action.name == 'device_create' and len(args) > 1 else None)
    elif action.name in {'device_picker', 'device_picker_page'}:
        await show_picker(chat_id, owner, message_id, args[0], args[1], bot, backend, state,
            force=True, page=int(args[2]) if action.name == 'device_picker_page' else 0)
    elif action.name == 'device_pick':
        await select_device(chat_id, owner, message_id, *args, bot, backend, state)
    elif action.name == 'device_delete':
        item = await get_device(backend, owner, args[0], args[1])
        if item['status'] != 'active':
            raise BackendError('device_unavailable', 409)
        key = str(uuid4())
        locale = await locale_for(state)
        await render(bot, chat_id, Screen(tr(locale, 'devices.delete_title', name=item['display_name']),
            (tr(locale, 'devices.delete_warning'),), embedded_buttons=True, navigation=True),
            [[user.button(owner, tr(locale, 'devices.delete'), 'device_delete_confirm', *args,
                str(item['revision']), key).model_copy(update={'style': 'danger'})],
             [user.button(owner, tr(locale, 'back'), 'device_card', *args)]], state, message_id)
        current_message = (await state.get_data()).get('control_message_id', message_id)
        await state.update_data(device_delete_confirmation={'key': key, 'message_id': current_message})
    elif action.name == 'device_delete_confirm':
        confirmation = (await state.get_data()).get('device_delete_confirmation')
        if confirmation != {'key': args[3], 'message_id': message_id}:
            await show_card(chat_id, owner, message_id, args[0], args[1], bot, backend, state)
            return
        await backend.request('DELETE', f'/api/v1/profiles/{args[0]}/devices/{args[1]}',
            telegram_user_id=owner, command=True, command_key=args[3], revision=int(args[2]))
        await state.update_data(device_delete_confirmation=None)
        await show_card(chat_id, owner, message_id, args[0], args[1], bot, backend, state)
