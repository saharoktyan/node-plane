"""Node installation and operational maintenance; all effects belong to backend."""
from uuid import uuid4
from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section, Table, server_label
from .common import render, schedule_refresh
from ..navigation import remember_node, remember_label
from .callbacks import AdminNodeCallback, NodeSettingsCallback, EditNodeFieldCallback

router = Router()


def button(locale, key, callback, *, style=None):
    return InlineKeyboardButton(text=tr(locale, key), callback_data=callback, style=style)


def error(locale, exc):
    key = {'node_agent_unconfigured': 'node_tools.no_agent',
           'node_agent_unavailable': 'node_tools.unreachable',
           'node_operation_pending': 'node_tools.busy',
           'revision_conflict': 'node_tools.changed',
           'node_settings_incomplete': 'node_tools.incomplete'}.get(exc.code, 'node_tools.error')
    return tr(locale, key)


async def show_install(chat_id, user_id, message_id, node_key, bot, backend, state):
    locale = normalize_locale((await state.get_data()).get('locale'))
    node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=user_id)
    await remember_node(state, node)
    back = (f'node_manage:{node_key}' if node.get('applied_revision') else
            AdminNodeCallback(node_key=node_key).pack())
    overview = await backend.node_overview(user_id, node_key)
    if overview.get('bootstrap'):
        await show_bootstrap(chat_id, user_id, message_id, overview['bootstrap'], bot, backend, state)
        return
    if not node['protocols']:
        from .admin_nodes import RolloutLocalCallback, RolloutSshCallback, show_rollout_status
        if overview.get('agent_rollout'):
            await show_rollout_status(chat_id, user_id, message_id,
                overview['agent_rollout']['id'], bot, backend, state)
            return
        rows = []
        lines = [server_label(node)]
        try:
            await backend.node_services(user_id, node_key)
            lines.append(tr(locale, 'nodes.rollout.agent_ready'))
        except BackendError as exc:
            if exc.code == 'node_agent_unconfigured':
                target = (f'rollout_saved:{node_key}' if node.get('transport') == 'ssh' and node.get('ssh_target') else
                          RolloutLocalCallback(node_key=node_key).pack() if node.get('transport') == 'local' else
                          RolloutSshCallback(node_key=node_key).pack())
                rows.append([button(locale, 'node_tools.setup_agent', target, style='primary')])
            else:
                lines.append(error(locale, exc))
                rows.append([button(locale, 'node_tools.refresh', f'bootstrap_menu:{node_key}')])
        rows.append([button(locale, 'back', back)])
        await render(bot, chat_id, Screen(tr(locale, 'node_tools.setup_agent'), tuple(lines),
            embedded_buttons=True, navigation=True), rows, state, message_id)
        return
    if not node.get('applied_revision'):
        complete = overview['settings_complete']
        label = 'node_tools.bootstrap'
        if complete:
            try:
                await backend.node_services(user_id, node_key)
                label = 'node_tools.protocols_stage_button'
            except BackendError:
                pass
        if complete:
            await show_action_confirmation(chat_id, message_id, node, 'bootstrap', bot, state, title_key=label)
            return
        await render(bot, chat_id, Screen(tr(locale, 'node_tools.install'), (server_label(node),),
            embedded_buttons=True, navigation=True),
            [[button(locale, label if complete else 'nodes.card.settings',
                f'node_action:bootstrap:{node_key}' if complete else NodeSettingsCallback(node_key=node_key).pack(), style='primary')],
             [button(locale, 'back', back)]], state, message_id)
        return
    rows = []
    lines = []
    try:
        facts = await backend.node_services(user_id, node_key)
        job = overview.get('last_job')
        if job and job['status'] in {'awaiting_executor', 'running', 'blocked'}:
            lines.append(tr(locale, 'node_tools.busy'))
            rows.append([button(locale, 'node_tools.refresh', 'node_job:' + job['id'])])
        elif not facts['docker']:
            await show_action_confirmation(chat_id, message_id, node, 'bootstrap', bot, state)
            return
        else:
            if not overview['settings_complete']:
                lines.append(tr(locale, 'node_tools.incomplete'))
                rows.append([button(locale, 'nodes.card.settings', NodeSettingsCallback(node_key=node_key).pack())])
                rows.append([button(locale, 'back', back)])
                await render(bot, chat_id, Screen(tr(locale, 'node_tools.install'),
                    (server_label(node),), sections=(Section(tr(locale, 'nodes.rich.next_step'),
                        lines=tuple(lines), rows=(tuple(rows[0]),)),),
                    embedded_buttons=True, navigation=True), rows[-1:], state, message_id)
                return
            present = any(facts[p + '_config_valid'] for p in node['protocols'])
            if not present:
                await show_action_confirmation(chat_id, message_id, node, 'bootstrap', bot, state)
                return
            reusable = bool(node['protocols']) and all(facts[p + '_config_valid'] for p in node['protocols'])
            lines.append(tr(locale, 'node_tools.reinstall_note' if present else 'node_tools.bootstrap_note'))
            if reusable:
                rows.append([button(locale, 'node_tools.reinstall_keep', f'node_action:reinstall_keep:{node_key}', style='primary')])
            rows.append([button(locale, 'node_tools.reinstall_clean' if present else 'node_tools.bootstrap',
                                f'node_action:{"reinstall_clean" if present else "bootstrap"}:{node_key}')
                         .model_copy(update={'style': 'danger' if present else 'primary'})])
    except BackendError as exc:
        lines.append(error(locale, exc))
        if exc.code == 'node_agent_unconfigured':
            await show_action_confirmation(chat_id, message_id, node, 'bootstrap', bot, state)
            return
        else:
            rows.append([button(locale, 'node_tools.refresh', f'bootstrap_menu:{node_key}')])
    rows.append([button(locale, 'back', back)])
    actions = tuple(tuple(row) for row in rows[:-1])
    await render(bot, chat_id, Screen(tr(locale, 'node_tools.install'),
        (server_label(node),), sections=(Section(tr(locale, 'nodes.rich.next_step'),
            lines=tuple(lines), rows=actions),), embedded_buttons=True, navigation=True), rows[-1:], state, message_id)


@router.callback_query(F.data.startswith('node_section:'))
async def section_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    _, section, node_key = query.data.split(':', 2)
    await show_section(query.message.chat.id, query.from_user.id, query.message.message_id,
                       section, node_key, bot, backend, state)


async def show_section(chat_id, user_id, message_id, section, node_key, bot, backend, state):
    from .admin_nodes import editable_node, draft_controls
    fields = {'general': ('title', 'flag', 'region', 'notes'),
              'connection': ('public_host',),
              'xray': ('xray_host', 'xray_sni', 'xray_fingerprint', 'xray_tcp_port', 'xray_xhttp_port', 'xray_xhttp_path'),
              'awg': ('awg_public_host', 'awg_interface', 'awg_port', 'awg_i1_preset')}
    if section not in fields:
        return
    locale = normalize_locale((await state.get_data()).get('locale'))
    node = await editable_node(user_id, node_key, backend, state)
    if section in {'awg', 'xray'} and section not in node['protocols']:
        return
    await state.set_state(None)
    await state.update_data(edit_section=section, node_settings_view=section)
    def field_button(field):
        return button(locale, 'nodes.settings.field.' + field,
                      EditNodeFieldCallback(node_key=node_key, field=field).pack())
    defaults = {'xray_fingerprint': 'chrome', 'awg_interface': 'wg0', 'awg_i1_preset': 'quic'}
    if node['settings'].get('awg_port_mode') == 'auto':
        defaults['awg_port'] = tr(locale, 'nodes.awg.port_automatic')
    def group(title, selected, *, extra=(), collapsed=False):
        buttons = [field_button(field) for field in selected]
        rows = [tuple(buttons[index:index + 2]) for index in range(0, len(buttons), 2)]
        rows.extend(extra)
        return Section(tr(locale, title), collapsed=collapsed, heading_size=3 if section in {'awg', 'xray'} else 2,
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
        preferred = {'quic': 443, 'dns': 53}.get(node['settings'].get('awg_i1_preset', 'quic'))
        actual = node['settings'].get('awg_port')
        if node['settings'].get('awg_port_mode') == 'auto' and actual and preferred and actual != preferred:
            sections = (Section('', (tr(locale, 'nodes.awg.port_fallback', port=actual, preferred=preferred),)), *sections)
    if section in {'awg', 'xray'}:
        sections = (Section(tr(locale, 'nodes.draft.protocol_settings'),
            heading_size=2, sections=sections),)
    await render(bot, chat_id, Screen(tr(locale, 'nodes.rich.connection' if section == 'connection' else 'node_tools.' + section),
        sections=sections, embedded_buttons=True, navigation=True),
        await draft_controls(node, state, locale) +
        [[button(locale, 'nodes.card.back_to_settings', NodeSettingsCallback(node_key=node_key).pack())]], state, message_id)


@router.callback_query(F.data.startswith('node_tools:'))
async def tools_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    node_key = query.data.split(':', 1)[1]
    locale = normalize_locale((await state.get_data()).get('locale'))
    rows = [[button(locale, 'node_tools.' + name, f'node_view:{name}:{node_key}') for name in names]
            for names in (('diagnostics', 'ports'), ('runtime', 'repair'))]
    rows += [[button(locale, 'node_tools.cleanup_runtime', f'node_action:cleanup_runtime:{node_key}').model_copy(update={'style': 'danger'})],
             [button(locale, 'back', f'node_technical:{node_key}')]]
    await render(bot, query.message.chat.id, Screen(tr(locale, 'nodes.rich.technical'), (), embedded_buttons=True, navigation=True), rows, state, query.message.message_id)


@router.callback_query(F.data.startswith('node_view:'))
async def view_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    _, view, node_key = query.data.split(':', 2)
    locale = normalize_locale((await state.get_data()).get('locale'))
    rows, lines, sections = [], [], []
    try:
        node = await backend.request('GET', f'/api/v1/nodes/{node_key}', telegram_user_id=query.from_user.id)
        await remember_node(state, node)
        if view in {'runtime', 'diagnostics', 'entropy'}:
            facts = await backend.node_services(query.from_user.id, node_key)
            if view == 'entropy':
                lines = facts['entropy'] or [tr(locale, 'node_tools.no_config')]
            elif view == 'runtime':
                lines = [tr(locale, 'node_tools.drift' if facts['runtime_drift'] else 'node_tools.current')]
                sections = [Section(tr(locale, 'node_tools.runtime'), tables=(Table(
                    (tr(locale, 'account.rich.field'), tr(locale, 'nodes.rich.version')),
                    ((tr(locale, 'nodes.rich.installed'), facts['runtime_version'] or '—'),
                     (tr(locale, 'nodes.rich.target'), facts['desired_runtime_version'] or '—'))),)),
                    Section(tr(locale, 'nodes.rich.technical'), collapsed=True, tables=(Table(
                        (tr(locale, 'account.rich.field'), tr(locale, 'nodes.rich.commit')),
                        ((tr(locale, 'nodes.rich.installed'), facts['runtime_commit'] or '—'),
                         (tr(locale, 'nodes.rich.target'), facts['desired_runtime_commit'] or '—'))),))]
                if facts['runtime_drift']:
                    rows.append([button(locale, 'node_tools.sync_runtime', f'node_action:sync_runtime:{node_key}')])
            else:
                entries = tuple((tr(locale, 'node_tools.fact.' + key),
                    tr(locale, 'node_tools.yes' if facts[key] else 'node_tools.no'))
                    for key in ('docker', 'xray_config_valid', 'awg_config_valid', 'xray_running', 'awg_running')
                    if not key.startswith(('xray', 'awg')) or key.split('_')[0] in node['protocols'])
                sections = [Section(tr(locale, 'nodes.rich.services'), tables=(Table(
                    (tr(locale, 'account.rich.field'), tr(locale, 'account.rich.value')), entries),))]
        elif view == 'ports':
            rows = [[button(locale, 'node_tools.' + action, f'node_action:{action}:{node_key}')
                    for action in ('check_ports', 'open_ports')]]
        elif view == 'repair':
            actions = ['sync_env'] + (['sync_xray'] if 'xray' in node['protocols'] else []) + ['reconcile_access']
            choices = [button(locale, 'node_tools.' + action, f'node_action:{action}:{node_key}') for action in actions]
            rows = [choices[index:index + 2] for index in range(0, len(choices), 2)]
        else:
            return
    except BackendError as exc:
        lines = [error(locale, exc)]
        rows = [[button(locale, 'node_tools.refresh', query.data)]]
    rows.append([button(locale, 'back', f'node_section:awg:{node_key}' if view == 'entropy' else f'node_tools:{node_key}')])
    await render(bot, query.message.chat.id, Screen(tr(locale, 'node_tools.' + view), tuple(lines), sections=tuple(sections), embedded_buttons=True, navigation=True), rows, state, query.message.message_id)


async def show_action_confirmation(chat_id, message_id, node, action, bot, state, *, title_key=None):
    locale = normalize_locale((await state.get_data()).get('locale'))
    node_key = node['key']
    await remember_node(state, node)
    await state.update_data(node_job_draft={'node_key': node_key, 'action': action,
        'revision': node['desired_revision'], 'command_key': str(uuid4())})
    back = (AdminNodeCallback(node_key=node_key).pack() if action == 'bootstrap' else
            f'bootstrap_menu:{node_key}' if action in {'reinstall_clean', 'reinstall_keep', 'install_docker'} else
            f'node_tools:{node_key}')
    rows = [[button(locale, 'back', back),
             button(locale, 'node_tools.confirm', 'node_job_submit',
                style='danger' if action in {'reinstall_clean', 'cleanup_runtime'} else 'primary')]]
    await render(bot, chat_id, Screen(tr(locale, title_key or 'node_tools.' + action),
        lines=(tr(locale, 'node_tools.effect.' + action),), embedded_buttons=True,
        navigation=True), rows, state, message_id)


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
        await show_action_confirmation(query.message.chat.id, query.message.message_id,
            node, action, bot, state)
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
        if draft['action'] == 'bootstrap':
            job = await backend.bootstrap_node(query.from_user.id, **draft)
            await show_bootstrap(query.message.chat.id, query.from_user.id, query.message.message_id, job, bot, backend, state)
        else:
            job = await backend.node_action(query.from_user.id, **draft)
            await show_job(query.message.chat.id, query.from_user.id, query.message.message_id, job, bot, state, backend=backend)
    except BackendError as exc:
        await render(bot, query.message.chat.id, Screen(tr(locale, 'node_tools.error_title'), (error(locale, exc),), embedded_buttons=True, navigation=True),
            [[button(locale, 'back', AdminNodeCallback(node_key=draft['node_key']).pack())]], state, query.message.message_id)


async def show_bootstrap(chat_id, user_id, message_id, job, bot, backend, state):
    locale = normalize_locale((await state.get_data()).get('locale'))
    phase = job['phase']
    stages = ('agent', 'docker', 'protocols', 'done')
    label = {'agent': 'node_tools.setup_agent', 'docker': 'node_tools.install_docker',
             'protocols': 'node_tools.protocols_stage', 'done': 'node_tools.done.bootstrap'}[phase]
    lines = (tr(locale, 'operation.' + job['status']),
             f"{min(stages.index(phase) + 1, 3)}/3 · {tr(locale, label)}")
    if job['status'] == 'blocked':
        lines += (error(locale, BackendError(job.get('error_code') or 'bootstrap_unconfirmed', 409)),)
        if job.get('error_code'):
            lines += (job['error_code'],)
    if job.get('progress'):
        lines += (tr(locale, 'installation_progress.' + job['progress']['stage']),)
    rows = [[button(locale, 'node_tools.refresh', 'bootstrap_status:' + job['id'])]]
    if job['status'] == 'blocked' and job.get('child_id'):
        from .callbacks import RolloutStatusCallback
        callback = ('node_job:' + job['child_id'] if job['child_kind'] == 'node-jobs' else
                    RolloutStatusCallback(task_id=job['child_id']).pack())
        rows.append([button(locale, 'node_tools.resolve', callback)])
    rows.append([button(locale, 'back', AdminNodeCallback(node_key=job['node_key']).pack())])
    await render(bot, chat_id, Screen(tr(locale, 'node_tools.bootstrap'), lines,
        embedded_buttons=True, navigation=True), rows, state, message_id)
    if job['status'] in {'awaiting_executor', 'running'}:
        async def refresh():
            latest = await backend.node_bootstrap(user_id, job['id'])
            await show_bootstrap(chat_id, user_id, message_id, latest, bot, backend, state)
        schedule_refresh(state, refresh)


@router.callback_query(F.data.startswith('bootstrap_status:'))
async def bootstrap_status_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    job = await backend.node_bootstrap(query.from_user.id, query.data.split(':', 1)[1])
    await show_bootstrap(query.message.chat.id, query.from_user.id, query.message.message_id, job, bot, backend, state)


async def show_job(chat_id, user_id, message_id, job, bot, state, backend=None):
    await remember_label(state, 'node_jobs', job['id'], job['node_key'])
    locale = normalize_locale((await state.get_data()).get('locale'))
    lines = [tr(locale, 'operation.' + job['status'])]
    if job.get('progress'):
        lines.append(tr(locale, 'installation_progress.' + job['progress']['stage']))
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
    sections = []
    if result.get('ports') and job['status'] == 'succeeded':
        lines = [lines[0]] + ([tr(locale, 'node_tools.firewall_unmanaged')] if result.get('firewall') == 'unmanaged' else [])
        sections.append(Section(tr(locale, 'node_tools.ports'), tables=(Table(
            (tr(locale, 'nodes.rich.port'), tr(locale, 'nodes.rich.protocol'), tr(locale, 'nodes.rich.result')),
            tuple((str(item['port']), item['protocol'], tr(locale, 'node_tools.port.' + item['status']))
                  for item in result['ports'])),)))
    sections.append(Section(tr(locale, 'nodes.rich.technical'), collapsed=True,
        lines=(tr(locale, 'nodes.rich.operation_id', value=job['id']),)))
    if job['status'] in {'awaiting_executor', 'running', 'blocked'}:
        rows.append([button(locale, 'node_tools.refresh', 'node_job:' + job['id'])])
    if job['status'] == 'blocked':
        rows.append([button(locale, 'node_tools.resolve', 'node_resolve:' + job['id'])])
    rows.append([button(locale, 'back', AdminNodeCallback(node_key=job['node_key']).pack())])
    await render(bot, chat_id, Screen(tr(locale, 'node_tools.' + job['action']), tuple(lines), sections=tuple(sections), embedded_buttons=True, navigation=True), rows, state, message_id)
    if backend is not None and job['status'] in {'awaiting_executor', 'running'}:
        async def refresh():
            latest = await backend.node_job(user_id, job['id'])
            await show_job(chat_id, user_id, message_id, latest, bot, state, backend=backend)
        schedule_refresh(state, refresh)


@router.callback_query(F.data.startswith('node_job:'))
async def job_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    try:
        job = await backend.node_job(query.from_user.id, query.data.split(':', 1)[1])
        await show_job(query.message.chat.id, query.from_user.id, query.message.message_id, job, bot, state, backend=backend)
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
        [[button(locale, 'back', 'node_job:' + job_id), button(locale, 'node_tools.confirm', 'node_resolve_do:' + job_id, style='primary')]], state, query.message.message_id)


@router.callback_query(F.data.startswith('node_resolve_do:'))
async def resolve_do_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    job_id = query.data.split(':', 1)[1]
    try:
        job = await backend.resolve_node_job(query.from_user.id, job_id)
        await show_job(query.message.chat.id, query.from_user.id, query.message.message_id, job, bot, state, backend=backend)
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
    allow_registry = True
    try:
        result = await backend.remove_node_step(user_id, node_key, retry=retry)
        if result.get('error_code'):
            raise BackendError(result['error_code'], 409)
        if result['status'] in {'removed', 'removed_registry_only', 'removed_unprovisioned'}:
            note = ('node_tools.unprovisioned_removed' if result['status'] == 'removed_unprovisioned'
                    else 'node_tools.removed_note' if result['status'] == 'removed'
                    else 'nodes.maintenance.registry_removed_note')
            await render(bot, chat_id, Screen(tr(locale, 'node_tools.removed'),
                (tr(locale, note),) + ((tr(locale, 'nodes.maintenance.registry_tunnels'),)
                    if result['status'] == 'removed_registry_only' else ()), embedded_buttons=True, navigation=True),
                [[button(locale, 'nodes.card.to_list', AdminNodesCallback().pack())]], state, message_id)
            return
        lines = [tr(locale, 'node_tools.removal_progress')]
        if not result['revocations_complete'] and result['pending_tasks']:
            lines.append(tr(locale, 'nodes.maintenance.pending', count=result['pending_tasks']))
        elif result['revocations_complete']:
            lines.append(tr(locale, 'nodes.maintenance.phase', value=tr(locale,
                'nodes.maintenance.phase.' + (result['cleanup_phase'] or 'not_started'))))
    except BackendError as exc:
        allow_registry = exc.code not in {'resource_not_found', 'permission_denied', 'account_disabled'}
        rows = [[button(locale, 'node_tools.retry', f'remove_retry:{node_key}', style='danger')]]
        if exc.code in {'node_agent_unavailable', 'node_agent_unconfigured'}:
            lines = [tr(locale, 'node_tools.unreachable')]
        elif exc.code == 'host_verification_failed':
            lines = [tr(locale, 'node_tools.verification_failed')]
        elif exc.code == 'node_revocations_blocked':
            lines = [tr(locale, 'node_tools.revocations_blocked')]
        elif exc.code in {'node_cleanup_failed', 'node_cleanup_unavailable'}:
            lines = [tr(locale, 'node_tools.cleanup_failed')]
        elif exc.code in {'independent_verification_key_required', 'verification_key_unavailable'}:
            lines = [tr(locale, 'node_tools.verification_key')]
        else:
            lines = [error(locale, exc)]
    if allow_registry:
        rows.append([InlineKeyboardButton(text=tr(locale, 'nodes.maintenance.registry_only'),
            callback_data=ConfirmRegistryRemovalCallback(node_key=node_key).pack(), style='danger')])
    rows.append([button(locale, 'back', AdminNodeCallback(node_key=node_key).pack())])
    await render(bot, chat_id, Screen(tr(locale, 'nodes.card.delete'),
        (lines[0],), sections=(Section(tr(locale, 'nodes.rich.next_step'),
            lines=tuple(lines[1:]), rows=tuple(tuple(row) for row in rows[:-1])),),
        embedded_buttons=True, navigation=True), rows[-1:], state, message_id)
