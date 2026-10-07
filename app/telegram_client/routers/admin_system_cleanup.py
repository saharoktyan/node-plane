"""Controller reset/removal screens; all execution belongs to the backend."""

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
from .callbacks import AdminSettingsCallback
from .common import render

router = Router()


class CleanupState(StatesGroup):
    phrase = State()


def back(locale, callback="system_cleanup"):
    return InlineKeyboardButton(text=tr(locale, "back"), callback_data=callback)


def friendly(locale, exc):
    known = {
        "maintenance_busy",
        "confirmation_mismatch",
        "cleanup_plan_changed",
        "cleanup_plan_expired",
        "installation_manifest_required",
        "independent_verification_key_required",
        "verification_target_required",
        "node_cleanup_unverified",
        "uninstall_launch_uncertain",
        "system_cleanup_in_progress",
        "cleanup_retry_unsafe",
        "cleanup_abort_unsafe",
    }
    return (
        tr(locale, "system_cleanup.error." + exc.code)
        if exc.code in known
        else tr(locale, "system_cleanup.error.generic")
    )


async def show_root(chat_id, user_id, message_id, bot, backend, state):
    await state.set_state(None)
    locale = normalize_locale((await state.get_data()).get("locale"))
    value = await backend.system_cleanup_overview(user_id)
    rows = []
    latest = value.get("latest_job")
    running = latest and latest["status"] in {
        "queued",
        "running",
        "blocked",
        "awaiting_shutdown",
    }
    if value["supported"] and not running:
        for action, nodes in (
            ("reset", False),
            ("reset", True),
            ("remove", False),
            ("remove", True),
        ):
            rows.append(
                [
                    InlineKeyboardButton(
                        text=tr(
                            locale,
                            f"system_cleanup.{action}_{'nodes' if nodes else 'local'}",
                        ),
                        callback_data=f"sc_plan:{action}:{int(nodes)}",
                        style='danger',
                    )
                ]
            )
    if latest:
        rows.append(
            [
                InlineKeyboardButton(
                    text=tr(locale, "system_cleanup.result"),
                    callback_data="sc_job:" + latest["id"],
                )
            ]
        )
    rows.append([back(locale, AdminSettingsCallback().pack())])
    lines = (
        tr(locale, "system_cleanup.description"),
        tr(locale, "system_cleanup.counts", **value["counts"]),
    )
    if not value["supported"]:
        reason = value.get("reason")
        lines += (
            friendly(locale, BackendError(reason, 409))
            if reason == "installation_manifest_required"
            else tr(locale, "system_cleanup.unsupported"),
        )
    sections = [Section(tr(locale, 'maintenance.rich.inventory'), tables=(Table(
        (tr(locale, 'maintenance.rich.field'), tr(locale, 'maintenance.rich.value')),
        tuple((tr(locale, 'maintenance.rich.' + key), str(value['counts'][key]))
            for key in ('accounts', 'profiles', 'nodes'))),))]
    if value['supported'] and not running:
        sections.extend((
            Section(tr(locale, 'system_cleanup.rich.reset'), (tr(locale, 'system_cleanup.reset_warning'),),
                rows=(tuple(rows[0] + rows[1]),)),
            Section(tr(locale, 'system_cleanup.rich.remove'), (tr(locale, 'system_cleanup.remove_warning'),),
                rows=(tuple(rows[2] + rows[3]),))))
    if latest:
        sections.append(Section(tr(locale, 'system_cleanup.result'), rows=(tuple(rows[-2]),)))
    await render(
        bot,
        chat_id,
        Screen(tr(locale, "system_cleanup.title"), (lines[0], *lines[2:]),
            sections=tuple(sections), embedded_buttons=True, navigation=True),
        [rows[-1]],
        state,
        message_id,
    )


async def show_plan(chat_id, user_id, message_id, bot, state, error=None):
    data = await state.get_data()
    locale = normalize_locale(data.get("locale"))
    plan = data["system_cleanup_draft"]["plan"]
    await state.set_state(CleanupState.phrase)
    lines = [
        tr(locale, "system_cleanup." + plan["action"] + "_warning"),
        tr(
            locale,
            "system_cleanup.nodes_warning"
            if plan["cleanup_nodes"]
            else "system_cleanup.keep_nodes_warning",
        ),
        tr(
            locale,
            "system_cleanup.paths",
            base=plan["deployment"]["base_dir"],
            shared=plan["deployment"]["shared_dir"],
        ),
        tr(locale, "system_cleanup.phrase", phrase=plan["confirmation_phrase"]),
    ]
    if error:
        lines.append(error)
    await render(
        bot,
        chat_id,
        Screen(tr(locale, "system_cleanup.confirm"), tuple(lines[:2] + lines[3:]),
            sections=(Section(tr(locale, 'nodes.rich.technical'), (lines[2],), collapsed=True),),
            embedded_buttons=True, navigation=True),
        [[back(locale)]],
        state,
        message_id,
    )


async def show_job(
    chat_id, user_id, message_id, job_id, bot, backend, state, error=None
):
    await state.set_state(None)
    locale = normalize_locale((await state.get_data()).get("locale"))
    value = await backend.system_cleanup_job(user_id, job_id)
    lines = [
        tr(locale, "system_cleanup.status." + value["status"]),
        tr(locale, "system_cleanup.phase." + value["phase"]),
    ]
    if value.get("backup_id"):
        lines.append(tr(locale, "system_cleanup.backup", id=value["backup_id"]))
    sections = [Section(tr(locale, 'maintenance.rich.progress'), tables=(Table(
        (tr(locale, 'maintenance.rich.field'), tr(locale, 'maintenance.rich.value')),
        ((tr(locale, 'announce.rich.state'), lines[0]),
         (tr(locale, 'maintenance.rich.phase'), lines[1]))),))]
    if value['items']:
        sections.append(Section(tr(locale, 'admin.nodes'), tables=(Table(
            (tr(locale, 'admin.nodes'), tr(locale, 'announce.rich.state')),
            tuple((item['node_key'], tr(locale, 'system_cleanup.item.' + item['status']))
                for item in value['items'])),), collapsed=True))
    details = tuple(lines[2:])
    lines = []
    if value.get("error_code"):
        lines.append(friendly(locale, BackendError(value["error_code"], 409)))
    if error:
        lines.append(error)
    rows = []
    if value["status"] in {"queued", "running", "blocked", "awaiting_shutdown"}:
        rows.append(
            [
                InlineKeyboardButton(
                    text=tr(locale, "system_cleanup.refresh"),
                    callback_data="sc_job:" + job_id,
                )
            ]
        )
    if value["status"] == "blocked" and value["phase"] != "uninstall_launch":
        rows.append(
            [
                InlineKeyboardButton(
                    text=tr(locale, "system_cleanup.retry"),
                    callback_data="sc_retry:" + job_id,
                    style='danger',
                )
            ]
        )
    if value["status"] in {"blocked", "awaiting_shutdown"} and value["phase"] not in {
        "local_files",
        "uninstall_launch",
    }:
        rows.append(
            [
                InlineKeyboardButton(
                    text=tr(locale, "system_cleanup.abort"),
                    callback_data="sc_abort:" + job_id,
                )
            ]
        )
    if value["status"] == "awaiting_shutdown":
        lines += [
            tr(locale, "system_cleanup.shutdown_note"),
        ]
        details += (tr(locale, 'system_cleanup.unit', unit='node-plane-uninstall-' + job_id),)
        rows.append(
            [
                back(locale),
                InlineKeyboardButton(
                    text=tr(locale, "system_cleanup.shutdown"),
                    callback_data="sc_shutdown:" + job_id,
                    style='danger',
                ),
            ]
        )
    else:
        rows.append([back(locale)])
    if details:
        sections.append(Section(tr(locale, 'nodes.rich.technical'), details, collapsed=True))
    await render(
        bot,
        chat_id,
        Screen(tr(locale, "system_cleanup.title"), tuple(lines), sections=tuple(sections),
            embedded_buttons=True, navigation=True),
        rows,
        state,
        message_id,
    )


@router.callback_query(F.data == "system_cleanup")
async def cleanup_root(
    query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext
):
    await query.answer()
    locale = normalize_locale((await state.get_data()).get("locale"))
    try:
        await show_root(
            query.message.chat.id,
            query.from_user.id,
            query.message.message_id,
            bot,
            backend,
            state,
        )
    except BackendError as exc:
        await render(
            bot,
            query.message.chat.id,
            Screen(tr(locale, "system_cleanup.title"), (friendly(locale, exc),), embedded_buttons=True, navigation=True),
            [[back(locale, AdminSettingsCallback().pack())]],
            state,
            query.message.message_id,
        )


@router.callback_query(F.data.startswith("sc_"))
async def cleanup_action(
    query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext
):
    await query.answer()
    locale = normalize_locale((await state.get_data()).get("locale"))
    chat_id, user_id, message_id = (
        query.message.chat.id,
        query.from_user.id,
        query.message.message_id,
    )
    action, *args = query.data.split(":")
    try:
        if (
            action == "sc_plan"
            and len(args) == 2
            and args[0] in {"reset", "remove"}
            and args[1] in {"0", "1"}
        ):
            plan = await backend.system_cleanup_plan(user_id, args[0], args[1] == "1")
            await state.update_data(
                system_cleanup_draft={"plan": plan, "key": str(uuid4())},
                control_message_id=message_id,
            )
            await show_plan(chat_id, user_id, message_id, bot, state)
        elif action == "sc_job" and len(args) == 1:
            await show_job(chat_id, user_id, message_id, args[0], bot, backend, state)
        elif action in {"sc_retry", "sc_abort"} and len(args) == 1:
            await backend.system_cleanup_action(
                user_id, args[0], action.removeprefix("sc_")
            )
            await show_job(chat_id, user_id, message_id, args[0], bot, backend, state)
        elif action == "sc_shutdown" and len(args) == 1:
            job = await backend.system_cleanup_job(user_id, args[0])
            if job["status"] != "awaiting_shutdown":
                await show_job(
                    chat_id, user_id, message_id, args[0], bot, backend, state
                )
                return
            # Deliver the final result before acknowledging shutdown. Failed
            # Telegram edits must leave the backend/client alive.
            await render(
                bot,
                chat_id,
                Screen(
                    tr(locale, "system_cleanup.title"),
                    (
                        tr(locale, "system_cleanup.shutdown_accepted"),
                        tr(
                            locale,
                            "system_cleanup.unit",
                            unit="node-plane-uninstall-" + args[0],
                        ),
                    ),
                 embedded_buttons=True, navigation=True),
                [],
                state,
                message_id,
            )
            await backend.system_cleanup_action(user_id, args[0], "shutdown-ack")
    except BackendError as exc:
        await render(
            bot,
            chat_id,
            Screen(tr(locale, "system_cleanup.title"), (friendly(locale, exc),), embedded_buttons=True, navigation=True),
            [[back(locale)]],
            state,
            message_id,
        )


@router.message(CleanupState.phrase, F.text)
async def cleanup_phrase(
    message: Message, bot: Bot, backend: BackendClient, state: FSMContext
):
    if message.from_user is None or message.chat.type != "private":
        return
    data = await state.get_data()
    draft = data.get("system_cleanup_draft")
    if not draft:
        await state.set_state(None)
        return
    locale = normalize_locale(data.get("locale"))
    with suppress(TelegramAPIError):
        await message.delete()
    phrase = (message.text or "").strip()
    chat_id, user_id, message_id = (
        message.chat.id,
        message.from_user.id,
        data.get("control_message_id"),
    )
    if phrase != draft["plan"]["confirmation_phrase"]:
        await show_plan(
            chat_id,
            user_id,
            message_id,
            bot,
            state,
            tr(locale, "system_cleanup.error.confirmation_mismatch"),
        )
        return
    try:
        job = await backend.system_cleanup_command(
            user_id, draft["plan"]["id"], phrase, draft["key"]
        )
        await show_job(chat_id, user_id, message_id, job["id"], bot, backend, state)
    except BackendError as exc:
        await show_plan(chat_id, user_id, message_id, bot, state, friendly(locale, exc))
