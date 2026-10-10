"""Maintenance inventory and explicit, operation-specific recovery."""
from aiogram import Bot, F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section, Table
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


@router.callback_query(AuditPage.filter())
async def audit_page(query: CallbackQuery, callback_data: AuditPage, bot: Bot,
                     backend: BackendClient, state: FSMContext):
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        page = await backend.request('GET', f'/api/v1/system/workstation-audit?offset={callback_data.offset}',
                                     telegram_user_id=query.from_user.id)
    except BackendError:
        await show(query, bot, backend, state, note=tr(locale, 'settings.error_unavailable'))
        return
    sections = [Section(tr(locale, 'audit.attribution'),
                        (tr(locale, 'audit.attribution_text'),), collapsed=True)]
    for item in page['items']:
        result = tr(locale, 'audit.phase.' + item['phase'])
        if item['http_status'] is not None:
            result += ' · HTTP ' + str(item['http_status'])
        if item.get('outcome'):
            result += ' · ' + tr(locale, 'audit.outcome.' + item['outcome'])
        enrollment = ()
        if item.get('target'):
            enrollment = (tr(locale, 'audit.target', target=item['target']),
                          tr(locale, 'audit.key', fingerprint=item['key_fingerprint']))
        action = {
            'credential': 'credential',
            'privileged SSH workstation registration': 'registration',
            'privileged SSH workstation revoke-access': 'revoke',
            'privileged SSH workstation restore-access': 'restore',
            'SSH controller key enrollment': 'enrollment',
        }.get(item['action'])
        action = tr(locale, 'audit.action.' + action) if action else item['action'].replace('/api/v1/system/', '')
        sections.append(Section('', sections=(
            Section(item['occurred_at'][:16].replace('T', ' ') + ' · ' + item['account_label'],
                lines=(item['occurred_at'], item['action'],
                       tr(locale, 'audit.ssh_user', user=item['ssh_user']), item['device_fingerprint'],
                       tr(locale, 'audit.account', id=item['account_id']),
                       tr(locale, 'audit.session', id=item['session_id']),
                       tr(locale, 'audit.request', id=item['request_id'] or '—'),
                       tr(locale, 'audit.command', id=item['command_id'] or '—')) + enrollment,
                collapsed=True),
            Section('', lines=(f'{action} · {result}',)),
        )))
    if not page['items']:
        sections.append(Section(tr(locale, 'audit.events'), (tr(locale, 'audit.empty'),)))
    rows = []
    arrows = []
    if page['offset']:
        arrows.append(InlineKeyboardButton(text='←', callback_data=AuditPage(offset=max(0,page['offset']-page['page_size'])).pack()))
    if page['offset']+page['page_size'] < page['total']:
        arrows.append(InlineKeyboardButton(text='→', callback_data=AuditPage(offset=page['offset']+page['page_size']).pack()))
    if arrows:
        rows.append(arrows)
    rows += [[InlineKeyboardButton(text=tr(locale, 'recovery.refresh'), callback_data=AuditPage(offset=page['offset']).pack())],
             [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=RecoveryPage().pack())]]
    await render(bot, query.message.chat.id, Screen(tr(locale, 'audit.title'), sections=tuple(sections),
        embedded_buttons=True, navigation=True), rows, state, query.message.message_id)


@router.callback_query(RecoveryDiagnostics.filter())
async def controller_diagnostics(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        result = await backend.request('GET', '/api/v1/system/recovery/controller', telegram_user_id=query.from_user.id)
    except BackendError:
        await show(query, bot, backend, state, note=tr(locale, 'settings.error_unavailable'))
        return
    lines = (tr(locale, 'recovery.diagnostic_summary', errors=result['errors'], warnings=result['warnings']),)
    sections = tuple(Section(check['id'], (tr(locale, 'recovery.check.' + check['status']), check['detail']))
                     for check in result['checks'])
    rows = [[InlineKeyboardButton(text=tr(locale, 'recovery.refresh'), callback_data=RecoveryDiagnostics().pack())],
            [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=RecoveryPage().pack())]]
    await render(bot, query.message.chat.id, Screen(tr(locale, 'recovery.controller'), lines=lines,
        sections=sections, embedded_buttons=True, navigation=True), rows, state, query.message.message_id)


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
    sections = tuple(Section(item['created_at'][:16].replace('T', ' ') + ' · ' + tr(locale, 'recovery.kind.' + item['kind']),
        lines=(tr(locale, 'recovery.action.' + item['action']) + ' · ' + tr(locale, 'recovery.outcome.' + item['outcome']),),
        sections=(Section(tr(locale, 'recovery.operation'), (item['operation_id'],
            tr(locale, 'audit.account', id=item['actor_id'])), collapsed=True),)) for item in page['items'])
    arrows = []
    if page['offset']:
        arrows.append(InlineKeyboardButton(text='←', callback_data=RecoveryHistory(offset=max(0, page['offset']-page['page_size'])).pack()))
    if page['offset'] + page['page_size'] < page['total']:
        arrows.append(InlineKeyboardButton(text='→', callback_data=RecoveryHistory(offset=page['offset']+page['page_size']).pack()))
    rows = ([arrows] if arrows else []) + [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=RecoveryPage().pack())]]
    await render(bot, query.message.chat.id, Screen(tr(locale, 'recovery.history'),
        lines=() if sections else (tr(locale, 'audit.empty'),), sections=sections,
        embedded_buttons=True, navigation=True), rows, state, query.message.message_id)
