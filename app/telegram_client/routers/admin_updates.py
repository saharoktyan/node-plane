"""Version and fleet-update screens; all effects belong to the backend worker."""
from uuid import uuid4
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from ..backend import BackendClient, BackendError
from ..i18n import tr, normalize_locale
from ..screens import Screen
from .common import render
from .callbacks import UpdatesCallback, AdminNodeCallback

router = Router()


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


async def locale(state):
    return normalize_locale((await state.get_data()).get('locale'))


async def failure(query, bot, state):
    lang = await locale(state)
    await render(bot, query.message.chat.id, Screen(tr(lang, 'updates.unavailable'),
        (tr(lang, 'update_tools.retry_note'),)), [[button(tr(lang, 'back'), UpdatesCallback().pack())]],
        state, query.message.message_id)


async def show_versions(query, bot, backend, state, offset=0):
    lang = await locale(state)
    page = await backend.update_versions(query.from_user.id, offset)
    nonce = uuid4().hex[:8]
    await state.update_data(update_catalog=page, update_catalog_nonce=nonce)
    rows = []
    for index, item in enumerate(page['items']):
        marker = {'current': '✅', 'upgrade': '⬆️', 'downgrade': '⬇️'}.get(item['action'], '⛔')
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
    await render(bot, query.message.chat.id, Screen(tr(lang, 'update_tools.versions'), tuple(lines)),
                 rows, state, query.message.message_id)


async def confirm(query, bot, state, body, lines):
    lang = await locale(state)
    nonce = uuid4().hex[:8]
    await state.update_data(update_draft={'nonce': nonce, 'body': body, 'key': str(uuid4())})
    await render(bot, query.message.chat.id, Screen(tr(lang, 'update_tools.confirm'), tuple(lines)), [[
        button(tr(lang, 'back'), 'uv_page:0' if body['kind'] == 'version' else 'ufleet'),
        button(tr(lang, 'update_tools.install'), f'update_submit:{nonce}')]], state, query.message.message_id)


async def confirm_latest(query, bot, backend, state):
    lang = await locale(state)
    overview = await backend.updates_overview(query.from_user.id)
    page = await backend.update_versions(query.from_user.id)
    target = next((v for v in page['items'] if v['allowed'] and
        (v['ref'] == overview.get('upstream_ref') or
         v['version'].lstrip('v') == str(overview.get('remote_version', '')).lstrip('v'))), None)
    if not overview.get('update_available') or not target:
        return await show_versions(query, bot, backend, state)
    await confirm(query, bot, state, {'kind': 'version', 'target_ref': target['ref'], 'branch': page['branch']},
        [tr(lang, 'update_tools.target', value=target['version']), tr(lang, 'update_tools.version_warning')])


async def show_fleet(query, bot, backend, state):
    lang = await locale(state)
    value = await backend.update_rollout(query.from_user.id)
    lines = [tr(lang, 'update_tools.desired', version=value['desired_version'], commit=value['desired_commit'][:12]),
        tr(lang, 'update_tools.driver', status=tr(lang, 'update_tools.' + value['driver_status']),
           commit=str(value['driver'].get('commit') or '—')[:12])]
    for node in value['nodes']:
        lines.append(tr(lang, 'update_tools.node', title=node['title'],
            agent=tr(lang, 'update_tools.' + node['agent_status']),
            runtime=tr(lang, 'update_tools.' + node['runtime_status'])))
    rows = []
    latest = value.get('latest_job')
    pending = latest and latest['status'] in {'awaiting_executor', 'running'}
    if pending:
        rows.append([button(tr(lang, 'update_tools.progress'), f"update_job:{latest['id']}")])
    else:
        if value['agents_required']:
            rows.append([button(tr(lang, 'update_tools.agents'), 'ufleet_confirm:agents')])
        if value['runtimes_required']:
            rows.append([button(tr(lang, 'update_tools.runtimes'), 'ufleet_confirm:runtimes')])
    if latest and not pending:
        lines.append(tr(lang, 'update_tools.last_batch', status=tr(lang, 'update_tools.status.' + latest['status'])
            if latest['status'] in {'succeeded', 'blocked', 'running', 'awaiting_executor', 'superseded'} else latest['status']))
        rows.append([button(tr(lang, 'update_tools.result'), f"update_job:{latest['id']}")])
    if any(n['agent_status'] == 'unknown' for n in value['nodes']) or value['driver_status'] == 'unknown':
        lines.append(tr(lang, 'update_tools.unknown_note'))
    rows += [[button(tr(lang, 'updates.refresh'), 'ufleet')],
             [button(tr(lang, 'back'), UpdatesCallback().pack())]]
    await render(bot, query.message.chat.id, Screen(tr(lang, 'update_tools.fleet'), tuple(lines)),
                 rows, state, query.message.message_id)


async def show_job(query, bot, backend, state, job_id):
    lang = await locale(state)
    job = await backend.update_job(query.from_user.id, job_id)
    key = 'update_tools.status.' + job['status']
    lines = [tr(lang, key), tr(lang, 'update_tools.job_note')]
    if job.get('target_ref'):
        lines.insert(1, tr(lang, 'update_tools.target', value=job['target_ref']))
    rows = []
    for item in job['items']:
        title = tr(lang, 'update_tools.driver_name') if item['node_key'] == '@driver' else item['node_key']
        lines.append(f"{title}: {tr(lang, 'update_tools.status.' + item['status'])}")
        if item['status'] == 'blocked' and item['node_key'] != '@driver':
            rows.append([button(item['node_key'], AdminNodeCallback(node_key=item['node_key']).pack())])
    if job['status'] in {'awaiting_executor', 'running'}:
        rows.append([button(tr(lang, 'updates.refresh'), f'update_job:{job_id}')])
    if job['status'] == 'blocked':
        lines.append(tr(lang, 'update_tools.blocked_note'))
    rows.append([button(tr(lang, 'back'), UpdatesCallback().pack())])
    await render(bot, query.message.chat.id, Screen(tr(lang, 'update_tools.result'), tuple(lines)),
                 rows, state, query.message.message_id)


@router.callback_query(F.data.startswith('uv_page:'))
@router.callback_query(F.data.startswith('uv_select:'))
@router.callback_query(F.data.startswith('ufleet_confirm:'))
@router.callback_query(F.data.startswith('update_submit:'))
@router.callback_query(F.data.startswith('update_job:'))
@router.callback_query(F.data == 'ufleet')
async def update_tools_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    lang = await locale(state)
    try:
        if query.data.startswith('uv_page:'):
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
                    (tr(lang, 'update_tools.reason.' + reason),)), [[button(tr(lang, 'back'), f"uv_page:{page['offset']}")]], state, query.message.message_id)
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
        elif query.data.startswith('update_job:'):
            await show_job(query, bot, backend, state, query.data.split(':')[1])
    except (BackendError, ValueError, IndexError, KeyError):
        await failure(query, bot, state)
