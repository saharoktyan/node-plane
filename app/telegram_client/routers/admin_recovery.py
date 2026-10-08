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
    if kind == 'node' and action == 'resolve':
        return f'/api/v1/node-jobs/{identity}/resolve'
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
        sections.append(Section(tr(locale, 'recovery.kind.' + item['kind']) + (' · ' + item['node_key'] if item['node_key'] else ''),
            lines=((tr(locale, 'recovery.reason', code=item['error_code']),) if item.get('error_code') else ()) + ((tr(locale, 'recovery.manual'),) if not rows else ()),
            tables=(Table((tr(locale, 'recovery.operation'), tr(locale, 'recovery.status')),
                          ((item['id'], tr(locale, 'recovery.status.' + item['status'])),)),), rows=tuple(rows), divider_after=True))
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
    navigation += [[InlineKeyboardButton(text=tr(locale, 'audit.title'), callback_data=AuditPage().pack())],
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
            (tr(locale, 'recovery.explain.' + callback_data.action), callback_data.id),
            embedded_buttons=True, navigation=True), rows, state, query.message.message_id)
        return
    try:
        result = await backend.request('POST', path, telegram_user_id=query.from_user.id)
        note = tr(locale, 'recovery.result_status', status=tr(locale, 'recovery.status.' + result.get('status', 'unknown')))
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
