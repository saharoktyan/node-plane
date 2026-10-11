# Backend API

The HTTP application is `app/backend/http_api.py`. Pydantic request/response models and route implementations define field-level contracts. Inspect `/openapi.json` on a running backend for its machine-readable schema. Do not publish the administrative service directly without its authentication boundary.

## Identity and permissions

Bearer credentials identify an account or adapter and restrict allowed scopes. Telegram resolves an actor through its private adapter. Workstation's root-only SSH bridge enrolls a key fingerprint and issues short-lived credentials bound to one approved administrator.

The key binding survives local session closure. Revoking a session ends that session; revoking Workstation access prevents future sessions. A disabled/deleted/demoted account cannot silently move its key to another account. Audit records principal, account and key attribution without storing request secrets.

Authorization is applied in the backend, not inferred from a hidden button. Accounts, VPN profiles, grants and devices are distinct records. Workstation local connection profiles are outside the backend model.

## Mutation contracts

Use stable idempotency keys where required. Preserve command identities through retries and recovery; reuse with a different payload is invalid. Revision checks prevent applying a stale draft. Durable operation responses are acceptance, not immediate proof of runtime completion.

Poll the operation/read model for the result. Recovery endpoints observe or explicitly resolve the original operation. A blocked result must remain visible until evidence or an authorized decision resolves it.

## Error handling

Read the structured error code and response body. Authentication failure, insufficient permission, invalid input, revision conflict and uncertain execution require different responses. Preserve an operation ID for support; omit tokens and credential payloads from logs.

## API groups

- `/me`: identity, preferences and administrator-announcement notifications.
- `/accounts`, `/profiles`, `/access-requests`: account/profile access management.
- `/nodes`: inventory, settings, setup, diagnostics and lifecycle operations.
- `/system`: controller settings, updates, cleanup, backups, attention/audit and temporary configurations.
- `/integrations/telegram`: adapter-only identity and delivery coordination.

Consult the routes below and their Pydantic models for exact request bodies. The table is an inventory, not a claim that every route accepts every credential.

## Route inventory

| Method | Path | Handler |
| --- | --- | --- |
| `GET` | `/health/live` | `live` |
| `GET` | `/health/ready` | `ready` |
| `POST` | `/api/v1/integrations/telegram/identities/resolve` | `resolve` |
| `GET` | `/api/v1/me` | `me` |
| `PATCH` | `/api/v1/me/preferences` | `update_my_preferences` |
| `POST` | `/api/v1/announcements/preview` | `announcement_preview` |
| `GET` | `/api/v1/announcements` | `latest_announcement` |
| `GET` | `/api/v1/me/notifications` | `my_notifications` |
| `GET` | `/api/v1/me/notifications/{delivery_id}` | `my_notification` |
| `POST` | `/api/v1/me/notifications/{delivery_id}/dismiss` | `dismiss_my_notification` |
| `GET` | `/api/v1/system/traffic` | `traffic_policy` |
| `GET` | `/api/v1/system/cleanup` | `cleanup_overview` |
| `POST` | `/api/v1/system/cleanup/plans` | `cleanup_plan` |
| `POST` | `/api/v1/system/cleanup/jobs` | `cleanup_queue` |
| `GET` | `/api/v1/system/cleanup/jobs/{job_id}` | `cleanup_job` |
| `POST` | `/api/v1/system/cleanup/jobs/{job_id}/retry` | `cleanup_retry` |
| `POST` | `/api/v1/system/cleanup/jobs/{job_id}/abort` | `cleanup_abort` |
| `POST` | `/api/v1/system/cleanup/jobs/{job_id}/shutdown-ack` | `cleanup_shutdown_ack` |
| `PATCH` | `/api/v1/system/traffic/preferences` | `traffic_preferences` |
| `GET` | `/api/v1/system/alerts` | `alert_overview` |
| `POST` | `/api/v1/system/alerts/{event_id}/dismiss` | `alert_dismiss` |
| `GET` | `/api/v1/system/attention` | `system_attention` |
| `PATCH` | `/api/v1/system/alerts/preferences` | `alert_preferences` |
| `POST` | `/api/v1/integrations/telegram/alerts/claim` | `claim_alert` |
| `POST` | `/api/v1/integrations/telegram/alerts/{delivery_id}/ack` | `acknowledge_alert` |
| `POST` | `/api/v1/announcements` | `announcement_create` |
| `GET` | `/api/v1/announcements/{announcement_id}` | `announcement_status` |
| `POST` | `/api/v1/integrations/telegram/announcements/claim` | `claim_announcement` |
| `POST` | `/api/v1/integrations/telegram/announcements/{delivery_id}/ack` | `ack_announcement` |
| `POST` | `/api/v1/me/access-requests` | `create_access_request` |
| `GET` | `/api/v1/system/access-requests` | `get_access_request_policy` |
| `PATCH` | `/api/v1/system/access-requests` | `edit_access_request_policy` |
| `GET` | `/api/v1/system/bot-title` | `get_bot_title` |
| `PATCH` | `/api/v1/system/bot-title` | `edit_bot_title` |
| `GET` | `/api/v1/system/version` | `get_system_version` |
| `GET` | `/api/v1/me/access-requests` | `own_access_requests` |
| `GET` | `/api/v1/access-requests` | `pending_access_requests` |
| `GET` | `/api/v1/access-requests/{request_id}` | `get_pending_access_request` |
| `POST` | `/api/v1/access-requests/{request_id}/decision` | `decide_access_request` |
| `GET` | `/api/v1/me/profiles` | `own_profiles` |
| `GET` | `/api/v1/profiles/{profile_id}/summary` | `admin_profile_summary` |
| `GET` | `/api/v1/me/profiles/{profile_id}/summary` | `own_profile_summary` |
| `GET` | `/api/v1/profiles` | `all_profiles` |
| `GET` | `/api/v1/admin/overview` | `administration_overview` |
| `GET` | `/api/v1/profiles/{profile_id}` | `profile` |
| `GET` | `/api/v1/profiles/{profile_id}/grants` | `profile_grants` |
| `GET` | `/api/v1/profiles/{profile_id}/devices` | `profile_devices` |
| `POST` | `/api/v1/profiles/{profile_id}/devices` | `create_device` |
| `PATCH` | `/api/v1/profiles/{profile_id}/devices/{device_id}` | `rename_device` |
| `DELETE` | `/api/v1/profiles/{profile_id}/devices/{device_id}` | `delete_device` |
| `GET` | `/api/v1/profiles/{profile_id}/operation` | `latest_profile_operation` |
| `GET` | `/api/v1/me/nodes` | `own_nodes` |
| `GET` | `/api/v1/profiles/{profile_id}/nodes` | `profile_nodes` |
| `GET` | `/api/v1/nodes` | `list_nodes` |
| `GET` | `/api/v1/nodes/creation-options` | `node_creation_options` |
| `GET` | `/api/v1/system/installation-defaults` | `get_installation_defaults` |
| `PUT` | `/api/v1/system/installation-defaults` | `put_installation_defaults` |
| `GET` | `/api/v1/nodes/{node_key}` | `get_node` |
| `GET` | `/api/v1/nodes/{node_key}/overview` | `get_node_overview` |
| `GET` | `/api/v1/nodes/{node_key}/services` | `get_node_services` |
| `POST` | `/api/v1/nodes/{node_key}/bootstrap` | `bootstrap_node` |
| `GET` | `/api/v1/node-bootstraps/{identity}` | `get_node_bootstrap` |
| `POST` | `/api/v1/nodes/{node_key}/actions` | `queue_node_action` |
| `GET` | `/api/v1/node-jobs/{job_id}` | `get_node_job` |
| `POST` | `/api/v1/node-jobs/{job_id}/resolve` | `resolve_node_job` |
| `GET` | `/api/v1/nodes/{node_key}/runtime` | `inspect_node_runtime` |
| `GET` | `/api/v1/nodes/{node_key}/diagnostics` | `get_node_diagnostics` |
| `POST` | `/api/v1/nodes` | `create_node` |
| `PATCH` | `/api/v1/nodes/{node_key}` | `edit_node` |
| `POST` | `/api/v1/nodes/{node_key}/apply-settings` | `apply_backend_node_settings` |
| `GET` | `/api/v1/node-settings-operations/{task_id}` | `get_node_settings_operation` |
| `POST` | `/api/v1/nodes/{node_key}/agent-rollouts` | `request_agent_rollout` |
| `GET` | `/api/v1/agent-rollouts/{task_id}` | `get_agent_rollout` |
| `GET` | `/api/v1/nodes/{node_key}/maintenance` | `get_node_maintenance` |
| `POST` | `/api/v1/nodes/{node_key}/bind-verification-target` | `bind_node_verification_target` |
| `POST` | `/api/v1/nodes/{node_key}/drain` | `drain_node` |
| `POST` | `/api/v1/nodes/{node_key}/cleanup-step` | `cleanup_node_step` |
| `POST` | `/api/v1/nodes/{node_key}/verify-and-retire` | `verify_and_retire_node` |
| `POST` | `/api/v1/nodes/{node_key}/retire-registry-only` | `retire_node_registry_only` |
| `POST` | `/api/v1/nodes/{node_key}/remove-step` | `remove_node_step` |
| `GET` | `/api/v1/nodes/{node_key}/removal` | `get_node_removal` |
| `GET` | `/api/v1/accounts` | `list_accounts` |
| `GET` | `/api/v1/accounts/{account_id}` | `get_account` |
| `PATCH` | `/api/v1/accounts/{account_id}` | `update_account` |
| `GET` | `/api/v1/regions` | `grant_regions` |
| `GET` | `/api/v1/nodes/{node_key}/region-access-preview` | `preview_region_access` |
| `GET` | `/api/v1/profiles/{profile_id}/access-policy` | `get_access_policy` |
| `PUT` | `/api/v1/profiles/{profile_id}/access-policy` | `replace_access_policy` |
| `POST` | `/api/v1/profiles` | `create_profile` |
| `PATCH` | `/api/v1/profiles/{profile_id}` | `edit_profile` |
| `PATCH` | `/api/v1/profiles/{profile_id}/grants` | `replace_grants` |
| `DELETE` | `/api/v1/profiles/{profile_id}` | `delete_profile` |
| `GET` | `/api/v1/operations/{operation_id}` | `operation` |
| `POST` | `/api/v1/profiles/{profile_id}/config-issuances` | `request_config` |
| `GET` | `/api/v1/config-issuances/{issuance_id}` | `get_config_issuance` |
| `GET` | `/api/v1/config-issuances/{issuance_id}/artifact` | `get_config_artifact` |
| `GET` | `/api/v1/system/temporary-configs` | `list_temporary_configs` |
| `POST` | `/api/v1/system/temporary-configs` | `create_temporary_config` |
| `GET` | `/api/v1/system/temporary-configs/{config_id}` | `get_temporary_config` |
| `GET` | `/api/v1/system/temporary-configs/{config_id}/artifact` | `get_temporary_artifact` |
| `POST` | `/api/v1/system/temporary-configs/{config_id}/revoke` | `revoke_temporary_config` |
| `GET` | `/api/v1/system/ssh-key` | `get_system_ssh_key` |
| `GET` | `/api/v1/system/updates` | `get_updates` |
| `PATCH` | `/api/v1/system/updates/preferences` | `edit_update_preferences` |
| `POST` | `/api/v1/system/updates/check` | `check_updates` |
| `POST` | `/api/v1/system/updates/run` | `run_update` |
| `GET` | `/api/v1/system/updates/versions` | `update_versions` |
| `GET` | `/api/v1/system/updates/rollout` | `update_rollout` |
| `GET` | `/api/v1/system/recovery/history` | `recovery_history` |
| `GET` | `/api/v1/system/recovery/controller` | `recovery_controller` |
| `POST` | `/api/v1/system/recovery/{kind}/{identity}/{action}` | `recover_operation` |
| `GET` | `/api/v1/system/recovery` | `recovery_overview` |
| `GET` | `/api/v1/system/workstation-audit` | `workstation_audit` |
| `GET` | `/api/v1/system/updates/jobs/{job_id}` | `update_job` |
| `POST` | `/api/v1/system/updates/jobs/{job_id}/dismiss` | `dismiss_update_result` |
| `POST` | `/api/v1/system/updates/jobs/{job_id}/cancel` | `cancel_update` |
| `POST` | `/api/v1/system/updates/jobs/{job_id}/recheck` | `recheck_update` |
| `GET` | `/api/v1/system/backups` | `get_backups` |
| `GET` | `/api/v1/system/backups/catalog` | `backup_catalog` |
| `GET` | `/api/v1/system/backups/catalog/{backup_id}` | `backup_detail` |
| `POST` | `/api/v1/system/backups/catalog/{backup_id}/delete` | `delete_backup` |
| `PATCH` | `/api/v1/system/backups/preferences` | `backup_preferences` |
| `POST` | `/api/v1/system/backups/jobs` | `backup_command` |
| `GET` | `/api/v1/system/backups/jobs/{job_id}` | `backup_job` |
