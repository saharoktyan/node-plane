# app/services/awg_profiles.py
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _awg_store():
    from services.profile_state import awg_profile_store

    return awg_profile_store


def _normalize_server_entry(server_key: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "server_key": server_key,
        "config": str(entry.get("config") or ""),
        "wg_conf": entry.get("wg_conf"),
        "created_at": entry.get("created_at"),
    }


def _normalize_profile_entry(entry: Any) -> Dict[str, Any]:
    if not isinstance(entry, dict):
        return {"servers": {}}

    servers = entry.get("servers")
    if isinstance(servers, dict):
        normalized_servers: Dict[str, Dict[str, Any]] = {}
        for server_key, server_entry in servers.items():
            if isinstance(server_entry, dict):
                normalized_servers[str(server_key)] = _normalize_server_entry(str(server_key), server_entry)
        return {"servers": normalized_servers}

    server_key = entry.get("server_key") or entry.get("region")
    if isinstance(server_key, str) and server_key:
        return {
            "servers": {
                server_key: _normalize_server_entry(server_key, entry),
            }
        }

    return {"servers": {}}


def get_awg_profile(name: str) -> Dict[str, Any]:
    db = _awg_store().read()
    return _normalize_profile_entry(db.get(name))


def get_awg_servers(name: str) -> Dict[str, Dict[str, Any]]:
    return get_awg_profile(name)["servers"]


def get_awg_server(name: str, server_key: str) -> Optional[Dict[str, Any]]:
    return get_awg_servers(name).get(server_key)


def list_awg_server_keys(name: str) -> List[str]:
    return sorted(get_awg_servers(name).keys())


def upsert_awg_server(name: str, server_key: str, config: str, wg_conf: Optional[str], created_at: str) -> None:
    def mut(db: Dict[str, Any]) -> Dict[str, Any]:
        profile = _normalize_profile_entry(db.get(name))
        servers = dict(profile["servers"])
        servers[server_key] = _normalize_server_entry(
            server_key,
            {
                "config": config,
                "wg_conf": wg_conf,
                "created_at": created_at,
            },
        )
        db[name] = {"servers": servers}
        return db

    _awg_store().update(mut)


def update_awg_server(name: str, server_key: str, server_entry: Dict[str, Any]) -> None:
    def mut(db: Dict[str, Any]) -> Dict[str, Any]:
        profile = _normalize_profile_entry(db.get(name))
        servers = dict(profile["servers"])
        servers[server_key] = _normalize_server_entry(server_key, server_entry)
        db[name] = {"servers": servers}
        return db

    _awg_store().update(mut)


def remove_awg_server(name: str, server_key: str) -> None:
    def mut(db: Dict[str, Any]) -> Dict[str, Any]:
        profile = _normalize_profile_entry(db.get(name))
        servers = dict(profile["servers"])
        servers.pop(server_key, None)
        if servers:
            db[name] = {"servers": servers}
        else:
            db.pop(name, None)
        return db

    _awg_store().update(mut)


def remove_awg_profile(name: str) -> None:
    def mut(db: Dict[str, Any]) -> Dict[str, Any]:
        db.pop(name, None)
        return db

    _awg_store().update(mut)
