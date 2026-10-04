"""Backend-owned monitoring and transition-only administrator notifications."""

import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import grpc

from .announcements import AnnouncementService
from .authorization import AccessDenied, require_permission

DEFAULT_POLICY = {"enabled": False, "interval_minutes": 5, "notify_resolved": True}
RESOURCE_TYPES = {"disk_low", "ram_high", "load_high"}


class AlertService:
    def __init__(self, db, driver=None):
        self.db, self.driver = db, driver

    def initialize_schema(self):
        with self.db.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_alert_state (
                node_key TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                PRIMARY KEY(node_key,kind))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_alert_events (
                id TEXT PRIMARY KEY, node_key TEXT NOT NULL, kind TEXT NOT NULL,
                resolved INTEGER NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_alert_deliveries (
                id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES backend_alert_events(id),
                account_id TEXT NOT NULL, telegram_subject TEXT NOT NULL,
                status TEXT NOT NULL, claim_id TEXT, adapter_id TEXT, claimed_until TEXT,
                UNIQUE(event_id,telegram_subject))""")

    @staticmethod
    def _policy(conn):
        row = conn.execute(
            "SELECT value FROM backend_system_settings WHERE key='alert_policy'"
        ).fetchone()
        return json.loads(row["value"]) if row else dict(DEFAULT_POLICY)

    @staticmethod
    def retire_node(conn, node_key):
        conn.execute(
            "DELETE FROM backend_alert_deliveries WHERE event_id IN (SELECT id FROM backend_alert_events WHERE node_key=?)",
            (node_key,),
        )
        conn.execute("DELETE FROM backend_alert_events WHERE node_key=?", (node_key,))
        conn.execute("DELETE FROM backend_alert_state WHERE node_key=?", (node_key,))

    def preferences(self, actor, changes):
        require_permission(actor, "settings.manage")
        if not changes or set(changes) - set(DEFAULT_POLICY):
            raise AccessDenied("invalid_input", 422)
        for key, value in changes.items():
            if key == "interval_minutes":
                valid = type(value) is int and value in {5, 15}
            else:
                valid = type(value) is bool
            if not valid:
                raise AccessDenied("invalid_input", 422)
        with self.db.transaction() as conn:
            policy = self._policy(conn)
            policy.update(changes)
            conn.execute(
                "INSERT INTO backend_system_settings VALUES ('alert_policy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(policy),),
            )
            if not policy["enabled"]:
                conn.execute(
                    "UPDATE backend_alert_deliveries SET status='skipped' WHERE status='queued'"
                )
        return policy

    def overview(self, actor):
        require_permission(actor, "settings.manage")
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT value FROM backend_system_settings WHERE key='alert_last_scan'"
            ).fetchone()
            state = conn.execute("""SELECT s.node_key,s.kind,s.first_seen_at,s.last_seen_at,n.title,n.flag
                FROM backend_alert_state s JOIN backend_nodes n ON n.key=s.node_key
                WHERE n.enabled=1 ORDER BY s.node_key,s.kind""").fetchall()
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
            for entry in conn.execute(
                "SELECT status,COUNT(*) AS count FROM backend_alert_deliveries GROUP BY status"
            ).fetchall():
                counts[entry["status"]] = entry["count"]
            return {
                **self._policy(conn),
                "active_count": len(state),
                "active": [dict(r) for r in state],
                "last_scan": json.loads(row["value"]) if row else None,
                "delivery_counts": counts,
            }

    @staticmethod
    def classify(observation, protocols):
        """Return failing conditions and checks with a definite measurement.

        Unknown fields never resolve an earlier resource alarm.
        """
        failures, known = {}, {"node_unreachable"}
        for protocol in protocols:
            kind = protocol + "_down"
            field = protocol + "_running"
            if (
                observation.get("inspection_available") is True
                and type(observation.get(field)) is bool
            ):
                known.add(kind)
                if not observation[field]:
                    failures[kind] = {}
        metrics = observation.get("host_metrics") or {}
        if not isinstance(metrics, dict):
            metrics = {}

        def numeric(key, maximum=None):
            value = metrics.get(key)
            return (
                type(value) in {int, float}
                and math.isfinite(value)
                and value >= 0
                and (maximum is None or value <= maximum)
            )

        if numeric("disk_free_percent", 100):
            known.add("disk_low")
            if metrics["disk_free_percent"] < 10:
                failures["disk_low"] = {"value": metrics["disk_free_percent"]}
        if numeric("ram_used_percent", 100):
            known.add("ram_high")
            if metrics["ram_used_percent"] >= 90:
                failures["ram_high"] = {"value": metrics["ram_used_percent"]}
        if (
            numeric("load1")
            and type(metrics.get("cpus")) is int
            and metrics["cpus"] > 0
        ):
            known.add("load_high")
            if metrics["load1"] / metrics["cpus"] >= 2:
                failures["load_high"] = {
                    "value": metrics["load1"],
                    "cpus": metrics["cpus"],
                }
        return failures, known

    def _observe(self, node):
        try:
            return self.classify(
                self.driver.inspect_node_services(node["key"]),
                json.loads(node["protocols_json"]),
            )
        except grpc.RpcError as exc:
            if exc.code() in {
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.DEADLINE_EXCEEDED,
                grpc.StatusCode.FAILED_PRECONDITION,
            }:
                return {"node_unreachable": {}}, {"node_unreachable"}
            return {}, set()
        except Exception:  # noqa: BLE001 -- malformed observations stay unknown
            return {}, set()

    @staticmethod
    def _event(conn, node, kind, payload, resolved, timestamp):
        event_id = str(uuid4())
        conn.execute(
            "INSERT INTO backend_alert_events VALUES (?,?,?,?,?,?)",
            (
                event_id,
                node["key"],
                kind,
                int(resolved),
                json.dumps({"node_title": node["title"], **payload}),
                timestamp,
            ),
        )
        for admin in conn.execute("""SELECT a.id,i.subject FROM backend_accounts a
            JOIN backend_external_identities i ON i.account_id=a.id AND i.provider='telegram'
            WHERE a.role='admin' AND a.status='approved' """).fetchall():
            conn.execute(
                "INSERT INTO backend_alert_deliveries (id,event_id,account_id,telegram_subject,status) VALUES (?,?,?,?,?)",
                (str(uuid4()), event_id, admin["id"], admin["subject"], "queued"),
            )

    def scheduled(self):
        timestamp = datetime.now(timezone.utc)
        with self.db.connect() as conn:
            from .maintenance_gate import active
            if active(conn):
                return False
            policy = self._policy(conn)
            row = conn.execute(
                "SELECT value FROM backend_system_settings WHERE key='alert_last_scan'"
            ).fetchone()
            last = json.loads(row["value"]) if row else None
            if not policy["enabled"] or (
                last
                and datetime.fromisoformat(last["at"])
                > timestamp - timedelta(minutes=policy["interval_minutes"])
            ):
                return False
            if conn.execute(
                "SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running')"
            ).fetchone():
                return False
            nodes = [
                dict(row)
                for row in conn.execute("""SELECT n.* FROM backend_nodes n
                WHERE enabled=1 AND applied_revision>0 AND applied_revision=desired_revision
                AND NOT EXISTS (SELECT 1 FROM backend_node_jobs j WHERE j.node_key=n.key AND j.status IN ('awaiting_executor','running'))
                AND NOT EXISTS (SELECT 1 FROM backend_node_settings_tasks t WHERE t.node_key=n.key AND t.status IN ('awaiting_executor','running'))
                AND NOT EXISTS (SELECT 1 FROM backend_node_drains d WHERE d.node_key=n.key)
                AND NOT EXISTS (SELECT 1 FROM backend_node_cleanup c WHERE c.node_key=n.key)
                AND NOT EXISTS (SELECT 1 FROM backend_node_retirements r WHERE r.node_key=n.key)
                ORDER BY n.key""").fetchall()
            ]
        results, status = [], "success"
        try:
            # Driver failures are control-plane failures, not node outages.
            if nodes:
                self.driver.binary_info()
                with ThreadPoolExecutor(max_workers=min(4, len(nodes))) as pool:
                    results = list(pool.map(self._observe, nodes))
                self.driver.binary_info()
            if any(
                (
                    {
                        "node_unreachable",
                        *RESOURCE_TYPES,
                        *[p + "_down" for p in json.loads(node["protocols_json"])],
                    }
                    - known
                )
                for node, (_, known) in zip(nodes, results)
            ):
                status = "partial"
        except Exception:  # noqa: BLE001 -- persist a sanitized failed scan
            results, status = [], "failed"
        with self.db.transaction() as conn:
            # Serialize monitor commits against policy changes/restore admission.
            conn.execute(
                "UPDATE backend_account_guard SET revision=revision+1 WHERE id=1"
            )
            from .maintenance_gate import active
            if active(conn):
                return False
            policy = self._policy(conn)
            if (
                not policy["enabled"]
                or conn.execute(
                    "SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running')"
                ).fetchone()
            ):
                return False
            for node, (failures, known) in zip(nodes, results):
                current = conn.execute(
                    "SELECT enabled,desired_revision,applied_revision FROM backend_nodes WHERE key=?",
                    (node["key"],),
                ).fetchone()
                if (
                    not current
                    or not current["enabled"]
                    or current["desired_revision"] != node["desired_revision"]
                    or current["applied_revision"] != node["applied_revision"]
                ):
                    continue
                previous = {
                    row["kind"]: row
                    for row in conn.execute(
                        "SELECT * FROM backend_alert_state WHERE node_key=?",
                        (node["key"],),
                    ).fetchall()
                }
                for kind, payload in failures.items():
                    if kind not in previous:
                        self._event(
                            conn, node, kind, payload, False, timestamp.isoformat()
                        )
                    conn.execute(
                        "INSERT INTO backend_alert_state VALUES (?,?,?,?,?) ON CONFLICT(node_key,kind) DO UPDATE SET payload_json=excluded.payload_json,last_seen_at=excluded.last_seen_at",
                        (
                            node["key"],
                            kind,
                            json.dumps(payload),
                            timestamp.isoformat(),
                            timestamp.isoformat(),
                        ),
                    )
                for kind, row in previous.items():
                    if kind in {"xray_down", "awg_down"} and kind.removesuffix(
                        "_down"
                    ) not in json.loads(node["protocols_json"]):
                        conn.execute(
                            "DELETE FROM backend_alert_state WHERE node_key=? AND kind=?",
                            (node["key"], kind),
                        )
                        continue
                    if kind in known and kind not in failures:
                        if policy["notify_resolved"]:
                            self._event(
                                conn,
                                node,
                                kind,
                                json.loads(row["payload_json"]),
                                True,
                                timestamp.isoformat(),
                            )
                        conn.execute(
                            "DELETE FROM backend_alert_state WHERE node_key=? AND kind=?",
                            (node["key"], kind),
                        )
            conn.execute("""DELETE FROM backend_alert_state WHERE NOT EXISTS
                (SELECT 1 FROM backend_nodes n WHERE n.key=backend_alert_state.node_key AND n.enabled=1)""")
            conn.execute(
                "INSERT INTO backend_system_settings VALUES ('alert_last_scan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    json.dumps(
                        {
                            "at": timestamp.isoformat(),
                            "status": status,
                            "nodes_checked": len(results),
                        }
                    ),
                ),
            )
        return True

    def claim(self, principal, key):
        AnnouncementService._transport(principal)
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
            from .maintenance_gate import active
            if active(conn):
                return False
            conn.execute(
                "UPDATE backend_alert_deliveries SET status='unknown' WHERE status='claimed' AND claimed_until<=?",
                (timestamp.isoformat(),),
            )
            if not self._policy(conn)["enabled"]:
                return None
            prior = conn.execute(
                "SELECT * FROM backend_alert_deliveries WHERE claim_id=? AND adapter_id=?",
                (key, principal.id),
            ).fetchone()
            if prior:
                return (
                    self._delivery(conn, prior)
                    if prior["status"] == "claimed"
                    else None
                )
            for row in conn.execute(
                "SELECT d.* FROM backend_alert_deliveries d JOIN backend_alert_events e ON e.id=d.event_id WHERE d.status='queued' ORDER BY e.created_at,e.id,d.id LIMIT 100"
            ).fetchall():
                admin = conn.execute(
                    """SELECT 1 FROM backend_accounts a JOIN backend_external_identities i ON i.account_id=a.id AND i.provider='telegram'
                    WHERE a.id=? AND a.role='admin' AND a.status='approved' AND i.subject=?""",
                    (row["account_id"], row["telegram_subject"]),
                ).fetchone()
                node = conn.execute(
                    """SELECT 1 FROM backend_nodes n JOIN backend_alert_events e ON e.node_key=n.key
                    WHERE e.id=? AND n.enabled=1
                    AND NOT EXISTS (SELECT 1 FROM backend_node_drains d WHERE d.node_key=n.key)
                    AND NOT EXISTS (SELECT 1 FROM backend_node_cleanup c WHERE c.node_key=n.key)""",
                    (row["event_id"],),
                ).fetchone()
                if not admin or not node:
                    conn.execute(
                        "UPDATE backend_alert_deliveries SET status='skipped' WHERE id=?",
                        (row["id"],),
                    )
                    continue
                conn.execute(
                    "UPDATE backend_alert_deliveries SET status='claimed',claim_id=?,adapter_id=?,claimed_until=? WHERE id=?",
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
        event = dict(
            conn.execute(
                "SELECT * FROM backend_alert_events WHERE id=?", (row["event_id"],)
            ).fetchone()
        )
        details = conn.execute(
            "SELECT locale,language_code FROM backend_telegram_identity_details WHERE subject=?",
            (row["telegram_subject"],),
        ).fetchone()
        return {
            "id": row["id"],
            "telegram_user_id": int(row["telegram_subject"]),
            "locale": (details["locale"] or details["language_code"])
            if details
            else None,
            "event": {
                "node_key": event["node_key"],
                "kind": event["kind"],
                "resolved": bool(event["resolved"]),
                "at": event["created_at"],
                "payload": json.loads(event["payload_json"]),
            },
        }

    def acknowledge(self, principal, delivery_id, key, status):
        AnnouncementService._transport(principal)
        if status not in {"sent", "failed", "unknown"}:
            raise AccessDenied("invalid_input", 422)
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM backend_alert_deliveries WHERE id=? AND adapter_id=? AND claim_id=?",
                (delivery_id, principal.id, key),
            ).fetchone()
            if not row:
                raise AccessDenied("resource_not_found", 404)
            if row["status"] != "claimed" and row["status"] != status:
                raise AccessDenied("delivery_finalized", 409)
            conn.execute(
                "UPDATE backend_alert_deliveries SET status=? WHERE id=?",
                (status, delivery_id),
            )
        return {"status": status}
