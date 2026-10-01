"""Consent-bound, cumulative traffic accounting from native agent counters.

First observations are baselines, not usage. Resets/restarts add only the new
epoch's counters; outages retain the last baseline and never become zeroes.
"""

import base64
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .authorization import AccessDenied
from .config_issuance import ConfigIssuanceService

INTERVAL_MINUTES = 5
BATCH_SIZE = 32
MAX_COUNTER = 2**63 - 1


def setting(conn, key, default=None):
    row = conn.execute(
        "SELECT value FROM backend_system_settings WHERE key=?", (key,)
    ).fetchone()
    return json.loads(row["value"]) if row else default


class TrafficService:
    def __init__(self, db, driver=None):
        self.db, self.driver = db, driver

    def initialize_schema(self):
        with self.db.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_traffic_usage (
                profile_id TEXT NOT NULL REFERENCES backend_profiles(id) ON DELETE CASCADE,
                account_id TEXT NOT NULL REFERENCES backend_accounts(id) ON DELETE CASCADE,
                node_key TEXT NOT NULL REFERENCES backend_nodes(key) ON DELETE CASCADE,
                protocol TEXT NOT NULL CHECK(protocol IN ('awg','xray')),
                uplink_bytes BIGINT NOT NULL DEFAULT 0, downlink_bytes BIGINT NOT NULL DEFAULT 0,
                epoch TEXT, identity TEXT, last_uplink BIGINT, last_downlink BIGINT,
                tracked_since TEXT, last_sample_at TEXT, status TEXT NOT NULL,
                PRIMARY KEY(profile_id,node_key,protocol))""")

    @staticmethod
    def _enabled(conn):
        return setting(conn, "traffic_enabled", False) is True

    @staticmethod
    def _token(conn, account_id):
        return (
            setting(conn, "traffic_generation"),
            setting(conn, "traffic_consent_generation:" + account_id),
        )

    @staticmethod
    def _targets(conn):
        rows = conn.execute("""SELECT p.id,p.owner_account_id,g.node_key,g.protocol
            FROM backend_profiles p JOIN backend_grants g ON g.profile_id=p.id
            JOIN backend_accounts a ON a.id=p.owner_account_id
            JOIN backend_system_settings s ON s.key=('traffic_consent:' || a.id)
            WHERE a.status='approved' AND s.value='true'
            AND NOT EXISTS (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id=p.id)
            ORDER BY p.id,g.node_key,g.protocol""").fetchall()
        targets = []
        for row in rows:
            try:
                profile, node, identity, latest = ConfigIssuanceService._current(
                    conn,
                    row["id"],
                    row["node_key"],
                    row["protocol"],
                    "tcp" if row["protocol"] == "xray" else "vpn",
                )
            except AccessDenied as exc:
                # XHTTP-only nodes still have the same email traffic counters.
                if row["protocol"] == "xray" and exc.code == "config_not_supported":
                    try:
                        profile, node, identity, latest = (
                            ConfigIssuanceService._current(
                                conn, row["id"], row["node_key"], "xray", "xhttp"
                            )
                        )
                    except AccessDenied:
                        continue
                else:
                    continue
            if node["applied_revision"] <= 0:
                continue
            if conn.execute(
                "SELECT 1 FROM backend_node_cleanup WHERE node_key=?",
                (row["node_key"],),
            ).fetchone():
                continue
            if conn.execute(
                "SELECT 1 FROM backend_node_retirements WHERE node_key=?",
                (row["node_key"],),
            ).fetchone():
                continue
            expected = identity["xray_uuid"]
            if row["protocol"] == "awg":
                try:
                    interface = json.loads(latest["result_json"])["wg_conf"].split(
                        "[Peer]", 1
                    )[0]
                    keys = re.findall(r"(?m)^PublicKey\s*=\s*(\S+)\s*$", interface)
                    if (
                        len(keys) != 1
                        or len(base64.b64decode(keys[0], validate=True)) != 32
                    ):
                        continue
                    expected = keys[0]
                except (ValueError, TypeError, KeyError):
                    continue
            targets.append(
                {
                    "key": (row["id"], row["node_key"], row["protocol"]),
                    "account_id": row["owner_account_id"],
                    "profile_revision": profile["desired_revision"],
                    "node_revision": node["desired_revision"],
                    "token": TrafficService._token(conn, row["owner_account_id"]),
                    "intent": {
                        "node_key": row["node_key"],
                        "protocol": row["protocol"],
                        "runtime_name": profile["runtime_name"],
                        "identity": expected,
                    },
                }
            )
        return targets

    @staticmethod
    def _prune(conn, targets):
        # Ownership transfer, deletion, account suspension and consent withdrawal
        # must not expose history to a different owner or resurrect it later.
        conn.execute("""DELETE FROM backend_traffic_usage WHERE NOT EXISTS (
            SELECT 1 FROM backend_profiles p JOIN backend_accounts a ON a.id=p.owner_account_id
            JOIN backend_system_settings s ON s.key=('traffic_consent:' || a.id)
            WHERE p.id=backend_traffic_usage.profile_id AND a.id=backend_traffic_usage.account_id
            AND a.status='approved' AND s.value='true'
            AND NOT EXISTS (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id=p.id))""")
        active = {target["key"] for target in targets}
        for row in conn.execute(
            "SELECT profile_id,node_key,protocol FROM backend_traffic_usage"
        ).fetchall():
            key = (row["profile_id"], row["node_key"], row["protocol"])
            if key not in active:
                conn.execute(
                    """UPDATE backend_traffic_usage SET epoch=NULL,identity=NULL,
                    last_uplink=NULL,last_downlink=NULL,status='paused'
                    WHERE profile_id=? AND node_key=? AND protocol=?""",
                    key,
                )

    def _observe(self, target):
        try:
            result = self.driver.traffic_snapshot(target["intent"])
            if (
                not isinstance(result, dict)
                or set(result)
                != {*target["intent"], "epoch", "uplink_bytes", "downlink_bytes"}
                or any(result.get(k) != v for k, v in target["intent"].items())
            ):
                raise ValueError("invalid traffic identity")
            if not isinstance(result["epoch"], str) or not re.fullmatch(
                r"[0-9a-f]{64}", result["epoch"]
            ):
                raise ValueError("invalid traffic epoch")
            for key in ("uplink_bytes", "downlink_bytes"):
                if type(result[key]) is not int or not 0 <= result[key] <= MAX_COUNTER:
                    raise ValueError("invalid traffic counter")
            return result
        except Exception:  # noqa: BLE001 -- unknown observations must not reset counters
            return None

    @staticmethod
    def _record(conn, target, observation, timestamp):
        key = target["key"]
        previous = conn.execute(
            """SELECT * FROM backend_traffic_usage
            WHERE profile_id=? AND node_key=? AND protocol=?""",
            key,
        ).fetchone()
        if (
            previous
            and previous["last_sample_at"]
            and previous["last_sample_at"] >= timestamp
        ):
            return
        if not previous:
            conn.execute(
                """INSERT INTO backend_traffic_usage
                (profile_id,node_key,protocol,account_id,status) VALUES (?,?,?,?,'unknown')""",
                (*key, target["account_id"]),
            )
        if observation is None:
            conn.execute(
                """UPDATE backend_traffic_usage SET status='unknown'
                WHERE profile_id=? AND node_key=? AND protocol=?""",
                key,
            )
            return
        up, down = observation["uplink_bytes"], observation["downlink_bytes"]
        increments = [0, 0]
        if previous and previous["epoch"] is not None:
            for index, (value, field) in enumerate(
                ((up, "last_uplink"), (down, "last_downlink"))
            ):
                increments[index] = (
                    value - previous[field]
                    if previous["epoch"] == observation["epoch"]
                    and previous["identity"] == observation["identity"]
                    and value >= previous[field]
                    else value
                )
        up_total = (previous["uplink_bytes"] if previous else 0) + increments[0]
        down_total = (previous["downlink_bytes"] if previous else 0) + increments[1]
        if max(up_total, down_total) > MAX_COUNTER:
            conn.execute(
                """UPDATE backend_traffic_usage SET status='unknown'
                WHERE profile_id=? AND node_key=? AND protocol=?""",
                key,
            )
            return
        conn.execute(
            """UPDATE backend_traffic_usage SET uplink_bytes=?,downlink_bytes=?,
            epoch=?,identity=?,last_uplink=?,last_downlink=?,
            tracked_since=COALESCE(tracked_since,?),last_sample_at=?,status='current'
            WHERE profile_id=? AND node_key=? AND protocol=?""",
            (
                up_total,
                down_total,
                observation["epoch"],
                observation["identity"],
                up,
                down,
                timestamp,
                timestamp,
                *key,
            ),
        )

    def scheduled(self):
        timestamp = datetime.now(timezone.utc)
        with self.db.transaction() as conn:
            from .maintenance_gate import active
            if active(conn):
                return False
            conn.execute(
                "UPDATE backend_account_guard SET revision=revision+1 WHERE id=1"
            )
            from .maintenance_gate import active
            if active(conn):
                return False
            last = setting(conn, "traffic_last_scan")
            if not self._enabled(conn):
                self._prune(conn, [])
                return False
            if conn.execute(
                "SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running')"
            ).fetchone():
                return False
            if last and datetime.fromisoformat(last["at"]) > timestamp - timedelta(
                minutes=INTERVAL_MINUTES
            ):
                return False
            targets = self._targets(conn)
            self._prune(conn, targets)
            cursor = setting(conn, "traffic_cursor", [])
            rotated = [t for t in targets if list(t["key"]) > cursor] + [
                t for t in targets if list(t["key"]) <= cursor
            ]
            batch = rotated[:BATCH_SIZE]
            generation = setting(conn, "traffic_generation")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self._observe, batch))
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE backend_account_guard SET revision=revision+1 WHERE id=1"
            )
            from .maintenance_gate import active
            if active(conn):
                return False
            if not self._enabled(conn) or generation != setting(
                conn, "traffic_generation"
            ):
                return False
            if conn.execute(
                "SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running')"
            ).fetchone():
                return False
            # Lock revisions after remote reads so edits/revocations cannot cross
            # the final authorization check and accounting commit.
            for profile_id in sorted({t["key"][0] for t in batch}):
                conn.execute(
                    "UPDATE backend_profiles SET desired_revision=desired_revision WHERE id=?",
                    (profile_id,),
                )
            for node_key in sorted({t["key"][1] for t in batch}):
                conn.execute(
                    "UPDATE backend_nodes SET desired_revision=desired_revision WHERE key=?",
                    (node_key,),
                )
            current = {t["key"]: t for t in self._targets(conn)}
            self._prune(conn, list(current.values()))
            accepted = failures = 0
            for target, result in zip(batch, results):
                if current.get(target["key"]) != target:
                    continue
                self._record(conn, target, result, timestamp.isoformat())
                accepted += 1
                failures += result is None
            scan = {
                "at": timestamp.isoformat(),
                "status": "partial" if failures else "success",
                "profiles_checked": accepted,
                "unknown": failures,
                "eligible_pairs": len(current),
            }
            for key, value in (
                ("traffic_last_scan", scan),
                ("traffic_cursor", list(batch[-1]["key"]) if batch else []),
            ):
                conn.execute(
                    "INSERT INTO backend_system_settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value)),
                )
        return True

    def summary(self, account_id, profile_id):
        # Caller must first authorize ownership through ProfileService.
        with self.db.connect() as conn:
            if not self._enabled(conn):
                return None
            if setting(conn, "traffic_consent:" + account_id, False) is not True:
                return {"status": "consent_required", "items": []}
            rows = conn.execute(
                """SELECT u.* FROM backend_traffic_usage u
                JOIN backend_profiles p ON p.id=u.profile_id AND p.owner_account_id=u.account_id
                WHERE u.profile_id=? AND u.account_id=? ORDER BY u.protocol,u.node_key""",
                (profile_id, account_id),
            ).fetchall()
            items = []
            for protocol in ("awg", "xray"):
                group = [
                    r for r in rows if r["protocol"] == protocol and r["tracked_since"]
                ]
                if group:
                    items.append(
                        {
                            "protocol": protocol,
                            "uplink_bytes": sum(r["uplink_bytes"] for r in group),
                            "downlink_bytes": sum(r["downlink_bytes"] for r in group),
                            "tracked_since": min(r["tracked_since"] for r in group),
                            "last_sample_at": min(r["last_sample_at"] for r in group),
                        }
                    )
            stale = datetime.now(timezone.utc) - timedelta(minutes=15)
            unknown = any(
                r["status"] != "current"
                or not r["last_sample_at"]
                or datetime.fromisoformat(r["last_sample_at"]) < stale
                for r in rows
            )
            return {
                "status": "unknown" if unknown else "current" if rows else "waiting",
                "items": items,
            }
