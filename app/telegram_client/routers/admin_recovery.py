"""Maintenance inventory and explicit, operation-specific recovery."""
from uuid import uuid4
from aiogram import Bot, F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section, Table, shorten_label
from .common import render
from .callbacks import AdminSettingsCallback

router = Router()


class RecoveryPage(CallbackData, prefix='recpage'):
    offset: int = 0


class RecoveryDiagnostics(CallbackData, prefix='recdiag'):
    pass


class RecoveryHistory(CallbackData, prefix='rechist'):
    offset: int = 0


class AuditPage(CallbackData, prefix='wsaudit'):
    offset: int = 0
    errors: int = 0


class RecoveryAction(CallbackData, prefix='recact'):
    kind: str
    id: str
    action: str
    confirm: int = 0


def action_path(kind, identity, action):
    from uuid import UUID
    identity = str(UUID(identity))
    if kind == 'update' and action in {'cancel', 'recheck'}:
        return f'/api/v1/system/updates/jobs/{identity}/{action}'
    if kind in {'node', 'settings', 'profile', 'agent', 'bootstrap'} and action in {'recheck', 'resolve'}:
        if kind == 'agent' and action != 'recheck':
            raise ValueError('unsupported recovery action')
        return f'/api/v1/system/recovery/{kind}/{identity}/{action}'
    raise ValueError('unsupported recovery action')


async def show(query, bot, backend, state, offset=0, note=''):
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        page = await backend.request('GET', f'/api/v1/system/recovery?offset={offset}', telegram_user_id=query.from_user.id)
    except BackendError:
        await render(bot, query.message.chat.id, Screen(tr(locale, 'recovery.title'),
            (tr(locale, 'settings.error_unavailable'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminSettingsCallback().pack())]], state, query.message.message_id)
        return
    sections = []
    if note:
        sections.append(Section(tr(locale, 'recovery.result'), (note,)))
    if page['maintenance_active']:
        sections.append(Section(tr(locale, 'recovery.maintenance'), (tr(locale, 'recovery.maintenance_text'),)))
    for item in page['items']:
        rows = [tuple(InlineKeyboardButton(text=tr(locale, 'recovery.action.' + action),
                    callback_data=RecoveryAction(kind=item['kind'], id=item['id'], action=action).pack(),
                    style='danger' if action == 'cancel' else 'primary')
                for action in item['actions'])] if item['actions'] else []
        code = item.get('error_code', '')
        reason = tr(locale, 'recovery.cause.' + code)
        if reason == 'recovery.cause.' + code:
            reason = tr(locale, 'recovery.cause.outcome_unconfirmed') + (' (' + code + ')' if code else '')
        hint = tr(locale, 'recovery.hint.' + item.get('next_step', 'worker'))
        if hint.startswith('recovery.hint.'):
            hint = tr(locale, 'recovery.manual')
        details = (item['id'],) + ((tr(locale, 'recovery.profile_id', id=item['subject_id']),) if item.get('subject_id') else ())
        if item.get('related_id'):
            details += (tr(locale, 'recovery.child_id', id=item['related_id']),)
        sections.append(Section(tr(locale, 'recovery.kind.' + item['kind']) + (' · ' + item['node_key'] if item['node_key'] else ''),
            lines=(tr(locale, 'recovery.status.' + item['status']), reason, hint),
            sections=(Section(tr(locale, 'recovery.operation'), lines=details, collapsed=True),),
            rows=tuple(rows), divider_after=True))
    if not page['items']:
        sections.append(Section(tr(locale, 'recovery.operations'), (tr(locale, 'recovery.empty'),)))
    navigation = []
    if page['total'] > page['page_size']:
        buttons = []
        if page['offset']:
            buttons.append(InlineKeyboardButton(text='←', callback_data=RecoveryPage(offset=page['offset']-page['page_size']).pack()))
        if page['offset']+page['page_size'] < page['total']:
            buttons.append(InlineKeyboardButton(text='→', callback_data=RecoveryPage(offset=page['offset']+page['page_size']).pack()))
        navigation.append(buttons)
    navigation += [[InlineKeyboardButton(text=tr(locale, 'recovery.controller'), callback_data=RecoveryDiagnostics().pack())],
                   [InlineKeyboardButton(text=tr(locale, 'recovery.history'), callback_data=RecoveryHistory().pack())],
                   [InlineKeyboardButton(text=tr(locale, 'audit.title'), callback_data=AuditPage().pack())],
                   [InlineKeyboardButton(text=tr(locale, 'recovery.refresh'), callback_data=RecoveryPage(offset=page['offset']).pack())],
                   [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminSettingsCallback().pack())]]
    await render(bot, query.message.chat.id, Screen(tr(locale, 'recovery.title'), sections=tuple(sections),
        embedded_buttons=True, navigation=True), navigation, state, query.message.message_id)


@router.callback_query(RecoveryPage.filter())
async def recovery_page(query: CallbackQuery, callback_data: RecoveryPage, bot: Bot,
                        backend: BackendClient, state: FSMContext):
    await query.answer()
    await state.set_state(None)
    await show(query, bot, backend, state, callback_data.offset)


@router.callback_query(RecoveryAction.filter())
async def recovery_action(query: CallbackQuery, callback_data: RecoveryAction, bot: Bot,
                          backend: BackendClient, state: FSMContext):
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        path = action_path(callback_data.kind, callback_data.id, callback_data.action)
    except ValueError:
        await show(query, bot, backend, state)
        return
    if not callback_data.confirm:
        rows = [[InlineKeyboardButton(text=tr(locale, 'recovery.action.' + callback_data.action),
                    callback_data=RecoveryAction(kind=callback_data.kind, id=callback_data.id,
                        action=callback_data.action, confirm=1).pack(),
                    style='danger' if callback_data.action == 'cancel' else 'primary')],
                [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=RecoveryPage().pack())]]
        await render(bot, query.message.chat.id, Screen(tr(locale, 'recovery.confirm'),
            (tr(locale, 'recovery.explain.' + callback_data.kind + '.' + callback_data.action) if callback_data.kind in {'settings', 'profile', 'agent', 'bootstrap'} else tr(locale, 'recovery.explain.' + callback_data.action), callback_data.id),
            embedded_buttons=True, navigation=True), rows, state, query.message.message_id)
        return
    try:
        result = await backend.request('POST', path, telegram_user_id=query.from_user.id)
        note = tr(locale, 'recovery.result_status', status=tr(locale, 'recovery.status.' + result.get('status', 'unknown')))
        if result.get('status') == 'blocked':
            note = tr(locale, 'recovery.still_blocked')
        if result.get('replacement_id'):
            note += '\n' + tr(locale, 'recovery.replacement', id=result['replacement_id'])
    except BackendError:
        note = tr(locale, 'recovery.unconfirmed')
    await show(query, bot, backend, state, note=note)


async def detail_link(state, title, lines, back):
    entries = dict(list((await state.get_data()).get('recovery_details', {}).items())[-60:])
    token = uuid4().hex[:12]
    entries[token] = {'title': title, 'lines': lines, 'back': back}
    await state.update_data(recovery_details=entries)
    return 'recdetail:' + token


@router.callback_query(F.data.startswith('recdetail:'))
async def recovery_detail(query: CallbackQuery, bot: Bot, state: FSMContext):
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    entry = (await state.get_data()).get('recovery_details', {}).get(query.data.split(':', 1)[1])
    await render(bot, query.message.chat.id, Screen(
        entry['title'] if entry else tr(locale, 'recovery.operation'),
        lines=tuple(entry['lines']) if entry else (tr(locale, 'settings.error_unavailable'),),
        embedded_buttons=True, navigation=True), [[InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=entry['back'] if entry else RecoveryPage().pack())]], state, query.message.message_id)


def audit_action(locale, value):
    action = {'credential': 'credential', 'privileged SSH workstation registration': 'registration',
        'privileged SSH workstation revoke-access': 'revoke',
        'privileged SSH workstation restore-access': 'restore',
        'SSH controller key enrollment': 'enrollment'}.get(value)
    if not action:
        for path, key in (('/system/updates/run', 'update'), ('/system/recovery/', 'recovery'),
                          ('/nodes/', 'node'), ('/profiles/', 'profile')):
            if path in value:
                action = key
                break
    return tr(locale, 'audit.action.' + action) if action else value.replace('/api/v1/', '')


def audit_result(locale, item):
    if item.get('http_status') is not None and item['http_status'] >= 400:
        return tr(locale, 'recovery.check.error')
    if item.get('outcome'):
        return tr(locale, 'audit.outcome.' + item['outcome'])
    return tr(locale, 'audit.phase.' + item['phase'])


@router.callback_query(AuditPage.filter())
async def audit_page(query: CallbackQuery, callback_data: AuditPage, bot: Bot,
                     backend: BackendClient, state: FSMContext):
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        page = await backend.request('GET', f'/api/v1/system/workstation-audit?offset={callback_data.offset}' +
            ('&errors_only=true' if callback_data.errors else ''), telegram_user_id=query.from_user.id)
    except BackendError:
        await show(query, bot, backend, state, note=tr(locale, 'settings.error_unavailable'))
        return
    back = AuditPage(offset=page['offset'], errors=callback_data.errors).pack()
    sections = []
    for index, item in enumerate(page['items']):
        action = audit_action(locale, item['action'])
        result = audit_result(locale, item)
        account_label = item['account_label']
        if account_label.isdecimal():
            account_label = tr(locale, 'audit.telegram_id', id=account_label)
        details = (item['occurred_at'], account_label, item['action'], result,
            tr(locale, 'audit.ssh_user', user=item['ssh_user']), item['device_fingerprint'],
            tr(locale, 'audit.account', id=item['account_id']), tr(locale, 'audit.session', id=item['session_id']),
            tr(locale, 'audit.request', id=item['request_id'] or '—'),
            tr(locale, 'audit.command', id=item['command_id'] or '—'))
        if item.get('http_status') is not None:
            details += ('HTTP ' + str(item['http_status']),)
        if item.get('target'):
            details += (tr(locale, 'audit.target', target=item['target']),
                        tr(locale, 'audit.key', fingerprint=item['key_fingerprint']))
        link = await detail_link(state, action, details, back)
        sections.append(Section('', lines=(item['occurred_at'][:16].replace('T', ' ') + ' · ' + account_label,),
            bold_first_line=True, inline_rows=((result, ' · ', InlineKeyboardButton(text=action + ' →', callback_data=link)),),
            divider_after=index < len(page['items']) - 1))
    filters = [InlineKeyboardButton(text=tr(locale, 'audit.filter.all'), callback_data=AuditPage().pack(),
                                   style='primary' if not callback_data.errors else None),
               InlineKeyboardButton(text=tr(locale, 'audit.filter.errors'), callback_data=AuditPage(errors=1).pack(),
                                   style='primary' if callback_data.errors else None)]
    arrows = []
    if page['offset']:
        arrows.append(InlineKeyboardButton(text='←', callback_data=AuditPage(offset=max(0,page['offset']-page['page_size']), errors=callback_data.errors).pack()))
    if page['offset']+page['page_size'] < page['total']:
        arrows.append(InlineKeyboardButton(text='→', callback_data=AuditPage(offset=page['offset']+page['page_size'], errors=callback_data.errors).pack()))
    rows = ([arrows] if arrows else []) + [[InlineKeyboardButton(text=tr(locale, 'recovery.refresh'), callback_data=back)],
        [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=RecoveryPage().pack())]]
    await render(bot, query.message.chat.id, Screen(tr(locale, 'audit.title'),
        lines=() if sections else (tr(locale, 'audit.empty'),),
        sections=(Section('', rows=(tuple(filters),)), *sections), embedded_buttons=True, navigation=True),
        rows, state, query.message.message_id)


def diagnostic_name(locale, identity):
    names = {'file:current': 'installation', 'file:current/.venv/bin/python': 'python',
        'file:shared/.env': 'environment', 'environment': 'environment_keys',
        'release:current/VERSION': 'version', 'release:current/BUILD_COMMIT': 'build',
        'disk': 'disk', 'database': 'database', 'maintenance': 'maintenance',
        'worker': 'worker', 'api': 'api', 'unit:node-plane-backend.service': 'backend',
        'unit:node-plane-driver.service': 'driver', 'unit:node-plane-telegram.service': 'telegram',
        'unit:node-plane-backend-worker.timer': 'worker_timer'}
    if identity in names:
        return tr(locale, 'recovery.diagnostic.' + names[identity])
    if identity.startswith('blocked:'):
        table = identity.split(':', 1)[1]
        kinds = {'backend_node_jobs': 'node', 'backend_node_settings_tasks': 'settings',
            'backend_agent_rollouts': 'agent', 'backend_node_bootstraps': 'bootstrap',
            'backend_operation_tasks': 'profile', 'backend_update_jobs': 'update',
            'backend_node_removals': 'removal', 'backend_backup_jobs': 'backup'}
        kind = kinds.get(table)
        return tr(locale, 'recovery.kind.' + kind) if kind else table
    return identity


@router.callback_query(RecoveryDiagnostics.filter())
async def controller_diagnostics(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        result = await backend.request('GET', '/api/v1/system/recovery/controller', telegram_user_id=query.from_user.id)
    except BackendError:
        await show(query, bot, backend, state, note=tr(locale, 'settings.error_unavailable'))
        return
    lines = ((tr(locale, 'recovery.diagnostic_summary', errors=result['errors'], warnings=result['warnings'])
        if result['errors'] or result['warnings'] else tr(locale, 'recovery.diagnostic.healthy')),)
    sections = []
    for check in sorted(result['checks'], key=lambda c: {'error': 0, 'warning': 1, 'ok': 2}.get(c['status'], 3)):
        name = diagnostic_name(locale, check['id'])
        link = await detail_link(state, name, (tr(locale, 'recovery.check.' + check['status']),
            check['detail']), RecoveryDiagnostics().pack())
        sections.append(Section('', inline_rows=((tr(locale, 'recovery.check.' + check['status']), ' · ',
                InlineKeyboardButton(text=name + ' →', callback_data=link)),), bold_first_line=True,
            sections=(Section('', lines=(shorten_label(check['detail'], 120),)),) if check['status'] != 'ok' else ()))
    rows = [[InlineKeyboardButton(text=tr(locale, 'recovery.refresh'), callback_data=RecoveryDiagnostics().pack())],
            [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=RecoveryPage().pack())]]
    await render(bot, query.message.chat.id, Screen(tr(locale, 'recovery.controller'), lines=lines,
        sections=tuple(sections), embedded_buttons=True, navigation=True), rows, state, query.message.message_id)


@router.callback_query(RecoveryHistory.filter())
async def recovery_history(query: CallbackQuery, callback_data: RecoveryHistory, bot: Bot,
                           backend: BackendClient, state: FSMContext):
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        page = await backend.request('GET', f'/api/v1/system/recovery/history?offset={callback_data.offset}', telegram_user_id=query.from_user.id)
    except BackendError:
        await show(query, bot, backend, state, note=tr(locale, 'settings.error_unavailable'))
        return
    sections = []
    back = RecoveryHistory(offset=page['offset']).pack()
    for index, item in enumerate(page['items']):
        label = tr(locale, 'recovery.kind.' + item['kind'])
        if item.get('node_title') or item.get('node_key'):
            label += ' · ' + (item.get('node_title') or item['node_key'])
        result = tr(locale, 'recovery.outcome.' + item['outcome']) + ' · ' + tr(locale, 'recovery.action.' + item['action'])
        link = await detail_link(state, label, (item['created_at'], result, item['operation_id'],
            tr(locale, 'audit.account', id=item['actor_id'])), back)
        sections.append(Section('', lines=(item['created_at'][:16].replace('T', ' ') + ' · ' + label,),
            inline_rows=((InlineKeyboardButton(text=result + ' →', callback_data=link),),), bold_first_line=True,
            divider_after=index < len(page['items']) - 1))
    arrows = []
    if page['offset']:
        arrows.append(InlineKeyboardButton(text='←', callback_data=RecoveryHistory(offset=max(0, page['offset']-page['page_size'])).pack()))
    if page['offset'] + page['page_size'] < page['total']:
        arrows.append(InlineKeyboardButton(text='→', callback_data=RecoveryHistory(offset=page['offset']+page['page_size']).pack()))
    rows = ([arrows] if arrows else []) + [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=RecoveryPage().pack())]]
    await render(bot, query.message.chat.id, Screen(tr(locale, 'recovery.history'),
        lines=() if sections else (tr(locale, 'audit.empty'),), sections=tuple(sections),
        embedded_buttons=True, navigation=True), rows, state, query.message.message_id)
