from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from db import ensure_schema, get_db
from services.app_settings import is_global_telemetry_enabled
from services.awg import extract_client_public_key, list_awg_peer_transfers
from domain.servers import get_access_methods_for_codes
from services.server_registry import list_servers
from services.xray import list_xray_user_transfers


log = logging.getLogger("traffic_usage")
_db = get_db()
_schema_ready = False


def _ensure_runtime_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _db.transaction() as conn:
        ensure_schema(conn)
    _schema_ready = True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _month_start_iso(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def record_traffic_sample(
    profile_name: str,
    server_key: str,
    protocol_kind: str,
    remote_id: str,
    rx_bytes_total: int,
    tx_bytes_total: int,
    sampled_at: str,
) -> None:
    _ensure_runtime_schema()
    with _db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO traffic_samples(
                profile_name, server_key, protocol_kind, remote_id,
                rx_bytes_total, tx_bytes_total, sampled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_name,
                server_key,
                protocol_kind,
                remote_id,
                int(rx_bytes_total),
                int(tx_bytes_total),
                sampled_at,
            ),
        )


def _collect_awg_server_samples(server_key: str) -> tuple[int, str]:
    _ensure_runtime_schema()
    if not is_global_telemetry_enabled():
        return 0, "telemetry disabled globally"
    code, records, raw = list_awg_peer_transfers(server_key)
    if code != 0:
        return code, raw

    peer_map = {
        str(item.get("peer_key") or ""): item
        for item in records
        if str(item.get("peer_key") or "")
    }
    sampled_at = _now_iso()
    collected = 0

    missing_peer_keys = 0

    with _db.transaction() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT cfg.profile_name, cfg.config_text, cfg.wg_conf
            FROM awg_server_configs cfg
            JOIN telegram_users tu ON tu.profile_name = cfg.profile_name
            WHERE cfg.server_key = ?
              AND tu.telemetry_enabled = 1
            ORDER BY cfg.profile_name
            """,
            (server_key,),
        ).fetchall()

        for row in rows:
            profile_name = str(row["profile_name"])
            peer_key = extract_client_public_key(str(row["wg_conf"] or "")) or extract_client_public_key(str(row["config_text"] or ""))
            if not peer_key:
                missing_peer_keys += 1
                continue
            item = peer_map.get(peer_key)
            if not item:
                continue
            conn.execute(
                """
                INSERT INTO traffic_samples(
                    profile_name, server_key, protocol_kind, remote_id,
                    rx_bytes_total, tx_bytes_total, sampled_at
                ) VALUES (?, ?, 'awg', ?, ?, ?, ?)
                """,
                (
                    profile_name,
                    server_key,
                    peer_key,
                    int(item["rx_bytes_total"]),
                    int(item["tx_bytes_total"]),
                    sampled_at,
                ),
            )
            collected += 1

    return 0, f"server={server_key}\nsamples={collected}\nmissing_peer_keys={missing_peer_keys}"


def _collect_xray_server_samples(server_key: str) -> tuple[int, str]:
    _ensure_runtime_schema()
    if not is_global_telemetry_enabled():
        return 0, "telemetry disabled globally"
    code, records, raw = list_xray_user_transfers(server_key)
    if code != 0:
        return code, raw

    stats_by_name = {
        str(item.get("name") or ""): item
        for item in records
        if str(item.get("name") or "")
    }
    stats_by_name_lower = {name.lower(): item for name, item in stats_by_name.items()}
    sampled_at = _now_iso()
    collected = 0
    unmatched = 0
    skipped_without_method = 0

    with _db.transaction() as conn:
        rows = conn.execute(
            """
            SELECT
                xp.profile_name,
                xp.uuid,
                pam.access_code
            FROM xray_profiles xp
            LEFT JOIN profile_access_methods pam ON pam.profile_name = xp.profile_name
            WHERE EXISTS (
                SELECT 1
                FROM telegram_users tu
                WHERE tu.profile_name = xp.profile_name
                  AND tu.telemetry_enabled = 1
            )
            ORDER BY xp.profile_name, pam.access_code
            """,
        ).fetchall()

        profiles: dict[str, dict[str, object]] = {}
        for row in rows:
            profile_name = str(row["profile_name"] or "")
            if not profile_name:
                continue
            entry = profiles.setdefault(
                profile_name,
                {"uuid": str(row["uuid"] or ""), "access_codes": []},
            )
            access_code = str(row["access_code"] or "").strip()
            if access_code:
                codes = entry["access_codes"]
                if isinstance(codes, list) and access_code not in codes:
                    codes.append(access_code)

        for profile_name, row in sorted(profiles.items()):
            uuid_val = str(row.get("uuid") or "")
            access_codes = [str(code) for code in list(row.get("access_codes") or []) if str(code).strip()]
            if not profile_name:
                continue
            methods = get_access_methods_for_codes(access_codes)
            if not any(method.protocol_kind == "xray" and method.server_key == server_key for method in methods):
                skipped_without_method += 1
                continue
            item = (
                stats_by_name.get(profile_name)
                or stats_by_name_lower.get(profile_name.lower())
                or (stats_by_name.get(uuid_val) if uuid_val else None)
                or (stats_by_name_lower.get(uuid_val.lower()) if uuid_val else None)
            )
            if not item:
                unmatched += 1
                continue
            conn.execute(
                """
                INSERT INTO traffic_samples(
                    profile_name, server_key, protocol_kind, remote_id,
                    rx_bytes_total, tx_bytes_total, sampled_at
                ) VALUES (?, ?, 'xray', ?, ?, ?, ?)
                """,
                (
                    profile_name,
                    server_key,
                    uuid_val or profile_name,
                    int(item.get("downlink_bytes_total") or 0),
                    int(item.get("uplink_bytes_total") or 0),
                    sampled_at,
                ),
            )
            collected += 1

    return (
        0,
        f"server={server_key}\n"
        f"samples={collected}\n"
        f"unmatched_stats={unmatched}\n"
        f"skipped_without_method={skipped_without_method}",
    )


def collect_awg_traffic_samples() -> tuple[int, str]:
    if not is_global_telemetry_enabled():
        return 0, "telemetry disabled globally"
    blocks: list[str] = []
    errors = 0
    for server in list_servers():
        if "awg" not in server.protocol_kinds:
            continue
        if server.bootstrap_state != "bootstrapped":
            continue
        code, out = _collect_awg_server_samples(server.key)
        blocks.append(out)
        if code != 0:
            errors += 1
            log.warning("AWG traffic sampling failed for %s: %s", server.key, out)
    return (1 if errors else 0), "\n\n".join(blocks) if blocks else "no awg servers to sample"


def collect_xray_traffic_samples() -> tuple[int, str]:
    if not is_global_telemetry_enabled():
        return 0, "telemetry disabled globally"
    blocks: list[str] = []
    errors = 0
    for server in list_servers():
        if "xray" not in server.protocol_kinds:
            continue
        if server.bootstrap_state != "bootstrapped":
            continue
        code, out = _collect_xray_server_samples(server.key)
        blocks.append(out)
        if code != 0:
            errors += 1
            log.warning("Xray traffic sampling failed for %s: %s", server.key, out)
    return (1 if errors else 0), "\n\n".join(blocks) if blocks else "no xray servers to sample"


def run_collect_traffic_once() -> tuple[int, str]:
    awg_code, awg_out = collect_awg_traffic_samples()
    xray_code, xray_out = collect_xray_traffic_samples()
    code = 1 if awg_code or xray_code else 0
    out = f"[AWG]\n{awg_out}\n\n[Xray]\n{xray_out}"
    return code, out


def collect_traffic_job(_context: Any) -> None:
    code, out = run_collect_traffic_once()
    if code != 0:
        log.warning("Traffic sampling finished with errors:\n%s", out)
    else:
        log.info("Traffic sampling completed:\n%s", out)


def debug_awg_traffic_report(server_key: str) -> tuple[int, str]:
    _ensure_runtime_schema()
    code, records, raw = list_awg_peer_transfers(server_key)
    if code != 0:
        return code, raw

    peer_keys = {
        str(item.get("peer_key") or "")
        for item in records
        if str(item.get("peer_key") or "")
    }

    with _db.connect() as conn:
        rows = conn.execute(
            """
            SELECT
                cfg.profile_name,
                cfg.config_text,
                cfg.wg_conf,
                MAX(COALESCE(tu.telemetry_enabled, 0)) AS telemetry_enabled
            FROM awg_server_configs cfg
            LEFT JOIN telegram_users tu ON tu.profile_name = cfg.profile_name
            WHERE cfg.server_key = ?
            GROUP BY cfg.profile_name, cfg.config_text, cfg.wg_conf
            ORDER BY cfg.profile_name
            """,
            (server_key,),
        ).fetchall()

    lines = [
        f"AWG traffic debug: server={server_key}",
        f"server_peers={len(peer_keys)}",
        f"stored_profiles={len(rows)}",
    ]
    if peer_keys:
        preview = ", ".join(sorted(peer_keys)[:10])
        lines.append(f"peer_keys={preview}")

    matched = 0
    missing_key = 0
    missing_on_server = 0
    for row in rows:
        profile_name = str(row["profile_name"] or "")
        wg_conf = str(row["wg_conf"] or "")
        config_text = str(row["config_text"] or "")
        telemetry_enabled = bool(row["telemetry_enabled"]) if row["telemetry_enabled"] is not None else False
        peer_key = extract_client_public_key(wg_conf) or extract_client_public_key(config_text)
        if not peer_key:
            missing_key += 1
            peer_state = "missing_local_key"
        elif peer_key in peer_keys:
            matched += 1
            peer_state = "matched"
        else:
            missing_on_server += 1
            peer_state = "missing_on_server"
        lines.append(
            f"- {profile_name}: telemetry={'on' if telemetry_enabled else 'off'}"
            f", wg_conf={'yes' if bool(wg_conf) else 'no'}"
            f", config={'yes' if bool(config_text) else 'no'}"
            f", peer_key={(peer_key or '—')}"
            f", state={peer_state}"
        )

    lines.append(
        f"summary: matched={matched}, missing_local_key={missing_key}, missing_on_server={missing_on_server}"
    )
    lines.append("")
    lines.append("raw_server_transfer:")
    lines.append(raw[-2000:] if raw else "—")
    return 0, "\n".join(lines)


def debug_profile_traffic_report(profile_name: str, protocol_kind: str = "awg") -> tuple[int, str]:
    _ensure_runtime_schema()
    protocol = str(protocol_kind or "awg").strip().lower()
    if protocol not in {"awg", "xray"}:
        return 1, f"Unsupported protocol_kind: {protocol_kind}"

    month_start = _month_start_iso()
    with _db.connect() as conn:
        rows = conn.execute(
            """
            SELECT server_key, remote_id, rx_bytes_total, tx_bytes_total, sampled_at
            FROM traffic_samples
            WHERE profile_name = ? AND protocol_kind = ? AND sampled_at >= ?
            ORDER BY server_key, remote_id, sampled_at
            """,
            (profile_name, protocol, month_start),
        ).fetchall()

    totals = get_profile_monthly_usage(profile_name, protocol)
    lines = [
        f"Traffic samples debug: profile={profile_name}, protocol={protocol}",
        f"month_start={month_start}",
        f"sample_rows={len(rows)}",
        f"peers={int(totals['peers'])}",
        f"rx_bytes={int(totals['rx_bytes'])}",
        f"tx_bytes={int(totals['tx_bytes'])}",
        f"total_bytes={int(totals['total_bytes'])}",
    ]
    if not rows:
        return 0, "\n".join(lines + ["", "samples=none"])

    groups: Dict[tuple[str, str], list[Dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["server_key"]), str(row["remote_id"]))
        groups.setdefault(key, []).append(dict(row))

    lines.append("")
    lines.append("per_peer:")
    for (server_key, remote_id), samples in groups.items():
        first = samples[0]
        last = samples[-1]
        rx_delta = max(0, int(last["rx_bytes_total"]) - int(first["rx_bytes_total"]))
        tx_delta = max(0, int(last["tx_bytes_total"]) - int(first["tx_bytes_total"]))
        lines.append(
            f"- server={server_key}, remote_id={remote_id}, samples={len(samples)}, "
            f"first={first['sampled_at']}, last={last['sampled_at']}, "
            f"rx_first={int(first['rx_bytes_total'])}, rx_last={int(last['rx_bytes_total'])}, "
            f"tx_first={int(first['tx_bytes_total'])}, tx_last={int(last['tx_bytes_total'])}, "
            f"rx_delta={rx_delta}, tx_delta={tx_delta}"
        )
    return 0, "\n".join(lines)


def get_profile_monthly_usage(profile_name: str, protocol_kind: str = "awg") -> Dict[str, int]:
    _ensure_runtime_schema()
    if not is_global_telemetry_enabled():
        return {"rx_bytes": 0, "tx_bytes": 0, "total_bytes": 0, "samples": 0, "peers": 0}
    month_start = _month_start_iso()
    with _db.connect() as conn:
        rows = conn.execute(
            """
            SELECT server_key, remote_id, rx_bytes_total, tx_bytes_total, sampled_at
            FROM traffic_samples
            WHERE profile_name = ? AND protocol_kind = ? AND sampled_at >= ?
            ORDER BY server_key, remote_id, sampled_at
            """,
            (profile_name, protocol_kind, month_start),
        ).fetchall()

    totals = {"rx_bytes": 0, "tx_bytes": 0, "total_bytes": 0, "samples": 0, "peers": 0}
    groups: Dict[tuple[str, str], list[Dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["server_key"]), str(row["remote_id"]))
        groups.setdefault(key, []).append(dict(row))

    totals["samples"] = len(rows)
    totals["peers"] = len(groups)

    for samples in groups.values():
        if not samples:
            continue
        first = samples[0]
        last = samples[-1]
        rx_delta = max(0, int(last["rx_bytes_total"]) - int(first["rx_bytes_total"]))
        tx_delta = max(0, int(last["tx_bytes_total"]) - int(first["tx_bytes_total"]))
        totals["rx_bytes"] += rx_delta
        totals["tx_bytes"] += tx_delta

    totals["total_bytes"] = totals["rx_bytes"] + totals["tx_bytes"]
    return totals
