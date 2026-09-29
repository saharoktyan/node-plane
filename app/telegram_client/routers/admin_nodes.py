import re
from pathlib import Path
from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.fsm.context import FSMContext

from ..backend import BackendClient, BackendError
from ..screens import Screen
from .common import render
from .states import NodeDraftState, NodeEditState, MaintenanceState, AgentDraftState
from .callbacks import (
    AdminNodesCallback, NewNodeCallback, SubmitNodeCallback, AdminNodeCallback,
    NodeSettingsCallback, EditNodeFieldCallback, NodeProtocolsCallback,
    ToggleNodeProtocolCallback, ToggleNodeTransportCallback, NodeMaintenanceCallback,
    BindLocalCallback, BindSshCallback, ConfirmNodeDrainCallback, DrainNodeCallback,
    CleanupStepCallback, VerifyRetirementCallback, ConfirmRegistryRemovalCallback,
    RetireRegistryCallback, UpdatesCallback, NodeUpdatesCallback, RefreshRuntimeCallback,
    RolloutLocalCallback, RolloutSshCallback, RolloutStatusCallback, RetryRolloutCallback,
    ProbeNodeCallback, ApplyNodeCallback, NodeApplyStatusCallback, HomeCallback
)
import asyncio

router = Router()

@router.callback_query(AdminNodesCallback.filter())
async def admin_nodes_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await show_admin_nodes(query.message.chat.id, query.from_user.id, query.message.message_id, bot, backend, state)

async def show_admin_nodes(chat_id, user_id, message_id, bot, backend, state):
    page = await backend.admin_nodes(user_id)
    rows = [[InlineKeyboardButton(text=f"{node['flag']} {node['title']}".strip(), callback_data=AdminNodeCallback(node_key=node['key']).pack())] for node in page['items']]
    rows.append([InlineKeyboardButton(text='➕ Добавить сервер', callback_data=NewNodeCallback().pack())])
    rows.append([InlineKeyboardButton(text='🔙 Назад', callback_data='admin_menu')])
    await render(bot, chat_id, Screen('Серверы', ('Выберите сервер для настройки и управления.',) if page['items'] else ('Пока нет зарегистрированных серверов.',)), rows, state, message_id)

@router.callback_query(NewNodeCallback.filter())
async def new_node_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await state.set_state(NodeDraftState.waiting_for_node_base)
    rows = [[InlineKeyboardButton(text='Cancel', callback_data=AdminNodesCallback().pack())]]
    await render(bot, query.message.chat.id, Screen('New node', ('Send: key | name | region | public host', 'Example: lv1 | Latvia #1 | EU | lv1.example.com')), rows, state, query.message.message_id)

@router.message(NodeDraftState.waiting_for_node_base, F.text)
async def process_node_base(message: Message, bot: Bot, backend: BackendClient, state: FSMContext):
    if message.from_user is None or message.chat.type != 'private': return
    user_id = message.from_user.id
    data = await state.get_data()
    message_id = data.get('control_message_id')
    fields = [part.strip() for part in (message.text or '').split('|')]
    
    rows = [[InlineKeyboardButton(text='Cancel', callback_data=AdminNodesCallback().pack())]]
    if len(fields) != 4 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', fields[0]) or not fields[1] or not fields[2] or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]', fields[3]):
        await render(bot, message.chat.id, Screen('New node', ('Send: key | name | region | public host', 'Example: lv1 | Latvia #1 | EU | lv1.example.com')), rows, state, message_id)
        return
        
    await state.update_data(node_base=dict(zip(('key', 'title', 'region', 'public_host'), fields)))
    await state.set_state(NodeDraftState.waiting_for_protocol_choice)
    await show_node_protocol_choice(message.chat.id, user_id, message_id, bot, backend, state)

async def show_node_protocol_choice(chat_id, user_id, message_id, bot, backend, state):
    rows = [[InlineKeyboardButton(text='AWG + Xray', callback_data=SubmitNodeCallback(choice='both').pack())],
            [InlineKeyboardButton(text='AWG only', callback_data=SubmitNodeCallback(choice='awg').pack()),
             InlineKeyboardButton(text='Xray only', callback_data=SubmitNodeCallback(choice='xray').pack())],
            [InlineKeyboardButton(text='Cancel', callback_data=AdminNodesCallback().pack())]]
    await render(bot, chat_id, Screen('Choose protocols', ('Default ports: AWG 51820, Xray TCP 443, XHTTP 8443.', 'Xray SNI defaults to www.cloudflare.com. Settings can be edited later.')), rows, state, message_id)

@router.callback_query(SubmitNodeCallback.filter())
async def submit_node_cb(query: CallbackQuery, callback_data: SubmitNodeCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    choice = callback_data.choice
    data = await state.get_data()
    base = data.get('node_base')
    if not base:
        await show_admin_nodes(query.message.chat.id, user_id, query.message.message_id, bot, backend, state)
        return
        
    protocols = ['awg', 'xray'] if choice == 'both' else [choice]
    settings = {'public_host': base['public_host']}
    if 'awg' in protocols: settings['awg_port'] = 51820
    if 'xray' in protocols:
        settings.update({'xray_sni': 'www.cloudflare.com', 'xray_tcp_port': 443, 'xray_xhttp_port': 8443, 'xray_xhttp_path': '/assets'})
    
    body = {key: base[key] for key in ('key', 'title', 'region')}
    body.update({'protocols': protocols, 'xray_transports': ['tcp', 'xhttp'] if 'xray' in protocols else [], 'settings': settings})
    
    try:
        node = await backend.create_node(user_id, body, command_key=None)
        await state.clear()
        await show_admin_node(query.message.chat.id, user_id, query.message.message_id, node['key'], bot, backend, state)
    except BackendError as exc:
        await render(bot, query.message.chat.id, Screen('Could not register node', (f'Reason: {exc.code}', 'Retry this selection or cancel.')),
            [[InlineKeyboardButton(text='Retry', callback_data=SubmitNodeCallback(choice=choice).pack())],
             [InlineKeyboardButton(text='Cancel', callback_data=AdminNodesCallback().pack())]], state, query.message.message_id)

@router.callback_query(AdminNodeCallback.filter())
async def admin_node_cb(query: CallbackQuery, callback_data: AdminNodeCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await show_admin_node(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

async def show_admin_node(chat_id, user_id, message_id, node_key, bot, backend, state):
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    node_state = 'Ready' if node['enabled'] and node['desired_revision'] == node['applied_revision'] else 'Setup needed'
    rows = [[InlineKeyboardButton(text='Probe agent', callback_data=ProbeNodeCallback(node_key=node_key).pack())]]
    rows.append([InlineKeyboardButton(text='Settings', callback_data=NodeSettingsCallback(node_key=node_key).pack()),
                 InlineKeyboardButton(text='Maintenance', callback_data=NodeMaintenanceCallback(node_key=node_key).pack())])
    rows.append([InlineKeyboardButton(text='Set up local agent', callback_data=RolloutLocalCallback(node_key=node_key).pack())])
    rows.append([InlineKeyboardButton(text='Set up SSH agent', callback_data=RolloutSshCallback(node_key=node_key).pack())])
    rows.append([InlineKeyboardButton(text='Updates', callback_data=NodeUpdatesCallback(node_key=node_key).pack())])
    if node['desired_revision'] != node['applied_revision']:
        rows.append([InlineKeyboardButton(text='Apply settings', callback_data=ApplyNodeCallback(node_key=node_key).pack())])
    rows.append([InlineKeyboardButton(text='🔙 Назад', callback_data=AdminNodesCallback().pack())])
    await render(bot, chat_id, Screen(node['title'], (f"State: {node_state}", f"Protocols: {', '.join(node['protocols']) or 'none'}", f"Revision: {node['applied_revision']} / {node['desired_revision']}")), rows, state, message_id)

@router.callback_query(NodeSettingsCallback.filter())
async def node_settings_cb(query: CallbackQuery, callback_data: NodeSettingsCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await show_node_settings(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

async def show_node_settings(chat_id, user_id, message_id, node_key, bot, backend, state):
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    settings = node['settings']
    fields = [('title', 'Name'), ('region', 'Region'), ('flag', 'Flag'),
              ('public_host', 'Public host'), ('awg_port', 'AWG port'),
              ('xray_sni', 'Xray SNI'), ('xray_tcp_port', 'TCP port'),
              ('xray_xhttp_port', 'XHTTP port'), ('xray_xhttp_path', 'XHTTP path')]
    
    rows = [[InlineKeyboardButton(text=label, callback_data=EditNodeFieldCallback(node_key=node_key, field=field).pack())]
            for field, label in fields if field in {'title', 'region', 'flag', 'public_host'}
            or (field.startswith('awg_') and 'awg' in node['protocols'])
            or (field.startswith('xray_') and 'xray' in node['protocols'])]
    rows += [[InlineKeyboardButton(text='Protocols', callback_data=NodeProtocolsCallback(node_key=node_key).pack())],
             [InlineKeyboardButton(text='🔙 Назад', callback_data=AdminNodeCallback(node_key=node_key).pack())]]
             
    details = (f"Name: {node['title']}", f"Region: {node['region']}", f"Flag: {node['flag'] or 'none'}", *(f'{k}: {v}' for k, v in sorted(settings.items())))
    await render(bot, chat_id, Screen('Node settings', (f"Desired revision: {node['desired_revision']}", f"Applied revision: {node['applied_revision']}", 'Save fields here, then apply on the node card.'), 'Current values', details), rows, state, message_id)

@router.callback_query(EditNodeFieldCallback.filter())
async def edit_node_field_cb(query: CallbackQuery, callback_data: EditNodeFieldCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    node_key, field = callback_data.node_key, callback_data.field
    allowed = {'title', 'region', 'flag', 'public_host', 'awg_port', 'xray_sni', 'xray_tcp_port', 'xray_xhttp_port', 'xray_xhttp_path'}
    if field not in allowed:
        await show_node_settings(query.message.chat.id, user_id, query.message.message_id, node_key, bot, backend, state)
        return
        
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    await state.set_state(NodeEditState.waiting_for_value)
    await state.update_data(edit_node_key=node_key, edit_field=field, edit_revision=node['desired_revision'], edit_settings=node['settings'])
    
    current = node.get(field) if field in {'title', 'region', 'flag'} else node['settings'].get(field)
    await render(bot, query.message.chat.id, Screen('Edit ' + field.replace('_', ' '), (f"Current: {current or 'not set'}", 'Send the new value as a message.', 'It will take effect only after Apply settings.')),
        [[InlineKeyboardButton(text='Cancel', callback_data=NodeSettingsCallback(node_key=node_key).pack())]], state, query.message.message_id)

@router.message(NodeEditState.waiting_for_value, F.text)
async def process_node_edit(message: Message, bot: Bot, backend: BackendClient, state: FSMContext):
    if message.from_user is None or message.chat.type != 'private': return
    user_id, value = message.from_user.id, message.text.strip()
    data = await state.get_data()
    node_key, field, revision, settings, message_id = data['edit_node_key'], data['edit_field'], data['edit_revision'], data['edit_settings'], data.get('control_message_id')
    
    if field.endswith('_port'):
        if not value.isdecimal() or not 1 <= int(value) <= 65535:
            await render(bot, message.chat.id, Screen('Invalid port', ('Enter a number from 1 to 65535.',)), [[InlineKeyboardButton(text='Cancel', callback_data=NodeSettingsCallback(node_key=node_key).pack())]], state, message_id)
            return
        parsed = int(value)
    else: parsed = value
    
    body = ({field: parsed} if field in {'title', 'region', 'flag'} else {'settings': {**settings, field: parsed}})
    try:
        await backend.edit_node(user_id, node_key, revision, body, command_key=None)
        await state.clear()
        await show_node_settings(message.chat.id, user_id, message_id, node_key, bot, backend, state)
    except BackendError as exc:
        await render(bot, message.chat.id, Screen('Could not save setting', (f'Reason: {exc.code}', 'Refresh settings and try again.')), [[InlineKeyboardButton(text='Cancel', callback_data=NodeSettingsCallback(node_key=node_key).pack())]], state, message_id)

@router.callback_query(NodeProtocolsCallback.filter())
async def node_protocols_cb(query: CallbackQuery, callback_data: NodeProtocolsCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await show_node_protocols(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

async def show_node_protocols(chat_id, user_id, message_id, node_key, bot, backend, state):
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    enabled, transports = set(node['protocols']), set(node['xray_transports'])
    rows = [[InlineKeyboardButton(text=('✓ ' if kind in enabled else '+ ') + kind.upper(), callback_data=ToggleNodeProtocolCallback(node_key=node_key, kind=kind).pack())] for kind in ('awg', 'xray')]
    if 'xray' in enabled:
        rows += [[InlineKeyboardButton(text=('✓ ' if kind in transports else '+ ') + kind.upper(), callback_data=ToggleNodeTransportCallback(node_key=node_key, kind=kind).pack())] for kind in ('tcp', 'xhttp')]
    rows.append([InlineKeyboardButton(text='🔙 Назад', callback_data=NodeSettingsCallback(node_key=node_key).pack())])
    await render(bot, chat_id, Screen('Node protocols', ('Changes are saved as desired state. Apply them on the node card.', 'Removing a protocol in use by profiles is rejected.')), rows, state, message_id)

@router.callback_query(ToggleNodeProtocolCallback.filter())
async def toggle_protocol_cb(query: CallbackQuery, callback_data: ToggleNodeProtocolCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await toggle_node_feature(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, callback_data.kind, False, bot, backend, state)

@router.callback_query(ToggleNodeTransportCallback.filter())
async def toggle_transport_cb(query: CallbackQuery, callback_data: ToggleNodeTransportCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await toggle_node_feature(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, callback_data.kind, True, bot, backend, state)

async def toggle_node_feature(chat_id, user_id, message_id, node_key, kind, transport, bot, backend, state):
    if kind not in ({'tcp', 'xhttp'} if transport else {'awg', 'xray'}): return
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    protocols, transports = set(node['protocols']), set(node['xray_transports'])
    selected = transports if transport else protocols
    selected.symmetric_difference_update({kind})
    if not protocols:
        await render(bot, chat_id, Screen('Protocol required', ('Keep at least one VPN protocol enabled.',)), [[InlineKeyboardButton(text='🔙 Назад', callback_data=NodeProtocolsCallback(node_key=node_key).pack())]], state, message_id)
        return
    if 'xray' not in protocols: transports.clear()
    elif not transports: transports.update({'tcp', 'xhttp'} if not transport else {'tcp'})
    
    settings = dict(node['settings'])
    if 'awg' in protocols: settings.setdefault('awg_port', 51820)
    if 'xray' in protocols:
        for f, d in {'xray_sni': 'www.cloudflare.com', 'xray_tcp_port': 443, 'xray_xhttp_port': 8443, 'xray_xhttp_path': '/assets'}.items():
            settings.setdefault(f, d)
            
    await backend.edit_node(user_id, node_key, node['desired_revision'], {'protocols': sorted(protocols), 'xray_transports': sorted(transports), 'settings': settings}, command_key=None)
    await show_node_protocols(chat_id, user_id, message_id, node_key, bot, backend, state)

@router.callback_query(ProbeNodeCallback.filter())
async def probe_node_cb(query: CallbackQuery, callback_data: ProbeNodeCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    node_key = callback_data.node_key
    try:
        observation = await backend.node_runtime(user_id, node_key)
        await render(bot, query.message.chat.id, Screen(f'Node {node_key}', (f"Agent: {observation['health_state']}", f"Xray config: {'present' if observation['xray_config_present'] else 'missing'}", f"AWG config: {'present' if observation['awg_config_present'] else 'missing'}")), [[InlineKeyboardButton(text='🔙 Назад', callback_data=AdminNodeCallback(node_key=node_key).pack())]], state, query.message.message_id)
    except BackendError as exc:
        pass # Handle properly

@router.callback_query(ApplyNodeCallback.filter())
async def apply_node_cb(query: CallbackQuery, callback_data: ApplyNodeCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await apply_node(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

async def apply_node(chat_id, user_id, message_id, node_key, bot, backend, state, revision=None):
    if revision is None:
        node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
        revision = node['desired_revision']
    operation = await backend.apply_node_settings(user_id, node_key, revision)
    await render(bot, chat_id, Screen('Applying node settings', ('The backend worker is verifying the node.',)), [[InlineKeyboardButton(text='🔙 Назад', callback_data=AdminNodeCallback(node_key=node_key).pack())]], state, message_id)
    
    for _ in range(30):
        s = await backend.node_settings_operation(user_id, operation['id'])
        if s['status'] == 'succeeded':
            await show_admin_node(chat_id, user_id, message_id, node_key, bot, backend, state)
            return True
        if s['status'] in {'blocked', 'superseded'}: break
        await asyncio.sleep(1)
        
    await show_node_apply_status(chat_id, user_id, message_id, operation['id'], node_key, bot, backend, state)
    return False

@router.callback_query(NodeApplyStatusCallback.filter())
async def node_apply_status_cb(query: CallbackQuery, callback_data: NodeApplyStatusCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await show_node_apply_status(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.operation_id, callback_data.node_key, bot, backend, state)

async def show_node_apply_status(chat_id, user_id, message_id, operation_id, node_key, bot, backend, state):
    s = await backend.node_settings_operation(user_id, operation_id)
    if s['status'] == 'succeeded':
        await show_admin_node(chat_id, user_id, message_id, node_key, bot, backend, state)
        return
    rows = [[InlineKeyboardButton(text='🔙 Назад', callback_data=AdminNodeCallback(node_key=node_key).pack())]]
    if s['status'] in {'blocked', 'superseded'}:
        await render(bot, chat_id, Screen('Node needs attention', ('Settings were not confirmed. Check the agent and operation state.',)), rows, state, message_id)
        return
    rows.insert(0, [InlineKeyboardButton(text='Refresh', callback_data=NodeApplyStatusCallback(operation_id=operation_id, node_key=node_key).pack())])
    await render(bot, chat_id, Screen('Still applying settings', ('The backend is working. Refresh to check the same operation.',)), rows, state, message_id)

@router.callback_query(NodeMaintenanceCallback.filter())
async def node_maintenance_cb(query: CallbackQuery, callback_data: NodeMaintenanceCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await show_node_maintenance(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

async def show_node_maintenance(chat_id, user_id, message_id, node_key, bot, backend, state):
    st = await backend.node_maintenance(user_id, node_key)
    lines = [f"State: {st['status']}"]
    target = st.get('verification_target')
    lines.append(f"Verification target: {target or 'not bound'}")
    if st['status'] == 'draining':
        lines.extend((f"Revocations pending: {st['pending_tasks']}", f"Revocations blocked: {st['blocked_tasks']}", f"Runtime cleanup: {st['cleanup_phase'] or 'not started'}"))
    
    rows = [[InlineKeyboardButton(text='Refresh', callback_data=NodeMaintenanceCallback(node_key=node_key).pack())]]
    if st['status'] == 'active':
        if not target:
            rows += [[InlineKeyboardButton(text='Bind local host', callback_data=BindLocalCallback(node_key=node_key).pack())],
                     [InlineKeyboardButton(text='Bind SSH host', callback_data=BindSshCallback(node_key=node_key).pack())]]
        else:
            rows.append([InlineKeyboardButton(text='Start full cleanup', callback_data=ConfirmNodeDrainCallback(node_key=node_key).pack())])
    elif st['revocations_complete']:
        if st['cleanup_phase'] in {'uninstall_uncertain', 'uninstall_scheduled'}:
            rows.append([InlineKeyboardButton(text='Verify and remove node', callback_data=VerifyRetirementCallback(node_key=node_key).pack())])
        else:
            rows.append([InlineKeyboardButton(text='Next cleanup step', callback_data=CleanupStepCallback(node_key=node_key, expected_phase=st['cleanup_phase'] or 'not_started').pack())])
            
    rows += [[InlineKeyboardButton(text='Remove from bot only', callback_data=ConfirmRegistryRemovalCallback(node_key=node_key).pack())],
             [InlineKeyboardButton(text='🔙 Назад', callback_data=AdminNodeCallback(node_key=node_key).pack())]]
    
    await render(bot, chat_id, Screen('Node maintenance', tuple(lines), 'Full cleanup', ('Bind the host before draining. SSH binding requires root access and a pinned host key.', 'After uninstall, remote verification needs a separate root SSH key on the controller.', 'Registry-only removal leaves runtime artifacts on the VPS.')), rows, state, message_id)

@router.callback_query(BindLocalCallback.filter())
async def bind_local_cb(query: CallbackQuery, callback_data: BindLocalCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await backend.bind_verification_target(query.from_user.id, callback_data.node_key, 'local')
    await show_node_maintenance(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

@router.callback_query(BindSshCallback.filter())
async def bind_ssh_cb(query: CallbackQuery, callback_data: BindSshCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await state.set_state(MaintenanceState.waiting_for_ssh_target)
    await state.update_data(maintenance_node_key=callback_data.node_key)
    await render(bot, query.message.chat.id, Screen('Bind SSH verification host', ('Send root@host for the node. The host key must already be pinned on the controller.', 'Port 22 is currently supported in this screen.')), [[InlineKeyboardButton(text='Cancel', callback_data=NodeMaintenanceCallback(node_key=callback_data.node_key).pack())]], state, query.message.message_id)

@router.message(MaintenanceState.waiting_for_ssh_target, F.text)
async def process_maintenance_ssh(message: Message, bot: Bot, backend: BackendClient, state: FSMContext):
    if message.from_user is None or message.chat.type != 'private': return
    user_id = message.from_user.id
    data = await state.get_data()
    node_key, message_id = data['maintenance_node_key'], data.get('control_message_id')
    target = (message.text or '').strip()
    
    if not re.fullmatch(r'root@[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]', target):
        await render(bot, message.chat.id, Screen('Bind SSH verification host', ('Send root@host for the node. The host key must already be pinned on the controller.', 'Port 22 is currently supported in this screen.')), [[InlineKeyboardButton(text='Cancel', callback_data=NodeMaintenanceCallback(node_key=node_key).pack())]], state, message_id)
        return
        
    try:
        await backend.bind_verification_target(user_id, node_key, 'ssh', target)
        await state.clear()
        await show_node_maintenance(message.chat.id, user_id, message_id, node_key, bot, backend, state)
    except BackendError as exc:
        await render(bot, message.chat.id, Screen('Host verification failed', (f'Reason: {exc.code}', 'Check SSH access and retry.')), [[InlineKeyboardButton(text='Cancel', callback_data=NodeMaintenanceCallback(node_key=node_key).pack())]], state, message_id)

@router.callback_query(ConfirmNodeDrainCallback.filter())
async def confirm_node_drain_cb(query: CallbackQuery, callback_data: ConfirmNodeDrainCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    node_key = callback_data.node_key
    await render(bot, query.message.chat.id, Screen('Start full cleanup', (f'Node: {node_key}', 'All profile grants for this node will be revoked.', 'Cleanup then removes protocol containers, configs, and the agent.', 'The node record is removed only after independent host verification.')), [[InlineKeyboardButton(text='Start drain', callback_data=DrainNodeCallback(node_key=node_key).pack())], [InlineKeyboardButton(text='Cancel', callback_data=NodeMaintenanceCallback(node_key=node_key).pack())]], state, query.message.message_id)

@router.callback_query(DrainNodeCallback.filter())
async def drain_node_cb(query: CallbackQuery, callback_data: DrainNodeCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await backend.drain_node(query.from_user.id, callback_data.node_key)
    await show_node_maintenance(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

@router.callback_query(CleanupStepCallback.filter())
async def cleanup_step_cb(query: CallbackQuery, callback_data: CleanupStepCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await backend.cleanup_node_step(query.from_user.id, callback_data.node_key, callback_data.expected_phase)
    await show_node_maintenance(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

@router.callback_query(VerifyRetirementCallback.filter())
async def verify_retirement_cb(query: CallbackQuery, callback_data: VerifyRetirementCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await backend.verify_and_retire_node(query.from_user.id, callback_data.node_key)
    await render(bot, query.message.chat.id, Screen('Node removed', ('The host was verified clean and the node record was retired.',)), [[InlineKeyboardButton(text='Nodes', callback_data=AdminNodesCallback().pack())]], state, query.message.message_id)

@router.callback_query(ConfirmRegistryRemovalCallback.filter())
async def confirm_registry_removal_cb(query: CallbackQuery, callback_data: ConfirmRegistryRemovalCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    node_key = callback_data.node_key
    await render(bot, query.message.chat.id, Screen('Remove from bot only', (f'Node: {node_key}', 'This removes its access grants and backend record.', 'The node runtime, containers, agent, and SSH keys may remain.', 'Use only when the VPS cannot be verified or cleaned.')), [[InlineKeyboardButton(text='Confirm registry removal', callback_data=RetireRegistryCallback(node_key=node_key).pack())], [InlineKeyboardButton(text='Cancel', callback_data=NodeMaintenanceCallback(node_key=node_key).pack())]], state, query.message.message_id)

@router.callback_query(RetireRegistryCallback.filter())
async def retire_registry_cb(query: CallbackQuery, callback_data: RetireRegistryCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    result = await backend.retire_node_registry_only(query.from_user.id, callback_data.node_key)
    await render(bot, query.message.chat.id, Screen('Node removed from bot', (f"Node: {result['node_key']}", 'Remote runtime was not verified or removed.')), [[InlineKeyboardButton(text='Nodes', callback_data=AdminNodesCallback().pack())]], state, query.message.message_id)

@router.callback_query(UpdatesCallback.filter())
async def updates_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    page = await backend.admin_nodes(user_id)
    rows = [[InlineKeyboardButton(text=node['title'], callback_data=NodeUpdatesCallback(node_key=node['key']).pack())] for node in page['items']]
    rows.append([InlineKeyboardButton(text='🔙 Назад', callback_data='admin_menu')])
    await render(bot, query.message.chat.id, Screen('Updates', ('Select a node to compare its runtime with this release.', 'Controller updates are installed from the controller host.')), rows, state, query.message.message_id)

@router.callback_query(NodeUpdatesCallback.filter())
async def node_updates_cb(query: CallbackQuery, callback_data: NodeUpdatesCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    node_key = callback_data.node_key
    expected = (Path(__file__).resolve().parents[3] / 'VERSION').read_text().strip()
    try:
        runtime = await backend.node_runtime(user_id, node_key)
        installed = runtime['runtime_version'] or 'unknown'
        error = None
    except BackendError as exc:
        installed = 'unreachable'
        error = exc.code
        
    lines = (f'Current release: {expected}', f'Node runtime: {installed}')
    if error: lines += (f'Agent status: {error}',)
    elif installed != expected: lines += ('Agent/runtime setup may be needed.',)
    else: lines += ('Runtime version matches this release.',)
    
    rows = []
    if installed != expected:
        if installed == 'unreachable':
            rows += [[InlineKeyboardButton(text='Set up local agent', callback_data=RolloutLocalCallback(node_key=node_key).pack())],
                     [InlineKeyboardButton(text='Set up SSH agent', callback_data=RolloutSshCallback(node_key=node_key).pack())]]
        else:
            rows.append([InlineKeyboardButton(text='Refresh runtime', callback_data=RefreshRuntimeCallback(node_key=node_key).pack())])
    rows.append([InlineKeyboardButton(text='🔙 Назад', callback_data=AdminNodeCallback(node_key=node_key).pack())])
    await render(bot, query.message.chat.id, Screen('Node updates', lines), rows, state, query.message.message_id)

@router.callback_query(RefreshRuntimeCallback.filter())
async def refresh_runtime_cb(query: CallbackQuery, callback_data: RefreshRuntimeCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    node_key = callback_data.node_key
    
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    updated = await backend.edit_node(user_id, node_key, node['desired_revision'], {'settings': node['settings']}, command_key=None)
    await apply_node(query.message.chat.id, user_id, query.message.message_id, updated['key'], bot, backend, state, revision=updated['desired_revision'])

@router.callback_query(RolloutLocalCallback.filter())
async def rollout_local_cb(query: CallbackQuery, callback_data: RolloutLocalCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    node_key = callback_data.node_key
    await state.update_data(rollout_node_key=node_key, rollout_ssh_target=None)
    await queue_rollout(query.message.chat.id, user_id, query.message.message_id, node_key, 'local', bot, backend, state)

@router.callback_query(RolloutSshCallback.filter())
async def rollout_ssh_cb(query: CallbackQuery, callback_data: RolloutSshCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    node_key = callback_data.node_key
    await state.set_state(AgentDraftState.waiting_for_ssh_target)
    await state.update_data(rollout_node_key=node_key)
    await render(bot, query.message.chat.id, Screen('SSH agent setup', ('Send an SSH target such as root@lv1.example.com.', 'The controller uses its configured SSH key; port 22 is used.')), [[InlineKeyboardButton(text='Cancel', callback_data=AdminNodeCallback(node_key=node_key).pack())]], state, query.message.message_id)

@router.message(AgentDraftState.waiting_for_ssh_target, F.text)
async def process_agent_ssh(message: Message, bot: Bot, backend: BackendClient, state: FSMContext):
    if message.from_user is None or message.chat.type != 'private': return
    user_id = message.from_user.id
    target = (message.text or '').strip()
    data = await state.get_data()
    node_key, message_id = data['rollout_node_key'], data.get('control_message_id')
    
    if not re.fullmatch(r'(?:[A-Za-z_][A-Za-z0-9._-]*@)?[A-Za-z0-9.-]+', target):
        await render(bot, message.chat.id, Screen('SSH agent setup', ('Send an SSH target such as root@lv1.example.com.',)), [[InlineKeyboardButton(text='Cancel', callback_data=AdminNodeCallback(node_key=node_key).pack())]], state, message_id)
        return
        
    await state.update_data(rollout_ssh_target=target)
    await queue_rollout(message.chat.id, user_id, message_id, node_key, 'ssh', bot, backend, state, target)
    await state.clear()

async def queue_rollout(chat_id, user_id, message_id, node_key, transport, bot, backend, state, ssh_target=None):
    try:
        task = await backend.rollout_agent(user_id, node_key, transport, ssh_target=ssh_target, command_key=None)
        await show_rollout_status(chat_id, user_id, message_id, task['id'], bot, backend, state)
    except BackendError as exc:
        await render(bot, chat_id, Screen('Could not queue agent setup', (f'Reason: {exc.code}', 'Retry the same request or cancel.')), [[InlineKeyboardButton(text='Retry', callback_data=RetryRolloutCallback().pack())], [InlineKeyboardButton(text='Cancel', callback_data=AdminNodeCallback(node_key=node_key).pack())]], state, message_id)

@router.callback_query(RetryRolloutCallback.filter())
async def retry_rollout_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    node_key, ssh_target = data.get('rollout_node_key'), data.get('rollout_ssh_target')
    if not node_key:
        await show_admin_nodes(query.message.chat.id, query.from_user.id, query.message.message_id, bot, backend, state)
        return
    transport = 'ssh' if ssh_target else 'local'
    await queue_rollout(query.message.chat.id, query.from_user.id, query.message.message_id, node_key, transport, bot, backend, state, ssh_target)

@router.callback_query(RolloutStatusCallback.filter())
async def rollout_status_cb(query: CallbackQuery, callback_data: RolloutStatusCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await show_rollout_status(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.task_id, bot, backend, state)

async def show_rollout_status(chat_id, user_id, message_id, task_id, bot, backend, state):
    task = await backend.agent_rollout(user_id, task_id)
    if task['status'] == 'succeeded': lines = ('Agent installed and reachable through the driver.', 'Apply node settings to deploy VPN protocols.')
    elif task['status'] == 'blocked': lines = ('Agent setup did not finish or its result is uncertain.', 'Inspect the backend worker and node before retrying.')
    else: lines = ('Agent setup is running in the backend worker.', 'Refresh to see the confirmed result.')
    
    rows = [] if task['status'] in {'succeeded', 'blocked'} else [[InlineKeyboardButton(text='Refresh', callback_data=RolloutStatusCallback(task_id=task_id).pack())]]
    if task['status'] == 'succeeded': rows.append([InlineKeyboardButton(text='Refresh runtime', callback_data=RefreshRuntimeCallback(node_key=task['node_key']).pack())])
    rows.append([InlineKeyboardButton(text='Node card', callback_data=AdminNodeCallback(node_key=task['node_key']).pack())])
    await render(bot, chat_id, Screen('Agent setup', lines), rows, state, message_id)

