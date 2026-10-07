"""Compose/preview/result screens; backend owns immutable delivery work."""

from contextlib import suppress
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section, Table
from .common import render

router = Router()


class Compose(StatesGroup):
    text = State()


def button(locale, key, data, *, style=None):
    return InlineKeyboardButton(text=tr(locale, key), callback_data=data, style=style)


async def preview(chat_id, message_id, user_id, bot, backend, state):
    data = await state.get_data()
    locale = normalize_locale(data.get("locale"))
    draft = data["announcement_draft"]
    value = await backend.announcement_preview(user_id, draft["text"])
    await render(
        bot,
        chat_id,
        Screen(
            tr(locale, "announce.preview"),
            sections=(Section(tr(locale, 'announce.rich.message'), (value['text'],)),
                Section(tr(locale, 'announce.rich.audience'),
                    (tr(locale, 'announce.recipients', count=value['recipients']),))),
            embedded_buttons=True, navigation=True,
        ),
        [
            [
                button(locale, "back", "announce_edit"),
                button(locale, "announce.send", f"announce_send:{draft['nonce']}").model_copy(update={'style': 'primary'}),
            ]
        ],
        state,
        message_id,
    )


async def result(query, bot, backend, state, job_id):
    locale = normalize_locale((await state.get_data()).get("locale"))
    value = await backend.announcement_status(query.from_user.id, job_id)
    counts = value["counts"]
    lines = [tr(locale, 'announce.rich.' + value['status'])]
    if not value["total"]:
        lines.insert(0, tr(locale, "announce.empty"))
    rows = []
    if value["status"] == "running":
        rows.append([button(locale, "updates.refresh", f"announce_job:{job_id}")])
    rows.append([button(locale, "back", "admin_menu")])
    await render(
        bot,
        query.message.chat.id,
        Screen(tr(locale, "announce.result"), tuple(lines),
            sections=(Section(tr(locale, 'announce.rich.delivery'), tables=(Table(
                (tr(locale, 'announce.rich.state'), tr(locale, 'announce.rich.count')),
                tuple((tr(locale, 'announce.rich.' + kind), str(counts.get(kind, 0)))
                    for kind in ('queued', 'claimed', 'sent', 'failed', 'unknown', 'skipped'))),)),
                Section(tr(locale, 'announce.rich.details'),
                    (tr(locale, 'announce.delivery_note'),), collapsed=True)),
            embedded_buttons=True, navigation=True),
        rows,
        state,
        query.message.message_id,
    )


@router.callback_query(F.data == "announce_menu")
@router.callback_query(F.data == "announce_compose")
@router.callback_query(F.data == "announce_edit")
@router.callback_query(F.data == "announce_preview")
@router.callback_query(F.data.startswith("announce_send:"))
@router.callback_query(F.data.startswith("announce_job:"))
async def announcement_cb(
    query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext
):
    await query.answer()
    locale = normalize_locale((await state.get_data()).get("locale"))
    try:
        if query.data == "announce_menu":
            await state.set_state(None)
            overview = await backend.announcement_latest(query.from_user.id)
            compose = button(locale, 'announce.compose', 'announce_compose').model_copy(update={'style': 'primary'})
            sections = [Section(tr(locale, 'announce.rich.message'), rows=((compose,),))]
            rows = []
            if overview["last_job"]:
                last = overview['last_job']
                sections.append(Section(tr(locale, 'announce.last'),
                    (tr(locale, 'announce.rich.' + last['status']),),
                    rows=((button(locale, 'announce.rich.open_result', f"announce_job:{last['id']}"),),),
                    tables=(Table((tr(locale, 'announce.rich.state'), tr(locale, 'announce.rich.count')),
                        tuple((tr(locale, 'announce.rich.' + kind), str(last['counts'].get(kind, 0)))
                              for kind in ('sent', 'failed', 'unknown'))),)))
            rows.append([button(locale, "back", "admin_menu")])
            await render(
                bot,
                query.message.chat.id,
                Screen(tr(locale, "announce.title"), sections=tuple(sections),
                    embedded_buttons=True, navigation=True),
                rows,
                state,
                query.message.message_id,
            )
        elif query.data in {"announce_compose", "announce_edit"}:
            me = await backend.me(query.from_user.id)
            if "settings.manage" not in me["permissions"]:
                raise BackendError("permission_denied", 403)
            data = await state.get_data()
            if query.data == "announce_compose":
                await state.update_data(announcement_draft=None)
            await state.set_state(Compose.text)
            draft = (
                data.get("announcement_draft")
                if query.data == "announce_edit"
                else None
            )
            navigation = [button(locale, "back", "announce_menu")]
            if draft:
                navigation.append(
                    button(locale, "announce.preview", "announce_preview", style='primary')
                )
            rows = [navigation]
            await render(
                bot,
                query.message.chat.id,
                Screen(
                    tr(locale, "announce.title"),
                    (tr(locale, "announce.prompt"),),
                    sections=(Section(tr(locale, 'announce.rich.draft'), (draft['text'],)),) if draft else (),
                    embedded_buttons=True, navigation=True,
                ),
                rows,
                state,
                query.message.message_id,
            )
        elif query.data == "announce_preview":
            await state.set_state(None)
            await preview(
                query.message.chat.id,
                query.message.message_id,
                query.from_user.id,
                bot,
                backend,
                state,
            )
        elif query.data.startswith("announce_send:"):
            await state.set_state(None)
            draft = (await state.get_data()).get("announcement_draft")
            if not draft or draft["nonce"] != query.data.split(":")[1]:
                return
            value = await backend.announcement_create(
                query.from_user.id, draft["text"], draft["key"]
            )
            await result(query, bot, backend, state, value["id"])
        else:
            await result(query, bot, backend, state, query.data.split(":")[1])
    except (BackendError, KeyError):
        await render(
            bot,
            query.message.chat.id,
            Screen(tr(locale, "announce.title"), (tr(locale, "announce.error"),), embedded_buttons=True, navigation=True),
            [[button(locale, "back", "admin_menu")]],
            state,
            query.message.message_id,
        )


@router.message(Compose.text, F.text)
async def announcement_text(
    message: Message, bot: Bot, backend: BackendClient, state: FSMContext
):
    if not message.from_user or message.chat.type != "private":
        return
    data = await state.get_data()
    locale = normalize_locale(data.get("locale"))
    with suppress(TelegramAPIError):
        await message.delete()
    text = (message.text or "").strip()
    if not 1 <= len(text) <= 3000:
        await render(
            bot,
            message.chat.id,
            Screen(tr(locale, "announce.title"), (tr(locale, "announce.invalid"),), embedded_buttons=True, navigation=True),
            [[button(locale, "back", "announce_menu")]],
            state,
            data.get("control_message_id"),
        )
        return
    await state.update_data(
        announcement_draft={"text": text, "key": str(uuid4()), "nonce": uuid4().hex[:8]}
    )
    await state.set_state(None)
    try:
        await preview(
            message.chat.id,
            data.get("control_message_id"),
            message.from_user.id,
            bot,
            backend,
            state,
        )
    except BackendError:
        await render(
            bot,
            message.chat.id,
            Screen(tr(locale, "announce.title"), (tr(locale, "announce.error"),), embedded_buttons=True, navigation=True),
            [[button(locale, "back", "admin_menu")]],
            state,
            data.get("control_message_id"),
        )
