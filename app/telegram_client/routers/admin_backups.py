"""Backup screens backed exclusively by backend commands and read models."""

from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen
from .callbacks import AdminSettingsCallback
from .common import render

router = Router()


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


async def draw(query, bot, state, title, lines, rows):
    lang = normalize_locale((await state.get_data()).get("locale"))
    await render(
        bot,
        query.message.chat.id,
        Screen(tr(lang, title), tuple(lines)),
        rows,
        state,
        query.message.message_id,
    )


async def overview(query, bot, backend, state):
    lang = normalize_locale((await state.get_data()).get("locale"))
    value = await backend.backups_overview(query.from_user.id)
    lines = [
        tr(
            lang,
            "backups.count",
            count=value["count"],
            size=round(value["size_bytes"] / 1048576, 1),
        ),
        tr(
            lang,
            "backups.latest",
            date=value["latest"]["created_at"][:19] if value["latest"] else "—",
        ),
        tr(lang, "backups.scope"),
    ]
    rows = [
        [
            button(tr(lang, "backups.create"), "backup_create"),
            button(tr(lang, "backups.restore"), "backup_list:0"),
        ],
        [button(tr(lang, "backups.settings"), "backup_settings")],
    ]
    if value.get("last_job"):
        lines.append(
            tr(
                lang,
                "backups.last",
                status=tr(lang, "update_tools.status." + value["last_job"]["status"]),
            )
        )
        rows.append(
            [
                button(
                    tr(lang, "backups.result"), f"backup_job:{value['last_job']['id']}"
                )
            ]
        )
    rows.append([button(tr(lang, "back"), AdminSettingsCallback().pack())])
    await draw(query, bot, state, "backups.title", lines, rows)


async def catalog(query, bot, backend, state, offset):
    lang = normalize_locale((await state.get_data()).get("locale"))
    page = await backend.backup_catalog(query.from_user.id, offset)
    rows = [
        [
            button(
                f"{item['created_at'][:19]} · {item['app_version']}",
                f"backup_detail:{item['id']}",
            )
        ]
        for item in page["items"]
    ]
    nav = []
    if offset:
        nav.append(button("‹", f"backup_list:{max(0, offset - 8)}"))
    if page["next_offset"] is not None:
        nav.append(button("›", f"backup_list:{page['next_offset']}"))
    if nav:
        rows.append(nav)
    rows.append([button(tr(lang, "back"), "backups")])
    await state.update_data(backup_offset=offset)
    await draw(
        query,
        bot,
        state,
        "backups.restore",
        [tr(lang, "backups.choose") if page["items"] else tr(lang, "backups.empty")],
        rows,
    )


async def settings(query, bot, backend, state):
    lang = normalize_locale((await state.get_data()).get("locale"))
    value = await backend.backups_overview(query.from_user.id)
    rows = [
        [
            button(
                tr(lang, "backups.enabled" if value["enabled"] else "backups.disabled"),
                "backup_pref:enabled",
            )
        ]
    ]
    for field, values in (("interval_hours", (6, 12, 24)), ("keep_count", (5, 10, 20))):
        rows.append(
            [
                button(
                    str(v),
                    f"backup_pref:{field}:{v}",
                ).model_copy(update={"style": "primary" if value[field] == v else None})
                for v in values
            ]
        )
    rows.append([button(tr(lang, "back"), "backups")])
    await draw(
        query,
        bot,
        state,
        "backups.settings",
        [
            tr(
                lang,
                "backups.policy",
                hours=value["interval_hours"],
                keep=value["keep_count"],
            )
        ],
        rows,
    )


async def result(query, bot, backend, state, job_id):
    lang = normalize_locale((await state.get_data()).get("locale"))
    job = await backend.backup_job(query.from_user.id, job_id)
    lines = [tr(lang, "update_tools.status." + job["status"])]
    if job.get("phase") == "revoking":
        lines.append(tr(lang, "backups.revoking"))
    if job.get("result", {}):
        status = job["result"].get("status")
        if status == "duplicate":
            lines.append(tr(lang, "backups.duplicate"))
        elif job["action"] == "restore" and status == "success":
            lines.append(tr(lang, "backups.restored"))
        elif status == "failed":
            lines.append(tr(lang, "backups.failed"))
    rows = []
    if job["status"] in {"awaiting_executor", "running"}:
        rows.append([button(tr(lang, "updates.refresh"), f"backup_job:{job_id}")])
    rows.append([button(tr(lang, "back"), "backups")])
    await draw(query, bot, state, "backups.result", lines, rows)


@router.callback_query(F.data == "backups")
@router.callback_query(F.data == "backup_create")
@router.callback_query(F.data == "backup_settings")
@router.callback_query(F.data.startswith("backup_list:"))
@router.callback_query(F.data.startswith("backup_detail:"))
@router.callback_query(F.data.startswith("backup_restore:"))
@router.callback_query(F.data.startswith("backup_pref:"))
@router.callback_query(F.data.startswith("backup_submit:"))
@router.callback_query(F.data.startswith("backup_job:"))
async def backup_cb(
    query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext
):
    await query.answer()
    lang = normalize_locale((await state.get_data()).get("locale"))
    try:
        action = query.data
        if action == "backups":
            await overview(query, bot, backend, state)
        elif action == "backup_settings":
            await settings(query, bot, backend, state)
        elif action.startswith("backup_list:"):
            await catalog(query, bot, backend, state, max(0, int(action.split(":")[1])))
        elif action.startswith("backup_detail:"):
            info = await backend.backup_detail(query.from_user.id, action.split(":")[1])
            lines = [
                tr(
                    lang,
                    "backups.metadata",
                    date=info["created_at"][:19],
                    version=info["app_version"],
                    profiles=info["profiles"],
                    nodes=info["nodes"],
                ),
                tr(lang, "backups.scope"),
            ]
            rows = []
            if info["compatible"]:
                rows.append(
                    [
                        button(
                            tr(lang, "backups.restore"), f"backup_restore:{info['id']}"
                        )
                    ]
                )
            else:
                lines.append(tr(lang, "backups.incompatible"))
            offset = (await state.get_data()).get("backup_offset", 0)
            rows.append([button(tr(lang, "back"), f"backup_list:{offset}")])
            await draw(query, bot, state, "backups.details", lines, rows)
        elif action == "backup_create" or action.startswith("backup_restore:"):
            body = {"action": "create"}
            if action.startswith("backup_restore:"):
                info = await backend.backup_detail(
                    query.from_user.id, action.split(":")[1]
                )
                if not info["compatible"]:
                    raise BackendError("backup_incompatible", 409)
                body = {
                    "action": "restore",
                    "backup_id": info["id"],
                    "checksum": info["checksum"],
                }
            nonce = uuid4().hex[:8]
            await state.update_data(
                backup_draft={"body": body, "key": str(uuid4()), "nonce": nonce}
            )
            back = (
                f"backup_detail:{body['backup_id']}"
                if body["action"] == "restore"
                else "backups"
            )
            await draw(
                query,
                bot,
                state,
                "backups.confirm",
                [
                    tr(
                        lang,
                        "backups.restore_warning"
                        if body["action"] == "restore"
                        else "backups.create_note",
                    )
                ],
                [
                    [
                        button(tr(lang, "back"), back),
                        button(
                            tr(
                                lang,
                                "backups.create"
                                if body["action"] == "create"
                                else "backups.restore",
                            ),
                            f"backup_submit:{nonce}",
                        ),
                    ]
                ],
            )
        elif action.startswith("backup_submit:"):
            draft = (await state.get_data()).get("backup_draft")
            if not draft or draft["nonce"] != action.split(":")[1]:
                return await overview(query, bot, backend, state)
            job = await backend.backup_command(
                query.from_user.id, draft["body"], draft["key"]
            )
            await result(query, bot, backend, state, job["id"])
        elif action.startswith("backup_job:"):
            await result(query, bot, backend, state, action.split(":")[1])
        elif action.startswith("backup_pref:"):
            parts = action.split(":")
            field = parts[1]
            changes = (
                {field: int(parts[2])}
                if len(parts) == 3
                else {
                    "enabled": not (await backend.backups_overview(query.from_user.id))[
                        "enabled"
                    ]
                }
            )
            await backend.backup_preferences(query.from_user.id, changes)
            await settings(query, bot, backend, state)
    except (BackendError, ValueError, KeyError):
        await draw(
            query,
            bot,
            state,
            "backups.failed",
            [tr(lang, "backups.failed_note")],
            [[button(tr(lang, "back"), "backups")]],
        )
