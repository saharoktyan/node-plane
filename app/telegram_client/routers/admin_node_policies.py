"""VPN policies of one node: BitTorrent over VLESS (VPN plan, section 7.2)."""
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section
from .callbacks import NodeSettingsCallback
from .common import render

router = Router()

KEY = 'xray.bittorrent'
INSTALLATION = '/api/v1/vpn-policies/installation'


def bittorrent(policy, node_key):
    """The node's own value or None, and the effective value with its source."""
    own = next((item for item in policy['values'] if item['key'] == KEY and item['node_key'] == node_key
                and item['protocol'] is None), None)
    installation = next(item for item in policy['keys'] if item['key'] == KEY)['effective']
    return own, (own['value'] if own else installation), installation


def summary(locale, policy, node_key):
    own, effective, _ = bittorrent(policy, node_key)
    return tr(locale, 'nodebt.summary', value=tr(locale, 'bittorrent.' + effective),
              source=tr(locale, 'nodebt.source.node' if own else 'nodebt.source.installation'))


def settings_section(locale, node, policy):
    """The node settings entry; only nodes with VLESS have the setting."""
    if (policy is None or 'xray' not in node['protocols']
            or KEY not in {item.get('key') for item in policy.get('keys', ())}):
        return ()
    return (Section(tr(locale, 'nodebt.section'), (summary(locale, policy, node['key']),),
        ((InlineKeyboardButton(text=tr(locale, 'bittorrent.title'), callback_data=f"nodebt:{node['key']}"),),)),)


@router.callback_query(F.data.startswith('nodebt:'))
async def node_bittorrent_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext) -> None:
    from .admin_settings import _friendly_error
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    node_key, _, choice = query.data.removeprefix('nodebt:').partition(':')
    rows = [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data=NodeSettingsCallback(node_key=node_key).pack())]]
    lines, sections = (), ()
    try:
        policy = await backend.request('GET', INSTALLATION, telegram_user_id=query.from_user.id)
        own, _, _ = bittorrent(policy, node_key)
        value = None if choice == 'inherit' else choice if choice in {'allow', 'block'} else ...
        if choice and value is not ... and value != (own['value'] if own else None):
            policy = await backend.request('PATCH', INSTALLATION, telegram_user_id=query.from_user.id, command=True,
                body={'changes': [{'key': KEY, 'node_key': node_key, 'value': value,
                                   'revision': own['revision'] if own else 0}]})
        own, _, installation = bittorrent(policy, node_key)
        selected = own['value'] if own else 'inherit'
        choices = ((InlineKeyboardButton(text=tr(locale, 'nodebt.inherit', value=tr(locale, 'bittorrent.' + installation)),
                        callback_data=f'nodebt:{node_key}:inherit', style='primary' if selected == 'inherit' else None),),
                   tuple(InlineKeyboardButton(text=tr(locale, 'bittorrent.' + option), callback_data=f'nodebt:{node_key}:{option}',
                        style='primary' if selected == option else None) for option in ('allow', 'block')))
        sections = (Section(tr(locale, 'bittorrent.policy'), (summary(locale, policy, node_key),), rows=choices),)
        lines = (tr(locale, 'nodebt.hint'),)
    except BackendError as exc:
        lines = (_friendly_error(locale, exc),)
    await render(bot, query.message.chat.id, Screen(tr(locale, 'bittorrent.title'), lines, sections=sections,
        embedded_buttons=True, navigation=True), rows, state, query.message.message_id)
