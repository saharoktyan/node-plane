"""Named devices for AWG and VLESS; identity and authorization belong to the backend."""
from uuid import uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.state import State, StatesGroup

from ..backend import BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section
from ..navigation import remember_label, parent_path
from . import user
from .common import render, schedule_refresh

router = Router()

AWG = ('awg', 'vpn')


def target_args(target):
    """Callback arguments naming the configuration a picker issues; AWG is implied."""
    return () if tuple(target) == AWG else tuple(target)


class DeviceName(StatesGroup):
    waiting = State()


async def locale_for(state):
    return normalize_locale((await state.get_data()).get('locale'))


async def devices(backend, user_id, profile_id):
    return (await listing(backend, user_id, profile_id))[0]


async def listing(backend, user_id, profile_id):
    page = await backend.request('GET', f'/api/v1/profiles/{profile_id}/devices', telegram_user_id=user_id)
    return page['items'], page.get('device_limit')


def limit_state(locale, items, limit):
    """Lines describing the device limit, and whether another device fits."""
    if limit is None:
        return (), True
    active = sum(item['status'] == 'active' for item in items)
    if active >= limit:
        return (tr(locale, 'devices.limit_reached', limit=limit),), False
    return (tr(locale, 'devices.count', count=active, limit=limit),), True


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
    items, limit = await listing(backend, user_id, profile_id)
    items = [d for d in items if d['status'] != 'retired']
    items.sort(key=lambda d: (d['display_name'].casefold(), d['id']))
    limit_lines, can_add = limit_state(locale, items, limit)
    pages = max(1, (len(items) + 9) // 10)
    page = max(0, min(page, pages - 1))
    rows = [[user.button(user_id, d['display_name'] +
        (' · ' + tr(locale, 'devices.deleting') if d['status'] == 'deleting' else ''),
        'device_card', profile_id, d['id'])] for d in items[page * 10:(page + 1) * 10]]
    rows.extend(user.server_pagination(user_id, locale, page, pages, 'device_page', profile_id))
    if can_add:
        rows.append([user.button(user_id, tr(locale, 'devices.add'), 'device_create', profile_id)
            .model_copy(update={'style': 'primary'})])
    rows.append([user.button(user_id, tr(locale, 'back'), 'home')])
    await render(bot, chat_id, Screen(tr(locale, 'devices.title'),
        ((tr(locale, 'devices.description'),) if items else (tr(locale, 'devices.empty'),)) + limit_lines,
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
            user.button(user_id, tr(locale, 'devices.rotate'), 'device_rotate', profile_id, device_id)])
        rows.append([user.button(user_id, tr(locale, 'devices.delete'), 'device_delete', profile_id, device_id)
            .model_copy(update={'style': 'danger'})])
    elif item['status'] == 'deleting':
        rows.append([user.button(user_id, tr(locale, 'devices.refresh'), 'device_card', profile_id, device_id)])
    rows.append([user.button(user_id, tr(locale, 'back'), 'device_list', profile_id)])
    lines = (tr(locale, 'devices.management') if item['status'] == 'active' else tr(locale, 'devices.status.' + item['status']),)
    if item['status'] == 'active' and item.get('networks'):
        lines += (tr(locale, 'devices.networks', **item['networks']),)
    await render(bot, chat_id, Screen(item['display_name'], lines,
        embedded_buttons=True, navigation=True), rows, state, message_id)
    if item['status'] == 'deleting':
        schedule_refresh(state, lambda: show_card(chat_id, user_id, message_id, profile_id, device_id, bot, backend, state))


async def show_picker(chat_id, user_id, message_id, profile_id, node_key, bot, backend, state, *, force=False, page=0,
                      returning=False, target=AWG):
    locale = await locale_for(state)
    extra = target_args(target)
    items, limit = await listing(backend, user_id, profile_id)
    items = [d for d in items if d['status'] == 'active']
    items.sort(key=lambda d: (d['display_name'].casefold(), d['id']))
    for item in items:
        await remember_label(state, 'devices', item['id'], item['display_name'])
    if len(items) <= 1 and returning:
        await back_from_picker(chat_id, user_id, message_id, profile_id, node_key, bot, backend, state, target)
        return
    if not items:
        await prompt_name(chat_id, user_id, message_id, profile_id, None, bot, backend, state,
                          node_key=node_key, first_device=True, target=target)
        return
    if len(items) == 1 and not force:
        await select_device(chat_id, user_id, message_id, profile_id, node_key,
            items[0]['id'], bot, backend, state, target=target)
        return
    pages = max(1, (len(items) + 9) // 10)
    page = max(0, min(page, pages - 1))
    rows = [[user.button(user_id, d['display_name'], 'device_pick', profile_id, node_key, d['id'], *extra)]
        for d in items[page * 10:(page + 1) * 10]]
    rows.extend(user.server_pagination(user_id, locale, page, pages, 'device_picker_page', profile_id, node_key, *extra))
    limit_lines, can_add = limit_state(locale, items, limit)
    if can_add:
        rows.append([user.button(user_id, tr(locale, 'devices.add'), 'device_create', profile_id, node_key, *extra)
            .model_copy(update={'style': 'primary'})])
    rows.append([back_button(user_id, locale, profile_id, node_key, target)])
    picker = user.button(user_id, tr(locale, 'devices.title'), 'device_picker', profile_id, node_key, *extra)
    path = parent_path(picker.callback_data, locale, await state.get_data())
    await render(bot, chat_id, Screen(tr(locale, 'devices.title'), breadcrumbs=path[:-1],
        lines=((tr(locale, 'devices.empty'),) if not items else ()) + limit_lines, embedded_buttons=True,
        navigation=True), rows, state, message_id)


def back_button(user_id, locale, profile_id, node_key, target):
    """Back from the picker: to the server for AWG, to the transport choice for VLESS."""
    if tuple(target) == AWG:
        return user.button(user_id, tr(locale, 'back'), 'node', profile_id, node_key)
    return user.button(user_id, tr(locale, 'back'), 'protocol', profile_id, node_key, target[0])


async def back_from_picker(chat_id, user_id, message_id, profile_id, node_key, bot, backend, state, target):
    if tuple(target) == AWG:
        await user.show_node(chat_id, user_id, message_id, profile_id, node_key, bot, backend, state)
    else:
        await user.show_protocol(chat_id, user_id, message_id, profile_id, node_key, target[0], bot, backend, state)


async def select_device(chat_id, user_id, message_id, profile_id, node_key, device_id, bot, backend, state, *, target=AWG):
    extra = target_args(target)
    try:
        await user.issue(chat_id, user_id, message_id, profile_id, node_key, *target,
            bot, backend, state, device_id=device_id)
    except BackendError as error:
        if error.code != 'profile_not_synced':
            raise
        locale = await locale_for(state)
        await render(bot, chat_id, Screen(tr(locale, 'devices.preparing'),
            (tr(locale, 'devices.preparing_text'),), embedded_buttons=True, navigation=True),
            [[user.button(user_id, tr(locale, 'devices.refresh'), 'device_pick', profile_id, node_key, device_id, *extra)],
             [user.button(user_id, tr(locale, 'back'), 'device_picker_back', profile_id, node_key, *extra)]], state, message_id)


async def prompt_name(chat_id, user_id, message_id, profile_id, device_id, bot, backend, state, *, node_key=None,
                      first_device=False, target=AWG):
    if device_id:
        item = await get_device(backend, user_id, profile_id, device_id)
        revision = item['revision']
        if item['status'] != 'active':
            raise BackendError('device_unavailable', 409)
    else:
        profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}', telegram_user_id=user_id)
        revision = profile['desired_revision']
    await state.update_data(device_name_draft={'profile_id': profile_id, 'device_id': device_id,
        'revision': revision, 'owner': user_id, 'message_id': message_id, 'node_key': node_key,
        'first_device': first_device, 'target': list(target)})
    await state.set_state(DeviceName.waiting)
    locale = await locale_for(state)
    back = back_button(user_id, locale, profile_id, node_key, target) if first_device else user.button(
        user_id, tr(locale, 'back'), 'device_card' if device_id else ('device_picker' if node_key else 'device_list'),
        profile_id, *((device_id,) if device_id else ((node_key, *target_args(target)) if node_key else ())))
    await render(bot, chat_id, Screen(tr(locale, 'devices.first' if first_device else 'devices.rename' if device_id else 'devices.add'),
        (*((tr(locale, 'devices.first_hint'),) if first_device else ()), tr(locale, 'devices.name_prompt')),
        embedded_buttons=True, navigation=True), [[back]], state, message_id)


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
            'revision_conflict': 'devices.changed', 'device_limit_reached': 'devices.limit_refused'
            }.get(error.code, 'action.error.retry')
        # Another name cannot fix a reached limit, so stop asking for one.
        prompt = () if error.code == 'device_limit_reached' else (tr(locale, 'devices.name_prompt'),)
        await render(bot, message.chat.id, Screen(tr(locale, 'devices.add' if not device_id else 'devices.rename'),
            (tr(locale, key), *prompt), embedded_buttons=True, navigation=True),
            [[user.button(message.from_user.id, tr(locale, 'back'), 'node' if draft.get('node_key') else 'device_list',
                profile_id, *((draft['node_key'],) if draft.get('node_key') else ()))]],
            state, draft['message_id'])
        if error.code in {'revision_conflict', 'device_limit_reached'} or error.status in {401, 403, 404}:
            await state.set_state(None)
        return
    await state.set_state(None)
    await state.update_data(device_name_draft=None)
    if draft.get('node_key'):
        target = tuple(draft.get('target') or AWG)
        if draft.get('first_device'):
            await remember_label(state, 'devices', result['device']['id'], result['device']['display_name'])
            await select_device(message.chat.id, message.from_user.id, draft['message_id'], profile_id,
                draft['node_key'], result['device']['id'], bot, backend, state, target=target)
            return
        await show_picker(message.chat.id, message.from_user.id, draft['message_id'], profile_id,
            draft['node_key'], bot, backend, state, target=target)
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
            node_key=args[1] if action.name == 'device_create' and len(args) > 1 else None,
            target=tuple(args[2:4]) if action.name == 'device_create' and len(args) > 2 else AWG)
    elif action.name in {'device_picker', 'device_picker_back'}:
        await show_picker(chat_id, owner, message_id, args[0], args[1], bot, backend, state,
            force=True, returning=action.name == 'device_picker_back', target=tuple(args[2:4]) or AWG)
    elif action.name == 'device_picker_page':
        await show_picker(chat_id, owner, message_id, args[0], args[1], bot, backend, state,
            force=True, page=int(args[-1]), target=tuple(args[2:-1]) or AWG)
    elif action.name == 'device_pick':
        await select_device(chat_id, owner, message_id, *args[:3], bot, backend, state,
            target=tuple(args[3:5]) or AWG)
    elif action.name == 'device_rotate':
        item = await get_device(backend, owner, args[0], args[1])
        if item['status'] != 'active':
            raise BackendError('device_unavailable', 409)
        key = str(uuid4())
        locale = await locale_for(state)
        await render(bot, chat_id, Screen(tr(locale, 'devices.rotate_title', name=item['display_name']),
            (tr(locale, 'devices.rotate_warning'),), embedded_buttons=True, navigation=True),
            [[user.button(owner, tr(locale, 'devices.rotate'), 'device_rotate_confirm', *args,
                str(item['revision']), key).model_copy(update={'style': 'danger'})],
             [user.button(owner, tr(locale, 'back'), 'device_card', *args)]], state, message_id)
    elif action.name == 'device_rotate_confirm':
        # The key and revision come from the confirmation screen; a replay
        # returns the recorded result instead of rotating again.
        await backend.request('POST', f'/api/v1/profiles/{args[0]}/devices/{args[1]}/rotate',
            telegram_user_id=owner, command=True, command_key=args[3], revision=int(args[2]))
        await show_card(chat_id, owner, message_id, args[0], args[1], bot, backend, state)
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
