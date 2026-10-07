import os
import logging
from dataclasses import replace

from aiogram import F, Router, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section
from .common import render, send_notice
from .callbacks import (RequestsCallback, ReviewCallback, DecideCallback,
                        NotificationReviewCallback, NotificationDecisionCallback, HomeCallback)

router = Router()


class RequestSearchState(StatesGroup):
    waiting_for_query = State()


def _request_name(item, locale):
    name = ' '.join(filter(None, (item.get('first_name'), item.get('last_name'))))
    return name or (tr(locale, 'requests.username', username=item['username'])
        if item.get('username') else
        tr(locale, 'requests.account', id=item['account_id'][:8]))


def _request_date(item):
    return (item.get('created_at') or '').replace('T', ' ').split('.')[0] or '—'


def _request_details(item, locale):
    return (tr(locale, 'requests.detail_id', id=item.get('telegram_user_id') or '—'),
            tr(locale, 'requests.detail_request_id', id=item['id']),
            tr(locale, 'requests.detail_account_id', id=item['account_id']))


@router.callback_query(RequestsCallback.filter())
async def requests_cb(query: CallbackQuery, bot: Bot, backend: BackendClient,
                      state: FSMContext):
    await query.answer()
    await show_requests(query, bot, backend, state)


async def show_requests(query: CallbackQuery, bot: Bot, backend: BackendClient,
                        state: FSMContext):
    await state.set_state(None)
    await state.update_data(request_cursors=[None], request_page_index=0,
                            request_search=None)
    await render_request_page(query.message.chat.id, query.from_user.id,
        query.message.message_id, bot, backend, state, 0)


async def render_request_page(chat_id: int, user_id: int, message_id: int,
                              bot: Bot, backend: BackendClient, state: FSMContext,
                              page_index: int, *, return_home_when_empty: bool = False):
    locale = normalize_locale((await state.get_data()).get('locale'))
    data = await state.get_data()
    cursors = list(data.get('request_cursors') or [None])
    if page_index < 0 or page_index >= len(cursors):
        page_index = 0
    search = data.get('request_search')
    try:
        page = await backend.pending_access_requests(user_id,
            cursor=cursors[page_index], search=search, limit=10)
    except BackendError:
        rows = [[InlineKeyboardButton(text=tr(locale, 'requests.retry'),
            callback_data='request_retry')],
            [InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data='admin_menu')]]
        await render(bot, chat_id, Screen(tr(locale, 'requests.title'),
            (tr(locale, 'requests.unavailable'),), embedded_buttons=True,
            navigation=True), rows, state, message_id)
        return
    if not page['items'] and page_index > 0:
        await render_request_page(chat_id, user_id, message_id, bot, backend,
                                  state, page_index - 1, return_home_when_empty=return_home_when_empty)
        return
    if page.get('next_cursor'):
        if len(cursors) == page_index + 1:
            cursors.append(page['next_cursor'])
        else:
            cursors[page_index + 1] = page['next_cursor']
    else:
        cursors = cursors[:page_index + 1]
    await state.update_data(request_cursors=cursors,
                            request_page_index=page_index)
    if not page['items'] and not search and return_home_when_empty:
        from .user import show_admin_menu
        await show_admin_menu(chat_id, user_id, message_id, bot, backend, state)
        return

    controls = []
    pending_total = page.get('pending_total')
    show_search = (pending_total > 5 if pending_total is not None else
                   len(page['items']) > 5 or bool(page.get('next_cursor')) or page_index > 0)
    if show_search:
        controls.append(InlineKeyboardButton(text=tr(locale, 'requests.search'),
            callback_data='request_search'))
    if search:
        controls.append(InlineKeyboardButton(text=tr(locale, 'requests.clear_search'),
            callback_data='request_search_clear'))
    sections = [Section(tr(locale, 'requests.filters'),
        (tr(locale, 'requests.search_active', query=search),) if search else (),
        (tuple(controls),))] if controls else []
    for index, item in enumerate(page['items']):
        sections.append(Section(_request_name(item, locale),
            (tr(locale, 'requests.detail_date', date=_request_date(item)),),
            ((InlineKeyboardButton(text=tr(locale, 'requests.review_button'),
                callback_data=ReviewCallback(request_id=item['id']).pack()),),),
            sections=(Section(tr(locale, 'requests.details'), _request_details(item, locale),
                              collapsed=True),),
            divider_after=index < len(page['items']) - 1))
    if not page['items']:
        lines = [tr(locale, 'requests.empty')]
    else:
        lines = [tr(locale, 'requests.page_count', count=len(page['items']))]
    if page_index or page.get('next_cursor'):
        lines.append(tr(locale, 'requests.page', number=page_index + 1))
    navigation = []
    if page_index > 0:
        navigation.append(InlineKeyboardButton(text='←',
            callback_data=f'request_page:{page_index - 1}'))
    if page.get('next_cursor'):
        navigation.append(InlineKeyboardButton(text='→',
            callback_data=f'request_page:{page_index + 1}'))
    rows = []
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data='admin_menu')])
    await render(bot, chat_id, Screen(tr(locale, 'requests.title'), tuple(lines),
        sections=tuple(sections), embedded_buttons=True, navigation=True),
                 rows, state, message_id)


@router.callback_query(F.data.startswith('request_page:'))
async def request_page_cb(query: CallbackQuery, bot: Bot,
                          backend: BackendClient, state: FSMContext):
    await query.answer()
    await state.set_state(None)
    try:
        page_index = int(query.data.split(':', 1)[1])
    except ValueError:
        page_index = 0
    await render_request_page(query.message.chat.id, query.from_user.id,
        query.message.message_id, bot, backend, state, page_index)


@router.callback_query(F.data == 'request_retry')
async def request_retry_cb(query: CallbackQuery, bot: Bot,
                           backend: BackendClient, state: FSMContext):
    await query.answer()
    index = (await state.get_data()).get('request_page_index', 0)
    await render_request_page(query.message.chat.id, query.from_user.id,
        query.message.message_id, bot, backend, state, index)


@router.callback_query(F.data == 'request_search')
async def request_search_cb(query: CallbackQuery, bot: Bot, state: FSMContext):
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale'))
    await state.set_state(RequestSearchState.waiting_for_query)
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'requests.search_title'),
               (tr(locale, 'requests.search_prompt'),), embedded_buttons=True, navigation=True),
        [[InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=f"request_page:{(await state.get_data()).get('request_page_index', 0)}")]],
        state, query.message.message_id)


@router.callback_query(F.data == 'request_search_clear')
async def request_search_clear_cb(query: CallbackQuery, bot: Bot,
                                  backend: BackendClient, state: FSMContext):
    await query.answer()
    await state.set_state(None)
    await state.update_data(request_cursors=[None], request_page_index=0,
                            request_search=None)
    await render_request_page(query.message.chat.id, query.from_user.id,
        query.message.message_id, bot, backend, state, 0)


@router.message(RequestSearchState.waiting_for_query, F.text)
async def request_search_text(message: Message, bot: Bot, backend: BackendClient,
                              state: FSMContext):
    if message.from_user is None or message.chat.type != 'private':
        return
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    locale = normalize_locale((await state.get_data()).get('locale'))
    search = (message.text or '').strip()
    if not search or len(search) > 128:
        await render(bot, message.chat.id,
            Screen(tr(locale, 'requests.search_title'),
                (tr(locale, 'requests.search_invalid'),), embedded_buttons=True, navigation=True),
            [[InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data=f"request_page:{(await state.get_data()).get('request_page_index', 0)}")]],
            state, (await state.get_data()).get('control_message_id'))
        return
    await state.set_state(None)
    await state.update_data(request_cursors=[None], request_page_index=0,
                            request_search=search)
    await render_request_page(message.chat.id, message.from_user.id,
        (await state.get_data()).get('control_message_id'), bot, backend, state, 0)


@router.callback_query(ReviewCallback.filter())
async def review_cb(query: CallbackQuery, callback_data: ReviewCallback, bot: Bot,
                    backend: BackendClient, state: FSMContext, *, notification: bool = False):
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale') or
                              query.from_user.language_code)
    try:
        item = await backend.pending_access_request(query.from_user.id,
                                                    callback_data.request_id)
    except BackendError as exc:
        if notification:
            if exc.code == 'resource_not_found':
                await close_request_notification(query, bot, state)
                return
            raise
        await render_request_page(query.message.chat.id, query.from_user.id,
            query.message.message_id, bot, backend, state,
            (await state.get_data()).get('request_page_index', 0))
        return
    decision_type = NotificationDecisionCallback if notification else DecideCallback
    name = _request_name(item, locale)
    rows = [
        [InlineKeyboardButton(text=tr(locale, 'requests.approve'),
            callback_data=decision_type(request_id=item['id'], decision='approve').pack(), style='primary'),
         InlineKeyboardButton(text=tr(locale, 'requests.reject'),
            callback_data=decision_type(request_id=item['id'], decision='reject').pack(), style='danger')],
        [InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data='notification_close' if notification else
                f"request_page:{(await state.get_data()).get('request_page_index', 0)}")]
    ]
    lines = [tr(locale, 'requests.detail_name', name=name),
        tr(locale, 'requests.detail_username',
           username='@' + item['username'] if item.get('username') else '—'),
        tr(locale, 'requests.detail_date', date=_request_date(item)),
        tr(locale, 'requests.pending_state')]
    control_id = (await state.get_data()).get('control_message_id')
    try:
        await render(bot, query.message.chat.id,
            Screen(tr(locale, 'requests.title'), tuple(lines),
                sections=(Section(tr(locale, 'requests.details'), _request_details(item, locale),
                                  collapsed=True),), embedded_buttons=True, navigation=True), rows, state,
            query.message.message_id)
    finally:
        if notification:
            await state.update_data(control_message_id=control_id)


@router.callback_query(DecideCallback.filter())
async def decide_cb(query: CallbackQuery, callback_data: DecideCallback, bot: Bot,
                    backend: BackendClient, state: FSMContext):
    await query.answer()
    await apply_decision(query, callback_data.request_id, callback_data.decision,
                         bot, backend, state)


@router.callback_query(NotificationDecisionCallback.filter())
async def notification_decision_cb(query: CallbackQuery,
        callback_data: NotificationDecisionCallback, bot: Bot,
        backend: BackendClient, state: FSMContext):
    await query.answer()
    await apply_decision(query, callback_data.request_id, callback_data.decision,
                         bot, backend, state, notification=True)


async def apply_decision(query: CallbackQuery, request_id: str, decision: str,
                         bot: Bot, backend: BackendClient, state: FSMContext, *,
                         notification: bool = False):
    user_id = query.from_user.id
    if decision not in {'approve', 'reject'}:
        return
    locale = normalize_locale((await state.get_data()).get('locale') or
                              query.from_user.language_code)
    try:
        request_info = await backend.pending_access_request(user_id, request_id)
    except BackendError as exc:
        if notification:
            if exc.code == 'resource_not_found':
                await close_request_notification(query, bot, state)
                return
            raise
        await render_request_page(query.message.chat.id, user_id,
            query.message.message_id, bot, backend, state,
            (await state.get_data()).get('request_page_index', 0))
        return
    requester_locale = normalize_locale(request_info.get('locale') or
                                        request_info.get('language_code'))
    try:
        result = await backend.request('POST',
            f'/api/v1/access-requests/{request_id}/decision',
            telegram_user_id=user_id, command=True,
            body={'decision': decision})
    except BackendError as exc:
        if exc.code not in {'request_already_decided', 'resource_not_found'}:
            raise
        if notification:
            await close_request_notification(query, bot, state)
            return
        await render_request_page(query.message.chat.id, user_id,
            query.message.message_id, bot, backend, state,
            (await state.get_data()).get('request_page_index', 0))
        return

    if notification:
        await state.update_data(notification_result={'request_id': request_id, 'account_id': result['account_id'], 'decision': decision})
        await show_decision_result(query, bot, state)
    else:
        await render_request_page(query.message.chat.id, user_id,
            query.message.message_id, bot, backend, state,
            (await state.get_data()).get('request_page_index', 0), return_home_when_empty=True)
    try:
        account = await backend.request('GET',
            f"/api/v1/accounts/{result['account_id']}", telegram_user_id=user_id)
        recipient = account.get('telegram_user_id')
        if recipient:
            requester_state = FSMContext(storage=state.storage,
                key=replace(state.key, chat_id=recipient, user_id=recipient,
                            thread_id=None, business_connection_id=None, destiny='default'))
            await requester_state.set_state(None)
            await requester_state.update_data(locale=requester_locale, issuance_poll_token=None,
                                              home_presentation=None)
            await render(bot, recipient, Screen(tr(requester_locale, 'requests.title'),
                (tr(requester_locale, 'requests.approved' if decision == 'approve' else 'requests.rejected'),),
                embedded_buttons=True, navigation=True),
                [[InlineKeyboardButton(text=tr(requester_locale, 'requests.to_menu'),
                                       callback_data=HomeCallback().pack())]], requester_state)
    except (BackendError, TelegramAPIError):
        pass


@router.callback_query(NotificationReviewCallback.filter())
async def notification_review_cb(query: CallbackQuery,
        callback_data: NotificationReviewCallback, bot: Bot,
        backend: BackendClient, state: FSMContext):
    await review_cb(query, ReviewCallback(request_id=callback_data.request_id),
                    bot, backend, state, notification=True)


async def close_request_notification(query, bot, state):
    try:
        await bot.delete_message(query.message.chat.id, query.message.message_id)
    except TelegramAPIError:
        logging.getLogger(__name__).warning('Could not delete decided access notification')
        try:
            await bot.edit_message_reply_markup(chat_id=query.message.chat.id,
                message_id=query.message.message_id, reply_markup=None)
        except TelegramAPIError:
            pass
    if (await state.get_data()).get('notification_session'):
        await state.set_state(None)
        await state.update_data(notification_closed=True)
    if (await state.get_data()).get('control_message_id') == query.message.message_id:
        await state.update_data(control_message_id=None)


@router.callback_query(F.data == 'notification_close')
async def notification_close_cb(query: CallbackQuery, bot: Bot, state: FSMContext):
    await query.answer()
    await close_request_notification(query, bot, state)


async def notify_admins(bot: Bot, backend: BackendClient, request_id: str):
    for raw_id in os.environ.get('ADMIN_IDS', '').split(','):
        try:
            admin_id = int(raw_id.strip())
            admin = await backend.me(admin_id)
            if admin['role'] != 'admin' or admin['status'] != 'approved':
                continue
            policy = await backend.access_request_policy(admin_id)
            if not policy.get('notify_requests', True):
                continue
            locale = normalize_locale(admin.get('locale') or admin.get('language_code'))
            notice = Screen(tr(locale, 'requests.title'),
                (tr(locale, 'requests.notification'),), embedded_buttons=True)
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=tr(locale, 'requests.approve'),
                    callback_data=NotificationDecisionCallback(request_id=request_id,
                        decision='approve').pack(), style='primary'),
                InlineKeyboardButton(text=tr(locale, 'requests.reject'),
                    callback_data=NotificationDecisionCallback(request_id=request_id,
                        decision='reject').pack(), style='danger')], [
                InlineKeyboardButton(text=tr(locale, 'requests.review_button'),
                    callback_data=NotificationReviewCallback(
                        request_id=request_id).pack())]])
            await send_notice(bot, admin_id, notice, markup)
        except (ValueError, BackendError, TelegramAPIError):
            continue


async def show_decision_result(query, bot, state):
    data = await state.get_data()
    result = data['notification_result']
    locale = normalize_locale(data.get('locale'))
    rows = []
    if result['decision'] == 'approve':
        rows.append([InlineKeyboardButton(text=tr(locale, 'requests.edit_profile'),
            callback_data=f"request_profile:{result['request_id']}", style='primary')])
    rows.append([InlineKeyboardButton(text=tr(locale, 'setup.close'), callback_data='notification_close')])
    await render(bot, query.message.chat.id, Screen(tr(locale, 'requests.title'),
        (tr(locale, 'requests.approved' if result['decision'] == 'approve' else 'requests.rejected'),),
        embedded_buttons=True, navigation=True), rows, state, query.message.message_id)


@router.callback_query(F.data.startswith('request_profile:'))
async def request_profile_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    result = (await state.get_data()).get('notification_result')
    if not result or result['decision'] != 'approve' or result['request_id'] != query.data.split(':', 1)[1]:
        return
    account = await backend.request('GET', f"/api/v1/accounts/{result['account_id']}", telegram_user_id=query.from_user.id)
    if account['status'] != 'approved' or not account.get('telegram_user_id'):
        return
    profiles = await backend.profiles(account['telegram_user_id'])
    profile = next((p for p in profiles['items'] if p.get('owner_account_id') == account['id'] and not p.get('deleting')), None)
    if profile:
        from .admin_profiles import start_profile_setup
        await start_profile_setup(query.message.chat.id, query.from_user.id, query.message.message_id,
            profile['id'], bot, backend, state)
