from __future__ import annotations

import concurrent.futures
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
    target_servers = [
        s for s in list_servers() 
        if "awg" in s.protocol_kinds and s.bootstrap_state == "bootstrapped"
    ]
    if not target_servers:
        return 0, "no awg servers to sample"

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_collect_awg_server_samples, s.key): s.key for s in target_servers}
        for future in concurrent.futures.as_completed(futures):
            server_key = futures[future]
            try:
                code, out = future.result()
                blocks.append(out)
                if code != 0:
                    errors += 1
                    log.warning("AWG traffic sampling failed for %s: %s", server_key, out)
            except Exception as e:
                errors += 1
                out = f"server={server_key}\nerror={e}"
                blocks.append(out)
                log.warning("AWG traffic sampling crashed for %s: %s", server_key, e)

    return (1 if errors else 0), "\n\n".join(blocks)


def collect_xray_traffic_samples() -> tuple[int, str]:
    if not is_global_telemetry_enabled():
        return 0, "telemetry disabled globally"
    blocks: list[str] = []
    errors = 0
    target_servers = [
        s for s in list_servers() 
        if "xray" in s.protocol_kinds and s.bootstrap_state == "bootstrapped"
    ]
    if not target_servers:
        return 0, "no xray servers to sample"

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_collect_xray_server_samples, s.key): s.key for s in target_servers}
        for future in concurrent.futures.as_completed(futures):
            server_key = futures[future]
            try:
                code, out = future.result()
                blocks.append(out)
                if code != 0:
                    errors += 1
                    log.warning("Xray traffic sampling failed for %s: %s", server_key, out)
            except Exception as e:
                errors += 1
                out = f"server={server_key}\nerror={e}"
                blocks.append(out)
                log.warning("Xray traffic sampling crashed for %s: %s", server_key, e)

    return (1 if errors else 0), "\n\n".join(blocks)


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
    rows = _get_profile_monthly_usage_rows(profile_name, protocol, month_start)

    totals = get_profile_monthly_usage(profile_name, protocol)
    lines = [
        f"Traffic samples debug: profile={profile_name}, protocol={protocol}",
        f"month_start={month_start}",
        f"peers={int(totals['peers'])}",
        f"rx_bytes={int(totals['rx_bytes'])}",
        f"tx_bytes={int(totals['tx_bytes'])}",
        f"total_bytes={int(totals['total_bytes'])}",
    ]
    if not rows:
        return 0, "\n".join(lines + ["", "samples=none"])

    lines.append("")
    lines.append("per_peer:")
    for row in rows:
        server_key = str(row["server_key"])
        remote_id = str(row["remote_id"])
        samples_count = int(row.get("sample_count") or 0)
        first_sample = str(row.get("first_sample") or "")
        last_sample = str(row.get("last_sample") or "")
        rx_first = int(row.get("rx_first") or 0)
        rx_last = int(row.get("rx_last") or 0)
        tx_first = int(row.get("tx_first") or 0)
        tx_last = int(row.get("tx_last") or 0)

        rx_delta = max(0, rx_last - rx_first)
        tx_delta = max(0, tx_last - tx_first)

        lines.append(
            f"- server={server_key}, remote_id={remote_id}, samples={samples_count}, "
            f"first={first_sample}, last={last_sample}, "
            f"rx_first={rx_first}, rx_last={rx_last}, "
            f"tx_first={tx_first}, tx_last={tx_last}, "
            f"rx_delta={rx_delta}, tx_delta={tx_delta}"
        )
    return 0, "\n".join(lines)


def _get_profile_monthly_usage_rows(profile_name: str, protocol_kind: str, month_start: str) -> list[Dict[str, Any]]:
    with _db.connect() as conn:
        rows = conn.execute(
            """
            WITH scoped AS (
                SELECT
                    rowid,
                    server_key,
                    remote_id,
                    rx_bytes_total,
                    tx_bytes_total,
                    sampled_at
                FROM traffic_samples
                WHERE profile_name = ? AND protocol_kind = ? AND sampled_at >= ?
            ),
            ranked AS (
                SELECT
                    server_key,
                    remote_id,
                    rx_bytes_total,
                    tx_bytes_total,
                    sampled_at,
                    COUNT(*) OVER (PARTITION BY server_key, remote_id) AS sample_count,
                    ROW_NUMBER() OVER (
                        PARTITION BY server_key, remote_id
                        ORDER BY sampled_at ASC, rowid ASC
                    ) AS rn_first,
                    ROW_NUMBER() OVER (
                        PARTITION BY server_key, remote_id
                        ORDER BY sampled_at DESC, rowid DESC
                    ) AS rn_last
                FROM scoped
            )
            SELECT
                first.server_key,
                first.remote_id,
                first.sample_count,
                first.sampled_at AS first_sample,
                last.sampled_at AS last_sample,
                first.rx_bytes_total AS rx_first,
                last.rx_bytes_total AS rx_last,
                first.tx_bytes_total AS tx_first,
                last.tx_bytes_total AS tx_last
            FROM ranked first
            JOIN ranked last
              ON last.server_key = first.server_key
             AND last.remote_id = first.remote_id
            WHERE first.rn_first = 1
              AND last.rn_last = 1
            ORDER BY first.server_key, first.remote_id
            """,
            (profile_name, protocol_kind, month_start),
        ).fetchall()
    return [dict(row) for row in rows]


def get_profile_monthly_usage(profile_name: str, protocol_kind: str = "awg") -> Dict[str, int]:
    _ensure_runtime_schema()
    if not is_global_telemetry_enabled():
        return {"rx_bytes": 0, "tx_bytes": 0, "total_bytes": 0, "samples": 0, "peers": 0}
    month_start = _month_start_iso()
    rows = _get_profile_monthly_usage_rows(profile_name, protocol_kind, month_start)

    totals = {"rx_bytes": 0, "tx_bytes": 0, "total_bytes": 0, "samples": 0, "peers": len(rows)}

    for row in rows:
        rx_first = int(row.get("rx_first") or 0)
        rx_last = int(row.get("rx_last") or 0)
        tx_first = int(row.get("tx_first") or 0)
        tx_last = int(row.get("tx_last") or 0)

        rx_delta = max(0, rx_last - rx_first)
        tx_delta = max(0, tx_last - tx_first)

        totals["rx_bytes"] += rx_delta
        totals["tx_bytes"] += tx_delta
        totals["samples"] += int(row.get("sample_count") or 0)

    totals["total_bytes"] = totals["rx_bytes"] + totals["tx_bytes"]
    return totals
