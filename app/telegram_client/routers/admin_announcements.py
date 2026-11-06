"""Compose/preview/result screens; backend owns immutable delivery work.

The text is written in the languages the administrator wants
(`localized_editor`); each recipient gets their language's text, or the one
chosen for it. The preview shows how many read each language and what.
"""

from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section, Table
from . import localized_editor
from .common import render, schedule_refresh

router = Router()


def button(locale, key, data, *, style=None):
    return InlineKeyboardButton(text=tr(locale, key), callback_data=data, style=style)


async def preview(chat_id, message_id, user_id, bot, backend, state):
    data = await state.get_data()
    locale = normalize_locale(data.get("locale"))
    draft = data["announcement_draft"]
    value = await backend.announcement_preview(user_id, draft["texts"])
    texts = value["texts"]
    sections = [Section(tr(locale, 'announce.rich.audience'),
                        (tr(locale, 'announce.recipients', count=value['recipients']),))]
    for language, count in value["languages"].items():
        name = localized_editor.language_name(locale, language)
        source = texts["shown"][language]
        head = tr(locale, 'announce.language', name=name, count=count)
        lines = (texts["texts"][source],) if source == language else (
            tr(locale, 'ltext.shows', name=localized_editor.language_name(locale, source)),)
        sections.append(Section(head, lines, collapsed=source != language))
    await render(
        bot,
        chat_id,
        Screen(
            tr(locale, "announce.preview"),
            sections=tuple(sections),
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
    if value["status"] == "running":
        schedule_refresh(state, lambda: result(query, bot, backend, state, job_id))


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
            draft = (await state.get_data()).get("announcement_draft") if query.data == "announce_edit" else None
            if not draft:
                await state.update_data(announcement_draft=None)
            await localized_editor.open_editor(query.message.chat.id, query.from_user.id, query.message.message_id,
                                               bot, backend, state, 'announcement', draft and draft["texts"])
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
                query.from_user.id, draft["texts"], draft["key"]
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


async def _save(chat_id, user_id, message_id, bot, backend, state, value):
    """The written text becomes the draft to preview; a changed text is a new
    announcement, so it gets a new key."""
    draft = (await state.get_data()).get("announcement_draft")
    if not draft or draft["texts"] != value:
        draft = {"texts": value, "key": str(uuid4()), "nonce": uuid4().hex[:8]}
    await state.update_data(announcement_draft=draft)
    await preview(chat_id, message_id, user_id, bot, backend, state)


localized_editor.register('announcement', localized_editor.Context('announce.title', 'announce_menu', 3000, _save))
