# app/utils/tg.py
from __future__ import annotations

import logging
from typing import Optional

from telegram import InlineKeyboardMarkup, Update
from telegram.error import BadRequest, RetryAfter
from telegram.ext import CallbackContext

logger = logging.getLogger(__name__)



def safe_delete_by_id(bot, chat_id: int, message_id: int) -> bool:
    try:
        bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except RetryAfter as e:
        logger.warning("safe_delete_by_id rate limited: retry_after=%s", getattr(e, "retry_after", None))
        return False
    except Exception:
        return False

def safe_delete_update_message(update, context) -> bool:
    try:
        msg = update.effective_message
        if not msg:
            return False
        return safe_delete_by_id(context.bot, msg.chat_id, msg.message_id)
    except Exception:
        return False

def answer_cb(update):
    q = update.callback_query
    if not q:
        return
    try:
        q.answer()
    except Exception:
        pass

from telegram import InlineKeyboardMarkup

def _validate_markup(reply_markup):
    if reply_markup is None:
        return
    if not isinstance(reply_markup, InlineKeyboardMarkup):
        raise TypeError(f"reply_markup is {type(reply_markup)} not InlineKeyboardMarkup")

    for r_i, row in enumerate(reply_markup.inline_keyboard):
        if not isinstance(row, list):
            raise TypeError(f"row {r_i} is {type(row)} not list")
        for b_i, btn in enumerate(row):
            if not hasattr(btn, "to_dict"):
                raise TypeError(f"bad btn at {r_i}:{b_i} -> {type(btn)} {btn!r}")



def safe_edit_message(
    update: Update,
    context: CallbackContext,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = None,
    disable_preview: bool = True,
) -> None:
    q = update.callback_query
    _validate_markup(reply_markup)

    if not q or not q.message:
        return
    try:
        q.message.edit_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_preview,
        )
    except RetryAfter as e:
        logger.warning("safe_edit_message rate limited: retry_after=%s", getattr(e, "retry_after", None))
    except BadRequest as e:
        # "Message is not modified" etc.
        logger.warning("safe_edit_message: %s", e)
    except Exception:
        logger.exception("safe_edit_message failed")


def safe_edit_by_ids(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = None,
    disable_web_page_preview: bool = True,
) -> None:
    _validate_markup(reply_markup)
    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
    except RetryAfter as e:
        logger.warning("safe_edit_by_ids rate limited: retry_after=%s", getattr(e, "retry_after", None))
    except BadRequest as e:
        logger.warning("safe_edit_by_ids: %s", e)
    except Exception as e:
        logger.exception("safe_edit_by_ids failed: %s", e)

def strip_inline_keyboard(update: Update, context: CallbackContext) -> None:
    try:
        msg = update.effective_message or (update.callback_query.message if update.callback_query else None)
        if not msg:
            return
        # если это не наше сообщение или не редактируемое — просто молча выходим
        context.bot.edit_message_reply_markup(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            reply_markup=None,
        )
    except Exception:
        pass
