"""Node installation and operational maintenance; all effects belong to backend."""
from uuid import uuid4
from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section, Table
from .common import render
from .callbacks import AdminNodeCallback, NodeSettingsCallback, EditNodeFieldCallback

router = Router()


def button(locale, key, callback):
    return InlineKeyboardButton(text=tr(locale, key), callback_data=callback)


def error(locale, exc):
    key = {'node_agent_unconfigured': 'node_tools.no_agent',
           'node_agent_unavailable': 'node_tools.unreachable',
           'node_operation_pending': 'node_tools.busy',
           'revision_conflict': 'node_tools.changed',
           'node_settings_incomplete': 'node_tools.incomplete'}.get(exc.code, 'node_tools.error')
    return tr(locale, key)


async def show_install(chat_id, user_id, message_id, node_key, bot, backend, state):
    from .admin_nodes import RolloutLocalCallback, RolloutSshCallback
    locale = normalize_locale((await state.get_data()).get('locale'))
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    overview = await backend.node_overview(user_id, node_key)
    rows = []
    lines = []
    try:
        facts = await backend.node_services(user_id, node_key)
        job = overview.get('last_job')
        if job and job['status'] in {'awaiting_executor', 'running', 'blocked'}:
            lines.append(tr(locale, 'node_tools.busy'))
            rows.append([button(locale, 'node_tools.refresh', 'node_job:' + job['id'])])
        elif not facts['docker']:
            lines.append(tr(locale, 'node_tools.docker_missing'))
            rows.append([button(locale, 'node_tools.install_docker', f'node_action:install_docker:{node_key}')])
        else:
            if not overview['settings_complete']:
                lines.append(tr(locale, 'node_tools.incomplete'))
                rows.append([button(locale, 'nodes.card.settings', NodeSettingsCallback(node_key=node_key).pack())])
                rows.append([button(locale, 'back', AdminNodeCallback(node_key=node_key).pack())])
                await render(bot, chat_id, Screen(tr(locale, 'node_tools.install'), tuple(lines), embedded_buttons=True, navigation=True), rows, state, message_id)
                return
            present = any(facts[p + '_config_valid'] for p in node['protocols'])
            reusable = bool(node['protocols']) and all(facts[p + '_config_valid'] for p in node['protocols'])
            lines.append(tr(locale, 'node_tools.reinstall_note' if present else 'node_tools.bootstrap_note'))
            if reusable:
                rows.append([button(locale, 'node_tools.reinstall_keep', f'node_action:reinstall_keep:{node_key}')])
            rows.append([button(locale, 'node_tools.reinstall_clean' if present else 'node_tools.bootstrap',
                                f'node_action:{"reinstall_clean" if present else "bootstrap"}:{node_key}')])
    except BackendError as exc:
        lines.append(error(locale, exc))
        if exc.code == 'node_agent_unconfigured':
            target = (f'rollout_saved:{node_key}' if node.get('transport') == 'ssh' and node.get('ssh_target') else
                      RolloutLocalCallback(node_key=node_key).pack() if node.get('transport') == 'local' else
                      RolloutSshCallback(node_key=node_key).pack())
            rows.append([button(locale, 'node_tools.setup_agent', target)])
        else:
            rows.append([button(locale, 'node_tools.refresh', f'bootstrap_menu:{node_key}')])
    rows.append([button(locale, 'back', AdminNodeCallback(node_key=node_key).pack())])
    await render(bot, chat_id, Screen(tr(locale, 'node_tools.install'), tuple(lines), embedded_buttons=True, navigation=True), rows, state, message_id)


@router.callback_query(F.data.startswith('node_section:'))
async def section_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    _, section, node_key = query.data.split(':', 2)
    await show_section(query.message.chat.id, query.from_user.id, query.message.message_id,
                       section, node_key, bot, backend, state)


async def show_section(chat_id, user_id, message_id, section, node_key, bot, backend, state):
    fields = {'general': ('title', 'flag', 'region', 'notes'),
              'connection': ('public_host',),
              'xray': ('xray_host', 'xray_sni', 'xray_fingerprint', 'xray_tcp_port', 'xray_xhttp_port', 'xray_xhttp_path'),
              'awg': ('awg_public_host', 'awg_interface', 'awg_port', 'awg_i1_preset')}
    if section not in fields:
        return
    locale = normalize_locale((await state.get_data()).get('locale'))
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    if section in {'awg', 'xray'} and section not in node['protocols']:
        return
    await state.set_state(None)
    await state.update_data(edit_section=section)
    def field_button(field):
        return button(locale, 'nodes.settings.field.' + field,
                      EditNodeFieldCallback(node_key=node_key, field=field).pack())
    defaults = {'xray_fingerprint': 'chrome', 'awg_interface': 'wg0', 'awg_i1_preset': 'quic'}
    def group(title, selected, *, extra=(), collapsed=False):
        buttons = [field_button(field) for field in selected]
        rows = [tuple(buttons[index:index + 2]) for index in range(0, len(buttons), 2)]
        rows.extend(extra)
        return Section(tr(locale, title), collapsed=collapsed,
            tables=(Table((tr(locale, 'account.rich.field'), tr(locale, 'account.rich.value')),
                tuple((tr(locale, 'nodes.settings.field.' + field), str(node.get(field, node['settings'].get(field, defaults.get(field, '—'))) or '—')) for field in selected)),) if selected else (), rows=tuple(rows))
    if section == 'general':
        sections = (group('nodes.rich.name_region', ('title', 'region', 'flag')), group('nodes.settings.field.notes', ('notes',), collapsed=True))
    elif section == 'connection':
        sections = (group('nodes.rich.connection', ('public_host',)),
            Section(tr(locale, 'nodes.rich.advanced'), collapsed=True,
                rows=((button(locale, 'nodes.settings.field.transport', f'node_connection:{node_key}'),),)))
    elif section == 'xray':
        sections = (group('nodes.rich.connection', ('xray_host', 'xray_sni')),
            group('node_tools.ports', ('xray_tcp_port', 'xray_xhttp_port')),
            group('nodes.rich.advanced', ('xray_fingerprint', 'xray_xhttp_path'), collapsed=True))
    else:
        sections = (group('nodes.rich.connection', ('awg_public_host', 'awg_port')),
            group('nodes.rich.obscuration', ('awg_i1_preset',), extra=((
                button(locale, 'node_tools.regenerate_entropy', f'node_action:regenerate_entropy:{node_key}'),),)),
            group('nodes.rich.advanced', ('awg_interface',), extra=((
                button(locale, 'node_tools.entropy', f'node_view:entropy:{node_key}'),),), collapsed=True))
    await render(bot, chat_id, Screen(tr(locale, 'nodes.rich.connection' if section == 'connection' else 'node_tools.' + section),
        (tr(locale, 'nodes.rich.settings_note'),), sections=sections, embedded_buttons=True, navigation=True),
        [[button(locale, 'nodes.card.back_to_settings', NodeSettingsCallback(node_key=node_key).pack())]], state, message_id)


@router.callback_query(F.data.startswith('node_tools:'))
async def tools_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    node_key = query.data.split(':', 1)[1]
    locale = normalize_locale((await state.get_data()).get('locale'))
    rows = [[button(locale, 'node_tools.' + name, f'node_view:{name}:{node_key}') for name in names]
            for names in (('diagnostics', 'ports'), ('runtime', 'repair'))]
    rows += [[button(locale, 'node_tools.cleanup_runtime', f'node_action:cleanup_runtime:{node_key}')],
             [button(locale, 'back', f'node_technical:{node_key}')]]
    await render(bot, query.message.chat.id, Screen(tr(locale, 'nodes.rich.technical'), (), embedded_buttons=True, navigation=True), rows, state, query.message.message_id)


@router.callback_query(F.data.startswith('node_view:'))
async def view_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    _, view, node_key = query.data.split(':', 2)
    locale = normalize_locale((await state.get_data()).get('locale'))
    rows, lines = [], []
    try:
        node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=query.from_user.id)
        if view in {'runtime', 'diagnostics', 'entropy'}:
            facts = await backend.node_services(query.from_user.id, node_key)
            if view == 'entropy':
                lines = facts['entropy'] or [tr(locale, 'node_tools.no_config')]
            elif view == 'runtime':
                lines = [tr(locale, 'node_tools.runtime_version', version=facts['runtime_version'] or '—', commit=facts['runtime_commit'] or '—'),
                         tr(locale, 'node_tools.runtime_target', version=facts['desired_runtime_version'], commit=facts['desired_runtime_commit']),
                         tr(locale, 'node_tools.drift' if facts['runtime_drift'] else 'node_tools.current')]
                if facts['runtime_drift']:
                    rows.append([button(locale, 'node_tools.sync_runtime', f'node_action:sync_runtime:{node_key}')])
            else:
                lines = [tr(locale, 'node_tools.fact', name=tr(locale, 'node_tools.fact.' + key),
                    value=tr(locale, 'node_tools.yes' if facts[key] else 'node_tools.no'))
                    for key in ('docker', 'xray_config_valid', 'awg_config_valid', 'xray_running', 'awg_running')
                    if not key.startswith(('xray', 'awg')) or key.split('_')[0] in node['protocols']]
        elif view == 'ports':
            rows = [[button(locale, 'node_tools.' + action, f'node_action:{action}:{node_key}')
                    for action in ('check_ports', 'open_ports')]]
        elif view == 'repair':
            actions = ['sync_env'] + (['sync_xray'] if 'xray' in node['protocols'] else []) + ['reconcile_access']
            rows = [[button(locale, 'node_tools.' + action, f'node_action:{action}:{node_key}')] for action in actions]
        else:
            return
    except BackendError as exc:
        lines = [error(locale, exc)]
        rows = [[button(locale, 'node_tools.refresh', query.data)]]
    rows.append([button(locale, 'back', f'node_section:awg:{node_key}' if view == 'entropy' else f'node_tools:{node_key}')])
    await render(bot, query.message.chat.id, Screen(tr(locale, 'node_tools.' + view), tuple(lines), embedded_buttons=True, navigation=True), rows, state, query.message.message_id)


@router.callback_query(F.data.startswith('node_action:'))
async def action_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    _, action, node_key = query.data.split(':', 2)
    locale = normalize_locale((await state.get_data()).get('locale'))
    ACTIONS = {'bootstrap', 'reinstall_keep', 'reinstall_clean', 'cleanup_runtime',
        'install_docker', 'check_ports', 'open_ports', 'sync_runtime', 'sync_env',
        'sync_xray', 'regenerate_entropy', 'reconcile_access'}
    if action not in ACTIONS:
        return
    try:
        node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=query.from_user.id)
        await state.update_data(node_job_draft={'node_key': node_key, 'action': action,
            'revision': node['desired_revision'], 'command_key': str(uuid4())})
        rows = [[button(locale, 'back', f'bootstrap_menu:{node_key}' if action in {'bootstrap', 'reinstall_clean', 'reinstall_keep', 'install_docker'} else f'node_tools:{node_key}'),
                 button(locale, 'node_tools.confirm', 'node_job_submit')]]
        await render(bot, query.message.chat.id, Screen(tr(locale, 'node_tools.' + action), embedded_buttons=True, navigation=True, lines=
            (tr(locale, 'node_tools.confirm_note'),) + ((tr(locale, 'node_tools.clean_warning'),)
                if action in {'reinstall_clean', 'cleanup_runtime'} else ())), rows, state, query.message.message_id)
    except BackendError as exc:
        await render(bot, query.message.chat.id, Screen(tr(locale, 'node_tools.error_title'), (error(locale, exc),), embedded_buttons=True, navigation=True),
            [[button(locale, 'back', AdminNodeCallback(node_key=node_key).pack())]], state, query.message.message_id)


@router.callback_query(F.data == 'node_job_submit')
async def submit_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    draft = data.get('node_job_draft')
    if not draft:
        return
    locale = normalize_locale(data.get('locale'))
    try:
        job = await backend.node_action(query.from_user.id, **draft)
        await show_job(query.message.chat.id, query.from_user.id, query.message.message_id, job, bot, state)
    except BackendError as exc:
        await render(bot, query.message.chat.id, Screen(tr(locale, 'node_tools.error_title'), (error(locale, exc),), embedded_buttons=True, navigation=True),
            [[button(locale, 'back', AdminNodeCallback(node_key=draft['node_key']).pack())]], state, query.message.message_id)


async def show_job(chat_id, user_id, message_id, job, bot, state):
    locale = normalize_locale((await state.get_data()).get('locale'))
    lines = [tr(locale, 'operation.' + job['status'])]
    result = job.get('result') or {}
    if job['status'] == 'succeeded':
        lines = [tr(locale, 'node_tools.done.' + job['action'])]
        lines += [tr(locale, 'node_tools.port_result', port=item['port'], protocol=item['protocol'],
                     value=tr(locale, 'node_tools.port.' + item['status'])) for item in result.get('ports', [])]
        if result.get('firewall') == 'unmanaged':
            lines.append(tr(locale, 'node_tools.firewall_unmanaged'))
    elif job['status'] == 'blocked':
        lines.append(tr(locale, 'node_tools.blocked'))
    rows = []
    if job['status'] in {'awaiting_executor', 'running', 'blocked'}:
        rows.append([button(locale, 'node_tools.refresh', 'node_job:' + job['id'])])
    if job['status'] == 'blocked':
        rows.append([button(locale, 'node_tools.resolve', 'node_resolve:' + job['id'])])
    rows.append([button(locale, 'back', AdminNodeCallback(node_key=job['node_key']).pack())])
    await render(bot, chat_id, Screen(tr(locale, 'node_tools.' + job['action']), tuple(lines), embedded_buttons=True, navigation=True), rows, state, message_id)


@router.callback_query(F.data.startswith('node_job:'))
async def job_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    try:
        job = await backend.node_job(query.from_user.id, query.data.split(':', 1)[1])
        await show_job(query.message.chat.id, query.from_user.id, query.message.message_id, job, bot, state)
    except BackendError as exc:
        locale = normalize_locale((await state.get_data()).get('locale'))
        await render(bot, query.message.chat.id, Screen(tr(locale, 'node_tools.error_title'), (error(locale, exc),), embedded_buttons=True, navigation=True),
            [[button(locale, 'node_tools.refresh', query.data)]], state, query.message.message_id)


@router.callback_query(F.data.startswith('node_resolve:'))
async def resolve_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    job_id = query.data.split(':', 1)[1]
    await render(bot, query.message.chat.id, Screen(tr(locale, 'node_tools.resolve'),
        (tr(locale, 'node_tools.resolve_note'),), embedded_buttons=True, navigation=True),
        [[button(locale, 'back', 'node_job:' + job_id), button(locale, 'node_tools.confirm', 'node_resolve_do:' + job_id)]], state, query.message.message_id)


@router.callback_query(F.data.startswith('node_resolve_do:'))
async def resolve_do_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    job_id = query.data.split(':', 1)[1]
    try:
        job = await backend.resolve_node_job(query.from_user.id, job_id)
        await show_job(query.message.chat.id, query.from_user.id, query.message.message_id, job, bot, state)
    except BackendError:
        locale = normalize_locale((await state.get_data()).get('locale'))
        await render(bot, query.message.chat.id, Screen(tr(locale, 'node_tools.error_title'),
            (tr(locale, 'node_tools.resolve_failed'),), embedded_buttons=True, navigation=True), [[button(locale, 'back', 'node_job:' + job_id)]], state, query.message.message_id)


@router.callback_query(F.data.startswith('remove_progress:'))
async def removal_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await advance_removal(query.message.chat.id, query.from_user.id, query.message.message_id,
                          query.data.split(':', 1)[1], bot, backend, state)


@router.callback_query(F.data.startswith('remove_retry:'))
async def removal_retry_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await advance_removal(query.message.chat.id, query.from_user.id, query.message.message_id,
                          query.data.split(':', 1)[1], bot, backend, state, retry=True)


async def advance_removal(chat_id, user_id, message_id, node_key, bot, backend, state, retry=False):
    from .callbacks import AdminNodesCallback, ConfirmRegistryRemovalCallback
    locale = normalize_locale((await state.get_data()).get('locale'))
    rows = [[button(locale, 'node_tools.refresh', f'remove_progress:{node_key}')]]
    try:
        result = await backend.remove_node_step(user_id, node_key, retry=retry)
        if result.get('error_code'):
            raise BackendError(result['error_code'], 409)
        if result['status'] in {'removed', 'removed_registry_only'}:
            await render(bot, chat_id, Screen(tr(locale, 'node_tools.removed'),
                (tr(locale, 'node_tools.removed_note' if result['status'] == 'removed' else 'nodes.maintenance.registry_removed_note'),), embedded_buttons=True, navigation=True),
                [[button(locale, 'nodes.card.to_list', AdminNodesCallback().pack())]], state, message_id)
            return
        lines = [tr(locale, 'node_tools.removal_progress')]
        if not result['revocations_complete']:
            lines.append(tr(locale, 'nodes.maintenance.pending', count=result['pending_tasks']))
        else:
            lines.append(tr(locale, 'nodes.maintenance.phase', value=tr(locale,
                'nodes.maintenance.phase.' + (result['cleanup_phase'] or 'not_started'))))
    except BackendError as exc:
        rows = [[button(locale, 'node_tools.retry', f'remove_retry:{node_key}')]]
        if exc.code in {'node_cleanup_unavailable', 'node_agent_unavailable', 'node_agent_unconfigured', 'host_verification_failed'}:
            lines = [tr(locale, 'node_tools.unreachable')]
            await state.update_data(unreachable_removal_node=node_key)
            rows.append([button(locale, 'nodes.maintenance.registry_only', ConfirmRegistryRemovalCallback(node_key=node_key).pack())])
        elif exc.code in {'independent_verification_key_required', 'verification_key_unavailable'}:
            lines = [tr(locale, 'node_tools.verification_key')]
        else:
            lines = [error(locale, exc)]
    rows.append([button(locale, 'back', AdminNodeCallback(node_key=node_key).pack())])
    await render(bot, chat_id, Screen(tr(locale, 'nodes.card.delete'), tuple(lines), embedded_buttons=True, navigation=True), rows, state, message_id)
