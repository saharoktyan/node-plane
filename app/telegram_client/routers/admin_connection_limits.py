"""Concurrent VLESS source limits for the installation and per profile (VPN plan, section 5)."""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..backend import BackendClient, BackendError
from .callbacks import AdminSettingsCallback
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section
from .common import render

router = Router()

MODES = ('off', 'observe', 'enforce')
SOURCE_CHOICES = (1, 2, 3, 5)
FIELDS = {'mode': 'connections.mode', 'max': 'connections.max_sources'}
INSTALLATION = '/api/v1/vpn-policies/installation'


def _key(policy, key):
    return next(item for item in policy['keys'] if item['key'] == key)


def value_label(locale, field, value):
    return tr(locale, 'connlimit.mode.' + value) if field == 'mode' else str(value)


def summary(locale, policy):
    """The effective limit of a policy listing in one line."""
    mode, sources = _key(policy, FIELDS['mode'])['effective'], _key(policy, FIELDS['max'])['effective']
    if mode == 'off':
        return tr(locale, 'connlimit.mode.off')
    return tr(locale, 'connlimit.summary', mode=value_label(locale, 'mode', mode), sources=sources)


def parse(field, raw):
    """The chosen value, None to inherit, or Ellipsis for no valid choice."""
    if raw == 'inherit':
        return None
    if field == 'mode':
        return raw if raw in MODES else ...
    return int(raw) if raw.isdigit() and int(raw) in SOURCE_CHOICES else ...


def _choices(locale, field, prefix, selected):
    return tuple(InlineKeyboardButton(text=value_label(locale, field, value), callback_data=f'{prefix}:{field}:{value}',
        style='primary' if selected == value else None) for value in (MODES if field == 'mode' else SOURCE_CHOICES))


async def _show(query, bot, backend, state, profile_id=None):
    from .admin_profiles import _profile_error
    from .admin_settings import _friendly_error
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    if profile_id is None:
        prefix, path, back, error = 'connlimit', INSTALLATION, AdminSettingsCallback().pack(), _friendly_error
    else:
        prefix, path = f'pconnlimit:{profile_id}', f'/api/v1/profiles/{profile_id}/vpn-policies'
        back, error = f'admin_profile_edit:{profile_id}', _profile_error
    field, _, raw = query.data.removeprefix(prefix).lstrip(':').partition(':')
    lines, sections = (), ()
    try:
        policy = await backend.request('GET', path, telegram_user_id=query.from_user.id)
        if field in FIELDS:
            current = _key(policy, FIELDS[field])
            value = parse(field, raw)
            # The installation default is stored as no value, like a profile that inherits.
            if profile_id is None and value == current['default']:
                value = None
            if value is not ... and value != current['value']:
                policy = await backend.request('PATCH', path, telegram_user_id=query.from_user.id, command=True,
                    body={'changes': [{'key': FIELDS[field], 'value': value, 'revision': current['revision']}]})
        installation = policy if profile_id is None else await backend.request(
            'GET', INSTALLATION, telegram_user_id=query.from_user.id)
        for name, title in (('mode', 'connlimit.mode'), ('max', 'connlimit.sources')):
            current = _key(policy, FIELDS[name])
            if profile_id is None:
                rows = (_choices(locale, name, prefix, current['effective']),)
            else:
                inherited = value_label(locale, name, _key(installation, FIELDS[name])['effective'])
                inherit = InlineKeyboardButton(text=tr(locale, 'connlimit.inherit', value=inherited),
                    callback_data=f'{prefix}:{name}:inherit', style='primary' if current['value'] is None else None)
                rows = ((inherit,), _choices(locale, name, prefix, current['value']))
            sections += (Section(tr(locale, title), rows=rows),)
        lines = (tr(locale, 'connlimit.hint' if profile_id is None else 'connlimit.profile_hint'),
                 tr(locale, 'connlimit.timing', window=_key(installation, 'connections.window_seconds')['effective'],
                    cooldown=_key(installation, 'connections.cooldown_seconds')['effective']))
    except BackendError as exc:
        lines, sections = (error(locale, exc),), ()
    await render(bot, query.message.chat.id, Screen(tr(locale, 'connlimit.title'), lines, sections=sections,
        embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=back)]], state, query.message.message_id)


@router.callback_query(F.data.startswith('connlimit'))
async def installation_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await _show(query, bot, backend, state)


@router.callback_query(F.data.startswith('pconnlimit:'))
async def profile_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await _show(query, bot, backend, state, query.data.removeprefix('pconnlimit:').partition(':')[0])


def profile_section(locale, profile_id, policy):
    """The profile editor's entry; empty when the policy could not be read."""
    if policy is None or not {*FIELDS.values()} <= {item['key'] for item in policy.get('keys', ())}:
        return ()
    own = any(_key(policy, key)['value'] is not None for key in FIELDS.values())
    text = summary(locale, policy) if own else tr(locale, 'connlimit.inherit', value=summary(locale, policy))
    return (Section(tr(locale, 'connlimit.title'), (text,), ((InlineKeyboardButton(
        text=tr(locale, 'connlimit.configure'), callback_data=f'pconnlimit:{profile_id}'),),)),)
