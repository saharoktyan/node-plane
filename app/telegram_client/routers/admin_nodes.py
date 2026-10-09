import re
import secrets
from copy import deepcopy
from uuid import uuid4
from urllib.parse import urlencode
from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from ..backend import BackendClient, BackendError
from ..screens import Screen, Section, Table, server_label
from ..i18n import normalize_locale, tr
from ..node_templates import NODE_TEMPLATES
from .common import render
from ..navigation import remember_node, parent_path
from .states import NodeDraftState, NodeEditState, MaintenanceState, AgentDraftState
from .callbacks import (
    AdminNodesCallback, NewNodeCallback, AdminNodeCallback,
    NodeSettingsCallback, EditNodeFieldCallback, NodeProtocolsCallback,
    ToggleNodeProtocolCallback, ToggleNodeTransportCallback, NodeMaintenanceCallback,
    BindLocalCallback, BindSshCallback, ConfirmNodeDrainCallback, DrainNodeCallback,
    CleanupStepCallback, VerifyRetirementCallback, ConfirmRegistryRemovalCallback,
    RetireRegistryCallback,
    RolloutLocalCallback, RolloutSshCallback, RolloutStatusCallback, RetryRolloutCallback,
    ProbeNodeCallback, ApplyNodeCallback, NodeApplyStatusCallback
)
import asyncio

router = Router()


class NodeSearchState(StatesGroup):
    waiting_for_query = State()


async def _clear_node_flow(state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    await state.update_data(**{key: value for key, value in data.items()
        if key in {'locale', 'admin_node_search', 'admin_node_cursors', 'admin_node_page', 'node_settings_draft', 'node_settings_view', '_navigation_nodes', '_navigation_node_jobs'}})


_DRAFT_FIELDS = ('title', 'region', 'flag', 'notes', 'transport', 'ssh_target',
                 'protocols', 'xray_transports', 'settings')


async def editable_node(user_id, node_key, backend, state):
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    await remember_node(state, node)
    draft = (await state.get_data()).get('node_settings_draft')
    if draft and draft['node_key'] == node_key:
        node = {**node, **deepcopy(draft['values'])}
    return node


async def change_node_draft(user_id, node_key, values, backend, state, *, baseline_node=None):
    data = await state.get_data()
    draft = data.get('node_settings_draft')
    if not draft or draft['node_key'] != node_key:
        node = baseline_node or await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
        baseline = {field: deepcopy(node.get(field)) for field in _DRAFT_FIELDS}
        baseline['notes'] = baseline['notes'] or ''
        draft = {'node_key': node_key, 'revision': node['desired_revision'],
                 'baseline': baseline, 'values': deepcopy(baseline), 'command_key': str(uuid4())}
    draft = deepcopy(draft)
    draft['values'].update(deepcopy(values))
    await state.update_data(node_settings_draft=draft, node_region_confirmation=None)


async def draft_controls(node, state, locale):
    draft = (await state.get_data()).get('node_settings_draft')
    if draft and draft['node_key'] == node['key'] and draft['values'] != draft['baseline']:
        return [[InlineKeyboardButton(text=tr(locale, 'nodes.draft.save_only' if not node['applied_revision'] else 'nodes.draft.save'), callback_data=f'node_draft_save:{node["key"]}', style='primary'),
                 InlineKeyboardButton(text=tr(locale, 'nodes.draft.reset'), callback_data=f'node_draft_reset:{node["key"]}')]]
    if not node['applied_revision']:
        return [[InlineKeyboardButton(text=tr(locale, 'nodes.card.bootstrap'), callback_data=f'bootstrap_menu:{node["key"]}', style='primary')]]
    if node['desired_revision'] > node['applied_revision']:
        return [[InlineKeyboardButton(text=tr(locale, 'nodes.card.apply'), callback_data=ApplyNodeCallback(node_key=node['key']).pack(), style='primary')]]
    return []


@router.callback_query(F.data.startswith('node_draft_reset:'))
async def reset_node_draft_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    node_key = query.data.split(':', 1)[1]
    data = await state.get_data()
    draft = data.get('node_settings_draft')
    if draft and draft['node_key'] == node_key:
        await state.update_data(node_settings_draft=None)
    view = data.get('node_settings_view', 'root')
    args = (query.message.chat.id, query.from_user.id, query.message.message_id)
    if view == 'protocols':
        await show_node_protocols(*args, node_key, bot, backend, state)
    elif view == 'channel':
        await show_node_connection(*args, node_key, bot, backend, state)
    elif view in {'awg', 'xray', 'general', 'connection'}:
        from .admin_node_tools import show_section
        await show_section(*args, view, node_key, bot, backend, state)
    else:
        await show_node_settings(*args, node_key, bot, backend, state)


@router.callback_query(F.data.startswith('node_draft_save:'))
async def save_node_draft_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await _save_node_draft(query, bot, backend, state, query.data.split(':', 1)[1])


async def _save_node_draft(query, bot, backend, state, node_key, *, confirmed=False):
    data = await state.get_data()
    draft = data.get('node_settings_draft')
    if not draft or draft['node_key'] != node_key or draft['values'] == draft['baseline']:
        return
    try:
        values = {key: value for key, value in draft['values'].items() if value != draft['baseline'][key]}
        if 'region' in values and not confirmed:
            preview = await backend.request('GET', f'/api/v1/nodes/{node_key}/region-access-preview?' +
                urlencode({'region': values['region']}), telegram_user_id=query.from_user.id)
            if preview['affected_profiles']:
                locale = normalize_locale(data.get('locale'))
                nonce = secrets.token_urlsafe(6)
                await state.update_data(node_region_confirmation={'nonce': nonce, 'node_key': node_key,
                    'values': deepcopy(values), 'revision': draft['revision'], 'command_key': draft['command_key'],
                    'user_id': query.from_user.id, 'message_id': query.message.message_id})
                await render(bot, query.message.chat.id, Screen(tr(locale, 'policy.region_confirm'),
                    (tr(locale, 'policy.region_move', old=draft['baseline']['region'], new=values['region']),
                     tr(locale, 'policy.region_affected', count=preview['affected_profiles']),
                     tr(locale, 'policy.region_warning')), embedded_buttons=True, navigation=True),
                    [[InlineKeyboardButton(text=tr(locale, 'policy.confirm'),
                        callback_data=f'node_region_yes:{nonce}', style='primary')],
                     [InlineKeyboardButton(text=tr(locale, 'back'),
                        callback_data=NodeSettingsCallback(node_key=node_key).pack())]], state, query.message.message_id)
                return
        if confirmed:
            values['confirm_access_change'] = True
        node = await backend.edit_node(query.from_user.id, node_key, draft['revision'], values, command_key=draft['command_key'])
    except BackendError as exc:
        locale = normalize_locale(data.get('locale'))
        message = tr(locale, 'nodes.draft.in_use' if exc.code == 'node_protocol_in_use' else
                     'nodes.draft.conflict' if exc.code == 'revision_conflict' else 'nodes.settings.save_failed')
        await render(bot, query.message.chat.id, Screen(tr(locale, 'nodes.settings.save_failed_title'),
            (message,), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=NodeSettingsCallback(node_key=node_key).pack())]], state, query.message.message_id)
        return
    await state.update_data(node_settings_draft=None, node_region_confirmation=None)
    if not node['applied_revision']:
        await show_admin_node(query.message.chat.id, query.from_user.id, query.message.message_id,
            node_key, bot, backend, state)
        return
    await apply_node(query.message.chat.id, query.from_user.id, query.message.message_id, node_key, bot, backend, state, revision=node['desired_revision'])


@router.callback_query(F.data.startswith('node_region_yes:'))
async def confirm_node_region_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    confirmation, draft = data.get('node_region_confirmation'), data.get('node_settings_draft')
    if not confirmation or not draft:
        return
    values = {k: v for k, v in draft['values'].items() if v != draft['baseline'][k]}
    if (confirmation['nonce'] != query.data.split(':', 1)[1]
            or confirmation['message_id'] != query.message.message_id
            or confirmation['user_id'] != query.from_user.id
            or data.get('control_message_id') != query.message.message_id
            or confirmation['values'] != values or confirmation['revision'] != draft['revision']
            or confirmation['command_key'] != draft['command_key'] or confirmation['node_key'] != draft['node_key']):
        return
    await _save_node_draft(query, bot, backend, state, draft['node_key'], confirmed=True)

@router.callback_query(AdminNodesCallback.filter())
async def admin_nodes_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await _clear_node_flow(state)
    await show_admin_nodes(query.message.chat.id, query.from_user.id, query.message.message_id, bot, backend, state)

async def show_admin_nodes(chat_id, user_id, message_id, bot, backend, state, page_index=None):
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    cursors = list(data.get('admin_node_cursors') or [None])
    page_index = data.get('admin_node_page', 0) if page_index is None else page_index
    if page_index < 0 or page_index >= len(cursors):
        page_index = 0
    search = data.get('admin_node_search')
    page = await backend.admin_nodes(user_id, cursor=cursors[page_index],
                                     search=search, limit=10, order='region')
    if page.get('next_cursor'):
        if len(cursors) == page_index + 1:
            cursors.append(page['next_cursor'])
        else:
            cursors[page_index + 1] = page['next_cursor']
    else:
        cursors = cursors[:page_index + 1]
    await state.update_data(admin_node_cursors=cursors, admin_node_page=page_index)
    groups = {}
    for node in page['items']:
        await remember_node(state, node)
        groups.setdefault(node.get('region') or tr(locale, 'nodes.region_unknown'), []).append(node)
    sections = []
    controls = [InlineKeyboardButton(text=tr(locale, 'nodes.admin.add'), callback_data=NewNodeCallback().pack(), style='primary'),
        InlineKeyboardButton(text=tr(locale, 'nodes.admin.search'), callback_data='admin_node_search')]
    if search:
        controls.append(InlineKeyboardButton(text=tr(locale, 'nodes.admin.show_all'), callback_data='admin_node_all'))
    sections.append(Section('', rows=(tuple(controls),)))
    for region, items in groups.items():
        entries = []
        for node in items:
            summary = node.get('overview')
            label = server_label(node)
            if summary and summary['state'] != 'applied_unverified':
                label += ' · ' + tr(locale, 'nodes.card.state.' + summary['state'])
            entries.append(Section('', rows=((InlineKeyboardButton(text=label,
                callback_data=AdminNodeCallback(node_key=node['key']).pack()),),)))
        sections.append(Section(region, sections=tuple(entries)))
    arrows = []
    if page_index > 0:
        arrows.append(InlineKeyboardButton(text='←', callback_data=f'admin_node_page:{page_index - 1}'))
    if page.get('next_cursor'):
        arrows.append(InlineKeyboardButton(text='→', callback_data=f'admin_node_page:{page_index + 1}'))
    rows = [arrows] if arrows else []
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data='admin_menu')])
    lines = [tr(locale, 'nodes.admin.no_results' if search else 'nodes.admin.empty')] if not page['items'] else []
    if search:
        lines.append(tr(locale, 'profiles.search.active', query=search))
    if page_index or page.get('next_cursor'):
        lines.append(tr(locale, 'nodes.admin.page', number=page_index + 1))
    await render(bot, chat_id, Screen(tr(locale, 'nodes.admin.title'), tuple(lines),
        sections=tuple(sections), embedded_buttons=True, navigation=True), rows, state, message_id)


@router.callback_query(F.data.startswith('admin_node_page:'))
async def admin_node_page_cb(query: CallbackQuery, bot: Bot,
                             backend: BackendClient, state: FSMContext):
    await query.answer()
    try:
        page_index = int(query.data.split(':', 1)[1])
    except ValueError:
        return
    await show_admin_nodes(query.message.chat.id, query.from_user.id,
        query.message.message_id, bot, backend, state, page_index)


@router.callback_query(F.data == 'admin_node_all')
async def admin_node_all_cb(query: CallbackQuery, bot: Bot,
                            backend: BackendClient, state: FSMContext):
    await query.answer()
    await state.update_data(admin_node_search=None, admin_node_cursors=[None],
                            admin_node_page=0)
    await show_admin_nodes(query.message.chat.id, query.from_user.id,
        query.message.message_id, bot, backend, state)


@router.callback_query(F.data == 'admin_node_search')
async def admin_node_search_cb(query: CallbackQuery, bot: Bot, state: FSMContext):
    await query.answer()
    await state.set_state(NodeSearchState.waiting_for_query)
    locale = normalize_locale((await state.get_data()).get('locale'))
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'nodes.admin.search_title'),
            (tr(locale, 'nodes.admin.search_prompt'),), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=AdminNodesCallback().pack())]],
        state, query.message.message_id)


@router.message(NodeSearchState.waiting_for_query, F.text)
async def admin_node_search_message(message: Message, bot: Bot,
                                    backend: BackendClient, state: FSMContext):
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    search = message.text.strip()
    if not 1 <= len(search) <= 128:
        await render(bot, message.chat.id,
            Screen(tr(locale, 'nodes.admin.search_title'),
                (tr(locale, 'nodes.admin.search_invalid'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data=AdminNodesCallback().pack())]],
            state, data.get('control_message_id'))
        return
    await state.set_state(None)
    await state.update_data(admin_node_search=search, admin_node_cursors=[None],
                            admin_node_page=0)
    await show_admin_nodes(message.chat.id, message.from_user.id,
        data.get('control_message_id'), bot, backend, state)



@router.callback_query(NewNodeCallback.filter())
async def new_node_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    try:
        options = await backend.node_creation_options(query.from_user.id)
    except BackendError:
        locale = normalize_locale((await state.get_data()).get('locale'))
        await render(bot, query.message.chat.id, Screen(tr(locale, 'node.wizard.summary.title'),
            (tr(locale, 'node.wizard.unavailable'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminNodesCallback().pack())]], state, query.message.message_id)
        return
    defaults = options['defaults']
    await state.update_data(wizard_data={'protocols': list(defaults['protocols']),
        'xray_transports': list(defaults['xray_transports']), 'settings': dict(defaults['settings'])},
        wizard_templates=options.get('templates', []),
        wizard_local_available=options['local_available'], create_node_command_key=str(uuid4()),
                            rollout_command_key=str(uuid4()), wizard_saved=False)
    await render_wizard_transport(query.message.chat.id, bot, state,
                                  query.message.message_id)


_WIZARD_TEXT_STATES = {
    'key': NodeDraftState.waiting_for_key,
    'title': NodeDraftState.waiting_for_title,
    'region': NodeDraftState.waiting_for_region,
    'flag': NodeDraftState.waiting_for_flag,
    'target': NodeDraftState.waiting_for_target,
    'public_host': NodeDraftState.waiting_for_public_host,
}
_WIZARD_PREVIOUS = {
    'key': 'template', 'title': 'key', 'region': 'title', 'flag': 'region',
    'target': 'template', 'public_host': 'template', 'protocols': 'public_host',
}


async def render_wizard_step(chat_id: int, bot: Bot, state: FSMContext,
                             step: str, message_id: int | None = None,
                             error: bool = False):
    if step == 'region' and not error:
        await render_wizard_regions(chat_id, bot, state, message_id)
        return
    await state.set_state(_WIZARD_TEXT_STATES[step])
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    current = data.get('wizard_data', {}).get('ssh_target' if step == 'target' else step)
    lines = [tr(locale, f'node.wizard.{step}.prompt')]
    if current:
        lines.append(tr(locale, 'node.wizard.current', value=current))
    if error:
        lines.insert(0, tr(locale, f'node.wizard.{step}.invalid'))
    navigation = []
    previous = _WIZARD_PREVIOUS.get(step)
    if step in {'target', 'public_host'} and data.get('wizard_data', {}).get('template') == 'custom':
        previous = 'flag'
    if step == 'public_host' and data.get('wizard_data', {}).get('transport') == 'ssh':
        previous = 'target'
    if previous:
        navigation.append(InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=f'wizard_back:{previous}'))
    else:
        navigation.append(InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=AdminNodesCallback().pack()))
    if step == 'region' and error:
        navigation[0] = InlineKeyboardButton(text=tr(locale, 'back'), callback_data='wizard_back:region')
    if step == 'flag':
        navigation.append(InlineKeyboardButton(text=tr(locale, 'node.wizard.skip'),
            callback_data='wizard_skip_flag', style='primary'))
    rows = [navigation]
    await render(bot, chat_id, Screen(tr(locale, f'node.wizard.{step}.title'),
                                       tuple(lines), embedded_buttons=True, navigation=True), rows, state, message_id)


@router.callback_query(F.data.startswith('wizard_back:'))
async def wizard_back_cb(query: CallbackQuery, bot: Bot, state: FSMContext):
    await query.answer()
    step = query.data.split(':', 1)[1]
    if step in _WIZARD_TEXT_STATES:
        await render_wizard_step(query.message.chat.id, bot, state, step,
                                 query.message.message_id)
    elif step == 'transport':
        await render_wizard_transport(query.message.chat.id, bot, state,
                                      query.message.message_id)
    elif step == 'template':
        await render_wizard_templates(query.message.chat.id, bot, state,
                                      query.message.message_id)
    elif step == 'protocols':
        await render_wizard_protocols(query.message.chat.id, bot, state,
                                      query.message.message_id)

@router.message(NodeDraftState.waiting_for_key, F.text)
async def process_wizard_key(message: Message, bot: Bot, state: FSMContext):
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', message.text.strip()):
        await render_wizard_step(message.chat.id, bot, state, 'key',
                                 data.get('control_message_id'), error=True)
        return
    w = dict(data.get('wizard_data', {}))
    w['key'] = message.text.strip()
    await state.update_data(wizard_data=w)
    await render_wizard_step(message.chat.id, bot, state, 'title',
                             data.get('control_message_id'))

@router.message(NodeDraftState.waiting_for_title, F.text)
async def process_wizard_title(message: Message, bot: Bot, state: FSMContext):
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    value = message.text.strip()
    if not value or len(value) > 128:
        await render_wizard_step(message.chat.id, bot, state, 'title',
                                 data.get('control_message_id'), error=True)
        return
    w = dict(data.get('wizard_data', {}))
    w['title'] = value
    await state.update_data(wizard_data=w)
    await render_wizard_step(message.chat.id, bot, state, 'region',
                             data.get('control_message_id'))

@router.message(NodeDraftState.waiting_for_region, F.text)
async def process_wizard_region(message: Message, bot: Bot, state: FSMContext):
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    value = message.text.strip()
    if not value or len(value) > 128:
        await render_wizard_step(message.chat.id, bot, state, 'region',
                                 data.get('control_message_id'), error=True)
        return
    w = dict(data.get('wizard_data', {}))
    w['region'] = value
    await state.update_data(wizard_data=w)
    await render_wizard_step(message.chat.id, bot, state, 'flag',
                             data.get('control_message_id'))

@router.callback_query(F.data == "wizard_skip_flag")
async def wizard_skip_flag_cb(query: CallbackQuery, bot: Bot, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    w = dict(data.get('wizard_data', {}))
    w['flag'] = ''
    await state.update_data(wizard_data=w)
    await render_wizard_address(query.message.chat.id, bot, state, query.message.message_id)

@router.message(NodeDraftState.waiting_for_flag, F.text)
async def process_wizard_flag(message: Message, bot: Bot, state: FSMContext):
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    value = message.text.strip()
    if not value or len(value) > 16:
        await render_wizard_step(message.chat.id, bot, state, 'flag',
                                 data.get('control_message_id'), error=True)
        return
    w = dict(data.get('wizard_data', {}))
    w['flag'] = value
    await state.update_data(wizard_data=w)
    await render_wizard_address(message.chat.id, bot, state,
                                data.get('control_message_id'))

async def render_wizard_transport(chat_id: int, bot: Bot, state: FSMContext, message_id: int | None = None):
    await state.set_state(NodeDraftState.waiting_for_transport)
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    selected = data.get('wizard_data', {}).get('transport')
    rows = [
        [InlineKeyboardButton(text=tr(locale, 'node.wizard.transport.ssh'), callback_data="wizard_transport:ssh", style='primary' if selected == 'ssh' else None),
         InlineKeyboardButton(text=tr(locale, 'node.wizard.transport.local'), callback_data="wizard_transport:local", style='primary' if selected == 'local' else None)],
        [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminNodesCallback().pack())]
    ]
    if data.get('wizard_local_available') is False:
        rows[0] = rows[0][:1]
    await render(bot, chat_id, Screen(tr(locale, 'node.wizard.transport.title'),
        (tr(locale, 'node.wizard.transport.prompt'),) +
        ((tr(locale, 'node.wizard.local_exists'),) if data.get('wizard_local_available') is False else ()),
        embedded_buttons=True, navigation=True), rows, state, message_id)

@router.callback_query(F.data.startswith("wizard_transport:"))
async def wizard_transport_cb(query: CallbackQuery, bot: Bot, state: FSMContext):
    await query.answer()
    if await state.get_state() != NodeDraftState.waiting_for_transport.state:
        return
    transport = query.data.split(":")[1]
    if transport not in {'local', 'ssh'}:
        return
    data = await state.get_data()
    if data.get('wizard_saved'):
        return
    if transport == 'local' and data.get('wizard_local_available') is False:
        await render_wizard_transport(query.message.chat.id, bot, state, query.message.message_id)
        return
    w = dict(data.get('wizard_data', {}))
    w['transport'] = transport
    await state.update_data(wizard_data=w)
    
    if transport == 'local':
        w['ssh_target'] = None
        await state.update_data(wizard_data=w)
    await render_wizard_templates(query.message.chat.id, bot, state, query.message.message_id)


async def render_wizard_templates(chat_id, bot, state, message_id=None):
    await state.set_state(NodeDraftState.waiting_for_template)
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    selected = data.get('wizard_data', {}).get('template')
    sections = []
    for region_token, _, region_name in REGION_PRESETS:
        buttons = [InlineKeyboardButton(
            text=f"{template.flag} {tr(locale, 'node.template.' + template.code)}",
            callback_data='wizard_template:' + template.code,
            style='primary' if selected == template.code else None)
            for template in NODE_TEMPLATES if template.region == region_name]
        if buttons:
            sections.append(Section(tr(locale, 'region.' + region_token), rows=tuple(
                tuple(buttons[index:index + 2]) for index in range(0, len(buttons), 2))))
    rows = [[InlineKeyboardButton(text=tr(locale, 'node.wizard.template.custom'),
                callback_data='wizard_template:custom')],
            [InlineKeyboardButton(text=tr(locale, 'back'), callback_data='wizard_back:transport')]]
    await render(bot, chat_id, Screen(tr(locale, 'node.wizard.template.title'),
        (tr(locale, 'node.wizard.template.prompt'),), sections=tuple(sections),
        embedded_buttons=True, navigation=True), rows, state, message_id)


async def render_wizard_address(chat_id, bot, state, message_id=None):
    data = await state.get_data()
    step = 'target' if data.get('wizard_data', {}).get('transport') == 'ssh' else 'public_host'
    await render_wizard_step(chat_id, bot, state, step, message_id)


@router.callback_query(F.data.startswith('wizard_template:'))
async def wizard_template_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    if await state.get_state() != NodeDraftState.waiting_for_template.state:
        return
    data = await state.get_data()
    w = dict(data.get('wizard_data', {}))
    token = query.data.split(':', 1)[1]
    if w.get('transport') not in {'local', 'ssh'} or data.get('wizard_saved'):
        return
    if token == 'custom':
        w['template'] = 'custom'
        await state.update_data(wizard_data=w)
        await render_wizard_step(query.message.chat.id, bot, state, 'key', query.message.message_id)
        return
    template = next((item for item in NODE_TEMPLATES if item.code == token), None)
    if template is None:
        return
    if w.get('template') != token:
        preview = next((t['draft'] for t in data.get('wizard_templates', []) if t['code'] == token), None)
        if preview:
            w.update(preview)
            await state.update_data(wizard_data=w)
            await render_wizard_address(query.message.chat.id, bot, state, query.message.message_id)
            return
        keys = []
        cursor = None
        try:
            while True:
                params = {'search': template.code, 'limit': 100, **({'cursor': cursor} if cursor else {})}
                page = await backend.request('GET', '/api/v1/nodes?' + urlencode(params),
                    telegram_user_id=query.from_user.id)
                keys.extend(item['key'] for item in page['items'])
                cursor = page.get('next_cursor')
                if not cursor:
                    break
        except BackendError:
            locale = normalize_locale(data.get('locale'))
            await render(bot, query.message.chat.id,
                Screen(tr(locale, 'node.wizard.template.title'),
                    (tr(locale, 'node.wizard.unavailable'),), embedded_buttons=True, navigation=True),
                [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data='wizard_back:template')]],
                state, query.message.message_id)
            return
        w.update(template.draft(keys))
        await state.update_data(wizard_data=w)
    await render_wizard_address(query.message.chat.id, bot, state, query.message.message_id)

@router.message(NodeDraftState.waiting_for_target, F.text)
async def process_wizard_target(message: Message, bot: Bot, state: FSMContext):
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    w = dict(data.get('wizard_data', {}))
    target = message.text.strip()
    if target.endswith(':22'):
        target = target[:-3]
    if not re.fullmatch(r'root@[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]', target):
        await render_wizard_step(message.chat.id, bot, state, 'target',
                                 data.get('control_message_id'), error=True)
        return
    w['ssh_target'] = target
    await state.update_data(wizard_data=w)
    await render_wizard_step(message.chat.id, bot, state, 'public_host',
                             data.get('control_message_id'))

@router.message(NodeDraftState.waiting_for_public_host, F.text)
async def process_wizard_host(message: Message, bot: Bot, backend: BackendClient, state: FSMContext):
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    value = message.text.strip()
    if not value or len(value) > 255 or any(c.isspace() for c in value):
        await render_wizard_step(message.chat.id, bot, state, 'public_host',
                                 data.get('control_message_id'), error=True)
        return
    w = dict(data.get('wizard_data', {}))
    w['public_host'] = value
    w.setdefault('protocols', [])
    await state.update_data(wizard_data=w)
    await state.set_state(NodeDraftState.waiting_for_protocols)
    await render_wizard_protocols(message.chat.id, bot, state,
                                  data.get('control_message_id'))

async def render_wizard_protocols(chat_id: int, bot: Bot, state: FSMContext, message_id: int | None = None):
    await state.set_state(NodeDraftState.waiting_for_protocols)
    data = await state.get_data()
    w = data.get('wizard_data', {})
    protocols = w.get('protocols', [])
    locale = normalize_locale(data.get('locale'))
    
    rows = [[InlineKeyboardButton(text=tr(locale, 'protocol.' + code),
                callback_data='wizard_proto:' + code,
                style='primary' if code in protocols else None) for code in ('xray', 'awg')],
        [InlineKeyboardButton(text=tr(locale, 'back'), callback_data='wizard_back:public_host'),
         InlineKeyboardButton(text=tr(locale, 'node.wizard.review'), callback_data='wizard_proto:done', style='primary')]]
    await render(bot, chat_id, Screen(tr(locale, 'node.wizard.protocols.title'),
        (tr(locale, 'node.wizard.protocols.prompt'),
         tr(locale, 'node.wizard.protocols.xray_default')), embedded_buttons=True, navigation=True), rows, state, message_id)


async def render_wizard_summary(chat_id: int, bot: Bot, state: FSMContext,
                                message_id: int | None = None):
    await state.set_state(NodeDraftState.waiting_for_summary)
    data = await state.get_data()
    w = data.get('wizard_data', {})
    locale = normalize_locale(data.get('locale'))
    transports = ', '.join(w.get('xray_transports') or ['tcp', 'xhttp']) if 'xray' in w.get('protocols', []) else '—'
    lines = (
        tr(locale, 'node.wizard.summary.key', value=w.get('key', '—')),
        tr(locale, 'node.wizard.summary.name', value=w.get('title', '—')),
        tr(locale, 'node.wizard.summary.region', value=w.get('region', '—'), flag=w.get('flag', '')),
        tr(locale, 'node.wizard.summary.transport', value=tr(locale,
            'node.wizard.transport.value.' + w['transport']) if w.get('transport') in {'local', 'ssh'} else '—'),
        tr(locale, 'node.wizard.summary.target', value=w.get('ssh_target') or '—'),
        tr(locale, 'node.wizard.summary.host', value=w.get('public_host', '—')),
        tr(locale, 'node.wizard.summary.protocols', value=', '.join(w.get('protocols', [])) or '—'),
        tr(locale, 'node.wizard.summary.xray_transports', value=transports),
        tr(locale, 'node.wizard.summary.note'),
    )
    rows = [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data='wizard_proto:back'),
             InlineKeyboardButton(text=tr(locale, 'node.wizard.save'), callback_data='wizard_save', style='primary')]]
    def summary_table(fields):
        return Table((tr(locale, 'account.rich.field'), tr(locale, 'account.rich.value')),
            tuple((tr(locale, 'nodes.settings.field.' + field), str(value or '—')) for field, value in fields))
    sections = (
        Section(tr(locale, 'nodes.rich.name_region'), tables=(summary_table((
            ('title', w.get('title')), ('region', w.get('region')), ('flag', w.get('flag')))),)),
        Section(tr(locale, 'nodes.rich.connection'), tables=(summary_table((
            ('transport', tr(locale, 'node.wizard.transport.value.' + w['transport'])),
            ('public_host', w.get('public_host')))),)),
        Section(tr(locale, 'nodes.rich.services'), lines=(
            ', '.join(tr(locale, 'protocol.' + code) for code in w.get('protocols', [])),
            tr(locale, 'node.wizard.summary.xray_transports', value=transports)) if 'xray' in w.get('protocols', []) else (
            ', '.join(tr(locale, 'protocol.' + code) for code in w.get('protocols', [])),)),
        Section(tr(locale, 'nodes.rich.technical'), collapsed=True, lines=(lines[0], lines[4])),
    )
    if 'awg' in w.get('protocols', []):
        settings = w.get('settings', {})
        sections = (*sections[:-1], Section(tr(locale, 'protocol.awg'), tables=(summary_table((
            ('awg_i1_preset', settings.get('awg_i1_preset', 'quic')),
            ('awg_port', settings.get('awg_port') or tr(locale, 'nodes.awg.port_automatic')))),)), sections[-1])
    await render(bot, chat_id, Screen(tr(locale, 'node.wizard.summary.title'),
                 sections=sections, embedded_buttons=True, navigation=True),
                 rows, state, message_id)

@router.callback_query(F.data.startswith("wizard_proto:"))
async def wizard_proto_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    action = query.data.split(":")[1]
    data = await state.get_data()
    w = data.get('wizard_data', {})
    protocols = w.get('protocols', [])
    
    if action == "done":
        if not protocols:
            locale = normalize_locale(data.get('locale'))
            await render(bot, query.message.chat.id, Screen(tr(locale, 'node.wizard.protocols.title'),
                (tr(locale, 'node.wizard.protocols.invalid'),), embedded_buttons=True, navigation=True),
                [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data='wizard_proto:back')]],
                state, query.message.message_id)
            return
        await render_wizard_summary(query.message.chat.id, bot, state,
                                    query.message.message_id)
        return

    if action == 'back':
        await render_wizard_protocols(query.message.chat.id, bot, state,
                                      query.message.message_id)
        return

    if action not in {'xray', 'awg'}:
        return
    if action in protocols:
        protocols.remove(action)
    else:
        protocols.append(action)
    w['protocols'] = protocols
    await state.update_data(wizard_data=w)
    await render_wizard_protocols(query.message.chat.id, bot, state,
                                  query.message.message_id)


@router.callback_query(F.data == 'wizard_save')
async def wizard_save_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                         state: FSMContext):
    await query.answer()
    if await state.get_state() != NodeDraftState.waiting_for_summary.state:
        return
    data = await state.get_data()
    w = data.get('wizard_data', {})
    locale = normalize_locale(data.get('locale'))
    required = {'key', 'title', 'region', 'flag', 'transport', 'public_host', 'protocols'}
    if not required.issubset(w) or not w['protocols'] or (
            w['transport'] == 'ssh' and not w.get('ssh_target')):
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'node.wizard.summary.title'),
                (tr(locale, 'node.wizard.incomplete'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data='wizard_proto:back')]],
            state, query.message.message_id)
        return
    try:
        created = await backend.create_node(query.from_user.id, {
            **({'template': w['template']} if w.get('template') not in {None, 'custom'} else {}),
            'key': w['key'], 'title': w['title'], 'region': w['region'],
            'flag': w['flag'], 'protocols': w['protocols'],
            'xray_transports': (w.get('xray_transports') or ['tcp', 'xhttp']) if 'xray' in w['protocols'] else [],
            'settings': {**w.get('settings', {}), 'public_host': w['public_host']},
            'transport': w['transport'], 'ssh_target': w.get('ssh_target'),
        }, command_key=data['create_node_command_key'])
    except BackendError as exc:
        if exc.code == 'local_node_exists':
            await state.update_data(wizard_local_available=False)
            await render_wizard_transport(query.message.chat.id, bot, state, query.message.message_id)
            return
        error_key = ('node.wizard.local_exists' if exc.code == 'local_node_exists' else 'node.wizard.duplicate' if exc.status == 409 else
                     'node.wizard.unavailable' if exc.status == 503 else
                     'node.wizard.save_failed')
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'node.wizard.summary.title'),
                (tr(locale, error_key),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data='wizard_proto:back')]],
            state, query.message.message_id)
        return
    if isinstance(created, dict) and isinstance(created.get('key'), str):
        w.update({field: created[field] for field in ('key', 'title', 'region', 'flag')})
    await state.set_state(None)
    await state.update_data(wizard_saved=True, wizard_data=w)
    rows = [[InlineKeyboardButton(text=tr(locale, 'node.wizard.setup_agent'),
        callback_data='wizard_setup_agent', style='primary')],
        [InlineKeyboardButton(text=tr(locale, 'node.wizard.open_card'),
        callback_data=AdminNodeCallback(node_key=w['key']).pack())],
        [InlineKeyboardButton(text=tr(locale, 'nodes.card.to_list'),
        callback_data=AdminNodesCallback().pack())]]
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'node.wizard.created_title'),
            (tr(locale, 'node.wizard.created', name=w['title']),
             tr(locale, 'node.wizard.not_installed')), embedded_buttons=True, navigation=True),
        rows, state, query.message.message_id)


@router.callback_query(F.data == 'wizard_setup_agent')
async def wizard_setup_agent_cb(query: CallbackQuery, bot: Bot,
                                backend: BackendClient, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    w = data.get('wizard_data', {})
    if not data.get('wizard_saved') or not w.get('key'):
        return
    node = await backend.request('GET', f"/api/v1/nodes/{w['key']}", telegram_user_id=query.from_user.id)
    await remember_node(state, node)
    if node.get('transport') not in {'local', 'ssh'}:
        return
    await state.update_data(rollout_node_key=w['key'], rollout_ssh_target=node.get('ssh_target'))
    await queue_rollout(query.message.chat.id, query.from_user.id,
        query.message.message_id, w['key'], node['transport'], bot, backend,
        state, node.get('ssh_target'))

async def show_admin_node(chat_id, user_id, message_id, node_key, bot, backend, state):
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
        await remember_node(state, node)
    except BackendError:
        await render(bot, chat_id, Screen(tr(locale, 'nodes.card.unavailable'),
            (tr(locale, 'nodes.card.retry'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'nodes.card.to_list'),
                callback_data=AdminNodesCallback().pack())]], state, message_id)
        return
    try:
        overview = await backend.node_overview(user_id, node_key)
    except BackendError:
        overview = None

    if overview and overview.get('removal_status') in {'queued', 'running', 'blocked'}:
        title = f"{node.get('region') or tr(locale, 'nodes.region_unknown')} · {server_label(node)}"
        await render(bot, chat_id, Screen(title,
            (tr(locale, 'nodes.card.state', value=tr(locale, 'nodes.card.state.' + overview['state'])),),
            embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'nodes.card.deletion_status'),
                callback_data=f'remove_progress:{node_key}', style='primary')],
             [InlineKeyboardButton(text=tr(locale, 'nodes.card.to_list'),
                callback_data=AdminNodesCallback().pack())]], state, message_id)
        return

    draft = (await state.get_data()).get('node_settings_draft')
    pending = bool(node['applied_revision'] and node['desired_revision'] > node['applied_revision'] or
        draft and draft['node_key'] == node_key and draft['values'] != draft['baseline'])
    lines = []
    sections = []
    install_rows = []
    if overview:
        state_key = overview.get('state', 'unknown')
        if state_key != 'applied_unverified':
            lines.append(tr(locale, 'nodes.card.state', value=tr(locale, 'nodes.card.state.' + state_key)))
        job = overview.get('last_job')
        if job and job['status'] in {'awaiting_executor', 'running', 'blocked'}:
            install_rows.append((InlineKeyboardButton(text=tr(locale, 'node_tools.last_operation'), callback_data='node_job:' + job['id'], style='primary'),))
        if not node['applied_revision']:
            install_rows.append((InlineKeyboardButton(text=tr(locale, 'nodes.card.bootstrap'), callback_data=f'bootstrap_menu:{node_key}', style='primary'),))
        sections.append(Section(tr(locale, 'nodes.rich.access'), tables=(Table(
            (tr(locale, 'admin.rich.item'), tr(locale, 'admin.rich.value')),
            ((tr(locale, 'nodes.rich.ready'), str(overview['ready'])),
             (tr(locale, 'nodes.rich.pending_access'), str(overview['pending'])),
             (tr(locale, 'nodes.rich.failed'), str(overview['failed'] + overview['attention'])))),)))
    else:
        lines.append(tr(locale, 'nodes.card.summary_unavailable'))
    install_rows.append((InlineKeyboardButton(text=tr(locale, 'nodes.card.probe'), callback_data=ProbeNodeCallback(node_key=node_key).pack()),))
    sections.insert(0, Section(tr(locale, 'nodes.rich.installation'), rows=tuple(install_rows)))
    sections.insert(1, Section(tr(locale, 'nodes.rich.configuration'),
        (tr(locale, 'nodes.rich.pending'),) if pending else (),
        rows=((InlineKeyboardButton(text=tr(locale, 'nodes.card.settings'), callback_data=NodeSettingsCallback(node_key=node_key).pack(), style='primary' if pending else None),),)))
    if node.get('notes'):
        sections.append(Section(tr(locale, 'nodes.settings.field.notes'), (node['notes'],), collapsed=True))
    title = f"{node.get('region') or tr(locale, 'nodes.region_unknown')} · {server_label(node)}"
    await render(bot, chat_id, Screen(title, tuple(lines), sections=tuple(sections), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'nodes.rich.manage'), callback_data=f'node_manage:{node_key}', style='link')],
         [InlineKeyboardButton(text=tr(locale, 'nodes.card.to_list'), callback_data=AdminNodesCallback().pack())]], state, message_id)


@router.callback_query(F.data.startswith('node_manage:'))
async def node_manage_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    node_key = query.data.split(':', 1)[1]
    locale = normalize_locale((await state.get_data()).get('locale'))
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=query.from_user.id)
    await remember_node(state, node)
    await render(bot, query.message.chat.id, Screen(tr(locale, 'nodes.rich.manage'), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'nodes.draft.reinstall' if node['applied_revision'] else 'nodes.card.bootstrap'), callback_data=f'bootstrap_menu:{node_key}', style='primary')],
         [InlineKeyboardButton(text=tr(locale, 'nodes.maintenance.title'), callback_data=NodeMaintenanceCallback(node_key=node_key).pack())],
         [InlineKeyboardButton(text=tr(locale, 'nodes.rich.technical'), callback_data=f'node_technical:{node_key}')],
         [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminNodeCallback(node_key=node_key).pack())]], state, query.message.message_id)


@router.callback_query(F.data.startswith('node_technical:'))
async def node_technical_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    node_key = query.data.split(':', 1)[1]
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=query.from_user.id)
    await remember_node(state, node)
    overview = await backend.node_overview(query.from_user.id, node_key)
    locale = normalize_locale((await state.get_data()).get('locale'))
    lines = (tr(locale, 'nodes.card.agent_transport', value=node.get('transport') or '—'),
        tr(locale, 'nodes.card.ssh_target', value=node.get('ssh_target') or '—'),
        tr(locale, 'nodes.card.revisions', applied=node['applied_revision'], desired=node['desired_revision']))
    rows = [[InlineKeyboardButton(text=tr(locale, 'node_tools.diagnostics'), callback_data=f'node_tools:{node_key}')]]
    if overview.get('last_job'):
        rows.append([InlineKeyboardButton(text=tr(locale, 'node_tools.last_operation'), callback_data='node_job:' + overview['last_job']['id'])])
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data=f'node_manage:{node_key}')])
    await render(bot, query.message.chat.id, Screen(tr(locale, 'nodes.rich.technical'), lines,
        details_title=tr(locale, 'nodes.rich.identifiers'), details_lines=(node['key'],),
        embedded_buttons=True, navigation=True), rows, state, query.message.message_id)

@router.callback_query(AdminNodeCallback.filter())
async def admin_node_cb(query: CallbackQuery, callback_data: AdminNodeCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await _clear_node_flow(state)
    await show_admin_node(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

@router.callback_query(NodeSettingsCallback.filter())
async def node_settings_cb(query: CallbackQuery, callback_data: NodeSettingsCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await _clear_node_flow(state)
    await show_node_settings(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

async def show_node_settings(chat_id, user_id, message_id, node_key, bot, backend, state):
    node = await editable_node(user_id, node_key, backend, state)
    locale = normalize_locale((await state.get_data()).get('locale'))
    await state.set_state(None)
    await state.update_data(node_settings_view='root')
    controls = await draft_controls(node, state, locale)
    sections = ([Section(tr(locale, 'nodes.rich.configuration'),
        (tr(locale, 'nodes.rich.pending'),),
        rows=tuple(tuple(row) for row in controls))] if controls else []) + [
        Section(tr(locale, 'node_tools.general'), rows=((InlineKeyboardButton(text=tr(locale, 'profile.layout.edit'), callback_data=f'node_section:general:{node_key}'),),)),
        Section(tr(locale, 'nodes.rich.connection'), rows=((InlineKeyboardButton(text=tr(locale, 'profile.layout.edit'), callback_data=f'node_section:connection:{node_key}'),),)),
        Section(tr(locale, 'nodes.draft.protocol_settings'), rows=(tuple(
            InlineKeyboardButton(text=tr(locale, 'protocol.' + protocol), callback_data=f'node_section:{protocol}:{node_key}') for protocol in node['protocols']),
            (InlineKeyboardButton(text=tr(locale, 'nodes.rich.protocol_options'), callback_data=NodeProtocolsCallback(node_key=node_key).pack()),)))]
    await render(bot, chat_id, Screen(tr(locale, 'nodes.settings.title'), sections=tuple(sections), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'nodes.card.back_to_server'), callback_data=AdminNodeCallback(node_key=node_key).pack())]], state, message_id)


async def show_node_connection(chat_id, user_id, message_id, node_key, bot, backend, state):
    locale = normalize_locale((await state.get_data()).get('locale'))
    node = await editable_node(user_id, node_key, backend, state)
    await state.update_data(node_settings_view='channel')
    transport = node.get('transport')
    rows = [[InlineKeyboardButton(text=tr(locale, 'nodes.connection.local'),
        callback_data=f'node_connection_set:{node_key}:local', style='primary' if transport == 'local' else None),
        InlineKeyboardButton(text=tr(locale, 'nodes.connection.ssh'),
        callback_data=f'node_connection_set:{node_key}:ssh', style='primary' if transport == 'ssh' else None)]]
    if transport == 'ssh':
        rows.append([InlineKeyboardButton(text=tr(locale, 'nodes.connection.edit_target'),
            callback_data=EditNodeFieldCallback(node_key=node_key, field='ssh_target').pack())])
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=f'node_section:connection:{node_key}')])
    controls = await draft_controls(node, state, locale)
    await render(bot, chat_id, Screen(tr(locale, 'nodes.connection.title'),
        sections=(Section(tr(locale, 'nodes.rich.connection'), tables=(Table(
            (tr(locale, 'account.rich.field'), tr(locale, 'account.rich.value')),
            ((tr(locale, 'nodes.settings.field.transport'), tr(locale, 'node.wizard.transport.value.' + transport) if transport in {'ssh', 'local'} else '—'),
             (tr(locale, 'nodes.settings.field.ssh_target'), node.get('ssh_target') or '—'))),),
            rows=tuple(tuple(row) for row in rows[:-1])),
            Section(tr(locale, 'nodes.rich.about_connection'), collapsed=True,
                lines=(tr(locale, 'nodes.connection.note'),))),
        embedded_buttons=True, navigation=True), controls + rows[-1:], state, message_id)


@router.callback_query(F.data.startswith('node_connection:'))
async def node_connection_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                             state: FSMContext):
    await query.answer()
    await show_node_connection(query.message.chat.id, query.from_user.id,
        query.message.message_id, query.data.split(':', 1)[1], bot, backend, state)


@router.callback_query(F.data.startswith('node_connection_set:'))
async def node_connection_set_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                                 state: FSMContext):
    await query.answer()
    _, node_key, transport = query.data.split(':', 2)
    if transport not in {'local', 'ssh'}:
        return
    node = await editable_node(query.from_user.id, node_key, backend, state)
    if transport == 'ssh' and not node.get('ssh_target'):
        await _prompt_node_ssh_target(query.message.chat.id, query.message.message_id,
            node_key, node, bot, state, switch_transport=True)
        return
    body = {'transport': transport, 'ssh_target': node['ssh_target'] if transport == 'ssh' else None}
    try:
        await change_node_draft(query.from_user.id, node_key, body, backend, state)
    except BackendError:
        locale = normalize_locale((await state.get_data()).get('locale'))
        await render(bot, query.message.chat.id, Screen(tr(locale, 'nodes.connection.title'),
            (tr(locale, 'nodes.settings.save_failed'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data=f'node_connection:{node_key}')]], state, query.message.message_id)
        return
    await show_node_connection(query.message.chat.id, query.from_user.id,
        query.message.message_id, node_key, bot, backend, state)


async def _prompt_node_ssh_target(chat_id, message_id, node_key, node, bot, state,
                                  *, switch_transport=False):
    locale = normalize_locale((await state.get_data()).get('locale'))
    await state.set_state(NodeEditState.waiting_for_value)
    await state.update_data(edit_node_key=node_key, edit_field='ssh_target',
        edit_revision=node['desired_revision'], edit_settings=node['settings'],
        edit_switch_transport=switch_transport, edit_command_key=str(uuid4()))
    await render(bot, chat_id, Screen(tr(locale, 'nodes.connection.target_title'),
        (tr(locale, 'nodes.connection.target', value=node.get('ssh_target') or '—'),
         tr(locale, 'nodes.connection.target_prompt')), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=f'node_connection:{node_key}')]], state, message_id)

@router.callback_query(EditNodeFieldCallback.filter())
async def edit_node_field_cb(query: CallbackQuery, callback_data: EditNodeFieldCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    node_key, field = callback_data.node_key, callback_data.field
    allowed = {'title', 'region', 'flag', 'notes', 'public_host', 'awg_port', 'awg_public_host', 'awg_interface', 'awg_i1_preset', 'xray_host', 'xray_fingerprint', 'xray_sni', 'xray_tcp_port', 'xray_xhttp_port', 'xray_xhttp_path', 'ssh_target'}
    if field not in allowed:
        await show_node_settings(query.message.chat.id, user_id, query.message.message_id, node_key, bot, backend, state)
        return
        
    node = await editable_node(user_id, node_key, backend, state)
    await change_node_draft(user_id, node_key, {}, backend, state, baseline_node=node)
    if field == 'ssh_target':
        await _prompt_node_ssh_target(query.message.chat.id, query.message.message_id,
            node_key, node, bot, state)
        return
    await state.set_state(NodeEditState.waiting_for_value)
    await state.update_data(edit_node_key=node_key, edit_field=field,
        edit_revision=node['desired_revision'], edit_settings=node['settings'],
        edit_command_key=str(uuid4()))
    
    current = node.get(field) if field in {'title', 'region', 'flag', 'notes'} else node['settings'].get(field)
    locale = normalize_locale((await state.get_data()).get('locale'))
    if field == 'region':
        await state.set_state(None)
        choices = [InlineKeyboardButton(text=f'{icon} {tr(locale, "region." + token)}',
            callback_data=f'node_region:{token}:{node_key}', style='primary' if current == name else None)
            for token, icon, name in REGION_PRESETS]
        rows = [choices[index:index + 2] for index in range(0, len(choices), 2)]
        rows += [[InlineKeyboardButton(text=tr(locale, 'region.other'), callback_data=f'node_region:other:{node_key}')],
                 [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=f'node_section:general:{node_key}')]]
        await render(bot, query.message.chat.id, Screen(tr(locale, 'nodes.settings.field.region'),
            (tr(locale, 'region.choose'),), embedded_buttons=True, navigation=True), rows, state, query.message.message_id)
        return
    if field == 'awg_i1_preset':
        choices = [InlineKeyboardButton(text=label,
                    callback_data=f'node_awg_preset:{preset}:{node_key}',
                    style='primary' if preset == (current or 'quic') else None)
                for preset, label in (('quic', 'QUIC'), ('dns', 'DNS'), ('chaos', 'Chaos'))]
        rows = [choices[:2], choices[2:]]
        rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=f'node_section:awg:{node_key}')])
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'nodes.settings.field.awg_i1_preset'),
                embedded_buttons=True, navigation=True), rows, state, query.message.message_id)
        return
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'nodes.settings.edit_title',
                field=tr(locale, 'nodes.settings.field.' + field)),
            (tr(locale, 'nodes.settings.send_value'),), sections=(
                Section(tr(locale, 'nodes.rich.current_value'), lines=(str(current or '—'),)),),
            embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=f'node_section:{(await state.get_data()).get("edit_section", "general")}:{node_key}')]],
        state, query.message.message_id)


@router.callback_query(F.data.startswith('node_region:'))
async def edit_node_region_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    _, token, node_key = query.data.split(':', 2)
    data = await state.get_data()
    if data.get('edit_node_key') != node_key or data.get('edit_field') != 'region':
        return
    locale = normalize_locale(data.get('locale'))
    if token == 'other':
        await state.set_state(NodeEditState.waiting_for_value)
        await render(bot, query.message.chat.id, Screen(tr(locale, 'nodes.settings.field.region'),
            (tr(locale, 'region.custom'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=EditNodeFieldCallback(node_key=node_key, field='region').pack())]], state, query.message.message_id)
        return
    name = next((name for code, _, name in REGION_PRESETS if code == token), None)
    if name is None:
        return
    await change_node_draft(query.from_user.id, node_key, {'region': name}, backend, state)
    from .admin_node_tools import show_section
    await show_section(query.message.chat.id, query.from_user.id, query.message.message_id, 'general', node_key, bot, backend, state)

@router.callback_query(F.data.startswith('node_awg_preset:'))
async def select_awg_preset(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    _, preset, node_key = query.data.split(':', 2)
    if preset not in {'quic', 'dns', 'chaos'}:
        return
    node = await editable_node(query.from_user.id, node_key, backend, state)
    settings = {**node['settings'], 'awg_i1_preset': preset, 'awg_port_mode': 'auto'}
    if preset != node['settings'].get('awg_i1_preset') or node['settings'].get('awg_port_mode') != 'auto':
        settings.pop('awg_port', None)
    await change_node_draft(query.from_user.id, node_key,
        {'settings': settings}, backend, state)
    await _clear_node_flow(state)
    from .admin_node_tools import show_section
    await show_section(query.message.chat.id, query.from_user.id, query.message.message_id,
        'awg', node_key, bot, backend, state)

@router.message(NodeEditState.waiting_for_value, F.text)
async def process_node_edit(message: Message, bot: Bot, backend: BackendClient, state: FSMContext):
    await message.delete()
    if message.from_user is None or message.chat.type != 'private': return
    user_id, value = message.from_user.id, message.text.strip()
    data = await state.get_data()
    node_key, field, revision, settings, message_id = data['edit_node_key'], data['edit_field'], data['edit_revision'], data['edit_settings'], data.get('control_message_id')
    
    if value == '.':
        from .admin_node_tools import show_section
        await state.set_state(None)
        await show_section(message.chat.id, user_id, message_id, data.get('edit_section', 'general'), node_key, bot, backend, state)
        return
    if field.endswith('_port'):
        if not value.isdecimal() or not 1 <= int(value) <= 65535:
            locale = normalize_locale(data.get('locale'))
            await render(bot, message.chat.id, Screen(tr(locale, 'nodes.settings.invalid_port'),
                (tr(locale, 'nodes.settings.port_range'),), embedded_buttons=True, navigation=True),
                [[InlineKeyboardButton(text=tr(locale, 'back'),
                    callback_data=f'node_section:{(await state.get_data()).get("edit_section", "general")}:{node_key}')]], state, message_id)
            return
        parsed = int(value)
    else: parsed = value
    
    if field == 'ssh_target':
        if not re.fullmatch(r'(?:[A-Za-z_][A-Za-z0-9._-]*@)?(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])', value):
            locale = normalize_locale(data.get('locale'))
            await render(bot, message.chat.id, Screen(tr(locale, 'nodes.connection.target_title'),
                (tr(locale, 'nodes.connection.target_invalid'),), embedded_buttons=True, navigation=True),
                [[InlineKeyboardButton(text=tr(locale, 'back'),
                    callback_data=f'node_connection:{node_key}')]], state, message_id)
            return
        body = {'ssh_target': value}
        if data.get('edit_switch_transport'):
            body['transport'] = 'ssh'
    else:
        body = ({field: parsed} if field in {'title', 'region', 'flag', 'notes'} else {'settings': {**settings, field: parsed}})
        if field == 'awg_port':
            body['settings']['awg_port_mode'] = 'manual'
    try:
        await change_node_draft(user_id, node_key, body, backend, state)
        await _clear_node_flow(state)
        if field == 'ssh_target':
            await show_node_connection(message.chat.id, user_id, message_id,
                                       node_key, bot, backend, state)
        else:
            from .admin_node_tools import show_section
            await show_section(message.chat.id, user_id, message_id, data.get('edit_section', 'general'), node_key, bot, backend, state)
    except BackendError as exc:
        locale = normalize_locale(data.get('locale'))
        callback = (f'node_connection:{node_key}' if field == 'ssh_target' else
                    NodeSettingsCallback(node_key=node_key).pack())
        await render(bot, message.chat.id, Screen(tr(locale, 'nodes.settings.save_failed_title'),
            (tr(locale, 'nodes.settings.save_failed'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=callback)]],
            state, message_id)

@router.callback_query(NodeProtocolsCallback.filter())
async def node_protocols_cb(query: CallbackQuery, callback_data: NodeProtocolsCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await show_node_protocols(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

async def show_node_protocols(chat_id, user_id, message_id, node_key, bot, backend, state):
    locale = normalize_locale((await state.get_data()).get('locale'))
    node = await editable_node(user_id, node_key, backend, state)
    await state.set_state(None)
    await state.update_data(node_settings_view='protocols')
    enabled, transports = set(node['protocols']), set(node['xray_transports'])
    rows = [[InlineKeyboardButton(text=tr(locale, 'protocol.' + kind), style='primary' if kind in enabled else None,
        callback_data=ToggleNodeProtocolCallback(node_key=node_key, kind=kind).pack()) for kind in ('awg', 'xray')]]
    if 'xray' in enabled:
        rows += [[InlineKeyboardButton(text=kind.upper(), style='primary' if kind in transports else None,
            callback_data=ToggleNodeTransportCallback(node_key=node_key, kind=kind).pack()) for kind in ('tcp', 'xhttp')]]
    rows.extend(await draft_controls(node, state, locale))
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data=NodeSettingsCallback(node_key=node_key).pack())])
    await render(bot, chat_id, Screen(tr(locale, 'nodes.protocols.title'),
        (tr(locale, 'nodes.draft.protocol_note'),), details_title=tr(locale, 'nodes.rich.advanced'),
        details_lines=(tr(locale, 'nodes.protocols.grants_note'),), embedded_buttons=True, navigation=True),
        rows, state, message_id)

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
    node = await editable_node(user_id, node_key, backend, state)
    protocols, transports = set(node['protocols']), set(node['xray_transports'])
    selected = transports if transport else protocols
    selected.symmetric_difference_update({kind})
    if not protocols:
        locale = normalize_locale((await state.get_data()).get('locale'))
        await render(bot, chat_id, Screen(tr(locale, 'nodes.protocols.required_title'),
            (tr(locale, 'nodes.protocols.required'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data=NodeProtocolsCallback(node_key=node_key).pack())]],
            state, message_id)
        return
    if 'xray' not in protocols: transports.clear()
    elif not transports:
        if transport:
            locale = normalize_locale((await state.get_data()).get('locale'))
            await queryless_protocol_error(chat_id, message_id, node_key, bot, state, locale)
            return
        transports.update({'tcp', 'xhttp'})
    
    settings = dict(node['settings'])
    if 'awg' in protocols: settings.setdefault('awg_port', 51820)
    if 'xray' in protocols:
        for f, d in {'xray_sni': 'www.cloudflare.com', 'xray_tcp_port': 443, 'xray_xhttp_port': 8443, 'xray_xhttp_path': '/assets'}.items():
            settings.setdefault(f, d)
            
    await change_node_draft(user_id, node_key,
        {'protocols': sorted(protocols), 'xray_transports': sorted(transports),
         'settings': settings}, backend, state)
    await show_node_protocols(chat_id, user_id, message_id, node_key, bot, backend, state)


async def queryless_protocol_error(chat_id, message_id, node_key, bot, state, locale):
    await render(bot, chat_id, Screen(tr(locale, 'nodes.protocols.required_title'),
        (tr(locale, 'nodes.draft.transport_required'),), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=NodeProtocolsCallback(node_key=node_key).pack())]], state, message_id)

@router.callback_query(ProbeNodeCallback.filter())
async def probe_node_cb(query: CallbackQuery, callback_data: ProbeNodeCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    node_key = callback_data.node_key
    locale = normalize_locale((await state.get_data()).get('locale'))
    back = [[InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=AdminNodeCallback(node_key=node_key).pack())]]
    try:
        observation = await backend.node_runtime(user_id, node_key)
        agent_state = observation['health_state']
        if agent_state == 'degraded' and not observation.get('runtime_version'):
            # Older agents used absence of the runtime directory as agent health.
            agent_state = 'running'
        if agent_state not in {'running', 'degraded'}:
            agent_state = 'unknown'
        lines = (tr(locale, 'nodes.probe.agent', value=tr(locale, 'nodes.probe.state.' + agent_state)),
            tr(locale, 'nodes.probe.agent_version', value=observation.get('agent_version') or '—'),
            tr(locale, 'nodes.probe.version', value=observation.get('runtime_version') or '—'),
            tr(locale, 'nodes.probe.xray', value=tr(locale,
                'nodes.probe.present' if observation['xray_config_present'] else 'nodes.probe.missing')),
            tr(locale, 'nodes.probe.awg', value=tr(locale,
                'nodes.probe.present' if observation['awg_config_present'] else 'nodes.probe.missing')),
        )
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'nodes.probe.title'), (lines[0],), sections=(
                Section(tr(locale, 'nodes.rich.services'), lines=lines[3:5]),
                Section(tr(locale, 'nodes.rich.technical'), collapsed=True, lines=lines[1:3]),
            ), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'nodes.diagnostics.open'),
                callback_data=f'node_diagnostics:{node_key}')], *back], state,
            query.message.message_id)
    except BackendError as exc:
        cause = exc.code if exc.code in {'node_agent_unconfigured',
            'node_agent_unavailable', 'driver_unavailable'} else 'unknown'
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'nodes.probe.failed_title'),
                (tr(locale, 'nodes.probe.error.' + cause),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'nodes.card.bootstrap'),
                callback_data=f'bootstrap_menu:{node_key}')], *back],
            state, query.message.message_id)

@router.callback_query(F.data.startswith('node_diagnostics:'))
async def node_diagnostics_cb(query: CallbackQuery, bot: Bot,
                              backend: BackendClient, state: FSMContext):
    await query.answer()
    node_key = query.data.split(':', 1)[1]
    locale = normalize_locale((await state.get_data()).get('locale'))
    back = [InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=ProbeNodeCallback(node_key=node_key).pack())]
    try:
        result = await backend.node_diagnostics(query.from_user.id, node_key)
    except BackendError as exc:
        cause = exc.code if exc.code in {'node_agent_unconfigured',
            'node_agent_unavailable', 'driver_unavailable'} else 'unknown'
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'nodes.diagnostics.failed_title'),
                (tr(locale, 'nodes.probe.error.' + cause),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'nodes.card.bootstrap'),
                callback_data=f'bootstrap_menu:{node_key}')], back],
            state, query.message.message_id)
        return
    lines = [tr(locale, 'nodes.diagnostics.field.' + field,
        value=tr(locale, 'nodes.diagnostics.status.' + result[field]))
        for field in ('docker', 'runtime_root', 'xray_config', 'awg_config')]
    lines.append(tr(locale, 'nodes.diagnostics.version',
        value=result.get('runtime_version') or '—'))
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'nodes.diagnostics.title'), sections=(
            Section(tr(locale, 'nodes.rich.services'), lines=tuple(lines[:4])),
            Section(tr(locale, 'nodes.rich.technical'), collapsed=True, lines=tuple(lines[4:])),
        ), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'nodes.diagnostics.refresh'),
            callback_data=f'node_diagnostics:{node_key}')], back],
        state, query.message.message_id)

@router.callback_query(ApplyNodeCallback.filter())
async def apply_node_cb(query: CallbackQuery, callback_data: ApplyNodeCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await apply_node(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

async def apply_node(chat_id, user_id, message_id, node_key, bot, backend, state, revision=None):
    locale = normalize_locale((await state.get_data()).get('locale'))
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    await remember_node(state, node)
    if not node['applied_revision']:
        from .admin_node_tools import show_install
        await show_install(chat_id, user_id, message_id, node_key, bot, backend, state)
        return False
    if revision is None:
        revision = node['desired_revision']
    try:
        operation = await backend.apply_node_settings(user_id, node_key, revision)
    except BackendError:
        await render(bot, chat_id, Screen(tr(locale, 'nodes.apply.blocked_title'),
            (tr(locale, 'nodes.apply.queue_failed'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data=NodeSettingsCallback(node_key=node_key).pack())]],
            state, message_id)
        return False
    await render(bot, chat_id, Screen(tr(locale, 'nodes.apply.running_title'),
        (tr(locale, 'nodes.apply.running'),), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=NodeSettingsCallback(node_key=node_key).pack())]], state, message_id)
    
    for _ in range(30):
        try:
            s = await backend.node_settings_operation(user_id, operation['id'])
        except BackendError:
            break
        if s['status'] == 'succeeded':
            await show_node_settings(chat_id, user_id, message_id, node_key, bot, backend, state)
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
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        s = await backend.node_settings_operation(user_id, operation_id)
    except BackendError:
        await render(bot, chat_id, Screen(tr(locale, 'nodes.apply.blocked_title'),
            (tr(locale, 'nodes.apply.status_unavailable'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'nodes.apply.refresh'),
                callback_data=NodeApplyStatusCallback(operation_id=operation_id,
                    node_key=node_key).pack())],
             [InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data=NodeSettingsCallback(node_key=node_key).pack())]],
            state, message_id)
        return
    if s['status'] == 'succeeded':
        await show_node_settings(chat_id, user_id, message_id, node_key, bot, backend, state)
        return
    rows = [[InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=NodeSettingsCallback(node_key=node_key).pack())]]
    if s.get('error_code') == 'node_installation_required':
        rows.insert(0, [InlineKeyboardButton(text=tr(locale, 'nodes.card.bootstrap'),
            callback_data=f'bootstrap_menu:{node_key}', style='primary')])
        await render(bot, chat_id, Screen(tr(locale, 'node_tools.install'),
            (tr(locale, 'nodes.apply.installation_required'),), embedded_buttons=True, navigation=True),
            rows, state, message_id)
        return
    if s['status'] in {'blocked', 'superseded'}:
        await render(bot, chat_id, Screen(tr(locale, 'nodes.apply.blocked_title'),
            (tr(locale, 'nodes.apply.blocked'),), embedded_buttons=True, navigation=True), rows, state, message_id)
        return
    rows.insert(0, [InlineKeyboardButton(text=tr(locale, 'nodes.apply.refresh'),
        callback_data=NodeApplyStatusCallback(operation_id=operation_id, node_key=node_key).pack())])
    await render(bot, chat_id, Screen(tr(locale, 'nodes.apply.running_title'),
        (tr(locale, 'nodes.apply.pending'),), embedded_buttons=True, navigation=True), rows, state, message_id)

@router.callback_query(NodeMaintenanceCallback.filter())
async def node_maintenance_cb(query: CallbackQuery, callback_data: NodeMaintenanceCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await show_node_maintenance(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

async def show_node_maintenance(chat_id, user_id, message_id, node_key, bot, backend, state):
    await state.update_data(registry_removal_confirmation=None)
    locale = normalize_locale((await state.get_data()).get('locale'))
    st = await backend.node_maintenance(user_id, node_key)
    lines = [tr(locale, 'nodes.maintenance.state',
        value=tr(locale, 'nodes.maintenance.status.' + st['status']))]
    if st['status'] == 'draining':
        lines.extend((tr(locale, 'nodes.maintenance.pending', count=st['pending_tasks']),
            tr(locale, 'nodes.maintenance.blocked', count=st['blocked_tasks']),
            tr(locale, 'nodes.maintenance.phase', value=tr(locale,
                'nodes.maintenance.phase.' + (st['cleanup_phase'] or 'not_started')))))
    
    if st['status'] == 'active':
        rows = [[InlineKeyboardButton(text=tr(locale, 'nodes.maintenance.start'), callback_data=ConfirmNodeDrainCallback(node_key=node_key).pack(), style="danger")]]
    else:
        rows = [[InlineKeyboardButton(text=tr(locale, 'nodes.maintenance.refresh'), callback_data=f'remove_progress:{node_key}')]]
    rows.append([InlineKeyboardButton(text=tr(locale, 'nodes.maintenance.registry_only'),
        callback_data=ConfirmRegistryRemovalCallback(node_key=node_key).pack(), style='danger')])
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data=f'node_manage:{node_key}')])
    await render(bot, chat_id, Screen(tr(locale, 'nodes.maintenance.title'), tuple(lines),
        embedded_buttons=True, navigation=True), rows, state, message_id)

@router.callback_query(BindLocalCallback.filter())
async def bind_local_cb(query: CallbackQuery, callback_data: BindLocalCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    try:
        await backend.bind_verification_target(query.from_user.id, callback_data.node_key, 'local')
    except BackendError:
        await show_node_maintenance_error(query.message.chat.id, query.message.message_id,
            callback_data.node_key, bot, state)
        return
    await show_node_maintenance(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

async def show_node_maintenance_error(chat_id, message_id, node_key, bot, state):
    locale = normalize_locale((await state.get_data()).get('locale'))
    await render(bot, chat_id, Screen(tr(locale, 'nodes.maintenance.error_title'),
        (tr(locale, 'nodes.maintenance.error'),), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=NodeMaintenanceCallback(node_key=node_key).pack())]],
        state, message_id)

@router.callback_query(BindSshCallback.filter())
async def bind_ssh_cb(query: CallbackQuery, callback_data: BindSshCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await state.set_state(MaintenanceState.waiting_for_ssh_target)
    await state.update_data(maintenance_node_key=callback_data.node_key)
    locale = normalize_locale((await state.get_data()).get('locale'))
    await render(bot, query.message.chat.id, Screen(tr(locale, 'nodes.maintenance.ssh_title'),
        (tr(locale, 'nodes.maintenance.ssh_prompt'),
         tr(locale, 'nodes.maintenance.ssh_port_note')), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=NodeMaintenanceCallback(node_key=callback_data.node_key).pack())]],
        state, query.message.message_id)

@router.message(MaintenanceState.waiting_for_ssh_target, F.text)
async def process_maintenance_ssh(message: Message, bot: Bot, backend: BackendClient, state: FSMContext):
    if message.from_user is None or message.chat.type != 'private': return
    user_id = message.from_user.id
    data = await state.get_data()
    node_key, message_id = data['maintenance_node_key'], data.get('control_message_id')
    target = (message.text or '').strip()
    locale = normalize_locale(data.get('locale'))
    
    if not re.fullmatch(r'root@[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]', target):
        await render(bot, message.chat.id, Screen(tr(locale, 'nodes.maintenance.ssh_title'),
            (tr(locale, 'nodes.maintenance.ssh_invalid'),
             tr(locale, 'nodes.maintenance.ssh_port_note')), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data=NodeMaintenanceCallback(node_key=node_key).pack())]],
            state, message_id)
        return
        
    try:
        await backend.bind_verification_target(user_id, node_key, 'ssh', target)
        await _clear_node_flow(state)
        await show_node_maintenance(message.chat.id, user_id, message_id, node_key, bot, backend, state)
    except BackendError:
        await show_node_maintenance_error(message.chat.id, message_id, node_key,
                                          bot, state)

@router.callback_query(ConfirmNodeDrainCallback.filter())
async def confirm_node_drain_cb(query: CallbackQuery, callback_data: ConfirmNodeDrainCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    node_key = callback_data.node_key
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        maintenance = await backend.node_maintenance(query.from_user.id, node_key)
    except BackendError:
        await show_node_maintenance_error(query.message.chat.id, query.message.message_id,
            node_key, bot, state)
        return
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'nodes.maintenance.start'),
            (tr(locale, 'nodes.maintenance.node', key=node_key),
             '• ' + tr(locale, 'nodes.maintenance.drain_grants', count=maintenance['affected_profiles']),
             '• ' + tr(locale, 'nodes.maintenance.drain_runtime'),
             '• ' + tr(locale, 'nodes.maintenance.drain_agent'),
             '• ' + tr(locale, 'nodes.maintenance.drain_registry')), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'nodes.maintenance.drain_confirm'),
            callback_data=DrainNodeCallback(node_key=node_key).pack(), style='danger')],
         [InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=NodeMaintenanceCallback(node_key=node_key).pack())]],
        state, query.message.message_id)

@router.callback_query(DrainNodeCallback.filter())
async def drain_node_cb(query: CallbackQuery, callback_data: DrainNodeCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    from .admin_node_tools import advance_removal
    await advance_removal(query.message.chat.id, query.from_user.id, query.message.message_id,
                          callback_data.node_key, bot, backend, state)

@router.callback_query(CleanupStepCallback.filter())
async def cleanup_step_cb(query: CallbackQuery, callback_data: CleanupStepCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    try:
        await backend.cleanup_node_step(query.from_user.id, callback_data.node_key,
                                        callback_data.expected_phase)
    except BackendError:
        await show_node_maintenance_error(query.message.chat.id, query.message.message_id,
            callback_data.node_key, bot, state)
        return
    await show_node_maintenance(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

@router.callback_query(VerifyRetirementCallback.filter())
async def verify_retirement_cb(query: CallbackQuery, callback_data: VerifyRetirementCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    try:
        await backend.verify_and_retire_node(query.from_user.id, callback_data.node_key)
    except BackendError:
        await show_node_maintenance_error(query.message.chat.id, query.message.message_id,
            callback_data.node_key, bot, state)
        return
    locale = normalize_locale((await state.get_data()).get('locale'))
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'nodes.maintenance.removed_title'),
            (tr(locale, 'nodes.maintenance.removed_verified'),), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'nodes.maintenance.nodes'),
            callback_data=AdminNodesCallback().pack())]], state, query.message.message_id)

@router.callback_query(ConfirmRegistryRemovalCallback.filter())
async def confirm_registry_removal_cb(query: CallbackQuery, callback_data: ConfirmRegistryRemovalCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await show_registry_confirmation(query.message.chat.id, query.from_user.id,
        query.message.message_id, callback_data.node_key, bot, backend, state)


async def show_registry_confirmation(chat_id, user_id, message_id, node_key, bot, backend, state):
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
        await remember_node(state, node)
    except BackendError as exc:
        await state.update_data(registry_removal_confirmation=None)
        if exc.status == 404:
            await render(bot, chat_id, Screen(tr(locale, 'nodes.card.unavailable'),
                embedded_buttons=True, navigation=True),
                [[InlineKeyboardButton(text=tr(locale, 'nodes.card.to_list'), callback_data=AdminNodesCallback().pack())]],
                state, message_id)
        else:
            await show_node_maintenance_error(chat_id, message_id, node_key, bot, state)
        return
    await render(bot, chat_id,
        Screen(tr(locale, 'nodes.maintenance.registry_only'),
            (server_label(node),
             tr(locale, 'nodes.maintenance.registry_grants'),
             tr(locale, 'nodes.maintenance.registry_runtime'),
             tr(locale, 'nodes.maintenance.registry_tunnels'),
             tr(locale, 'nodes.maintenance.registry_use')), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'nodes.maintenance.registry_confirm'),
            callback_data=RetireRegistryCallback(node_key=node_key).pack(), style='danger')],
         [InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=NodeMaintenanceCallback(node_key=node_key).pack())]],
        state, message_id)
    await state.update_data(registry_removal_confirmation={
        'node_key': node_key,
        'message_id': (await state.get_data()).get('control_message_id') or message_id})

@router.callback_query(RetireRegistryCallback.filter())
async def retire_registry_cb(query: CallbackQuery, callback_data: RetireRegistryCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    confirmation = (await state.get_data()).get('registry_removal_confirmation')
    if confirmation != {'node_key': callback_data.node_key, 'message_id': query.message.message_id}:
        await show_registry_confirmation(query.message.chat.id, query.from_user.id,
            query.message.message_id, callback_data.node_key, bot, backend, state)
        return
    try:
        result = await backend.retire_node_registry_only(query.from_user.id,
                                                         callback_data.node_key)
    except BackendError as exc:
        locale = normalize_locale((await state.get_data()).get('locale'))
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'nodes.maintenance.error_title'),
                (tr(locale, 'nodes.maintenance.registry_busy' if exc.code == 'maintenance_busy'
                    else 'nodes.maintenance.registry_failed'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data=NodeMaintenanceCallback(node_key=callback_data.node_key).pack())]],
            state, query.message.message_id)
        return
    await state.update_data(registry_removal_confirmation=None)
    locale = normalize_locale((await state.get_data()).get('locale'))
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'nodes.maintenance.registry_removed_title'),
            (tr(locale, 'nodes.maintenance.node', key=result['node_key']),
             tr(locale, 'nodes.maintenance.registry_removed_note'),
             tr(locale, 'nodes.maintenance.registry_tunnels')), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'nodes.maintenance.nodes'),
            callback_data=AdminNodesCallback().pack())]], state, query.message.message_id)

@router.callback_query(RolloutLocalCallback.filter())
async def rollout_local_cb(query: CallbackQuery, callback_data: RolloutLocalCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    node_key = callback_data.node_key
    await state.update_data(rollout_node_key=node_key, rollout_ssh_target=None,
                            rollout_command_key=str(uuid4()))
    await queue_rollout(query.message.chat.id, user_id, query.message.message_id, node_key, 'local', bot, backend, state)

@router.callback_query(RolloutSshCallback.filter())
async def rollout_ssh_cb(query: CallbackQuery, callback_data: RolloutSshCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    node_key = callback_data.node_key
    await state.set_state(AgentDraftState.waiting_for_ssh_target)
    await state.update_data(rollout_node_key=node_key,
                            rollout_command_key=str(uuid4()))
    locale = normalize_locale((await state.get_data()).get('locale'))
    await render(bot, query.message.chat.id, Screen(tr(locale, 'nodes.rollout.ssh_title'), (tr(locale, 'nodes.rollout.ssh_prompt'),), embedded_buttons=True, navigation=True), [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminNodeCallback(node_key=node_key).pack())]], state, query.message.message_id)

@router.message(AgentDraftState.waiting_for_ssh_target, F.text)
async def process_agent_ssh(message: Message, bot: Bot, backend: BackendClient, state: FSMContext):
    if message.from_user is None or message.chat.type != 'private': return
    user_id = message.from_user.id
    target = (message.text or '').strip()
    data = await state.get_data()
    node_key, message_id = data['rollout_node_key'], data.get('control_message_id')
    
    if not re.fullmatch(r'(?:[A-Za-z_][A-Za-z0-9._-]*@)?[A-Za-z0-9.-]+', target):
        locale = normalize_locale(data.get('locale'))
        await render(bot, message.chat.id, Screen(tr(locale, 'nodes.rollout.ssh_title'), (tr(locale, 'nodes.rollout.ssh_prompt'),), embedded_buttons=True, navigation=True), [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminNodeCallback(node_key=node_key).pack())]], state, message_id)
        return
        
    await state.update_data(rollout_ssh_target=target)
    await queue_rollout(message.chat.id, user_id, message_id, node_key, 'ssh', bot, backend, state, target)

async def queue_rollout(chat_id, user_id, message_id, node_key, transport, bot, backend, state, ssh_target=None):
    try:
        data = await state.get_data()
        task = await backend.rollout_agent(user_id, node_key, transport,
            ssh_target=ssh_target,
            command_key=data.get('rollout_command_key') or str(uuid4()))
        await _clear_node_flow(state)
        await show_rollout_status(chat_id, user_id, message_id, task['id'], bot, backend, state)
    except BackendError as exc:
        locale = normalize_locale((await state.get_data()).get('locale'))
        await render(bot, chat_id, Screen(tr(locale, 'nodes.rollout.queue_failed'), (tr(locale, 'nodes.rollout.queue_failed_note'),), embedded_buttons=True, navigation=True), [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminNodeCallback(node_key=node_key).pack()), InlineKeyboardButton(text=tr(locale, 'node_tools.retry'), callback_data=RetryRolloutCallback().pack())]], state, message_id)

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
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        task = await backend.agent_rollout(user_id, task_id)
    except BackendError:
        await render(bot, chat_id, Screen(tr(locale, 'nodes.rollout.title'),
            (tr(locale, 'nodes.rollout.status_unavailable'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'nodes.rollout.refresh'),
                callback_data=RolloutStatusCallback(task_id=task_id).pack())]],
            state, message_id)
        return
    status = task['status'] if task['status'] in {'succeeded', 'blocked',
        'running', 'awaiting_executor'} else 'unknown'
    rows = [] if status in {'succeeded', 'blocked'} else [
        [InlineKeyboardButton(text=tr(locale, 'nodes.rollout.refresh'),
            callback_data=RolloutStatusCallback(task_id=task_id).pack())]]
    if status == 'blocked' and task.get('failure_code') == 'rust_required':
        rows.append([InlineKeyboardButton(text=tr(locale, 'nodes.rollout.install_rust'),
            callback_data=f"rust_offer:{task_id}", style='primary')])
    rows.append([InlineKeyboardButton(text=tr(locale, 'nodes.rollout.card'),
        callback_data=AdminNodeCallback(node_key=task['node_key']).pack())])
    if status == 'succeeded':
        from .user import button
        rows.append([button(user_id, tr(locale, 'nodes.rollout.main_menu'), 'admin_menu')])
    lines = [tr(locale, 'nodes.rollout.' + status)]
    for path in task.get('journal_archives', []):
        lines.append(tr(locale, 'nodes.rollout.journal_archived', path=path))
    if task.get('failure_code') == 'build_resources':
        lines.append(tr(locale, 'nodes.rollout.build_resources'))
    if task.get('failure_code') == 'ssh_prerequisites':
        lines.append(tr(locale, 'nodes.rollout.ssh_prerequisites'))
    if task.get('failure_code') in {'ssh_authentication', 'ssh_host_key'}:
        lines.append(tr(locale, 'nodes.rollout.' + task['failure_code']))
    if status in {'awaiting_executor', 'running'}:
        lines.append(tr(locale, 'nodes.rich.independent'))
    await render(bot, chat_id, Screen(tr(locale, 'nodes.rollout.title'),
        tuple(lines), sections=(Section(tr(locale, 'nodes.rich.technical'), collapsed=True,
            lines=(tr(locale, 'nodes.rich.operation_id', value=task_id),)),),
        embedded_buttons=True, navigation=True,
        breadcrumbs=parent_path('admin_node:' + task['node_key'], locale,
                                await state.get_data())), rows, state, message_id)


@router.callback_query(F.data.startswith('rust_offer:'))
async def rust_offer_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    task_id = query.data.split(':', 1)[1]
    task = await backend.agent_rollout(query.from_user.id, task_id)
    if task.get('status') != 'blocked' or task.get('failure_code') != 'rust_required':
        return await show_rollout_status(query.message.chat.id, query.from_user.id,
            query.message.message_id, task_id, bot, backend, state)
    locale = normalize_locale((await state.get_data()).get('locale'))
    await state.update_data(rust_rollout_task=task_id, rust_rollout_key=str(uuid4()))
    await render(bot, query.message.chat.id, Screen(tr(locale, 'nodes.rollout.install_rust'),
        (tr(locale, 'nodes.rollout.rust_note'),), embedded_buttons=True, navigation=True), [[
        InlineKeyboardButton(text=tr(locale, 'back'), callback_data=RolloutStatusCallback(task_id=task_id).pack()),
        InlineKeyboardButton(text=tr(locale, 'nodes.rollout.install_rust'), callback_data=f'rust_confirm:{task_id}', style='primary')]], state, query.message.message_id)


@router.callback_query(F.data.startswith('rust_confirm:'))
async def rust_confirm_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    task_id = query.data.split(':', 1)[1]
    data = await state.get_data()
    if data.get('rust_rollout_task') != task_id:
        return
    task = await backend.agent_rollout(query.from_user.id, task_id)
    node = await backend.request('GET', f"/api/v1/nodes/{task['node_key']}", telegram_user_id=query.from_user.id)
    await remember_node(state, node)
    queued = await backend.rollout_agent(query.from_user.id, task['node_key'], node['transport'],
        ssh_target=node.get('ssh_target'), command_key=data['rust_rollout_key'], install_rust=True)
    await show_rollout_status(query.message.chat.id, query.from_user.id,
        query.message.message_id, queued['id'], bot, backend, state)




@router.callback_query(F.data.startswith("bootstrap_menu:"))
async def bootstrap_menu_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    from .admin_node_tools import show_install
    await query.answer()
    await show_install(query.message.chat.id, query.from_user.id, query.message.message_id,
                       query.data.split(':', 1)[1], bot, backend, state)


@router.callback_query(F.data.startswith('rollout_saved:'))
async def rollout_saved_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                           state: FSMContext):
    await query.answer()
    node_key = query.data.split(':', 1)[1]
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=query.from_user.id)
    await remember_node(state, node)
    if node.get('transport') != 'ssh' or not node.get('ssh_target'):
        await show_admin_node(query.message.chat.id, query.from_user.id,
                              query.message.message_id, node_key, bot, backend, state)
        return
    await state.update_data(rollout_node_key=node_key, rollout_ssh_target=node['ssh_target'],
                            rollout_command_key=str(uuid4()))
    await queue_rollout(query.message.chat.id, query.from_user.id, query.message.message_id,
                        node_key, 'ssh', bot, backend, state, node['ssh_target'])


REGION_PRESETS = (
    ('europe', '🌍', 'Europe'), ('asia', '🌏', 'Asia'),
    ('north_america', '🌎', 'North America'), ('south_america', '🌎', 'South America'),
    ('africa', '🌍', 'Africa'), ('oceania', '🌏', 'Oceania'),
)


async def render_wizard_regions(chat_id, bot, state, message_id=None):
    await state.set_state(None)
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    current = data.get('wizard_data', {}).get('region')
    buttons = [InlineKeyboardButton(text=f"{flag} {tr(locale, 'region.' + token)}",
        callback_data='wizard_region:' + token, style='primary' if current == name else None)
        for token, flag, name in REGION_PRESETS]
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    rows += [[InlineKeyboardButton(text=tr(locale, 'region.other'), callback_data='wizard_region:other')],
             [InlineKeyboardButton(text=tr(locale, 'back'), callback_data='wizard_back:title')]]
    await render(bot, chat_id, Screen(tr(locale, 'node.wizard.region.title'),
        (tr(locale, 'region.choose'),), embedded_buttons=True, navigation=True), rows, state, message_id)


@router.callback_query(F.data.startswith('wizard_region:'))
async def wizard_region_cb(query: CallbackQuery, bot: Bot, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    if data.get('wizard_saved') or not data.get('wizard_data', {}).get('title'):
        return
    token = query.data.split(':', 1)[1]
    if token == 'other':
        await state.set_state(NodeDraftState.waiting_for_region)
        locale = normalize_locale(data.get('locale'))
        await render(bot, query.message.chat.id, Screen(tr(locale, 'node.wizard.region.title'),
            (tr(locale, 'region.custom'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data='wizard_back:region')]], state, query.message.message_id)
        return
    preset = next((p for p in REGION_PRESETS if p[0] == token), None)
    if preset is None:
        return
    await state.update_data(wizard_data={**data['wizard_data'], 'region': preset[2]})
    await render_wizard_step(query.message.chat.id, bot, state, 'flag', query.message.message_id)
