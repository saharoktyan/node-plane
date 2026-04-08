from __future__ import annotations

from collections import defaultdict
from typing import List, Optional, Set, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import CB_CFG, LIST_PAGE_SIZE
from domain.servers import get_access_methods, get_access_methods_for_codes, get_server
from i18n import t
from services.provisioning_state import render_profile_server_state_summary


def _selected_method_labels_for_server(server_key: str, selected: Set[str]) -> str:
    labels = [method.short_label.split(" ", 1)[1] for method in get_access_methods_for_codes(sorted(selected)) if method.server_key == server_key]
    return ", ".join(labels)


def render_proto_keyboard(selected: Set[str], lang: str = "ru", editing: bool = False) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    seen_servers: set[str] = set()
    for method in get_access_methods():
        if method.server_key in seen_servers:
            continue
        seen_servers.add(method.server_key)
        server = get_server(method.server_key)
        chosen = _selected_method_labels_for_server(method.server_key, selected)
        label = f"{server.flag} {server.title}"
        if chosen:
            label += f" · {chosen}"
        rows.append([InlineKeyboardButton(label, callback_data=f"{CB_CFG}proto:server:{method.server_key}")])
    rows.append(
        [
            InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_CFG}back"),
            InlineKeyboardButton(t(lang, "admin.wizard.save_now" if editing else "admin.wizard.next"), callback_data=f"{CB_CFG}proto:done"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def render_proto_server_keyboard(server_key: str, selected: Set[str], lang: str = "ru") -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    methods = [method for method in get_access_methods() if method.server_key == server_key]
    for method in methods:
        method_name = method.short_label.split(" ", 1)[1]
        label = f">{method_name}<" if method.code in selected else method_name
        rows.append([InlineKeyboardButton(label, callback_data=f"{CB_CFG}proto:method:{method.code}")])
    rows.append([InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_CFG}proto:servers")])
    return InlineKeyboardMarkup(rows)


def render_protocols_summary(protocols: Set[str]) -> str:
    if not protocols:
        return "—"

    grouped = defaultdict(list)
    for method in get_access_methods_for_codes(sorted(protocols)):
        grouped[method.server_key].append(method)

    lines = []
    for server_key, methods in grouped.items():
        server = get_server(server_key)
        labels = ", ".join(method.short_label.split(" ", 1)[1] for method in methods)
        lines.append(f"• {server.flag} *{server.title}*: {labels}")
    return "\n".join(lines)


def _profile_provisioning_block(name: str, lang: str = "ru") -> str:
    state_txt = render_profile_server_state_summary(name, lang)
    if state_txt == "—":
        return "—"
    lines = [line.strip() for line in state_txt.splitlines() if line.strip()]
    issue_lines = [line for line in lines if not line.startswith("• ")]
    if not issue_lines:
        return "• Все методы готовы" if lang == "ru" else "• All methods are ready"
    return "\n".join(lines)


def render_protocol_select_text(name: str, selected: Set[str], editing: bool = False, lang: str = "ru") -> str:
    summary = render_protocols_summary(selected)
    action = "Измени" if (editing and lang == "ru") else "Выбери" if lang == "ru" else "Update" if editing else "Choose"
    profile_label = "Профиль" if lang == "ru" else "Profile"
    choose_text = "серверы и способы подключения" if lang == "ru" else "servers and connection methods"
    current_label = "Текущий выбор" if lang == "ru" else "Current selection"
    done_text = (
        "Когда закончишь, нажми *Сохранить*."
        if editing and lang == "ru"
        else "When finished, press *Save*."
        if editing
        else "Когда закончишь, нажми *Далее*."
        if lang == "ru"
        else "When finished, press *Next*."
    )
    return (
        f"{profile_label}: *{name}*\n\n"
        f"{action} {choose_text}.\n\n"
        f"{current_label}:\n{summary}\n\n"
        f"{done_text}"
    )


def render_protocol_server_select_text(name: str, server_key: str, selected: Set[str], editing: bool = False, lang: str = "ru") -> str:
    server = get_server(server_key)
    current = _selected_method_labels_for_server(server_key, selected) or "—"
    if lang == "ru":
        action = "Измени" if editing else "Выбери"
        done_text = "Нажми нужный протокол. Повторное нажатие снимет выбор."
        current_label = "Текущий выбор"
    else:
        action = "Update" if editing else "Choose"
        done_text = "Press a protocol to toggle it for this server."
        current_label = "Current selection"
    return (
        f"{'Профиль' if lang == 'ru' else 'Profile'}: *{name}*\n\n"
        f"{server.flag} *{server.title}*\n\n"
        f"{action} {'протоколы на этом сервере' if lang == 'ru' else 'protocols on this server'}.\n\n"
        f"{current_label}: {current}\n\n"
        f"{done_text}"
    )


def render_pick(names: List[str], page: int, lang: str = "ru") -> Tuple[str, InlineKeyboardMarkup]:
    total = len(names)
    pages = max(1, (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = names[page * LIST_PAGE_SIZE : (page + 1) * LIST_PAGE_SIZE]
    has_multiple_pages = pages > 1

    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton(f"👤 {name}", callback_data=f"{CB_CFG}pick:{name}")]
        for name in chunk
    ]

    if has_multiple_pages:
        nav: List[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"{CB_CFG}pickpage:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data=f"{CB_CFG}pickpage:{page}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"{CB_CFG}pickpage:{page+1}"))
        rows.append(nav)
        rows.append([InlineKeyboardButton(t(lang, "admin.wizard.search"), callback_data=f"{CB_CFG}search")])
    rows.append([InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_CFG}cancel")])
    return t(lang, "admin.wizard.choose_profile", total=total), InlineKeyboardMarkup(rows)


def render_profile_dashboard(names: List[str], page: int, lang: str = "ru") -> Tuple[str, InlineKeyboardMarkup]:
    total = len(names)
    pages = max(1, (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = names[page * LIST_PAGE_SIZE : (page + 1) * LIST_PAGE_SIZE]
    has_multiple_pages = pages > 1

    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton(f"👤 {name}", callback_data=f"{CB_CFG}card:{name}")]
        for name in chunk
    ]

    rows.append([InlineKeyboardButton(t(lang, "admin.wizard.new_profile"), callback_data=f"{CB_CFG}start:create")])
    if has_multiple_pages:
        rows.append([InlineKeyboardButton(t(lang, "admin.wizard.search"), callback_data=f"{CB_CFG}search")])
        nav: List[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"{CB_CFG}dashboard:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data=f"{CB_CFG}dashboard:{page}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"{CB_CFG}dashboard:{page+1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_CFG}cancel")])
    return t(lang, "admin.wizard.profiles", total=total), InlineKeyboardMarkup(rows)


def render_edit_menu(name: str, protocols: Set[str], frozen: bool, lang: str = "ru") -> Tuple[str, InlineKeyboardMarkup]:
    fr = ("frozen" if frozen else "active") if lang == "en" else ("заморожен" if frozen else "активен")
    title = "✏️ Редактирование" if lang == "ru" else "✏️ Edit"
    status = "Статус" if lang == "ru" else "Status"
    choose = "Что изменить:" if lang == "ru" else "What to edit:"
    return (
        (
            f"{title}: `{name}`\n\n"
            f"• {status}: *{fr}*\n\n"
            f"{choose}"
        ),
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔌 Протоколы" if lang == "ru" else "🔌 Protocols", callback_data=f"{CB_CFG}edit:proto")],
                [
                    InlineKeyboardButton("🧊 Статус" if lang == "ru" else "🧊 Status", callback_data=f"{CB_CFG}edit:status"),
                ],
                [
                    InlineKeyboardButton("💾 Сохранить" if lang == "ru" else "💾 Save", callback_data=f"{CB_CFG}edit:save"),
                    InlineKeyboardButton("🗑 Удалить профиль" if lang == "ru" else "🗑 Delete Profile", callback_data=f"{CB_CFG}edit:delete"),
                ],
                [InlineKeyboardButton("⬅️ К профилю" if lang == "ru" else "⬅️ To Profile", callback_data=f"{CB_CFG}card:{name}")],
            ]
        ),
    )


def render_status_menu(name: str, frozen: bool, lang: str = "ru") -> Tuple[str, InlineKeyboardMarkup]:
    if lang == "ru":
        text = f"🧊 Статус профиля: `{name}`\n\nСейчас: *{'заморожен' if frozen else 'активен'}*\n\nВыбери действие:"
        action_label = "🔥 Разморозить" if frozen else "🧊 Заморозить"
    else:
        text = f"🧊 Profile status: `{name}`\n\nCurrent: *{'frozen' if frozen else 'active'}*\n\nChoose an action:"
        action_label = "🔥 Unfreeze" if frozen else "🧊 Freeze"
    return (
        text,
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        action_label,
                        callback_data=f"{CB_CFG}edit:unfreeze" if frozen else f"{CB_CFG}edit:freeze",
                    ),
                ],
                [InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_CFG}back")],
            ]
        ),
    )


def render_delete_confirm(name: str, lang: str = "ru") -> Tuple[str, InlineKeyboardMarkup]:
    title = "🗑 *Удаление профиля:*" if lang == "ru" else "🗑 *Delete Profile:*"
    body = (
        "Это удалит:\n"
        "• запись профиля из БД\n"
        "• Xray-профиль на выбранных серверах\n"
        "• AWG-профиль на выбранных серверах\n\n"
        "*Точно удалить?*"
        if lang == "ru"
        else
        "This will remove:\n"
        "• the profile record from the database\n"
        "• the Xray profile on selected servers\n"
        "• the AWG profile on selected servers\n\n"
        "*Delete it for sure?*"
    )
    return (
        (
            f"{title} `{name}`\n\n{body}"
        ),
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Да, удалить" if lang == "ru" else "✅ Yes, delete", callback_data=f"{CB_CFG}edit:delete_confirm")],
                [InlineKeyboardButton(t(lang, "menu.back"), callback_data=f"{CB_CFG}back")],
            ]
        ),
    )


def render_profile_card(name: str, protocols: Set[str], frozen: bool, page: int = 0, lang: str = "ru") -> Tuple[str, InlineKeyboardMarkup]:
    proto_txt = render_protocols_summary(protocols)
    state_txt = _profile_provisioning_block(name, lang)
    fr = ("frozen" if frozen else "active") if lang == "en" else ("заморожен" if frozen else "активен")
    access = "Доступ" if lang == "ru" else "Access"
    provision = "Применение" if lang == "ru" else "Provisioning"
    status = "Статус" if lang == "ru" else "Status"
    return (
        (
            f"👤 `{name}`\n\n"
            f"{access}:\n{proto_txt}\n\n"
            f"{provision}:\n{state_txt}\n\n"
            f"• {status}: *{fr}*"
        ),
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✏️ Редактировать" if lang == "ru" else "✏️ Edit", callback_data=f"{CB_CFG}cardedit:{name}")],
                [InlineKeyboardButton("⬅️ К профилям" if lang == "ru" else "⬅️ To Profiles", callback_data=f"{CB_CFG}dashboard:{page}")],
            ]
        ),
    )
