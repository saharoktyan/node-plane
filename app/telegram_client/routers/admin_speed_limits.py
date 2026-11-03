"""Speed limits per device for the installation and per profile (VPN plan, section 11d)."""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section
from .callbacks import AdminSettingsCallback
from .common import render

router = Router()

INSTALLATION = '/api/v1/vpn-policies/installation'
FIELDS = {'up': 'speed.up_kbit', 'down': 'speed.down_kbit'}
# Offered limits in kbit/s.
CHOICES = (1_000, 5_000, 10_000, 20_000, 50_000, 100_000)


def _key(policy, key):
    return next(item for item in policy['keys'] if item['key'] == key)


def rate_label(locale, kbit):
    if not kbit:
        return tr(locale, 'speed.none')
    if kbit % 1000 == 0:
        return tr(locale, 'speed.mbit', value=kbit // 1000)
    return tr(locale, 'speed.kbit', value=kbit)


def summary(locale, policy):
    """The effective limits of a policy listing in one line."""
    up, down = (_key(policy, key)['effective'] for key in FIELDS.values())
    if not up and not down:
        return tr(locale, 'speed.none')
    up, down = (rate_label(locale, kbit) if kbit else tr(locale, 'speed.unlimited') for kbit in (up, down))
    return tr(locale, 'speed.summary', up=up, down=down)


def parse(raw, *, profile=False):
    """The chosen value, None to inherit, or Ellipsis for no valid choice.
    No limit is the installation default, or 0 to exempt a profile."""
    if raw == 'inherit' or (raw == 'none' and not profile):
        return None
    if raw == 'none':
        return 0
    return int(raw) if raw.isdigit() and int(raw) in CHOICES else ...


def _choices(locale, field, prefix, selected, none):
    values = (none, *CHOICES)
    buttons = tuple(InlineKeyboardButton(text=rate_label(locale, value),
        callback_data=f'{prefix}:{field}:{"none" if not value else value}',
        style='primary' if selected == value else None) for value in values)
    return tuple(buttons[i:i + 4] for i in range(0, len(buttons), 4))


def delivery_section(locale, delivery):
    """Which nodes enforce the current limits and what their agents report."""
    items = delivery.get('items', ())
    if not items:
        return ()
    current = sum(item['status'] == 'current' for item in items)
    lines = [tr(locale, 'connlimit.nodes_current', current=current, total=len(items))]
    for item in items:
        name = ' '.join(part for part in (item.get('flag'), item.get('title') or item['node_key']) if part)
        if item['status'] != 'current':
            lines.append(tr(locale, 'connlimit.node.' + item['status'], node=name))
        for outcome in item.get('protocols', ()):
            if outcome['state'] in ('failed', 'unsupported'):
                lines.append(tr(locale, 'speed.node.' + outcome['state'], node=name,
                                protocol=tr(locale, 'speed.protocol.' + outcome['protocol']),
                                detail=outcome.get('detail') or '—'))
    return (Section(tr(locale, 'connlimit.nodes'), tuple(lines)),)


async def _show(query, bot, backend, state, profile_id=None):
    from .admin_profiles import _profile_error
    from .admin_settings import _friendly_error
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    if profile_id is None:
        prefix, path, back, error = 'speedlim', INSTALLATION, AdminSettingsCallback().pack(), _friendly_error
    else:
        prefix, path = f'pspeedlim:{profile_id}', f'/api/v1/profiles/{profile_id}/vpn-policies'
        back, error = f'admin_profile_edit:{profile_id}', _profile_error
    field, _, raw = query.data.removeprefix(prefix).lstrip(':').partition(':')
    lines, sections = (), ()
    try:
        policy = await backend.request('GET', path, telegram_user_id=query.from_user.id)
        if field in FIELDS:
            current = _key(policy, FIELDS[field])
            value = parse(raw, profile=profile_id is not None)
            if value is not ... and value != current['value']:
                policy = await backend.request('PATCH', path, telegram_user_id=query.from_user.id, command=True,
                    body={'changes': [{'key': FIELDS[field], 'value': value, 'revision': current['revision']}]})
        installation = policy if profile_id is None else await backend.request(
            'GET', INSTALLATION, telegram_user_id=query.from_user.id)
        for name in FIELDS:
            current = _key(policy, FIELDS[name])
            if profile_id is None:
                rows = _choices(locale, name, prefix, current['effective'] or None, None)
            else:
                inherited = rate_label(locale, _key(installation, FIELDS[name])['effective'])
                inherit = InlineKeyboardButton(text=tr(locale, 'speed.inherit', value=inherited),
                    callback_data=f'{prefix}:{name}:inherit', style='primary' if current['value'] is None else None)
                rows = ((inherit,), *_choices(locale, name, prefix, current['value'], 0))
            sections += (Section(tr(locale, 'speed.field.' + name), rows=rows),)
        if profile_id is None:
            sections += delivery_section(locale, await backend.request(
                'GET', '/api/v1/speed-limits/nodes', telegram_user_id=query.from_user.id))
        lines = (tr(locale, 'speed.hint' if profile_id is None else 'speed.profile_hint'),)
    except BackendError as exc:
        lines, sections = (error(locale, exc),), ()
    await render(bot, query.message.chat.id, Screen(tr(locale, 'speed.title'), lines, sections=sections,
        embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=back)]], state, query.message.message_id)


@router.callback_query(F.data.startswith('speedlim'))
async def installation_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await _show(query, bot, backend, state)


@router.callback_query(F.data.startswith('pspeedlim:'))
async def profile_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await _show(query, bot, backend, state, query.data.removeprefix('pspeedlim:').partition(':')[0])


def profile_section(locale, profile_id, policy):
    """The profile editor's entry; empty when the policy could not be read."""
    if policy is None or not {*FIELDS.values()} <= {item['key'] for item in policy.get('keys', ())}:
        return ()
    own = any(_key(policy, key)['value'] is not None for key in FIELDS.values())
    text = summary(locale, policy) if own else tr(locale, 'speed.inherit', value=summary(locale, policy))
    return (Section(tr(locale, 'speed.title'), (text,), ((InlineKeyboardButton(
        text=tr(locale, 'connlimit.configure'), callback_data=f'pspeedlim:{profile_id}'),),)),)
