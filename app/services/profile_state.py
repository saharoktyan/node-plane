from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from config import SQLITE_DB_PATH
from db.schema import ensure_schema
from db.sqlite_db import SQLiteDB
from db.stores import SQLiteAWGStore, SQLiteProfileStateStore, SQLiteTelegramUsersStore
from domain.servers import get_access_method, get_access_methods_for_kind

logger = logging.getLogger(__name__)

_sqlite_db = SQLiteDB(SQLITE_DB_PATH)
_runtime_ready = False


def _bootstrap_sqlite_runtime() -> None:
    global _runtime_ready
    if _runtime_ready:
        return
    with _sqlite_db.transaction() as conn:
        ensure_schema(conn)
    _runtime_ready = True


_bootstrap_sqlite_runtime()

profile_store = SQLiteProfileStateStore(_sqlite_db)
user_store = SQLiteTelegramUsersStore(_sqlite_db)
awg_profile_store = SQLiteAWGStore(_sqlite_db)

_AWG_VPN_RE = re.compile(r"(vpn://[A-Za-z0-9+/=_-]+)")


def parse_stored_datetime(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_delta(td: timedelta) -> str:
    total = int(td.total_seconds())
    if total < 0:
        total = 0
    days = total // 86400
    hours = (total % 86400) // 3600
    mins = (total % 3600) // 60
    if days > 0:
        return f"{days}д {hours}ч"
    if hours > 0:
        return f"{hours}ч {mins}м"
    return f"{mins}м"


def get_profile(name: str) -> Dict[str, Any]:
    data = profile_store.read()
    rec = data.get(name)
    return rec if isinstance(rec, dict) else {}


def is_frozen(name: str) -> bool:
    return bool(get_profile(name).get("frozen", False))


def get_profile_access_status(name: str, lang: str = "ru") -> Dict[str, Any]:
    rec = get_profile(name)
    access_word = "Доступ" if lang == "ru" else "Access"
    active_word = "активен" if lang == "ru" else "active"
    frozen_word = "заморожен" if lang == "ru" else "frozen"
    if not rec:
        return {"active": True, "frozen": False, "text": f"{access_word}: *{active_word}*"}

    frozen = bool(rec.get("frozen", False))
    return {
        "active": True,
        "frozen": frozen,
        "text": f"{access_word}: *{frozen_word if frozen else active_word}*",
    }


def freeze_profile(name: str) -> Tuple[bool, str]:
    def mut(d: Dict[str, Any]) -> Dict[str, Any]:
        rec = d.get(name)
        if not isinstance(rec, dict):
            rec = {"type": "none", "created_at": utcnow().isoformat(timespec="minutes"), "expires_at": None}
        rec["frozen"] = True
        d[name] = rec
        return d

    profile_store.update(mut)
    return True, f"🧊 Профиль *{name}* заморожен."


def unfreeze_profile(name: str) -> Tuple[bool, str]:
    def mut(d: Dict[str, Any]) -> Dict[str, Any]:
        rec = d.get(name)
        if not isinstance(rec, dict):
            return d
        rec["frozen"] = False
        d[name] = rec
        return d

    profile_store.update(mut)
    return True, f"🔥 Профиль *{name}* разморожен."


def get_allowed_protocols(name: str) -> List[str]:
    rec = get_profile(name)
    plist = rec.get("protocols")
    if isinstance(plist, list) and plist:
        return [str(x) for x in plist if get_access_method(str(x))]
    x = rec.get("xray")
    if isinstance(x, dict) and x.get("enabled"):
        methods = get_access_methods_for_kind("xray")
        if methods:
            return [methods[0].code]
    return []


def ensure_telegram_profile(user_id: int, preferred_name: str | None = None) -> str:
    users = user_store.read()
    rec = users.get(str(user_id)) if isinstance(users, dict) else None
    existing_profile_name = str(rec.get("profile_name") or "").strip() if isinstance(rec, dict) else ""
    if existing_profile_name and get_profile(existing_profile_name):
        return existing_profile_name

    username = str(preferred_name or "").strip()
    if not username and isinstance(rec, dict):
        username = str(rec.get("username") or "").strip()

    candidate = username or f"tg_{user_id}"
    if username and get_profile(candidate):
        candidate = f"tg_{user_id}"
    suffix = 1
    while get_profile(candidate):
        candidate = f"tg_{user_id}_{suffix}"
        suffix += 1

    now = utcnow().isoformat(timespec="minutes")

    def mut(db: Dict[str, Any]) -> Dict[str, Any]:
        item = db.get(candidate)
        if not isinstance(item, dict):
            item = {}
        item.setdefault("type", "none")
        item.setdefault("created_at", now)
        item.setdefault("expires_at", None)
        item.setdefault("frozen", False)
        item.setdefault("warned_before_exp", False)
        item.setdefault("protocols", [])
        item["updated_at"] = now
        db[candidate] = item
        return db

    profile_store.update(mut)
    user_store.upsert_user(user_id, profile_name=candidate)
    return candidate


def ensure_xray_caps(name: str, uuid_val: str) -> None:
    def mut(s: Dict[str, Any]) -> Dict[str, Any]:
        rec = s.get(name)
        if not isinstance(rec, dict):
            rec = {}
        rec.setdefault("type", "none")
        rec.setdefault("created_at", utcnow().isoformat(timespec="minutes"))
        rec.setdefault("expires_at", None)
        rec.setdefault("frozen", False)
        rec.setdefault("warned_before_exp", False)

        rec["uuid"] = uuid_val
        x = rec.get("xray")
        if not isinstance(x, dict):
            x = {}
        x.setdefault("enabled", True)
        x.setdefault("transports", ["xhttp", "tcp"])
        x.setdefault("default", "xhttp")
        x.setdefault("short_id", "")
        rec["xray"] = x

        s[name] = rec
        return s

    profile_store.update(mut)


def set_xray_short_id(name: str, short_id: str, server_key: str | None = None) -> None:
    def mut(s: Dict[str, Any]) -> Dict[str, Any]:
        rec = s.get(name)
        if not isinstance(rec, dict):
            rec = {}
        x = rec.get("xray")
        if not isinstance(x, dict):
            x = {}
        if server_key:
            mapping = x.get("server_short_ids")
            if not isinstance(mapping, dict):
                mapping = {}
            mapping[str(server_key)] = short_id
            x["server_short_ids"] = mapping
        else:
            x["short_id"] = short_id
        rec["xray"] = x
        s[name] = rec
        return s

    profile_store.update(mut)


def _extract_vpn_key(text: str) -> str | None:
    if not text:
        return None
    m = _AWG_VPN_RE.search(text)
    return m.group(1) if m else None
