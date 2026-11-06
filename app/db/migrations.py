"""Explicit PostgreSQL migrations; application rollback never downgrades the DB."""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from .migration_revisions import r0001_baseline, r0002_node_traffic, r0003_temporary_configs, r0004_node_bootstrap, r0005_recovery_audit, r0006_announcement_inbox, r0007_account_menu_refresh, r0008_access_decision_delivery, r0009_backup_labels, r0010_audit_descriptions, r0011_account_deletions, r0012_system_notifications, r0013_agent_rollout_cancellation, r0014_preapproval_visitors, r0015_vpn_policies, r0016_service_notifications, r0017_device_credentials, r0018_connection_limits, r0019_retired_peers, r0020_traffic_quotas, r0021_speed_limits, r0022_tariffs, r0023_renewal_requests, r0024_revocation_modes, r0025_agent_enrollments, r0026_agent_ssh_installs, r0027_node_agent_checks, r0028_account_details, r0029_account_channels, r0030_inbox_without_telegram, r0031_request_services, r0032_agent_installations, r0033_service_instances, r0034_task_services, r0035_roles, r0036_service_profiles, r0037_traffic_inbounds, r0038_relays, r0039_remote_nodes, r0040_logins, r0041_sessions, r0042_passkeys, r0043_invites, r0044_registrations, r0045_telegram_sign_in, r0046_account_merges, r0047_localized_texts, r0048_announcement_titles, r0049_metrics_history, r0050_web_cabinet

LOCK_KEY = 0x4E504C414E454442  # NPLANEDB; shared by every controller/schema.
LEDGER_DDL = '''CREATE TABLE IF NOT EXISTS backend_schema_revisions (
    revision INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL,
    minimum_reader INTEGER NOT NULL, applied_at TEXT NOT NULL)'''


class MigrationError(RuntimeError):
    """Sanitized deployment failure, safe to display without credentials or SQL."""


@dataclass(frozen=True)
class Revision:
    number: int
    name: str
    module: object
    minimum_reader: int

    @property
    def checksum(self):
        # Hash frozen source, including data transformations, not live repositories.
        return sha256(Path(self.module.__file__).read_bytes()).hexdigest()


REVISIONS = (
    Revision(1, 'baseline', r0001_baseline, 1),
    Revision(2, 'node_traffic', r0002_node_traffic, 1),
    Revision(3, 'temporary_configs', r0003_temporary_configs, 1),
    Revision(4, 'node_bootstrap', r0004_node_bootstrap, 1),
    Revision(5, 'recovery_audit', r0005_recovery_audit, 1),
    Revision(6, 'announcement_inbox', r0006_announcement_inbox, 1),
    Revision(7, 'account_menu_refresh', r0007_account_menu_refresh, 1),
    Revision(8, 'access_decision_delivery', r0008_access_decision_delivery, 1),
    Revision(9, 'backup_labels', r0009_backup_labels, 1),
    Revision(10, 'audit_descriptions', r0010_audit_descriptions, 1),
    Revision(11, 'account_deletions', r0011_account_deletions, 11),
    Revision(12, 'system_notifications', r0012_system_notifications, 11),
    Revision(13, 'agent_rollout_cancellation', r0013_agent_rollout_cancellation, 11),
    Revision(14, 'preapproval_visitors', r0014_preapproval_visitors, 14),
    Revision(15, 'vpn_policies', r0015_vpn_policies, 14),
    Revision(16, 'service_notifications', r0016_service_notifications, 14),
    # Older controllers would issue profile-wide Xray clients next to device clients.
    Revision(17, 'device_credentials', r0017_device_credentials, 17),
    Revision(18, 'connection_limits', r0018_connection_limits, 17),
    Revision(19, 'retired_peers', r0019_retired_peers, 19),
    Revision(20, 'traffic_quotas', r0020_traffic_quotas, 20),
    Revision(21, 'speed_limits', r0021_speed_limits, 20),
    # Older controllers would resolve limits without the tariff layer.
    Revision(22, 'tariffs', r0022_tariffs, 22),
    Revision(23, 'renewal_requests', r0023_renewal_requests, 22),
    Revision(24, 'revocation_modes', r0024_revocation_modes, 22),
    Revision(25, 'agent_enrollments', r0025_agent_enrollments, 22),
    Revision(26, 'agent_ssh_installs', r0026_agent_ssh_installs, 22),
    Revision(27, 'node_agent_checks', r0027_node_agent_checks, 22),
    Revision(28, 'account_details', r0028_account_details, 22),
    Revision(29, 'account_channels', r0029_account_channels, 22),
    Revision(30, 'inbox_without_telegram', r0030_inbox_without_telegram, 22),
    Revision(31, 'request_services', r0031_request_services, 22),
    Revision(32, 'agent_installations', r0032_agent_installations, 22),
    Revision(33, 'service_instances', r0033_service_instances, 22),
    Revision(34, 'task_services', r0034_task_services, 22),
    Revision(35, 'roles', r0035_roles, 22),
    Revision(36, 'service_profiles', r0036_service_profiles, 22),
    Revision(37, 'traffic_inbounds', r0037_traffic_inbounds, 22),
    Revision(38, 'relays', r0038_relays, 22),
    # Older controllers would write the `ssh` transport the schema no longer takes.
    Revision(39, 'remote_nodes', r0039_remote_nodes, 39),
    Revision(40, 'logins', r0040_logins, 39),
    Revision(41, 'sessions', r0041_sessions, 39),
    Revision(42, 'passkeys', r0042_passkeys, 39),
    Revision(43, 'invites', r0043_invites, 39),
    Revision(44, 'registrations', r0044_registrations, 39),
    Revision(45, 'telegram_sign_in', r0045_telegram_sign_in, 39),
    Revision(46, 'account_merges', r0046_account_merges, 39),
    Revision(47, 'localized_texts', r0047_localized_texts, 39),
    Revision(48, 'announcement_titles', r0048_announcement_titles, 39),
    Revision(49, 'metrics_history', r0049_metrics_history, 39),
    Revision(50, 'web_cabinet', r0050_web_cabinet, 39),
)


def history_exists(conn):
    # Repository unit fixtures use SQLite; production adapters are PostgreSQL.
    if getattr(conn, 'backend_name', '') != 'postgres':
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='backend_schema_revisions'").fetchone() is not None
    return conn.execute("SELECT to_regclass('backend_schema_revisions') AS name").fetchone()['name'] is not None


def _history(conn):
    return [dict(row) for row in conn.execute(
        'SELECT revision,name,checksum,minimum_reader,applied_at FROM backend_schema_revisions ORDER BY revision').fetchall()]


def _validate(rows, revisions, *, writing=False):
    expected = {r.number:r for r in revisions}
    if [r.number for r in revisions] != list(range(1, len(revisions)+1)):
        raise MigrationError('migration_registry_invalid')
    for index, row in enumerate(rows, 1):
        if row['revision'] != index or not 1 <= row['minimum_reader'] <= row['revision']:
            raise MigrationError('migration_history_invalid')
        known = expected.get(index)
        if known and (row['name'], row['checksum'], row['minimum_reader']) != (
                known.name,known.checksum,known.minimum_reader):
            raise MigrationError('migration_history_mismatch')
    if writing and len(rows) > len(revisions):
        raise MigrationError('database_newer_than_migrator')
    if rows and max(r['minimum_reader'] for r in rows) > len(revisions):
        raise MigrationError('database_reader_incompatible')


def schema_status(db, *, revisions=REVISIONS):
    """Read-only status. Future compatible additive revisions allow app rollback."""
    with db.connect() as conn:
        rows = _history(conn) if history_exists(conn) else []
    _validate(rows,revisions)
    head = rows[-1]['revision'] if rows else 0
    return {'current_revision':head, 'required_revision':len(revisions),
        'ready':head >= len(revisions), 'revisions':rows}


def check_schema(db, *, revisions=REVISIONS):
    result = schema_status(db,revisions=revisions)
    if not result['ready']:
        raise MigrationError('database_migration_required')
    return result


def migrate(db, *, revisions=REVISIONS):
    """Apply the pending batch and journal atomically under a DB-wide lock."""
    applied = []
    current = 0
    try:
        with db.transaction() as conn:
            conn.execute("SET LOCAL lock_timeout = '30s'")
            conn.execute('SELECT pg_advisory_xact_lock(?)', (LOCK_KEY,))
            conn.execute(LEDGER_DDL)
            rows = _history(conn)
            _validate(rows,revisions,writing=True)
            current = len(rows)
            # Serialize data backfills against already running backend workers.
            if conn.execute("SELECT to_regclass('backend_account_guard') AS name").fetchone()['name']:
                conn.execute('UPDATE backend_account_guard SET revision=revision WHERE id=1')
            for revision in revisions[current:]:
                revision.module.upgrade(conn)
                conn.execute('''INSERT INTO backend_schema_revisions
                    (revision,name,checksum,minimum_reader,applied_at) VALUES (?,?,?,?,?)''',
                    (revision.number,revision.name,revision.checksum,revision.minimum_reader,
                     datetime.now(timezone.utc).isoformat()))
                applied.append(revision.number)
    except MigrationError:
        raise
    except Exception as error:
        failed = current + len(applied) + 1
        raise MigrationError(f'database_migration_failed: revision={failed}; transaction rolled back') from error
    return {'current_revision':len(revisions), 'applied':applied}
