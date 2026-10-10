"""Member screens. Every data read and mutation goes through the backend API."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
import secrets
import time
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, LinkPreviewOptions, Message, MessageEntity)
import qrcode
from qrcode.exceptions import DataOverflowError

from ..backend import BackendClient, BackendError
from ..screens import Screen, Section, Table, server_label
from ..i18n import normalize_locale, tr
from .callbacks import HomeCallback
from .common import render
from ..navigation import remember_node, remember_label, parent_path

router = Router()


@dataclass(frozen=True)
class Action:
    owner_id: int
    name: str
    args: tuple[str, ...]


actions: OrderedDict[str, Action] = OrderedDict()
MAX_ACTIONS = 10000
ONBOARDING_TIMEOUT = 12
ADMIN_MENU_TIMEOUT = 3
SERVERS_PER_PAGE = 10


def server_page(nodes, page):
    ordered = sorted(nodes, key=lambda node: (
        node.get('region', '').casefold(), node['title'].casefold(), node['key']))
    pages = max(1, (len(ordered) + SERVERS_PER_PAGE - 1) // SERVERS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    return ordered[page * SERVERS_PER_PAGE:(page + 1) * SERVERS_PER_PAGE], page, pages


def region_sections(nodes, locale, make_section):
    groups = {}
    for node in nodes:
        region = node.get('region') or tr(locale, 'nodes.region_unknown')
        groups.setdefault(region, []).append(make_section(node))
    return tuple(Section(region, sections=tuple(sections)) for region, sections in groups.items())


def server_pagination(user_id, locale, page, pages, action, *args):
    if pages <= 1:
        return ()
    row = []
    if page > 0:
        row.append(button(user_id, tr(locale, 'pagination.previous'), action, *args, str(page - 1)))
    row.append(button(user_id, tr(locale, 'pagination.page', page=page + 1, pages=pages), 'page_number'))
    if page + 1 < pages:
        row.append(button(user_id, tr(locale, 'pagination.next'), action, *args, str(page + 1)))
    return (tuple(row),)


def button(owner_id: int, label: str, name: str, *args: str) -> InlineKeyboardButton:
    token = secrets.token_urlsafe(10)
    actions[token] = Action(owner_id, name, args)
    while len(actions) > MAX_ACTIONS:
        actions.popitem(last=False)
    return InlineKeyboardButton(text=label, callback_data='u:' + token)


async def clear_artifacts(bot: Bot, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    for message_id in data.get('artifact_message_ids', []):
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramAPIError:
            pass
    await state.update_data(artifact_message_ids=[], delivered_issuances=[],
                            issuance_poll_token=None, plain_uri_message_id=None)


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
    await state.update_data(device_delete_confirmation=None)
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    try:
        await clear_artifacts(bot, message.chat.id, state)
        # Clear history may hide messages locally while Telegram still accepts
        # edits to them. /start must send a visible, fresh control panel.
        await state.update_data(control_message_id=None, navigation_screen=None)
        await backend.resolve(message.from_user.id, username=message.from_user.username,
            first_name=message.from_user.first_name, last_name=message.from_user.last_name,
            language_code=message.from_user.language_code)
        account = await backend.me(message.from_user.id)
        await state.update_data(locale=normalize_locale(account.get('locale') or account.get('language_code')))
        if not account.get('locale_selected'):
            await show_language_picker(message.chat.id, message.from_user.id, bot, backend, state)
        else:
            await show_home(message.chat.id, message.from_user.id, bot, backend, state,
                            quick_start=True)
    except BackendError:
        await render(bot, message.chat.id,
            Screen(tr(message.from_user.language_code, 'home.title'),
                   (tr(message.from_user.language_code, 'home.service_unavailable'),), embedded_buttons=True, navigation=True),
            [], state)


@router.message(Command('id'))
async def id_cmd(message: Message, bot: Bot, backend: BackendClient,
                     state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    locale = await prepare_command(message, bot, state)
    username = '@' + message.from_user.username if message.from_user.username else ''
    await render(bot, message.chat.id, Screen(tr(locale, 'command.whoami.title'),
        sections=(Section('', tables=(Table(
            (tr(locale, 'maintenance.rich.field'), tr(locale, 'maintenance.rich.value')),
            ((tr(locale, 'command.rich.telegram_id'), str(message.from_user.id)),
             (tr(locale, 'command.rich.username'), username or '—'))),)),),
        embedded_buttons=True, navigation=True),
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
                        (tr(locale, 'command.version.value', value=version),), embedded_buttons=True, navigation=True)
    except BackendError:
        screen = Screen(tr(locale, 'command.error_title'),
                        (tr(locale, 'home.service_unavailable'),), embedded_buttons=True, navigation=True)
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
    from ..commands import COMMANDS
    await render(bot, message.chat.id, Screen(tr(locale, 'command.help.title'),
        sections=(Section('', tables=(Table(
            (tr(locale, 'command.rich.command'), tr(locale, 'command.rich.description')),
            tuple(('/' + command, tr(locale, 'command.menu.' + command)) for command in COMMANDS)),)),),
        embedded_buttons=True, navigation=True),
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
            (tr(locale, key),), embedded_buttons=True, navigation=True),
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
        await state.update_data(locale=normalize_locale(locale),
                                home_presentation=old_data.get('home_presentation'))
        await show_home(query.message.chat.id, query.from_user.id, bot, backend,
                        state, query.message.message_id, cached_navigation=True)


async def show_home(chat_id: int, user_id: int, bot: Bot, backend: BackendClient,
                    state: FSMContext, message_id: int | None = None, *,
                    account: dict | None = None, cached_navigation: bool = False,
                    quick_start: bool = False) -> None:
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    presentation = data.get('home_presentation')
    # Only reuse menu presentation for navigation, never as authorization.
    # Every destination/action still makes its own authenticated backend calls.
    if (cached_navigation and account is None and presentation and
            presentation.get('user_id') == user_id and
            presentation['account']['status'] == 'approved'):
        account, title = presentation['account'], presentation['title']
    else:
        try:
            async with asyncio.timeout(ONBOARDING_TIMEOUT):
                if account is None:
                    account, title = await asyncio.gather(backend.me(user_id), backend.bot_title(user_id))
                else:
                    title = await backend.bot_title(user_id)
                if account['status'] != 'approved':
                    policy, requests = await asyncio.gather(backend.access_request_policy(user_id),
                        backend.request('GET', '/api/v1/me/access-requests?limit=25', telegram_user_id=user_id))
        except TimeoutError:
            raise BackendError('backend_unavailable', 503) from None
        await state.update_data(home_presentation={'user_id': user_id,
            'account': {'status': account['status'], 'role': account['role']},
            'title': title})
    bot_title = title['title']
    rows: list[list[InlineKeyboardButton]] = []
    if account['status'] != 'approved':
        pending = any(item['status'] == 'pending' for item in requests['items'])
        if policy['enabled'] and not pending:
            rows.append([button(user_id, tr(locale, 'home.request_access'), 'request_access')
                         .model_copy(update={'style': 'primary'})])
        if pending:
            lines = (tr(locale, 'home.waiting'),)
        elif policy['enabled']:
            lines = (tr(locale, 'home.request_prompt'),)
        else:
            lines = (policy['gate_message'],)
    else:
        rows.append([button(user_id, tr(locale, 'home.get_config'), 'profiles').model_copy(update={'style': 'primary'})])
        rows.append([button(user_id, tr(locale, 'home.account'), 'account_info')])
        lines = (tr(locale, 'home.choose'),)
        if quick_start and account['role'] == 'admin':
            try:
                async with asyncio.timeout(3.0):
                    nodes_page = await backend.admin_nodes(user_id, limit=1)
                    if not nodes_page['items']:
                        lines = (tr(locale, 'home.admin_quick_start'),)
            except Exception:
                pass
    if account['status'] == 'approved':
        rows[-1].append(button(user_id, tr(locale, 'home.settings'), 'member_settings'))
    else:
        rows.append([button(user_id, tr(locale, 'home.settings'), 'member_settings')])
    if account['role'] == 'admin' and account['status'] == 'approved':
        rows.append([button(user_id, tr(locale, 'home.admin'), 'admin_menu')])
    if account['status'] == 'approved':
        rows.insert(2, [button(user_id, tr(locale, 'devices.title'), 'device_profiles')])
    if account['status'] != 'approved':
        request_rows = tuple(tuple(row) for row in rows[:-1])
        await render(bot, chat_id, Screen(bot_title,
            sections=(Section(tr(locale, 'home.access_title'), lines, rows=request_rows),),
            embedded_buttons=True, navigation=True), rows[-1:], state, message_id)
    else:
        await render(bot, chat_id, Screen(bot_title, lines, embedded_buttons=True), rows, state, message_id)


async def show_language_picker(chat_id: int, user_id: int, bot: Bot,
                               backend: BackendClient,
                               state: FSMContext, message_id: int | None = None) -> None:
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    rows = [[button(user_id, tr(locale, 'settings.russian'), 'first_locale', 'ru'),
             button(user_id, tr(locale, 'settings.english'), 'first_locale', 'en')]]
    title = (await backend.bot_title(user_id))['title']
    await render(bot, chat_id, Screen(title,
        (tr(locale, 'language.prompt'),), embedded_buttons=True), rows, state, message_id)


async def show_profiles(chat_id: int, user_id: int, message_id: int, bot: Bot,
                        backend: BackendClient, state: FSMContext) -> None:
    locale = normalize_locale((await state.get_data()).get('locale'))
    page = await backend.profiles(user_id)
    await state.update_data(member_profile_back='home' if len(page['items']) == 1 else 'profiles')
    if len(page['items']) == 1:
        await show_profile(chat_id, user_id, message_id, page['items'][0]['id'],
                           bot, backend, state)
        return
    rows = [[button(user_id, item['display_name'], 'profile', item['id'])]
            for item in page['items']]
    rows.append([button(user_id, tr(locale, 'back'), 'home')])
    await render(bot, chat_id, Screen(tr(locale, 'profiles.title'),
        (tr(locale, 'profiles.choose'),) if page['items'] else (tr(locale, 'profiles.empty'),), embedded_buttons=True),
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
    await render(bot, chat_id, Screen(tr(locale, 'account.title'), lines, embedded_buttons=True),
                 rows, state, message_id)


async def show_account_profile(chat_id: int, user_id: int, message_id: int,
                               profile_id: str, bot: Bot, backend: BackendClient,
                               state: FSMContext, *, username: str | None = None,
                               back_to: str = 'home', page_index: int = 0,
                               servers_open: bool = False) -> None:
    locale = normalize_locale((await state.get_data()).get('locale'))
    summary = await backend.member_profile_summary(user_id, profile_id)
    nodes, page_index, pages = server_page(summary['nodes'], page_index)
    status_key = ('profile.frozen' if summary['frozen'] else
                  'profile.expired' if summary['expired'] else 'profile.active')
    status = tr(locale, status_key)
    facts = (tr(locale, 'account.rich.field'), tr(locale, 'account.rich.value'))
    access = Section(tr(locale, 'account.rich.access'), tables=(Table(facts, (
        (tr(locale, 'account.rich.status'), status),
        (tr(locale, 'account.rich.expiry'), summary['expires_at'][:10] + ' UTC' if summary.get('expires_at') else tr(locale, 'profile.layout.unlimited')))),))
    identity = Section(tr(locale, 'account.rich.identity'), tables=(Table(facts, (
        (tr(locale, 'account.rich.username'), '@' + username if username else '—'),
        (tr(locale, 'account.rich.telegram_id'), str(user_id)),
        (tr(locale, 'account.rich.created'), (summary.get('created_at') or '')[:10] or '—'))),))
    statistics = Section(tr(locale, 'account.statistics'),
        tables=(Table(facts, (
        (tr(locale, 'admin.nodes'), str(summary.get('node_count', len(summary['nodes'])))),
        (tr(locale, 'account.rich.connections'), str(summary.get('protocol_count', 0))),
        (tr(locale, 'protocol.xray'), str(summary.get('xray_count', 0))),
        (tr(locale, 'protocol.awg'), str(summary.get('awg_count', 0))),
        (tr(locale, 'account.rich.issued'), str(summary.get('issued_count', 0))),
        (tr(locale, 'account.rich.last'), (summary.get('last_issued_at') or '')[:16].replace('T', ' ') or '—'))),))
    traffic = profile_traffic_section(summary, locale)
    servers = Section(tr(locale, 'account.access_title'),
        () if summary['nodes'] else (tr(locale, 'account.access_empty'),),
        collapsed=True, is_open=servers_open,
        sections=region_sections(nodes, locale, lambda node: profile_server_section(summary, node, locale,
            divider_after=node['key'] != nodes[-1]['key'])),
        rows=server_pagination(user_id, locale, page_index, pages,
            'account_nodes_page', profile_id, back_to))
    sections = (access, identity, statistics, *((traffic,) if traffic else ()), servers)
    await render(bot, chat_id, Screen(tr(locale, 'account.profile_title'),
        (summary['display_name'],), sections=sections, embedded_buttons=True, navigation=True),
        [[button(user_id, tr(locale, 'back'), back_to)]], state, message_id)


def profile_traffic_section(summary, locale):
    traffic = summary.get('traffic')
    if not traffic:
        return None
    lines = [tr(locale, 'traffic.status.' + traffic['status'])]
    tables = ()
    if traffic['status'] in {'current', 'unknown'} and traffic.get('items'):
        items = traffic['items']
        total = sum(item['uplink_bytes'] + item['downlink_bytes'] for item in items)
        lines.append(tr(locale, 'traffic.month_total', month=traffic.get('month') or '—', total=_traffic_bytes(total)))
        tables = (Table((tr(locale, 'profile.rich.protocols'), tr(locale, 'account.rich.traffic')),
            tuple((tr(locale, 'protocol.' + item['protocol']), _traffic_bytes(item['uplink_bytes'] + item['downlink_bytes'])) for item in items)),)
    return Section(tr(locale, 'traffic.title'), tuple(lines), tables=tables)


def profile_server_section(summary, node, locale, *, divider_after=False):
    traffic = summary.get('traffic')
    if not traffic:
        return Section(server_label(node), (' · '.join(tr(locale, 'protocol.' + protocol) for protocol in node['protocols']),),
            divider_after=divider_after, heading_size=3)
    rows, stale = [], False
    for protocol in node['protocols']:
        item = next((item for item in traffic.get('nodes', [])
            if item['node_key'] == node['key'] and item['protocol'] == protocol), None)
        rows.append((tr(locale, 'protocol.' + protocol),
            _traffic_bytes(item['uplink_bytes'] + item['downlink_bytes']) if item else tr(locale, 'account.rich.waiting')))
        stale |= bool(item and item['status'] != 'current')
    compact = ' · '.join(f'{label}: {value}' for label, value in rows)
    return Section(server_label(node), (compact, *((tr(locale, 'traffic.node_unknown'),) if stale else ())),
        divider_after=divider_after, heading_size=3)



async def show_account_stats(chat_id: int, user_id: int, message_id: int,
                             profile_id: str, bot: Bot, backend: BackendClient,
                             state: FSMContext, *, back_to: str = 'home') -> None:
    locale = normalize_locale((await state.get_data()).get('locale'))
    summary = await backend.member_profile_summary(user_id, profile_id)
    lines = profile_statistics(summary, locale)
    await render(bot, chat_id, Screen(tr(locale, 'account.stats_title'), lines, embedded_buttons=True, navigation=True),
        [[button(user_id, tr(locale, 'back'), 'account_profile', profile_id, back_to)]],
        state, message_id)


def profile_statistics(summary: dict, locale: str) -> tuple[str, ...]:
    lines = (tr(locale, 'account.member_since',
                value=(summary.get('created_at') or '')[:10] or '—'),
             tr(locale, 'account.stats_nodes', count=summary.get('node_count', len(summary.get('nodes', [])))),
             tr(locale, 'account.stats_protocols', count=summary.get('protocol_count', 0)),
             tr(locale, 'account.stats_xray', count=summary.get('xray_count', 0)),
             tr(locale, 'account.stats_awg', count=summary.get('awg_count', 0)),
             tr(locale, 'account.stats_issued', count=summary.get('issued_count', 0)),
             tr(locale, 'account.stats_last',
                value=(summary.get('last_issued_at') or '')[:16].replace('T', ' ') or '—'))
    traffic = summary.get('traffic')
    if traffic:
        lines += (tr(locale, 'traffic.status.' + traffic['status']),)
        if traffic['items']:
            lines += (tr(locale, 'traffic.month_total', month=traffic.get('month') or '—',
                total=_traffic_bytes(sum(item['uplink_bytes'] + item['downlink_bytes']
                    for item in traffic['items']))),)
    return lines


def profile_node_traffic(summary: dict, node: dict, locale: str) -> tuple[str, ...]:
    traffic = summary.get('traffic')
    lines = []
    for protocol in node['protocols']:
        label = tr(locale, 'protocol.' + protocol)
        if not traffic:
            lines.append(label)
            continue
        item = next((item for item in traffic.get('nodes', [])
                     if item['node_key'] == node['key'] and item['protocol'] == protocol), None)
        if item is None:
            lines.append(tr(locale, 'traffic.node_waiting', protocol=label))
            continue
        lines.append(tr(locale, 'traffic.node_month', protocol=label,
            month=traffic.get('month') or '—',
            total=_traffic_bytes(item['uplink_bytes'] + item['downlink_bytes']),
            upload=_traffic_bytes(item['uplink_bytes']), download=_traffic_bytes(item['downlink_bytes'])))
        if item['status'] != 'current':
            lines.append(tr(locale, 'traffic.node_unknown'))
    return tuple(lines)


def _traffic_bytes(value: int) -> str:
    amount = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB'):
        if amount < 1024 or unit == 'EiB':
            return f'{amount:.1f} {unit}'
        amount /= 1024
    raise ValueError('invalid traffic size')


async def show_profile(chat_id: int, user_id: int, message_id: int,
                       profile_id: str, bot: Bot, backend: BackendClient,
                       state: FSMContext, *, page_index: int | None = None) -> None:
    locale = normalize_locale((await state.get_data()).get('locale'))
    page = await backend.profile_nodes(user_id, profile_id)
    for node in page['items']:
        await remember_node(state, node)
    data = await state.get_data()
    saved = data.get('member_nodes_page', {})
    if page_index is None:
        page_index = saved.get('page', 0) if saved.get('profile_id') == profile_id else 0
    nodes, page_index, pages = server_page(page['items'], page_index)
    await state.update_data(member_nodes_page={'profile_id': profile_id, 'page': page_index})
    sections = region_sections(nodes, locale, lambda node: Section(server_label(node),
        rows=(tuple(button(user_id, tr(locale, f"protocol.{protocol['kind']}"), 'protocol',
            profile_id, node['key'], protocol['kind']) for protocol in node['protocols']),),
        divider_after=True, heading_size=3))
    back = (await state.get_data()).get('member_profile_back', 'profiles')
    await render(bot, chat_id, Screen(tr(locale, 'home.get_config'),
        () if page['items'] else (tr(locale, 'nodes.empty'),),
        sections=sections, embedded_buttons=True, navigation=True),
        [*server_pagination(user_id, locale, page_index, pages, 'config_nodes_page', profile_id),
         [button(user_id, tr(locale, 'back'), back)]], state, message_id)



async def show_node(chat_id: int, user_id: int, message_id: int,
                    profile_id: str, node_key: str, bot: Bot,
                    backend: BackendClient, state: FSMContext) -> None:
    page = await backend.profile_nodes(user_id, profile_id)
    node = next((item for item in page['items'] if item['key'] == node_key), None)
    if node is None:
        await show_profile(chat_id, user_id, message_id, profile_id, bot, backend, state)
        return
    await remember_node(state, node)
    locale = normalize_locale((await state.get_data()).get('locale'))
    rows = [[button(user_id, tr(locale, f"protocol.{protocol['kind']}"),
                    'protocol', profile_id, node_key, protocol['kind'])]
            for protocol in node['protocols']]
    rows.append([button(user_id, tr(locale, 'back'), 'profile', profile_id)])
    await render(bot, chat_id, Screen(server_label(node),
        (node['region'], tr(locale, 'node.choose_protocol')), embedded_buttons=True, navigation=True),
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
    await remember_node(state, node)
    if protocol == 'awg':
        from .user_devices import show_picker
        await show_picker(chat_id, user_id, message_id, profile_id, node_key,
                          bot, backend, state)
        return
    transports = [kind for kind in ('xhttp', 'tcp') if kind in selected['transports']]
    rows = [[button(user_id, tr(locale, f'transport.{kind}'), 'issue',
                    profile_id, node_key, 'xray', kind) for kind in transports],
            [button(user_id, tr(locale, 'back'), 'profile', profile_id)]]
    await render(bot, chat_id, Screen(tr(locale, 'xray.title', node=server_label(node)),
        (tr(locale, 'transport.choose'),), sections=(Section(tr(locale, 'ui.transport_help'),
            (tr(locale, 'ui.transport_help_text'),), collapsed=True),),
        embedded_buttons=True, navigation=True), rows, state, message_id)



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
    device_back = result['protocol'] == 'awg' and result.get('device_id')
    rows = [[button(user_id, tr(locale, 'back'),
                    'device_picker_back' if device_back else ('profile' if result['protocol'] == 'awg' else 'protocol'),
                    profile_id, *((node_key,) if device_back else (() if result['protocol'] == 'awg' else (node_key, 'xray'))) )]]
    if result['status'] == 'succeeded':
        artifact = await backend.artifact(user_id, issuance_id)
        if (await state.get_data()).get('issuance_poll_token') != view_token:
            return
        content = artifact['content']
        filename = artifact['filename'] or f"{result['protocol']}-{node_key}.txt"
        files = artifact.get('files') or [{'filename': filename, 'content': content}]
        uri = content if result['protocol'] == 'xray' or result['transport'] == 'vpn' else None
        image = await asyncio.to_thread(config_qr,
            qr_payload(result['protocol'], result['transport'], uri)) if uri else None
        if (await state.get_data()).get('issuance_poll_token') != view_token:
            return
        import_hint = tr(locale, 'config.import_xray' if result['protocol'] == 'xray'
                         else 'config.import_awg_vpn' if uri else 'config.import_awg_conf')
        title = artifact.get('display_name') or tr(locale, 'config.ready')
        breadcrumbs = ()
        if device_back:
            data = await state.get_data()
            title = data.get('_navigation_devices', {}).get(result['device_id'])
            if not title:
                from .user_devices import get_device
                item = await get_device(backend, user_id, profile_id, result['device_id'])
                title = item['display_name']
                await remember_label(state, 'devices', result['device_id'], title)
            picker = button(user_id, tr(locale, 'devices.title'), 'device_picker', profile_id, node_key)
            breadcrumbs = parent_path(picker.callback_data, locale, await state.get_data())
        screen = Screen(title,
            (tr(locale, 'ui.config_intro'),),
            uri=uri, uri_collapsed=True, uri_title=tr(locale, 'ui.config_link'), qr=image, qr_title=tr(locale, 'ui.config_qr'),
            sections=(), details_title=tr(locale, 'ui.config_help'), details_lines=(import_hint,),
            files=tuple((item['filename'], item['content'].encode()) for item in files),
            files_title=tr(locale, 'ui.config_files'),
            uri_rows=((button(user_id, tr(locale, 'config.link.send'), 'issuance_plain', issuance_id),),) if uri else (),
            embedded_buttons=True, navigation=True, breadcrumbs=breadcrumbs,
            navigation_return=button(user_id, tr(locale, 'config.ready'), 'issuance', issuance_id).callback_data)
        rich = await render(bot, chat_id, screen, rows, state, message_id)
        # Older Telegram deployments may reject rich media. Preserve downloads
        # there, while supported deployments keep everything in the control message.
        data = await state.get_data()
        if rich is False and data.get('issuance_poll_token') == view_token and issuance_id not in data.get('delivered_issuances', []):
            for name, body in screen.files:
                sent = await bot.send_document(chat_id, BufferedInputFile(body, name))
                if (await state.get_data()).get('issuance_poll_token') != view_token:
                    try:
                        await bot.delete_message(chat_id, sent.message_id)
                    except TelegramAPIError:
                        pass
                    return
                await track_artifact(state, sent.message_id)
            if image:
                sent = await bot.send_photo(chat_id, BufferedInputFile(image, 'config.png'))
                if (await state.get_data()).get('issuance_poll_token') != view_token:
                    try:
                        await bot.delete_message(chat_id, sent.message_id)
                    except TelegramAPIError:
                        pass
                    return
                await track_artifact(state, sent.message_id)
            data = await state.get_data()
            await state.update_data(delivered_issuances=[*data.get('delivered_issuances', []), issuance_id][-20:])
    elif result['status'] in {'blocked', 'superseded', 'failed'}:
        await render(bot, chat_id, Screen(tr(locale, 'config.not_ready'),
            (tr(locale, 'config.unavailable'),), embedded_buttons=True, navigation=True),
            rows, state, message_id)
    else:
        rows.insert(0, [button(user_id, tr(locale, 'config.refresh'), 'issuance', issuance_id)])
        await render(bot, chat_id, Screen(tr(locale, 'config.preparing_title'),
            (tr(locale, 'config.pending'),), embedded_buttons=True), rows, state, message_id)


async def issue(chat_id: int, user_id: int, message_id: int, profile_id: str,
                node_key: str, protocol: str, transport: str, bot: Bot,
                backend: BackendClient, state: FSMContext, *, device_id: str | None = None) -> None:
    locale = normalize_locale((await state.get_data()).get('locale'))
    poll_token = secrets.token_urlsafe(16)
    await state.update_data(issuance_poll_token=poll_token)

    async def owns_screen():
        return (await state.get_data()).get('issuance_poll_token') == poll_token

    try:
        await render(bot, chat_id, Screen(tr(locale, 'config.preparing_title'),
            (tr(locale, 'config.preparing'),), embedded_buttons=True, navigation=True),
            [[button(user_id, tr(locale, 'back'),
                     'device_picker_back' if device_id else ('profile' if protocol == 'awg' else 'protocol'), profile_id,
                     *((node_key,) if device_id else (() if protocol == 'awg' else (node_key, protocol))))]], state, message_id)
        queued = await backend.issue(user_id, profile_id, node_key, protocol, transport,
            **({'device_id': device_id} if device_id else {}))
        if not await owns_screen():
            return
        if queued.get('status') in {'succeeded', 'blocked', 'superseded', 'failed'}:
            await show_issuance(chat_id, user_id, message_id, queued['id'], bot, backend, state)
            return
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
            (tr(locale, 'qr.too_long'),), embedded_buttons=True, navigation=True), rows, state, message_id)
        return
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L,
                         box_size=6, border=4)
    code.add_data(content)
    try:
        code.make(fit=True)
    except DataOverflowError:
        await render(bot, chat_id, Screen(tr(locale, 'qr.unavailable'),
            (tr(locale, 'qr.too_long'),), embedded_buttons=True, navigation=True), rows, state, message_id)
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
    await render(bot, chat_id, Screen(tr(locale, 'qr.ready'), (tr(locale, 'qr.scan'),), embedded_buttons=True, navigation=True),
                 rows, state, message_id)


def config_qr(content: str) -> bytes | None:
    if len(content.encode()) > 2500:
        return None
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L,
                         box_size=6, border=4)
    code.add_data(content)
    try:
        code.make(fit=True)
    except DataOverflowError:
        return None
    image = BytesIO()
    code.make_image(fill_color='black', back_color='white').save(image, format='PNG')
    return image.getvalue()


def qr_payload(protocol: str, transport: str, content: str) -> str:
    return content.removeprefix('vpn://') if protocol == 'awg' and transport == 'vpn' else content


async def send_plain_uri(chat_id, user_id, issuance_id, bot, backend, state):
    view_token = secrets.token_urlsafe(16)
    await state.update_data(issuance_poll_token=view_token)
    result = await backend.issuance(user_id, issuance_id)
    if result['status'] != 'succeeded' or not (result['protocol'] == 'xray' or result['transport'] == 'vpn'):
        raise BackendError('configuration_unavailable', 409)
    artifact = await backend.artifact(user_id, issuance_id)
    data = await state.get_data()
    if data.get('issuance_poll_token') != view_token:
        return
    uri = artifact['content']
    locale = normalize_locale(data.get('locale'))
    sent = await bot.send_message(chat_id=chat_id, text=uri, parse_mode=None,
        entities=[MessageEntity(type='code', offset=0, length=len(uri.encode('utf-16-le')) // 2)],
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=tr(locale, 'setup.close'), callback_data='config_uri_close')]]))
    if (await state.get_data()).get('issuance_poll_token') != view_token:
        try:
            await bot.delete_message(chat_id, sent.message_id)
        except TelegramAPIError:
            pass
        return
    # A compatibility message is an artifact, never the main control panel.
    previous = data.get('plain_uri_message_id')
    if previous and previous != sent.message_id:
        try:
            await bot.delete_message(chat_id, previous)
        except TelegramAPIError:
            pass
    await track_artifact(state, sent.message_id)
    await state.update_data(plain_uri_message_id=sent.message_id)


@router.callback_query(F.data == 'config_uri_close')
async def close_plain_uri_cb(query: CallbackQuery, bot: Bot, state: FSMContext):
    if query.message is None or query.from_user is None or query.message.chat.type != 'private' or query.message.chat.id != query.from_user.id:
        return
    await query.answer()
    data = await state.get_data()
    message_id = query.message.message_id
    if message_id == data.get('control_message_id'):
        return
    try:
        await bot.delete_message(query.message.chat.id, message_id)
    except TelegramAPIError:
        pass
    await state.update_data(artifact_message_ids=[item for item in data.get('artifact_message_ids', []) if item != message_id],
        plain_uri_message_id=None if message_id == data.get('plain_uri_message_id') else data.get('plain_uri_message_id'))


@router.callback_query(F.data.startswith('u:'))
async def user_action_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                         state: FSMContext) -> None:
    if query.message is None or query.from_user is None or query.message.chat.type != 'private':
        return
    token = (query.data or '')[2:]
    action = actions.get(token)
    if action is not None and action.owner_id != query.from_user.id:
        locale = normalize_locale((await state.get_data()).get('locale') or query.from_user.language_code)
        await query.answer(tr(locale, 'callback.stale'), show_alert=True)
        return
    if action is not None:
        actions.move_to_end(token)
    await state.update_data(issuance_poll_token=None)
    try:
        text = tr(action.args[0], 'language.saving') if action and action.name == 'first_locale' else None
        await query.answer(text=text, request_timeout=3)
    except (TelegramBadRequest, TelegramNetworkError):
        logging.getLogger(__name__).warning('Callback acknowledgement unavailable; continuing screen navigation')
    chat_id, user_id, message_id = query.message.chat.id, query.from_user.id, query.message.message_id
    try:
        if action is None:
            # Lost on restart or evicted from the bounded cache: never replay an
            # unknown command. Re-authorize and refresh this control message.
            await clear_artifacts(bot, chat_id, state)
            account = await backend.me(user_id)
            await state.update_data(locale=normalize_locale(account.get('locale') or
                                                            query.from_user.language_code))
            if account.get('locale_selected') is False:
                await show_language_picker(chat_id, user_id, bot, backend, state, message_id)
            else:
                await show_home(chat_id, user_id, bot, backend, state, message_id, account=account)
            return
        if action.name == 'page_number':
            return
        await state.set_state(None)
        if action.name != 'device_delete_confirm':
            await state.update_data(device_delete_confirmation=None)
        if action.name not in {'issuance', 'issuance_plain'}:
            await clear_artifacts(bot, chat_id, state)
        if action.name.startswith('device_'):
            from .user_devices import handle_action
            await handle_action(action, query, bot, backend, state)
        elif action.name == 'home':
            await show_home(chat_id, user_id, bot, backend, state, message_id,
                            cached_navigation=True)
        elif action.name == 'first_locale':
            started = time.monotonic()
            logging.getLogger(__name__).info('Language selection received; saving preference and opening home')
            try:
                async with asyncio.timeout(ONBOARDING_TIMEOUT):
                    account = await backend.set_locale(user_id, action.args[0])
                    await state.update_data(locale=action.args[0])
                    await show_home(chat_id, user_id, bot, backend, state, message_id,
                                    account=account, quick_start=True)
            except TimeoutError:
                raise BackendError('backend_unavailable', 503) from None
            finally:
                logging.getLogger(__name__).info('Language selection transition finished in %.3fs',
                                                time.monotonic() - started)
        elif action.name == 'profiles':
            await show_profiles(chat_id, user_id, message_id, bot, backend, state)
        elif action.name == 'profile':
            await show_profile(chat_id, user_id, message_id, action.args[0], bot, backend, state)
        elif action.name == 'config_nodes_page':
            await show_profile(chat_id, user_id, message_id, action.args[0], bot, backend, state,
                               page_index=int(action.args[1]))
        elif action.name == 'node':
            await show_node(chat_id, user_id, message_id, *action.args, bot, backend, state)
        elif action.name == 'protocol':
            await show_protocol(chat_id, user_id, message_id, *action.args, bot, backend, state)
        elif action.name == 'issue':
            await issue(chat_id, user_id, message_id, *action.args, bot, backend, state)
        elif action.name == 'issuance':
            await show_issuance(chat_id, user_id, message_id, action.args[0], bot, backend, state)
        elif action.name == 'issuance_plain':
            await send_plain_uri(chat_id, user_id, action.args[0], bot, backend, state)
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
        elif action.name == 'account_nodes_page':
            await show_account_profile(chat_id, user_id, message_id, action.args[0],
                bot, backend, state, username=query.from_user.username,
                back_to=action.args[1], page_index=int(action.args[2]), servers_open=True)
        elif action.name == 'account_stats':
            await show_account_stats(chat_id, user_id, message_id, action.args[0],
                bot, backend, state, back_to=action.args[1])
        elif action.name == 'member_settings':
            await show_member_settings(chat_id, user_id, message_id, bot, backend, state)
        elif action.name == 'announcement_silent':
            await backend.set_announcement_silent(user_id, action.args[0] == 'true')
            await show_member_settings(chat_id, user_id, message_id, bot, backend, state)
        elif action.name == 'set_locale':
            await backend.set_locale(user_id, action.args[0])
            await state.update_data(locale=action.args[0])
            await show_member_settings(chat_id, user_id, message_id, bot, backend, state)
        elif action.name == 'request_access':
            request = await backend.request_access(user_id)
            await show_home(chat_id, user_id, bot, backend, state, message_id)
            from .admin_requests import notify_admins
            await notify_admins(bot, backend, request['id'])
        elif action.name == 'admin_menu':
            await show_admin_menu(chat_id, user_id, message_id, bot, backend, state)
    except BackendError as exc:
        locale = normalize_locale((await state.get_data()).get('locale'))
        if exc.code in {'access_requests_disabled', 'request_already_pending'}:
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
            (tr(locale, 'action.error.' + cause),), embedded_buttons=True, navigation=True),
            [[button(user_id, tr(locale, 'home.title'), 'home')]],
            state, message_id)


async def show_admin_menu(chat_id: int, user_id: int, message_id: int,
                          bot: Bot, backend: BackendClient,
                          state: FSMContext) -> None:
    from .callbacks import (AdminNodesCallback,
                            AdminProfilesCallback, AdminSettingsCallback, RequestsCallback)
    locale = normalize_locale((await state.get_data()).get('locale'))
    async def optional_read(read):
        try:
            return await asyncio.wait_for(read, timeout=ADMIN_MENU_TIMEOUT)
        except TimeoutError:
            raise BackendError('backend_unavailable', 503) from None

    title, overview = await asyncio.gather(optional_read(backend.bot_title(user_id)),
        optional_read(backend.admin_overview(user_id)), return_exceptions=True)
    # Authorization failures must never be disguised as an unavailable summary.
    for result in (title, overview):
        if isinstance(result, BackendError) and result.status in {401, 403}:
            raise result
        if isinstance(result, Exception) and not isinstance(result, BackendError):
            raise result
    bot_title = title['title'] if isinstance(title, dict) else tr(locale, 'home.title')
    requests_button = InlineKeyboardButton(text=tr(locale, 'admin.requests'),
        callback_data=RequestsCallback().pack())
    sections = []
    if isinstance(overview, dict):
        sections.append(Section(tr(locale, 'admin.rich.overview'),
            tables=(Table(
                (tr(locale, 'admin.rich.item'), tr(locale, 'admin.rich.value')),
                ((tr(locale, 'admin.nodes'), f"{overview['nodes_enabled']}/{overview['nodes_total']}"),
                 (tr(locale, 'admin.profiles'), f"{overview['profiles_active']}/{overview['profiles_total']}"),
                 (tr(locale, 'admin.requests'), str(overview['pending_requests'])),
                 (tr(locale, 'admin.status.open_problems'), str(len(overview['problem_nodes']))))),)))
        attention_lines, attention_buttons = [], []
        if overview['pending_requests']:
            attention_lines.append(tr(locale, 'admin.status.pending', count=overview['pending_requests']))
            attention_buttons.append(requests_button.model_copy(update={'style': 'primary'}))
        if overview['problem_nodes']:
            attention_lines.append(tr(locale, 'admin.status.problems', count=len(overview['problem_nodes'])))
            attention_buttons.append(InlineKeyboardButton(text=tr(locale, 'admin.status.open_problems'),
                callback_data='admin_problem_nodes', style='primary'))
        if attention_buttons:
            sections.append(Section(tr(locale, 'admin.rich.attention'), tuple(attention_lines),
                                    (tuple(attention_buttons),)))
    else:
        sections.append(Section(tr(locale, 'admin.rich.overview'),
                                (tr(locale, 'admin.rich.overview_unavailable'),)))
    sections.extend((
        Section(tr(locale, 'admin.rich.management'), rows=((
            InlineKeyboardButton(text=tr(locale, 'admin.profiles'), callback_data=AdminProfilesCallback().pack()),
            InlineKeyboardButton(text=tr(locale, 'admin.nodes'), callback_data=AdminNodesCallback().pack())),)),
        *((Section(tr(locale, 'admin.rich.access_management') +
            (' · ' + tr(locale, 'requests.pending_count', count=overview['pending_requests'])
             if isinstance(overview, dict) else ''), rows=((requests_button,),)),)
          if isinstance(overview, dict) and overview['pending_requests'] else ()),
        Section(tr(locale, 'admin.rich.system'), rows=((
            InlineKeyboardButton(text=tr(locale, 'admin.status'), callback_data='admin_status'),
            InlineKeyboardButton(text=tr(locale, 'admin.settings'), callback_data=AdminSettingsCallback().pack(),
                style='danger' if isinstance(overview, dict) and overview.get('unacknowledged_alerts') else None)),
            (InlineKeyboardButton(text=tr(locale, 'announce.title'), callback_data='announce_menu'),))),
    ))
    await render(bot, chat_id, Screen(f"{tr(locale, 'admin.menu')} · {bot_title}",
        sections=tuple(sections), embedded_buttons=True, navigation=True),
        [[button(user_id, tr(locale, 'back'), 'home')]], state, message_id)


@router.callback_query(F.data == 'admin_status')
async def admin_status_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                          state: FSMContext) -> None:
    await query.answer()
    await show_admin_status(query.message.chat.id, query.from_user.id,
        query.message.message_id, bot, backend, state)


async def show_admin_status(chat_id: int, user_id: int, message_id: int,
                            bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    from .callbacks import RequestsCallback
    locale = normalize_locale((await state.get_data()).get('locale'))
    overview = await backend.admin_overview(user_id)
    lines = [tr(locale, 'admin.status.version', version=overview['version']),
             tr(locale, 'admin.status.nodes', active=overview['nodes_enabled'],
                total=overview['nodes_total']),
             tr(locale, 'admin.status.profiles', active=overview['profiles_active'],
                total=overview['profiles_total']),
             tr(locale, 'admin.status.frozen', count=overview['profiles_frozen']),
             tr(locale, 'admin.status.pending', count=overview['pending_requests']),
             tr(locale, 'admin.status.problems', count=len(overview['problem_nodes']))]
    rows = []
    if overview['pending_requests']:
        rows.append([InlineKeyboardButton(text=tr(locale, 'admin.requests'),
            callback_data=RequestsCallback().pack())])
    if overview['problem_nodes']:
        rows.append([InlineKeyboardButton(text=tr(locale, 'admin.status.open_problems'),
            callback_data='admin_problem_nodes')])
    attention = tuple(tuple(row) for row in rows)
    rows = [[InlineKeyboardButton(text=tr(locale, 'admin.status.refresh'),
        callback_data='admin_status')],
        [InlineKeyboardButton(text=tr(locale, 'back'), callback_data='admin_menu')]]
    sections = [Section(tr(locale, 'admin.rich.overview'), tables=(Table(
        (tr(locale, 'admin.rich.item'), tr(locale, 'admin.rich.value')),
        ((tr(locale, 'admin.nodes'), f"{overview['nodes_enabled']}/{overview['nodes_total']}"),
         (tr(locale, 'admin.profiles'), f"{overview['profiles_active']}/{overview['profiles_total']}"),
         (tr(locale, 'admin.rich.frozen'), str(overview['profiles_frozen'])),
         (tr(locale, 'admin.requests'), str(overview['pending_requests'])),
         (tr(locale, 'admin.status.open_problems'), str(len(overview['problem_nodes']))))),))]
    if attention:
        sections.append(Section(tr(locale, 'admin.rich.attention'), rows=attention))
    sections.append(Section(tr(locale, 'nodes.rich.technical'), collapsed=True, lines=(lines[0],)))
    await render(bot, chat_id, Screen(tr(locale, 'admin.status.title'),
        sections=tuple(sections), embedded_buttons=True, navigation=True), rows[-2:], state, message_id)


@router.callback_query(F.data == 'admin_problem_nodes')
async def admin_problem_nodes_cb(query: CallbackQuery, bot: Bot,
                                 backend: BackendClient, state: FSMContext) -> None:
    from .callbacks import AdminNodeCallback
    await query.answer()
    await show_admin_problem_nodes(query.message.chat.id, query.from_user.id,
        query.message.message_id, bot, backend, state)


@router.callback_query(F.data.startswith('admin_problem_page:'))
async def admin_problem_page_cb(query: CallbackQuery, bot: Bot,
                                backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    page = query.data.split(':', 1)[1]
    if not page.isdecimal() or len(page) > 6:
        return
    await show_admin_problem_nodes(query.message.chat.id, query.from_user.id,
        query.message.message_id, bot, backend, state, int(page))


async def show_admin_problem_nodes(chat_id, user_id, message_id, bot, backend, state, page=0):
    from .callbacks import AdminNodeCallback
    locale = normalize_locale((await state.get_data()).get('locale'))
    overview = await backend.admin_overview(user_id)
    affected = sorted(overview['problem_nodes'], key=lambda node: (
        node.get('region') or '', node['title'], node['key']))
    pages = max(1, (len(affected) + 9) // 10)
    page = min(max(0, page), pages - 1)
    sections = region_sections(affected[page * 10:(page + 1) * 10], locale,
        lambda node: Section('', rows=((InlineKeyboardButton(text=server_label(node),
            callback_data=AdminNodeCallback(node_key=node['key']).pack()),),)))
    rows = []
    if pages > 1:
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton(text='←', callback_data=f'admin_problem_page:{page - 1}'))
        navigation.append(InlineKeyboardButton(text=f'{page + 1}/{pages}', callback_data=f'admin_problem_page:{page}'))
        if page + 1 < pages:
            navigation.append(InlineKeyboardButton(text='→', callback_data=f'admin_problem_page:{page + 1}'))
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text=tr(locale, 'admin.status.refresh'),
        callback_data=f'admin_problem_page:{page}')])
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data='admin_status')])
    lines = (tr(locale, 'admin.status.problem_nodes_hint'),) if affected else (
        tr(locale, 'admin.status.no_problems'),)
    await render(bot, chat_id,
        Screen(tr(locale, 'admin.status.problem_nodes'), lines, sections=tuple(sections),
            embedded_buttons=True, navigation=True), rows, state, message_id)


async def show_member_settings(chat_id: int, user_id: int, message_id: int,
                               bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    locale = normalize_locale((await state.get_data()).get('locale'))
    current = await backend.me(user_id)
    locale = normalize_locale(current.get('locale') or locale)
    await state.update_data(locale=locale, locale_restore_pending=False)
    silent = current.get('announcement_silent', False)
    selected_locale = locale
    sections = [Section(tr(locale, 'settings.locale'), rows=(tuple(
        button(user_id, tr(locale, 'settings.russian' if kind == 'ru' else 'settings.english'),
               'set_locale', kind).model_copy(update={'style': 'primary' if selected_locale == kind else None})
        for kind in ('ru', 'en')),)),
        Section(tr(locale, 'ui.notifications'), rows=((
            button(user_id, tr(locale, 'ui.enable_sound'), 'announcement_silent', 'false')
                .model_copy(update={'style': 'primary' if not silent else None}),
            button(user_id, tr(locale, 'ui.disable_sound'), 'announcement_silent', 'true')
                .model_copy(update={'style': 'primary' if silent else None}),),))]
    await render(bot, chat_id, Screen(tr(locale, 'settings.title'),
        sections=tuple(sections), embedded_buttons=True, navigation=True),
        [[button(user_id, tr(locale, 'back'), 'home')]], state, message_id)


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
