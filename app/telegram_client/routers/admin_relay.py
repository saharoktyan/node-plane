"""The web cabinet's relay. One screen with three ways in: system settings,
the node card and the node's settings. Callbacks carry the way in
(`relay:{action}:{where}`), so Back and the breadcrumbs follow it."""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..navigation import remember_node
from ..screens import Screen, Section, server_label
from .common import render

router = Router()

RELAY = '/api/v1/relay'
TLS_MODES = ('acme', 'files', 'proxy')
# Typed fields by their callback letter.
FIELDS = {'d': 'domain', 'o': 'port', 'c': 'acme_contact', 'r': 'certificate_path', 'k': 'key_path'}
ERRORS = {'relay_serves_cabinet', 'relay_node_without_agent', 'relay_port_in_use', 'relay_exists_on_another_node', 'relay_removing',
          'agent_unavailable', 'relay_apply_failed', 'relay_domain_invalid', 'relay_port_invalid',
          'relay_acme_needs_port_443', 'relay_acme_contact_invalid', 'relay_certificate_path_invalid',
          'relay_tls_invalid'}


class RelayState(StatesGroup):
    waiting_for_value = State()


def callback(action, where):
    return f'relay:{action}:{where}'


def parse(data):
    """The action and the way in: 's', 'v' (services), 'c:{node}' (card) or 'n:{node}' (node settings)."""
    _, action, where = (data.split(':', 2) + ['', ''])[:3]
    return action, where


def origin_node(where):
    kind, _, key = where.partition(':')
    return key if kind in {'c', 'n'} and key else None


def back_callback(where):
    """Where the relay screen was opened from."""
    kind, _, key = where.partition(':')
    if kind == 'c' and key:
        return 'admin_node:' + key
    if kind == 'n' and key:
        return 'node_settings:' + key
    return 'services' if kind == 'v' else 'admin_settings'


def has_relay(node):
    return any(item.get('service') == 'relay' for item in node.get('service_instances') or ())


def card_rows(locale, node):
    """The node card button; only a node that runs the relay has it."""
    if not has_relay(node):
        return ()
    return ((InlineKeyboardButton(text=tr(locale, 'relay.settings'), callback_data=callback('v', 'c:' + node['key'])),),)


def settings_section(locale, node, installed):
    """The node settings entry: the relay settings, or setting it up here."""
    if not installed and not has_relay(node):
        return ()
    label = 'relay.settings' if has_relay(node) else 'relay.add'
    return (Section(tr(locale, 'relay.section'), (tr(locale, 'relay.section_hint'),),
        ((InlineKeyboardButton(text=tr(locale, label), callback_data=callback('v', 'n:' + node['key'])),),)),)


def check(draft, field, value):
    """Sets a typed field; the error key when the value is not usable."""
    value = value.strip()
    if field == 'domain':
        domain = value.lower()
        labels = domain.split('.')
        if ('.' not in domain or len(domain) > 253 or any(not label or label[0] == '-' or label[-1] == '-'
                or not all(c.isascii() and (c.isalnum() or c == '-') for c in label) for label in labels)):
            return 'relay.invalid.domain'
        draft['domain'] = domain
    elif field == 'port':
        if not value.isdigit() or not 0 < int(value) < 65536:
            return 'relay.invalid.port'
        if draft['tls'] == 'acme' and int(value) != 443:
            return 'relay.invalid.acme_port'
        draft['port'] = int(value)
    elif field == 'acme_contact':
        if value and '@' not in value:
            return 'relay.invalid.contact'
        draft['acme_contact'] = value
    elif field in {'certificate_path', 'key_path'}:
        if not value.startswith('/'):
            return 'relay.invalid.path'
        draft[field] = value
    return None


def missing(draft):
    if not draft['node_key']:
        return 'relay.missing.node'
    if not draft['domain']:
        return 'relay.missing.domain'
    if draft['tls'] == 'files' and not (draft['certificate_path'] and draft['key_path']):
        return 'relay.missing.paths'
    return None


def body(draft):
    value = {'node_key': draft['node_key'], 'domain': draft['domain'], 'port': draft['port'], 'tls': draft['tls']}
    if draft['tls'] == 'acme' and draft['acme_contact']:
        value['acme_contact'] = draft['acme_contact']
    if draft['tls'] == 'files':
        value.update(certificate_path=draft['certificate_path'], key_path=draft['key_path'])
    return value


def new_draft(relay, where):
    if relay:
        return {'node_key': relay['node_key'], 'domain': relay['domain'], 'port': relay['port'], 'tls': relay['tls'],
                'acme_contact': relay.get('acme_contact') or '', 'certificate_path': relay.get('certificate_path') or '',
                'key_path': relay.get('key_path') or '', 'node_choice': False}
    node = origin_node(where)
    return {'node_key': node or '', 'domain': '', 'port': 443, 'tls': 'acme', 'acme_contact': '',
            'certificate_path': '', 'key_path': '', 'node_choice': node is None}


def link_line(locale, relay):
    link = relay.get('link')
    if not isinstance(link, dict):
        return tr(locale, 'relay.link.unknown')
    if not link.get('agent_reachable'):
        return tr(locale, 'relay.link.agent')
    if not link.get('running'):
        return tr(locale, 'relay.link.stopped')
    if link.get('connected'):
        return tr(locale, 'relay.link.connected')
    if link.get('error'):
        return tr(locale, 'relay.link.error', error=link['error'])
    return tr(locale, 'relay.link.disconnected')


def error_text(locale, exc):
    from .admin_settings import _friendly_error
    return tr(locale, 'relay.error.' + exc.code) if exc.code in ERRORS else _friendly_error(locale, exc)


async def node_title(user_id, key, backend, state):
    titles = (await state.get_data()).get('_navigation_nodes', {})
    if key in titles:
        return titles[key]
    try:
        node = await backend.request('GET', f'/api/v1/nodes/{key}', telegram_user_id=user_id)
    except BackendError:
        return key
    await remember_node(state, node)
    return server_label(node)


async def candidates(user_id, backend):
    """The nodes a relay can run on: those with an installed agent."""
    nodes = await backend.request('GET', '/api/v1/nodes?stage=node&limit=100', telegram_user_id=user_id)
    return [(node['key'], server_label(node)) for node in nodes.get('items', ()) if node.get('stage') == 'node']


async def show_relay(chat_id, user_id, message_id, where, bot, backend, state, notice=None, confirm=False):
    locale = normalize_locale((await state.get_data()).get('locale'))
    await state.set_state(None)
    await state.update_data(relay_draft=None, relay_input=None)
    back = [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=back_callback(where))]
    lines, rows = [notice] if notice else [], []
    try:
        relay = (await backend.request('GET', RELAY, telegram_user_id=user_id)).get('relay')
    except BackendError as exc:
        relay, lines = None, [error_text(locale, exc)]
        rows.append([InlineKeyboardButton(text=tr(locale, 'relay.refresh'), callback_data=callback('v', where))])
    else:
        if relay is None:
            lines.append(tr(locale, 'relay.none'))
            rows.append([InlineKeyboardButton(text=tr(locale, 'relay.setup_here' if origin_node(where) else 'relay.setup'),
                callback_data=callback('e', where), style='primary')])
        else:
            if origin_node(where) and origin_node(where) != relay['node_key']:
                lines.append(tr(locale, 'relay.elsewhere'))
            lines += [tr(locale, 'relay.address', value=relay['url']),
                      tr(locale, 'relay.node', value=await node_title(user_id, relay['node_key'], backend, state)),
                      tr(locale, 'relay.status', value=tr(locale, 'relay.status.' + relay['status'])),
                      tr(locale, 'relay.tls', value=tr(locale, 'relay.tls.' + relay['tls'])),
                      link_line(locale, relay)]
            if relay.get('error_code'):
                lines.append(tr(locale, 'relay.last_error', value=relay['error_code']))
            if confirm:
                lines.append(tr(locale, 'relay.remove_confirm'))
                rows.append([InlineKeyboardButton(text=tr(locale, 'relay.remove'), callback_data=callback('rmy', where),
                                                  style='danger'),
                             InlineKeyboardButton(text=tr(locale, 'cancel'), callback_data=callback('v', where))])
            elif relay['status'] != 'removing':
                rows.append([InlineKeyboardButton(text=tr(locale, 'relay.change'), callback_data=callback('e', where))])
                rows.append([InlineKeyboardButton(text=tr(locale, 'relay.remove'), callback_data=callback('rm', where),
                                                  style='danger')])
            rows.append([InlineKeyboardButton(text=tr(locale, 'relay.refresh'), callback_data=callback('v', where))])
    await render(bot, chat_id, Screen(tr(locale, 'relay.title'), tuple(lines), embedded_buttons=True, navigation=True),
        rows + [back], state, message_id)


async def show_draft(chat_id, user_id, message_id, where, bot, backend, state, notice=None):
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    draft = data['relay_draft']
    lines = [notice] if notice else []
    lines.append(tr(locale, 'relay.edit_hint' if not draft['node_choice'] and data.get('relay_exists') else 'relay.intro'))
    sections = []
    if draft['node_choice']:
        nodes = data.get('relay_nodes') or []
        sections.append(Section(tr(locale, 'relay.node_title'), () if nodes else (tr(locale, 'relay.no_nodes'),),
            tuple((InlineKeyboardButton(text=title, callback_data=callback(f'p{index}', where),
                style='primary' if key == draft['node_key'] else None),) for index, (key, title) in enumerate(nodes))))
    else:
        lines.append(tr(locale, 'relay.node', value=await node_title(user_id, draft['node_key'], backend, state)))
    shown = lambda value: value or '—'
    fields = [('d', tr(locale, 'relay.field.domain', value=shown(draft['domain'])))]
    if draft['tls'] != 'acme':
        fields.append(('o', tr(locale, 'relay.field.port', value=draft['port'])))
    if draft['tls'] == 'acme':
        fields.append(('c', tr(locale, 'relay.field.contact', value=shown(draft['acme_contact']))))
    if draft['tls'] == 'files':
        fields += [('r', tr(locale, 'relay.field.certificate', value=shown(draft['certificate_path']))),
                   ('k', tr(locale, 'relay.field.key', value=shown(draft['key_path'])))]
    sections.append(Section(tr(locale, 'relay.settings_title'), (),
        tuple((InlineKeyboardButton(text=label, callback_data=callback('f' + letter, where)),) for letter, label in fields)))
    sections.append(Section(tr(locale, 'relay.tls_title'), (tr(locale, 'relay.tls_hint.' + draft['tls']),),
        tuple((InlineKeyboardButton(text=tr(locale, 'relay.tls.' + mode), callback_data=callback(f't{index}', where),
            style='primary' if mode == draft['tls'] else None),) for index, mode in enumerate(TLS_MODES))))
    rows = [[InlineKeyboardButton(text=tr(locale, 'relay.save'), callback_data=callback('save', where), style='primary')],
            [InlineKeyboardButton(text=tr(locale, 'back'), callback_data=callback('v', where))]]
    await render(bot, chat_id, Screen(tr(locale, 'relay.setup_title'), tuple(lines), sections=tuple(sections),
        embedded_buttons=True, navigation=True), rows, state, message_id)


@router.callback_query(F.data.startswith('relay:'))
async def relay_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    await query.answer()
    action, where = parse(query.data)
    chat_id, user_id, message_id = query.message.chat.id, query.from_user.id, query.message.message_id
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    draft = data.get('relay_draft')
    if action in {'v', 'rm'}:
        return await show_relay(chat_id, user_id, message_id, where, bot, backend, state, confirm=action == 'rm')
    if action == 'rmy':
        try:
            await backend.request('DELETE', RELAY, telegram_user_id=user_id, command=True)
        except BackendError as exc:
            return await show_relay(chat_id, user_id, message_id, where, bot, backend, state, error_text(locale, exc))
        return await show_relay(chat_id, user_id, message_id, where, bot, backend, state, tr(locale, 'relay.removing'))
    if action == 'e' and draft is None:
        try:
            relay = (await backend.request('GET', RELAY, telegram_user_id=user_id)).get('relay')
            draft = new_draft(relay, where)
            nodes = await candidates(user_id, backend) if draft['node_choice'] else []
        except BackendError as exc:
            return await show_relay(chat_id, user_id, message_id, where, bot, backend, state, error_text(locale, exc))
        await state.update_data(relay_draft=draft, relay_nodes=nodes, relay_exists=relay is not None)
    if draft is None:
        # A stale button after the draft was dropped: start from the relay screen.
        return await show_relay(chat_id, user_id, message_id, where, bot, backend, state)
    await state.set_state(None)
    notice = None
    if action.startswith('p') and action[1:].isdigit() and draft['node_choice']:
        nodes = data.get('relay_nodes') or []
        if int(action[1:]) < len(nodes):
            draft['node_key'] = nodes[int(action[1:])][0]
    elif action.startswith('t') and action[1:].isdigit() and int(action[1:]) < len(TLS_MODES):
        draft['tls'] = TLS_MODES[int(action[1:])]
        if draft['tls'] == 'acme':
            # Let's Encrypt checks the domain on port 443.
            draft['port'] = 443
    elif action.startswith('f') and action[1:] in FIELDS:
        field = FIELDS[action[1:]]
        await state.set_state(RelayState.waiting_for_value)
        await state.update_data(relay_input={'field': field, 'where': where, 'message_id': message_id})
        return await render(bot, chat_id, Screen(tr(locale, 'relay.field_title.' + field),
            (tr(locale, 'relay.prompt.' + field),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=callback('e', where))]], state, message_id)
    elif action == 'save':
        reason = missing(draft)
        if reason:
            notice = tr(locale, reason)
        else:
            try:
                await backend.request('PUT', RELAY, telegram_user_id=user_id, command=True, body=body(draft))
            except BackendError as exc:
                notice = error_text(locale, exc)
            else:
                return await show_relay(chat_id, user_id, message_id, where, bot, backend, state, tr(locale, 'relay.saved'))
    await state.update_data(relay_draft=draft)
    await show_draft(chat_id, user_id, message_id, where, bot, backend, state, notice)


@router.message(RelayState.waiting_for_value, F.text)
async def relay_value(message: Message, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    request, draft = data.get('relay_input') or {}, data.get('relay_draft')
    if draft is None or not request:
        await state.set_state(None)
        return
    where, message_id = request['where'], request['message_id']
    error = check(draft, request['field'], message.text or '')
    if error:
        return await render(bot, message.chat.id, Screen(tr(locale, 'relay.field_title.' + request['field']),
            (tr(locale, error), tr(locale, 'relay.prompt.' + request['field'])), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=callback('e', where))]], state, message_id)
    await state.set_state(None)
    await state.update_data(relay_draft=draft, relay_input=None)
    await show_draft(message.chat.id, message.from_user.id, message_id, where, bot, backend, state)
