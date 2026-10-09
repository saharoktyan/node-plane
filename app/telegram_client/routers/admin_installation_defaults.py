"""Reviewable defaults for future installations, separate from location templates."""
from types import SimpleNamespace
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton

from ..backend import BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section, Table
from .callbacks import AdminSettingsCallback
from .common import render

router = Router()
PATH = '/api/v1/system/installation-defaults'
EDITABLE = ('awg_port', 'xray_sni', 'xray_fingerprint',
    'xray_tcp_port', 'xray_xhttp_port', 'xray_xhttp_path')


class DefaultField(StatesGroup):
    waiting = State()


def button(text, action, *, selected=False):
    return InlineKeyboardButton(text=text, callback_data='idefault:' + action,
        style='primary' if selected else None)


async def show(query, bot, state, *, error=None):
    data = await state.get_data()
    draft = data['installation_defaults_draft']
    locale = normalize_locale(data.get('locale'))
    settings = draft['settings']
    advanced = data.get('installation_defaults_view') == 'advanced'
    protocol_rows = (tuple(button(tr(locale, 'protocol.' + kind), 'protocol:' + kind,
        selected=kind in draft['protocols']) for kind in ('xray', 'awg')),)
    transport_rows = (tuple(button(tr(locale, 'transport.' + kind), 'transport:' + kind,
        selected=kind in draft['xray_transports']) for kind in ('tcp', 'xhttp')),)
    sections = [] if advanced else [Section(tr(locale, 'defaults.protocols'), rows=protocol_rows)]
    if not advanced and 'xray' in draft['protocols']:
        sections.append(Section(tr(locale, 'defaults.transports'), rows=transport_rows))
    if not advanced and 'awg' in draft['protocols']:
        sections.append(Section(tr(locale, 'defaults.awg'),
            rows=(tuple(button(preset.upper(), 'preset:' + preset,
                selected=settings.get('awg_i1_preset', 'quic') == preset)
                for preset in ('quic', 'dns', 'chaos')),
                (button(tr(locale, 'defaults.auto'), 'auto', selected=settings.get('awg_port_mode', 'auto') == 'auto'),
                 button(tr(locale, 'defaults.manual_value', port=settings['awg_port']) if settings.get('awg_port_mode') == 'manual' and settings.get('awg_port') else tr(locale, 'defaults.manual'),
                    'field:awg_port', selected=settings.get('awg_port_mode') == 'manual')))))
    fields = [f for f in EDITABLE if ('awg' if f.startswith('awg') else 'xray') in draft['protocols']]
    def field_value(field):
        if field == 'awg_port' and settings.get('awg_port_mode', 'auto') == 'auto':
            return tr(locale, 'defaults.auto_value')
        return str(settings.get(field, '—'))
    if advanced:
        for protocol, title in [('awg', 'defaults.awg'), ('xray', 'protocol.xray')]:
            options = [field for field in fields if field.startswith(protocol)]
            if options:
                sections.append(Section(tr(locale, title), tables=(Table(
                    (tr(locale, 'account.rich.field'), tr(locale, 'account.rich.value')),
                    tuple((tr(locale, 'nodes.settings.field.' + f), field_value(f)) for f in options),
                    row_callbacks=tuple('idefault:field:' + f for f in options)),)))
        sections.append(Section('', lines=(tr(locale, 'defaults.edit_hint'),)))
    else:
        sections.append(Section(tr(locale, 'nodes.rich.advanced'), collapsed=True,
            tables=(Table((tr(locale, 'account.rich.field'), tr(locale, 'account.rich.value')),
                tuple((tr(locale, 'nodes.settings.field.' + f), field_value(f)) for f in fields)),)))
        sections.append(Section('', rows=((button(tr(locale, 'defaults.edit_advanced'), 'advanced'),),)))
    rows = [[button(tr(locale, 'defaults.save'), 'save', selected=True), button(tr(locale, 'defaults.reset'), 'reset')],
        [button(tr(locale, 'back'), 'main') if advanced else
         InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminSettingsCallback().pack())]]
    await render(bot, query.message.chat.id, Screen(tr(locale, 'defaults.edit_advanced' if advanced else 'defaults.title'),
        ((error,) if error else ()) + (tr(locale, 'defaults.future_only'),),
        sections=tuple(sections), embedded_buttons=True, navigation=True), rows, state, query.message.message_id)


@router.callback_query(F.data.startswith('idefault:'))
async def defaults_cb(query, bot, backend, state):
    await query.answer()
    await state.set_state(None)
    action = query.data[len('idefault:'):]
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        data = await state.get_data()
        if action == 'open' or not data.get('installation_defaults_draft'):
            current = await backend.request('GET', PATH, telegram_user_id=query.from_user.id)
            await state.update_data(installation_defaults_draft=current,
                installation_defaults_original=current, installation_defaults_view='main')
            await show(query, bot, state)
            return
        draft = dict(data['installation_defaults_draft'])
        draft['settings'] = dict(draft['settings'])
        if action in {'advanced', 'main'}:
            await state.update_data(installation_defaults_view=action)
        elif action.startswith('protocol:'):
            kind = action.split(':')[1]
            if kind not in {'awg', 'xray'}:
                return
            draft['protocols'] = sorted(set(draft['protocols']) ^ {kind})
            if kind == 'xray':
                draft['xray_transports'] = ['tcp', 'xhttp'] if kind in draft['protocols'] else []
        elif action.startswith('transport:'):
            kind = action.split(':')[1]
            if kind not in {'tcp', 'xhttp'}:
                return
            draft['xray_transports'] = sorted(set(draft['xray_transports']) ^ {kind})
        elif action.startswith('preset:'):
            preset = action.split(':')[1]
            if preset not in {'quic', 'dns', 'chaos'}:
                return
            draft['settings'].update(awg_i1_preset=preset, awg_port_mode='auto')
            draft['settings'].pop('awg_port', None)
        elif action == 'auto':
            draft['settings']['awg_port_mode'] = 'auto'
            draft['settings'].pop('awg_port', None)
        elif action == 'reset':
            draft = data.get('installation_defaults_original') or await backend.request(
                'GET', PATH, telegram_user_id=query.from_user.id)
        elif action.startswith('field:'):
            field = action.split(':')[1]
            if field not in EDITABLE:
                return
            await state.update_data(installation_defaults_field=field)
            await state.set_state(DefaultField.waiting)
            await render(bot, query.message.chat.id, Screen(tr(locale, 'nodes.settings.field.' + field),
                (tr(locale, 'nodes.settings.send_value'),), embedded_buttons=True, navigation=True),
                [[button(tr(locale, 'back'), 'back')]], state, query.message.message_id)
            return
        elif action == 'save':
            current = await backend.request('PUT', PATH, telegram_user_id=query.from_user.id,
                revision=draft['revision'], body={k: draft[k] for k in ('protocols', 'xray_transports', 'settings')})
            draft = current
            await state.update_data(installation_defaults_original=current)
        await state.update_data(installation_defaults_draft=draft)
        await show(query, bot, state)
    except BackendError as error:
        if (await state.get_data()).get('installation_defaults_draft'):
            await show(query, bot, state, error=tr(locale,
                'defaults.changed' if error.code == 'revision_conflict' else 'defaults.invalid' if error.status == 422 else 'action.error.retry'))
        else:
            await render(bot, query.message.chat.id, Screen(tr(locale, 'defaults.title'),
                (tr(locale, 'action.error.retry'),), embedded_buttons=True, navigation=True),
                [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=AdminSettingsCallback().pack())]], state, query.message.message_id)


@router.message(DefaultField.waiting, F.text, ~F.text.startswith('/'))
async def field_text(message, bot, state):
    if not message.from_user or message.chat.type != 'private':
        return
    data = await state.get_data()
    field = data.get('installation_defaults_field')
    if field not in EDITABLE or not data.get('installation_defaults_draft'):
        return
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    locale = normalize_locale(data.get('locale'))
    value = message.text.strip()
    query = SimpleNamespace(message=SimpleNamespace(chat=message.chat,
        message_id=data.get('control_message_id')))
    if field.endswith('_port'):
        if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 65535:
            await show(query, bot, state, error=tr(locale, 'defaults.invalid'))
            await state.set_state(None)
            return
        value = int(value)
    draft = dict(data['installation_defaults_draft'])
    draft['settings'] = dict(draft['settings'], **{field: value})
    if field == 'awg_port':
        draft['settings']['awg_port_mode'] = 'manual'
    await state.update_data(installation_defaults_draft=draft)
    await state.set_state(None)
    await show(query, bot, state)
