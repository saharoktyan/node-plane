"""Expand long breadcrumb paths without replaying the current screen handler."""
from aiogram import F, Router, BaseMiddleware
from aiogram.types import InputRichMessage, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound, TelegramNetworkError
from ..screens import Screen, Section
from ..i18n import normalize_locale, tr
from .common import log_rich_failure

router = Router()


def has_profile_changes(data):
    if not data.get('edit_profile_id'):
        return False
    return any(data.get(draft, []) != data.get(original, []) for draft, original in (
        ('draft_grants', 'original_grants'), ('draft_rules', 'original_rules'),
        ('draft_exclusions', 'original_exclusions')))


def has_node_changes(data):
    draft = data.get('node_settings_draft')
    return bool(draft and draft.get('values') != draft.get('baseline'))


def leaving_node_settings(callback, data):
    draft = data.get('node_settings_draft') or {}
    return callback in {'admin_menu', 'admin_nodes'} or (
        (callback or '').startswith(('admin_node:', 'node_settings:'))
        and callback.split(':', 1)[1] != draft.get('node_key'))


def safe_discard_destination(callback, kind, data):
    if kind == 'node_settings':
        return leaving_node_settings(callback, data)
    if kind == 'access':
        return callback in {'admin_menu', 'admin_profiles'} or (callback or '').startswith(('admin_profile:', 'admin_profile_edit:', 'prof_manage:'))
    return callback in {'admin_menu', 'admin_nodes'}


async def edit_navigation(query, bot, screen, rows):
    try:
        await bot.edit_message_text(chat_id=query.message.chat.id, message_id=query.message.message_id,
            rich_message=screen.rich(rows), reply_markup=InlineKeyboardMarkup(inline_keyboard=[]), request_timeout=10)
    except (TelegramBadRequest, TelegramNotFound, TelegramNetworkError) as exc:
        if 'message is not modified' in str(exc).lower():
            return
        log_rich_failure('navigation', exc)
        await bot.edit_message_text(chat_id=query.message.chat.id, message_id=query.message.message_id,
            text=screen.plain(), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), request_timeout=15)


class NavigationGuardMiddleware(BaseMiddleware):
    """Warn before leaving changed access or server settings drafts."""
    async def __call__(self, handler, event, data):
        query = getattr(event, 'callback_query', None)
        state = data.get('state')
        if query is None or state is None:
            return await handler(event, data)
        values = await state.get_data()
        saved = values.get('navigation_screen')
        if not saved or query.message is None or query.message.message_id != saved['message_id']:
            return await handler(event, data)
        leaving_access = has_profile_changes(values) and (
            safe_discard_destination(query.data, 'access', values))
        leaving_creation = bool(values.get('wizard_data') and not values.get('wizard_saved')) and (
            query.data in {'admin_menu', 'admin_nodes'})
        leaving_settings = has_node_changes(values) and leaving_node_settings(query.data, values)
        if not (leaving_access or leaving_creation or leaving_settings):
            return await handler(event, data)
        await query.answer()
        await state.update_data(navigation_discard={'nonce': saved['nonce'], 'callback': query.data,
                                                   'kind': 'access' if leaving_access else 'node' if leaving_creation else 'node_settings'})
        locale = normalize_locale(values.get('locale'))
        title = tr(locale, 'navigation.unsaved')
        note = tr(locale, 'navigation.discard_note')
        rows = [[InlineKeyboardButton(text=tr(locale, 'back'), callback_data='nav_return:' + saved['nonce']),
                 InlineKeyboardButton(text=tr(locale, 'navigation.leave'),
                    callback_data='nav_discard:' + saved['nonce'], style='danger')]]
        await edit_navigation(query, data['bot'], Screen(title, (note,), embedded_buttons=True, navigation=True), rows)


@router.callback_query(F.data.startswith(('nav_more:', 'nav_return:', 'nav_discard:')))
async def navigation_cb(query, bot, state, dispatcher=None, **handler_data):
    data = await state.get_data()
    saved = data.get('navigation_screen')
    action, nonce = query.data.split(':', 1)
    if not saved or nonce != saved['nonce'] or query.message.message_id != saved['message_id']:
        await query.answer()
        return
    if action == 'nav_return' and saved.get('return_callback'):
        if dispatcher is None:
            await query.answer()
            return
        handler_data.update(bot=bot, state=state, dispatcher=dispatcher,
                            raw_state=await state.get_state())
        # Re-read the existing issuance and recheck access; never create/replay it.
        await dispatcher.propagate_event(update_type='callback_query',
            event=query.model_copy(update={'data': saved['return_callback']}), **handler_data)
        return
    if action == 'nav_discard':
        discard = data.get('navigation_discard')
        if (not discard or discard['nonce'] != nonce or dispatcher is None
                or (discard['callback'] not in {p['callback'] for p in saved['parents']}
                    and not safe_discard_destination(discard['callback'], discard['kind'], data))):
            await query.answer()
            return
        if discard['kind'] == 'access':
            await state.update_data(edit_profile_id=None, draft_grants=None, original_grants=None,
                draft_rules=None, original_rules=None, draft_exclusions=None, original_exclusions=None)
        elif discard['kind'] == 'node_settings':
            await state.update_data(node_settings_draft=None, node_region_confirmation=None,
                edit_node_key=None, edit_field=None)
        else:
            await state.update_data(wizard_data=None, wizard_saved=False)
        await state.update_data(navigation_discard=None)
        # Keep the already selected notification FSM and middleware context.
        # Do not reacquire the same FSM event lock through a nested feed_update.
        # The normal destination handler still performs backend authorization.
        handler_data.update(bot=bot, state=state, dispatcher=dispatcher,
                            raw_state=await state.get_state())
        await dispatcher.propagate_event(update_type='callback_query',
            event=query.model_copy(update={'data': discard['callback']}), **handler_data)
        return
    await query.answer()
    await state.update_data(navigation_discard=None)
    locale = normalize_locale(data.get('locale'))
    if action == 'nav_more':
        # Every destination comes from the structural, read-only route registry.
        rows = [[InlineKeyboardButton(text=p['label'], callback_data=p['callback'])]
                for p in saved['parents']]
        rows.append([InlineKeyboardButton(text=tr(locale, 'back'), callback_data='nav_return:' + nonce)])
        screen = Screen(tr(locale, 'navigation.title'),
                        sections=(Section(saved['title']),), embedded_buttons=True, navigation=True)
        rich, plain, markup, entities = screen.rich(rows), screen.plain(), None, []
        fallback = InlineKeyboardMarkup(inline_keyboard=rows)
    else:
        rich = InputRichMessage.model_validate(saved['rich']) if saved['rich'] else None
        plain = saved['plain']
        markup = InlineKeyboardMarkup.model_validate(saved['markup']) if saved['markup'] else None
        entities = [MessageEntity.model_validate(e) for e in saved['entities']]
        fallback = InlineKeyboardMarkup.model_validate(saved['fallback_markup']) if saved['fallback_markup'] else None
    if rich:
        try:
            await bot.edit_message_text(chat_id=query.message.chat.id, message_id=query.message.message_id,
                rich_message=rich, reply_markup=markup or InlineKeyboardMarkup(inline_keyboard=[]), request_timeout=10)
            return
        except (TelegramBadRequest, TelegramNotFound, TelegramNetworkError) as exc:
            if 'message is not modified' in str(exc).lower():
                return
            log_rich_failure('navigation', exc)
    await bot.edit_message_text(chat_id=query.message.chat.id, message_id=query.message.message_id,
        text=plain, entities=entities or None, reply_markup=fallback, request_timeout=15)
