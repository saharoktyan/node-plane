"""Alert policy and active conditions backed by the monitoring service."""

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section, Table, server_label
from .callbacks import AdminSettingsCallback, AdminNodeCallback
from .common import render

router = Router()


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


@router.callback_query(F.data == "alerts")
@router.callback_query(F.data.startswith("alert_pref:"))
@router.callback_query(F.data.startswith("alerts_active:"))
@router.callback_query(F.data.startswith("alert_dismiss:"))
@router.callback_query(F.data.in_({'alerts_preview_notice', 'alerts_preview_list'}))
async def alerts_cb(
    query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext
):
    await query.answer()
    locale = normalize_locale((await state.get_data()).get("locale"))
    try:
        value = await backend.alerts_overview(query.from_user.id)
        if query.data.startswith('alert_dismiss:'):
            await backend.dismiss_alert(query.from_user.id, query.data.split(':', 1)[1])
            value = await backend.alerts_overview(query.from_user.id)
        if query.data == 'alerts_preview_notice':
            from datetime import datetime, timezone
            from dataclasses import replace
            from ..alert_delivery import alert_screen
            from ..notification_transport import send_notification
            example = alert_screen({'locale': locale, 'event': {'kind':'node_unreachable',
                'resolved':False, 'at':datetime.now(timezone.utc).isoformat(),
                'payload':{'node_title':tr(locale, 'alerts.example_server')}}})
            example = replace(example, lines=(tr(locale, 'alerts.preview_note'),) + example.lines)
            await send_notification(bot, query.from_user.id, example,
                rows=[[button(tr(locale, 'setup.close'), 'update_notice_close')]])
        if query.data.startswith("alert_pref:"):
            parts = query.data.split(':')
            field = parts[1]
            if field == 'interval_minutes' and len(parts) == 3 and parts[2] in {'5', '15'}:
                change = {field: int(parts[2])}
            elif field in {'enabled', 'notify_resolved'} and len(parts) == 3 and parts[2] in {'on', 'off'}:
                change = {field: parts[2] == 'on'}
            elif field in {'enabled', 'notify_resolved'} and len(parts) == 2:
                change = {field: not value[field]}
            else:
                raise ValueError('invalid preference')
            await backend.alert_preferences(query.from_user.id, change)
            value = await backend.alerts_overview(query.from_user.id)
        if query.data.startswith(("alerts_active:", 'alert_dismiss:')) or query.data == 'alerts_preview_list':
            preview = query.data == 'alerts_preview_list'
            if preview:
                value['active'] = [{'title':tr(locale, 'alerts.example_server'),
                    'kind':kind, 'preview':True} for kind in ('node_unreachable', 'disk_low')]
            requested = query.data.split(':')[1] if query.data.startswith('alerts_active:') else '0'
            if not requested.isdecimal() or len(requested) > 6:
                raise ValueError('invalid page')
            active = value['active']
            offset = min(int(requested) // 8, max(0, (len(active) - 1) // 8)) * 8
            sections = []
            for item in active[offset:offset + 8]:
                details = (tr(locale, 'alerts.observed', at=item['last_seen_at'][:19]),) if item.get('last_seen_at') else ()
                actions = []
                if item.get('node_key'):
                    actions.append(button(tr(locale, 'alerts.rich.open'), AdminNodeCallback(node_key=item['node_key']).pack()))
                if not item.get('dismissed') and (item.get('event_id') or preview):
                    actions.append(button(tr(locale, 'alerts.dismiss'),
                        'alerts' if preview else 'alert_dismiss:' + item['event_id']))
                lines = (server_label(item) + ' · ' + tr(locale, 'alerts.condition.' + item['kind']),)
                if item.get('dismissed'):
                    lines += (tr(locale, 'alerts.dismissed'),)
                sections.append(Section(None, lines, bold_first_line=True,
                    rows=(tuple(actions),) if actions else (),
                    sections=(Section(tr(locale, 'alerts.rich.observations'), details, collapsed=True),)
                        if details else (), divider_after=True))
            nav = []
            if offset:
                nav.append(button('←', f'alerts_active:{offset - 8}'))
            if offset + 8 < len(active):
                nav.append(button('→', f'alerts_active:{offset + 8}'))
            rows = [nav] if nav else []
            rows.append([button(tr(locale, 'back'), 'alerts')])
            await render(bot, query.message.chat.id,
                Screen(tr(locale, 'alerts.active_title'),
                    (tr(locale, 'alerts.preview_note'),) if preview else (tr(locale, 'alerts.none'),) if not active else (),
                    sections=tuple(sections), embedded_buttons=True, navigation=True),
                rows, state, query.message.message_id)
            return
        last = value.get('last_scan') or {}
        counts = value['delivery_counts']
        def choices(field):
            return tuple(button(tr(locale, 'settings.rich.enable' if selected else 'settings.rich.disable'),
                f'alert_pref:{field}:{"on" if selected else "off"}').model_copy(
                    update={'style': 'primary' if value[field] == selected else None})
                for selected in (True, False))
        intervals = tuple(button(tr(locale, 'alerts.minutes', minutes=minutes),
            f'alert_pref:interval_minutes:{minutes}').model_copy(
                update={'style': 'primary' if value['interval_minutes'] == minutes else None}) for minutes in (5, 15))
        sections = [Section(tr(locale, 'alerts.rich.monitoring'),
            (tr(locale, 'alerts.last', at=(last.get('at') or '—')[:19],
                status=tr(locale, 'alerts.status.' + last.get('status', 'never'))),), rows=(choices('enabled'),)),
            Section(tr(locale, 'alerts.rich.interval'), rows=(intervals,)),
            Section(tr(locale, 'alerts.rich.recovery'), rows=(choices('notify_resolved'),))]
        if value['active_count']:
            sections.append(Section(tr(locale, 'admin.rich.attention'),
                (tr(locale, 'alerts.active_count', count=value['active_count']),),
                rows=((button(tr(locale, 'alerts.active_title'), 'alerts_active:0'),),)))
        else:
            sections.append(Section(tr(locale, 'alerts.active_title'), (tr(locale, 'alerts.none'),)))
        sections.append(Section(tr(locale, 'alerts.rich.details'), collapsed=True,
            lines=(tr(locale, 'alerts.thresholds'), tr(locale, 'alerts.unknown_note')),
            tables=(Table((tr(locale, 'announce.rich.state'), tr(locale, 'announce.rich.count')),
                tuple((tr(locale, 'announce.rich.' + kind), str(counts.get(kind, 0)))
                    for kind in ('queued', 'claimed', 'failed', 'unknown'))),)))
        rows = [[button(tr(locale, 'updates.refresh'), 'alerts')],
                [button(tr(locale, 'alerts.preview_notice'), 'alerts_preview_notice'),
                 button(tr(locale, 'alerts.preview_list'), 'alerts_preview_list')],
                [button(tr(locale, 'back'), AdminSettingsCallback().pack())]]
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'alerts.title'), sections=tuple(sections), embedded_buttons=True, navigation=True),
            rows, state, query.message.message_id)
    except (BackendError, ValueError, KeyError):
        await render(
            bot,
            query.message.chat.id,
            Screen(tr(locale, "alerts.title"), (tr(locale, "alerts.error"),), embedded_buttons=True, navigation=True),
            [[button(tr(locale, "back"), AdminSettingsCallback().pack())]],
            state,
            query.message.message_id,
        )
