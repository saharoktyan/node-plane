"""Member screens. Every data read and mutation goes through the backend API."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
import secrets
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message
import qrcode
from qrcode.exceptions import DataOverflowError

from ..backend import BackendClient, BackendError
from ..screens import Screen
from ..i18n import normalize_locale, tr
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


async def clear_artifacts(bot: Bot, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    for message_id in data.get('artifact_message_ids', []):
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramAPIError:
            pass
    await state.update_data(artifact_message_ids=[], delivered_issuances=[],
                            issuance_poll_token=None)


async def track_artifact(state: FSMContext, message_id: int) -> None:
    data = await state.get_data()
    message_ids = list(data.get('artifact_message_ids', []))
    if message_id not in message_ids:
        message_ids.append(message_id)
    await state.update_data(artifact_message_ids=message_ids[-20:])


@router.message(CommandStart())
async def start_cmd(message: Message, bot: Bot, backend: BackendClient,
                    state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    await state.set_state(None)
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    try:
        await clear_artifacts(bot, message.chat.id, state)
        await backend.resolve(message.from_user.id, username=message.from_user.username,
            first_name=message.from_user.first_name, last_name=message.from_user.last_name,
            language_code=message.from_user.language_code)
        account = await backend.me(message.from_user.id)
        await state.update_data(locale=normalize_locale(account.get('locale') or account.get('language_code')))
        if not account.get('locale_selected'):
            await show_language_picker(message.chat.id, message.from_user.id, bot, backend, state)
        else:
            await show_home(message.chat.id, message.from_user.id, bot, backend, state)
    except BackendError:
        await render(bot, message.chat.id,
            Screen(tr(message.from_user.language_code, 'home.title'),
                   (tr(message.from_user.language_code, 'home.service_unavailable'),)),
            [], state)


@router.message(Command('id'))
async def id_cmd(message: Message, bot: Bot, backend: BackendClient,
                     state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    locale = await prepare_command(message, bot, state)
    username = '@' + message.from_user.username if message.from_user.username else ''
    lines = (tr(locale, 'command.whoami.id', value=message.from_user.id),
             tr(locale, 'command.whoami.username', value=username))
    await render(bot, message.chat.id, Screen(tr(locale, 'command.whoami.title'), lines),
        [[button(message.from_user.id, tr(locale, 'back'), 'home')]], state)


@router.message(Command('version'))
async def version_cmd(message: Message, bot: Bot, backend: BackendClient,
                      state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    locale = await prepare_command(message, bot, state)
    try:
        await backend.resolve(message.from_user.id, username=message.from_user.username,
            first_name=message.from_user.first_name, last_name=message.from_user.last_name,
            language_code=message.from_user.language_code)
        version = (await backend.system_version(message.from_user.id))['version']
        screen = Screen(tr(locale, 'command.version.title'),
                        (tr(locale, 'command.version.value', value=version),))
    except BackendError:
        screen = Screen(tr(locale, 'command.error_title'),
                        (tr(locale, 'home.service_unavailable'),))
    await render(bot, message.chat.id, screen,
        [[button(message.from_user.id, tr(locale, 'back'), 'home')]], state)


async def prepare_command(message: Message, bot: Bot, state: FSMContext) -> str:
    await state.set_state(None)
    await clear_artifacts(bot, message.chat.id, state)
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    return normalize_locale((await state.get_data()).get('locale') or
                            message.from_user.language_code)


@router.message(Command('help'))
async def help_cmd(message: Message, bot: Bot, backend: BackendClient,
                   state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    locale = await prepare_command(message, bot, state)
    await render(bot, message.chat.id, Screen(tr(locale, 'command.help.title'),
        (tr(locale, 'command.help.body'),)),
        [[button(message.from_user.id, tr(locale, 'back'), 'home')]], state)


@router.message(Command('status'))
async def status_cmd(message: Message, bot: Bot, backend: BackendClient,
                     state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    locale = await prepare_command(message, bot, state)
    await state.update_data(locale=locale)
    try:
        await show_admin_status(message.chat.id, message.from_user.id, None,
                                bot, backend, state)
    except BackendError as exc:
        key = 'command.status.denied' if exc.status in {401,403} else 'home.service_unavailable'
        await render(bot, message.chat.id, Screen(tr(locale, 'command.error_title'),
            (tr(locale, key),)),
            [[button(message.from_user.id, tr(locale, 'back'), 'home')]], state)


@router.callback_query(HomeCallback.filter())
async def home_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                  state: FSMContext) -> None:
    await query.answer()
    if query.message:
        old_data = await state.get_data()
        locale = old_data.get('locale') or query.from_user.language_code
        await clear_artifacts(bot, query.message.chat.id, state)
        await state.clear()
        await state.update_data(locale=normalize_locale(locale))
        await show_home(query.message.chat.id, query.from_user.id, bot, backend,
                        state, query.message.message_id)


async def show_home(chat_id: int, user_id: int, bot: Bot, backend: BackendClient,
                    state: FSMContext, message_id: int | None = None) -> None:
    locale = normalize_locale((await state.get_data()).get('locale'))
    account = await backend.me(user_id)
    bot_title = (await backend.bot_title(user_id))['title']
    rows: list[list[InlineKeyboardButton]] = []
    if account['status'] != 'approved':
        policy = await backend.access_request_policy(user_id)
        requests = await backend.request('GET', '/api/v1/me/access-requests?limit=25',
                                         telegram_user_id=user_id)
        pending = any(item['status'] == 'pending' for item in requests['items'])
        if policy['enabled'] and not pending:
            rows.append([button(user_id, tr(locale, 'home.request_access'), 'request_access')])
        if pending:
            lines = (tr(locale, 'home.waiting'),)
        elif policy['enabled']:
            lines = (tr(locale, 'home.request_prompt'),)
        else:
            lines = (policy['gate_message'],)
    else:
        rows.append([button(user_id, tr(locale, 'home.get_config'), 'profiles')])
        rows.append([button(user_id, tr(locale, 'home.account'), 'account_info')])
        lines = (tr(locale, 'home.choose'),)
    rows.append([button(user_id, tr(locale, 'home.settings'), 'member_settings')])
    if account['role'] == 'admin' and account['status'] == 'approved':
        rows.append([button(user_id, tr(locale, 'home.admin'), 'admin_menu')])
    await render(bot, chat_id, Screen(bot_title, lines), rows, state, message_id)


async def show_language_picker(chat_id: int, user_id: int, bot: Bot,
                               backend: BackendClient,
                               state: FSMContext, message_id: int | None = None) -> None:
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    rows = [[button(user_id, tr(locale, 'settings.russian'), 'first_locale', 'ru'),
             button(user_id, tr(locale, 'settings.english'), 'first_locale', 'en')]]
    title = (await backend.bot_title(user_id))['title']
    await render(bot, chat_id, Screen(title,
        (tr(locale, 'language.prompt'),)), rows, state, message_id)


async def show_profiles(chat_id: int, user_id: int, message_id: int, bot: Bot,
                        backend: BackendClient, state: FSMContext) -> None:
    locale = normalize_locale((await state.get_data()).get('locale'))
    page = await backend.profiles(user_id)
    if len(page['items']) == 1:
        await show_profile(chat_id, user_id, message_id, page['items'][0]['id'],
                           bot, backend, state)
        return
    rows = [[button(user_id, item['display_name'], 'profile', item['id'])]
            for item in page['items']]
    rows.append([button(user_id, tr(locale, 'back'), 'home')])
    await render(bot, chat_id, Screen(tr(locale, 'profiles.title'),
        (tr(locale, 'profiles.choose'),) if page['items'] else (tr(locale, 'profiles.empty'),)),
        rows, state, message_id)


async def show_account_info(chat_id: int, user_id: int, message_id: int,
                            bot: Bot, backend: BackendClient, state: FSMContext,
                            username: str | None = None) -> None:
    locale = normalize_locale((await state.get_data()).get('locale'))
    account = await backend.me(user_id)
    page = await backend.profiles(user_id)
    if len(page['items']) == 1:
        await show_account_profile(chat_id, user_id, message_id, page['items'][0]['id'],
                                   bot, backend, state, username=username)
        return
    rows = [[button(user_id, item['display_name'], 'account_profile',
                    item['id'], 'account_info')] for item in page['items']]
    rows.append([button(user_id, tr(locale, 'back'), 'home')])
    lines = (tr(locale, 'account.id', id=account['id']),
             tr(locale, 'account.status', status=tr(locale, f"status.{account['status']}")),
             tr(locale, 'account.profiles_choose' if page['items'] else 'account.profiles_empty'))
    await render(bot, chat_id, Screen(tr(locale, 'account.title'), lines),
                 rows, state, message_id)


async def show_account_profile(chat_id: int, user_id: int, message_id: int,
                               profile_id: str, bot: Bot, backend: BackendClient,
                               state: FSMContext, *, username: str | None = None,
                               back_to: str = 'home') -> None:
    locale = normalize_locale((await state.get_data()).get('locale'))
    summary = await backend.member_profile_summary(user_id, profile_id)
    status_key = ('profile.frozen' if summary['frozen'] else
                  'profile.expired' if summary['expired'] else 'profile.active')
    status = tr(locale, status_key)
    lines = [tr(locale, 'account.profile_name', name=summary['display_name']),
             tr(locale, 'account.status', status=status),
             tr(locale, 'account.telegram_id', id=user_id),
             tr(locale, 'account.username', value='@' + username if username else '—')]
    if summary.get('expires_at'):
        lines.append(tr(locale, 'account.profile_expires',
            value=summary['expires_at'][:10]))
    if summary['nodes']:
        lines.append(tr(locale, 'account.access_title'))
        for node in summary['nodes']:
            protocols = ', '.join(tr(locale, 'protocol.' + kind)
                                  for kind in node['protocols'])
            lines.append(tr(locale, 'account.access_node',
                node=f"{node['flag']} {node['title']}".strip(), protocols=protocols))
    else:
        lines.append(tr(locale, 'account.access_empty'))
    rows = [[button(user_id, tr(locale, 'account.statistics'), 'account_stats',
                    profile_id, back_to)],
            [button(user_id, tr(locale, 'back'), back_to)]]
    await render(bot, chat_id, Screen(tr(locale, 'account.profile_title'), tuple(lines)),
                 rows, state, message_id)


async def show_account_stats(chat_id: int, user_id: int, message_id: int,
                             profile_id: str, bot: Bot, backend: BackendClient,
                             state: FSMContext, *, back_to: str = 'home') -> None:
    locale = normalize_locale((await state.get_data()).get('locale'))
    summary = await backend.member_profile_summary(user_id, profile_id)
    lines = (tr(locale, 'account.profile_name', name=summary['display_name']),
             tr(locale, 'account.member_since',
                value=(summary.get('created_at') or '')[:10] or '—'),
             tr(locale, 'account.stats_nodes', count=summary['node_count']),
             tr(locale, 'account.stats_protocols', count=summary['protocol_count']),
             tr(locale, 'account.stats_xray', count=summary['xray_count']),
             tr(locale, 'account.stats_awg', count=summary['awg_count']),
             tr(locale, 'account.stats_issued', count=summary['issued_count']),
             tr(locale, 'account.stats_last',
                value=(summary.get('last_issued_at') or '')[:16].replace('T', ' ') or '—'))
    traffic = summary.get('traffic')
    if traffic:
        lines += (tr(locale, 'traffic.status.' + traffic['status']),)
        for item in traffic['items']:
            lines += (tr(locale, 'traffic.totals',
                         protocol='AmneziaWG' if item['protocol'] == 'awg' else 'Xray',
                         upload=_traffic_bytes(item['uplink_bytes']),
                         download=_traffic_bytes(item['downlink_bytes'])),
                      tr(locale, 'traffic.sample',
                         since=item['tracked_since'][:16].replace('T', ' '),
                         at=item['last_sample_at'][:16].replace('T', ' ')))
    await render(bot, chat_id, Screen(tr(locale, 'account.stats_title'), lines),
        [[button(user_id, tr(locale, 'back'), 'account_profile', profile_id, back_to)]],
        state, message_id)


def _traffic_bytes(value: int) -> str:
    amount = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB'):
        if amount < 1024 or unit == 'EiB':
            return f'{amount:.1f} {unit}'
        amount /= 1024
    raise ValueError('invalid traffic size')


async def show_profile(chat_id: int, user_id: int, message_id: int,
                       profile_id: str, bot: Bot, backend: BackendClient,
                       state: FSMContext) -> None:
    locale = normalize_locale((await state.get_data()).get('locale'))
    profile = await backend.member_profile_summary(user_id, profile_id)
    page = await backend.profile_nodes(user_id, profile_id)
    rows = [[button(user_id, f"{node['flag']} {node['title']}".strip(),
                    'node', profile_id, node['key'])] for node in page['items']]
    rows.append([button(user_id, tr(locale, 'back'), 'profiles')])
    status_key = ('profile.frozen' if profile['frozen'] else
                  'profile.expired' if profile['expired'] else 'profile.active')
    status = tr(locale, status_key)
    await render(bot, chat_id, Screen(profile['display_name'],
        (tr(locale, 'account.status', status=status), tr(locale, 'nodes.choose')) if page['items'] else
        (tr(locale, 'account.status', status=status), tr(locale, 'nodes.empty'))),
        rows, state, message_id)


async def show_node(chat_id: int, user_id: int, message_id: int,
                    profile_id: str, node_key: str, bot: Bot,
                    backend: BackendClient, state: FSMContext) -> None:
    page = await backend.profile_nodes(user_id, profile_id)
    node = next((item for item in page['items'] if item['key'] == node_key), None)
    if node is None:
        await show_profile(chat_id, user_id, message_id, profile_id, bot, backend, state)
        return
    locale = normalize_locale((await state.get_data()).get('locale'))
    rows = [[button(user_id, tr(locale, f"protocol.{protocol['kind']}"),
                    'protocol', profile_id, node_key, protocol['kind'])]
            for protocol in node['protocols']]
    rows.append([button(user_id, tr(locale, 'back'), 'profile', profile_id)])
    await render(bot, chat_id, Screen(node['title'],
        (node['region'], tr(locale, 'node.choose_protocol'))),
        rows, state, message_id)


async def show_protocol(chat_id: int, user_id: int, message_id: int, profile_id: str,
                       node_key: str, protocol: str, bot: Bot, backend: BackendClient,
                       state: FSMContext) -> None:
    locale = normalize_locale((await state.get_data()).get('locale'))
    page = await backend.profile_nodes(user_id, profile_id)
    node = next((item for item in page['items'] if item['key'] == node_key), None)
    selected = next((item for item in node['protocols'] if item['kind'] == protocol), None) if node else None
    if selected is None:
        await show_node(chat_id, user_id, message_id, profile_id, node_key, bot, backend, state)
        return
    if protocol == 'awg':
        rows = [
            [button(user_id, tr(locale, 'awg.get_vpn'), 'issue', profile_id, node_key, 'awg', 'vpn')],
            [button(user_id, tr(locale, 'awg.get_conf'), 'issue', profile_id, node_key, 'awg', 'conf')],
        ]
        title = tr(locale, 'awg.title', node=node['title'])
    else:
        rows = [[button(user_id, tr(locale, f'transport.{transport}'), 'issue',
                        profile_id, node_key, 'xray', transport)]
                for transport in selected['transports']]
        title = tr(locale, 'xray.title', node=node['title'])
    rows.append([button(user_id, tr(locale, 'back'), 'node', profile_id, node_key)])
    prompt = tr(locale, 'awg.choose_format' if protocol == 'awg' else 'transport.choose')
    await render(bot, chat_id, Screen(title, (prompt,)), rows, state, message_id)


async def show_issuance(chat_id: int, user_id: int, message_id: int,
                        issuance_id: str, bot: Bot, backend: BackendClient,
                        state: FSMContext) -> None:
    view_token = secrets.token_urlsafe(16)
    await state.update_data(issuance_poll_token=view_token)
    try:
        await _render_issuance(chat_id, user_id, message_id, issuance_id,
                               bot, backend, state, view_token)
    except BackendError:
        if (await state.get_data()).get('issuance_poll_token') == view_token:
            raise


async def _render_issuance(chat_id: int, user_id: int, message_id: int,
                           issuance_id: str, bot: Bot, backend: BackendClient,
                           state: FSMContext, view_token: str) -> None:
    result = await backend.issuance(user_id, issuance_id)
    if (await state.get_data()).get('issuance_poll_token') != view_token:
        return
    profile_id, node_key = result['profile_id'], result['node_key']
    locale = normalize_locale((await state.get_data()).get('locale'))
    rows = [[button(user_id, tr(locale, 'back'), 'protocol', profile_id, node_key,
                    result['protocol'])]]
    if result['status'] == 'succeeded':
        artifact = await backend.artifact(user_id, issuance_id)
        if (await state.get_data()).get('issuance_poll_token') != view_token:
            return
        content = artifact['content']
        filename = artifact['filename'] or f"{result['protocol']}-{node_key}.txt"
        data = await state.get_data()
        delivered = set(data.get('delivered_issuances', []))
        if issuance_id not in delivered:
            sent = await bot.send_document(chat_id, BufferedInputFile(content.encode(), filename))
            if (await state.get_data()).get('issuance_poll_token') != view_token:
                try:
                    await bot.delete_message(chat_id, sent.message_id)
                except TelegramAPIError:
                    pass
                return
            await track_artifact(state, sent.message_id)
            delivered.add(issuance_id)
            await state.update_data(delivered_issuances=list(delivered))
        if (result['protocol'] == 'xray' or result.get('transport') == 'vpn') and len(content.encode()) <= 2500:
            rows.insert(0, [button(user_id, tr(locale, 'config.qr'), 'qr', issuance_id)])
        if result['protocol'] == 'xray':
            details_title = tr(locale, 'config.vless_link')
            details = (content,)
            import_hint = tr(locale, 'config.import_xray')
        elif result['transport'] == 'vpn':
            details_title = tr(locale, 'config.awg_uri')
            details = (content,)
            import_hint = tr(locale, 'config.import_awg_vpn')
        else:
            details_title = None
            details = ()
            import_hint = tr(locale, 'config.import_awg_conf')
        await render(bot, chat_id, Screen(tr(locale, 'config.ready'),
            (import_hint,), details_title, details), rows, state, message_id)
    elif result['status'] in {'blocked', 'superseded', 'failed'}:
        await render(bot, chat_id, Screen(tr(locale, 'config.not_ready'),
            (tr(locale, 'config.unavailable'),)),
            rows, state, message_id)
    else:
        rows.insert(0, [button(user_id, tr(locale, 'config.refresh'), 'issuance', issuance_id)])
        await render(bot, chat_id, Screen(tr(locale, 'config.preparing_title'),
            (tr(locale, 'config.pending'),)), rows, state, message_id)


async def issue(chat_id: int, user_id: int, message_id: int, profile_id: str,
                node_key: str, protocol: str, transport: str, bot: Bot,
                backend: BackendClient, state: FSMContext) -> None:
    locale = normalize_locale((await state.get_data()).get('locale'))
    poll_token = secrets.token_urlsafe(16)
    await state.update_data(issuance_poll_token=poll_token)

    async def owns_screen():
        return (await state.get_data()).get('issuance_poll_token') == poll_token

    try:
        queued = await backend.issue(user_id, profile_id, node_key, protocol, transport)
        if not await owns_screen():
            return
        await render(bot, chat_id, Screen(tr(locale, 'config.preparing_title'),
            (tr(locale, 'config.preparing'),)),
            [[button(user_id, tr(locale, 'back'), 'protocol', profile_id, node_key, protocol)]], state, message_id)
        for _ in range(15):
            if not await owns_screen():
                return
            result = await backend.issuance(user_id, queued['id'])
            if not await owns_screen():
                return
            if result['status'] in {'succeeded', 'blocked', 'superseded', 'failed'}:
                break
            await asyncio.sleep(1)
        if await owns_screen():
            await show_issuance(chat_id, user_id, message_id, queued['id'], bot, backend, state)
    except BackendError:
        if await owns_screen():
            raise


async def show_qr(chat_id: int, user_id: int, message_id: int, issuance_id: str,
                  bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    view_token = secrets.token_urlsafe(16)
    await state.update_data(issuance_poll_token=view_token)
    result = await backend.issuance(user_id, issuance_id)
    if (await state.get_data()).get('issuance_poll_token') != view_token:
        return
    artifact = await backend.artifact(user_id, issuance_id)
    if (await state.get_data()).get('issuance_poll_token') != view_token:
        return
    content = qr_payload(result['protocol'], result['transport'], artifact['content'])
    locale = normalize_locale((await state.get_data()).get('locale'))
    rows = [[button(user_id, tr(locale, 'back'), 'qr_back', issuance_id)]]
    if len(content.encode()) > 2500:
        await render(bot, chat_id, Screen(tr(locale, 'qr.unavailable'),
            (tr(locale, 'qr.too_long'),)), rows, state, message_id)
        return
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L,
                         box_size=6, border=4)
    code.add_data(content)
    try:
        code.make(fit=True)
    except DataOverflowError:
        await render(bot, chat_id, Screen(tr(locale, 'qr.unavailable'),
            (tr(locale, 'qr.too_long'),)), rows, state, message_id)
        return
    image = BytesIO()
    code.make_image(fill_color='black', back_color='white').save(image, format='PNG')
    sent = await bot.send_photo(chat_id, BufferedInputFile(image.getvalue(), 'config.png'))
    if (await state.get_data()).get('issuance_poll_token') != view_token:
        try:
            await bot.delete_message(chat_id, sent.message_id)
        except TelegramAPIError:
            pass
        return
    await track_artifact(state, sent.message_id)
    await render(bot, chat_id, Screen(tr(locale, 'qr.ready'), (tr(locale, 'qr.scan'),)),
                 rows, state, message_id)


def qr_payload(protocol: str, transport: str, content: str) -> str:
    return content.removeprefix('vpn://') if protocol == 'awg' and transport == 'vpn' else content


@router.callback_query(F.data.startswith('u:'))
async def user_action_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                         state: FSMContext) -> None:
    if query.message is None or query.from_user is None or query.message.chat.type != 'private':
        return
    token = (query.data or '')[2:]
    action = actions.get(token)
    if action is None or action.owner_id != query.from_user.id or action.expires_at < time.monotonic():
        locale = normalize_locale((await state.get_data()).get('locale') or query.from_user.language_code)
        await query.answer(tr(locale, 'callback.stale'), show_alert=True)
        return
    actions.pop(token, None)
    await state.update_data(issuance_poll_token=None)
    await query.answer()
    chat_id, user_id, message_id = query.message.chat.id, query.from_user.id, query.message.message_id
    try:
        if action.name != 'issuance':
            await clear_artifacts(bot, chat_id, state)
        if action.name == 'home':
            await show_home(chat_id, user_id, bot, backend, state, message_id)
        elif action.name == 'first_locale':
            await backend.set_locale(user_id, action.args[0])
            await state.update_data(locale=action.args[0])
            await show_home(chat_id, user_id, bot, backend, state, message_id)
        elif action.name == 'profiles':
            await show_profiles(chat_id, user_id, message_id, bot, backend, state)
        elif action.name == 'profile':
            await show_profile(chat_id, user_id, message_id, action.args[0], bot, backend, state)
        elif action.name == 'node':
            await show_node(chat_id, user_id, message_id, *action.args, bot, backend, state)
        elif action.name == 'protocol':
            await show_protocol(chat_id, user_id, message_id, *action.args, bot, backend, state)
        elif action.name == 'issue':
            await issue(chat_id, user_id, message_id, *action.args, bot, backend, state)
        elif action.name == 'issuance':
            await show_issuance(chat_id, user_id, message_id, action.args[0], bot, backend, state)
        elif action.name == 'qr':
            await show_qr(chat_id, user_id, message_id, action.args[0], bot, backend, state)
        elif action.name == 'qr_back':
            await show_issuance(chat_id, user_id, message_id, action.args[0], bot,
                                backend, state)
        elif action.name == 'account_info':
            await show_account_info(chat_id, user_id, message_id, bot, backend, state,
                                    username=query.from_user.username)
        elif action.name == 'account_profile':
            await show_account_profile(chat_id, user_id, message_id, action.args[0],
                bot, backend, state, username=query.from_user.username,
                back_to=action.args[1])
        elif action.name == 'account_stats':
            await show_account_stats(chat_id, user_id, message_id, action.args[0],
                bot, backend, state, back_to=action.args[1])
        elif action.name == 'member_settings':
            await show_member_settings(chat_id, user_id, message_id, bot, backend, state)
        elif action.name == 'traffic_consent':
            await backend.set_traffic_consent(user_id, action.args[0] == 'true')
            await show_member_settings(chat_id, user_id, message_id, bot, backend, state)
        elif action.name == 'announcement_silent':
            await backend.set_announcement_silent(user_id, action.args[0] == 'true')
            await show_member_settings(chat_id, user_id, message_id, bot, backend, state)
        elif action.name == 'set_locale':
            await backend.set_locale(user_id, action.args[0])
            await state.update_data(locale=action.args[0])
            await show_member_settings(chat_id, user_id, message_id, bot, backend, state,
                                       saved=True)
        elif action.name == 'request_access':
            request = await backend.request_access(user_id)
            await show_home(chat_id, user_id, bot, backend, state, message_id)
            from .admin_requests import notify_admins
            await notify_admins(bot, backend, request['id'])
        elif action.name == 'admin_menu':
            await show_admin_menu(chat_id, user_id, message_id, bot, backend, state)
    except BackendError as exc:
        locale = normalize_locale((await state.get_data()).get('locale'))
        if exc.code == 'access_requests_disabled':
            await show_home(chat_id, user_id, bot, backend, state, message_id)
            return
        if exc.code in {'profile_frozen', 'profile_expired', 'grant_revoked',
                        'account_disabled', 'permission_denied'}:
            cause = 'access'
        elif exc.status == 404:
            cause = 'missing'
        elif exc.status >= 500:
            cause = 'service'
        else:
            cause = 'retry'
        await render(bot, chat_id, Screen(tr(locale, 'action.unavailable'),
            (tr(locale, 'action.error.' + cause),)),
            [[button(user_id, tr(locale, 'home.title'), 'home')]],
            state, message_id)


async def show_admin_menu(chat_id: int, user_id: int, message_id: int,
                          bot: Bot, backend: BackendClient,
                          state: FSMContext) -> None:
    from .callbacks import (AdminNodesCallback,
                            AdminProfilesCallback, AdminSettingsCallback, RequestsCallback)
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        title = (await backend.bot_title(user_id))['title']
    except BackendError:
        title = tr(locale, 'admin.menu')
    rows = [
        [InlineKeyboardButton(text=tr(locale, 'admin.status'), callback_data='admin_status'),
         InlineKeyboardButton(text=tr(locale, 'admin.requests'), callback_data=RequestsCallback().pack())],
        [InlineKeyboardButton(text=tr(locale, 'admin.nodes'), callback_data=AdminNodesCallback().pack()),
         InlineKeyboardButton(text=tr(locale, 'admin.profiles'), callback_data=AdminProfilesCallback().pack())],
        [InlineKeyboardButton(text=tr(locale, 'announce.title'), callback_data='announce_menu')],
        [InlineKeyboardButton(text=tr(locale, 'admin.settings'), callback_data=AdminSettingsCallback().pack())],
        [button(user_id, tr(locale, 'back'), 'home')],
    ]
    await render(bot, chat_id, Screen(title, (tr(locale, 'admin.description'),)),
                 rows, state, message_id)


@router.callback_query(F.data == 'admin_status')
async def admin_status_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                          state: FSMContext) -> None:
    await query.answer()
    await show_admin_status(query.message.chat.id, query.from_user.id,
        query.message.message_id, bot, backend, state)


async def show_admin_status(chat_id: int, user_id: int, message_id: int,
                            bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    from .callbacks import AdminNodesCallback, AdminProfilesCallback, RequestsCallback
    locale = normalize_locale((await state.get_data()).get('locale'))
    overview = await backend.admin_overview(user_id)
    lines = [tr(locale, 'admin.status.version', version=overview['version']),
             tr(locale, 'admin.status.nodes', active=overview['nodes_enabled'],
                total=overview['nodes_total']),
             tr(locale, 'admin.status.profiles', active=overview['profiles_active'],
                total=overview['profiles_total']),
             tr(locale, 'admin.status.frozen', count=overview['profiles_frozen']),
             tr(locale, 'admin.status.pending', count=overview['pending_requests']),
             tr(locale, 'admin.status.problems', count=len(overview['problem_nodes'])),
             tr(locale, 'admin.status.runtime_note')]
    rows = []
    if overview['pending_requests']:
        rows.append([InlineKeyboardButton(text=tr(locale, 'admin.requests'),
            callback_data=RequestsCallback().pack())])
    if overview['problem_nodes']:
        rows.append([InlineKeyboardButton(text=tr(locale, 'admin.status.open_problems'),
            callback_data='admin_problem_nodes')])
    rows.extend([[InlineKeyboardButton(text=tr(locale, 'admin.nodes'),
        callback_data=AdminNodesCallback().pack()),
        InlineKeyboardButton(text=tr(locale, 'admin.profiles'),
        callback_data=AdminProfilesCallback().pack())],
        [InlineKeyboardButton(text=tr(locale, 'admin.status.refresh'),
        callback_data='admin_status')],
        [InlineKeyboardButton(text=tr(locale, 'back'), callback_data='admin_menu')]])
    await render(bot, chat_id, Screen(tr(locale, 'admin.status.title'), tuple(lines)),
                 rows, state, message_id)


@router.callback_query(F.data == 'admin_problem_nodes')
async def admin_problem_nodes_cb(query: CallbackQuery, bot: Bot,
                                 backend: BackendClient, state: FSMContext) -> None:
    from .callbacks import AdminNodeCallback
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    overview = await backend.admin_overview(query.from_user.id)
    rows = [[InlineKeyboardButton(text=node['title'],
        callback_data=AdminNodeCallback(node_key=node['key']).pack())]
        for node in overview['problem_nodes']]
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data='admin_status')])
    lines = (tr(locale, 'admin.status.problem_nodes_hint'),) if rows[:-1] else (
        tr(locale, 'admin.status.no_problems'),)
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'admin.status.problem_nodes'), lines),
        rows, state, query.message.message_id)


async def show_member_settings(chat_id: int, user_id: int, message_id: int,
                               bot: Bot, backend: BackendClient, state: FSMContext,
                               saved: bool = False) -> None:
    locale = normalize_locale((await state.get_data()).get('locale'))
    current = await backend.me(user_id)
    silent = current.get('announcement_silent', False)
    rows = [
        [button(user_id, tr(locale, 'settings.russian'), 'set_locale', 'ru'),
         button(user_id, tr(locale, 'settings.english'), 'set_locale', 'en')],
        [button(user_id, tr(locale, 'announce.sound_off' if silent else 'announce.sound_on'),
            'announcement_silent', 'false' if silent else 'true')],
        [button(user_id, tr(locale, 'back'), 'home')],
    ]
    lines = [tr(locale, 'settings.locale')]
    if current.get('traffic_available') or current.get('traffic_consent'):
        consent = current.get('traffic_consent', False)
        rows.insert(-1, [button(user_id, tr(locale, 'traffic.consent_on' if consent else 'traffic.consent_off'),
            'traffic_consent', 'false' if consent else 'true')])
        lines.append(tr(locale, 'traffic.consent_description'))
    if saved:
        lines.insert(0, tr(locale, 'settings.locale_saved'))
    await render(bot, chat_id, Screen(tr(locale, 'settings.title'), tuple(lines)),
                 rows, state, message_id)


@router.callback_query(F.data == 'admin_menu')
async def admin_menu_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                        state: FSMContext) -> None:
    await query.answer()
    account = await backend.me(query.from_user.id)
    if account['role'] != 'admin' or account['status'] != 'approved':
        return
    await state.set_state(None)
    await show_admin_menu(query.message.chat.id, query.from_user.id,
                          query.message.message_id, bot, backend, state)
