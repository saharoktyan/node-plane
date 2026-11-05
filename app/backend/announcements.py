"""Durable announcements; recipient policy belongs to the backend.

Delivery claims are never replayed after uncertainty. Telegram cannot provide
idempotent send operations, so expired claims are reported as unknown.
"""

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from .authorization import AccessDenied, PrincipalKind, require_permission


# Accounts and Telegram identities are retained for audit and reapproval. They
# are not recipients unless they still own a profile visible in the bot.
RECIPIENT_PROFILE = """EXISTS (SELECT 1 FROM backend_profiles p
    WHERE p.owner_account_id = a.id AND NOT EXISTS
        (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id = p.id))"""


from .service_notifications import SYSTEM_ACTOR

# Delivery states visible in the Inbox; `inbox_only` was muted by the recipient.
INBOX_STATUSES = "('sent','inbox_only')"


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
            from app.db.migration_revisions.r0006_announcement_inbox import upgrade
            upgrade(conn)
            from app.db.migration_revisions.r0012_system_notifications import upgrade as system_notifications
            system_notifications(conn)
            if getattr(self.db, 'backend_name', '') == 'postgres':
                from app.db.migration_revisions.r0016_service_notifications import upgrade as service_notifications
                service_notifications(conn)
            elif not any(column['name'] == 'dedup_key' for column in
                         conn.execute('PRAGMA table_info(backend_system_notifications)').fetchall()):
                # Unit databases cannot drop NOT NULL in place; rebuild the empty test table.
                conn.execute('DROP TABLE backend_system_notifications')
                conn.execute("""CREATE TABLE backend_system_notifications (
                    announcement_id TEXT PRIMARY KEY REFERENCES backend_announcements(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL, profile_id TEXT NOT NULL, expires_at TEXT,
                    service_title TEXT NOT NULL, dedup_key TEXT UNIQUE, device_id TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(kind,profile_id,expires_at))""")

    @staticmethod
    def queue_expired_access(conn, timestamp):
        """One notification per deadline, admitted with the expiry transaction."""
        from .service_notifications import queue
        rows = conn.execute("""SELECT p.id,p.expires_at FROM backend_profiles p
            JOIN backend_accounts a ON a.id=p.owner_account_id
            WHERE a.status='approved'
            AND EXISTS (SELECT 1 FROM backend_external_identities i WHERE i.account_id=a.id AND i.provider='telegram')
            AND p.expires_at IS NOT NULL AND p.expires_at<=?
            AND NOT EXISTS (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id=p.id)
            AND NOT EXISTS (SELECT 1 FROM backend_system_notifications s
                WHERE s.kind='access_expired' AND s.profile_id=p.id AND s.expires_at=p.expires_at)
            ORDER BY p.expires_at,p.id LIMIT 100""", (timestamp.isoformat(),)).fetchall()
        for row in rows:
            if datetime.fromisoformat(row['expires_at']) > timestamp:
                continue
            queue(conn, kind='access_expired', profile_id=row['id'], expires_at=row['expires_at'],
                  dedup_key=f"access_expired:{row['id']}:{row['expires_at']}", timestamp=timestamp)

    @staticmethod
    def notification_metadata(conn, announcement_id):
        system = conn.execute('SELECT kind,service_title,payload_json FROM backend_system_notifications WHERE announcement_id=?',
                              (announcement_id,)).fetchone()
        if system:
            return {'notification_kind':system['kind'], 'service_title':system['service_title'],
                    'payload':json.loads(system['payload_json'] or '{}'),
                    'sender_name':'System', 'sender_username':None}
        return {'notification_kind':'announcement',
                'sender_username':AnnouncementService.sender_username(conn, announcement_id)}

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
            f"""SELECT a.id,i.subject FROM backend_accounts a
            JOIN backend_external_identities i ON i.account_id=a.id AND i.provider='telegram'
            WHERE a.status='approved' AND a.id<>? AND {RECIPIENT_PROFILE}
            ORDER BY a.id,i.subject""",
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
                    "inbox_only",
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
                "SELECT id FROM backend_announcements WHERE actor_id<>? ORDER BY created_at DESC,id DESC LIMIT 1", (SYSTEM_ACTOR,)
            ).fetchone()
        return {"last_job": self.get(actor, row["id"]) if row else None}

    @staticmethod
    def sender_username(conn, announcement_id):
        row = conn.execute("""SELECT d.username FROM backend_announcements a
            JOIN backend_external_identities i ON i.account_id=a.actor_id AND i.provider='telegram'
            JOIN backend_telegram_identity_details d ON d.subject=i.subject
            WHERE a.id=? ORDER BY i.subject LIMIT 1""", (announcement_id,)).fetchone()
        return row['username'] if row and row['username'] else None

    def inbox(self, actor, offset=0, limit=10):
        require_permission(actor, "account.self.read")
        limit = max(1, min(int(limit), 50))
        with self.db.connect() as conn:
            total = conn.execute(f"""SELECT COUNT(*) AS count
                FROM backend_announcement_deliveries d
                WHERE d.account_id=? AND d.status IN {INBOX_STATUSES}
                AND NOT EXISTS (SELECT 1 FROM backend_announcement_user_state x
                    WHERE x.delivery_id=d.id AND x.account_id=d.account_id AND x.dismissed_at IS NOT NULL)""",
                (actor.account.id,)).fetchone()["count"]
            offset = min(max(0, int(offset)), max(0, total - 1))
            offset = offset // limit * limit
            rows = conn.execute(f"""SELECT d.id,a.id AS announcement_id,a.text,a.created_at,s.viewed_at
                FROM backend_announcement_deliveries d
                JOIN backend_announcements a ON a.id=d.announcement_id
                LEFT JOIN backend_announcement_user_state s ON s.delivery_id=d.id AND s.account_id=d.account_id
                WHERE d.account_id=? AND d.status IN {INBOX_STATUSES}
                AND (s.dismissed_at IS NULL)
                ORDER BY a.created_at DESC,a.id DESC LIMIT ? OFFSET ?""",
                (actor.account.id, limit, offset)).fetchall()
            items = [{**dict(row), **self.notification_metadata(conn,row["announcement_id"])} for row in rows]
        return {"items": items, "total": total,
            "offset": offset, "page_size": limit}

    def inbox_item(self, actor, delivery_id):
        require_permission(actor, "account.self.read")
        viewed_at = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            row = conn.execute(f"""SELECT d.id,a.id AS announcement_id,a.text,a.created_at
                FROM backend_announcement_deliveries d
                JOIN backend_announcements a ON a.id=d.announcement_id
                WHERE d.id=? AND d.account_id=? AND d.status IN {INBOX_STATUSES}
                AND NOT EXISTS (SELECT 1 FROM backend_announcement_user_state x
                    WHERE x.delivery_id=d.id AND x.account_id=d.account_id AND x.dismissed_at IS NOT NULL)""",
                (delivery_id, actor.account.id)).fetchone()
            if row is None:
                raise AccessDenied("resource_not_found", 404)
            conn.execute("""INSERT INTO backend_announcement_user_state
                (delivery_id,account_id,viewed_at) VALUES (?,?,?)
                ON CONFLICT(delivery_id) DO UPDATE SET viewed_at=excluded.viewed_at""",
                (delivery_id, actor.account.id, viewed_at))
            result = dict(row)
            result["viewed_at"] = viewed_at
            result.update(self.notification_metadata(conn,row["announcement_id"]))
        return result

    def dismiss_inbox_item(self, actor, delivery_id):
        require_permission(actor, "account.self.read")
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            row = conn.execute(f"""SELECT id FROM backend_announcement_deliveries
                WHERE id=? AND account_id=? AND status IN {INBOX_STATUSES}""",
                (delivery_id, actor.account.id)).fetchone()
            if row is None:
                raise AccessDenied("resource_not_found", 404)
            conn.execute("""INSERT INTO backend_announcement_user_state
                (delivery_id,account_id,dismissed_at) VALUES (?,?,?)
                ON CONFLICT(delivery_id) DO UPDATE SET dismissed_at=excluded.dismissed_at""",
                (delivery_id, actor.account.id, timestamp))
        return {"dismissed": True}

    @staticmethod
    def _transport(principal):
        # Only a trusted Telegram adapter can claim transport work. Human roles
        # are checked when admitting the immutable announcement, not by delivery.
        if (
            principal.kind != PrincipalKind.ADAPTER
            or "settings.manage" not in principal.scopes
        ):
            raise AccessDenied("permission_denied")

    @staticmethod
    def _eligible(conn, row):
        system = conn.execute('SELECT * FROM backend_system_notifications WHERE announcement_id=?',
                              (row['announcement_id'],)).fetchone()
        if system:
            from .service_notifications import current
            return current(conn, system, row)
        return conn.execute(
            f"""SELECT 1 FROM backend_accounts a
            JOIN backend_external_identities i ON i.account_id=a.id AND i.provider='telegram'
            JOIN backend_announcements c ON c.id=?
            JOIN backend_accounts sender ON sender.id=c.actor_id
            WHERE a.id=? AND a.status='approved' AND i.subject=?
            AND sender.status='approved' AND sender.role='admin'
            AND a.id<>sender.id AND {RECIPIENT_PROFILE} """,
            (row["announcement_id"], row["account_id"], row["telegram_subject"]),
        ).fetchone()

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
            from .telegram_delivery_retry import remaining
            if remaining(conn, principal, timestamp):
                return None
            self._expire(conn, timestamp)
            if conn.execute(
                "SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running')"
            ).fetchone():
                return None
            previous = conn.execute(
                "SELECT * FROM backend_announcement_deliveries WHERE claim_id=? AND adapter_id=?",
                (key, principal.id),
            ).fetchone()
            if previous:
                # Lost claim response cannot cause duplicate work: a caller may
                # repeat only the initial claim, never a completed send.
                if previous["status"] != "claimed":
                    return None
                if not self._eligible(conn, previous):
                    # The earlier claim may already have reached Telegram.
                    # Preserve that uncertainty instead of marking it unsent.
                    conn.execute(
                        "UPDATE backend_announcement_deliveries SET status='unknown' WHERE id=?",
                        (previous["id"],),
                    )
                    return None
                return self._delivery(conn, previous)
            for row in conn.execute(
                "SELECT * FROM backend_announcement_deliveries WHERE status='queued' ORDER BY announcement_id,id"
            ).fetchall():
                if not self._eligible(conn, row):
                    conn.execute(
                        "UPDATE backend_announcement_deliveries SET status='skipped' WHERE id=?",
                        (row["id"],),
                    )
                    continue
                if self._service_mode(conn, row) == 'off':
                    conn.execute(
                        "UPDATE backend_announcement_deliveries SET status='inbox_only' WHERE id=?",
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
    def _service_mode(conn, row):
        """Recipient's service notification mode, or None for administrator announcements."""
        if not conn.execute('SELECT 1 FROM backend_system_notifications WHERE announcement_id=?',
                            (row['announcement_id'],)).fetchone():
            return None
        from .service_notifications import mode
        return mode(conn, row['account_id'])

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
        service = AnnouncementService._service_mode(conn, row)
        account = conn.execute(
            "SELECT locale FROM backend_accounts WHERE id=?", (row["account_id"],)
        ).fetchone()
        details = conn.execute(
            "SELECT locale,language_code FROM backend_telegram_identity_details WHERE subject=?",
            (row["telegram_subject"],),
        ).fetchone()
        return {
            "id": row["id"],
            "telegram_user_id": int(row["telegram_subject"]),
            "text": text,
            "locale": (account["locale"] if account and account["locale"] else None)
            or ((details["locale"] or details["language_code"]) if details else None),
            "silent": (service == 'silent') if service else
                (bool(json.loads(silent["value"])) if silent else False),
            **AnnouncementService.notification_metadata(conn,row["announcement_id"]),
        }

    def acknowledge(self, principal, delivery_id, key, status, retry_after=None):
        self._transport(principal)
        from .telegram_delivery_retry import validate_ack, defer
        validate_ack(status, retry_after)
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM backend_announcement_deliveries WHERE id=? AND adapter_id=? AND claim_id=?",
                (delivery_id, principal.id, key),
            ).fetchone()
            if not row:
                raise AccessDenied("resource_not_found", 404)
            if status == 'retry':
                if row['status'] == 'claimed':
                    defer(conn, 'backend_announcement_deliveries', delivery_id, principal, retry_after)
                elif row['status'] != 'queued':
                    raise AccessDenied('delivery_finalized', 409)
                return {'status': 'retry'}
            if row["status"] != "claimed":
                if row["status"] != status:
                    raise AccessDenied("delivery_finalized", 409)
            else:
                conn.execute(
                    "UPDATE backend_announcement_deliveries SET status=? WHERE id=?",
                    (status, delivery_id),
                )
        return {"status": status}
