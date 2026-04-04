from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from config import ADMIN_IDS, SQLITE_DB_PATH
from db.schema import ensure_schema
from db.sqlite_db import SQLiteDB
from i18n import get_user_locale, t
from services import app_settings
from services.server_registry import RegisteredServer, list_servers
from services.server_runtime import run_server_command

_log = logging.getLogger("alerts")
_db = SQLiteDB(SQLITE_DB_PATH)
_schema_ready = False
_HOST_CHECK_TIMEOUT = 25
_MAX_WORKERS = 4
_CONFIRM_CYCLES = 1


@dataclass(frozen=True)
class AlertRecord:
    alert_key: str
    server_key: str
    alert_type: str
    severity: str
    payload: dict[str, Any]


def _ensure_runtime_schema() -> None:
    global _schema_ready
    if _schema_ready:
        with _db.connect() as conn:
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='alert_state'").fetchone()
        if row:
            return
        _schema_ready = False
    with _db.transaction() as conn:
        ensure_schema(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_state (
                alert_key TEXT PRIMARY KEY,
                server_key TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                active INTEGER NOT NULL DEFAULT 0,
                hit_streak INTEGER NOT NULL DEFAULT 0,
                clear_streak INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT NOT NULL DEFAULT '',
                last_sent_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
    _schema_ready = True


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _render_alert_message(record: AlertRecord, lang: str, resolved: bool = False) -> str:
    payload = record.payload
    server = str(payload.get("server_name") or record.server_key)
    if record.alert_type == "node_unreachable":
        title = t(lang, "admin.alerts.msg_unreachable_resolved" if resolved else "admin.alerts.msg_unreachable")
        if resolved:
            return f"{title}\nServer: {server}"
        return f"{title}\nServer: {server}\nReason: {payload.get('message') or 'unknown'}"
    if record.alert_type == "disk_low":
        title = t(lang, "admin.alerts.msg_disk_resolved" if resolved else "admin.alerts.msg_disk_low")
        return f"{title}\nServer: {server}\nFree: {payload.get('free_percent')}%"
    if record.alert_type == "ram_high":
        title = t(lang, "admin.alerts.msg_ram_resolved" if resolved else "admin.alerts.msg_ram_high")
        return f"{title}\nServer: {server}\nUsed: {payload.get('used_percent')}%"
    if record.alert_type == "load_high":
        title = t(lang, "admin.alerts.msg_load_resolved" if resolved else "admin.alerts.msg_load_high")
        return f"{title}\nServer: {server}\nLoad(1m): {payload.get('load1')}\nCPUs: {payload.get('cpus')}"
    if record.alert_type == "service_down":
        service = str(payload.get("service") or "service")
        title = t(lang, "admin.alerts.msg_service_resolved" if resolved else "admin.alerts.msg_service_down", service=service)
        if resolved:
            return f"{title}\nServer: {server}"
        return f"{title}\nServer: {server}\nStatus: {payload.get('status') or 'down'}"
    return f"{'✅' if resolved else '⚠️'} {record.alert_type}\nServer: {server}"


def _send_alert(bot: object, record: AlertRecord, resolved: bool = False) -> None:
    if bot is None:
        return
    for admin_id in ADMIN_IDS:
        try:
            lang = get_user_locale(admin_id)
            bot.send_message(chat_id=admin_id, text=_render_alert_message(record, lang, resolved=resolved))
        except Exception:
            _log.exception("Failed to send alert to admin %s", admin_id)


def _load_state() -> dict[str, dict[str, Any]]:
    _ensure_runtime_schema()
    with _db.connect() as conn:
        rows = conn.execute("SELECT * FROM alert_state").fetchall()
    items: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except Exception:
            payload = {}
        items[str(row["alert_key"])] = {
            "alert_key": str(row["alert_key"]),
            "server_key": str(row["server_key"]),
            "alert_type": str(row["alert_type"]),
            "severity": str(row["severity"]),
            "payload": payload if isinstance(payload, dict) else {},
            "active": int(row["active"] or 0),
            "hit_streak": int(row["hit_streak"] or 0),
            "clear_streak": int(row["clear_streak"] or 0),
            "first_seen_at": str(row["first_seen_at"] or ""),
            "last_seen_at": str(row["last_seen_at"] or ""),
            "last_sent_at": str(row["last_sent_at"] or ""),
        }
    return items


def _upsert_state(item: dict[str, Any]) -> None:
    _ensure_runtime_schema()
    with _db.transaction() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO alert_state(
                alert_key, server_key, alert_type, severity, payload_json,
                active, hit_streak, clear_streak, first_seen_at, last_seen_at, last_sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["alert_key"],
                item["server_key"],
                item["alert_type"],
                item["severity"],
                json.dumps(item.get("payload") or {}, ensure_ascii=False, sort_keys=True),
                int(item.get("active") or 0),
                int(item.get("hit_streak") or 0),
                int(item.get("clear_streak") or 0),
                str(item.get("first_seen_at") or ""),
                str(item.get("last_seen_at") or ""),
                str(item.get("last_sent_at") or ""),
            ),
        )


def _delete_state(alert_key: str) -> None:
    _ensure_runtime_schema()
    with _db.transaction() as conn:
        conn.execute("DELETE FROM alert_state WHERE alert_key = ?", (alert_key,))


def count_active_alerts() -> int:
    _ensure_runtime_schema()
    with _db.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM alert_state WHERE active = 1").fetchone()
    return int(row["c"]) if row and row["c"] is not None else 0


def _float(value: str, default: float = 0.0) -> float:
    try:
        return float(str(value or "").strip())
    except Exception:
        return default


def _int(value: str, default: int = 0) -> int:
    try:
        return int(str(value or "").strip())
    except Exception:
        return default


def _service_specs(server: RegisteredServer) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    if "xray" in server.protocol_kinds:
        specs.append(("xray", "xray"))
    if "awg" in server.protocol_kinds:
        specs.append(("awg", "amnezia-awg"))
    return specs


def _health_script(server: RegisteredServer) -> str:
    service_cases: list[str] = []
    for service_name, default_container in _service_specs(server):
        env_name = "XRAY_CONTAINER_NAME" if service_name == "xray" else "AWG_CONTAINER_NAME"
        service_cases.append(
            f"""
container="${{{env_name}:-{default_container}}}"
if docker_cmd inspect "$container" >/dev/null 2>&1; then
  if [[ "$(docker_cmd inspect -f '{{{{.State.Running}}}}' "$container" 2>/dev/null || echo false)" == "true" ]]; then
    echo "service:{service_name}:running"
  else
    echo "service:{service_name}:stopped"
  fi
else
  echo "service:{service_name}:missing"
fi
"""
        )
    return f"""#!/usr/bin/env bash
set -euo pipefail
if [[ -r /proc/loadavg ]]; then
  echo "load1:$(cut -d' ' -f1 /proc/loadavg)"
fi
cpus="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
echo "cpus:$cpus"
if command -v df >/dev/null 2>&1; then
  echo "disk_free_percent:$(df -P / | awk 'NR==2 {{gsub("%","",$5); print 100-$5}}')"
fi
if [[ -r /proc/meminfo ]]; then
  mem_total="$(awk '/MemTotal:/ {{print $2}}' /proc/meminfo)"
  mem_avail="$(awk '/MemAvailable:/ {{print $2}}' /proc/meminfo)"
  if [[ -n "$mem_total" && "$mem_total" != "0" ]]; then
    echo "mem_used_percent:$(( (100 * (mem_total - mem_avail)) / mem_total ))"
  fi
fi
docker_cmd() {{
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return
  fi
  if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    sudo docker "$@"
    return
  fi
  return 1
}}
if [[ -f /etc/node-plane/node.env ]]; then
  source /etc/node-plane/node.env
fi
{"".join(service_cases)}
"""


def _server_alerts(server: RegisteredServer) -> list[AlertRecord]:
    if not server.enabled or server.bootstrap_state != "bootstrapped":
        return []
    rc, out = run_server_command(server, _health_script(server), timeout=_HOST_CHECK_TIMEOUT)
    if rc != 0:
        return [
            AlertRecord(
                alert_key=f"server:{server.key}:node_unreachable",
                server_key=server.key,
                alert_type="node_unreachable",
                severity="critical",
                payload={"server_name": f"{server.flag} {server.title} ({server.key})", "message": (out or "").strip()[:300]},
            )
        ]
    payload: dict[str, str] = {}
    for raw_line in (out or "").splitlines():
        line = raw_line.strip()
        if ":" not in line:
            continue
        if line.startswith("service:"):
            parts = line.split(":", 2)
            if len(parts) == 3:
                payload[f"service:{parts[1]}"] = parts[2]
            continue
        key, value = line.split(":", 1)
        payload[key.strip()] = value.strip()
    alerts: list[AlertRecord] = []
    server_name = f"{server.flag} {server.title} ({server.key})"
    free_percent = _int(payload.get("disk_free_percent"), 100)
    if free_percent < 15:
        alerts.append(
            AlertRecord(
                alert_key=f"server:{server.key}:disk_low",
                server_key=server.key,
                alert_type="disk_low",
                severity="critical" if free_percent < 10 else "warning",
                payload={"server_name": server_name, "free_percent": free_percent},
            )
        )
    mem_used = _int(payload.get("mem_used_percent"), 0)
    if mem_used > 85:
        alerts.append(
            AlertRecord(
                alert_key=f"server:{server.key}:ram_high",
                server_key=server.key,
                alert_type="ram_high",
                severity="critical" if mem_used > 92 else "warning",
                payload={"server_name": server_name, "used_percent": mem_used},
            )
        )
    load1 = _float(payload.get("load1"), 0.0)
    cpus = max(1, _int(payload.get("cpus"), 1))
    if load1 > (cpus * 1.5):
        alerts.append(
            AlertRecord(
                alert_key=f"server:{server.key}:load_high",
                server_key=server.key,
                alert_type="load_high",
                severity="critical" if load1 > (cpus * 2.5) else "warning",
                payload={"server_name": server_name, "load1": f"{load1:.2f}", "cpus": cpus},
            )
        )
    for service_name, _default in _service_specs(server):
        service_status = str(payload.get(f"service:{service_name}") or "").strip().lower()
        if service_status != "running":
            alerts.append(
                AlertRecord(
                    alert_key=f"server:{server.key}:service:{service_name}:down",
                    server_key=server.key,
                    alert_type="service_down",
                    severity="critical",
                    payload={"server_name": server_name, "service": service_name, "status": service_status or "down"},
                )
            )
    return alerts


def _collect_alerts() -> list[AlertRecord]:
    servers = [server for server in list_servers(include_disabled=False) if server.enabled]
    records: list[AlertRecord] = []
    if not servers:
        return records
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, max(1, len(servers)))) as pool:
        futures = {pool.submit(_server_alerts, server): server.key for server in servers}
        for future in as_completed(futures):
            try:
                records.extend(future.result())
            except Exception:
                _log.exception("Alert scan failed for server %s", futures[future])
    return records


def _apply_scan(records: Iterable[AlertRecord], *, bot: object | None = None) -> None:
    seen_at = _utcnow_iso()
    current = {record.alert_key: record for record in records}
    state = _load_state()

    for alert_key, record in current.items():
        row = state.get(alert_key)
        if not row:
            row = {
                "alert_key": alert_key,
                "server_key": record.server_key,
                "alert_type": record.alert_type,
                "severity": record.severity,
                "payload": record.payload,
                "active": 0,
                "hit_streak": 0,
                "clear_streak": 0,
                "first_seen_at": seen_at,
                "last_seen_at": seen_at,
                "last_sent_at": "",
            }
        row["server_key"] = record.server_key
        row["alert_type"] = record.alert_type
        row["severity"] = record.severity
        row["payload"] = record.payload
        row["last_seen_at"] = seen_at
        row["clear_streak"] = 0
        row["hit_streak"] = int(row.get("hit_streak") or 0) + 1
        if int(row.get("active") or 0) != 1 and int(row["hit_streak"]) >= _CONFIRM_CYCLES:
            row["active"] = 1
            row["last_sent_at"] = seen_at
            _send_alert(bot, record, resolved=False)
        _upsert_state(row)

    for alert_key, row in state.items():
        if alert_key in current:
            continue
        if int(row.get("active") or 0) != 1:
            _delete_state(alert_key)
            continue
        row["clear_streak"] = int(row.get("clear_streak") or 0) + 1
        row["hit_streak"] = 0
        if int(row["clear_streak"]) >= _CONFIRM_CYCLES:
            if app_settings.is_alerts_notify_resolved_enabled():
                record = AlertRecord(
                    alert_key=alert_key,
                    server_key=str(row.get("server_key") or ""),
                    alert_type=str(row.get("alert_type") or ""),
                    severity=str(row.get("severity") or "warning"),
                    payload=dict(row.get("payload") or {}),
                )
                _send_alert(bot, record, resolved=True)
            _delete_state(alert_key)
        else:
            _upsert_state(row)


def get_alerts_overview() -> dict[str, Any]:
    state = app_settings.get_alerts_state()
    return {
        **state,
        "active_count": count_active_alerts(),
    }


def alert_monitor_job(context: object | None = None) -> None:
    if not app_settings.is_alerts_enabled():
        return
    overview = app_settings.get_alerts_state()
    last_run = _parse_iso(str(overview.get("last_run_at") or ""))
    interval = timedelta(minutes=app_settings.get_alerts_interval_minutes())
    if last_run and (_utcnow() - last_run) < interval:
        return
    bot = getattr(context, "bot", None) if context is not None else None
    started_at = _utcnow_iso()
    try:
        records = _collect_alerts()
        _apply_scan(records, bot=bot)
        app_settings.record_alerts_run("success", started_at)
    except Exception as exc:
        _log.exception("Alert monitor job failed")
        app_settings.record_alerts_run("failed", started_at, error=str(exc))
