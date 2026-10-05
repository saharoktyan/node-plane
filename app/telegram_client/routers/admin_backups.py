"""Backup screens backed exclusively by backend commands and read models."""

from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen, Section, Table
from .callbacks import AdminSettingsCallback
from .common import render

router = Router()


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


async def draw(query, bot, state, title, lines, rows, sections=()):
    lang = normalize_locale((await state.get_data()).get("locale"))
    await render(
        bot,
        query.message.chat.id,
        Screen(tr(lang, title), tuple(lines), sections=tuple(sections), embedded_buttons=True, navigation=True),
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
    summary = Table((tr(lang, 'maintenance.rich.field'), tr(lang, 'maintenance.rich.value')), (
        (tr(lang, 'backups.rich.count'), str(value['count'])),
        (tr(lang, 'backups.rich.size'), f"{value['size_bytes'] / 1048576:.1f} MiB"),
        (tr(lang, 'backups.rich.latest'), value['latest']['created_at'][:19] if value['latest'] else '—')))
    sections = [Section(tr(lang, 'backups.rich.storage'), tables=(summary,), rows=(tuple(rows[0]),)),
        Section(tr(lang, 'backups.settings'), (tr(lang, 'backups.policy', hours=value['interval_hours'], keep=value['keep_count']),),
            rows=(tuple(rows[1]),)),
        Section(tr(lang, 'backups.rich.scope'), (tr(lang, 'backups.scope'),), collapsed=True)]
    if value.get('last_job'):
        sections.insert(2, Section(tr(lang, 'backups.result'), (lines[-1],), rows=(tuple(rows[2]),)))
    await draw(query, bot, state, 'backups.title', [], [rows[-1]], sections)


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
        nav.append(button("←", f"backup_list:{max(0, offset - 8)}"))
    if page["next_offset"] is not None:
        nav.append(button("→", f"backup_list:{page['next_offset']}"))
    if nav:
        rows.append(nav)
    rows.append([button(tr(lang, "back"), "backups")])
    await state.update_data(backup_offset=offset)
    lines = [tr(lang, 'backups.choose') if page['items'] else tr(lang, 'backups.empty')]
    if page.get('total', 0) > 8:
        lines.append(tr(lang, 'pagination.page', page=offset // 8 + 1,
            pages=max(1, (page['total'] + 7) // 8)))
    await draw(
        query,
        bot,
        state,
        "backups.restore",
        lines,
        rows,
    )


async def settings(query, bot, backend, state):
    lang = normalize_locale((await state.get_data()).get("locale"))
    value = await backend.backups_overview(query.from_user.id)
    rows = [[button(tr(lang, 'settings.rich.enable'), 'backup_pref:enabled:1').model_copy(
        update={'style': 'primary' if value['enabled'] else None}),
        button(tr(lang, 'settings.rich.disable'), 'backup_pref:enabled:0').model_copy(
        update={'style': 'primary' if not value['enabled'] else None})]]
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
        [], [rows[-1]], sections=(
            Section(tr(lang, 'backups.rich.automatic'), rows=(tuple(rows[0]),)),
            Section(tr(lang, 'backups.rich.interval'), rows=(tuple(rows[1]),)),
            Section(tr(lang, 'backups.rich.retention'), rows=(tuple(rows[2]),))),
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
    sections = (Section(tr(lang, 'maintenance.rich.progress'), tables=(Table(
        (tr(lang, 'maintenance.rich.field'), tr(lang, 'maintenance.rich.value')),
        ((tr(lang, 'announce.rich.state'), lines[0]),
         (tr(lang, 'command.rich.description'), tr(lang,
            'backups.create' if job['action'] == 'create' else 'backups.restore')))),)),)
    await draw(query, bot, state, "backups.result", lines[1:], rows, sections)


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
            rows = []
            if info["compatible"]:
                rows.append(
                    [
                        button(
                            tr(lang, "backups.restore"), f"backup_restore:{info['id']}"
                        ).model_copy(update={'style': 'danger'})
                    ]
                )
            offset = (await state.get_data()).get("backup_offset", 0)
            rows.append([button(tr(lang, "back"), f"backup_list:{offset}")])
            metadata = Table((tr(lang, 'maintenance.rich.field'), tr(lang, 'maintenance.rich.value')), (
                (tr(lang, 'backups.rich.created'), info['created_at'][:19]),
                (tr(lang, 'backups.rich.version'), info['app_version']),
                (tr(lang, 'profiles.title'), str(info['profiles'])),
                (tr(lang, 'admin.nodes'), str(info['nodes']))))
            await draw(query, bot, state, 'backups.details',
                [] if info['compatible'] else [tr(lang, 'backups.incompatible')], rows,
                sections=(Section(tr(lang, 'backups.rich.contents'), tables=(metadata,)),
                    Section(tr(lang, 'backups.rich.scope'), (tr(lang, 'backups.scope'),), collapsed=True)))
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
                        ).model_copy(update={'style': 'danger' if body['action'] == 'restore' else 'primary'}),
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
            allowed = {'enabled': {0, 1}, 'interval_hours': {6, 12, 24}, 'keep_count': {5, 10, 20}}
            if field not in allowed or len(parts) not in {2, 3} or len(parts) == 2 and field != 'enabled':
                raise ValueError('Invalid backup preference')
            if len(parts) == 3 and int(parts[2]) not in allowed[field]:
                raise ValueError('Invalid backup preference value')
            changes = (
                {field: bool(int(parts[2])) if field == 'enabled' else int(parts[2])}
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
