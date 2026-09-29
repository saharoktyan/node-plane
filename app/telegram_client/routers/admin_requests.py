import os
from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramAPIError

from ..backend import BackendClient, BackendError
from ..screens import Screen
from .common import render, send_notice
from .callbacks import (
    RequestsCallback, ReviewCallback, DecideCallback,
    NotificationReviewCallback, HomeCallback
)

router = Router()

@router.callback_query(RequestsCallback.filter())
async def requests_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    page = await backend.request('GET', '/api/v1/access-requests?limit=25', telegram_user_id=user_id)
    rows = []
    for item in page['items']:
        rows.append([InlineKeyboardButton(text=f"Review {item['account_id'][:8]}", callback_data=ReviewCallback(request_id=item['id']).pack())])
    if not page['items']:
        # if there are no requests, render home (you could just fallback or import show_home if you wanted to avoid duplication)
        from .user import show_home
        await show_home(query.message.chat.id, user_id, bot, backend, state, query.message.message_id)
        return
    rows.append([InlineKeyboardButton(text='Back', callback_data=HomeCallback().pack())])
    await render(bot, query.message.chat.id, Screen('Access requests', (f"Pending: {len(page['items'])}",)), rows, state, query.message.message_id)

@router.callback_query(ReviewCallback.filter())
async def review_cb(query: CallbackQuery, callback_data: ReviewCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    request_id = callback_data.request_id
    rows = [
        [InlineKeyboardButton(text='Approve', callback_data=DecideCallback(request_id=request_id, decision='approve').pack()),
         InlineKeyboardButton(text='Reject', callback_data=DecideCallback(request_id=request_id, decision='reject').pack())],
        [InlineKeyboardButton(text='Back', callback_data=RequestsCallback().pack())]
    ]
    await render(bot, query.message.chat.id, Screen('Access request', ('Approve or reject this account.',)), rows, state, query.message.message_id)

@router.callback_query(DecideCallback.filter())
async def decide_cb(query: CallbackQuery, callback_data: DecideCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    request_id = callback_data.request_id
    decision = callback_data.decision
    
    result = await backend.request('POST', f'/api/v1/access-requests/{request_id}/decision',
                                   telegram_user_id=user_id, command=True, body={'decision': decision})
    
    # call requests
    page = await backend.request('GET', '/api/v1/access-requests?limit=25', telegram_user_id=user_id)
    rows = []
    for item in page['items']:
        rows.append([InlineKeyboardButton(text=f"Review {item['account_id'][:8]}", callback_data=ReviewCallback(request_id=item['id']).pack())])
    if not page['items']:
        from .user import show_home
        await show_home(query.message.chat.id, user_id, bot, backend, state, query.message.message_id)
    else:
        rows.append([InlineKeyboardButton(text='Back', callback_data=HomeCallback().pack())])
        await render(bot, query.message.chat.id, Screen('Access requests', (f"Pending: {len(page['items'])}",)), rows, state, query.message.message_id)
    
    # notify requester
    account_id = result['account_id']
    try:
        account = await backend.request('GET', f'/api/v1/accounts/{account_id}', telegram_user_id=user_id)
        recipient = account.get('telegram_user_id')
        if recipient:
            await send_notice(bot, recipient, Screen('Access request', ('Your access request was approved.' if decision == 'approve' else 'Your access request was rejected.',)))
    except (BackendError, TelegramAPIError):
        pass

@router.callback_query(NotificationReviewCallback.filter())
async def notification_review_cb(query: CallbackQuery, callback_data: NotificationReviewCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    user_id = query.from_user.id
    request_id = callback_data.request_id
    rows = [
        [InlineKeyboardButton(text='Approve', callback_data=DecideCallback(request_id=request_id, decision='approve').pack()),
         InlineKeyboardButton(text='Reject', callback_data=DecideCallback(request_id=request_id, decision='reject').pack())],
        [InlineKeyboardButton(text='Back', callback_data=RequestsCallback().pack())]
    ]
    await render(bot, query.message.chat.id, Screen('Access request', ('Approve or reject this account.',)), rows, state, query.message.message_id)

async def notify_admins(bot: Bot, backend: BackendClient, request_id: str):
    for raw_id in os.environ.get('ADMIN_IDS', '').split(','):
        try:
            admin_id = int(raw_id.strip())
            admin = await backend.me(admin_id)
            if admin['role'] != 'admin' or admin['status'] != 'approved':
                continue
            notice = Screen('Access request', ('A user requested access to Node Plane.',))
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text='Review request', callback_data=NotificationReviewCallback(request_id=request_id).pack())
            ]])
            await send_notice(bot, admin_id, notice, markup)
        except (ValueError, BackendError, TelegramAPIError):
            continue

