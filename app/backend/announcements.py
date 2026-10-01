"""Durable announcements; recipient policy belongs to the backend.

Delivery claims are never replayed after uncertainty. Telegram cannot provide
idempotent send operations, so expired claims are reported as unknown.
"""

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from .authorization import AccessDenied, PrincipalKind, require_permission


class AnnouncementService:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        with self.db.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_announcements (
                id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, command_key TEXT NOT NULL,
                text TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(actor_id,command_key))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_announcement_deliveries (
                id TEXT PRIMARY KEY, announcement_id TEXT NOT NULL REFERENCES backend_announcements(id),
                account_id TEXT NOT NULL, telegram_subject TEXT NOT NULL,
                status TEXT NOT NULL, claim_id TEXT, adapter_id TEXT, claimed_until TEXT,
                UNIQUE(announcement_id,telegram_subject))""")

    @staticmethod
    def validate_text(text):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 3000:
            raise AccessDenied("invalid_input", 422)
        return text.strip()

    def preview(self, actor, text):
        require_permission(actor, "settings.manage")
        text = self.validate_text(text)
        with self.db.connect() as conn:
            recipients = self._recipients(conn, actor.account.id)
        return {"text": text, "recipients": len(recipients)}

    @staticmethod
    def _recipients(conn, sender):
        return conn.execute(
            """SELECT a.id,i.subject FROM backend_accounts a
            JOIN backend_external_identities i ON i.account_id=a.id AND i.provider='telegram'
            WHERE a.status='approved' AND a.id<>? ORDER BY a.id,i.subject""",
            (sender,),
        ).fetchall()

    def queue(self, actor, text, key):
        require_permission(actor, "settings.manage")
        text = self.validate_text(text)
        try:
            key = str(UUID(key))
        except (ValueError, TypeError, AttributeError):
            raise AccessDenied("invalid_idempotency_key", 422) from None
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            conn.execute(
                "UPDATE backend_account_guard SET revision=revision+1 WHERE id=1"
            )
            sender = conn.execute(
                "SELECT role,status FROM backend_accounts WHERE id=?",
                (actor.account.id,),
            ).fetchone()
            if (
                not sender
                or sender["role"] != "admin"
                or sender["status"] != "approved"
            ):
                raise AccessDenied("permission_denied")
            previous = conn.execute(
                "SELECT id,text FROM backend_announcements WHERE actor_id=? AND command_key=?",
                (actor.account.id, key),
            ).fetchone()
            if previous:
                if previous["text"] != text:
                    raise AccessDenied("idempotency_conflict", 409)
                announcement_id = previous["id"]
            else:
                announcement_id = str(uuid4())
                conn.execute(
                    "INSERT INTO backend_announcements VALUES (?,?,?,?,?)",
                    (
                        announcement_id,
                        actor.account.id,
                        key,
                        text,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                for recipient in self._recipients(conn, actor.account.id):
                    conn.execute(
                        "INSERT INTO backend_announcement_deliveries (id,announcement_id,account_id,telegram_subject,status) VALUES (?,?,?,?,?)",
                        (
                            str(uuid4()),
                            announcement_id,
                            recipient["id"],
                            recipient["subject"],
                            "queued",
                        ),
                    )
        return self.get(actor, announcement_id)

    @staticmethod
    def _expire(conn, now):
        conn.execute(
            "UPDATE backend_announcement_deliveries SET status='unknown' WHERE status='claimed' AND claimed_until<=?",
            (now.isoformat(),),
        )

    def get(self, actor, announcement_id):
        require_permission(actor, "settings.manage")
        with self.db.transaction() as conn:
            self._expire(conn, datetime.now(timezone.utc))
            record = conn.execute(
                "SELECT id FROM backend_announcements WHERE id=?", (announcement_id,)
            ).fetchone()
            if not record:
                raise AccessDenied("resource_not_found", 404)
            counts = {
                status: 0
                for status in (
                    "queued",
                    "claimed",
                    "sent",
                    "failed",
                    "unknown",
                    "skipped",
                )
            }
            for row in conn.execute(
                "SELECT status,COUNT(*) AS count FROM backend_announcement_deliveries WHERE announcement_id=? GROUP BY status",
                (announcement_id,),
            ).fetchall():
                counts[row["status"]] = row["count"]
        return {
            "id": announcement_id,
            "counts": counts,
            "total": sum(counts.values()),
            "status": "running"
            if counts["queued"] or counts["claimed"]
            else "completed",
        }

    def latest(self, actor):
        require_permission(actor, "settings.manage")
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM backend_announcements ORDER BY created_at DESC,id DESC LIMIT 1"
            ).fetchone()
        return {"last_job": self.get(actor, row["id"]) if row else None}

    @staticmethod
    def _transport(principal):
        # Only a trusted Telegram adapter can claim transport work. Human roles
        # are checked when admitting the immutable announcement, not by delivery.
        if (
            principal.kind != PrincipalKind.ADAPTER
            or "settings.manage" not in principal.scopes
        ):
            raise AccessDenied("permission_denied")

    def claim(self, principal, key):
        self._transport(principal)
        try:
            key = str(UUID(key))
        except (ValueError, TypeError, AttributeError):
            raise AccessDenied("invalid_idempotency_key", 422) from None
        timestamp = datetime.now(timezone.utc)
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
            from .maintenance_gate import active
            if active(conn):
                return None
            self._expire(conn, timestamp)
            previous = conn.execute(
                "SELECT * FROM backend_announcement_deliveries WHERE claim_id=? AND adapter_id=?",
                (key, principal.id),
            ).fetchone()
            if previous:
                # Lost claim response cannot cause duplicate work: a caller may
                # repeat only the initial claim, never a completed send.
                return (
                    self._delivery(conn, previous)
                    if previous["status"] == "claimed"
                    else None
                )
            if conn.execute(
                "SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running')"
            ).fetchone():
                return None
            for row in conn.execute(
                "SELECT * FROM backend_announcement_deliveries WHERE status='queued' ORDER BY announcement_id,id"
            ).fetchall():
                eligible = conn.execute(
                    """SELECT 1 FROM backend_accounts a
                    JOIN backend_external_identities i ON i.account_id=a.id AND i.provider='telegram'
                    JOIN backend_announcements c ON c.id=?
                    JOIN backend_accounts sender ON sender.id=c.actor_id
                    WHERE a.id=? AND a.status='approved' AND i.subject=?
                    AND sender.status='approved' AND sender.role='admin' """,
                    (
                        row["announcement_id"],
                        row["account_id"],
                        row["telegram_subject"],
                    ),
                ).fetchone()
                if not eligible:
                    conn.execute(
                        "UPDATE backend_announcement_deliveries SET status='skipped' WHERE id=?",
                        (row["id"],),
                    )
                    continue
                conn.execute(
                    "UPDATE backend_announcement_deliveries SET status='claimed',claim_id=?,adapter_id=?,claimed_until=? WHERE id=?",
                    (
                        key,
                        principal.id,
                        (timestamp + timedelta(minutes=2)).isoformat(),
                        row["id"],
                    ),
                )
                return self._delivery(conn, row)
        return None

    @staticmethod
    def _delivery(conn, row):
        text = conn.execute(
            "SELECT text FROM backend_announcements WHERE id=?",
            (row["announcement_id"],),
        ).fetchone()["text"]
        silent = conn.execute(
            "SELECT value FROM backend_system_settings WHERE key=?",
            ("announcement_silent:" + row["account_id"],),
        ).fetchone()
        details = conn.execute(
            "SELECT locale,language_code FROM backend_telegram_identity_details WHERE subject=?",
            (row["telegram_subject"],),
        ).fetchone()
        return {
            "id": row["id"],
            "telegram_user_id": int(row["telegram_subject"]),
            "text": text,
            "locale": (details["locale"] or details["language_code"])
            if details
            else None,
            "silent": bool(json.loads(silent["value"])) if silent else False,
        }

    def acknowledge(self, principal, delivery_id, key, status):
        self._transport(principal)
        if status not in {"sent", "failed", "unknown"}:
            raise AccessDenied("invalid_input", 422)
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM backend_announcement_deliveries WHERE id=? AND adapter_id=? AND claim_id=?",
                (delivery_id, principal.id, key),
            ).fetchone()
            if not row:
                raise AccessDenied("resource_not_found", 404)
            if row["status"] != "claimed":
                if row["status"] != status:
                    raise AccessDenied("delivery_finalized", 409)
            else:
                conn.execute(
                    "UPDATE backend_announcement_deliveries SET status=? WHERE id=?",
                    (status, delivery_id),
                )
        return {"status": status}
