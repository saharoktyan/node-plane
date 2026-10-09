"""Version and fleet-update screens; all effects belong to the backend worker."""
from uuid import uuid4
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from ..backend import BackendClient, BackendError
from ..i18n import tr, normalize_locale
from ..screens import Screen, Section, Table, server_label
from .common import render
from .callbacks import UpdatesCallback, AdminNodeCallback, AdminSettingsCallback

router = Router()


def button(text, data, *, style=None):
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


async def locale(state):
    return normalize_locale((await state.get_data()).get('locale'))


async def failure(query, bot, state):
    lang = await locale(state)
    await render(bot, query.message.chat.id, Screen(tr(lang, 'updates.unavailable'),
        (tr(lang, 'update_tools.retry_note'),), embedded_buttons=True, navigation=True), [[button(tr(lang, 'back'), UpdatesCallback().pack())]],
        state, query.message.message_id)


async def show_versions(query, bot, backend, state, offset=0):
    lang = await locale(state)
    page = await backend.update_versions(query.from_user.id, offset)
    nonce = uuid4().hex[:8]
    await state.update_data(update_catalog=page, update_catalog_nonce=nonce)
    rows = []
    for index, item in enumerate(page['items']):
        marker = {'current': '=', 'upgrade': '↑', 'downgrade': '↓'}.get(item['action'], '–')
        rows.append([button(f"{marker} {item['version']}", f'uv_select:{nonce}:{index}')])
    navigation = []
    if offset:
        navigation.append(button('‹', f'uv_page:{max(0, offset - 8)}'))
    if page.get('next_offset') is not None:
        navigation.append(button('›', f"uv_page:{page['next_offset']}"))
    if navigation:
        rows.append(navigation)
    rows.append([button(tr(lang, 'back'), UpdatesCallback().pack())])
    lines = [tr(lang, 'update_tools.catalog_hint'),
             tr(lang, 'update_tools.page', page=offset // 8 + 1, total=max(1, (page.get('total', 0) + 7) // 8))]
    if page.get('status') == 'error':
        lines.append(tr(lang, 'update_tools.catalog_failed'))
    elif not page['items']:
        lines.append(tr(lang, 'update_tools.empty'))
    await render(bot, query.message.chat.id, Screen(tr(lang, 'update_tools.versions'), tuple(lines), embedded_buttons=True, navigation=True),
                 rows, state, query.message.message_id)


async def confirm(query, bot, state, body, lines):
    lang = await locale(state)
    nonce = uuid4().hex[:8]
    await state.update_data(update_draft={'nonce': nonce, 'body': body, 'key': str(uuid4())})
    await render(bot, query.message.chat.id, Screen(tr(lang, 'update_tools.confirm'), tuple(lines), embedded_buttons=True, navigation=True), [[
        button(tr(lang, 'back'), 'uv_page:0' if body['kind'] == 'version' else UpdatesCallback().pack() if body['kind'] == 'stack' else 'ufleet'),
        button(tr(lang, 'update_tools.install'), f'update_submit:{nonce}', style='primary')]], state, query.message.message_id)


async def confirm_latest(query, bot, backend, state):
    lang = await locale(state)
    overview = await backend.updates_overview(query.from_user.id)
    if overview.get('upstream_ref') and overview.get('branch'):
        return await confirm(query, bot, state, {'kind': 'stack',
            'target_ref': overview['upstream_ref'], 'branch': overview['branch']},
            [tr(lang, 'update_tools.target', value=overview.get('remote_label') or overview['upstream_ref']),
             tr(lang, 'updates.rich.stack_warning')])
    page = await backend.update_versions(query.from_user.id)
    target = next((v for v in page['items'] if (v['allowed'] or v.get('action') == 'current') and
        (v['ref'] == overview.get('upstream_ref') or
         v['version'].lstrip('v') == str(overview.get('remote_version', '')).lstrip('v'))), None)
    if not target:
        return await show_versions(query, bot, backend, state)
    await confirm(query, bot, state, {'kind': 'stack', 'target_ref': target['ref'], 'branch': page['branch']},
        [tr(lang, 'update_tools.target', value=target['version']), tr(lang, 'updates.rich.stack_warning')])


async def show_fleet(query, bot, backend, state, page=0, opened=False):
    lang = await locale(state)
    value = await backend.update_rollout(query.from_user.id)
    lines = [tr(lang, 'update_tools.desired', version=value['desired_version'], commit=value['desired_commit'][:12]),
        tr(lang, 'update_tools.driver', status=tr(lang, 'update_tools.' + value['driver_status']),
           commit=str(value['driver'].get('commit') or '—')[:12])]
    rows = []
    latest = value.get('latest_job')
    pending = latest and latest['status'] in {'awaiting_executor', 'running'}
    if pending:
        rows.append([button(tr(lang, 'update_tools.progress'), f"update_job:{latest['id']}")])
    else:
        if value['agents_required']:
            rows.append([button(tr(lang, 'update_tools.agents'), 'ufleet_confirm:agents', style='primary')])
        if value['runtimes_required']:
            rows.append([button(tr(lang, 'update_tools.runtimes'), 'ufleet_confirm:runtimes', style='primary')])
    if latest and not pending:
        lines.append(tr(lang, 'update_tools.last_batch', status=tr(lang, 'update_tools.status.' + latest['status'])
            if latest['status'] in {'succeeded', 'blocked', 'running', 'awaiting_executor', 'superseded'} else latest['status']))
        rows.append([button(tr(lang, 'update_tools.result'), f"update_job:{latest['id']}")])
    if any(n['agent_status'] == 'unknown' for n in value['nodes']) or value['driver_status'] == 'unknown':
        lines.append(tr(lang, 'update_tools.unknown_note'))
    rows += [[button(tr(lang, 'updates.refresh'), 'ufleet')],
             [button(tr(lang, 'back'), UpdatesCallback().pack())]]
    await render(bot, query.message.chat.id, Screen(tr(lang, 'update_tools.fleet'), tuple(lines),
        sections=(server_sections(lang, value['nodes'], page, 'fleet_nodes', opened=opened),), embedded_buttons=True, navigation=True),
                 rows, state, query.message.message_id)


def state_label(lang, status):
    if status in {'waiting', 'current', 'required', 'unknown'}:
        return tr(lang, 'updates.rich.' + status)
    return tr(lang, 'update_tools.status.' + status)


def server_sections(lang, nodes, page, action, *, opened=False, title_key='updates.rich.servers'):
    from .user import region_sections, server_pagination
    ordered = sorted(nodes, key=lambda n: (n.get('region') or '', n.get('title') or n.get('node_key') or n.get('key') or ''))
    pages = max(1, (len(ordered) + 9) // 10)
    page = min(max(0, page), pages - 1)
    def entry(node):
        key = node.get('node_key') or node.get('key')
        title = server_label({**node, 'title': node.get('title') or key})
        status = node.get('status') or node.get('agent_status', 'unknown')
        lines = [state_label(lang, status)]
        if node.get('runtime_status'):
            lines.append(tr(lang, 'updates.rich.runtime_state', status=state_label(lang, node['runtime_status'])))
        if node.get('error_code'):
            lines.append(tr(lang, 'updates.rich.agent_failed'))
        return Section(title, tuple(lines), heading_size=3, divider_after=True,
            rows=((button(tr(lang, 'alerts.rich.open'), AdminNodeCallback(node_key=key).pack()),),))
    sections = region_sections(ordered[page * 10:page * 10 + 10], lang, entry)
    nav = []
    if pages > 1:
        if page:
            nav.append(button('←', f'{action}:{page - 1}'))
        if page + 1 < pages:
            nav.append(button('→', f'{action}:{page + 1}'))
    return Section(tr(lang, title_key),
        (tr(lang, 'pagination.page', page=page + 1, pages=pages),) if pages > 1 else (),
        collapsed=True, is_open=opened, sections=sections, rows=(tuple(nav),) if nav else ())


async def show_overview(query, bot, backend, state, page=0, opened=False):
    from .admin_settings import UpdateActionCallback
    lang = await locale(state)
    overview = await backend.updates_overview(query.from_user.id)
    latest = overview.get('latest_job')
    try:
        fleet = (await state.get_data()).get('updates_fleet') if opened else None
        if fleet is None:
            fleet = await backend.update_rollout(query.from_user.id)
            await state.update_data(updates_fleet=fleet)
    except BackendError:
        fleet = {'nodes': [], 'driver_status': 'unknown'}
    active = latest and latest['status'] in {'awaiting_executor', 'running'}
    result = (latest or {}).get('result') or {}
    components = result.get('components') or {}
    fallback = 'waiting' if overview.get('update_available') else 'current'
    component_rows = tuple((tr(lang, 'updates.rich.component.' + key), state_label(lang,
        components.get(key) or (fleet.get('driver_status', 'unknown') if key == 'driver' else fallback)))
        for key in ('backend', 'worker', 'driver', 'telegram'))
    sections = [Section(tr(lang, 'updates.rich.controller'), tables=(Table(
        (tr(lang, 'updates.rich.component'), tr(lang, 'announce.rich.state')), component_rows),))]
    action_rows = []
    if active:
        action_rows.append((button(tr(lang, 'update_tools.progress'), f"update_job:{latest['id']}"),))
    elif overview.get('update_supported') and overview.get('update_available'):
        action_rows.append((button(tr(lang, 'updates.run'), UpdateActionCallback(action='run').pack()).model_copy(update={'style': 'primary'}),))
    check = button(tr(lang, 'updates.check'), UpdateActionCallback(action='check').pack())
    if action_rows:
        action_rows[0] += (check,)
    else:
        action_rows.append((check,))
    sections.append(Section('', rows=tuple(action_rows)))
    nodes = [n for n in fleet.get('nodes', []) if n.get('agent_status') == 'required']
    if nodes:
        sections.append(server_sections(lang, nodes, page, 'updates_nodes', opened=opened,
            title_key='updates.rich.outdated_servers'))
    sections.append(Section(tr(lang, 'updates.rich.checks'), rows=((
        button(tr(lang, 'updates.auto_on' if overview.get('auto_check_enabled') else 'updates.auto_off'),
            UpdateActionCallback(action='auto_check').pack()).model_copy(update={
                'style': 'primary' if overview.get('auto_check_enabled') else None}),),)))
    sections.append(Section(tr(lang, 'updates.rich.selection'), rows=((
        button(tr(lang, 'updates.choose_branch'), UpdateActionCallback(action='branch_menu').pack()),
        button(tr(lang, 'update_tools.versions'), 'uv_page:0')),)))
    component_actions = [(button(tr(lang, 'update_tools.fleet'), 'ufleet'),)]
    if latest and not active:
        component_actions.append((button(tr(lang, 'update_tools.result'), f"update_job:{latest['id']}"),))
    sections.append(Section(tr(lang, 'updates.rich.component_actions'), rows=tuple(component_actions)))
    lines = (tr(lang, 'updates.current', value=overview.get('current_label') or overview.get('current_version') or '—'),)
    if overview.get('update_available'):
        lines += (tr(lang, 'updates.available', value=overview.get('remote_label') or overview.get('remote_version') or '—'),)
    if latest and latest['status'] in {'partial', 'rolled_back', 'blocked'}:
        lines += (state_label(lang, latest['status']),)
    await render(bot, query.message.chat.id, Screen(tr(lang, 'updates.title'), lines,
        sections=tuple(sections), embedded_buttons=True, navigation=True),
        [[button(tr(lang, 'updates.refresh'), UpdatesCallback().pack())],
         [button(tr(lang, 'back'), AdminSettingsCallback().pack())]], state, query.message.message_id)


async def show_job(query, bot, backend, state, job_id, page=0, opened=False):
    lang = await locale(state)
    job = await backend.update_job(query.from_user.id, job_id)
    result = job.get('result') or {}
    sections = []
    driver_item = next((i for i in job['items'] if i['node_key'] == '@driver'), None)
    if driver_item:
        sections.append(Section(tr(lang, 'updates.rich.component.driver'),
            (state_label(lang, driver_item['status']),)))
    if job['kind'] == 'stack':
        components = result.get('components') or {}
        sections.append(Section(tr(lang, 'updates.rich.controller'), tables=(Table(
            (tr(lang, 'updates.rich.component'), tr(lang, 'announce.rich.state')),
            tuple((tr(lang, 'updates.rich.component.' + key), state_label(lang, components.get(key, 'waiting')))
                for key in ('backend', 'worker', 'driver', 'telegram'))),)))
    failed = [i for i in job['items'] if i['status'] in {'blocked', 'superseded'}]
    if failed:
        sections.append(Section(tr(lang, 'admin.rich.attention'),
            (tr(lang, 'updates.rich.failed_nodes', count=len(failed)),),
            rows=tuple((button(server_label({**i, 'title': i.get('title') or i['node_key']}),
                AdminNodeCallback(node_key=i['node_key']).pack()),) for i in failed if i['node_key'] != '@driver')))
    nodes = [i for i in job['items'] if i['node_key'] != '@driver']
    if nodes:
        sections.append(server_sections(lang, nodes, page,
            'update_nodes:' + job_id, opened=opened))
    rows = []
    if job['status'] in {'awaiting_executor', 'running'}:
        rows.append([button(tr(lang, 'updates.refresh'), f'update_job:{job_id}')])
    if job['status'] == 'awaiting_executor':
        rows.append([button(tr(lang, 'updates.recovery.cancel'), f'update_cancel:{job_id}')])
    if job['status'] == 'blocked' and job['kind'] == 'stack':
        rows.append([button(tr(lang, 'updates.recovery.recheck'), f'update_recheck:{job_id}')])
    rows.append([button(tr(lang, 'back'), UpdatesCallback().pack())])
    lines = [state_label(lang, job['status'])]
    if job.get('target_ref'):
        lines.append(tr(lang, 'update_tools.target', value=job['target_ref']))
    if job['status'] == 'partial':
        lines.append(tr(lang, 'updates.rich.partial_note'))
    if job['status'] == 'rolled_back':
        lines.append(tr(lang, 'updates.rich.rollback_note'))
    if job['status'] == 'blocked':
        lines.append(tr(lang, 'update_tools.blocked_note'))
    await render(bot, query.message.chat.id, Screen(tr(lang, 'update_tools.result'), tuple(lines),
        sections=tuple(sections), embedded_buttons=True, navigation=True), rows, state, query.message.message_id)


@router.callback_query(F.data.startswith('uv_page:'))
@router.callback_query(F.data.startswith('uv_select:'))
@router.callback_query(F.data.startswith('ufleet_confirm:'))
@router.callback_query(F.data.startswith('update_submit:'))
@router.callback_query(F.data.startswith('update_job:'))
@router.callback_query(F.data.startswith('update_cancel:'))
@router.callback_query(F.data.startswith('update_cancel_do:'))
@router.callback_query(F.data.startswith('update_recheck:'))
@router.callback_query(F.data == 'ufleet')
@router.callback_query(F.data.startswith('fleet_nodes:'))
@router.callback_query(F.data.startswith('updates_nodes:'))
@router.callback_query(F.data.startswith('update_nodes:'))
async def update_tools_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    lang = await locale(state)
    try:
        if query.data.startswith('fleet_nodes:'):
            await show_fleet(query, bot, backend, state, int(query.data.split(':')[1]), opened=True)
        elif query.data.startswith('updates_nodes:'):
            await show_overview(query, bot, backend, state, int(query.data.split(':')[1]), opened=True)
        elif query.data.startswith('update_nodes:'):
            _, job_id, page = query.data.split(':')
            await show_job(query, bot, backend, state, job_id, int(page), opened=True)
        elif query.data.startswith('uv_page:'):
            await show_versions(query, bot, backend, state, max(0, int(query.data.split(':')[1])))
        elif query.data.startswith('uv_select:'):
            _, nonce, index = query.data.split(':')
            data = await state.get_data()
            if nonce != data.get('update_catalog_nonce'):
                return await show_versions(query, bot, backend, state)
            page = data['update_catalog']
            item = page['items'][int(index)]
            if not item['allowed']:
                reason = item.get('reason') or 'unrecognized_version'
                await render(bot, query.message.chat.id, Screen(tr(lang, 'update_tools.versions'),
                    (tr(lang, 'update_tools.reason.' + reason),), embedded_buttons=True, navigation=True), [[button(tr(lang, 'back'), f"uv_page:{page['offset']}")]], state, query.message.message_id)
                return
            await confirm(query, bot, state, {'kind': 'version', 'branch': page['branch'], 'target_ref': item['ref']},
                [tr(lang, 'update_tools.target', value=item['version']),
                 tr(lang, 'update_tools.reason.' + item['reason']), tr(lang, 'update_tools.version_warning')])
        elif query.data == 'ufleet':
            await show_fleet(query, bot, backend, state)
        elif query.data.startswith('ufleet_confirm:'):
            kind = query.data.split(':')[1]
            value = await backend.update_rollout(query.from_user.id)
            if kind not in {'agents', 'runtimes'} or not value[kind + '_required']:
                return await show_fleet(query, bot, backend, state)
            await confirm(query, bot, state, {'kind': kind},
                [tr(lang, 'update_tools.batch_note'),
                 tr(lang, 'update_tools.agents' if kind == 'agents' else 'update_tools.runtimes')])
        elif query.data.startswith('update_submit:'):
            draft = (await state.get_data()).get('update_draft')
            if not draft or draft['nonce'] != query.data.split(':')[1]:
                return await show_versions(query, bot, backend, state)
            job = await backend.run_update(query.from_user.id, draft['body'], draft['key'])
            await show_job(query, bot, backend, state, job['id'])
        elif query.data.startswith('update_cancel:'):
            job_id = query.data.split(':')[1]
            await render(bot, query.message.chat.id, Screen(tr(lang, 'updates.recovery.cancel'),
                (tr(lang, 'updates.recovery.cancel_note'),), embedded_buttons=True, navigation=True),
                [[button(tr(lang, 'back'), f'update_job:{job_id}'),
                  button(tr(lang, 'updates.recovery.cancel'), f'update_cancel_do:{job_id}').model_copy(update={'style': 'danger'})]],
                state, query.message.message_id)
        elif query.data.startswith('update_cancel_do:') or query.data.startswith('update_recheck:'):
            action, job_id = query.data.split(':')
            await backend.request('POST', f'/api/v1/system/updates/jobs/{job_id}/' +
                ('cancel' if action == 'update_cancel_do' else 'recheck'), telegram_user_id=query.from_user.id)
            await show_job(query, bot, backend, state, job_id)
        elif query.data.startswith('update_job:'):
            await show_job(query, bot, backend, state, query.data.split(':')[1])
    except BackendError as exc:
        if exc.code in {'update_cancel_unsafe', 'update_recovery_unconfirmed', 'update_recovery_unavailable'}:
            await render(bot, query.message.chat.id, Screen(tr(lang, 'update_tools.result'),
                (tr(lang, 'updates.recovery.unsafe' if exc.code == 'update_cancel_unsafe' else 'updates.recovery.unconfirmed'),),
                embedded_buttons=True, navigation=True),
                [[button(tr(lang, 'back'), 'update_job:' + query.data.split(':')[-1])]], state, query.message.message_id)
        else:
            await failure(query, bot, state)
    except (ValueError, IndexError, KeyError):
        await failure(query, bot, state)
