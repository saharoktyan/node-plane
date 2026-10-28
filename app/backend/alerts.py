"""Backend monitoring, durable incidents and independently timed reminders."""

import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import grpc

from .announcements import AnnouncementService
from .host_resources import (
    cpu_metrics,
    CHECK_SECONDS,
    CPU_WARNING,
    CPU_CRITICAL,
    CPU_RECOVERED,
    LOAD_TRIGGER,
    LOAD_RECOVERED,
    RAM_TRIGGER,
    RAM_RECOVERED,
    DISK_TRIGGER,
    DISK_RECOVERED,
    REPEAT_CHOICES,
    DEFAULT_REPEAT_MINUTES,
)
from .authorization import AccessDenied, require_permission
from .nodes import AGENT_INSTALLED_SQL

DEFAULT_POLICY = {
    "enabled": False,
    "interval_minutes": 5,
    "notify_resolved": True,
    "repeat_minutes": DEFAULT_REPEAT_MINUTES,
}
RESOURCE_TYPES = {"disk_low", "ram_high", "load_high", "cpu_high"}
FAST_KINDS = {"cpu_high", "ram_high", "load_high"}
MONITORABLE_NODE_SQL = AGENT_INSTALLED_SQL + """
    AND NOT EXISTS (SELECT 1 FROM backend_node_drains d WHERE d.node_key=n.key)
    AND NOT EXISTS (SELECT 1 FROM backend_node_cleanup c WHERE c.node_key=n.key)
    AND NOT EXISTS (SELECT 1 FROM backend_node_retirements r WHERE r.node_key=n.key)"""


def _clean_payload(payload):
    if not isinstance(payload, dict):
        return {}
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def is_healthy_observation(kind, failures, known, measured):
    if kind not in known or kind in failures:
        return False
    if kind == "cpu_high":
        return "cpu_below_recovery" in known
    if kind == "load_high":
        m = measured.get("load_high")
        if not m or "value" not in m or "cpus" not in m or m["cpus"] <= 0:
            return False
        return (m["value"] / m["cpus"]) < LOAD_RECOVERED
    if kind == "ram_high":
        m = measured.get("ram_high")
        if not m or "value" not in m:
            return False
        return m["value"] < RAM_RECOVERED
    if kind == "disk_low":
        m = measured.get("disk_low")
        if not m or "value" not in m:
            return False
        return m["value"] >= DISK_RECOVERED
    return True


def monitored_protocols(node):
    return (json.loads(node['protocols_json']) if node['enabled']
            and node['applied_revision'] > 0 and node['applied_revision'] == node['desired_revision'] else [])


class AlertService:
    def __init__(self, db, driver=None, stop=None):
        self.db, self.driver, self.stop = db, driver, stop

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
        policy = dict(DEFAULT_POLICY)
        if row:
            policy.update(json.loads(row["value"]))
        if policy.get("interval_minutes") == 15:
            policy["interval_minutes"] = 10
        return policy

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
                valid = type(value) is int and value in {3, 5, 10}
            elif key == "repeat_minutes":
                valid = type(value) is int and value in set(REPEAT_CHOICES)
            else:
                valid = type(value) is bool
            if not valid:
                raise AccessDenied("invalid_input", 422)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_account_guard SET revision=revision+1 WHERE id=1")
            policy = self._policy(conn)
            policy.update(changes)
            conn.execute(
                "INSERT INTO backend_system_settings VALUES ('alert_policy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(policy),),
            )
            if not policy["enabled"]:
                conn.execute(
                    """UPDATE backend_alert_deliveries SET status='skipped' WHERE status='queued'
                    AND event_id IN (SELECT id FROM backend_alert_events WHERE kind!='update_available')"""
                )
        return policy

    def overview(self, actor):
        require_permission(actor, "settings.manage")
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT value FROM backend_system_settings WHERE key='alert_last_scan'"
            ).fetchone()
            state = self.active(conn, actor.account.id)
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
                """SELECT d.status,COUNT(*) AS count FROM backend_alert_deliveries d
                JOIN backend_alert_events e ON e.id=d.event_id WHERE e.kind!='update_available' GROUP BY d.status"""
            ).fetchall():
                counts[entry["status"]] = entry["count"]
            cpu_scan = conn.execute("SELECT value FROM backend_system_settings WHERE key='alert_last_cpu_scan'").fetchone()
            return {
                **self._policy(conn),
                "active_count": len(state),
                "unacknowledged_count": sum(not r['dismissed'] for r in state),
                "active": [dict(r) for r in state],
                "last_scan": json.loads(row["value"]) if row else None,
                "last_cpu_scan": json.loads(cpu_scan["value"]) if cpu_scan else None,
                "cpu_sample_seconds": 5, "cpu_window_seconds": 60, "cpu_check_seconds": CHECK_SECONDS,
                "delivery_counts": counts,
            }

    @staticmethod
    def active(conn, account_id):
        saved = conn.execute('SELECT value FROM backend_system_settings WHERE key=?',
            ('alerts.dismissed.' + account_id,)).fetchone()
        dismissed = set(json.loads(saved['value'])) if saved else set()
        rows = conn.execute(f'''SELECT s.node_key,s.kind,s.first_seen_at,s.last_seen_at,s.payload_json,n.title,n.flag,
            (SELECT e.id FROM backend_alert_events e WHERE e.node_key=s.node_key
             AND e.kind=s.kind AND e.resolved=0 ORDER BY e.created_at DESC,e.id DESC LIMIT 1) AS event_id
            FROM backend_alert_state s JOIN backend_nodes n ON n.key=s.node_key
            WHERE {MONITORABLE_NODE_SQL}
            AND (s.kind NOT IN ('awg_down','xray_down') OR
                (n.enabled=1 AND n.applied_revision>0 AND n.applied_revision=n.desired_revision
                AND ((s.kind='awg_down' AND n.protocols_json LIKE '%"awg"%')
                    OR (s.kind='xray_down' AND n.protocols_json LIKE '%"xray"%'))))
            ORDER BY s.node_key,s.kind''').fetchall()
        return [{**{key:row[key] for key in row.keys() if key != 'payload_json'}, 'payload':_clean_payload(json.loads(row['payload_json'])), 'dismissed': row['event_id'] in dismissed} for row in rows]

    def dismiss(self, actor, event_id):
        require_permission(actor, 'settings.manage')
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_account_guard SET revision=revision WHERE id=1')
            active = self.active(conn, actor.account.id)
            if not any(r['event_id'] == event_id for r in active):
                raise AccessDenied('alert_not_active', 409)
            dismissed = {r['event_id'] for r in active if r['dismissed']}
            dismissed.add(event_id)
            conn.execute('INSERT INTO backend_system_settings(key,value) VALUES (?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('alerts.dismissed.' + actor.account.id, json.dumps(sorted(dismissed))))
        return {'dismissed_event_id': event_id}

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
            if metrics["disk_free_percent"] < DISK_TRIGGER:
                failures["disk_low"] = {"value": metrics["disk_free_percent"]}
        if numeric("ram_used_percent", 100):
            known.add("ram_high")
            if metrics["ram_used_percent"] >= RAM_TRIGGER:
                failures["ram_high"] = {"value": metrics["ram_used_percent"]}
        if (
            numeric("load1")
            and type(metrics.get("cpus")) is int
            and metrics["cpus"] > 0
        ):
            known.add("load_high")
            if metrics["load1"] / metrics["cpus"] >= LOAD_TRIGGER:
                failures["load_high"] = {
                    "value": metrics["load1"],
                    "cpus": metrics["cpus"],
                }
        cpu = cpu_metrics(metrics)
        if cpu is not None:
            known.add('cpu_high')
            value = cpu['cpu_used_percent']
            if value < CPU_RECOVERED:
                known.add('cpu_below_recovery')
            if value >= CPU_WARNING:
                failures['cpu_high'] = {'value': value, 'window_seconds': cpu['cpu_sample_window_seconds'],
                    'severity': 'critical' if value >= CPU_CRITICAL else 'warning'}
        return failures, known

    @staticmethod
    def measurements(observation):
        metrics = observation.get('host_metrics') or {}
        if not isinstance(metrics, dict):
            return {}
        measured = {}
        for kind, key in [('disk_low','disk_free_percent'), ('ram_high','ram_used_percent')]:
            value = metrics.get(key)
            if type(value) in {int,float} and math.isfinite(value) and 0 <= value <= 100:
                measured[kind] = {'value': value}
        value, cpus = metrics.get('load1'), metrics.get('cpus')
        if type(value) in {int,float} and math.isfinite(value) and value >= 0 and type(cpus) is int and cpus > 0:
            measured['load_high'] = {'value': value, 'cpus': cpus}
        cpu = cpu_metrics(metrics)
        if cpu is not None:
            measured['cpu_high'] = {'value': cpu['cpu_used_percent'], 'window_seconds': cpu['cpu_sample_window_seconds']}
        return measured

    def _observe_cpu(self, node):
        if self.stop is not None and self.stop.is_set():
            return {}, set(), {}
        try:
            observation = self.driver.host_metrics(node['key'])
            failures, known = self.classify(observation, [])
            return (
                {kind: value for kind, value in failures.items() if kind in FAST_KINDS},
                known & (FAST_KINDS | {'cpu_below_recovery'}),
                {kind: value for kind, value in self.measurements(observation).items() if kind in FAST_KINDS}
            )
        except Exception:
            # A missing CPU capability or unavailable sample is never proof of an outage.
            return {}, set(), {}

    def _observe(self, node):
        try:
            observation = self.driver.inspect_node_services(node['key'])
            failures, known = self.classify(observation, monitored_protocols(node))
            return failures, known, self.measurements(observation)
        except grpc.RpcError as exc:
            if exc.code() in {
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.DEADLINE_EXCEEDED,
                grpc.StatusCode.FAILED_PRECONDITION,
            }:
                return {"node_unreachable": {}}, {"node_unreachable"}, {}
            return {}, set(), {}
        except Exception:  # noqa: BLE001 -- malformed observations stay unknown
            return {}, set(), {}

    @staticmethod
    def _event(conn, node, kind, payload, resolved, timestamp):
        event_id = str(uuid4())
        clean = _clean_payload(payload)
        conn.execute(
            "INSERT INTO backend_alert_events VALUES (?,?,?,?,?,?)",
            (
                event_id,
                node["key"],
                kind,
                int(resolved),
                json.dumps({"node_title": node["title"], **clean}),
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

    def scheduled(self, cpu_only=False):
        scan_key = "alert_last_cpu_scan" if cpu_only else "alert_last_scan"
        timestamp = datetime.now(timezone.utc)
        with self.db.connect() as conn:
            from .maintenance_gate import active
            if active(conn):
                return False
            policy = self._policy(conn)
            row = conn.execute(
                "SELECT value FROM backend_system_settings WHERE key=?", (scan_key,)
            ).fetchone()
            last = json.loads(row["value"]) if row else None
            if not policy["enabled"] or (
                last
                and datetime.fromisoformat(last["at"])
                > timestamp - timedelta(seconds=CHECK_SECONDS)
            ):
                return False
            if conn.execute(
                "SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running')"
            ).fetchone():
                return False
            nodes = [
                dict(row)
                for row in conn.execute(f"""SELECT n.* FROM backend_nodes n
                WHERE {AGENT_INSTALLED_SQL}
                AND NOT EXISTS (SELECT 1 FROM backend_node_jobs j WHERE j.node_key=n.key AND j.status IN ('awaiting_executor','running'))
                AND NOT EXISTS (SELECT 1 FROM backend_node_settings_tasks t WHERE t.node_key=n.key AND t.status IN ('awaiting_executor','running'))
                AND NOT EXISTS (SELECT 1 FROM backend_agent_rollouts a WHERE a.node_key=n.key AND a.status IN ('awaiting_executor','running'))
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
                    results = list(pool.map(self._observe_cpu if cpu_only else self._observe, nodes))
                self.driver.binary_info()
            if any(
                (
                    (FAST_KINDS if cpu_only else {
                        "node_unreachable",
                        *RESOURCE_TYPES,
                        *[p + "_down" for p in monitored_protocols(node)],
                    }) - known
                )
                for node, (_, known, _) in zip(nodes, results)
            ):
                status = "partial"
        except Exception:  # noqa: BLE001 -- persist a sanitized failed scan
            results, status = [({}, set(), {}) for _ in nodes], "failed"
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
            # Another scan may have completed while this one's RPCs were in flight.
            committed = conn.execute("SELECT value FROM backend_system_settings WHERE key=?", (scan_key,)).fetchone()
            if committed and datetime.fromisoformat(json.loads(committed['value'])['at']) >= timestamp:
                return False
            fast_row = conn.execute("SELECT value FROM backend_system_settings WHERE key='alert_last_cpu_scan'").fetchone() if not cpu_only else None
            newer_fast_scan = bool(fast_row and datetime.fromisoformat(json.loads(fast_row['value'])['at']) >= timestamp)
            full_row = conn.execute("SELECT value FROM backend_system_settings WHERE key='alert_last_scan'").fetchone() if cpu_only else None
            newer_full_scan = bool(full_row and datetime.fromisoformat(json.loads(full_row['value'])['at']) > timestamp)

            repeat_minutes = policy.get("repeat_minutes", DEFAULT_REPEAT_MINUTES)

            for node, (failures, known, measured) in zip(nodes, results):
                current = conn.execute(
                    f"""SELECT n.enabled,n.desired_revision,n.applied_revision FROM backend_nodes n WHERE n.key=?
                    AND {MONITORABLE_NODE_SQL}
                    AND NOT EXISTS (SELECT 1 FROM backend_agent_rollouts a WHERE a.node_key=n.key AND a.status IN ('awaiting_executor','running'))""",
                    (node["key"],),
                ).fetchone()
                if (
                    not current
                    or current["enabled"] != node["enabled"]
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

                # Coordinate fast/full observations: older full metrics must not overwrite newer fast state for ALL cpu/ram/la
                ignored = set()
                if not cpu_only:
                    for k in FAST_KINDS:
                        prev_k = previous.get(k)
                        if newer_fast_scan or (prev_k and prev_k['last_seen_at'] > timestamp.isoformat()):
                            ignored.add(k)
                            failures.pop(k, None)
                            known.discard(k)
                            if k == 'cpu_high':
                                known.discard('cpu_below_recovery')
                else:
                    for k in FAST_KINDS:
                        prev_k = previous.get(k)
                        if newer_full_scan or (prev_k and prev_k['last_seen_at'] > timestamp.isoformat()):
                            ignored.add(k)
                            failures.pop(k, None)
                            known.discard(k)
                            if k == 'cpu_high':
                                known.discard('cpu_below_recovery')

                for kind, payload in list(failures.items()):
                    prev_row = previous.get(kind)
                    prev_payload = json.loads(prev_row["payload_json"]) if prev_row else {}
                    if kind == 'cpu_high' and prev_payload:
                        if prev_payload.get('severity') == 'critical' and payload['value'] >= 90:
                            payload['severity'] = 'critical'

                    critical_notified = prev_payload.get('_critical_notified', prev_payload.get('severity') == 'critical')
                    should_emit = False
                    if prev_row is None:
                        should_emit = True
                    else:
                        if kind == 'cpu_high':
                            curr_sev = payload.get('severity', 'warning')
                            if not critical_notified and curr_sev == 'critical':
                                should_emit = True
                        if not should_emit and repeat_minutes > 0:
                            last_notified_str = prev_payload.get('_last_notified_at')
                            if not last_notified_str:
                                evt_row = conn.execute(
                                    """SELECT created_at FROM backend_alert_events
                                    WHERE node_key=? AND kind=? AND resolved=0
                                    ORDER BY created_at DESC, id DESC LIMIT 1""",
                                    (node["key"], kind),
                                ).fetchone()
                                last_notified_str = evt_row["created_at"] if evt_row else prev_row["first_seen_at"]
                            try:
                                last_notified_dt = datetime.fromisoformat(last_notified_str)
                                if timestamp >= last_notified_dt + timedelta(minutes=repeat_minutes):
                                    should_emit = True
                            except (ValueError, TypeError):
                                pass

                    if kind == 'cpu_high':
                        payload['_critical_notified'] = critical_notified or payload.get('severity') == 'critical'
                    if should_emit:
                        payload["_last_notified_at"] = timestamp.isoformat()
                    elif "_last_notified_at" in prev_payload:
                        payload["_last_notified_at"] = prev_payload["_last_notified_at"]
                    payload.pop("_healthy_since", None)

                    if should_emit:
                        self._event(
                            conn, node, kind, payload, False, timestamp.isoformat()
                        )
                    conn.execute(
                        "INSERT INTO backend_alert_state (node_key,kind,payload_json,first_seen_at,last_seen_at) VALUES (?,?,?,?,?) ON CONFLICT(node_key,kind) DO UPDATE SET payload_json=excluded.payload_json,last_seen_at=excluded.last_seen_at",
                        (
                            node["key"],
                            kind,
                            json.dumps(payload),
                            prev_row["first_seen_at"] if prev_row else timestamp.isoformat(),
                            timestamp.isoformat(),
                        ),
                    )
                for kind, row in previous.items():
                    if kind in ignored or (cpu_only and kind not in FAST_KINDS):
                        continue
                    if kind in failures:
                        continue
                    if kind in {"xray_down", "awg_down"} and kind.removesuffix(
                        "_down"
                    ) not in monitored_protocols(node):
                        conn.execute(
                            "DELETE FROM backend_alert_state WHERE node_key=? AND kind=?",
                            (node["key"], kind),
                        )
                        continue
                    prev_payload = json.loads(row["payload_json"])
                    if kind not in known:
                        if "_healthy_since" in prev_payload:
                            prev_payload.pop("_healthy_since", None)
                            conn.execute(
                                "UPDATE backend_alert_state SET payload_json=? WHERE node_key=? AND kind=?",
                                (json.dumps(prev_payload), node["key"], kind),
                            )
                        continue
                    if is_healthy_observation(kind, failures, known, measured):
                        healthy_since = prev_payload.get("_healthy_since")
                        # Two snapshots separated by an unobserved outage are not confirmation.
                        if healthy_since and timestamp - datetime.fromisoformat(row['last_seen_at']) > timedelta(seconds=CHECK_SECONDS * 3):
                            healthy_since = None
                        if not healthy_since:
                            prev_payload["_healthy_since"] = timestamp.isoformat()
                            conn.execute(
                                "UPDATE backend_alert_state SET payload_json=?, last_seen_at=? WHERE node_key=? AND kind=?",
                                (json.dumps(prev_payload), timestamp.isoformat(), node["key"], kind),
                            )
                        else:
                            try:
                                candidate_dt = datetime.fromisoformat(healthy_since)
                                if timestamp - candidate_dt >= timedelta(seconds=30):
                                    if policy["notify_resolved"]:
                                        self._event(
                                            conn,
                                            node,
                                            kind,
                                            measured.get(kind, {}),
                                            True,
                                            timestamp.isoformat(),
                                        )
                                    conn.execute(
                                        "DELETE FROM backend_alert_state WHERE node_key=? AND kind=?",
                                        (node["key"], kind),
                                    )
                                else:
                                    conn.execute(
                                        "UPDATE backend_alert_state SET last_seen_at=? WHERE node_key=? AND kind=?",
                                        (timestamp.isoformat(), node["key"], kind),
                                    )
                            except (ValueError, TypeError):
                                prev_payload["_healthy_since"] = timestamp.isoformat()
                                conn.execute(
                                    "UPDATE backend_alert_state SET payload_json=?, last_seen_at=? WHERE node_key=? AND kind=?",
                                    (json.dumps(prev_payload), timestamp.isoformat(), node["key"], kind),
                                )
                    else:
                        if "_healthy_since" in prev_payload:
                            prev_payload.pop("_healthy_since", None)
                        conn.execute(
                            "UPDATE backend_alert_state SET payload_json=?, last_seen_at=? WHERE node_key=? AND kind=?",
                            (json.dumps(prev_payload), timestamp.isoformat(), node["key"], kind),
                        )
            conn.execute(f"""DELETE FROM backend_alert_state WHERE NOT EXISTS
                (SELECT 1 FROM backend_nodes n WHERE n.key=backend_alert_state.node_key AND {MONITORABLE_NODE_SQL})""")
            conn.execute(
                "INSERT INTO backend_system_settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    scan_key, json.dumps(
                        {
                            "at": timestamp.isoformat(),
                            "status": status,
                            "nodes_checked": 0 if status == "failed" else len(results),
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
        selection = None
        with self.db.connect() as conn:
            pending_update = conn.execute("""SELECT 1 FROM backend_alert_deliveries d
                JOIN backend_alert_events e ON e.id=d.event_id
                WHERE d.status='queued' AND e.kind='update_available' LIMIT 1""").fetchone()
        if pending_update:
            from app.services import app_settings
            selection = (app_settings.is_updates_auto_check_enabled(),
                app_settings.get_updates_branch(), app_settings.get_updates_dev_track())
        timestamp = datetime.now(timezone.utc)
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
            from .maintenance_gate import active
            if active(conn):
                return None
            from .telegram_delivery_retry import remaining
            if remaining(conn, principal, timestamp):
                return None
            conn.execute(
                "UPDATE backend_alert_deliveries SET status='unknown' WHERE status='claimed' AND claimed_until<=?",
                (timestamp.isoformat(),),
            )
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
                event = conn.execute('SELECT kind,payload_json FROM backend_alert_events WHERE id=?', (row['event_id'],)).fetchone()
                update_notice = event['kind'] == 'update_available'
                if update_notice:
                    payload = json.loads(event['payload_json'])
                    eligible = selection == (True, payload['branch'], payload['dev_track'])
                else:
                    eligible = self._policy(conn)['enabled']
                if not eligible:
                    conn.execute("UPDATE backend_alert_deliveries SET status='skipped' WHERE id=?", (row['id'],))
                    continue
                admin = conn.execute(
                    """SELECT 1 FROM backend_accounts a JOIN backend_external_identities i ON i.account_id=a.id AND i.provider='telegram'
                    WHERE a.id=? AND a.role='admin' AND a.status='approved' AND i.subject=?""",
                    (row["account_id"], row["telegram_subject"]),
                ).fetchone()
                node = conn.execute(
                    f"""SELECT 1 FROM backend_nodes n JOIN backend_alert_events e ON e.node_key=n.key
                    WHERE e.id=? AND {MONITORABLE_NODE_SQL}
                    AND (e.kind NOT IN ('awg_down','xray_down') OR
                        (n.enabled=1 AND n.applied_revision>0 AND n.applied_revision=n.desired_revision
                        AND ((e.kind='awg_down' AND n.protocols_json LIKE '%"awg"%')
                            OR (e.kind='xray_down' AND n.protocols_json LIKE '%"xray"%'))))
                    AND NOT EXISTS (SELECT 1 FROM backend_node_drains d WHERE d.node_key=n.key)
                    AND NOT EXISTS (SELECT 1 FROM backend_node_cleanup c WHERE c.node_key=n.key)""",
                    (row["event_id"],),
                ).fetchone()
                if not admin or not (update_notice or node):
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

    def acknowledge(self, principal, delivery_id, key, status, retry_after=None):
        AnnouncementService._transport(principal)
        from .telegram_delivery_retry import validate_ack, defer
        validate_ack(status, retry_after)
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM backend_alert_deliveries WHERE id=? AND adapter_id=? AND claim_id=?",
                (delivery_id, principal.id, key),
            ).fetchone()
            if not row:
                raise AccessDenied("resource_not_found", 404)
            if status == 'retry':
                if row['status'] == 'claimed':
                    defer(conn, 'backend_alert_deliveries', delivery_id, principal, retry_after)
                elif row['status'] != 'queued':
                    raise AccessDenied('delivery_finalized', 409)
                return {'status': 'retry'}
            if row["status"] != "claimed" and row["status"] != status:
                raise AccessDenied("delivery_finalized", 409)
            conn.execute(
                "UPDATE backend_alert_deliveries SET status=? WHERE id=?",
                (status, delivery_id),
            )
        return {"status": status}
