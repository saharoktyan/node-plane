"""Frozen baseline for current backend installations and clean databases.

Never edit an applied revision. Add a new revision for subsequent changes.
"""
from datetime import datetime, timezone
import unicodedata
from uuid import NAMESPACE_URL, uuid4, uuid5

STRUCTURE = (
    """CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS backend_accounts (
        id TEXT PRIMARY KEY,
        role TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('member', 'admin')),
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected', 'disabled')),
        revision INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS backend_account_guard (
        id INTEGER PRIMARY KEY CHECK(id = 1), revision INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS backend_external_identities (
        provider TEXT NOT NULL,
        subject TEXT NOT NULL,
        account_id TEXT NOT NULL REFERENCES backend_accounts(id),
        PRIMARY KEY(provider, subject)
    )""",
    """CREATE TABLE IF NOT EXISTS backend_telegram_identity_details (
        subject TEXT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        language_code TEXT,
        locale TEXT,
        locale_selected INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS backend_identity_commands (
        principal_id TEXT NOT NULL, command_key TEXT NOT NULL,
        telegram_subject TEXT NOT NULL, account_id TEXT REFERENCES backend_accounts(id),
        PRIMARY KEY(principal_id, command_key)
    )""",
    """CREATE TABLE IF NOT EXISTS backend_controller_update_gate (
    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS backend_system_cleanup_plans (
    id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, principal_id TEXT NOT NULL,
    intent_json TEXT NOT NULL, expires_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS backend_system_cleanup_jobs (
    id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, principal_id TEXT NOT NULL,
    command_key TEXT NOT NULL, plan_id TEXT NOT NULL, intent_json TEXT NOT NULL,
    status TEXT NOT NULL, phase TEXT NOT NULL, backup_id TEXT,
    shutdown_ack INTEGER NOT NULL DEFAULT 0, local_plan_json TEXT,
    error_code TEXT, created_at TEXT NOT NULL,
    UNIQUE(principal_id,actor_id,command_key), UNIQUE(plan_id))""",
    """CREATE TABLE IF NOT EXISTS backend_system_cleanup_items (
    job_id TEXT NOT NULL, node_key TEXT NOT NULL, status TEXT NOT NULL,
    error_code TEXT, PRIMARY KEY(job_id,node_key))""",
    """CREATE TABLE IF NOT EXISTS backend_backup_jobs (
    id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, command_key TEXT NOT NULL,
    action TEXT NOT NULL, backup_id TEXT, checksum TEXT, status TEXT NOT NULL,
    phase TEXT, result_json TEXT, created_at TEXT NOT NULL,
    UNIQUE(actor_id,command_key))""",
    """CREATE TABLE IF NOT EXISTS backend_backup_revocations (job_id TEXT NOT NULL, operation_id TEXT NOT NULL, PRIMARY KEY(job_id,operation_id))""",
    """CREATE TABLE IF NOT EXISTS backend_credentials (
        id TEXT PRIMARY KEY,
        secret_hash TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('account', 'adapter', 'service')),
        account_id TEXT REFERENCES backend_accounts(id),
        scopes_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS backend_workstation_keys (
    fingerprint TEXT PRIMARY KEY, account_id TEXT NOT NULL,
    registered_at TEXT NOT NULL, revoked_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS backend_workstation_context (
    credential_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    account_id TEXT NOT NULL, account_label TEXT NOT NULL,
    ssh_user TEXT NOT NULL, device_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS backend_workstation_audit (
    id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL,
    credential_id TEXT, session_id TEXT NOT NULL,
    account_id TEXT NOT NULL, account_label TEXT NOT NULL,
    ssh_user TEXT NOT NULL, device_fingerprint TEXT NOT NULL,
    action TEXT NOT NULL, phase TEXT NOT NULL,
    request_id TEXT, command_id TEXT, http_status INTEGER)""",
    """CREATE INDEX IF NOT EXISTS backend_workstation_audit_time ON backend_workstation_audit(occurred_at,id)""",
    """CREATE TABLE IF NOT EXISTS backend_workstation_enrollment (
    event_id TEXT PRIMARY KEY, target TEXT NOT NULL,
    key_fingerprint TEXT NOT NULL, outcome TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS backend_alert_state (
    node_key TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    PRIMARY KEY(node_key,kind))""",
    """CREATE TABLE IF NOT EXISTS backend_alert_events (
    id TEXT PRIMARY KEY, node_key TEXT NOT NULL, kind TEXT NOT NULL,
    resolved INTEGER NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS backend_alert_deliveries (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES backend_alert_events(id),
    account_id TEXT NOT NULL, telegram_subject TEXT NOT NULL,
    status TEXT NOT NULL, claim_id TEXT, adapter_id TEXT, claimed_until TEXT,
    UNIQUE(event_id,telegram_subject))""",
    """CREATE TABLE IF NOT EXISTS backend_announcements (
    id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, command_key TEXT NOT NULL,
    text TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(actor_id,command_key))""",
    """CREATE TABLE IF NOT EXISTS backend_announcement_deliveries (
    id TEXT PRIMARY KEY, announcement_id TEXT NOT NULL REFERENCES backend_announcements(id),
    account_id TEXT NOT NULL, telegram_subject TEXT NOT NULL,
    status TEXT NOT NULL, claim_id TEXT, adapter_id TEXT, claimed_until TEXT,
    UNIQUE(announcement_id,telegram_subject))""",
    """CREATE TABLE IF NOT EXISTS backend_profiles (
        id TEXT PRIMARY KEY,
        runtime_name TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        owner_account_id TEXT REFERENCES backend_accounts(id),
        frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0, 1)),
        expires_at TEXT,
        created_at TEXT,
        desired_revision INTEGER NOT NULL DEFAULT 1
    )""",
    """ALTER TABLE backend_profiles ADD COLUMN IF NOT EXISTS created_at TEXT""",
    """CREATE TABLE IF NOT EXISTS backend_nodes (
        key TEXT PRIMARY KEY, title TEXT NOT NULL, region TEXT NOT NULL,
        flag TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
        protocols_json TEXT NOT NULL,
        xray_transports_json TEXT NOT NULL DEFAULT '[]',
        desired_revision INTEGER NOT NULL DEFAULT 1,
        applied_revision INTEGER NOT NULL DEFAULT 0,
        settings_json TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS backend_grants (
        profile_id TEXT NOT NULL REFERENCES backend_profiles(id) ON DELETE CASCADE,
        node_key TEXT NOT NULL REFERENCES backend_nodes(key) ON DELETE CASCADE,
        protocol TEXT NOT NULL CHECK(protocol IN ('awg', 'xray')),
        PRIMARY KEY(profile_id, node_key, protocol)
    )""",
    """CREATE TABLE IF NOT EXISTS backend_profile_deletions (
        profile_id TEXT PRIMARY KEY REFERENCES backend_profiles(id),
        requested_at TEXT NOT NULL,
        operation_id TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS idx_backend_profile_owner ON backend_profiles(owner_account_id, id)""",
    """CREATE TABLE IF NOT EXISTS backend_devices (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES backend_profiles(id) ON DELETE CASCADE,
        display_name TEXT NOT NULL,
        runtime_name TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK(status IN ('active', 'deleting', 'retired')),
        revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS backend_devices_profile ON backend_devices(profile_id, id)""",
    """CREATE TABLE IF NOT EXISTS backend_device_commands (
    principal_id TEXT NOT NULL,actor_id TEXT NOT NULL,command_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,result_json TEXT,
    PRIMARY KEY(principal_id,actor_id,command_key))""",
    """CREATE TABLE IF NOT EXISTS backend_regions (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, canonical TEXT NOT NULL UNIQUE)""",
    """CREATE TABLE IF NOT EXISTS backend_node_regions (
    node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key) ON DELETE CASCADE,
    region_id TEXT NOT NULL REFERENCES backend_regions(id))""",
    """CREATE TABLE IF NOT EXISTS backend_grant_policies (
    profile_id TEXT PRIMARY KEY REFERENCES backend_profiles(id) ON DELETE CASCADE,
    explicit_json TEXT NOT NULL, rules_json TEXT NOT NULL, exclusions_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS backend_node_settings_tasks (
        id TEXT PRIMARY KEY, node_key TEXT NOT NULL,
        actor_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
        command_key TEXT NOT NULL, revision INTEGER NOT NULL,
        intent_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('awaiting_executor', 'running', 'blocked', 'succeeded', 'superseded')),
        result_json TEXT,
        UNIQUE(actor_account_id, command_key)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_backend_node_settings_work ON backend_node_settings_tasks(status, node_key)""",
    """CREATE TABLE IF NOT EXISTS backend_node_drains (
        node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key),
        actor_id TEXT NOT NULL REFERENCES backend_accounts(id),
        operation_ids_json TEXT NOT NULL,
        started_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS backend_node_cleanup (
        node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key),
        actor_id TEXT NOT NULL REFERENCES backend_accounts(id),
        command_id TEXT NOT NULL UNIQUE,
        phase TEXT NOT NULL CHECK(phase IN (
            'preparing', 'prepared', 'runtime_deleted',
            'uninstall_uncertain', 'uninstall_scheduled')),
        started_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS backend_node_retirements (
        node_key TEXT PRIMARY KEY,
        actor_id TEXT NOT NULL REFERENCES backend_accounts(id),
        mode TEXT NOT NULL CHECK(mode IN ('verified', 'registry_only')),
        reason TEXT NOT NULL,
        previous_cleanup_phase TEXT,
        unfinished_tasks INTEGER NOT NULL,
        evidence_json TEXT,
        retired_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS backend_node_verification_targets (
        node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key),
        target TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS backend_node_host_identities (
        node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key),
        fingerprint TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS backend_node_removal_inventories (
        node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key),
        resources_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS backend_node_jobs (
        id TEXT PRIMARY KEY, node_key TEXT NOT NULL, actor_id TEXT NOT NULL,
        command_key TEXT NOT NULL, action TEXT NOT NULL, revision INTEGER NOT NULL,
        intent_json TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,
        UNIQUE(actor_id, command_key)
    )""",
    """CREATE TABLE IF NOT EXISTS backend_node_removals (
        node_key TEXT PRIMARY KEY, actor_id TEXT NOT NULL,
        status TEXT NOT NULL, error_code TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS backend_system_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS backend_traffic_usage (
    profile_id TEXT NOT NULL REFERENCES backend_profiles(id) ON DELETE CASCADE,
    account_id TEXT REFERENCES backend_accounts(id) ON DELETE CASCADE,
    node_key TEXT NOT NULL REFERENCES backend_nodes(key) ON DELETE CASCADE,
    protocol TEXT NOT NULL CHECK(protocol IN ('awg','xray')),
    uplink_bytes BIGINT NOT NULL DEFAULT 0, downlink_bytes BIGINT NOT NULL DEFAULT 0,
    epoch TEXT, identity TEXT, last_uplink BIGINT, last_downlink BIGINT,
    tracked_since TEXT, last_sample_at TEXT, status TEXT NOT NULL,
    period_month TEXT,
    PRIMARY KEY(profile_id,node_key,protocol))""",
    """ALTER TABLE backend_traffic_usage ADD COLUMN IF NOT EXISTS period_month TEXT""",
    """ALTER TABLE backend_traffic_usage ALTER COLUMN account_id DROP NOT NULL""",
    """CREATE TABLE IF NOT EXISTS backend_traffic_peers (
    profile_id TEXT NOT NULL REFERENCES backend_profiles(id) ON DELETE CASCADE,
    node_key TEXT NOT NULL REFERENCES backend_nodes(key) ON DELETE CASCADE,
    device_id TEXT NOT NULL REFERENCES backend_devices(id) ON DELETE CASCADE,
    epoch TEXT, identity TEXT, last_uplink BIGINT, last_downlink BIGINT,
    last_sample_at TEXT,status TEXT NOT NULL,
    PRIMARY KEY(profile_id,node_key,device_id))""",
    """CREATE TABLE IF NOT EXISTS backend_operations (
        id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES backend_profiles(id),
        actor_id TEXT NOT NULL REFERENCES backend_accounts(id),
        desired_revision INTEGER NOT NULL, status TEXT NOT NULL
            CHECK(status IN ('no_targets', 'awaiting_executor', 'running', 'succeeded', 'blocked', 'superseded')),
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS backend_operation_tasks (
        id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES backend_operations(id),
        node_key TEXT NOT NULL, protocol TEXT NOT NULL CHECK(protocol IN ('awg', 'xray')),
        device_id TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL CHECK(action IN ('ensure', 'delete')),
        status TEXT NOT NULL CHECK(status IN ('awaiting_executor', 'running', 'succeeded', 'blocked', 'superseded')),
        intent_json TEXT NOT NULL, result_json TEXT, driver_operation_id TEXT,
        inspection_json TEXT, inspected_at TEXT
    )""",
    """ALTER TABLE backend_operation_tasks ADD COLUMN IF NOT EXISTS device_id TEXT NOT NULL DEFAULT ''""",
    """ALTER TABLE backend_operation_tasks DROP CONSTRAINT IF EXISTS backend_operation_tasks_operation_id_node_key_protocol_key""",
    """CREATE UNIQUE INDEX IF NOT EXISTS backend_operation_task_target
    ON backend_operation_tasks(operation_id,node_key,protocol,device_id)""",
    """CREATE TABLE IF NOT EXISTS backend_profile_identities (
        profile_id TEXT PRIMARY KEY REFERENCES backend_profiles(id),
        xray_uuid TEXT NOT NULL, xray_short_id TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS backend_repairs (
        task_id TEXT PRIMARY KEY REFERENCES backend_operation_tasks(id),
        actor_id TEXT NOT NULL REFERENCES backend_accounts(id),
        operation_id TEXT NOT NULL REFERENCES backend_operations(id),
        observation_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS backend_operations_profile ON backend_operations(profile_id, desired_revision)""",
    """CREATE TABLE IF NOT EXISTS backend_profile_commands (
        principal_id TEXT NOT NULL, actor_id TEXT NOT NULL, command_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL, result_json TEXT,
        PRIMARY KEY(principal_id, actor_id, command_key)
    )""",
    """CREATE TABLE IF NOT EXISTS backend_access_requests (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES backend_accounts(id),
        create_key TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
        created_at TEXT NOT NULL,
        decided_at TEXT,
        decided_by TEXT REFERENCES backend_accounts(id),
        decision_key TEXT,
        UNIQUE(account_id, create_key)
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_backend_pending_access_request
    ON backend_access_requests(account_id) WHERE status = 'pending'""",
    """CREATE INDEX IF NOT EXISTS idx_backend_access_requests_status
    ON backend_access_requests(status, id)""",
    """CREATE TABLE IF NOT EXISTS backend_account_commands (
        actor_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
        command_key TEXT NOT NULL,
        target_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
        input_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        PRIMARY KEY(actor_account_id, command_key)
    )""",
    """CREATE TABLE IF NOT EXISTS backend_update_jobs (
    id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, command_key TEXT NOT NULL,
    kind TEXT NOT NULL, intent_json TEXT NOT NULL, status TEXT NOT NULL,
    result_json TEXT, created_at TEXT NOT NULL, UNIQUE(actor_id, command_key))""",
    """CREATE TABLE IF NOT EXISTS backend_update_items (
    job_id TEXT NOT NULL, node_key TEXT NOT NULL, intent_json TEXT NOT NULL,
    status TEXT NOT NULL, child_id TEXT, error_code TEXT,
    PRIMARY KEY(job_id, node_key))""",
    """CREATE TABLE IF NOT EXISTS backend_node_notes (node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key) ON DELETE CASCADE, notes TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS backend_node_connections (
        node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key) ON DELETE CASCADE,
        transport TEXT NOT NULL CHECK(transport IN ('local', 'ssh')),
        ssh_target TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS backend_node_commands (
        actor_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
        command_key TEXT NOT NULL,
        input_json TEXT NOT NULL,
        result_json TEXT,
        PRIMARY KEY(actor_account_id, command_key)
    )""",
    """CREATE TABLE IF NOT EXISTS backend_config_issuances (
        id TEXT PRIMARY KEY, actor_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
        command_key TEXT NOT NULL, profile_id TEXT NOT NULL REFERENCES backend_profiles(id),
        node_key TEXT NOT NULL, protocol TEXT NOT NULL, transport TEXT NOT NULL,
        device_id TEXT NOT NULL DEFAULT '', device_revision INTEGER NOT NULL DEFAULT 0,
        profile_revision INTEGER NOT NULL, node_revision INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('awaiting_executor', 'running', 'blocked', 'succeeded', 'superseded')),
        result_json TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        UNIQUE(actor_account_id, command_key)
    )""",
    """ALTER TABLE backend_config_issuances ADD COLUMN IF NOT EXISTS device_id TEXT NOT NULL DEFAULT ''""",
    """ALTER TABLE backend_config_issuances ADD COLUMN IF NOT EXISTS device_revision INTEGER NOT NULL DEFAULT 0""",
    """CREATE INDEX IF NOT EXISTS idx_backend_config_issuances_work ON backend_config_issuances(status, id)""",
    """CREATE TABLE IF NOT EXISTS backend_agent_rollouts (
        id TEXT PRIMARY KEY, node_key TEXT NOT NULL,
        actor_account_id TEXT NOT NULL REFERENCES backend_accounts(id),
        command_key TEXT NOT NULL, intent_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('awaiting_executor', 'running', 'blocked', 'succeeded')),
        UNIQUE(actor_account_id, command_key)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_backend_agent_rollouts_work ON backend_agent_rollouts(status, id)""",
    """CREATE TABLE IF NOT EXISTS backend_agent_rollout_failures (task_id TEXT PRIMARY KEY, code TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS backend_agent_rollout_archives (
    task_id TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(task_id, path))""",
)

REPAIRS = (
    """UPDATE backend_accounts SET status = 'pending', revision = revision + 1
    WHERE role = 'member' AND status = 'approved'
    AND EXISTS (SELECT 1 FROM backend_profiles p
        JOIN backend_profile_deletions d ON d.profile_id = p.id
        WHERE p.owner_account_id = backend_accounts.id)
    AND NOT EXISTS (SELECT 1 FROM backend_profiles p
        WHERE p.owner_account_id = backend_accounts.id AND NOT EXISTS
            (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id = p.id))""",
    """DELETE FROM backend_system_settings WHERE key LIKE 'traffic_consent:%' OR key LIKE 'traffic_consent_generation:%'""",
    """UPDATE backend_operation_tasks SET device_id=(
    SELECT d.id FROM backend_devices d JOIN backend_operations o ON o.profile_id=d.profile_id
    JOIN backend_profiles p ON p.id=o.profile_id
    WHERE o.id=backend_operation_tasks.operation_id AND d.runtime_name=p.runtime_name)
    WHERE protocol='awg' AND device_id='' AND EXISTS (
        SELECT 1 FROM backend_devices d JOIN backend_operations o ON o.profile_id=d.profile_id
        JOIN backend_profiles p ON p.id=o.profile_id
        WHERE o.id=backend_operation_tasks.operation_id AND d.runtime_name=p.runtime_name)""",
    """UPDATE backend_config_issuances SET device_id=(
    SELECT d.id FROM backend_devices d JOIN backend_profiles p ON p.id=d.profile_id
    WHERE p.id=backend_config_issuances.profile_id AND d.runtime_name=p.runtime_name),device_revision=1
    WHERE protocol='awg' AND device_id='' AND EXISTS (
        SELECT 1 FROM backend_devices d JOIN backend_profiles p ON p.id=d.profile_id
        WHERE p.id=backend_config_issuances.profile_id AND d.runtime_name=p.runtime_name)""",
)


def upgrade(conn):
    for sql in STRUCTURE:
        conn.execute(sql)
    conn.execute('''INSERT INTO backend_account_guard(id,revision) VALUES (1,1)
        ON CONFLICT(id) DO NOTHING''')
    # Workers use the same row to serialize registry and accounting commits.
    conn.execute('UPDATE backend_account_guard SET revision=revision WHERE id=1')
    for row in conn.execute('''SELECT n.key,n.region FROM backend_nodes n
        WHERE NOT EXISTS (SELECT 1 FROM backend_node_regions r WHERE r.node_key=n.key)''').fetchall():
        canonical = ' '.join(unicodedata.normalize('NFKC', row['region']).casefold().split())
        conn.execute('''INSERT INTO backend_regions(id,title,canonical) VALUES (?,?,?)
            ON CONFLICT(canonical) DO NOTHING''', (str(uuid4()),row['region'],canonical))
        region = conn.execute('SELECT id FROM backend_regions WHERE canonical=?', (canonical,)).fetchone()
        conn.execute('INSERT INTO backend_node_regions(node_key,region_id) VALUES (?,?)',
            (row['key'],region['id']))
    # Preserve historical peer identity and command payloads during adoption.
    for profile in conn.execute('''SELECT p.* FROM backend_profiles p
        WHERE NOT EXISTS (SELECT 1 FROM backend_devices d WHERE d.profile_id=p.id)
        AND (EXISTS (SELECT 1 FROM backend_grants g WHERE g.profile_id=p.id AND g.protocol='awg')
            OR EXISTS (SELECT 1 FROM backend_operation_tasks t JOIN backend_operations o ON o.id=t.operation_id
                WHERE o.profile_id=p.id AND t.protocol='awg')) ORDER BY p.id''').fetchall():
        identity = str(uuid5(NAMESPACE_URL, 'node-plane:default-device:' + profile['id']))
        conn.execute('''INSERT INTO backend_devices
            (id,profile_id,display_name,runtime_name,status,revision,created_at)
            VALUES (?,?,'Device 1',?,'active',1,?)''',
            (identity,profile['id'],profile['runtime_name'],
             profile['created_at'] or datetime.now(timezone.utc).isoformat()))
    for sql in REPAIRS:
        conn.execute(sql)
    conn.execute('''INSERT INTO backend_traffic_peers
        (profile_id,node_key,device_id,epoch,identity,last_uplink,last_downlink,last_sample_at,status)
        SELECT u.profile_id,u.node_key,d.id,u.epoch,u.identity,u.last_uplink,u.last_downlink,u.last_sample_at,u.status
        FROM backend_traffic_usage u JOIN backend_profiles p ON p.id=u.profile_id
        JOIN backend_devices d ON d.profile_id=p.id AND d.runtime_name=p.runtime_name
        WHERE u.protocol='awg' ON CONFLICT(profile_id,node_key,device_id) DO NOTHING''')
