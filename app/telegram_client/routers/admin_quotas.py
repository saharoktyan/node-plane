"""Traffic quotas for the installation and per profile (VPN plan, section 8)."""
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section
from .callbacks import AdminSettingsCallback
from .common import render

router = Router()


class QuotaInputState(StatesGroup):
    waiting_for_timezone = State()
    waiting_for_anchor = State()

GIB = 1024 ** 3
INSTALLATION = '/api/v1/vpn-policies/installation'
# Field name in callback data -> policy key and the offered values.
FIELDS = {
    'limit': ('quota.bytes', (10 * GIB, 50 * GIB, 100 * GIB, 200 * GIB, 500 * GIB)),
    'reset': ('quota.reset', ('calendar_month', 'monthly_anchor', 'interval', 'none')),
    'days': ('quota.interval_days', (7, 30)),
    'exhausted': ('quota.on_exhausted', ('freeze', 'notify', 'throttle')),
    'throttle': ('quota.throttle_kbit', (512, 1000, 2000, 5000)),
    'tz': ('quota.timezone', ('UTC', 'Europe/Moscow', 'Europe/Kyiv', 'Europe/Berlin', 'Asia/Almaty')),
}


def _key(policy, key):
    return next(item for item in policy['keys'] if item['key'] == key)


def size(value):
    from .user import _traffic_bytes
    return _traffic_bytes(value)


def value_label(locale, field, value):
    if not value:
        return tr(locale, 'quota.none')
    if field == 'limit':
        return f'{value // GIB} GiB' if value % GIB == 0 else size(value)
    if field in {'reset', 'exhausted'}:
        return tr(locale, f'quota.{field}.{value}')
    if field == 'days':
        return tr(locale, 'quota.days', days=value)
    if field == 'throttle':
        from .admin_speed_limits import rate_label
        return rate_label(locale, value)
    return value


def parse(field, raw, *, profile=False):
    """The chosen value, None to inherit, or Ellipsis for no valid choice. No
    limit is the installation default, or 0 to exempt a profile."""
    if raw == 'inherit' or (raw == 'none' and not profile):
        return None
    if raw == 'none' and field == 'limit':
        return 0
    values = FIELDS[field][1]
    value = int(raw) if raw.isdigit() and field in {'limit', 'days', 'throttle'} else raw
    return value if value in values else ...


def usage_line(locale, quota):
    """Used traffic of the open period against its limit, with the next reset."""
    period = quota.get('period')
    if quota.get('limit_bytes') is None or period is None:
        return tr(locale, 'quota.none')
    line = tr(locale, 'quota.usage', used=size(period['used_bytes']), limit=size(quota['limit_bytes']))
    if period.get('end'):
        line += ' · ' + tr(locale, 'quota.until', date=period['end'][:10])
    if period.get('blocked'):
        line += ' · ' + tr(locale, 'quota.suspended')
    elif quota.get('throttled'):
        line += ' · ' + tr(locale, 'quota.throttled', speed=value_label(locale, 'throttle', quota['throttled']))
    return line


def _choices(locale, field, prefix, selected, *, none=()):
    """`none` holds the value that stands for no limit, if offered."""
    values = none + FIELDS[field][1]
    buttons = tuple(InlineKeyboardButton(text=value_label(locale, field, value),
        callback_data=f'{prefix}:{field}:{"none" if not value else value}',
        style='primary' if selected == value else None) for value in values)
    return tuple(buttons[i:i + 3] for i in range(0, len(buttons), 3))


async def _show(query, bot, backend, state, profile_id=None):
    await query.answer()
    await show(query.message.chat.id, query.from_user.id, query.message.message_id, query.data,
               bot, backend, state, profile_id)


async def show(chat_id, user_id, message_id, data, bot, backend, state, profile_id=None):
    from .admin_profiles import _profile_error
    from .admin_settings import _friendly_error
    locale = normalize_locale((await state.get_data()).get('locale'))
    if profile_id is None:
        prefix, path, back, error = 'tquota', INSTALLATION, AdminSettingsCallback().pack(), _friendly_error
    else:
        prefix, path = f'pquota:{profile_id}', f'/api/v1/profiles/{profile_id}/vpn-policies'
        back, error = f'admin_profile_edit:{profile_id}', _profile_error
    field, _, raw = data.removeprefix(prefix).lstrip(':').partition(':')
    rows = [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=back)]]
    lines, sections = (), ()
    try:
        if profile_id is not None and field == 'reset_now':
            # Confirmed reset: the key comes from the confirmation button.
            await backend.request('POST', f'/api/v1/profiles/{profile_id}/quota/reset',
                telegram_user_id=user_id, command=True, command_key=raw)
            field = ''
        if field in ('tz_input', 'anchor_input'):
            await _prompt(chat_id, user_id, message_id, field, prefix, bot, backend, state, profile_id, locale)
            return
        if profile_id is not None and field == 'reset_ask':
            await render(bot, chat_id, Screen(tr(locale, 'quota.reset_title'),
                (tr(locale, 'quota.reset_warning'),), embedded_buttons=True, navigation=True),
                [[InlineKeyboardButton(text=tr(locale, 'quota.reset_now'), style='danger',
                    callback_data=f'{prefix}:reset_now:{uuid4()}')],
                 [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=prefix)]],
                state, message_id)
            return
        policy = await backend.request('GET', path, telegram_user_id=user_id)
        if field in FIELDS and (profile_id is None or field != 'tz'):
            current = _key(policy, FIELDS[field][0])
            value = parse(field, raw, profile=profile_id is not None)
            # The installation default is stored as no value, like a profile that inherits.
            if profile_id is None and value == current['default']:
                value = None
            if value is not ... and value != current['value']:
                policy = await backend.request('PATCH', path, telegram_user_id=user_id, command=True,
                    body={'changes': [{'key': FIELDS[field][0], 'value': value, 'revision': current['revision']}]})
        installation = policy if profile_id is None else await backend.request(
            'GET', INSTALLATION, telegram_user_id=user_id)
        shown = ('limit', 'reset', *(('days',) if _key(policy, 'quota.reset')['effective'] == 'interval' else ()),
                 'exhausted', *(('throttle',) if _key(policy, 'quota.on_exhausted')['effective'] == 'throttle' else ()),
                 *(('tz',) if profile_id is None else ()))
        for name in shown:
            current = _key(policy, FIELDS[name][0])
            if profile_id is None:
                choice_rows = _choices(locale, name, prefix, current['effective'],
                                       none=(None,) if name == 'limit' else ())
                if name == 'tz':
                    custom = current['effective'] not in FIELDS['tz'][1]
                    choice_rows += ((InlineKeyboardButton(
                        text=current['effective'] if custom else tr(locale, 'quota.tz_other'),
                        callback_data=f'{prefix}:tz_input', style='primary' if custom else None),),)
            else:
                inherited = value_label(locale, name, _key(installation, FIELDS[name][0])['effective'])
                inherit = InlineKeyboardButton(text=tr(locale, 'quota.inherit', value=inherited),
                    callback_data=f'{prefix}:{name}:inherit', style='primary' if current['value'] is None else None)
                choice_rows = ((inherit,), *_choices(locale, name, prefix, current['value'],
                                                     none=(0,) if name == 'limit' else ()))
            sections += (Section(tr(locale, 'quota.field.' + name), rows=choice_rows),)
        lines = (tr(locale, 'quota.hint' if profile_id is None else 'quota.profile_hint'),)
        if profile_id is not None:
            quota = await backend.request('GET', f'/api/v1/profiles/{profile_id}/quota',
                                          telegram_user_id=user_id)
            actions = ((InlineKeyboardButton(text=tr(locale, 'quota.reset_now'),
                callback_data=f'{prefix}:reset_ask'),),) if quota.get('period') else ()
            if quota.get('anchor_at') and quota.get('reset') in ('monthly_anchor', 'interval'):
                actions += ((InlineKeyboardButton(text=tr(locale, 'quota.anchor_move'),
                    callback_data=f'{prefix}:anchor_input'),),)
            anchor = (tr(locale, 'quota.anchor', date=_local_date(quota['anchor_at'], quota.get('timezone'))),) \
                if quota.get('anchor_at') and quota.get('reset') in ('monthly_anchor', 'interval') else ()
            usage = Section(tr(locale, 'quota.period'), (usage_line(locale, quota), *anchor), rows=actions)
            sections = (usage, *sections)
    except BackendError as exc:
        lines, sections = (error(locale, exc),), ()
    await render(bot, chat_id, Screen(tr(locale, 'quota.title'), lines, sections=sections,
        embedded_buttons=True, navigation=True), rows, state, message_id)


def _local_date(value, tz):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        return datetime.fromisoformat(value).astimezone(ZoneInfo(tz or 'UTC')).date().isoformat()
    except (ValueError, KeyError):
        return value[:10]


async def _prompt(chat_id, user_id, message_id, field, prefix, bot, backend, state, profile_id, locale):
    """Waits for a time zone name or an anchor date as text."""
    if field == 'anchor_input':
        quota = await backend.request('GET', f'/api/v1/profiles/{profile_id}/quota', telegram_user_id=user_id)
        await state.update_data(quota_input={'kind': 'anchor', 'profile_id': profile_id,
            'revision': quota['anchor_revision'], 'tz': quota.get('timezone') or 'UTC', 'message_id': message_id})
        await state.set_state(QuotaInputState.waiting_for_anchor)
        lines = (tr(locale, 'quota.anchor_prompt', tz=quota.get('timezone') or 'UTC'),)
    else:
        await state.update_data(quota_input={'kind': 'tz', 'message_id': message_id})
        await state.set_state(QuotaInputState.waiting_for_timezone)
        lines = (tr(locale, 'quota.tz_prompt'),)
    await render(bot, chat_id, Screen(tr(locale, 'quota.title'), lines, embedded_buttons=True, navigation=True),
                 [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=prefix)]], state, message_id)


@router.message(QuotaInputState.waiting_for_timezone, F.text)
@router.message(QuotaInputState.waiting_for_anchor, F.text)
async def quota_text(message: Message, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    """A typed time zone or anchor date; invalid input asks again."""
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    from .admin_settings import _friendly_error
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    locale = normalize_locale((await state.get_data()).get('locale'))
    pending = (await state.get_data()).get('quota_input') or {}
    user_id, message_id, value = message.from_user.id, pending.get('message_id'), (message.text or '').strip()
    profile_id = pending.get('profile_id')
    prefix = f'pquota:{profile_id}' if pending.get('kind') == 'anchor' else 'tquota'
    try:
        if pending.get('kind') == 'anchor':
            try:
                day = date.fromisoformat(value)
            except ValueError:
                raise BackendError('invalid_input', 422) from None
            anchor = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(pending['tz']))
            await backend.request('PATCH', f'/api/v1/profiles/{profile_id}/quota', telegram_user_id=user_id,
                command=True, revision=pending['revision'], body={'anchor_at': anchor.isoformat()})
        else:
            policy = await backend.request('GET', INSTALLATION, telegram_user_id=user_id)
            current = _key(policy, 'quota.timezone')
            await backend.request('PATCH', INSTALLATION, telegram_user_id=user_id, command=True,
                body={'changes': [{'key': 'quota.timezone', 'value': None if value == 'UTC' else value,
                                   'revision': current['revision']}]})
    except BackendError as exc:
        await render(bot, message.chat.id, Screen(tr(locale, 'quota.title'), (
            tr(locale, 'quota.input_invalid') if exc.status == 422 else _friendly_error(locale, exc),),
            embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=prefix)]], state, message_id)
        return
    await state.set_state(None)
    await state.update_data(quota_input=None)
    await show(message.chat.id, user_id, message_id, prefix, bot, backend, state, profile_id)


@router.callback_query(F.data.startswith('tquota'))
async def installation_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await _show(query, bot, backend, state)


@router.callback_query(F.data.startswith('pquota:'))
async def profile_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await _show(query, bot, backend, state, query.data.removeprefix('pquota:').partition(':')[0])


def profile_section(locale, profile_id, quota):
    """The profile editor's entry; empty when the quota could not be read."""
    if quota is None:
        return ()
    return (Section(tr(locale, 'quota.title'), (usage_line(locale, quota),), ((InlineKeyboardButton(
        text=tr(locale, 'quota.configure'), callback_data=f'pquota:{profile_id}'),),)),)
