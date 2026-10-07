"""Backend-owned system policy and account preferences for every client."""

from __future__ import annotations

import json
from uuid import uuid4

from .authorization import AccessDenied, require_permission


class SystemSettingsService:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        with self.db.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""")
            # Retire member opt-ins; the installation policy now covers every profile.
            conn.execute("DELETE FROM backend_system_settings WHERE key LIKE 'traffic_consent:%' OR key LIKE 'traffic_consent_generation:%'")

    def member_preferences(self, actor):
        require_permission(actor, "account.self.read")
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT value FROM backend_system_settings WHERE key=?",
                ("announcement_silent:" + actor.account.id,),
            ).fetchone()
        with self.db.connect() as conn:
            enabled = conn.execute(
                "SELECT value FROM backend_system_settings WHERE key='traffic_enabled'"
            ).fetchone()
        return {
            "announcement_silent": bool(json.loads(row["value"])) if row else False,
            "traffic_available": json.loads(enabled["value"]) is True
            if enabled
            else False,
        }

    def traffic_policy(self, actor):
        require_permission(actor, "settings.manage")
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT value FROM backend_system_settings WHERE key='traffic_enabled'"
            ).fetchone()
            scan = conn.execute(
                "SELECT value FROM backend_system_settings WHERE key='traffic_last_scan'"
            ).fetchone()
        return {
            "enabled": json.loads(row["value"]) is True if row else False,
            "collection_status": "ready",
            "interval_minutes": 5,
            "last_scan": json.loads(scan["value"]) if scan else None,
        }

    def update_traffic_policy(self, actor, enabled):
        require_permission(actor, "settings.manage")
        self._store_boolean(actor, "traffic_enabled", enabled)
        return self.traffic_policy(actor)

    def _store_boolean(self, actor, key, value):
        if type(value) is not bool:
            raise AccessDenied("invalid_input", 422)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            conn.execute(
                "UPDATE backend_account_guard SET revision=revision+1 WHERE id=1"
            )
            account = conn.execute(
                "SELECT role, status FROM backend_accounts WHERE id=?",
                (actor.account.id,),
            ).fetchone()
            if not account or account["role"] != "admin" or account["status"] != "approved":
                raise AccessDenied("permission_denied", 403)
            previous = conn.execute(
                "SELECT value FROM backend_system_settings WHERE key=?", (key,)
            ).fetchone()
            if previous and json.loads(previous["value"]) is value:
                return
            conn.execute(
                "INSERT INTO backend_system_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            generation = "traffic_generation"
            conn.execute(
                "INSERT INTO backend_system_settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (generation, json.dumps(str(uuid4()))),
            )
            conn.execute(
                "DELETE FROM backend_system_settings WHERE key='traffic_last_scan'"
            )
            # Pausing collection keeps totals but excludes the paused period.
            conn.execute("""UPDATE backend_traffic_usage SET epoch=NULL,identity=NULL,
                last_uplink=NULL,last_downlink=NULL,status='paused' """)
            conn.execute('''UPDATE backend_traffic_peers SET epoch=NULL,identity=NULL,
                last_uplink=NULL,last_downlink=NULL,status='paused' ''')

    def update_member_preferences(self, actor, silent):
        require_permission(actor, "account.self.preferences.write")
        if type(silent) is not bool:
            raise AccessDenied("invalid_input", 422)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            conn.execute(
                "INSERT INTO backend_system_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("announcement_silent:" + actor.account.id, json.dumps(silent)),
            )

    def bot_title(self, actor):
        require_permission(actor, "account.self.read")
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT value FROM backend_system_settings WHERE key = 'bot_menu_title'"
            ).fetchone()
        return {"title": row["value"] if row and row["value"].strip() else "Node Plane"}

    def update_bot_title(self, actor, title):
        require_permission(actor, "settings.manage")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 64:
            raise AccessDenied("invalid_input", 422)
        normalized = title.strip()
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            conn.execute(
                """INSERT INTO backend_system_settings(key, value) VALUES ('bot_menu_title', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (normalized,),
            )
        return {"title": normalized}
