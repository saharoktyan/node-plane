import os

from aiogram import F, Router, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen
from .common import render, send_notice
from .callbacks import (RequestsCallback, ReviewCallback, DecideCallback,
                        NotificationReviewCallback, NotificationDecisionCallback)

router = Router()


class RequestSearchState(StatesGroup):
    waiting_for_query = State()


def _request_name(item, locale):
    name = ' '.join(filter(None, (item.get('first_name'), item.get('last_name'))))
    return name or (tr(locale, 'requests.username', username=item['username'])
        if item.get('username') else
        tr(locale, 'requests.account', id=item['account_id'][:8]))


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
                              page_index: int):
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
            (tr(locale, 'requests.unavailable'),)), rows, state, message_id)
        return
    if not page['items'] and page_index > 0:
        await render_request_page(chat_id, user_id, message_id, bot, backend,
                                  state, page_index - 1)
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
    if not page['items'] and not search:
        from .user import show_admin_menu
        await show_admin_menu(chat_id, user_id, message_id, bot, state)
        return

    rows = [[InlineKeyboardButton(text=tr(locale, 'requests.review',
                name=_request_name(item, locale)),
            callback_data=ReviewCallback(request_id=item['id']).pack())]
            for item in page['items']]
    if not page['items']:
        lines = [tr(locale, 'requests.empty')]
    else:
        lines = [tr(locale, 'requests.count', count=len(page['items']))]
    if page_index or page.get('next_cursor'):
        lines.append(tr(locale, 'requests.page', number=page_index + 1))
    navigation = []
    if page_index > 0:
        navigation.append(InlineKeyboardButton(text='◀️',
            callback_data=f'request_page:{page_index - 1}'))
    if page.get('next_cursor'):
        navigation.append(InlineKeyboardButton(text='▶️',
            callback_data=f'request_page:{page_index + 1}'))
    if navigation:
        rows.append(navigation)
    controls = [InlineKeyboardButton(text=tr(locale, 'requests.search'),
        callback_data='request_search')]
    if search:
        controls.append(InlineKeyboardButton(text=tr(locale, 'requests.clear_search'),
            callback_data='request_search_clear'))
    rows.append(controls)
    rows.append([InlineKeyboardButton(text=tr(locale, 'back'),
        callback_data='admin_menu')])
    await render(bot, chat_id, Screen(tr(locale, 'requests.title'), tuple(lines)),
                 rows, state, message_id)


@router.callback_query(F.data.startswith('request_page:'))
async def request_page_cb(query: CallbackQuery, bot: Bot,
                          backend: BackendClient, state: FSMContext):
    await query.answer()
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
               (tr(locale, 'requests.search_prompt'),)),
        [[InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=RequestsCallback().pack())]],
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
                (tr(locale, 'requests.search_invalid'),)),
            [[InlineKeyboardButton(text=tr(locale, 'back'),
                callback_data=RequestsCallback().pack())]],
            state, (await state.get_data()).get('control_message_id'))
        return
    await state.set_state(None)
    await state.update_data(request_cursors=[None], request_page_index=0,
                            request_search=search)
    await render_request_page(message.chat.id, message.from_user.id,
        (await state.get_data()).get('control_message_id'), bot, backend, state, 0)


@router.callback_query(ReviewCallback.filter())
async def review_cb(query: CallbackQuery, callback_data: ReviewCallback, bot: Bot,
                    backend: BackendClient, state: FSMContext):
    await query.answer()
    locale = normalize_locale((await state.get_data()).get('locale') or
                              query.from_user.language_code)
    try:
        item = await backend.pending_access_request(query.from_user.id,
                                                    callback_data.request_id)
    except BackendError:
        await render_request_page(query.message.chat.id, query.from_user.id,
            query.message.message_id, bot, backend, state,
            (await state.get_data()).get('request_page_index', 0))
        return
    name = _request_name(item, locale)
    requested_at = item.get('created_at', '').replace('T', ' ').split('.')[0] or '—'
    rows = [
        [InlineKeyboardButton(text=tr(locale, 'requests.approve'),
            callback_data=DecideCallback(request_id=item['id'], decision='approve').pack()),
         InlineKeyboardButton(text=tr(locale, 'requests.reject'),
            callback_data=DecideCallback(request_id=item['id'], decision='reject').pack())],
        [InlineKeyboardButton(text=tr(locale, 'back'),
            callback_data=f"request_page:{(await state.get_data()).get('request_page_index', 0)}")]
    ]
    lines = [tr(locale, 'requests.detail_name', name=name),
        tr(locale, 'requests.detail_id', id=item.get('telegram_user_id') or '—'),
        tr(locale, 'requests.detail_username',
           username='@' + item['username'] if item.get('username') else '—'),
        tr(locale, 'requests.detail_date', date=requested_at)]
    await render(bot, query.message.chat.id,
        Screen(tr(locale, 'requests.title'), tuple(lines)), rows, state,
        query.message.message_id)


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
    await state.set_state(None)
    await state.update_data(request_cursors=[None], request_page_index=0,
                            request_search=None)
    await apply_decision(query, callback_data.request_id, callback_data.decision,
                         bot, backend, state)


async def apply_decision(query: CallbackQuery, request_id: str, decision: str,
                         bot: Bot, backend: BackendClient, state: FSMContext):
    user_id = query.from_user.id
    if decision not in {'approve', 'reject'}:
        return
    locale = normalize_locale((await state.get_data()).get('locale') or
                              query.from_user.language_code)
    try:
        request_info = await backend.pending_access_request(user_id, request_id)
    except BackendError:
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
        await render_request_page(query.message.chat.id, user_id,
            query.message.message_id, bot, backend, state,
            (await state.get_data()).get('request_page_index', 0))
        return

    await render_request_page(query.message.chat.id, user_id,
        query.message.message_id, bot, backend, state,
        (await state.get_data()).get('request_page_index', 0))
    try:
        account = await backend.request('GET',
            f"/api/v1/accounts/{result['account_id']}", telegram_user_id=user_id)
        recipient = account.get('telegram_user_id')
        if recipient:
            await send_notice(bot, recipient, Screen(tr(requester_locale,
                'requests.title'), (tr(requester_locale,
                'requests.approved' if decision == 'approve' else
                'requests.rejected'),)))
    except (BackendError, TelegramAPIError):
        pass


@router.callback_query(NotificationReviewCallback.filter())
async def notification_review_cb(query: CallbackQuery,
        callback_data: NotificationReviewCallback, bot: Bot,
        backend: BackendClient, state: FSMContext):
    await review_cb(query, ReviewCallback(request_id=callback_data.request_id),
                    bot, backend, state)


async def notify_admins(bot: Bot, backend: BackendClient, request_id: str):
    for raw_id in os.environ.get('ADMIN_IDS', '').split(','):
        try:
            admin_id = int(raw_id.strip())
            admin = await backend.me(admin_id)
            if admin['role'] != 'admin' or admin['status'] != 'approved':
                continue
            locale = normalize_locale(admin.get('locale') or admin.get('language_code'))
            notice = Screen(tr(locale, 'requests.title'),
                (tr(locale, 'requests.notification'),))
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=tr(locale, 'requests.approve'),
                    callback_data=NotificationDecisionCallback(request_id=request_id,
                        decision='approve').pack()),
                InlineKeyboardButton(text=tr(locale, 'requests.reject'),
                    callback_data=NotificationDecisionCallback(request_id=request_id,
                        decision='reject').pack())], [
                InlineKeyboardButton(text=tr(locale, 'requests.review_button'),
                    callback_data=NotificationReviewCallback(
                        request_id=request_id).pack())]])
            await send_notice(bot, admin_id, notice, markup)
        except (ValueError, BackendError, TelegramAPIError):
            continue
