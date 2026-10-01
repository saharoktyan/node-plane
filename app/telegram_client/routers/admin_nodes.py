import re
from uuid import uuid4
from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from ..backend import BackendClient, BackendError
from ..screens import Screen
from ..i18n import normalize_locale, tr
from .common import render
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
        if key in {'locale', 'admin_node_search', 'admin_node_cursors', 'admin_node_page'}})

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
                                     search=search, limit=10)
    if page.get('next_cursor'):
        if len(cursors) == page_index + 1:
            cursors.append(page['next_cursor'])
        else:
            cursors[page_index + 1] = page['next_cursor']
    else:
        cursors = cursors[:page_index + 1]
    await state.update_data(admin_node_cursors=cursors, admin_node_page=page_index)
    rows = []
    for node in page['items']:
        summary = node.get('overview')
        label = f"{node['flag']} {node['title']}".strip()
        if summary:
            marker = '⚠️' if summary['state'] != 'applied_unverified' or summary['failed'] or summary['attention'] else '✅'
            label = tr(locale, 'node_tools.list_row', marker=marker, name=label, ready=summary['ready'], total=summary['access_total'])
        rows.append([InlineKeyboardButton(text=label, callback_data=AdminNodeCallback(node_key=node['key']).pack())])
    arrows = []
    if page_index > 0:
        arrows.append(InlineKeyboardButton(text='◀️', callback_data=f'admin_node_page:{page_index - 1}'))
    if page.get('next_cursor'):
        arrows.append(InlineKeyboardButton(text='▶️', callback_data=f'admin_node_page:{page_index + 1}'))
    if arrows:
        rows.append(arrows)
    rows.append([InlineKeyboardButton(text=tr(locale, 'nodes.admin.add'), callback_data=NewNodeCallback().pack())])
    rows.append([InlineKeyboardButton(text=tr(locale, 'nodes.admin.search'), callback_data='admin_node_search')])
    if search:
        rows.append([InlineKeyboardButton(text=tr(locale, 'nodes.admin.show_all'), callback_data='admin_node_all')])
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data='admin_menu')])
    lines = [tr(locale, 'nodes.admin.choose') if page['items'] else
             tr(locale, 'nodes.admin.no_results' if search else 'nodes.admin.empty')]
    if page_index or page.get('next_cursor'):
        lines.append(tr(locale, 'nodes.admin.page', number=page_index + 1))
    await render(bot, chat_id, Screen(tr(locale, 'nodes.admin.title'), tuple(lines)),
                 rows, state, message_id)


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
            (tr(locale, 'nodes.admin.search_prompt'),)),
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
                (tr(locale, 'nodes.admin.search_invalid'),)),
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
    await state.update_data(wizard_data={}, create_node_command_key=str(uuid4()),
                            rollout_command_key=str(uuid4()), wizard_saved=False)
    await render_wizard_step(query.message.chat.id, bot, state, 'key',
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
    'title': 'key', 'region': 'title', 'flag': 'region',
    'transport': 'flag', 'target': 'transport',
    'public_host': 'transport', 'protocols': 'public_host',
}


async def render_wizard_step(chat_id: int, bot: Bot, state: FSMContext,
                             step: str, message_id: int | None = None,
                             error: bool = False):
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
    if step == 'public_host' and data.get('wizard_data', {}).get('transport') == 'ssh':
        previous = 'target'
    if previous:
        navigation.append(InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=f'wizard_back:{previous}'))
    else:
        navigation.append(InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=AdminNodesCallback().pack()))
    if step == 'flag':
        navigation.append(InlineKeyboardButton(text=tr(locale, 'node.wizard.skip'),
            callback_data='wizard_skip_flag'))
    rows = [navigation]
    await render(bot, chat_id, Screen(tr(locale, f'node.wizard.{step}.title'),
                                       tuple(lines)), rows, state, message_id)


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
    await render_wizard_transport(query.message.chat.id, bot, state, query.message.message_id)

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
    await render_wizard_transport(message.chat.id, bot, state,
                                  data.get('control_message_id'))

async def render_wizard_transport(chat_id: int, bot: Bot, state: FSMContext, message_id: int | None = None):
    await state.set_state(NodeDraftState.waiting_for_transport)
    locale = normalize_locale((await state.get_data()).get('locale'))
    rows = [
        [InlineKeyboardButton(text=tr(locale, 'node.wizard.transport.ssh'), callback_data="wizard_transport:ssh")],
        [InlineKeyboardButton(text=tr(locale, 'node.wizard.transport.local'), callback_data="wizard_transport:local")],
        [InlineKeyboardButton(text=tr(locale, 'back'), callback_data='wizard_back:flag')]
    ]
    await render(bot, chat_id, Screen(tr(locale, 'node.wizard.transport.title'),
        (tr(locale, 'node.wizard.transport.prompt'),)), rows, state, message_id)

@router.callback_query(F.data.startswith("wizard_transport:"))
async def wizard_transport_cb(query: CallbackQuery, bot: Bot, state: FSMContext):
    await query.answer()
    transport = query.data.split(":")[1]
    if transport not in {'local', 'ssh'}:
        return
    data = await state.get_data()
    w = dict(data.get('wizard_data', {}))
    w['transport'] = transport
    await state.update_data(wizard_data=w)
    
    if transport == "ssh":
        await render_wizard_step(query.message.chat.id, bot, state, 'target',
                                 query.message.message_id)
    else:
        w['ssh_target'] = None
        await state.update_data(wizard_data=w)
        await render_wizard_step(query.message.chat.id, bot, state, 'public_host',
                                 query.message.message_id)

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
    
    def mark(code: str, label: str) -> str:
        return f">{label}<" if code in protocols else label

    rows = [
        [InlineKeyboardButton(text=mark("xray", tr(locale, 'protocol.xray')), callback_data="wizard_proto:xray")],
        [InlineKeyboardButton(text=mark("awg", tr(locale, 'protocol.awg')), callback_data="wizard_proto:awg")],
        [InlineKeyboardButton(text=tr(locale, 'back'), callback_data='wizard_back:public_host'),
         InlineKeyboardButton(text=tr(locale, 'node.wizard.review'), callback_data="wizard_proto:done")]
    ]
    await render(bot, chat_id, Screen(tr(locale, 'node.wizard.protocols.title'),
        (tr(locale, 'node.wizard.protocols.prompt'),
         tr(locale, 'node.wizard.protocols.xray_default'))), rows, state, message_id)


async def render_wizard_summary(chat_id: int, bot: Bot, state: FSMContext,
                                message_id: int | None = None):
    await state.set_state(NodeDraftState.waiting_for_summary)
    data = await state.get_data()
    w = data.get('wizard_data', {})
    locale = normalize_locale(data.get('locale'))
    transports = ', '.join(('tcp', 'xhttp')) if 'xray' in w.get('protocols', []) else '—'
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
             InlineKeyboardButton(text=tr(locale, 'node.wizard.save'), callback_data='wizard_save')]]
    await render(bot, chat_id, Screen(tr(locale, 'node.wizard.summary.title'), lines),
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
                (tr(locale, 'node.wizard.protocols.invalid'),)),
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
                (tr(locale, 'node.wizard.incomplete'),)),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data='wizard_proto:back')]],
            state, query.message.message_id)
        return
    try:
        await backend.create_node(query.from_user.id, {
            'key': w['key'], 'title': w['title'], 'region': w['region'],
            'flag': w['flag'], 'protocols': w['protocols'],
            'xray_transports': ['tcp', 'xhttp'] if 'xray' in w['protocols'] else [],
            'settings': {'public_host': w['public_host']},
            'transport': w['transport'], 'ssh_target': w.get('ssh_target'),
        }, command_key=data['create_node_command_key'])
    except BackendError as exc:
        error_key = ('node.wizard.duplicate' if exc.status == 409 else
                     'node.wizard.unavailable' if exc.status == 503 else
                     'node.wizard.save_failed')
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'node.wizard.summary.title'),
                (tr(locale, error_key),)),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data='wizard_proto:back')]],
            state, query.message.message_id)
        return
    await state.set_state(None)
    await state.update_data(wizard_saved=True)
    rows = [[InlineKeyboardButton(text=tr(locale, 'node.wizard.setup_agent'),
        callback_data='wizard_setup_agent')],
        [InlineKeyboardButton(text=tr(locale, 'node.wizard.open_card'),
        callback_data=AdminNodeCallback(node_key=w['key']).pack())],
        [InlineKeyboardButton(text=tr(locale, 'nodes.card.to_list'),
        callback_data=AdminNodesCallback().pack())]]
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'node.wizard.created_title'),
            (tr(locale, 'node.wizard.created', name=w['title']),
             tr(locale, 'node.wizard.not_installed'))),
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
    except BackendError:
        await render(bot, chat_id, Screen(tr(locale, 'nodes.card.unavailable'),
            (tr(locale, 'nodes.card.retry'),)),
            [[InlineKeyboardButton(text=tr(locale, 'nodes.card.to_list'),
                callback_data=AdminNodesCallback().pack())]], state, message_id)
        return
    try:
        overview = await backend.node_overview(user_id, node_key)
    except BackendError:
        overview = None

    rows = [
        [
            InlineKeyboardButton(text=tr(locale, 'nodes.card.probe'), callback_data=ProbeNodeCallback(node_key=node_key).pack()),
            InlineKeyboardButton(text=tr(locale, 'nodes.card.bootstrap'), callback_data=f"bootstrap_menu:{node_key}")
        ],
        [
            InlineKeyboardButton(text=tr(locale, 'nodes.card.settings'), callback_data=NodeSettingsCallback(node_key=node_key).pack())
        ],
        [InlineKeyboardButton(text=tr(locale, 'nodes.card.delete'), callback_data=NodeMaintenanceCallback(node_key=node_key).pack())],
        [InlineKeyboardButton(text=tr(locale, 'nodes.card.to_list'), callback_data=AdminNodesCallback().pack())]
    ]
    lines = [
        tr(locale, 'nodes.card.region', value=node.get('region', '—'), flag=node.get('flag', '')),
        tr(locale, 'nodes.card.protocols', value=', '.join(node.get('protocols', [])) or '—'),
        tr(locale, 'nodes.card.transports', value=', '.join(node.get('xray_transports', [])) or '—'),
        tr(locale, 'nodes.card.agent_transport', value=node.get('transport') or '—'),
        tr(locale, 'nodes.card.description')
    ]
    if node.get('ssh_target'):
        lines.insert(-1, tr(locale, 'nodes.card.ssh_target', value=node['ssh_target']))
    if overview:
        state_key = overview.get('state', 'unknown')
        if state_key not in {'not_installed', 'applying', 'needs_attention',
                             'changes_pending', 'inactive', 'applied_unverified'}:
            state_key = 'unknown'
        lines.insert(-1, tr(locale, 'nodes.card.state',
            value=tr(locale, 'nodes.card.state.' + state_key)))
        lines.insert(-1, tr(locale, 'nodes.card.revisions',
            applied=overview['applied_revision'], desired=overview['desired_revision']))
        lines.insert(-1, tr(locale, 'nodes.card.access',
            ready=overview['ready'], total=overview['access_total'],
            pending=overview['pending'], failed=overview['failed'],
            attention=overview['attention']))
        if not overview.get('settings_complete', True):
            lines.insert(-1, tr(locale, 'nodes.card.settings_incomplete'))
    else:
        lines.insert(-1, tr(locale, 'nodes.card.summary_unavailable'))
    lines.insert(-1, tr(locale, 'nodes.card.probe_note'))
    if node.get('notes'):
        lines.append(node['notes'])
    if node['settings'].get('public_host'):
        lines.append(tr(locale, 'nodes.settings.value', field=tr(locale, 'nodes.settings.field.public_host'), value=node['settings']['public_host']))
    try:
        facts = await backend.node_services(user_id, node_key)
        for protocol in node['protocols']:
            lines.append(tr(locale, 'node_tools.fact', name=tr(locale, 'node_tools.fact.' + protocol + '_running'),
                value=tr(locale, 'node_tools.yes' if facts[protocol + '_running'] else 'node_tools.no')))
        lines.append(tr(locale, 'node_tools.runtime_version', version=facts['runtime_version'] or '—', commit=facts['runtime_commit'] or '—'))
        if facts['runtime_drift']:
            lines.append(tr(locale, 'node_tools.drift'))
    except BackendError as exc:
        from .admin_node_tools import error
        lines.append(error(locale, exc))
    if overview and overview.get('last_job'):
        job = overview['last_job']
        rows.insert(-1, [InlineKeyboardButton(text=tr(locale, 'node_tools.last_operation'), callback_data='node_job:' + job['id'])])
    await render(bot, chat_id, Screen(tr(locale, 'nodes.card.title', name=node.get('title')),
                                       tuple(lines)), rows, state, message_id)

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
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    settings = node['settings']
    locale = normalize_locale((await state.get_data()).get('locale'))
    await state.set_state(None)
    rows = [[InlineKeyboardButton(text=tr(locale, 'node_tools.general'), callback_data=f'node_section:general:{node_key}'),
             InlineKeyboardButton(text=tr(locale, 'node_tools.maintenance'), callback_data=f'node_tools:{node_key}')]]
    protocol_row = [InlineKeyboardButton(text=tr(locale, 'node_tools.' + protocol),
        callback_data=f'node_section:{protocol}:{node_key}') for protocol in ('xray', 'awg') if protocol in node['protocols']]
    if protocol_row:
        rows.append(protocol_row)
    rows += [[InlineKeyboardButton(text=tr(locale, 'nodes.card.apply'), callback_data=ApplyNodeCallback(node_key=node_key).pack())],
             [InlineKeyboardButton(text=tr(locale, 'nodes.card.back_to_server'), callback_data=AdminNodeCallback(node_key=node_key).pack())]]
    details = [tr(locale, 'nodes.settings.value',
                  field=tr(locale, 'nodes.settings.field.' + field),
                  value=node[field] or '—') for field in ('title', 'region', 'flag')]
    details.extend(tr(locale, 'nodes.settings.value',
                      field=tr(locale, 'nodes.settings.field.' + field), value=value)
                   for field, value in sorted(settings.items()))
    await render(bot, chat_id, Screen(tr(locale, 'nodes.settings.title'),
        (tr(locale, 'nodes.settings.revisions', desired=node['desired_revision'],
            applied=node['applied_revision']), tr(locale, 'nodes.settings.apply_note')),
        tr(locale, 'nodes.settings.current'), tuple(details)), rows, state, message_id)


async def show_node_connection(chat_id, user_id, message_id, node_key, bot, backend, state):
    locale = normalize_locale((await state.get_data()).get('locale'))
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    transport = node.get('transport')
    rows = [[InlineKeyboardButton(text=tr(locale, 'nodes.connection.local'),
        callback_data=f'node_connection_set:{node_key}:local')],
        [InlineKeyboardButton(text=tr(locale, 'nodes.connection.ssh'),
        callback_data=f'node_connection_set:{node_key}:ssh')]]
    if transport == 'ssh':
        rows.append([InlineKeyboardButton(text=tr(locale, 'nodes.connection.edit_target'),
            callback_data=EditNodeFieldCallback(node_key=node_key, field='ssh_target').pack())])
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data=NodeSettingsCallback(node_key=node_key).pack())])
    await render(bot, chat_id, Screen(tr(locale, 'nodes.connection.title'),
        (tr(locale, 'nodes.connection.current', value=transport or '—'),
         tr(locale, 'nodes.connection.target', value=node.get('ssh_target') or '—'),
         tr(locale, 'nodes.connection.note'))), rows, state, message_id)


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
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}',
                                 telegram_user_id=query.from_user.id)
    if transport == 'ssh' and not node.get('ssh_target'):
        await _prompt_node_ssh_target(query.message.chat.id, query.message.message_id,
            node_key, node, bot, state, switch_transport=True)
        return
    body = {'transport': transport, 'ssh_target': node['ssh_target'] if transport == 'ssh' else None}
    try:
        await backend.edit_node(query.from_user.id, node_key, node['desired_revision'], body,
                                command_key=str(uuid4()))
    except BackendError:
        locale = normalize_locale((await state.get_data()).get('locale'))
        await render(bot, query.message.chat.id, Screen(tr(locale, 'nodes.connection.title'),
            (tr(locale, 'nodes.settings.save_failed'),)),
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
         tr(locale, 'nodes.connection.target_prompt'))),
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
        
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
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
    if field == 'awg_i1_preset':
        rows = [[InlineKeyboardButton(text=label,
                    callback_data=f'node_awg_preset:{preset}:{node_key}')]
                for preset, label in (('quic', 'QUIC'), ('dns', 'DNS'), ('chaos', 'Chaos'))]
        rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=f'node_section:awg:{node_key}')])
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'nodes.settings.field.awg_i1_preset'),
                (tr(locale, 'nodes.settings.current_value', value=current or 'quic'),
                 tr(locale, 'nodes.settings.apply_note'))), rows, state, query.message.message_id)
        return
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'nodes.settings.edit_title',
                field=tr(locale, 'nodes.settings.field.' + field)),
            (tr(locale, 'nodes.settings.current_value', value=current or '—'),
             tr(locale, 'nodes.settings.send_value'), tr(locale, 'nodes.settings.apply_note'))),
        [[InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=f'node_section:{(await state.get_data()).get("edit_section", "general")}:{node_key}')]],
        state, query.message.message_id)

@router.callback_query(F.data.startswith('node_awg_preset:'))
async def select_awg_preset(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    _, preset, node_key = query.data.split(':', 2)
    if preset not in {'quic', 'dns', 'chaos'}:
        return
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=query.from_user.id)
    await backend.edit_node(query.from_user.id, node_key, node['desired_revision'],
        {'settings': {**node['settings'], 'awg_i1_preset': preset}}, command_key=str(uuid4()))
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
                (tr(locale, 'nodes.settings.port_range'),)),
                [[InlineKeyboardButton(text=tr(locale, 'back'),
                    callback_data=f'node_section:{(await state.get_data()).get("edit_section", "general")}:{node_key}')]], state, message_id)
            return
        parsed = int(value)
    else: parsed = value
    
    if field == 'ssh_target':
        if not re.fullmatch(r'(?:[A-Za-z_][A-Za-z0-9._-]*@)?(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])', value):
            locale = normalize_locale(data.get('locale'))
            await render(bot, message.chat.id, Screen(tr(locale, 'nodes.connection.target_title'),
                (tr(locale, 'nodes.connection.target_invalid'),)),
                [[InlineKeyboardButton(text=tr(locale, 'back'),
                    callback_data=f'node_connection:{node_key}')]], state, message_id)
            return
        body = {'ssh_target': value}
        if data.get('edit_switch_transport'):
            body['transport'] = 'ssh'
    else:
        body = ({field: parsed} if field in {'title', 'region', 'flag', 'notes'} else {'settings': {**settings, field: parsed}})
    try:
        await backend.edit_node(user_id, node_key, revision, body,
                                command_key=data['edit_command_key'])
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
            (tr(locale, 'nodes.settings.save_failed'),)),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=callback)]],
            state, message_id)

@router.callback_query(NodeProtocolsCallback.filter())
async def node_protocols_cb(query: CallbackQuery, callback_data: NodeProtocolsCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await show_node_protocols(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

async def show_node_protocols(chat_id, user_id, message_id, node_key, bot, backend, state):
    locale = normalize_locale((await state.get_data()).get('locale'))
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    enabled, transports = set(node['protocols']), set(node['xray_transports'])
    rows = [[InlineKeyboardButton(text=('✓ ' if kind in enabled else '+ ') + kind.upper(), callback_data=ToggleNodeProtocolCallback(node_key=node_key, kind=kind).pack())] for kind in ('awg', 'xray')]
    if 'xray' in enabled:
        rows += [[InlineKeyboardButton(text=('✓ ' if kind in transports else '+ ') + kind.upper(), callback_data=ToggleNodeTransportCallback(node_key=node_key, kind=kind).pack())] for kind in ('tcp', 'xhttp')]
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data=NodeSettingsCallback(node_key=node_key).pack())])
    await render(bot, chat_id, Screen(tr(locale, 'nodes.protocols.title'),
        (tr(locale, 'nodes.protocols.apply_note'), tr(locale, 'nodes.protocols.grants_note'))),
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
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    protocols, transports = set(node['protocols']), set(node['xray_transports'])
    selected = transports if transport else protocols
    selected.symmetric_difference_update({kind})
    if not protocols:
        locale = normalize_locale((await state.get_data()).get('locale'))
        await render(bot, chat_id, Screen(tr(locale, 'nodes.protocols.required_title'),
            (tr(locale, 'nodes.protocols.required'),)),
            [[InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data=NodeProtocolsCallback(node_key=node_key).pack())]],
            state, message_id)
        return
    if 'xray' not in protocols: transports.clear()
    elif not transports: transports.update({'tcp', 'xhttp'} if not transport else {'tcp'})
    
    settings = dict(node['settings'])
    if 'awg' in protocols: settings.setdefault('awg_port', 51820)
    if 'xray' in protocols:
        for f, d in {'xray_sni': 'www.cloudflare.com', 'xray_tcp_port': 443, 'xray_xhttp_port': 8443, 'xray_xhttp_path': '/assets'}.items():
            settings.setdefault(f, d)
            
    await backend.edit_node(user_id, node_key, node['desired_revision'],
        {'protocols': sorted(protocols), 'xray_transports': sorted(transports),
         'settings': settings}, command_key=str(uuid4()))
    await show_node_protocols(chat_id, user_id, message_id, node_key, bot, backend, state)

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
            tr(locale, 'nodes.probe.version', value=observation.get('runtime_version') or '—'),
            tr(locale, 'nodes.probe.xray', value=tr(locale,
                'nodes.probe.present' if observation['xray_config_present'] else 'nodes.probe.missing')),
            tr(locale, 'nodes.probe.awg', value=tr(locale,
                'nodes.probe.present' if observation['awg_config_present'] else 'nodes.probe.missing')),
            tr(locale, 'nodes.probe.note'))
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'nodes.probe.title'), lines),
            [[InlineKeyboardButton(text=tr(locale, 'nodes.diagnostics.open'),
                callback_data=f'node_diagnostics:{node_key}')], *back], state,
            query.message.message_id)
    except BackendError as exc:
        cause = exc.code if exc.code in {'node_agent_unconfigured',
            'node_agent_unavailable', 'driver_unavailable'} else 'unknown'
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'nodes.probe.failed_title'),
                (tr(locale, 'nodes.probe.error.' + cause),)),
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
                (tr(locale, 'nodes.probe.error.' + cause),)),
            [[InlineKeyboardButton(text=tr(locale, 'nodes.card.bootstrap'),
                callback_data=f'bootstrap_menu:{node_key}')], back],
            state, query.message.message_id)
        return
    lines = [tr(locale, 'nodes.diagnostics.field.' + field,
        value=tr(locale, 'nodes.diagnostics.status.' + result[field]))
        for field in ('docker', 'runtime_root', 'xray_config', 'awg_config')]
    lines.append(tr(locale, 'nodes.diagnostics.version',
        value=result.get('runtime_version') or '—'))
    lines.append(tr(locale, 'nodes.diagnostics.note'))
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'nodes.diagnostics.title'), tuple(lines)),
        [[InlineKeyboardButton(text=tr(locale, 'nodes.diagnostics.refresh'),
            callback_data=f'node_diagnostics:{node_key}')], back],
        state, query.message.message_id)

@router.callback_query(ApplyNodeCallback.filter())
async def apply_node_cb(query: CallbackQuery, callback_data: ApplyNodeCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await apply_node(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

async def apply_node(chat_id, user_id, message_id, node_key, bot, backend, state, revision=None):
    locale = normalize_locale((await state.get_data()).get('locale'))
    if revision is None:
        node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
        revision = node['desired_revision']
    try:
        operation = await backend.apply_node_settings(user_id, node_key, revision)
    except BackendError:
        await render(bot, chat_id, Screen(tr(locale, 'nodes.apply.blocked_title'),
            (tr(locale, 'nodes.apply.queue_failed'),)),
            [[InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data=NodeSettingsCallback(node_key=node_key).pack())]],
            state, message_id)
        return False
    await render(bot, chat_id, Screen(tr(locale, 'nodes.apply.running_title'),
        (tr(locale, 'nodes.apply.running'),)),
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
            (tr(locale, 'nodes.apply.status_unavailable'),)),
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
    if s['status'] in {'blocked', 'superseded'}:
        await render(bot, chat_id, Screen(tr(locale, 'nodes.apply.blocked_title'),
            (tr(locale, 'nodes.apply.blocked'),)), rows, state, message_id)
        return
    rows.insert(0, [InlineKeyboardButton(text=tr(locale, 'nodes.apply.refresh'),
        callback_data=NodeApplyStatusCallback(operation_id=operation_id, node_key=node_key).pack())])
    await render(bot, chat_id, Screen(tr(locale, 'nodes.apply.running_title'),
        (tr(locale, 'nodes.apply.pending'),)), rows, state, message_id)

@router.callback_query(NodeMaintenanceCallback.filter())
async def node_maintenance_cb(query: CallbackQuery, callback_data: NodeMaintenanceCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await show_node_maintenance(query.message.chat.id, query.from_user.id, query.message.message_id, callback_data.node_key, bot, backend, state)

async def show_node_maintenance(chat_id, user_id, message_id, node_key, bot, backend, state):
    locale = normalize_locale((await state.get_data()).get('locale'))
    st = await backend.node_maintenance(user_id, node_key)
    lines = [tr(locale, 'nodes.maintenance.state',
        value=tr(locale, 'nodes.maintenance.status.' + st['status']))]
    target = st.get('verification_target')
    lines.append(tr(locale, 'nodes.maintenance.target',
        value=target or tr(locale, 'nodes.maintenance.not_bound')))
    if st['status'] == 'draining':
        lines.extend((tr(locale, 'nodes.maintenance.pending', count=st['pending_tasks']),
            tr(locale, 'nodes.maintenance.blocked', count=st['blocked_tasks']),
            tr(locale, 'nodes.maintenance.phase', value=tr(locale,
                'nodes.maintenance.phase.' + (st['cleanup_phase'] or 'not_started')))))
    
    if st['status'] == 'active':
        rows = [[InlineKeyboardButton(text=tr(locale, 'nodes.maintenance.start'), callback_data=ConfirmNodeDrainCallback(node_key=node_key).pack())]]
    else:
        rows = [[InlineKeyboardButton(text=tr(locale, 'nodes.maintenance.refresh'), callback_data=f'remove_progress:{node_key}')]]
    if (await state.get_data()).get('unreachable_removal_node') == node_key:
        rows.append([InlineKeyboardButton(text=tr(locale, 'nodes.maintenance.registry_only'), callback_data=ConfirmRegistryRemovalCallback(node_key=node_key).pack())])
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminNodeCallback(node_key=node_key).pack())])
    await render(bot, chat_id, Screen(tr(locale, 'nodes.maintenance.title'), tuple(lines),
        tr(locale, 'nodes.maintenance.details_title'),
        (tr(locale, 'nodes.maintenance.binding_note'),
         tr(locale, 'nodes.maintenance.verification_note'),
         tr(locale, 'nodes.maintenance.registry_note'))), rows, state, message_id)

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
        (tr(locale, 'nodes.maintenance.error'),)),
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
         tr(locale, 'nodes.maintenance.ssh_port_note'))),
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
             tr(locale, 'nodes.maintenance.ssh_port_note'))),
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
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'nodes.maintenance.start'),
            (tr(locale, 'nodes.maintenance.node', key=node_key),
             tr(locale, 'nodes.maintenance.drain_grants'),
             tr(locale, 'nodes.maintenance.drain_runtime'),
             tr(locale, 'nodes.maintenance.drain_verify'))),
        [[InlineKeyboardButton(text=tr(locale, 'nodes.maintenance.drain_confirm'),
            callback_data=DrainNodeCallback(node_key=node_key).pack())],
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
            (tr(locale, 'nodes.maintenance.removed_verified'),)),
        [[InlineKeyboardButton(text=tr(locale, 'nodes.maintenance.nodes'),
            callback_data=AdminNodesCallback().pack())]], state, query.message.message_id)

@router.callback_query(ConfirmRegistryRemovalCallback.filter())
async def confirm_registry_removal_cb(query: CallbackQuery, callback_data: ConfirmRegistryRemovalCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    node_key = callback_data.node_key
    locale = normalize_locale((await state.get_data()).get('locale'))
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'nodes.maintenance.registry_only'),
            (tr(locale, 'nodes.maintenance.node', key=node_key),
             tr(locale, 'nodes.maintenance.registry_grants'),
             tr(locale, 'nodes.maintenance.registry_runtime'),
             tr(locale, 'nodes.maintenance.registry_use'))),
        [[InlineKeyboardButton(text=tr(locale, 'nodes.maintenance.registry_confirm'),
            callback_data=RetireRegistryCallback(node_key=node_key).pack())],
         [InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=NodeMaintenanceCallback(node_key=node_key).pack())]],
        state, query.message.message_id)

@router.callback_query(RetireRegistryCallback.filter())
async def retire_registry_cb(query: CallbackQuery, callback_data: RetireRegistryCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    try:
        result = await backend.retire_node_registry_only(query.from_user.id,
                                                         callback_data.node_key)
    except BackendError:
        await show_node_maintenance_error(query.message.chat.id, query.message.message_id,
            callback_data.node_key, bot, state)
        return
    locale = normalize_locale((await state.get_data()).get('locale'))
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'nodes.maintenance.registry_removed_title'),
            (tr(locale, 'nodes.maintenance.node', key=result['node_key']),
             tr(locale, 'nodes.maintenance.registry_removed_note'))),
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
    await render(bot, query.message.chat.id, Screen(tr(locale, 'nodes.rollout.ssh_title'), (tr(locale, 'nodes.rollout.ssh_prompt'),)), [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminNodeCallback(node_key=node_key).pack())]], state, query.message.message_id)

@router.message(AgentDraftState.waiting_for_ssh_target, F.text)
async def process_agent_ssh(message: Message, bot: Bot, backend: BackendClient, state: FSMContext):
    if message.from_user is None or message.chat.type != 'private': return
    user_id = message.from_user.id
    target = (message.text or '').strip()
    data = await state.get_data()
    node_key, message_id = data['rollout_node_key'], data.get('control_message_id')
    
    if not re.fullmatch(r'(?:[A-Za-z_][A-Za-z0-9._-]*@)?[A-Za-z0-9.-]+', target):
        locale = normalize_locale(data.get('locale'))
        await render(bot, message.chat.id, Screen(tr(locale, 'nodes.rollout.ssh_title'), (tr(locale, 'nodes.rollout.ssh_prompt'),)), [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminNodeCallback(node_key=node_key).pack())]], state, message_id)
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
        await render(bot, chat_id, Screen(tr(locale, 'nodes.rollout.queue_failed'), (tr(locale, 'nodes.rollout.queue_failed_note'),)), [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminNodeCallback(node_key=node_key).pack()), InlineKeyboardButton(text=tr(locale, 'node_tools.retry'), callback_data=RetryRolloutCallback().pack())]], state, message_id)

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
            (tr(locale, 'nodes.rollout.status_unavailable'),)),
            [[InlineKeyboardButton(text=tr(locale, 'nodes.rollout.refresh'),
                callback_data=RolloutStatusCallback(task_id=task_id).pack())]],
            state, message_id)
        return
    status = task['status'] if task['status'] in {'succeeded', 'blocked',
        'running', 'awaiting_executor'} else 'unknown'
    rows = [] if status in {'succeeded', 'blocked'} else [
        [InlineKeyboardButton(text=tr(locale, 'nodes.rollout.refresh'),
            callback_data=RolloutStatusCallback(task_id=task_id).pack())]]
    if status == 'succeeded':
        rows.append([InlineKeyboardButton(text=tr(locale, 'nodes.rollout.open_settings'),
            callback_data=NodeSettingsCallback(node_key=task['node_key']).pack())])
    if status == 'blocked' and task.get('failure_code') == 'rust_required':
        rows.append([InlineKeyboardButton(text=tr(locale, 'nodes.rollout.install_rust'),
            callback_data=f"rust_offer:{task_id}")])
    rows.append([InlineKeyboardButton(text=tr(locale, 'nodes.rollout.card'),
        callback_data=AdminNodeCallback(node_key=task['node_key']).pack())])
    lines = [tr(locale, 'nodes.rollout.' + status)]
    if task.get('failure_code') == 'build_resources':
        lines.append(tr(locale, 'nodes.rollout.build_resources'))
    await render(bot, chat_id, Screen(tr(locale, 'nodes.rollout.title'),
        tuple(lines)), rows, state, message_id)


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
        (tr(locale, 'nodes.rollout.rust_note'),)), [[
        InlineKeyboardButton(text=tr(locale, 'back'), callback_data=RolloutStatusCallback(task_id=task_id).pack()),
        InlineKeyboardButton(text=tr(locale, 'nodes.rollout.install_rust'), callback_data=f'rust_confirm:{task_id}')]], state, query.message.message_id)


@router.callback_query(F.data.startswith('rust_confirm:'))
async def rust_confirm_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    task_id = query.data.split(':', 1)[1]
    data = await state.get_data()
    if data.get('rust_rollout_task') != task_id:
        return
    task = await backend.agent_rollout(query.from_user.id, task_id)
    node = await backend.request('GET', f"/api/v1/nodes/{task['node_key']}", telegram_user_id=query.from_user.id)
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
    if node.get('transport') != 'ssh' or not node.get('ssh_target'):
        await show_admin_node(query.message.chat.id, query.from_user.id,
                              query.message.message_id, node_key, bot, backend, state)
        return
    await state.update_data(rollout_node_key=node_key, rollout_ssh_target=node['ssh_target'],
                            rollout_command_key=str(uuid4()))
    await queue_rollout(query.message.chat.id, query.from_user.id, query.message.message_id,
                        node_key, 'ssh', bot, backend, state, node['ssh_target'])
