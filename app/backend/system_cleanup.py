"""Strongly confirmed, durable reset/uninstall of the backend-owned stack."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from .authorization import (
    ADMIN_PERMISSIONS,
    AccessDenied,
    Account,
    Actor,
    Principal,
    PrincipalKind,
    require_permission,
)
from .backups import CLEAR, TABLES, BackupService
from .installation_maintenance import InstallationMaintenance
from .maintenance_gate import active
from .node_removal import NodeRemovalService

PHRASES = {"reset": "RESET NODE PLANE", "remove": "REMOVE NODE PLANE"}


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class SystemCleanupService:
    def __init__(self, db, driver=None, host=None, backups=None, removals=None):
        self.db, self.driver = db, driver
        self.host = host or InstallationMaintenance()
        self.backups = backups or BackupService(db)
        self.removals = removals or NodeRemovalService(db, driver)

    def initialize_schema(self):
        with self.db.transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_controller_update_gate (
                id INTEGER PRIMARY KEY, job_id TEXT NOT NULL)''')
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_system_cleanup_plans (
                id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, principal_id TEXT NOT NULL,
                intent_json TEXT NOT NULL, expires_at TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_system_cleanup_jobs (
                id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, principal_id TEXT NOT NULL,
                command_key TEXT NOT NULL, plan_id TEXT NOT NULL, intent_json TEXT NOT NULL,
                status TEXT NOT NULL, phase TEXT NOT NULL, backup_id TEXT,
                shutdown_ack INTEGER NOT NULL DEFAULT 0, local_plan_json TEXT,
                error_code TEXT, created_at TEXT NOT NULL,
                UNIQUE(principal_id,actor_id,command_key), UNIQUE(plan_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS backend_system_cleanup_items (
                job_id TEXT NOT NULL, node_key TEXT NOT NULL, status TEXT NOT NULL,
                error_code TEXT, PRIMARY KEY(job_id,node_key))""")

    @staticmethod
    def _busy(conn):
        for table in (
            "backend_operation_tasks",
            "backend_node_settings_tasks",
            "backend_node_jobs",
            "backend_agent_rollouts",
            "backend_config_issuances",
            "backend_backup_jobs",
            "backend_update_jobs",
        ):
            if conn.execute(
                f"SELECT 1 FROM {table} WHERE status IN ('awaiting_executor','running') LIMIT 1"
            ).fetchone():
                return True
        if conn.execute(
            "SELECT 1 FROM backend_node_removals WHERE status IN ('queued','running') LIMIT 1"
        ).fetchone():
            return True
        for table in ("backend_alert_deliveries", "backend_announcement_deliveries"):
            if conn.execute(
                f"SELECT 1 FROM {table} WHERE status='claimed' LIMIT 1"
            ).fetchone():
                return True
        return False

    @staticmethod
    def _fingerprint(conn):
        values = {}
        for table in (
            "backend_accounts",
            "backend_profiles",
            "backend_grants",
            "backend_nodes",
            "backend_node_connections",
        ):
            values[table] = sorted(
                [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()],
                key=encoded,
            )
        return hashlib.sha256(encoded(values).encode()).hexdigest()

    @staticmethod
    def _fresh_actor(conn, actor):
        row = conn.execute(
            "SELECT role,status FROM backend_accounts WHERE id=?", (actor.account.id,)
        ).fetchone()
        if not row:
            raise AccessDenied("permission_denied")
        current = Actor(
            actor.principal, Account(actor.account.id, row["role"], row["status"])
        )
        require_permission(current, "maintenance.manage")
        return current

    def overview(self, actor):
        require_permission(actor, "maintenance.manage")
        try:
            deployment, reason = self.host.deployment(), None
        except AccessDenied as exc:
            deployment, reason = None, exc.code
        with self.db.connect() as conn:
            latest = conn.execute(
                "SELECT id FROM backend_system_cleanup_jobs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            counts = {
                name: conn.execute(
                    f"SELECT COUNT(*) AS n FROM backend_{name}"
                ).fetchone()["n"]
                for name in ("accounts", "profiles", "nodes")
            }
        return {
            "supported": deployment is not None,
            "reason": reason,
            "deployment": deployment,
            "counts": counts,
            "latest_job": self.get(actor, latest["id"]) if latest else None,
        }

    def plan(self, actor, action, cleanup_nodes):
        require_permission(actor, "maintenance.manage")
        if action not in PHRASES or type(cleanup_nodes) is not bool:
            raise AccessDenied("invalid_input", 422)
        deployment = self.host.deployment()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE backend_account_guard SET revision=revision+1 WHERE id=1"
            )
            self._fresh_actor(conn, actor)
            if active(conn) or self._busy(conn):
                raise AccessDenied("maintenance_busy", 409)
            nodes = [
                dict(r)
                for r in conn.execute("""SELECT n.key,n.title,c.transport,c.ssh_target
                FROM backend_nodes n LEFT JOIN backend_node_connections c ON c.node_key=n.key ORDER BY n.key""").fetchall()
            ]
            if cleanup_nodes:
                for node in nodes:
                    if node["transport"] not in {"local", "ssh"}:
                        raise AccessDenied("verification_target_required", 409)
                    self.removals.verifier(
                        "local" if node["transport"] == "local" else node["ssh_target"]
                    )
            intent = {
                "action": action,
                "cleanup_nodes": cleanup_nodes,
                "deployment": deployment,
                "nodes": nodes,
                "fingerprint": self._fingerprint(conn),
            }
            plan_id = str(uuid4())
            expires_at = (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat()
            conn.execute(
                "INSERT INTO backend_system_cleanup_plans VALUES (?,?,?,?,?)",
                (
                    plan_id,
                    actor.account.id,
                    actor.principal.id,
                    encoded(intent),
                    expires_at,
                ),
            )
        return {
            "id": plan_id,
            "action": action,
            "cleanup_nodes": cleanup_nodes,
            "confirmation_phrase": PHRASES[action],
            "expires_at": expires_at,
            "deployment": deployment,
            "nodes": nodes,
        }

    def queue(self, actor, plan_id, phrase, key):
        require_permission(actor, "maintenance.manage")
        try:
            key = str(UUID(key))
        except (ValueError, TypeError, AttributeError):
            raise AccessDenied("invalid_idempotency_key", 422) from None
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE backend_account_guard SET revision=revision+1 WHERE id=1"
            )
            self._fresh_actor(conn, actor)
            plan = conn.execute(
                "SELECT * FROM backend_system_cleanup_plans WHERE id=? AND actor_id=? AND principal_id=?",
                (plan_id, actor.account.id, actor.principal.id),
            ).fetchone()
            if not plan:
                raise AccessDenied("resource_not_found", 404)
            intent = json.loads(plan["intent_json"])
            if phrase != PHRASES[intent["action"]]:
                raise AccessDenied("confirmation_mismatch", 422)
            prior = conn.execute(
                "SELECT * FROM backend_system_cleanup_jobs WHERE principal_id=? AND actor_id=? AND command_key=?",
                (actor.principal.id, actor.account.id, key),
            ).fetchone()
            if prior:
                if prior["plan_id"] != plan_id:
                    raise AccessDenied("idempotency_conflict", 409)
                return self._public(conn, prior)
            consumed = conn.execute(
                "SELECT * FROM backend_system_cleanup_jobs WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if consumed:
                return self._public(conn, consumed)
            if plan["expires_at"] <= now():
                raise AccessDenied("cleanup_plan_expired", 409)
            if active(conn) or self._busy(conn):
                raise AccessDenied("maintenance_busy", 409)
            if (
                self._fingerprint(conn) != intent["fingerprint"]
                or self.host.deployment() != intent["deployment"]
            ):
                raise AccessDenied("cleanup_plan_changed", 409)
            job_id = str(uuid4())
            conn.execute(
                """INSERT INTO backend_system_cleanup_jobs
                (id,actor_id,principal_id,command_key,plan_id,intent_json,status,phase,created_at)
                VALUES (?,?,?,?,?,?,'queued','backup',?)""",
                (
                    job_id,
                    actor.account.id,
                    actor.principal.id,
                    key,
                    plan_id,
                    plan["intent_json"],
                    now(),
                ),
            )
            for node in intent["nodes"] if intent["cleanup_nodes"] else []:
                conn.execute(
                    "INSERT INTO backend_system_cleanup_items VALUES (?,?,'pending',NULL)",
                    (job_id, node["key"]),
                )
            return self._public(
                conn,
                conn.execute(
                    "SELECT * FROM backend_system_cleanup_jobs WHERE id=?", (job_id,)
                ).fetchone(),
            )

    @staticmethod
    def _public(conn, row):
        intent = json.loads(row["intent_json"])
        items = [
            dict(r)
            for r in conn.execute(
                "SELECT node_key,status,error_code FROM backend_system_cleanup_items WHERE job_id=? ORDER BY node_key",
                (row["id"],),
            ).fetchall()
        ]
        return {
            key: row[key]
            for key in (
                "id",
                "status",
                "phase",
                "backup_id",
                "error_code",
                "created_at",
            )
        } | {
            "action": intent["action"],
            "cleanup_nodes": intent["cleanup_nodes"],
            "items": items,
            "shutdown_ack": bool(row["shutdown_ack"]),
        }

    def get(self, actor, job_id):
        require_permission(actor, "maintenance.manage")
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM backend_system_cleanup_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                raise AccessDenied("resource_not_found", 404)
            return self._public(conn, row)

    def acknowledge_shutdown(self, actor, job_id):
        require_permission(actor, "maintenance.manage")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE backend_account_guard SET revision=revision+1 WHERE id=1"
            )
            self._fresh_actor(conn, actor)
            row = conn.execute(
                "SELECT * FROM backend_system_cleanup_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if (
                not row
                or row["actor_id"] != actor.account.id
                or row["principal_id"] != actor.principal.id
            ):
                raise AccessDenied("resource_not_found", 404)
            if row["status"] != "awaiting_shutdown":
                raise AccessDenied("cleanup_not_ready", 409)
            conn.execute(
                "UPDATE backend_system_cleanup_jobs SET shutdown_ack=1 WHERE id=?",
                (job_id,),
            )
        return self.get(actor, job_id)

    def retry(self, actor, job_id):
        require_permission(actor, "maintenance.manage")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE backend_account_guard SET revision=revision+1 WHERE id=1"
            )
            self._fresh_actor(conn, actor)
            row = conn.execute(
                "SELECT * FROM backend_system_cleanup_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if (
                not row
                or row["actor_id"] != actor.account.id
                or row["principal_id"] != actor.principal.id
            ):
                raise AccessDenied("resource_not_found", 404)
            if row["status"] != "blocked" or row["phase"] == "uninstall_launch":
                raise AccessDenied("cleanup_retry_unsafe", 409)
            conn.execute(
                "UPDATE backend_system_cleanup_jobs SET status='running',error_code=NULL WHERE id=?",
                (job_id,),
            )
        try:
            if row["phase"] == "nodes":
                for item in self.get(actor, job_id)["items"]:
                    if item["status"] == "blocked":
                        self.removals.request(actor, item["node_key"], retry=True)
                        with self.db.transaction() as conn:
                            conn.execute(
                                "UPDATE backend_system_cleanup_items SET status='pending',error_code=NULL WHERE job_id=? AND node_key=?",
                                (job_id, item["node_key"]),
                            )
        except Exception:
            self._set(job_id, status="blocked", error="node_cleanup_unverified")
            raise
        return self.get(actor, job_id)

    def abort(self, actor, job_id):
        require_permission(actor, "maintenance.manage")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE backend_account_guard SET revision=revision+1 WHERE id=1"
            )
            self._fresh_actor(conn, actor)
            row = conn.execute(
                "SELECT * FROM backend_system_cleanup_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if (
                not row
                or row["actor_id"] != actor.account.id
                or row["principal_id"] != actor.principal.id
            ):
                raise AccessDenied("resource_not_found", 404)
            if row["status"] not in {"blocked", "awaiting_shutdown"} or row[
                "phase"
            ] in {"local_files", "uninstall_launch"}:
                raise AccessDenied("cleanup_abort_unsafe", 409)
            conn.execute(
                """UPDATE backend_node_removals SET status='blocked',error_code='system_cleanup_aborted'
                WHERE node_key IN (SELECT node_key FROM backend_system_cleanup_items WHERE job_id=?)
                AND status IN ('queued','running')""",
                (job_id,),
            )
            conn.execute(
                "UPDATE backend_system_cleanup_jobs SET status='aborted',error_code=NULL WHERE id=?",
                (job_id,),
            )
        if row["local_plan_json"]:
            self.host.discard_uninstall(json.loads(row["local_plan_json"]))
        return self.get(actor, job_id)

    def _set(self, job_id, *, status="running", phase=None, error=None):
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE backend_system_cleanup_jobs SET status=?,phase=COALESCE(?,phase),error_code=? WHERE id=?",
                (status, phase, error, job_id),
            )

    def _wipe_state(self, job, preserve_operator):
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE backend_account_guard SET revision=revision+1 WHERE id=1"
            )
            if getattr(self.db, "backend_name", "") == "postgres":
                names = ",".join(
                    TABLES
                    + CLEAR
                    + (
                        "backend_credentials",
                        "backend_backup_jobs",
                        "backend_backup_revocations",
                    )
                )
                conn.execute(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE")
            actor_id = job["actor_id"]
            kept = {}
            if preserve_operator:
                kept["backend_accounts"] = [
                    dict(
                        conn.execute(
                            "SELECT * FROM backend_accounts WHERE id=?", (actor_id,)
                        ).fetchone()
                    )
                ]
                kept["backend_external_identities"] = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM backend_external_identities WHERE account_id=?",
                        (actor_id,),
                    ).fetchall()
                ]
                subjects = [
                    r["subject"]
                    for r in kept["backend_external_identities"]
                    if r["provider"] == "telegram"
                ]
                kept["backend_telegram_identity_details"] = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM backend_telegram_identity_details"
                    ).fetchall()
                    if r["subject"] in subjects
                ]
                kept["backend_credentials"] = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM backend_credentials WHERE id=?",
                        (job["principal_id"],),
                    ).fetchall()
                ]
            # Retained reference tables may exist on upgraded installations.
            # Clear only the explicit Node Plane inventory, never arbitrary tables.
            if getattr(self.db, "backend_name", "") == "postgres":
                from db.runtime_tables import TABLE_COLUMNS, _generic_table_exists

                legacy = [name for name, _ in TABLE_COLUMNS] + ["alert_state"]
                present = [name for name in legacy if _generic_table_exists(conn, name)]
                if present:
                    conn.execute(
                        "LOCK TABLE " + ",".join(present) + " IN ACCESS EXCLUSIVE MODE"
                    )
                    for table in reversed(present):
                        conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM backend_credentials")
            clear_tables = tuple(
                table
                for table in CLEAR
                if not table.startswith("backend_system_cleanup_")
            )
            for table in (
                clear_tables
                + ("backend_backup_revocations", "backend_backup_jobs")
                + tuple(reversed(TABLES))
            ):
                conn.execute(f"DELETE FROM {table}")
            for table in (
                "backend_accounts",
                "backend_external_identities",
                "backend_telegram_identity_details",
                "backend_credentials",
            ):
                for row in kept.get(table, []):
                    columns = list(row)
                    conn.execute(
                        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                        tuple(row[c] for c in columns),
                    )
            # Keep the cleanup journal so retries cannot admit this destructive
            # command again after a successful reset. All unused plans expire.
            conn.execute(
                "DELETE FROM backend_system_cleanup_plans WHERE id<>?",
                (job["plan_id"],),
            )
            conn.execute(
                "UPDATE backend_system_cleanup_jobs SET phase=?,status='running' WHERE id=?",
                ("local_files" if preserve_operator else "uninstall_launch", job["id"]),
            )

    def run_one(self):
        with self.db.connect() as conn:
            jobs = conn.execute(
                "SELECT * FROM backend_system_cleanup_jobs WHERE status IN ('queued','running','awaiting_shutdown') ORDER BY created_at"
            ).fetchall()
        for job in jobs:
            try:
                with self.db.connect() as conn:
                    account = conn.execute(
                        "SELECT role,status FROM backend_accounts WHERE id=?",
                        (job["actor_id"],),
                    ).fetchone()
                # After full-remove state wipe there is no human account. The
                # saved launch intent must be reconciled, never automatically replayed.
                if job["phase"] == "uninstall_launch":
                    self._set(
                        job["id"], status="blocked", error="uninstall_launch_uncertain"
                    )
                    return True
                if not account:
                    raise AccessDenied("permission_denied")
                actor = Actor(
                    Principal(
                        job["principal_id"], PrincipalKind.SERVICE, ADMIN_PERMISSIONS
                    ),
                    Account(job["actor_id"], account["role"], account["status"]),
                )
                require_permission(actor, "maintenance.manage")
                intent = json.loads(job["intent_json"])
                if self.host.deployment() != intent["deployment"]:
                    raise AccessDenied("installation_manifest_mismatch", 409)
                if job["phase"] == "backup":
                    backup = self.backups.create_snapshot(
                        "pre_" + intent["action"], prune=False
                    )
                    with self.db.transaction() as conn:
                        conn.execute(
                            "UPDATE backend_system_cleanup_jobs SET status='running',phase='nodes',backup_id=? WHERE id=?",
                            (backup["backup_id"], job["id"]),
                        )
                    return True
                if job["phase"] == "nodes":
                    items = self.get(actor, job["id"])["items"]
                    for item in items:
                        if item["status"] == "succeeded":
                            continue
                        removal = self.removals.request(actor, item["node_key"])
                        status = removal.get("removal_status", removal["status"])
                        if status in {"blocked", "removed_registry_only"}:
                            with self.db.transaction() as conn:
                                conn.execute(
                                    "UPDATE backend_system_cleanup_items SET status='blocked',error_code=? WHERE job_id=? AND node_key=?",
                                    (
                                        removal.get("error_code")
                                        or "node_cleanup_unverified",
                                        job["id"],
                                        item["node_key"],
                                    ),
                                )
                            raise AccessDenied("node_cleanup_unverified", 409)
                        if status == "removed":
                            with self.db.transaction() as conn:
                                conn.execute(
                                    "UPDATE backend_system_cleanup_items SET status='succeeded',error_code=NULL WHERE job_id=? AND node_key=?",
                                    (job["id"], item["node_key"]),
                                )
                            return True
                    if any(item["status"] != "succeeded" for item in items):
                        return False
                    self._set(
                        job["id"],
                        phase="local_state"
                        if intent["action"] == "reset"
                        else "uninstall_prepare",
                    )
                    return True
                if job["phase"] == "local_state":
                    self._wipe_state(job, True)
                    return True
                if job["phase"] == "local_files":
                    self.host.clear_local(intent["deployment"], job["backup_id"])
                    self._set(job["id"], status="succeeded", phase="complete")
                    return True
                if job["phase"] == "uninstall_prepare":
                    plan = self.host.prepare_uninstall(intent["deployment"], job["id"])
                    with self.db.transaction() as conn:
                        conn.execute(
                            "UPDATE backend_system_cleanup_jobs SET status='awaiting_shutdown',phase='shutdown',local_plan_json=? WHERE id=?",
                            (encoded(plan), job["id"]),
                        )
                    return True
                if job["phase"] == "shutdown" and job["shutdown_ack"]:
                    self._wipe_state(job, False)
                    # Once entered, even an exception from systemd-run is an
                    # uncertain launch, and must never cause an automatic replay.
                    self.host.launch_uninstall(json.loads(job["local_plan_json"]))
                    # Accepted detach needs no controller data left on an
                    # external PostgreSQL server. Completion belongs to systemd.
                    with self.db.transaction() as conn:
                        conn.execute("DELETE FROM backend_system_cleanup_items")
                        conn.execute("DELETE FROM backend_system_cleanup_jobs")
                        conn.execute("DELETE FROM backend_system_cleanup_plans")
                    return True
            except Exception as exc:
                code = (
                    exc.code
                    if isinstance(exc, AccessDenied)
                    else "system_cleanup_failed"
                )
                self._set(job["id"], status="blocked", error=code)
                return True
        return False
