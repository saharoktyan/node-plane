from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext

from .common import render, BackendMiddleware
from ..backend import BackendClient, BackendError
from ..screens import Screen
from .callbacks import AdminSettingsCallback, HomeCallback, UpdatesCallback

class SshKeyCallback(CallbackData, prefix="ssh_key"):
    pass

class RegenerateSshKeyCallback(CallbackData, prefix="regen_ssh"):
    pass

router = Router()

@router.callback_query(AdminSettingsCallback.filter())
async def admin_settings_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    rows = [
        [
            InlineKeyboardButton(text="Название бота", callback_data="settings_bot_title"),
            InlineKeyboardButton(text="Заявки на доступ", callback_data=RequestsCallback().pack()),
        ],
        [
            InlineKeyboardButton(text="Обновления", callback_data=UpdatesCallback().pack()),
            InlineKeyboardButton(text="💾 Бэкапы", callback_data="settings_backups"),
        ],
        [InlineKeyboardButton(text="🔐 SSH ключ", callback_data=SshKeyCallback().pack())],
        [InlineKeyboardButton(text="Сброс / Очистка", callback_data="settings_cleanup")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_menu")]
    ]
    await render(bot, query.message.chat.id, Screen("Настройки системы", ("Глобальные параметры контроллера.",)), rows, state, query.message.message_id)


@router.callback_query(SshKeyCallback.filter())
async def ssh_key_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    try:
        response = await backend.request("GET", "/api/v1/system/ssh-key", telegram_user_id=query.from_user.id)
        pub_key = response["public_key"]
        
        rows = [
            [InlineKeyboardButton(text="🔙 Назад", callback_data=AdminSettingsCallback().pack())]
        ]
        
        await render(bot, query.message.chat.id, Screen(
            "Управление SSH ключом",
            ("Этот публичный ключ используется для аутентификации при автоматической установке узлов через SSH.", "Добавьте этот ключ в ~/.ssh/authorized_keys на целевом VPS перед добавлением узла в Node Plane."),
            "Публичный ключ",
            (f"`{pub_key}`",)
        ), rows, state, query.message.message_id)
        
    except BackendError as exc:
        await render(bot, query.message.chat.id, Screen("Ошибка", (f"Не удалось прочитать SSH ключ: {exc.code}",)), [[InlineKeyboardButton(text="🔙 Назад", callback_data=AdminSettingsCallback().pack())]], state, query.message.message_id)

@router.callback_query(F.data.startswith("settings_"))
async def settings_placeholder_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    rows = [[InlineKeyboardButton(text="🔙 Назад", callback_data=AdminSettingsCallback().pack())]]
    await render(bot, query.message.chat.id, Screen("В разработке", ("Этот раздел настроек еще не перенесен.",)), rows, state, query.message.message_id)



class UpdateActionCallback(CallbackData, prefix="upd_act"):
    action: str

@router.callback_query(UpdatesCallback.filter())
async def updates_menu_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    
    try:
        overview = await backend.updates_overview(query.from_user.id)
    except Exception as e:
        await query.answer(f"Ошибка загрузки: {e}", show_alert=True)
        return
        
    branch = overview.get('branch', 'unknown')
    mode = overview.get('install_mode', 'simple')
    cur_ver = overview.get('current_version', '-')
    cur_label = overview.get('current_label', '-')
    rem_ver = overview.get('remote_version', '-')
    rem_label = overview.get('remote_label', '-')
    
    last_status = overview.get('last_run_status', 'never')
    last_log = overview.get('last_run_log_tail', '')
    
    lines = [
        "📦 Инфо о системе:",
        f"Ветка: {branch}",
        f"Установка: {mode}",
        "",
        f"Текущая версия: {cur_label} ({cur_ver})",
        f"Доступная версия: {rem_label} ({rem_ver})" if rem_label else "Доступная версия: проверьте наличие",
        "",
        "Статус последнего обновления: " + last_status
    ]
    
    if last_status == 'failed' and last_log:
        lines.append(f"\nЛог ошибки:\n`{last_log}`")
        
    rows = []
    
    if last_status == 'running':
        rows.append([InlineKeyboardButton(text="🔄 Проверить статус установки", callback_data=UpdateActionCallback(action='status').pack())])
    else:
        rows.append([InlineKeyboardButton(text="🔍 Проверить обновления", callback_data=UpdateActionCallback(action='check').pack())])
        if overview.get('update_available'):
            rows.append([InlineKeyboardButton(text="🚀 Обновить всё (Update All)", callback_data=UpdateActionCallback(action='run').pack())])
            
    rows.append([InlineKeyboardButton(text="🧹 Очистка старых версий", callback_data=UpdateActionCallback(action='cleanup_menu').pack())])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=AdminSettingsCallback().pack())])
    
    await render(bot, query.message.chat.id, Screen("Обновления системы", tuple(lines)), rows, state, query.message.message_id)

@router.callback_query(UpdateActionCallback.filter())
async def update_action_cb(query: CallbackQuery, callback_data: UpdateActionCallback, bot: Bot, backend: BackendClient, state: FSMContext):
    action = callback_data.action
    user_id = query.from_user.id
    
    if action == 'check':
        await query.answer("Проверяем наличие обновлений...")
        await backend.check_updates(user_id)
        await updates_menu_cb(query, bot, backend, state)
        return
        
    if action == 'run':
        await query.answer("Запускаем обновление всех компонентов...")
        await backend.run_update(user_id)
        # Transition to status polling screen
        rows = [[InlineKeyboardButton(text="🔄 Check (Обновить)", callback_data=UpdateActionCallback(action='status').pack())]]
        await render(bot, query.message.chat.id, Screen("Обновление выполняется", ("Процесс обновления начат. Нажмите Check, чтобы проверить статус.",)), rows, state, query.message.message_id)
        return
        
    if action == 'status':
        await query.answer()
        overview = await backend.updates_overview(user_id)
        status = overview.get('last_run_status', '')
        log = overview.get('last_run_log_tail', '')
        
        import datetime
        try:
            started = overview.get('last_run_started_at', '')
            if started:
                started_dt = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
                elapsed = int((datetime.datetime.now(datetime.timezone.utc) - started_dt).total_seconds())
                time_str = f"{elapsed}s"
            else:
                time_str = "..."
        except:
            time_str = "..."
            
        core_st, tg_st, drv_st = "running", "pending", "pending"
        agents = {}
        for line in log.splitlines():
            if "activate new release" in line or "restart node-plane.service" in line:
                core_st = "done"
                tg_st = "running"
            if "Running post-update driver/agent setup" in line:
                tg_st = "done"
                drv_st = "running"
            if "install local node-plane-driver" in line or "node-plane-driver binary is up to date" in line:
                drv_st = "done"
            
            if "Deploying node-agent to local node" in line:
                parts = line.split("local node ")
                if len(parts) > 1:
                    key = parts[1].replace("...", "").strip()
                    agents[key] = "running"
            elif "Deploying node-agent to " in line:
                parts = line.split("Deploying node-agent to ")
                if len(parts) > 1:
                    key = parts[1].split(" ")[0].strip()
                    agents[key] = "running"
            
            if "node-agent is active on " in line:
                parts = line.split("node-agent is active on ")
                if len(parts) > 1:
                    key = parts[1].split(" ")[-1].strip()
                    agents[key] = "done"
            elif "node-agent deploy failures" in line or "failed to install/start node-agent on" in line.lower():
                for k, v in list(agents.items()):
                    if v == "running": agents[k] = "failed"
                    
        if status == 'success' or status == 'completed':
            core_st, tg_st, drv_st = "done", "done", "done"
            for k in list(agents.keys()): agents[k] = "done"
        elif status == 'failed':
            if core_st == "running": core_st = "failed"
            elif tg_st == "running": tg_st = "failed"
            elif drv_st == "running": drv_st = "failed"
            for k in list(agents.keys()):
                if agents[k] == "running": agents[k] = "failed"

        reason = "Неизвестная ошибка"
        for line in log.splitlines():
            if "Update failed during step:" in line:
                reason = line.split(":", 1)[-1].strip()
            elif "failed" in line.lower() or "error" in line.lower():
                reason = line[:50]

        def fmt(s):
            if s == "done": return f"done ({time_str})"
            if s == "running": return f"running ({time_str})"
            if s == "failed": return f"failed ({reason})"
            return s
            
        lines = [
            f"Core: {fmt(core_st)}",
            f"Telegram: {fmt(tg_st)}",
            f"Driver: {fmt(drv_st)}"
        ]
        if agents:
            lines.append("Agents:")
            for k, v in agents.items():
                lines.append(f"  - {k}: {fmt(v)}")

        if status == 'running':
            rows = [[InlineKeyboardButton(text="🔄 Check (Обновить)", callback_data=UpdateActionCallback(action='status').pack())]]
            await render(bot, query.message.chat.id, Screen("Обновление выполняется", tuple(lines)), rows, state, query.message.message_id)
        elif status == 'success' or status == 'completed':
            rows = [[InlineKeyboardButton(text="✅ Done", callback_data=UpdatesCallback().pack())]]
            lines.insert(0, "Обновление успешно завершено! Все компоненты обновлены.")
            lines.append("")
            await render(bot, query.message.chat.id, Screen("Успех", tuple(lines)), rows, state, query.message.message_id)
        else:
            rows = [[InlineKeyboardButton(text="🔙 Назад", callback_data=UpdatesCallback().pack())]]
            err_lines = list(lines)
            err_lines.append("")
            err_lines.append(f"Лог:\n`{log[-1000:]}`")
            await render(bot, query.message.chat.id, Screen("Ошибка обновления", tuple(err_lines)), rows, state, query.message.message_id)
        return
        
    if action == 'cleanup_menu':
        await query.answer()
        try:
            cl = await backend.cleanup_overview(user_id)
        except Exception as e:
            await query.answer(f"Ошибка: {e}", show_alert=True)
            return
            
        status = cl.get('last_run_status', 'never')
        
        lines = ["Меню очистки старых релизов", f"Статус: {status}"]
        if cl.get('last_run_log_tail'):
            lines.append(f"Лог:\n`{cl.get('last_run_log_tail')}`")
            
        rows = [
            [InlineKeyboardButton(text="🧹 Запустить очистку", callback_data=UpdateActionCallback(action='cleanup_run').pack())],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=UpdatesCallback().pack())]
        ]
        await render(bot, query.message.chat.id, Screen("Очистка (Cleanup)", tuple(lines)), rows, state, query.message.message_id)
        return
        
    if action == 'cleanup_run':
        await query.answer("Очистка запущена...")
        await backend.run_cleanup(user_id)
        rows = [[InlineKeyboardButton(text="🔄 Обновить статус", callback_data=UpdateActionCallback(action='cleanup_menu').pack())]]
        await render(bot, query.message.chat.id, Screen("Очистка", ("Процесс запущен.",)), rows, state, query.message.message_id)
        return

