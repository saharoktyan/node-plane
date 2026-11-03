"""Tariffs: the catalog in settings and the tariff of one profile.

A tariff's limits stay linked to its profiles; its term and nodes are
copied when it is assigned (backend.tariffs). Callback data carries only
fields and values: the open tariff and profile live in the FSM state,
since two UUIDs do not fit into 64 bytes.
"""
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section, Table
from .common import render

router = Router()

TERMS = (7, 30, 90, 180, 365)
GIB = 1024 ** 3
# Field in callback data -> policy key and offered values. None removes the
# tariff's value; 0 is "no limit", which exempts from the installation value.
FIELDS = {
    'dev': ('devices.max', (1, 2, 3, 5, 10)),
    'mode': ('connections.mode', ('off', 'observe', 'enforce')),
    'max': ('connections.max_sources', (1, 2, 3, 5)),
    'up': ('speed.up_kbit', (0, 1_000, 5_000, 10_000, 20_000, 50_000, 100_000)),
    'down': ('speed.down_kbit', (0, 1_000, 5_000, 10_000, 20_000, 50_000, 100_000)),
    'limit': ('quota.bytes', (0, 10 * GIB, 50 * GIB, 100 * GIB, 200 * GIB, 500 * GIB)),
    'reset': ('quota.reset', ('calendar_month', 'monthly_anchor', 'interval', 'none')),
    'days': ('quota.interval_days', (7, 30)),
    'exhausted': ('quota.on_exhausted', ('freeze', 'notify', 'throttle')),
    'throttle': ('quota.throttle_kbit', (512, 1000, 2000, 5000)),
}
TITLES = {'dev': 'devlimit.title', 'mode': 'connlimit.mode', 'max': 'connlimit.sources',
          'up': 'speed.field.up', 'down': 'speed.field.down', 'limit': 'quota.field.limit',
          'reset': 'quota.field.reset', 'days': 'quota.field.days', 'exhausted': 'quota.field.exhausted',
          'throttle': 'quota.field.throttle'}
ERRORS = {'tariff_exists', 'tariff_in_use', 'tariff_without_term', 'tariff_without_access',
          'admin_access_permanent', 'revision_conflict', 'tariff_not_found'}


class TariffTitle(StatesGroup):
    waiting = State()


async def _locale(state):
    return normalize_locale((await state.get_data()).get('locale'))


def button(text, data, *, style=None):
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def error_text(locale, exc):
    return tr(locale, 'tariff.error.' + exc.code if exc.code in ERRORS else 'tariff.error.failed')


def value_label(locale, field, value):
    if field == 'dev':
        return str(value)
    if field in ('mode', 'max'):
        from .admin_connection_limits import value_label as connection_label
        return connection_label(locale, field, value)
    if field in ('up', 'down'):
        from .admin_speed_limits import rate_label
        return rate_label(locale, value)
    from .admin_quotas import value_label as quota_label
    return quota_label(locale, field, value)


def term_label(locale, days):
    return tr(locale, 'tariff.term.none') if days is None else tr(locale, 'tariff.term.days', days=days)


def access_label(locale, policy, regions):
    if policy is None:
        return tr(locale, 'tariff.nodes.keep')
    if any(rule['scope'] == 'all' for rule in policy['rules']):
        return tr(locale, 'tariff.nodes.all')
    names = [regions.get(rule['region_id'], '?') for rule in policy['rules']]
    return ', '.join(names) if names else tr(locale, 'tariff.nodes.none')


def limits_lines(locale, policies):
    lines = [f"{tr(locale, TITLES[field])}: {value_label(locale, field, policies[key])}"
             for field, (key, _) in FIELDS.items() if key in policies]
    return lines or [tr(locale, 'tariff.limits.empty')]


async def _regions(backend, user_id):
    from .admin_profiles import _all_policy_regions
    return await _all_policy_regions(backend, user_id)


async def show_list(chat_id, user_id, message_id, bot, backend, state, note=None):
    locale = await _locale(state)
    await state.set_state(None)
    listed = await backend.request('GET', '/api/v1/tariffs', telegram_user_id=user_id)
    rows = [[button(f"{item['title']} · {term_label(locale, item['duration_days'])} · "
                    + tr(locale, 'tariff.profiles', count=item['profiles']), f"trf:{item['id']}")]
            for item in listed['items']]
    rows.append([button(tr(locale, 'tariff.create'), 'trf_new', style='primary')])
    rows.append([button(tr(locale, 'back'), 'admin_settings')])
    lines = (*((note,) if note else ()), tr(locale, 'tariff.hint') if listed['items'] else tr(locale, 'tariff.empty'))
    await render(bot, chat_id, Screen(tr(locale, 'tariff.title'), lines, embedded_buttons=True, navigation=True),
                 rows, state, message_id)


async def show_tariff(chat_id, user_id, message_id, tariff_id, bot, backend, state, note=None):
    locale = await _locale(state)
    await state.set_state(None)
    tariff = await backend.request('GET', f'/api/v1/tariffs/{tariff_id}', telegram_user_id=user_id)
    regions = {region['id']: region['title'] for region in await _regions(backend, user_id)}
    await state.update_data(tariff_id=tariff['id'], tariff_revision=tariff['revision'])
    summary = Table((tr(locale, 'maintenance.rich.field'), tr(locale, 'maintenance.rich.value')), (
        (tr(locale, 'tariff.term'), term_label(locale, tariff['duration_days'])),
        (tr(locale, 'tariff.nodes'), access_label(locale, tariff['access_policy'], regions)),
        (tr(locale, 'tariff.profiles_title'), str(tariff['profiles']))))
    sections = (
        Section(tariff['title'], tables=(summary,), rows=(
            (button(tr(locale, 'tariff.term'), 'trf_term'), button(tr(locale, 'tariff.nodes'), 'trf_nodes')),
            (button(tr(locale, 'tariff.rename'), 'trf_rename'),))),
        Section(tr(locale, 'tariff.limits'), tuple(limits_lines(locale, tariff['policies'])),
                rows=((button(tr(locale, 'tariff.limits.edit'), 'trf_limits'),),)),
        Section(tr(locale, 'settings.rich.danger'), collapsed=True,
                rows=((button(tr(locale, 'tariff.delete'), 'trf_del', style='danger'),),)))
    await render(bot, chat_id, Screen(tr(locale, 'tariff.card'), (note,) if note else (), sections=sections,
                 embedded_buttons=True, navigation=True), [[button(tr(locale, 'back'), 'tariffs')]], state, message_id)


async def _edit(query, backend, state, body):
    data = await state.get_data()
    return await backend.request('PATCH', f"/api/v1/tariffs/{data['tariff_id']}", telegram_user_id=query.from_user.id,
                                 body=body, revision=data['tariff_revision'])


@router.callback_query(F.data == 'tariffs')
async def tariffs_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    await show_list(query.message.chat.id, query.from_user.id, query.message.message_id, bot, backend, state)


@router.callback_query(F.data.startswith('trf:'))
async def tariff_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    try:
        await show_tariff(query.message.chat.id, query.from_user.id, query.message.message_id,
                          query.data.split(':', 1)[1], bot, backend, state)
    except BackendError as exc:
        await show_list(query.message.chat.id, query.from_user.id, query.message.message_id, bot, backend, state,
                        error_text(await _locale(state), exc))


@router.callback_query(F.data.in_({'trf_new', 'trf_rename'}))
async def title_prompt_cb(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await query.answer()
    locale = await _locale(state)
    creating = query.data == 'trf_new'
    if creating:
        await state.update_data(tariff_id=None)
    await state.set_state(TariffTitle.waiting)
    await state.update_data(tariff_title_message=query.message.message_id)
    back = 'tariffs' if creating else 'trf_back'
    await render(bot, query.message.chat.id, Screen(tr(locale, 'tariff.create' if creating else 'tariff.rename'),
        (tr(locale, 'tariff.title_prompt'),), embedded_buttons=True, navigation=True),
        [[button(tr(locale, 'back'), back)]], state, query.message.message_id)


@router.message(TariffTitle.waiting, F.text)
async def title_message(message: Message, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    data = await state.get_data()
    if message.chat.type != 'private' or not message.from_user:
        return
    try:
        await message.delete()
    except Exception:
        pass
    locale, message_id = normalize_locale(data.get('locale')), data.get('tariff_title_message')
    title = message.text.strip()
    try:
        if data.get('tariff_id'):
            tariff = await backend.request('PATCH', f"/api/v1/tariffs/{data['tariff_id']}",
                telegram_user_id=message.from_user.id, body={'title': title}, revision=data['tariff_revision'])
        else:
            tariff = await backend.request('POST', '/api/v1/tariffs', telegram_user_id=message.from_user.id,
                                           body={'title': title})
    except BackendError as exc:
        await render(bot, message.chat.id, Screen(tr(locale, 'tariff.title'),
            (error_text(locale, exc) if exc.code != 'invalid_input' else tr(locale, 'tariff.title_prompt'),),
            embedded_buttons=True, navigation=True), [[button(tr(locale, 'back'), 'tariffs')]], state, message_id)
        return
    await show_tariff(message.chat.id, message.from_user.id, message_id, tariff['id'], bot, backend, state)


@router.callback_query(F.data == 'trf_back')
async def back_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    data = await state.get_data()
    if not data.get('tariff_id'):
        return await show_list(query.message.chat.id, query.from_user.id, query.message.message_id, bot, backend, state)
    await show_tariff(query.message.chat.id, query.from_user.id, query.message.message_id,
                      data['tariff_id'], bot, backend, state)


@router.callback_query(F.data.startswith('trf_term'))
async def term_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    locale, data = await _locale(state), await state.get_data()
    if not data.get('tariff_id'):
        return
    choice = query.data.partition(':')[2]
    try:
        if choice:
            days = None if choice == 'none' else int(choice) if choice.isdigit() and int(choice) in TERMS else ...
            if days is not ...:
                await _edit(query, backend, state, {'duration_days': days})
                return await show_tariff(query.message.chat.id, query.from_user.id, query.message.message_id,
                                         data['tariff_id'], bot, backend, state)
        tariff = await backend.request('GET', f"/api/v1/tariffs/{data['tariff_id']}", telegram_user_id=query.from_user.id)
    except BackendError as exc:
        return await show_tariff(query.message.chat.id, query.from_user.id, query.message.message_id,
                                 data['tariff_id'], bot, backend, state, error_text(locale, exc))
    current = tariff['duration_days']
    choices = tuple(button(term_label(locale, days), f'trf_term:{days}', style='primary' if current == days else None)
                    for days in TERMS)
    rows = ((button(term_label(locale, None), 'trf_term:none', style='primary' if current is None else None),),
            choices[:3], choices[3:])
    await render(bot, query.message.chat.id, Screen(tr(locale, 'tariff.term'), (tr(locale, 'tariff.term_hint'),),
        sections=(Section('', rows=rows),), embedded_buttons=True, navigation=True),
        [[button(tr(locale, 'back'), 'trf_back')]], state, query.message.message_id)


@router.callback_query(F.data.startswith('trf_nodes'))
async def nodes_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    """None keeps a profile's nodes; otherwise every node or chosen regions."""
    await query.answer()
    locale, data = await _locale(state), await state.get_data()
    if not data.get('tariff_id'):
        return
    choice = query.data.partition(':')[2]
    try:
        tariff = await backend.request('GET', f"/api/v1/tariffs/{data['tariff_id']}", telegram_user_id=query.from_user.id)
        regions = await _regions(backend, query.from_user.id)
        policy = tariff['access_policy']
        changed = ...
        if choice == 'keep':
            changed = None
        elif choice == 'all':
            changed = {'explicit_grants': [], 'exclusions': [],
                       'rules': [{'scope': 'all', 'region_id': None, 'protocols': ['awg', 'xray']}]}
        elif choice.isdigit() and int(choice) < len(regions):
            region = regions[int(choice)]['id']
            rules = [] if policy is None else [r for r in policy['rules'] if r['scope'] == 'region']
            if any(rule['region_id'] == region for rule in rules):
                rules = [rule for rule in rules if rule['region_id'] != region]
            else:
                rules.append({'scope': 'region', 'region_id': region, 'protocols': ['awg', 'xray']})
            changed = {'explicit_grants': [], 'exclusions': [], 'rules': rules} if rules else None
        if changed is not ...:
            await state.update_data(tariff_revision=tariff['revision'])
            tariff = await _edit(query, backend, state, {'access_policy': changed})
            await state.update_data(tariff_revision=tariff['revision'])
            policy = tariff['access_policy']
    except BackendError as exc:
        return await show_tariff(query.message.chat.id, query.from_user.id, query.message.message_id,
                                 data['tariff_id'], bot, backend, state, error_text(locale, exc))
    every = policy is not None and any(rule['scope'] == 'all' for rule in policy['rules'])
    chosen = set() if policy is None else {rule['region_id'] for rule in policy['rules'] if rule['scope'] == 'region'}
    region_buttons = tuple(button(region['title'], f'trf_nodes:{index}',
                                  style='primary' if region['id'] in chosen else None)
                           for index, region in enumerate(regions[:30]))
    rows = ((button(tr(locale, 'tariff.nodes.keep'), 'trf_nodes:keep', style='primary' if policy is None else None),
             button(tr(locale, 'tariff.nodes.all'), 'trf_nodes:all', style='primary' if every else None)),
            *(region_buttons[i:i + 3] for i in range(0, len(region_buttons), 3)))
    await render(bot, query.message.chat.id, Screen(tr(locale, 'tariff.nodes'), (tr(locale, 'tariff.nodes_hint'),),
        sections=(Section('', rows=rows),), embedded_buttons=True, navigation=True),
        [[button(tr(locale, 'back'), 'trf_back')]], state, query.message.message_id)


@router.callback_query(F.data.startswith('trf_limits'))
async def limits_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    """`trf_limits` lists fields, `trf_limits:<field>` offers values,
    `trf_limits:<field>:<value|unset>` stores one."""
    await query.answer()
    locale, data = await _locale(state), await state.get_data()
    if not data.get('tariff_id'):
        return
    _, field, raw = (query.data.split(':') + ['', ''])[:3]
    if field and field not in FIELDS:
        return
    try:
        tariff = await backend.request('GET', f"/api/v1/tariffs/{data['tariff_id']}", telegram_user_id=query.from_user.id)
        if field and raw:
            key, values = FIELDS[field]
            value = None if raw == 'unset' else int(raw) if raw.isdigit() and field not in ('mode', 'reset', 'exhausted') else raw
            if value is None or value in values:
                policies = {k: v for k, v in tariff['policies'].items() if k != key}
                if value is not None:
                    policies[key] = value
                await state.update_data(tariff_revision=tariff['revision'])
                tariff = await _edit(query, backend, state, {'policies': policies})
                await state.update_data(tariff_revision=tariff['revision'])
                field = ''
    except BackendError as exc:
        return await show_tariff(query.message.chat.id, query.from_user.id, query.message.message_id,
                                 data['tariff_id'], bot, backend, state, error_text(locale, exc))
    policies = tariff['policies']
    if not field:
        buttons = tuple(button(f"{tr(locale, TITLES[name])}: " + (value_label(locale, name, policies[key])
                        if key in policies else tr(locale, 'tariff.limits.unset')), f'trf_limits:{name}')
                        for name, (key, _) in FIELDS.items())
        await render(bot, query.message.chat.id, Screen(tr(locale, 'tariff.limits'), (tr(locale, 'tariff.limits_hint'),),
            sections=(Section('', rows=tuple((b,) for b in buttons)),), embedded_buttons=True, navigation=True),
            [[button(tr(locale, 'back'), 'trf_back')]], state, query.message.message_id)
        return
    key, values = FIELDS[field]
    current = policies.get(key)
    choices = tuple(button(value_label(locale, field, value), f'trf_limits:{field}:{value}',
                           style='primary' if key in policies and current == value else None) for value in values)
    rows = ((button(tr(locale, 'tariff.limits.unset'), f'trf_limits:{field}:unset',
                    style='primary' if key not in policies else None),),
            *(choices[i:i + 3] for i in range(0, len(choices), 3)))
    await render(bot, query.message.chat.id, Screen(tr(locale, TITLES[field]), (tr(locale, 'tariff.limits_hint'),),
        sections=(Section('', rows=rows),), embedded_buttons=True, navigation=True),
        [[button(tr(locale, 'back'), 'trf_limits')]], state, query.message.message_id)


@router.callback_query(F.data.in_({'trf_del', 'trf_del_go'}))
async def delete_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    locale, data = await _locale(state), await state.get_data()
    if not data.get('tariff_id'):
        return
    if query.data == 'trf_del':
        await render(bot, query.message.chat.id, Screen(tr(locale, 'tariff.delete'), (tr(locale, 'tariff.delete_warning'),),
            embedded_buttons=True, navigation=True),
            [[button(tr(locale, 'back'), 'trf_back'), button(tr(locale, 'tariff.delete'), 'trf_del_go', style='danger')]],
            state, query.message.message_id)
        return
    try:
        await backend.request('DELETE', f"/api/v1/tariffs/{data['tariff_id']}", telegram_user_id=query.from_user.id,
                              revision=data['tariff_revision'])
    except BackendError as exc:
        return await show_tariff(query.message.chat.id, query.from_user.id, query.message.message_id,
                                 data['tariff_id'], bot, backend, state, error_text(locale, exc))
    await state.update_data(tariff_id=None)
    await show_list(query.message.chat.id, query.from_user.id, query.message.message_id, bot, backend, state)


def profile_section(locale, profile_id, summary, owner_admin):
    """The profile editor's tariff section."""
    tariff = (summary or {}).get('tariff')
    line = tr(locale, 'tariff.profile.none') if tariff is None else tariff['title']
    return (Section(tr(locale, 'tariff.profile'), (line,),
                    ((button(tr(locale, 'tariff.profile.change'), f'ptrf:{profile_id}'),),)),)


@router.callback_query(F.data.startswith('ptrf:'))
async def profile_pick_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    locale = await _locale(state)
    profile_id = query.data.split(':', 1)[1]
    listed = await backend.request('GET', '/api/v1/tariffs', telegram_user_id=query.from_user.id)
    items = listed['items'][:40]
    await state.update_data(tariff_profile=profile_id, tariff_choices=[item['id'] for item in items], tariff_draft=None)
    rows = [[button(f"{item['title']} · {term_label(locale, item['duration_days'])}", f'ptrf_pick:{index}')]
            for index, item in enumerate(items)]
    rows.append([button(tr(locale, 'tariff.profile.unlink'), 'ptrf_pick:none')])
    rows.append([button(tr(locale, 'back'), f'admin_profile_edit:{profile_id}')])
    await render(bot, query.message.chat.id, Screen(tr(locale, 'tariff.profile'),
        (tr(locale, 'tariff.profile.pick') if items else tr(locale, 'tariff.empty'),),
        embedded_buttons=True, navigation=True), rows, state, query.message.message_id)


async def _draft_screen(query, bot, backend, state):
    locale, data = await _locale(state), await state.get_data()
    draft, profile_id = data['tariff_draft'], data['tariff_profile']
    lines = [tr(locale, 'tariff.profile.confirm_unlink')] if draft['tariff_id'] is None else [draft['title']]
    rows = []
    if draft['tariff_id'] is not None and draft['term'] is not None and not draft['admin']:
        rows.append(tuple(button(tr(locale, 'tariff.expiry.' + mode), f'ptrf_opt:expiry:{mode}',
                                 style='primary' if draft['expiry'] == mode else None)
                          for mode in ('keep', 'start', 'extend')))
        lines.append(tr(locale, 'tariff.expiry.hint.' + draft['expiry'], days=draft['term']))
    if draft['tariff_id'] is not None and draft['access'] and not draft['admin']:
        rows.append((button(tr(locale, 'tariff.apply_access.' + ('on' if draft['apply_access'] else 'off')),
                            'ptrf_opt:access', style='primary' if draft['apply_access'] else None),))
        if draft['apply_access']:
            lines.append(tr(locale, 'tariff.apply_access.warning'))
    lines.append(tr(locale, 'tariff.limits_linked'))
    await render(bot, query.message.chat.id, Screen(tr(locale, 'tariff.profile'), tuple(lines),
        sections=(Section('', rows=tuple(rows)),) if rows else (), embedded_buttons=True, navigation=True),
        [[button(tr(locale, 'back'), f'ptrf:{profile_id}'), button(tr(locale, 'tariff.profile.apply'), 'ptrf_go',
                                                                  style='primary')]],
        state, query.message.message_id)


@router.callback_query(F.data.startswith('ptrf_pick:'))
async def profile_choice_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    data = await state.get_data()
    choice, choices = query.data.split(':', 1)[1], data.get('tariff_choices') or []
    if not data.get('tariff_profile') or (choice != 'none' and not (choice.isdigit() and int(choice) < len(choices))):
        return
    profile = await backend.request('GET', f"/api/v1/profiles/{data['tariff_profile']}",
                                    telegram_user_id=query.from_user.id)
    # The revision seen here makes a repeated tap replay the same command.
    draft = {'tariff_id': None, 'title': '', 'term': None, 'access': False, 'expiry': 'keep',
             'apply_access': False, 'admin': False, 'key': str(uuid4()), 'revision': profile['desired_revision']}
    if choice != 'none':
        tariff = await backend.request('GET', f'/api/v1/tariffs/{choices[int(choice)]}', telegram_user_id=query.from_user.id)
        from .admin_profiles import _owner_is_admin
        draft.update(tariff_id=tariff['id'], title=tariff['title'], term=tariff['duration_days'],
                     access=tariff['access_policy'] is not None,
                     admin=await _owner_is_admin(backend, query.from_user.id, profile))
        if draft['term'] is not None and not draft['admin']:
            draft['expiry'] = 'extend' if profile.get('expires_at') else 'start'
    await state.update_data(tariff_draft=draft)
    await _draft_screen(query, bot, backend, state)


@router.callback_query(F.data.startswith('ptrf_opt:'))
async def profile_option_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    draft = (await state.get_data()).get('tariff_draft')
    if not draft or draft['tariff_id'] is None:
        return
    parts = query.data.split(':')
    if parts[1:2] == ['expiry'] and len(parts) == 3 and parts[2] in ('keep', 'start', 'extend'):
        draft['expiry'] = parts[2]
    elif parts[1:] == ['access']:
        draft['apply_access'] = not draft['apply_access']
    # A changed choice is a different command.
    draft['key'] = str(uuid4())
    await state.update_data(tariff_draft=draft)
    await _draft_screen(query, bot, backend, state)


@router.callback_query(F.data == 'ptrf_go')
async def profile_apply_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    data = await state.get_data()
    draft, profile_id = data.get('tariff_draft'), data.get('tariff_profile')
    if not draft or not profile_id:
        return
    locale = await _locale(state)
    body = {'tariff_id': draft['tariff_id'], 'expiry': draft['expiry'] if not draft['admin'] else 'keep',
            'apply_access': draft['apply_access'] and not draft['admin']}
    try:
        await backend.request('PUT', f'/api/v1/profiles/{profile_id}/tariff', telegram_user_id=query.from_user.id,
                              body=body, command=True, command_key=draft['key'], revision=draft['revision'])
    except BackendError as exc:
        await render(bot, query.message.chat.id, Screen(tr(locale, 'tariff.profile'), (error_text(locale, exc),),
            embedded_buttons=True, navigation=True), [[button(tr(locale, 'back'), f'ptrf:{profile_id}')]],
            state, query.message.message_id)
        return
    await state.update_data(tariff_draft=None)
    from .admin_profiles import show_profile_edit_menu
    await show_profile_edit_menu(query.message.chat.id, query.from_user.id, query.message.message_id,
                                 profile_id, bot, backend, state)
