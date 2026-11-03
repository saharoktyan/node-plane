"""Overrides of connection and speed limits for one device or one node.

A device value overrides its profile; a node value overrides the
installation on that node. Both screens offer the same fields as the
installation screens, plus "as inherited", which removes the own value.
"""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section
from .callbacks import NodeSettingsCallback
from .common import render

router = Router()

INSTALLATION = '/api/v1/vpn-policies/installation'
# Field in callback data -> policy key and offered values.
FIELDS = {
    'mode': ('connections.mode', ('off', 'observe', 'enforce')),
    'max': ('connections.max_sources', (1, 2, 3, 5)),
    'up': ('speed.up_kbit', (0, 1_000, 5_000, 10_000, 20_000, 50_000, 100_000)),
    'down': ('speed.down_kbit', (0, 1_000, 5_000, 10_000, 20_000, 50_000, 100_000)),
}
TITLES = {'mode': 'connlimit.mode', 'max': 'connlimit.sources', 'up': 'speed.field.up', 'down': 'speed.field.down'}


def label(locale, field, value):
    if field == 'mode':
        return tr(locale, 'connlimit.mode.' + value)
    if field in ('up', 'down'):
        from .admin_speed_limits import rate_label
        return rate_label(locale, value)
    return str(value)


def parse(field, raw):
    """The chosen value, None to inherit, or Ellipsis for no valid choice."""
    if raw == 'inherit':
        return None
    values = FIELDS[field][1]
    value = int(raw) if raw.isdigit() and field != 'mode' else raw
    return value if value in values else ...


def node_value(policy, key, node_key):
    """The node's own installation value of `key`, or None."""
    return next((item for item in policy['values'] if item['key'] == key and item['node_key'] == node_key
                 and item['protocol'] is None), None)


def _sections(locale, fields, own, inherited, button, inherit='connlimit.inherit'):
    """`own` maps field -> (value or None); `inherited` field -> effective value."""
    sections = ()
    for field in fields:
        selected = own[field]
        rows = ((button(tr(locale, inherit, value=label(locale, field, inherited[field])),
                        field, 'inherit', selected is None),),)
        choices = tuple(button(label(locale, field, value), field, str(value), selected == value)
                        for value in FIELDS[field][1])
        rows += tuple(choices[i:i + 4] for i in range(0, len(choices), 4))
        sections += (Section(tr(locale, TITLES[field]), rows=rows),)
    return sections


def summary(locale, values):
    """Own values in one line, or None when everything is inherited."""
    parts = [f"{tr(locale, TITLES[field])}: {label(locale, field, value)}"
             for field, value in values.items() if value is not None]
    return '; '.join(parts) or None


async def show_device(chat_id, user_id, message_id, profile_id, device_id, field, raw, bot, backend, state):
    """Device overrides; buttons are opaque tokens since two ids exceed callback data."""
    from . import user
    from .admin_profiles import _profile_error
    locale = normalize_locale((await state.get_data()).get('locale'))
    path = f'/api/v1/profiles/{profile_id}/devices/{device_id}/vpn-policies'
    back = [[user.button(user_id, tr(locale, 'back'), 'device_card', profile_id, device_id)]]
    try:
        policy = await backend.request('GET', path, telegram_user_id=user_id)
        keys = {item['key']: item for item in policy['keys']}
        if field in FIELDS and (value := parse(field, raw)) is not ... \
                and value != keys[FIELDS[field][0]]['value']:
            current = keys[FIELDS[field][0]]
            policy = await backend.request('PATCH', path, telegram_user_id=user_id, command=True,
                body={'changes': [{'key': current['key'], 'value': value, 'revision': current['revision']}]})
            keys = {item['key']: item for item in policy['keys']}
        profile = await backend.request('GET', f'/api/v1/profiles/{profile_id}/vpn-policies', telegram_user_id=user_id)
        inherited_keys = {item['key']: item['effective'] for item in profile['keys']}
        own = {name: keys[key]['value'] for name, (key, _) in FIELDS.items()}
        inherited = {name: inherited_keys[key] for name, (key, _) in FIELDS.items()}

        def button(text, name, value, selected):
            return user.button(user_id, text, 'device_policy', profile_id, device_id, name, value).model_copy(
                update={'style': 'primary' if selected else None})
        screen = Screen(tr(locale, 'override.device_title'), (tr(locale, 'override.device_hint'),),
                        sections=_sections(locale, FIELDS, own, inherited, button, 'override.inherit_profile'),
                        embedded_buttons=True, navigation=True)
    except BackendError as exc:
        screen = Screen(tr(locale, 'override.device_title'), (_profile_error(locale, exc),),
                        embedded_buttons=True, navigation=True)
    await render(bot, chat_id, screen, back, state, message_id)


@router.callback_query(F.data.startswith('nodepol:'))
async def node_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    """Node values of the installation policies: `nodepol:<node>[:<field>:<value>]`."""
    from .admin_settings import _friendly_error
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    node_key, _, rest = query.data.removeprefix('nodepol:').partition(':')
    field, _, raw = rest.partition(':')
    back = [[InlineKeyboardButton(text=tr(locale, 'back'),
                                  callback_data=NodeSettingsCallback(node_key=node_key).pack())]]
    try:
        policy = await backend.request('GET', INSTALLATION, telegram_user_id=query.from_user.id)
        if field in FIELDS and (value := parse(field, raw)) is not ...:
            own = node_value(policy, FIELDS[field][0], node_key)
            if value != (own['value'] if own else None):
                policy = await backend.request('PATCH', INSTALLATION, telegram_user_id=query.from_user.id,
                    command=True, body={'changes': [{'key': FIELDS[field][0], 'node_key': node_key, 'value': value,
                                                     'revision': own['revision'] if own else 0}]})
        keys = {item['key']: item['effective'] for item in policy['keys']}
        own = {name: (item['value'] if (item := node_value(policy, key, node_key)) else None)
               for name, (key, _) in FIELDS.items()}
        inherited = {name: keys[key] for name, (key, _) in FIELDS.items()}

        def button(text, name, value, selected):
            return InlineKeyboardButton(text=text, callback_data=f'nodepol:{node_key}:{name}:{value}',
                                        style='primary' if selected else None)
        screen = Screen(tr(locale, 'override.node_title'), (tr(locale, 'override.node_hint'),),
                        sections=_sections(locale, FIELDS, own, inherited, button),
                        embedded_buttons=True, navigation=True)
    except BackendError as exc:
        screen = Screen(tr(locale, 'override.node_title'), (_friendly_error(locale, exc),),
                        embedded_buttons=True, navigation=True)
    await render(bot, query.message.chat.id, screen, back, state, query.message.message_id)


def node_section(locale, node, policy):
    """The node settings entry with the node's own values."""
    if policy is None or not {key for key, _ in FIELDS.values()} <= {item.get('key') for item in policy.get('keys', ())}:
        return ()
    own = {name: (item['value'] if (item := node_value(policy, key, node['key'])) else None)
           for name, (key, _) in FIELDS.items()}
    return (Section(tr(locale, 'override.node_title'), (summary(locale, own) or tr(locale, 'override.inherited'),),
        ((InlineKeyboardButton(text=tr(locale, 'connlimit.configure'), callback_data=f"nodepol:{node['key']}"),),)),)
