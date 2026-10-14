"""PostgreSQL configuration snapshots and worker-owned restore orchestration."""

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from .authorization import (
    ADMIN_PERMISSIONS,
    AccessDenied,
    Account,
    Actor,
    Principal,
    PrincipalKind,
    require_permission,
)
from .operations import OperationRepository

# Dependency order. Command journals, credentials and issued config artifacts
# are deliberately excluded: restoring them could replay work or revive tokens.
TABLES = (
    "backend_accounts",
    "backend_external_identities",
    "backend_telegram_identity_details",
    "backend_system_settings",
    "backend_nodes",
    "backend_regions",
    "backend_node_regions",
    "backend_node_connections",
    "backend_node_notes",
    "backend_profiles",
    "backend_devices",
    "backend_profile_identities",
    "backend_grants",
    "backend_grant_policies",
    "backend_access_requests",
)
CLEAR = (
    "backend_account_deletions",
    "backend_account_menu_refresh",
    "backend_node_bootstraps",
    "backend_temporary_configs",
    "backend_system_cleanup_items",
    "backend_system_cleanup_jobs",
    "backend_system_cleanup_plans",
    "backend_traffic_peers",
    "backend_traffic_usage",
    "backend_node_traffic",
    "backend_alert_deliveries",
    "backend_alert_events",
    "backend_alert_state",
    "backend_announcement_user_state",
    "backend_announcement_deliveries",
    "backend_announcements",
    "backend_repairs",
    "backend_config_issuances",
    "backend_operation_tasks",
    "backend_operations",
    "backend_profile_deletions",
    "backend_profile_commands",
    "backend_device_commands",
    "backend_node_settings_tasks",
    "backend_node_commands",
    "backend_agent_rollout_failures",
    "backend_agent_rollouts",
    "backend_node_cleanup",
    "backend_node_drains",
    "backend_node_verification_targets",
    "backend_node_host_identities",
    "backend_node_removal_inventories",
    "backend_node_retirements",
    "backend_node_jobs",
    "backend_node_removals",
    "backend_account_commands",
    "backend_identity_commands",
    "backend_update_items",
    "backend_update_jobs",
)


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode()


class BackupService:
    def __init__(self, db, root=None, driver=None):
        self.db = db
        self.driver = driver
        self.root = Path(
            root
            or Path(os.environ.get("NODE_PLANE_SHARED_DIR", "/opt/node-plane/shared"))
            / "backups/backend"
        )

    def initialize_schema(self):
        from .temporary_configs import TemporaryConfigService
        TemporaryConfigService(self.db).initialize_schema()
        with self.db.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_backup_jobs (
                id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, command_key TEXT NOT NULL,
                action TEXT NOT NULL, backup_id TEXT, checksum TEXT, status TEXT NOT NULL,
                phase TEXT, result_json TEXT, created_at TEXT NOT NULL,
                UNIQUE(actor_id,command_key))""")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS backend_backup_revocations (job_id TEXT NOT NULL, operation_id TEXT NOT NULL, PRIMARY KEY(job_id,operation_id))"
            )

            conn.execute('''CREATE TABLE IF NOT EXISTS backend_backup_labels (
                job_id TEXT PRIMARY KEY REFERENCES backend_backup_jobs(id) ON DELETE CASCADE,
                name TEXT NOT NULL)''')

    def _columns(self, conn, table):
        return [
            r["column_name"]
            for r in conn.execute(
                """SELECT column_name FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name=? ORDER BY ordinal_position""",
                (table,),
            ).fetchall()
        ]

    def settings(self):
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT value FROM backend_system_settings WHERE key='backup_policy'"
            ).fetchone()
        return (
            json.loads(row["value"])
            if row
            else {"enabled": False, "interval_hours": 24, "keep_count": 10}
        )

    def preferences(self, actor, changes):
        require_permission(actor, "settings.manage")
        if (
            not changes
            or set(changes) - {"enabled", "interval_hours", "keep_count"}
            or ("enabled" in changes and type(changes["enabled"]) is not bool)
            or (
                "interval_hours" in changes
                and (
                    type(changes["interval_hours"]) is not int
                    or changes["interval_hours"] not in {6, 12, 24}
                )
            )
            or (
                "keep_count" in changes
                and (
                    type(changes["keep_count"]) is not int
                    or changes["keep_count"] not in {5, 10, 20}
                )
            )
        ):
            raise AccessDenied("invalid_input", 422)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            row = conn.execute(
                "SELECT value FROM backend_system_settings WHERE key='backup_policy'"
            ).fetchone()
            policy = (
                json.loads(row["value"])
                if row
                else {"enabled": False, "interval_hours": 24, "keep_count": 10}
            )
            policy.update(changes)
            conn.execute(
                "INSERT INTO backend_system_settings VALUES ('backup_policy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(policy),),
            )
        return policy

    def _path(self, backup_id):
        try:
            backup_id = str(UUID(backup_id))
        except (TypeError, ValueError, AttributeError):
            raise AccessDenied("invalid_input", 422) from None
        return self.root / f"{backup_id}.json"

    def _load(self, backup_id):
        path = self._path(backup_id)
        if path.is_symlink() or not path.is_file():
            raise AccessDenied("resource_not_found", 404)
        return self.decode_snapshot(path.read_bytes())

    @staticmethod
    def decode_snapshot(content):
        try:
            payload = json.loads(content)
            if set(payload) - {'name'} != {'format', 'created_at', 'trigger', 'app_version', 'checksum', 'tables'}:
                raise ValueError()
            if any(not isinstance(payload[key], str) or not payload[key] or len(payload[key]) > 255
                   for key in ('created_at', 'trigger', 'app_version', 'checksum')):
                raise ValueError()
            if 'name' in payload and (not isinstance(payload['name'], str) or len(payload['name']) > 128):
                raise ValueError()
            if datetime.fromisoformat(payload['created_at']).tzinfo is None:
                raise ValueError()
            legacy = payload['format'] == 'node-plane-backend-v1'
            from .grant_policies import TABLE_COLUMNS, canonical_region
            old_policy = payload['format'] in ('node-plane-backend-v1', 'node-plane-backend-v2')
            expected_tables = set(TABLES) - (set(TABLE_COLUMNS) if old_policy else set()) - ({'backend_devices'} if legacy else set())
            if payload['format'] not in ('node-plane-backend-v1', 'node-plane-backend-v2', 'node-plane-backend-v3') or set(payload['tables']) != expected_tables:
                raise ValueError()
            if (
                hashlib.sha256(encoded(payload["tables"])).hexdigest()
                != payload["checksum"]
            ):
                raise ValueError()
            for data in payload["tables"].values():
                if not isinstance(data["columns"], list) or not isinstance(
                    data["rows"], list
                ):
                    raise TypeError()
                if any(
                    not isinstance(row, dict) or set(row) != set(data["columns"])
                    for row in data["rows"]
                ):
                    raise ValueError()
            if legacy:
                # Verify the original snapshot checksum before conversion.
                # Keep that checksum as the restore confirmation identity.
                from .devices import DEVICE_COLUMNS, default_device
                awg_profiles = {row['profile_id'] for row in payload['tables']['backend_grants']['rows'] if row['protocol'] == 'awg'}
                payload['tables']['backend_devices'] = {
                    'columns': list(DEVICE_COLUMNS),
                    'rows': [default_device(row, payload['created_at'])
                             for row in payload['tables']['backend_profiles']['rows']
                             if row['id'] in awg_profiles],
                }
            if old_policy:
                from uuid import NAMESPACE_URL, uuid5
                regions = {}
                links = []
                for node in payload['tables']['backend_nodes']['rows']:
                    canonical = canonical_region(node['region'])
                    region_id = str(uuid5(NAMESPACE_URL, 'node-plane:region:' + canonical))
                    regions.setdefault(canonical, {'id': region_id, 'title': node['region'], 'canonical': canonical})
                    links.append({'node_key': node['key'], 'region_id': region_id})
                rows = {'backend_regions': list(regions.values()), 'backend_node_regions': links,
                        'backend_grant_policies': []}
                for table, columns in TABLE_COLUMNS.items():
                    payload['tables'][table] = {'columns': columns, 'rows': rows[table]}
            return payload
        except (ValueError, KeyError, TypeError, RecursionError):
            raise AccessDenied("backup_invalid", 409) from None

    def export_snapshot(self, actor, backup_id):
        require_permission(actor, 'settings.manage')
        self._load(backup_id)
        return self._path(backup_id).read_bytes()

    def import_snapshot(self, actor, content):
        require_permission(actor, 'settings.manage')
        if len(content) > 64 * 1024 * 1024:
            raise AccessDenied('backup_too_large', 413)
        value = self.decode_snapshot(content)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            account = conn.execute('SELECT role,status FROM backend_accounts WHERE id=?', (actor.account.id,)).fetchone()
            if account is None or account['role'] != 'admin' or account['status'] != 'approved':
                raise AccessDenied('permission_denied')
            # Serialize imports and deduplicate by snapshot content, independent
            # of the filename chosen on another controller.
            conn.execute("INSERT INTO backend_system_settings(key,value) VALUES ('backup_import_lock','1') ON CONFLICT(key) DO NOTHING")
            conn.execute("UPDATE backend_system_settings SET value=value WHERE key='backup_import_lock'")
            for path in self.root.glob('*.json'):
                try:
                    previous = self._load(path.stem)
                    if all(previous.get(field) == value.get(field) for field in ('checksum','trigger','name','created_at')):
                        return self.detail(actor, path.stem)
                except (AccessDenied, OSError):
                    continue
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            backup_id = str(uuid4())
            temp = self.root / f'.{backup_id}.tmp'
            try:
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, self._path(backup_id))
                fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            finally:
                temp.unlink(missing_ok=True)
        return self.detail(actor, backup_id)

    def detail(self, actor, backup_id):
        require_permission(actor, "settings.manage")
        value = self._load(backup_id)
        compatible = True
        with self.db.connect() as conn:
            for table in TABLES:
                compatible &= (
                    self._columns(conn, table) == value["tables"][table]["columns"]
                )
        return {
            "id": str(UUID(backup_id)),
            "created_at": value["created_at"],
            "app_version": value["app_version"],
            "trigger": value["trigger"],
            "kind": "manual" if value["trigger"] == "manual" else "automatic",
            "name": value.get("name", ""),
            "checksum": value["checksum"],
            "size_bytes": self._path(backup_id).stat().st_size,
            "compatible": compatible,
            "accounts": len(value["tables"]["backend_accounts"]["rows"]),
            "profiles": len(value["tables"]["backend_profiles"]["rows"]),
            "nodes": len(value["tables"]["backend_nodes"]["rows"]),
        }

    def delete(self, actor, backup_id, checksum):
        require_permission(actor, "settings.manage")
        path = self._path(backup_id)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            if conn.execute("SELECT 1 FROM backend_backup_jobs WHERE backup_id=? AND status IN ('awaiting_executor','running','blocked') LIMIT 1", (str(UUID(backup_id)),)).fetchone():
                raise AccessDenied("backup_in_use", 409)
            value = self._load(backup_id)
            if checksum != value["checksum"]:
                raise AccessDenied("backup_changed", 409)
            path.unlink()
        return {"deleted_backup_id": str(UUID(backup_id))}

    def catalog(self, actor, offset=0, limit=8):
        require_permission(actor, "settings.manage")
        entries = []
        if self.root.exists():
            for path in self.root.glob("*.json"):
                try:
                    value = self._load(path.stem)
                    entries.append(
                        {
                            "id": path.stem,
                            "created_at": value["created_at"],
                            "app_version": value["app_version"],
                            "trigger": value["trigger"],
                            "kind": "manual" if value["trigger"] == "manual" else "automatic",
                            "name": value.get("name", ""),
                            "size_bytes": path.stat().st_size,
                        }
                    )
                except (AccessDenied, OSError):
                    continue
        entries.sort(key=lambda v: v["created_at"], reverse=True)
        return {
            "items": entries[offset : offset + limit],
            "total": len(entries),
            "total_size_bytes": sum(v["size_bytes"] for v in entries),
            "offset": offset,
            "next_offset": offset + limit if offset + limit < len(entries) else None,
        }

    def overview(self, actor):
        page = self.catalog(actor)
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM backend_backup_jobs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return {
            **self.settings(),
            "count": page["total"],
            "size_bytes": page["total_size_bytes"],
            "latest": page["items"][0] if page["items"] else None,
            "last_job": self.get(actor, row["id"]) if row else None,
        }

    @staticmethod
    def busy(conn):
        if conn.execute("SELECT 1 FROM backend_temporary_configs WHERE status NOT IN ('expired','revoked','cancelled') LIMIT 1").fetchone():
            return True
        if conn.execute("SELECT 1 FROM backend_alert_deliveries WHERE status='claimed' LIMIT 1").fetchone():
            return True
        if conn.execute("SELECT 1 FROM backend_announcement_deliveries WHERE status IN ('queued','claimed') LIMIT 1").fetchone():
            return True
        # Retain failed installation attempts for audit. A verified retirement
        # confirms host cleanup; registry-only retirement explicitly abandons
        # that certainty and prevents replay. Neither is active controller work.
        if conn.execute("""SELECT 1 FROM backend_agent_rollouts a
            WHERE a.status IN ('awaiting_executor','running','blocked')
            AND NOT (a.status='blocked'
                AND NOT EXISTS (SELECT 1 FROM backend_nodes n WHERE n.key=a.node_key)
                AND EXISTS (SELECT 1 FROM backend_node_retirements r
                    WHERE r.node_key=a.node_key AND r.mode IN ('verified','registry_only'))) LIMIT 1""").fetchone():
            return True
        for table in (
            "backend_node_bootstraps",
            "backend_node_jobs",
            "backend_node_settings_tasks",
            "backend_operation_tasks",
            "backend_config_issuances",
            "backend_update_jobs",
        ):
            if conn.execute(
                f"SELECT 1 FROM {table} WHERE status IN ('awaiting_executor','running','blocked') LIMIT 1"
            ).fetchone():
                return True
        return bool(
            conn.execute(
                "SELECT 1 FROM backend_node_removals WHERE status IN ('queued','running','blocked') LIMIT 1"
            ).fetchone()
        )

    def queue(self, actor, key, action, backup_id=None, checksum=None, name=None):
        require_permission(actor, "settings.manage")
        try:
            key = str(UUID(key))
        except (TypeError, ValueError, AttributeError):
            raise AccessDenied("invalid_idempotency_key", 422) from None
        if action not in {"create", "restore"} or (
            action == "create" and (backup_id or checksum)
        ):
            raise AccessDenied("invalid_input", 422)
        if name is not None and (action != "create" or not isinstance(name, str) or len(name) > 128):
            raise AccessDenied("invalid_input", 422)
        name = (name or "").strip()
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            # This endpoint checks its own pending job below, allowing a retry
            # with the same idempotency key to return the existing restore.
            admit(conn, allow_restore=True)
            prior = conn.execute(
                "SELECT * FROM backend_backup_jobs WHERE actor_id=? AND command_key=?",
                (actor.account.id, key),
            ).fetchone()
            if prior:
                label = conn.execute("SELECT name FROM backend_backup_labels WHERE job_id=?", (prior["id"],)).fetchone()
                if (label["name"] if label else "") != name:
                    raise AccessDenied("idempotency_conflict", 409)
                if (prior["action"], prior["backup_id"], prior["checksum"]) != (
                    action,
                    backup_id,
                    checksum,
                ):
                    raise AccessDenied("idempotency_conflict", 409)
                return self._public(prior)
            if conn.execute(
                "SELECT 1 FROM backend_backup_jobs WHERE status IN ('awaiting_executor','running')"
            ).fetchone():
                raise AccessDenied("backup_pending", 409)
            if action == "restore":
                detail = self.detail(actor, backup_id)
                if not detail["compatible"] or detail["checksum"] != checksum:
                    raise AccessDenied("backup_incompatible", 409)
                if self.busy(conn):
                    raise AccessDenied("maintenance_busy", 409)
            job_id = str(uuid4())
            conn.execute(
                "INSERT INTO backend_backup_jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    actor.account.id,
                    key,
                    action,
                    backup_id,
                    checksum,
                    "awaiting_executor",
                    None,
                    None,
                    now(),
                ),
            )
            if name:
                conn.execute("INSERT INTO backend_backup_labels(job_id,name) VALUES (?,?)", (job_id, name))
            return {
                "id": job_id,
                "action": action,
                "status": "awaiting_executor",
                "result": None,
            }

    @staticmethod
    def _public(row):
        return {
            "id": row["id"],
            "action": row["action"],
            "status": row["status"],
            "phase": row["phase"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }

    def get(self, actor, job_id):
        require_permission(actor, "settings.manage")
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM backend_backup_jobs WHERE id=?", (job_id,)
            ).fetchone()
        if not row:
            raise AccessDenied("resource_not_found", 404)
        return self._public(row)

    def create_snapshot(self, trigger="manual", prune=True, name=""):
        from config import APP_VERSION

        with self.db.transaction() as conn:
            # One consistent PostgreSQL snapshot across every configuration table.
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            tables = {}
            deleted_accounts = {r['account_id'] for r in conn.execute('SELECT account_id FROM backend_account_deletions').fetchall()}
            deleted_profiles = {r['id'] for r in conn.execute('SELECT id,owner_account_id FROM backend_profiles').fetchall() if r['owner_account_id'] in deleted_accounts}
            for table in TABLES:
                columns = self._columns(conn, table)
                if not columns:
                    raise AccessDenied("backup_schema_missing", 503)
                rows = [
                    dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()
                ]
                if table == "backend_accounts":
                    rows = [r for r in rows if r['id'] not in deleted_accounts]
                elif table == "backend_profiles":
                    rows = [r for r in rows if r['id'] not in deleted_profiles]
                elif table in {"backend_external_identities", "backend_access_requests"}:
                    rows = [r for r in rows if r['account_id'] not in deleted_accounts]
                if table == "backend_access_requests":
                    rows = [{**r,'decided_by':None} if r.get('decided_by') in deleted_accounts else r for r in rows]
                elif table in {"backend_devices", "backend_profile_identities", "backend_grants", "backend_grant_policies"}:
                    rows = [r for r in rows if r['profile_id'] not in deleted_profiles]
                if table == "backend_system_settings":
                    rows = [r for r in rows if r["key"] not in {
                        "backup_last_scheduled", "traffic_last_scan", "traffic_cursor",
                        "traffic_generation"
                    } and not r["key"].startswith(("traffic_consent:", "traffic_consent_generation:", "telegram_retry_until:"))]
                rows.sort(key=lambda row: encoded(row))
                tables[table] = {"columns": columns, "rows": rows}
        checksum = hashlib.sha256(encoded(tables)).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in sorted(
            self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:1]:
            try:
                previous = self._load(path.stem)
                if trigger != "manual" and previous["trigger"] != "manual" and previous["checksum"] == checksum:
                    return {"status": "duplicate", "backup_id": path.stem}
            except AccessDenied:
                pass
        backup_id = str(uuid4())
        payload = {
            "format": "node-plane-backend-v3",
            "created_at": now(),
            "trigger": trigger,
            "app_version": APP_VERSION,
            "checksum": checksum,
            "tables": tables,
        }
        if name:
            payload["name"] = name
        temp = self.root / f".{backup_id}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self._path(backup_id))
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if prune:
            files = []
            with self.db.connect() as conn:
                in_use = {row['backup_id'] for row in conn.execute("SELECT backup_id FROM backend_backup_jobs WHERE status IN ('awaiting_executor','running','blocked')").fetchall()}
            for path in self.root.glob("*.json"):
                try:
                    value = self._load(path.stem)
                    if value['trigger'] != 'manual':
                        files.append((value['created_at'], path))
                except (AccessDenied, OSError):
                    continue
            files.sort(key=lambda item: item[0], reverse=True)
            for _, path in files[self.settings()["keep_count"] :]:
                if path.stem not in in_use:
                    path.unlink(missing_ok=True)
        return {"status": "success", "backup_id": backup_id}

    def _restore(self, job, actor):
        payload = self._load(job["backup_id"])
        if (
            payload["checksum"] != job["checksum"]
            or not self.detail(actor, job["backup_id"])["compatible"]
        ):
            raise AccessDenied("backup_incompatible", 409)
        with self.db.transaction() as conn:
            names = ",".join(TABLES + CLEAR + ("backend_credentials",))
            conn.execute(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE")
            current = dict(
                conn.execute(
                    "SELECT * FROM backend_accounts WHERE id=?", (actor.account.id,)
                ).fetchone()
            )
            identities = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM backend_external_identities WHERE account_id=?",
                    (actor.account.id,),
                ).fetchall()
            ]
            revisions = {
                r["key"]: r["desired_revision"]
                for r in conn.execute(
                    "SELECT key,desired_revision FROM backend_nodes"
                ).fetchall()
            }
            profile_revisions = {
                r["id"]: r["desired_revision"]
                for r in conn.execute(
                    "SELECT id,desired_revision FROM backend_profiles"
                ).fetchall()
            }
            tables = payload["tables"]
            remap = {}
            for identity in identities:
                conflict = next((row for row in tables['backend_external_identities']['rows']
                    if (row['provider'], row['subject']) == (identity['provider'], identity['subject'])), None)
                if conflict and conflict['account_id'] != actor.account.id:
                    old = next((row for row in tables['backend_accounts']['rows']
                        if row['id'] == conflict['account_id']), None)
                    if identity['provider'] != 'telegram' or not old or old['role'] != 'admin' or old['status'] != 'approved':
                        raise AccessDenied('backup_identity_conflict', 409)
                    remap[old['id']] = actor.account.id
            if len(remap) > 1:
                raise AccessDenied('backup_identity_conflict', 409)
            # A freshly installed controller gives the same Telegram operator a
            # new account UUID. Keep current authentication and restore ownership
            # from that operator's former administrator account.
            for data in tables.values():
                for row in data['rows']:
                    for key in ('account_id', 'owner_account_id', 'decided_by'):
                        if row.get(key) in remap:
                            row[key] = remap[row[key]]
            recovery = [{"node_key": row["key"], "status": "pending"}
                        for row in tables["backend_nodes"]["rows"] if row["enabled"]]
            # Preserve the restoring administrator and its Telegram mapping so
            # restore cannot lock out the current installation operator.
            tables["backend_accounts"]["rows"] = [
                r
                for r in tables["backend_accounts"]["rows"]
                if r["id"] != actor.account.id and r['id'] not in remap
            ] + [current]
            for identity in identities:
                conflict = next(
                    (
                        r
                        for r in tables["backend_external_identities"]["rows"]
                        if (r["provider"], r["subject"])
                        == (identity["provider"], identity["subject"])
                    ),
                    None,
                )
                if conflict and conflict["account_id"] != actor.account.id:
                    raise AccessDenied("backup_identity_conflict", 409)
                if not conflict:
                    tables["backend_external_identities"]["rows"].append(identity)
            conn.execute("DELETE FROM backend_credentials WHERE account_id IS NOT NULL")
            for table in CLEAR + tuple(reversed(TABLES)):
                conn.execute(f"DELETE FROM {table}")
            for table in TABLES:
                columns = tables[table]["columns"]
                for row in tables[table]["rows"]:
                    if table == "backend_nodes":
                        row.update(
                            enabled=0,
                            applied_revision=0,
                            desired_revision=max(
                                row["desired_revision"], revisions.get(row["key"], 0)
                            )
                            + 1,
                        )
                    if table == "backend_profiles":
                        row["desired_revision"] = (
                            max(
                                row["desired_revision"],
                                profile_revisions.get(row["id"], 0),
                            )
                            + 1
                        )
                    conn.execute(
                        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                        tuple(row[c] for c in columns),
                    )
            conn.execute(
                "UPDATE backend_account_guard SET revision=revision+1 WHERE id=1"
            )
            conn.execute(
                "UPDATE backend_backup_jobs SET status=?,phase=?,result_json=? WHERE id=?",
                (
                    "running" if recovery else "succeeded",
                    "reconnecting" if recovery else "restored",
                    json.dumps({"status": "recovering" if recovery else "success",
                                "backup_id": job["backup_id"], "nodes": recovery}),
                    job["id"],
                ),
            )

    def _recover_nodes(self, job, actor):
        """Persist one recovery transition at a time, adopting queued children after a crash."""
        from .node_bootstrap import NodeBootstrapService
        result = json.loads(job["result_json"])
        changed = False
        for item in result["nodes"]:
            if item["status"] in {"succeeded", "failed"}:
                continue
            if not item.get("job_id"):
                key = str(uuid5(UUID(job["id"]), item["node_key"]))
                with self.db.connect() as conn:
                    node = conn.execute("SELECT desired_revision FROM backend_nodes WHERE key=?", (item["node_key"],)).fetchone()
                    recorded = conn.execute("SELECT id FROM backend_node_bootstraps WHERE actor_id=? AND command_key=?",
                        (actor.account.id, key)).fetchone()
                if recorded:
                    item.update(status="running", job_id=recorded["id"])
                elif not node:
                    item.update(status="failed", error_code="resource_not_found")
                else:
                    try:
                        child = NodeBootstrapService(self.db, self.driver).queue(actor,
                            item["node_key"], node["desired_revision"], key, recover_unreachable=True)
                        item.update(status="running", job_id=child["id"])
                    except AccessDenied as error:
                        item.update(status="failed", error_code=error.code)
                changed = True
                break
            with self.db.connect() as conn:
                child = conn.execute("SELECT status,error_code FROM backend_node_bootstraps WHERE id=?", (item["job_id"],)).fetchone()
            if not child or child["status"] in {"blocked", "superseded"}:
                item.update(status="failed", error_code=child["error_code"] if child else "child_operation_missing")
                changed = True
            elif child["status"] == "succeeded":
                item["status"] = "succeeded"
                changed = True
        complete = all(item["status"] in {"succeeded", "failed"} for item in result["nodes"])
        if complete:
            result["status"] = "partial" if any(item["status"] == "failed" for item in result["nodes"]) else "success"
        if changed or complete:
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_backup_jobs SET status=?,phase=?,result_json=? WHERE id=?",
                    ("succeeded" if complete else "running", "restored" if complete else "reconnecting", json.dumps(result), job["id"]))
            return True
        return False

    def run_one(self):
        with self.db.connect() as conn:
            job = conn.execute(
                "SELECT * FROM backend_backup_jobs WHERE status IN ('awaiting_executor','running') ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not job:
                return False
            account = conn.execute(
                "SELECT role,status FROM backend_accounts WHERE id=?",
                (job["actor_id"],),
            ).fetchone()
        actor = (
            Actor(
                Principal("backup-worker", PrincipalKind.SERVICE, ADMIN_PERMISSIONS),
                Account(job["actor_id"], account["role"], account["status"]),
            )
            if account
            else None
        )
        try:
            if (
                not actor
                or actor.account.role != "admin"
                or actor.account.status != "approved"
            ):
                raise AccessDenied("permission_denied")
            if job["action"] == "create":
                with self.db.connect() as conn:
                    label = conn.execute("SELECT name FROM backend_backup_labels WHERE job_id=?", (job["id"],)).fetchone()
                result = self.create_snapshot(name=label["name"] if label else "")
                with self.db.transaction() as conn:
                    conn.execute(
                        "UPDATE backend_backup_jobs SET status='succeeded',result_json=? WHERE id=?",
                        (json.dumps(result), job["id"]),
                    )
                return True
            if job["phase"] == "reconnecting":
                return self._recover_nodes(job, actor)
            if not job["phase"]:
                detail = self.detail(actor, job["backup_id"])
                if not detail["compatible"] or detail["checksum"] != job["checksum"]:
                    raise AccessDenied("backup_incompatible", 409)
                # Preserve the target even when retention would normally remove it.
                self.create_snapshot("pre_restore", prune=False)
                with self.db.transaction() as conn:
                    if self.busy(conn):
                        raise AccessDenied("maintenance_busy", 409)
                    for profile in conn.execute(
                        "SELECT id FROM backend_profiles"
                    ).fetchall():
                        conn.execute(
                            "UPDATE backend_profiles SET frozen=1,desired_revision=desired_revision+1 WHERE id=?",
                            (profile["id"],),
                        )
                        operation = OperationRepository.record(
                            conn,
                            actor,
                            profile["id"],
                            OperationRepository.targets(conn, profile["id"]),
                        )
                        if operation["status"] != "no_targets":
                            conn.execute(
                                "INSERT INTO backend_backup_revocations VALUES (?,?)",
                                (job["id"], operation["id"]),
                            )
                    conn.execute(
                        "UPDATE backend_backup_jobs SET status='running',phase='revoking' WHERE id=?",
                        (job["id"],),
                    )
                return True
            with self.db.connect() as conn:
                states = {
                    r["status"]
                    for r in conn.execute(
                        """SELECT o.status FROM backend_operations o JOIN backend_backup_revocations b ON b.operation_id=o.id WHERE b.job_id=?""",
                        (job["id"],),
                    ).fetchall()
                }
            if states & {"blocked", "superseded"}:
                raise AccessDenied("backup_revocations_failed", 409)
            if states - {"succeeded", "no_targets"}:
                return False
            self._restore(job, actor)
            return True
        except Exception as exc:  # noqa: BLE001 -- persist a sanitized worker failure
            code = exc.code if isinstance(exc, AccessDenied) else "backup_failed"
            logging.getLogger(__name__).error(
                'Backup operation blocked: job=%s action=%s phase=%s code=%s type=%s sqlstate=%s',
                job['id'], job['action'], job['phase'], code, type(exc).__name__,
                getattr(exc, 'sqlstate', None))
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE backend_backup_jobs SET status='blocked',result_json=? WHERE id=?",
                    (json.dumps({"status": "failed", "code": code}), job["id"]),
                )
            return True

    def scheduled(self):
        policy = self.settings()
        if not policy["enabled"]:
            return
        with self.db.connect() as conn:
            last = conn.execute(
                "SELECT created_at FROM backend_backup_jobs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            scheduled = conn.execute(
                "SELECT value FROM backend_system_settings WHERE key='backup_last_scheduled'"
            ).fetchone()
            if conn.execute(
                "SELECT 1 FROM backend_backup_jobs WHERE status IN ('awaiting_executor','running')"
            ).fetchone():
                return
        dates = [
            datetime.fromisoformat(v)
            for v in (
                last["created_at"] if last else None,
                scheduled["value"] if scheduled else None,
            )
            if v
        ]
        if dates and max(dates) > datetime.now(timezone.utc) - timedelta(
            hours=policy["interval_hours"]
        ):
            return
        self.create_snapshot("scheduled")
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO backend_system_settings VALUES ('backup_last_scheduled',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (now(),),
            )
