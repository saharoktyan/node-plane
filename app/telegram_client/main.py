"""First aiogram client slice: access request, profiles, and VPN artifacts."""
from __future__ import annotations

import asyncio
from io import BytesIO
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
import qrcode
from qrcode.exceptions import DataOverflowError

from .backend import BackendClient, BackendError
from .screens import Screen


@dataclass(frozen=True)
class Action:
    owner_id: int
    name: str
    args: tuple[str, ...]
    expires_at: float


@dataclass(frozen=True)
class Draft:
    account_id: str
    message_id: int
    expires_at: float
    command_key: str
    submitted_name: str | None = None


@dataclass
class NodeDraft:
    message_id: int
    command_key: str
    expires_at: float
    base: dict | None = None
    submitted_protocols: str | None = None


@dataclass
class AgentDraft:
    node_key: str
    message_id: int
    command_key: str
    expires_at: float
    ssh_target: str | None = None


@dataclass
class NodeEditDraft:
    node_key: str
    field: str
    message_id: int
    revision: int
    settings: dict
    command_key: str
    expires_at: float
    submitted_value: str | None = None


@dataclass
class MaintenanceDraft:
    node_key: str
    message_id: int
    expires_at: float


@dataclass
class RuntimeRefreshDraft:
    node_key: str
    revision: int
    settings: dict
    edit_key: str
    apply_key: str
    expires_at: float


class TelegramClient:
    def __init__(self, bot: Bot, backend: BackendClient):
        self.bot, self.backend = bot, backend
        self.router = Router()
        self.actions: dict[str, Action] = {}
        self.drafts: dict[int, Draft] = {}
        self.node_drafts: dict[int, NodeDraft] = {}
        self.agent_drafts: dict[int, AgentDraft] = {}
        self.node_edit_drafts: dict[int, NodeEditDraft] = {}
        self.maintenance_drafts: dict[int, MaintenanceDraft] = {}
        self.runtime_refresh_drafts: dict[int, RuntimeRefreshDraft] = {}
        self.control_messages: dict[int, int] = {}
        self.router.message.register(self.start, CommandStart())
        self.router.message.register(self.text_input, F.text)
        self.router.callback_query.register(self.callback, F.data.startswith('np:'))

    def button(self, owner_id: int, label: str, name: str, *args: str) -> InlineKeyboardButton:
        token = secrets.token_urlsafe(12)
        self.actions[token] = Action(owner_id, name, tuple(args), time.monotonic() + 900)
        return InlineKeyboardButton(text=label, callback_data='np:' + token)

    async def render(self, chat_id: int, owner_id: int, screen: Screen,
                     rows: list[list[InlineKeyboardButton]], message_id: int | None = None) -> None:
        # Rich blocks carry content; the inline keyboard is only navigation.
        markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        now = time.monotonic()
        self.actions = {token: action for token, action in self.actions.items()
                        if action.expires_at > now}
        existing = message_id or self.control_messages.get(chat_id)
        if existing:
            try:
                await self.bot.edit_message_text(chat_id=chat_id, message_id=existing,
                    rich_message=screen.rich(), reply_markup=markup)
                self.control_messages[chat_id] = existing
                return
            except TelegramBadRequest as exc:
                if 'message is not modified' in str(exc).lower():
                    return
                try:
                    await self.bot.edit_message_text(chat_id=chat_id,
                        message_id=existing, text=screen.plain(), reply_markup=markup)
                    self.control_messages[chat_id] = existing
                    return
                except TelegramBadRequest:
                    pass
        try:
            sent = await self.bot.send_rich_message(chat_id=chat_id,
                rich_message=screen.rich(), reply_markup=markup)
        except TelegramBadRequest:
            sent = await self.bot.send_message(chat_id=chat_id,
                text=screen.plain(), reply_markup=markup)
        self.control_messages[chat_id] = sent.message_id

    async def home(self, chat_id: int, user_id: int, message_id: int | None = None) -> None:
        account = await self.backend.me(user_id)
        if account['status'] != 'approved':
            own = await self.backend.request('GET', '/api/v1/me/access-requests?limit=25',
                                              telegram_user_id=user_id)
            pending = any(item['status'] == 'pending' for item in own['items'])
            lines = ('Your access request is waiting for approval.',) if pending else (
                'Request access to receive VPN configurations.',)
            rows = [] if pending else [[self.button(user_id, 'Request access', 'request_access')]]
            await self.render(chat_id, user_id, Screen('Node Plane', lines), rows, message_id)
            return
        rows = [[self.button(user_id, 'My profiles', 'profiles')]]
        if account['role'] == 'admin':
            rows.append([self.button(user_id, 'Access requests', 'requests')])
            rows.append([self.button(user_id, 'Profiles', 'admin_profiles'),
                         self.button(user_id, 'Accounts', 'accounts')])
            rows.append([self.button(user_id, 'Nodes', 'admin_nodes')])
            rows.append([self.button(user_id, 'Updates', 'updates')])
        await self.render(chat_id, user_id,
            Screen('Node Plane', ('Choose what to manage.',)), rows, message_id)

    async def profiles(self, chat_id: int, user_id: int, message_id: int) -> None:
        page = await self.backend.profiles(user_id)
        rows = [[self.button(user_id, p['display_name'], 'profile', p['id'])]
                for p in page['items']]
        rows.append([self.button(user_id, 'Back', 'home')])
        await self.render(chat_id, user_id, Screen('My profiles',
            ('Select a profile to get a current configuration.',) if page['items'] else
            ('No profiles are assigned yet.',)), rows, message_id)

    async def profile(self, chat_id: int, user_id: int, message_id: int,
                      profile_id: str) -> None:
        profile = await self.backend.request('GET', f'/api/v1/profiles/{profile_id}',
                                             telegram_user_id=user_id)
        nodes = await self.backend.profile_nodes(user_id, profile_id)
        rows = [[self.button(user_id, f"{node['flag']} {node['title']}".strip(),
                             'node', profile_id, node['key'])] for node in nodes['items']]
        rows.append([self.button(user_id, 'Back', 'profiles')])
        status = 'Frozen' if profile['frozen'] else 'Active'
        await self.render(chat_id, user_id, Screen(profile['display_name'],
            (f'Status: {status}', 'Select a node.') if nodes['items'] else
            (f'Status: {status}', 'No nodes are available for this profile.')),
            rows, message_id)

    async def node(self, chat_id: int, user_id: int, message_id: int,
                   profile_id: str, node_key: str) -> None:
        nodes = await self.backend.profile_nodes(user_id, profile_id)
        node = next((item for item in nodes['items'] if item['key'] == node_key), None)
        if node is None:
            await self.home(chat_id, user_id, message_id)
            return
        rows = []
        for protocol in node['protocols']:
            for transport in protocol['transports']:
                rows.append([self.button(user_id, f"{protocol['kind'].upper()} · {transport.upper()}",
                    'issue', profile_id, node_key, protocol['kind'], transport)])
        rows.append([self.button(user_id, 'Back', 'profile', profile_id)])
        await self.render(chat_id, user_id,
            Screen(node['title'], (f"Region: {node['region']}", 'Choose a configuration format.')),
            rows, message_id)

    async def issue(self, chat_id: int, user_id: int, message_id: int,
                    profile_id: str, node_key: str, protocol: str, transport: str) -> None:
        queued = await self.backend.issue(user_id, profile_id, node_key, protocol, transport)
        await self.render(chat_id, user_id,
            Screen('Preparing configuration', ('Checking your access and the live node…',)),
            [[self.button(user_id, 'Back', 'node', profile_id, node_key)]], message_id)
        for _ in range(30):
            state = await self.backend.issuance(user_id, queued['id'])
            if state['status'] == 'succeeded':
                await self.issuance_status(chat_id, user_id, message_id,
                    queued['id'], profile_id, node_key, protocol, transport)
                return
            if state['status'] in {'blocked', 'superseded'}:
                break
            await asyncio.sleep(1)
        await self.issuance_status(chat_id, user_id, message_id,
            queued['id'], profile_id, node_key, protocol, transport)

    async def issuance_status(self, chat_id: int, user_id: int, message_id: int,
                              issuance_id: str, profile_id: str, node_key: str,
                              protocol: str, transport: str) -> None:
        state = await self.backend.issuance(user_id, issuance_id)
        rows = [[self.button(user_id, 'Back', 'node', profile_id, node_key)]]
        if state['status'] == 'succeeded':
            artifact = await self.backend.artifact(user_id, issuance_id)
            filename = artifact['filename'] or f'{protocol}-{node_key}-{transport}.txt'
            await self.bot.send_document(chat_id, BufferedInputFile(
                artifact['content'].encode(), filename=filename))
            details = (artifact['content'],) if protocol == 'xray' else ()
            if len(artifact['content'].encode()) <= 2500:
                rows.insert(0, [self.button(user_id, 'Show QR code', 'show_qr',
                    issuance_id, profile_id, node_key)])
            await self.render(chat_id, user_id, Screen('Configuration ready',
                ('Import the attached file into your VPN client.',),
                'VLESS link' if details else None, details), rows, message_id)
            return
        if state['status'] in {'blocked', 'superseded'}:
            await self.render(chat_id, user_id, Screen('Configuration unavailable',
                ('Check your access and the node status, then request a new config.',)),
                rows, message_id)
            return
        rows.insert(0, [self.button(user_id, 'Refresh', 'issuance_status',
            issuance_id, profile_id, node_key, protocol, transport)])
        await self.render(chat_id, user_id, Screen('Still preparing configuration',
            ('The backend is working. Refresh to check the same request.',)),
            rows, message_id)

    async def show_qr(self, chat_id: int, user_id: int, message_id: int,
                      issuance_id: str, profile_id: str, node_key: str) -> None:
        artifact = await self.backend.artifact(user_id, issuance_id)
        content = artifact['content']
        if len(content.encode()) > 2500:
            await self.render(chat_id, user_id, Screen('QR unavailable',
                ('This configuration is too large for a reliable QR code.',
                 'Use the attached file instead.')),
                [[self.button(user_id, 'Back', 'node', profile_id, node_key)]], message_id)
            return
        code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L,
                            box_size=6, border=4)
        code.add_data(content)
        try:
            code.make(fit=True)
        except DataOverflowError:
            await self.render(chat_id, user_id, Screen('QR unavailable',
                ('This configuration is too large for a QR code.',)),
                [[self.button(user_id, 'Back', 'node', profile_id, node_key)]], message_id)
            return
        image = BytesIO()
        code.make_image(fill_color='black', back_color='white').save(image, format='PNG')
        await self.bot.send_photo(chat_id, BufferedInputFile(image.getvalue(), 'config.png'))
        await self.render(chat_id, user_id, Screen('QR code ready',
            ('Scan the image sent below this menu.')),
            [[self.button(user_id, 'Back', 'node', profile_id, node_key)]], message_id)

    async def requests(self, chat_id: int, user_id: int, message_id: int) -> None:
        page = await self.backend.request('GET', '/api/v1/access-requests?limit=25',
                                          telegram_user_id=user_id)
        rows = []
        for item in page['items']:
            rows.append([self.button(user_id, f"Review {item['account_id'][:8]}",
                'review', item['id'])])
        if not page['items']:
            await self.home(chat_id, user_id, message_id)
            return
        rows.append([self.button(user_id, 'Back', 'home')])
        await self.render(chat_id, user_id, Screen('Access requests',
            (f"Pending: {len(page['items'])}",)), rows, message_id)

    async def notify_admins(self, request_id: str) -> None:
        for raw_id in os.environ.get('ADMIN_IDS', '').split(','):
            try:
                admin_id = int(raw_id.strip())
                admin = await self.backend.me(admin_id)
                if admin['role'] != 'admin' or admin['status'] != 'approved':
                    continue
                notice = Screen('Access request',
                    ('A user requested access to Node Plane.',))
                markup = InlineKeyboardMarkup(inline_keyboard=[[
                    self.button(admin_id, 'Review request',
                                'notification_review', request_id)]])
                await self.send_notice(admin_id, notice, markup)
            except (ValueError, BackendError, TelegramAPIError):
                continue

    async def notify_requester(self, admin_id: int, request_id: str,
                               account_id: str, decision: str) -> None:
        try:
            account = await self.backend.request('GET', f'/api/v1/accounts/{account_id}',
                                                  telegram_user_id=admin_id)
            recipient = account.get('telegram_user_id')
            if recipient:
                await self.send_notice(recipient, Screen('Access request',
                    ('Your access request was approved.' if decision == 'approve' else
                     'Your access request was rejected.',)))
        except (BackendError, TelegramAPIError):
            pass

    async def send_notice(self, chat_id: int, screen: Screen,
                          markup: InlineKeyboardMarkup | None = None) -> None:
        try:
            await self.bot.send_rich_message(chat_id=chat_id,
                rich_message=screen.rich(), reply_markup=markup)
        except TelegramBadRequest:
            await self.bot.send_message(chat_id=chat_id,
                text=screen.plain(), reply_markup=markup)

    async def review(self, chat_id: int, user_id: int, message_id: int,
                     request_id: str) -> None:
        await self.render(chat_id, user_id,
            Screen('Access request', ('Approve or reject this account.',)),
            [[self.button(user_id, 'Approve', 'decide', request_id, 'approve'),
              self.button(user_id, 'Reject', 'decide', request_id, 'reject')],
             [self.button(user_id, 'Back', 'requests')]], message_id)

    async def accounts(self, chat_id: int, user_id: int, message_id: int) -> None:
        self.drafts.pop(user_id, None)
        page = await self.backend.accounts(user_id)
        rows = []
        for account in page['items']:
            name = str(account.get('telegram_user_id') or account['id'][:8])
            rows.append([self.button(user_id, f"{name} · {account['status']}",
                'account', account['id'])])
        rows.append([self.button(user_id, 'Back', 'home')])
        await self.render(chat_id, user_id, Screen('Accounts',
            ('Select an account to create its VPN profile.',)), rows, message_id)

    async def account(self, chat_id: int, user_id: int, message_id: int,
                      account_id: str) -> None:
        account = await self.backend.request('GET', f'/api/v1/accounts/{account_id}',
                                             telegram_user_id=user_id)
        label = str(account.get('telegram_user_id') or account['id'][:8])
        await self.render(chat_id, user_id, Screen(f'Account {label}',
            (f"Status: {account['status']}", f"Role: {account['role']}")),
            [[self.button(user_id, 'Create VPN profile', 'new_profile', account_id)],
             [self.button(user_id, 'Back', 'accounts')]], message_id)

    async def admin_profiles(self, chat_id: int, user_id: int, message_id: int) -> None:
        page = await self.backend.admin_profiles(user_id)
        rows = [[self.button(user_id, item['display_name'], 'admin_profile', item['id'])]
                for item in page['items']]
        rows.append([self.button(user_id, 'Back', 'home')])
        await self.render(chat_id, user_id, Screen('VPN profiles',
            ('Select a profile to manage access.',) if page['items'] else
            ('No profiles yet. Create one from Accounts.',)), rows, message_id)

    async def admin_profile(self, chat_id: int, user_id: int, message_id: int,
                            profile_id: str) -> None:
        profile = await self.backend.request('GET', f'/api/v1/profiles/{profile_id}',
                                             telegram_user_id=user_id)
        grants = (await self.backend.profile_grants(user_id, profile_id))['items']
        status = 'Frozen' if profile['frozen'] else 'Active'
        rows = [[self.button(user_id, 'Unfreeze' if profile['frozen'] else 'Freeze',
                             'toggle_freeze', profile_id)],
                [self.button(user_id, 'Add access', 'grant_nodes', profile_id)]]
        for grant in grants:
            rows.append([self.button(user_id, f"Remove {grant['node_key']} · {grant['protocol'].upper()}",
                'remove_grant', profile_id, grant['node_key'], grant['protocol'])])
        rows.append([self.button(user_id, 'Back', 'admin_profiles')])
        lines = (f'Status: {status}', f'Access entries: {len(grants)}',
                 'Changes are applied by the backend worker.')
        await self.render(chat_id, user_id, Screen(profile['display_name'], lines),
            rows, message_id)

    async def grant_nodes(self, chat_id: int, user_id: int, message_id: int,
                          profile_id: str) -> None:
        page = await self.backend.admin_nodes(user_id)
        rows = [[self.button(user_id, node['title'], 'grant_protocols', profile_id, node['key'])]
                for node in page['items'] if node['enabled']]
        rows.append([self.button(user_id, 'Back', 'admin_profile', profile_id)])
        await self.render(chat_id, user_id, Screen('Choose a node',
            ('Only installed and enabled nodes can receive profile access.',)),
            rows, message_id)

    async def admin_nodes(self, chat_id: int, user_id: int, message_id: int) -> None:
        self.node_drafts.pop(user_id, None)
        self.agent_drafts.pop(user_id, None)
        page = await self.backend.admin_nodes(user_id)
        rows = [[self.button(user_id, f"{node['flag']} {node['title']}".strip(),
                             'admin_node', node['key'])] for node in page['items']]
        rows.append([self.button(user_id, 'Add node', 'new_node')])
        rows.append([self.button(user_id, 'Back', 'home')])
        await self.render(chat_id, user_id, Screen('Nodes',
            ('Select a node to inspect its settings and agent.',) if page['items'] else
            ('No nodes are registered yet.',)), rows, message_id)

    async def admin_node(self, chat_id: int, user_id: int, message_id: int,
                         node_key: str) -> None:
        node = await self.backend.request('GET', f'/api/v1/nodes/{node_key}',
                                          telegram_user_id=user_id)
        state = 'Ready' if node['enabled'] and node['desired_revision'] == node['applied_revision'] else 'Setup needed'
        rows = [[self.button(user_id, 'Probe agent', 'probe_node', node_key)]]
        rows.append([self.button(user_id, 'Settings', 'node_settings', node_key),
                     self.button(user_id, 'Maintenance', 'node_maintenance', node_key)])
        rows.append([self.button(user_id, 'Set up local agent', 'rollout_local', node_key)])
        rows.append([self.button(user_id, 'Set up SSH agent', 'rollout_ssh', node_key)])
        rows.append([self.button(user_id, 'Updates', 'node_updates', node_key)])
        if node['desired_revision'] != node['applied_revision']:
            rows.append([self.button(user_id, 'Apply settings', 'apply_node', node_key)])
        rows.append([self.button(user_id, 'Back', 'admin_nodes')])
        await self.render(chat_id, user_id, Screen(node['title'],
            (f"State: {state}", f"Protocols: {', '.join(node['protocols']) or 'none'}",
             f"Revision: {node['applied_revision']} / {node['desired_revision']}")),
            rows, message_id)

    async def node_settings(self, chat_id: int, user_id: int, message_id: int,
                            node_key: str) -> None:
        self.node_edit_drafts.pop(user_id, None)
        node = await self.backend.request('GET', f'/api/v1/nodes/{node_key}',
                                          telegram_user_id=user_id)
        settings = node['settings']
        fields = [('title', 'Name'), ('region', 'Region'), ('flag', 'Flag'),
                  ('public_host', 'Public host'), ('awg_port', 'AWG port'),
                  ('xray_sni', 'Xray SNI'), ('xray_tcp_port', 'TCP port'),
                  ('xray_xhttp_port', 'XHTTP port'),
                  ('xray_xhttp_path', 'XHTTP path')]
        rows = [[self.button(user_id, label, 'edit_node_field', node_key, field)]
                for field, label in fields if field in {'title', 'region', 'flag', 'public_host'}
                or (field.startswith('awg_') and 'awg' in node['protocols'])
                or (field.startswith('xray_') and 'xray' in node['protocols'])]
        rows += [[self.button(user_id, 'Protocols', 'node_protocols', node_key)],
                 [self.button(user_id, 'Back', 'admin_node', node_key)]]
        details = (f"Name: {node['title']}", f"Region: {node['region']}",
                   f"Flag: {node['flag'] or 'none'}",
                   *(f'{key}: {value}' for key, value in sorted(settings.items())))
        await self.render(chat_id, user_id, Screen('Node settings',
            (f"Desired revision: {node['desired_revision']}",
             f"Applied revision: {node['applied_revision']}",
             'Save fields here, then apply on the node card.'),
            'Current values', details), rows, message_id)

    async def edit_node_field(self, chat_id: int, user_id: int, message_id: int,
                              node_key: str, field: str) -> None:
        allowed = {'title', 'region', 'flag', 'public_host', 'awg_port',
                   'xray_sni', 'xray_tcp_port', 'xray_xhttp_port', 'xray_xhttp_path'}
        if field not in allowed:
            await self.node_settings(chat_id, user_id, message_id, node_key)
            return
        node = await self.backend.request('GET', f'/api/v1/nodes/{node_key}',
                                          telegram_user_id=user_id)
        self.node_edit_drafts[user_id] = NodeEditDraft(node_key, field, message_id,
            node['desired_revision'], node['settings'], str(uuid4()), time.monotonic() + 600)
        current = node.get(field) if field in {'title', 'region', 'flag'} else node['settings'].get(field)
        await self.render(chat_id, user_id, Screen('Edit ' + field.replace('_', ' '),
            (f"Current: {current or 'not set'}", 'Send the new value as a message.',
             'It will take effect only after Apply settings.')),
            [[self.button(user_id, 'Cancel', 'node_settings', node_key)]], message_id)

    async def submit_node_edit(self, chat_id: int, user_id: int,
                               draft: NodeEditDraft, value: str) -> None:
        if draft.submitted_value is not None and draft.submitted_value != value:
            await self.render(chat_id, user_id, Screen('Edit pending',
                ('Retry the same value or cancel before changing it.')),
                [[self.button(user_id, 'Cancel', 'node_settings', draft.node_key)]],
                draft.message_id)
            return
        draft.submitted_value = value
        if draft.field.endswith('_port'):
            if not value.isdecimal() or not 1 <= int(value) <= 65535:
                await self.render(chat_id, user_id, Screen('Invalid port',
                    ('Enter a number from 1 to 65535.',)),
                    [[self.button(user_id, 'Cancel', 'node_settings', draft.node_key)]],
                    draft.message_id)
                draft.submitted_value = None
                return
            parsed: str | int = int(value)
        else:
            parsed = value
        body = ({draft.field: parsed} if draft.field in {'title', 'region', 'flag'} else
                {'settings': {**draft.settings, draft.field: parsed}})
        try:
            await self.backend.edit_node(user_id, draft.node_key, draft.revision,
                body, draft.command_key)
        except BackendError as exc:
            if exc.code != 'backend_unavailable':
                self.node_edit_drafts.pop(user_id, None)
            await self.render(chat_id, user_id, Screen('Could not save setting',
                (f'Reason: {exc.code}', 'Retry the same value.' if exc.code ==
                 'backend_unavailable' else 'Refresh settings and try again.')),
                [[self.button(user_id, 'Cancel', 'node_settings', draft.node_key)]],
                draft.message_id)
            return
        self.node_edit_drafts.pop(user_id, None)
        await self.node_settings(chat_id, user_id, draft.message_id, draft.node_key)

    async def node_protocols(self, chat_id: int, user_id: int, message_id: int,
                             node_key: str) -> None:
        node = await self.backend.request('GET', f'/api/v1/nodes/{node_key}',
                                          telegram_user_id=user_id)
        enabled = set(node['protocols'])
        transports = set(node['xray_transports'])
        rows = [[self.button(user_id, ('✓ ' if kind in enabled else '+ ') + kind.upper(),
                             'toggle_node_protocol', node_key, kind)] for kind in ('awg', 'xray')]
        if 'xray' in enabled:
            rows += [[self.button(user_id, ('✓ ' if kind in transports else '+ ') + kind.upper(),
                                  'toggle_node_transport', node_key, kind)]
                     for kind in ('tcp', 'xhttp')]
        rows.append([self.button(user_id, 'Back', 'node_settings', node_key)])
        await self.render(chat_id, user_id, Screen('Node protocols',
            ('Changes are saved as desired state. Apply them on the node card.',
             'Removing a protocol in use by profiles is rejected.')),
            rows, message_id)

    async def toggle_node_feature(self, chat_id: int, user_id: int, message_id: int,
                                  node_key: str, kind: str, *, transport: bool) -> None:
        if kind not in ({'tcp', 'xhttp'} if transport else {'awg', 'xray'}):
            await self.node_protocols(chat_id, user_id, message_id, node_key)
            return
        node = await self.backend.request('GET', f'/api/v1/nodes/{node_key}',
                                          telegram_user_id=user_id)
        protocols = set(node['protocols'])
        transports = set(node['xray_transports'])
        selected = transports if transport else protocols
        selected.symmetric_difference_update({kind})
        if not protocols:
            await self.render(chat_id, user_id, Screen('Protocol required',
                ('Keep at least one VPN protocol enabled.',)),
                [[self.button(user_id, 'Back', 'node_protocols', node_key)]], message_id)
            return
        if 'xray' not in protocols:
            transports.clear()
        elif not transports:
            transports.update({'tcp', 'xhttp'} if not transport else {'tcp'})
        body = {'protocols': sorted(protocols), 'xray_transports': sorted(transports)}
        settings = dict(node['settings'])
        if 'awg' in protocols:
            settings.setdefault('awg_port', 51820)
        if 'xray' in protocols:
            for field, default in {'xray_sni': 'www.cloudflare.com',
                                   'xray_tcp_port': 443, 'xray_xhttp_port': 8443,
                                   'xray_xhttp_path': '/assets'}.items():
                settings.setdefault(field, default)
        body['settings'] = settings
        await self.backend.edit_node(user_id, node_key, node['desired_revision'],
                                     body, str(uuid4()))
        await self.node_protocols(chat_id, user_id, message_id, node_key)

    async def node_maintenance(self, chat_id: int, user_id: int,
                               message_id: int, node_key: str) -> None:
        self.maintenance_drafts.pop(user_id, None)
        state = await self.backend.node_maintenance(user_id, node_key)
        lines = [f"State: {state['status']}"]
        target = state.get('verification_target')
        lines.append(f"Verification target: {target or 'not bound'}")
        if state['status'] == 'draining':
            lines.extend((f"Revocations pending: {state['pending_tasks']}",
                f"Revocations blocked: {state['blocked_tasks']}",
                f"Runtime cleanup: {state['cleanup_phase'] or 'not started'}"))
        rows = [[self.button(user_id, 'Refresh', 'node_maintenance', node_key)]]
        if state['status'] == 'active':
            if not target:
                rows += [[self.button(user_id, 'Bind local host', 'bind_local', node_key)],
                         [self.button(user_id, 'Bind SSH host', 'bind_ssh', node_key)]]
            else:
                rows.append([self.button(user_id, 'Start full cleanup',
                                         'confirm_node_drain', node_key)])
        elif state['revocations_complete']:
            if state['cleanup_phase'] in {'uninstall_uncertain', 'uninstall_scheduled'}:
                rows.append([self.button(user_id, 'Verify and remove node',
                                         'verify_retirement', node_key)])
            else:
                rows.append([self.button(user_id, 'Next cleanup step',
                    'cleanup_step', node_key, state['cleanup_phase'] or 'not_started')])
        rows += [[self.button(user_id, 'Remove from bot only',
                             'confirm_registry_removal', node_key)],
                 [self.button(user_id, 'Back', 'admin_node', node_key)]]
        await self.render(chat_id, user_id, Screen('Node maintenance', tuple(lines),
            'Full cleanup',
            ('Bind the host before draining. SSH binding requires root access and a pinned host key.',
             'After uninstall, remote verification needs a separate root SSH key on the controller.',
             'Registry-only removal leaves runtime artifacts on the VPS.')),
            rows, message_id)

    async def bind_ssh(self, chat_id: int, user_id: int,
                       message_id: int, node_key: str) -> None:
        self.maintenance_drafts[user_id] = MaintenanceDraft(node_key, message_id,
                                                            time.monotonic() + 600)
        await self.render(chat_id, user_id, Screen('Bind SSH verification host',
            ('Send root@host for the node. The host key must already be pinned on the controller.',
             'Port 22 is currently supported in this screen.')),
            [[self.button(user_id, 'Cancel', 'node_maintenance', node_key)]], message_id)

    async def bind_local(self, chat_id: int, user_id: int,
                         message_id: int, node_key: str) -> None:
        await self.backend.bind_verification_target(user_id, node_key, 'local')
        await self.node_maintenance(chat_id, user_id, message_id, node_key)

    async def confirm_node_drain(self, chat_id: int, user_id: int,
                                 message_id: int, node_key: str) -> None:
        await self.render(chat_id, user_id, Screen('Start full cleanup',
            (f'Node: {node_key}', 'All profile grants for this node will be revoked.',
             'Cleanup then removes protocol containers, configs, and the agent.',
             'The node record is removed only after independent host verification.')),
            [[self.button(user_id, 'Start drain', 'drain_node', node_key)],
             [self.button(user_id, 'Cancel', 'node_maintenance', node_key)]], message_id)

    async def drain_node(self, chat_id: int, user_id: int,
                         message_id: int, node_key: str) -> None:
        await self.backend.drain_node(user_id, node_key)
        await self.node_maintenance(chat_id, user_id, message_id, node_key)

    async def cleanup_step(self, chat_id: int, user_id: int,
                           message_id: int, node_key: str,
                           expected_phase: str) -> None:
        await self.backend.cleanup_node_step(user_id, node_key, expected_phase)
        await self.node_maintenance(chat_id, user_id, message_id, node_key)

    async def verify_retirement(self, chat_id: int, user_id: int,
                                message_id: int, node_key: str) -> None:
        await self.backend.verify_and_retire_node(user_id, node_key)
        await self.render(chat_id, user_id, Screen('Node removed',
            ('The host was verified clean and the node record was retired.',)),
            [[self.button(user_id, 'Nodes', 'admin_nodes')]], message_id)

    async def confirm_registry_removal(self, chat_id: int, user_id: int,
                                       message_id: int, node_key: str) -> None:
        await self.render(chat_id, user_id, Screen('Remove from bot only',
            (f'Node: {node_key}', 'This removes its access grants and backend record.',
             'The node runtime, containers, agent, and SSH keys may remain.',
             'Use only when the VPS cannot be verified or cleaned.')),
            [[self.button(user_id, 'Confirm registry removal', 'retire_registry', node_key)],
             [self.button(user_id, 'Cancel', 'node_maintenance', node_key)]], message_id)

    async def retire_registry(self, chat_id: int, user_id: int,
                              message_id: int, node_key: str) -> None:
        result = await self.backend.retire_node_registry_only(user_id, node_key)
        await self.render(chat_id, user_id, Screen('Node removed from bot',
            (f"Node: {result['node_key']}",
             'Remote runtime was not verified or removed.')),
            [[self.button(user_id, 'Nodes', 'admin_nodes')]], message_id)

    async def updates(self, chat_id: int, user_id: int, message_id: int) -> None:
        page = await self.backend.admin_nodes(user_id)
        rows = [[self.button(user_id, node['title'], 'node_updates', node['key'])]
                for node in page['items']]
        rows.append([self.button(user_id, 'Back', 'home')])
        await self.render(chat_id, user_id, Screen('Updates',
            ('Select a node to compare its runtime with this release.',
             'Controller updates are installed from the controller host.')),
            rows, message_id)

    async def node_updates(self, chat_id: int, user_id: int, message_id: int,
                           node_key: str) -> None:
        expected = (Path(__file__).resolve().parents[2] / 'VERSION').read_text().strip()
        try:
            runtime = await self.backend.node_runtime(user_id, node_key)
            installed = runtime['runtime_version'] or 'unknown'
        except BackendError as exc:
            installed = 'unreachable'
            error = exc.code
        else:
            error = None
        lines = (f'Current release: {expected}', f'Node runtime: {installed}')
        if error:
            lines += (f'Agent status: {error}',)
        elif installed != expected:
            lines += ('Agent/runtime setup may be needed.',)
        else:
            lines += ('Runtime version matches this release.',)
        rows = []
        if installed != expected:
            if installed == 'unreachable':
                rows += [[self.button(user_id, 'Set up local agent', 'rollout_local', node_key)],
                         [self.button(user_id, 'Set up SSH agent', 'rollout_ssh', node_key)]]
            else:
                rows.append([self.button(user_id, 'Refresh runtime',
                                         'refresh_runtime', node_key)])
        rows.append([self.button(user_id, 'Back', 'admin_node', node_key)])
        await self.render(chat_id, user_id, Screen('Node updates', lines), rows, message_id)

    async def refresh_runtime(self, chat_id: int, user_id: int,
                              message_id: int, node_key: str) -> None:
        draft = self.runtime_refresh_drafts.get(user_id)
        if draft is None or draft.node_key != node_key or draft.expires_at < time.monotonic():
            node = await self.backend.request('GET', f'/api/v1/nodes/{node_key}',
                                              telegram_user_id=user_id)
            draft = RuntimeRefreshDraft(node_key, node['desired_revision'],
                node['settings'], str(uuid4()), str(uuid4()), time.monotonic() + 600)
            self.runtime_refresh_drafts[user_id] = draft
        # A fresh desired revision asks the settings worker to stage the
        # current release bundle even when public settings did not change.
        updated = await self.backend.edit_node(user_id, node_key, draft.revision,
            {'settings': draft.settings}, draft.edit_key)
        confirmed = await self.apply_node(chat_id, user_id, message_id,
            updated['key'], revision=updated['desired_revision'], command_key=draft.apply_key)
        if confirmed:
            self.runtime_refresh_drafts.pop(user_id, None)

    async def node_protocol_choice(self, chat_id: int, user_id: int,
                                   draft: NodeDraft) -> None:
        rows = [[self.button(user_id, 'AWG + Xray', 'submit_node', 'both')],
                [self.button(user_id, 'AWG only', 'submit_node', 'awg'),
                 self.button(user_id, 'Xray only', 'submit_node', 'xray')],
                [self.button(user_id, 'Cancel', 'admin_nodes')]]
        await self.render(chat_id, user_id, Screen('Choose protocols',
            ('Default ports: AWG 51820, Xray TCP 443, XHTTP 8443.',
             'Xray SNI defaults to www.cloudflare.com. Settings can be edited later.')),
            rows, draft.message_id)

    async def submit_node(self, chat_id: int, user_id: int, message_id: int,
                          choice: str) -> None:
        draft = self.node_drafts.get(user_id)
        if draft is None or draft.base is None or draft.expires_at < time.monotonic():
            await self.admin_nodes(chat_id, user_id, message_id)
            return
        if draft.submitted_protocols is not None and draft.submitted_protocols != choice:
            await self.node_protocol_choice(chat_id, user_id, draft)
            return
        draft.submitted_protocols = choice
        protocols = ['awg', 'xray'] if choice == 'both' else [choice]
        settings = {'public_host': draft.base['public_host']}
        if 'awg' in protocols:
            settings['awg_port'] = 51820
        if 'xray' in protocols:
            settings.update({'xray_sni': 'www.cloudflare.com', 'xray_tcp_port': 443,
                'xray_xhttp_port': 8443, 'xray_xhttp_path': '/assets'})
        body = {key: draft.base[key] for key in ('key', 'title', 'region')}
        body.update({'protocols': protocols,
            'xray_transports': ['tcp', 'xhttp'] if 'xray' in protocols else [],
            'settings': settings})
        try:
            node = await self.backend.create_node(user_id, body, draft.command_key)
        except BackendError as exc:
            await self.render(chat_id, user_id, Screen('Could not register node',
                (f'Reason: {exc.code}', 'Retry this selection or cancel.')),
                [[self.button(user_id, 'Retry', 'submit_node', choice)],
                 [self.button(user_id, 'Cancel', 'admin_nodes')]], message_id)
            return
        self.node_drafts.pop(user_id, None)
        await self.admin_node(chat_id, user_id, message_id, node['key'])

    async def queue_rollout(self, chat_id: int, user_id: int, message_id: int,
                            node_key: str, transport: str, command_key: str,
                            ssh_target: str | None = None) -> None:
        task = await self.backend.rollout_agent(user_id, node_key, transport,
            ssh_target=ssh_target, command_key=command_key)
        await self.rollout_status(chat_id, user_id, message_id, task['id'])
        self.agent_drafts.pop(user_id, None)

    async def retry_rollout(self, chat_id: int, user_id: int,
                            message_id: int) -> None:
        draft = self.agent_drafts.get(user_id)
        if draft is None or draft.expires_at < time.monotonic():
            await self.admin_nodes(chat_id, user_id, message_id)
            return
        transport = 'ssh' if draft.ssh_target else 'local'
        try:
            await self.queue_rollout(chat_id, user_id, message_id, draft.node_key,
                transport, draft.command_key, draft.ssh_target)
        except BackendError as exc:
            await self.render(chat_id, user_id,
                Screen('Could not queue agent setup',
                    (f'Reason: {exc.code}', 'Retry the same request or cancel.')),
                [[self.button(user_id, 'Retry', 'retry_rollout')],
                 [self.button(user_id, 'Cancel', 'admin_node', draft.node_key)]],
                message_id)

    async def rollout_status(self, chat_id: int, user_id: int, message_id: int,
                             task_id: str) -> None:
        task = await self.backend.agent_rollout(user_id, task_id)
        if task['status'] == 'succeeded':
            lines = ('Agent installed and reachable through the driver.',
                     'Apply node settings to deploy VPN protocols.')
        elif task['status'] == 'blocked':
            lines = ('Agent setup did not finish or its result is uncertain.',
                     'Inspect the backend worker and node before retrying.')
        else:
            lines = ('Agent setup is running in the backend worker.',
                     'Refresh to see the confirmed result.')
        rows = [] if task['status'] in {'succeeded', 'blocked'} else [
            [self.button(user_id, 'Refresh', 'rollout_status', task_id)]]
        if task['status'] == 'succeeded':
            rows.append([self.button(user_id, 'Refresh runtime',
                                     'refresh_runtime', task['node_key'])])
        rows.append([self.button(user_id, 'Node card', 'admin_node', task['node_key'])])
        await self.render(chat_id, user_id, Screen('Agent setup', lines), rows, message_id)

    async def probe_node(self, chat_id: int, user_id: int, message_id: int,
                         node_key: str) -> None:
        observation = await self.backend.node_runtime(user_id, node_key)
        await self.render(chat_id, user_id, Screen(f'Node {node_key}',
            (f"Agent: {observation['health_state']}",
             f"Xray config: {'present' if observation['xray_config_present'] else 'missing'}",
             f"AWG config: {'present' if observation['awg_config_present'] else 'missing'}")),
            [[self.button(user_id, 'Back', 'admin_node', node_key)]], message_id)

    async def apply_node(self, chat_id: int, user_id: int, message_id: int,
                         node_key: str, *, revision: int | None = None,
                         command_key: str | None = None) -> bool:
        if revision is None:
            node = await self.backend.request('GET', f'/api/v1/nodes/{node_key}',
                                              telegram_user_id=user_id)
            revision = node['desired_revision']
        if command_key is None:
            operation = await self.backend.apply_node_settings(user_id, node_key, revision)
        else:
            operation = await self.backend.apply_node_settings(user_id, node_key,
                revision, command_key)
        await self.render(chat_id, user_id, Screen('Applying node settings',
            ('The backend worker is verifying the node.',)),
            [[self.button(user_id, 'Back', 'admin_node', node_key)]], message_id)
        for _ in range(30):
            state = await self.backend.node_settings_operation(user_id, operation['id'])
            if state['status'] == 'succeeded':
                await self.admin_node(chat_id, user_id, message_id, node_key)
                return True
            if state['status'] in {'blocked', 'superseded'}:
                break
            await asyncio.sleep(1)
        await self.node_apply_status(chat_id, user_id, message_id,
                                     operation['id'], node_key)
        return False

    async def node_apply_status(self, chat_id: int, user_id: int, message_id: int,
                                operation_id: str, node_key: str) -> None:
        state = await self.backend.node_settings_operation(user_id, operation_id)
        if state['status'] == 'succeeded':
            draft = self.runtime_refresh_drafts.get(user_id)
            if draft is not None and draft.node_key == node_key:
                self.runtime_refresh_drafts.pop(user_id, None)
            await self.admin_node(chat_id, user_id, message_id, node_key)
            return
        rows = [[self.button(user_id, 'Back', 'admin_node', node_key)]]
        if state['status'] in {'blocked', 'superseded'}:
            await self.render(chat_id, user_id, Screen('Node needs attention',
                ('Settings were not confirmed. Check the agent and operation state.',)),
                rows, message_id)
            return
        rows.insert(0, [self.button(user_id, 'Refresh', 'node_apply_status',
            operation_id, node_key)])
        await self.render(chat_id, user_id, Screen('Still applying settings',
            ('The backend is working. Refresh to check the same operation.',)),
            rows, message_id)

    async def grant_protocols(self, chat_id: int, user_id: int, message_id: int,
                              profile_id: str, node_key: str) -> None:
        node = await self.backend.request('GET', f'/api/v1/nodes/{node_key}',
                                          telegram_user_id=user_id)
        rows = [[self.button(user_id, protocol.upper(), 'add_grant',
                             profile_id, node_key, protocol)] for protocol in node['protocols']]
        rows.append([self.button(user_id, 'Back', 'grant_nodes', profile_id)])
        await self.render(chat_id, user_id, Screen(node['title'],
            ('Choose a protocol to grant.',)), rows, message_id)

    async def change_grant(self, user_id: int, profile_id: str, node_key: str,
                           protocol: str, *, add: bool) -> None:
        profile = await self.backend.request('GET', f'/api/v1/profiles/{profile_id}',
                                             telegram_user_id=user_id)
        grants = (await self.backend.profile_grants(user_id, profile_id))['items']
        target = {'node_key': node_key, 'protocol': protocol}
        if add and target not in grants:
            grants.append(target)
        elif not add:
            grants = [grant for grant in grants if grant != target]
        await self.backend.replace_grants(user_id, profile_id,
            profile['desired_revision'], grants)

    async def text_input(self, message: Message) -> None:
        if message.from_user is None or message.chat.type != 'private':
            return
        user_id = message.from_user.id
        edit_draft = self.node_edit_drafts.get(user_id)
        if edit_draft is not None:
            if edit_draft.expires_at >= time.monotonic():
                await self.submit_node_edit(message.chat.id, user_id, edit_draft,
                    (message.text or '').strip())
                return
            self.node_edit_drafts.pop(user_id, None)
        maintenance_draft = self.maintenance_drafts.get(user_id)
        if maintenance_draft is not None:
            if maintenance_draft.expires_at < time.monotonic():
                self.maintenance_drafts.pop(user_id, None)
            else:
                target = (message.text or '').strip()
                if not re.fullmatch(r'root@[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]', target):
                    await self.bind_ssh(message.chat.id, user_id,
                                        maintenance_draft.message_id,
                                        maintenance_draft.node_key)
                    return
                try:
                    await self.backend.bind_verification_target(user_id,
                        maintenance_draft.node_key, 'ssh', target)
                except BackendError as exc:
                    await self.render(message.chat.id, user_id,
                        Screen('Host verification failed',
                            (f'Reason: {exc.code}', 'Check SSH access and retry.')),
                        [[self.button(user_id, 'Cancel', 'node_maintenance',
                                      maintenance_draft.node_key)]],
                        maintenance_draft.message_id)
                    return
                self.maintenance_drafts.pop(user_id, None)
                await self.node_maintenance(message.chat.id, user_id,
                    maintenance_draft.message_id, maintenance_draft.node_key)
                return
        node_draft = self.node_drafts.get(user_id)
        if node_draft is not None and node_draft.expires_at >= time.monotonic():
            if node_draft.base is None:
                fields = [part.strip() for part in (message.text or '').split('|')]
                if (len(fields) != 4 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', fields[0])
                    or not fields[1] or not fields[2] or not re.fullmatch(
                        r'[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]', fields[3])):
                    await self.render(message.chat.id, user_id, Screen('New node',
                        ('Send: key | name | region | public host',
                         'Example: lv1 | Latvia #1 | EU | lv1.example.com')),
                        [[self.button(user_id, 'Cancel', 'admin_nodes')]], node_draft.message_id)
                    return
                node_draft.base = dict(zip(('key', 'title', 'region', 'public_host'), fields))
            await self.node_protocol_choice(message.chat.id, user_id, node_draft)
            return
        agent_draft = self.agent_drafts.get(user_id)
        if agent_draft is not None and agent_draft.expires_at >= time.monotonic():
            target = (message.text or '').strip()
            if not re.fullmatch(r'(?:[A-Za-z_][A-Za-z0-9._-]*@)?[A-Za-z0-9.-]+', target):
                await self.render(message.chat.id, user_id, Screen('SSH agent setup',
                    ('Send an SSH target such as root@lv1.example.com.',)),
                    [[self.button(user_id, 'Cancel', 'admin_node', agent_draft.node_key)]],
                    agent_draft.message_id)
                return
            if agent_draft.ssh_target is not None and agent_draft.ssh_target != target:
                await self.render(message.chat.id, user_id, Screen('SSH setup pending',
                    ('Retry the same target after a connection error, or cancel.',)),
                    [[self.button(user_id, 'Cancel', 'admin_node', agent_draft.node_key)]],
                    agent_draft.message_id)
                return
            agent_draft.ssh_target = target
            await self.retry_rollout(message.chat.id, user_id, agent_draft.message_id)
            return
        draft = self.drafts.get(user_id)
        if draft is None or draft.expires_at < time.monotonic() or not message.text:
            self.drafts.pop(user_id, None)
            return
        name = message.text.strip()
        if not name or len(name) > 128:
            await self.render(message.chat.id, user_id,
                Screen('Profile name', ('Enter a name from 1 to 128 characters.',)),
                [[self.button(user_id, 'Cancel', 'accounts')]], draft.message_id)
            return
        if draft.submitted_name is not None and name != draft.submitted_name:
            await self.render(message.chat.id, user_id,
                Screen('Profile creation pending',
                    ('Retry the same name after the connection error, or cancel.')),
                [[self.button(user_id, 'Cancel', 'accounts')]], draft.message_id)
            return
        self.drafts[user_id] = Draft(draft.account_id, draft.message_id,
            draft.expires_at, draft.command_key, name)
        try:
            result = await self.backend.create_profile(user_id, draft.account_id,
                name, draft.command_key)
        except BackendError as exc:
            if exc.code != 'backend_unavailable':
                self.drafts[user_id] = Draft(draft.account_id, draft.message_id,
                    draft.expires_at, str(uuid4()))
            await self.render(message.chat.id, user_id,
                Screen('Could not create profile', (f'Reason: {exc.code}',
                    'Retry the same name.' if exc.code == 'backend_unavailable' else 'Try another name.')),
                [[self.button(user_id, 'Cancel', 'accounts')]], draft.message_id)
            return
        self.drafts.pop(user_id, None)
        await self.admin_profile(message.chat.id, user_id, draft.message_id,
                                 result['profile']['id'])

    async def start(self, message: Message) -> None:
        if message.from_user is None or message.chat.type != 'private':
            return
        try:
            await self.backend.resolve(message.from_user.id)
            await self.home(message.chat.id, message.from_user.id)
        except BackendError:
            await self.render(message.chat.id, message.from_user.id,
                Screen('Node Plane', ('The service is temporarily unavailable. Try /start again.',)), [])

    async def callback(self, query: CallbackQuery) -> None:
        if query.message is None or query.from_user is None:
            return
        token = (query.data or '')[3:]
        action = self.actions.get(token)
        if action is None or action.owner_id != query.from_user.id or action.expires_at < time.monotonic():
            await query.answer('This screen expired. Send /start to refresh it.', show_alert=True)
            return
        self.actions.pop(token, None)
        await query.answer()
        chat_id, message_id, user_id = query.message.chat.id, query.message.message_id, query.from_user.id
        try:
            if action.name == 'home':
                await self.home(chat_id, user_id, message_id)
            elif action.name == 'profiles':
                await self.profiles(chat_id, user_id, message_id)
            elif action.name == 'profile':
                await self.profile(chat_id, user_id, message_id, *action.args)
            elif action.name == 'node':
                await self.node(chat_id, user_id, message_id, *action.args)
            elif action.name == 'issue':
                await self.issue(chat_id, user_id, message_id, *action.args)
            elif action.name == 'show_qr':
                await self.show_qr(chat_id, user_id, message_id, *action.args)
            elif action.name == 'issuance_status':
                await self.issuance_status(chat_id, user_id, message_id, *action.args)
            elif action.name == 'request_access':
                request = await self.backend.request_access(user_id)
                await self.home(chat_id, user_id, message_id)
                await self.notify_admins(request['id'])
            elif action.name == 'requests':
                await self.requests(chat_id, user_id, message_id)
            elif action.name == 'review':
                await self.review(chat_id, user_id, message_id, *action.args)
            elif action.name == 'decide':
                result = await self.backend.request('POST',
                    f'/api/v1/access-requests/{action.args[0]}/decision',
                    telegram_user_id=user_id, command=True, body={'decision': action.args[1]})
                await self.requests(chat_id, user_id, message_id)
                await self.notify_requester(user_id, result['id'],
                    result['account_id'], action.args[1])
            elif action.name == 'notification_review':
                await self.review(chat_id, user_id,
                    self.control_messages.get(chat_id, message_id), action.args[0])
            elif action.name == 'accounts':
                await self.accounts(chat_id, user_id, message_id)
            elif action.name == 'account':
                await self.account(chat_id, user_id, message_id, *action.args)
            elif action.name == 'new_profile':
                self.drafts[user_id] = Draft(action.args[0], message_id,
                                             time.monotonic() + 600, str(uuid4()))
                await self.render(chat_id, user_id,
                    Screen('New VPN profile', ('Send the profile name as a message.',)),
                    [[self.button(user_id, 'Cancel', 'accounts')]], message_id)
            elif action.name == 'admin_profiles':
                await self.admin_profiles(chat_id, user_id, message_id)
            elif action.name == 'admin_profile':
                await self.admin_profile(chat_id, user_id, message_id, *action.args)
            elif action.name == 'admin_nodes':
                await self.admin_nodes(chat_id, user_id, message_id)
            elif action.name == 'new_node':
                self.node_drafts[user_id] = NodeDraft(message_id, str(uuid4()),
                                                       time.monotonic() + 600)
                await self.render(chat_id, user_id, Screen('New node',
                    ('Send: key | name | region | public host',
                     'Example: lv1 | Latvia #1 | EU | lv1.example.com')),
                    [[self.button(user_id, 'Cancel', 'admin_nodes')]], message_id)
            elif action.name == 'submit_node':
                await self.submit_node(chat_id, user_id, message_id, *action.args)
            elif action.name == 'admin_node':
                await self.admin_node(chat_id, user_id, message_id, *action.args)
            elif action.name == 'node_settings':
                await self.node_settings(chat_id, user_id, message_id, *action.args)
            elif action.name == 'edit_node_field':
                await self.edit_node_field(chat_id, user_id, message_id, *action.args)
            elif action.name == 'node_protocols':
                await self.node_protocols(chat_id, user_id, message_id, *action.args)
            elif action.name == 'node_maintenance':
                await self.node_maintenance(chat_id, user_id, message_id, *action.args)
            elif action.name == 'bind_local':
                await self.bind_local(chat_id, user_id, message_id, *action.args)
            elif action.name == 'bind_ssh':
                await self.bind_ssh(chat_id, user_id, message_id, *action.args)
            elif action.name == 'confirm_node_drain':
                await self.confirm_node_drain(chat_id, user_id, message_id, *action.args)
            elif action.name == 'drain_node':
                await self.drain_node(chat_id, user_id, message_id, *action.args)
            elif action.name == 'cleanup_step':
                await self.cleanup_step(chat_id, user_id, message_id, *action.args)
            elif action.name == 'verify_retirement':
                await self.verify_retirement(chat_id, user_id, message_id, *action.args)
            elif action.name == 'confirm_registry_removal':
                await self.confirm_registry_removal(chat_id, user_id, message_id, *action.args)
            elif action.name == 'retire_registry':
                await self.retire_registry(chat_id, user_id, message_id, *action.args)
            elif action.name == 'updates':
                await self.updates(chat_id, user_id, message_id)
            elif action.name == 'node_updates':
                await self.node_updates(chat_id, user_id, message_id, *action.args)
            elif action.name == 'refresh_runtime':
                await self.refresh_runtime(chat_id, user_id, message_id, *action.args)
            elif action.name == 'toggle_node_protocol':
                await self.toggle_node_feature(chat_id, user_id, message_id,
                    *action.args, transport=False)
            elif action.name == 'toggle_node_transport':
                await self.toggle_node_feature(chat_id, user_id, message_id,
                    *action.args, transport=True)
            elif action.name == 'rollout_local':
                self.agent_drafts[user_id] = AgentDraft(action.args[0], message_id,
                    str(uuid4()), time.monotonic() + 600)
                await self.retry_rollout(chat_id, user_id, message_id)
            elif action.name == 'rollout_ssh':
                self.agent_drafts[user_id] = AgentDraft(action.args[0], message_id,
                    str(uuid4()), time.monotonic() + 600)
                await self.render(chat_id, user_id, Screen('SSH agent setup',
                    ('Send an SSH target such as root@lv1.example.com.',
                     'The controller uses its configured SSH key; port 22 is used.')),
                    [[self.button(user_id, 'Cancel', 'admin_node', action.args[0])]], message_id)
            elif action.name == 'rollout_status':
                await self.rollout_status(chat_id, user_id, message_id, *action.args)
            elif action.name == 'retry_rollout':
                await self.retry_rollout(chat_id, user_id, message_id)
            elif action.name == 'probe_node':
                await self.probe_node(chat_id, user_id, message_id, *action.args)
            elif action.name == 'apply_node':
                await self.apply_node(chat_id, user_id, message_id, *action.args)
            elif action.name == 'node_apply_status':
                await self.node_apply_status(chat_id, user_id, message_id, *action.args)
            elif action.name == 'grant_nodes':
                await self.grant_nodes(chat_id, user_id, message_id, *action.args)
            elif action.name == 'grant_protocols':
                await self.grant_protocols(chat_id, user_id, message_id, *action.args)
            elif action.name == 'add_grant':
                await self.change_grant(user_id, *action.args, add=True)
                await self.admin_profile(chat_id, user_id, message_id, action.args[0])
            elif action.name == 'remove_grant':
                await self.change_grant(user_id, *action.args, add=False)
                await self.admin_profile(chat_id, user_id, message_id, action.args[0])
            elif action.name == 'toggle_freeze':
                profile_id = action.args[0]
                profile = await self.backend.request('GET', f'/api/v1/profiles/{profile_id}',
                                                     telegram_user_id=user_id)
                await self.backend.edit_profile(user_id, profile_id,
                    profile['desired_revision'], {'frozen': not profile['frozen']})
                await self.admin_profile(chat_id, user_id, message_id, profile_id)
        except BackendError as exc:
            await self.render(chat_id, user_id,
                Screen('Action unavailable', (f'Reason: {exc.code}', 'Return to the current menu and try again.')),
                [[self.button(user_id, 'Home', 'home')]], message_id)


async def main() -> None:
    token = os.environ['BOT_TOKEN']
    adapter_file = Path(os.environ['NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE'])
    adapter_token = adapter_file.read_text(encoding='utf-8').strip()
    if not adapter_token:
        raise ValueError('backend adapter credential is empty')
    base_url = os.environ.get('NODE_PLANE_BACKEND_URL', 'http://127.0.0.1:8080')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with Bot(token=token) as bot:
            client = TelegramClient(bot, BackendClient(session, base_url, adapter_token))
            dispatcher = Dispatcher()
            dispatcher.include_router(client.router)
            await dispatcher.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
