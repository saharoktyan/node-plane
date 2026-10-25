# Node Plane Operation Recovery Audit

> [!NOTE]
> **Audit Baseline Notice (Pre-Fix Snapshot)**
> The original audit below reflects the baseline recovery audit conducted on 2026-10-24 prior to the implementation pass.
> For the verified implementation follow-up detailing current recovery invariants, client parity, gate enforcement, and release boundaries, see [Implementation Follow-up (2026-10-25)](#implementation-follow-up-2026-10-25) at the end of this document.

## Executive Summary

This audit inventories the durable operation families found in the inspected source in **Node Plane** capable of reaching a `blocked`, `failed`, or unknown outcome. It investigates state transitions into and out of blocked states, service evidence checks, maintenance gates, public routes, Telegram actions, and Node Plane Workstation (NPW) TUI/`--no-tui` actions. Source review is not a live fault-injection proof for every state.

Node Plane enforces a conservative reliability architecture: uncertain remote execution is never blindly replayed, fences are never deleted, and crashes during in-flight operations transition durable records into `blocked` states requiring explicit journal lookup, operator confirmation, or verified compensation.

However, source verification reveals **confirmed recovery gaps and several evidence-dependent limitations**, including:
1. **Restore Admission Blocked by Unlisted Config Issuance**: `backend_config_issuances` is capable of entering `blocked`, has no recovery mechanism and is omitted from `recovery.py`, but is checked by `BackupService.busy()`. A single blocked config issuance prevents subsequent backup restore admission until the record is safely settled; the current normal recovery flow provides no such action with HTTP 409 `maintenance_busy`.
2. **Bootstrap & Agent Rollout Deadlock on `rust_required`**: If an agent rollout fails because Rust is required to build on the target host, `preflight_failed` returns `False`. The parent bootstrap job cannot be cancelled or resolved via recovery, and the Telegram bot's "Install Rust" button fails with HTTP 409 `node_operation_pending` due to `require_idle` catching the blocked bootstrap.
3. **Hidden System Cleanup Fence**: `backend_system_cleanup_jobs` is omitted from `recovery.py` `SOURCES`, yet when `blocked` it activates the global maintenance gate in `recovery._maintenance()`, stripping all recovery actions (`actions: []`) from all other jobs fleet-wide while remaining invisible in the recovery inventory.
4. **Evidence-dependent Bootstrap Cancellation (safety limitation, not an unconditional defect)**: Cancelling a blocked bootstrap whose child is a `node-job` synchronously calls `NodeOperations.resolve()` on the child. If the remote node agent is unreachable, the gRPC call fails and blocks the cancellation with HTTP 409 `recovery_unconfirmed`.
5. **NPW Node Recovery Action Discrepancy**: `rust/node-plane-cli/src/nodes.rs` hardcodes an allowlist check to only `["recheck", "resolve"]`, preventing operators from issuing valid backend actions (`cancel` on bootstrap, `resume` on agent rollout) from the workstation TUI.

---

## Audit Scope & Verification Methodology

- **Repository Inspected**: Backend (`app/backend/`), Database schemas (`app/db/`), Telegram client routers (`app/telegram_client/`), Node Plane Workstation Rust CLI (`rust/node-plane-cli/`), Scripts (`scripts/`), Test suites (`tests/`).
- **Mode**: Read-only source audit. No runtime code, configurations, or external services modified.
- **Statuses**:
  - `VERIFIED_FROM_SOURCE`: Direct line-by-line verification in application code and schemas.
  - `TESTED`: Executed during this audit; reading an existing test alone does not qualify. See verification results below.
  - `GAP`: Concrete structural defect, missing transition, unrecoverable state, or UI/API routing mismatch.
  - `NEEDS_LIVE`: Dynamic interaction or external environment behavior requiring live multi-node deployment to observe.

---

## Master Inventory of Durable Operations

| Operation Domain | Primary Table(s) | Present in `recovery.py` `SOURCES` | Entry Transitions into Blocked | Recovery Actions in `recovery.py` | UI / Workstation Accessibility | Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Stack Updates** | `backend_update_jobs`, `backend_update_items`, `backend_controller_update_gate` | **Yes** (`update`) | Worker restart (`running` with no result); core systemd verification failure; rollback failure; child failure | `cancel` (if queued); `recheck` (if stack core) | Telegram: `update_cancel`, `update_recheck`; NPW: `--operation <id>`, `UpdateRecord` | `VERIFIED_FROM_SOURCE` |
| **Version Updates** | `backend_update_jobs`, `backend_update_items` | **Yes** (`update`) | Worker restart; unit mismatch; script failure | **None** (`actions: []`) | Telegram displays error; NPW reports error | `GAP` |
| **Fleet Rollouts (Agents/Runtimes)** | `backend_update_jobs`, `backend_update_items` | **Yes** (`update`) | Child agent rollout or node job blocked; verification timeout | **None** (`actions: []`) | Telegram displays error; child items can be viewed in recovery | `GAP` |
| **Node Bootstrap** | `backend_node_bootstraps` | **Yes** (`bootstrap`) | Child task blocked; agent unavailable probe; revision mismatch; unapproved actor | `recheck`, `resolve`, `cancel` (conditional) | Telegram: Recovery menu; NPW: `Command::Recover` (blocked by CLI action filter) | `GAP` |
| **Agent Rollout** | `backend_agent_rollouts`, `backend_agent_rollout_failures` | **Yes** (`agent`) | Worker restart; non-zero installer script exit; SSH auth failure; rust required | `recheck`, `resume` (conditional on preflight failure) | Telegram: Rollout card, "Install Rust" button; NPW: Node wizard | `GAP` |
| **Node Jobs** (Protocols, Docker, Ports, Runtime) | `backend_node_jobs` | **Yes** (`node`) | Worker restart; driver execution exception; RPC timeout | `recheck`, `resolve` | Telegram: Recovery menu, Node tools; NPW: `Command::Recover` | `VERIFIED_FROM_SOURCE` |
| **Node Settings** | `backend_node_settings_tasks` | **Yes** (`settings`) | Worker restart; apply RPC exception; validation failure | `recheck`, `resolve` (removed if other tasks active on node) | Telegram: Recovery menu; NPW: `Command::Recover` | `VERIFIED_FROM_SOURCE` |
| **Profile Operations** (Ensure/Delete, Expiry) | `backend_operations`, `backend_operation_tasks`, `backend_repairs` | **Yes** (`profile`) | Worker restart; driver execution exception; expired target provisioning | `recheck`, `resolve` | Telegram: Recovery menu, Profile card; NPW: TUI access screen | `VERIFIED_FROM_SOURCE` |
| **Temporary Configs** (Lease Issuance/Revoke) | `backend_temporary_configs`, `backend_temporary_config_events` | **Yes** (`temporary`) | Worker restart (`issuing` -> `blocked`); issue exception -> `blocked`; revoke exception -> `revoke_blocked` | `recheck`, `resolve` (`blocked`); `recheck` (`revoke_blocked`) | Telegram: Recovery menu, Temporary configs router | `VERIFIED_FROM_SOURCE` |
| **Config Issuance** (Client Artifacts) | `backend_config_issuances` | **NO** | Worker restart (`running` -> `blocked`); driver inspect exception | **None** (Omitted from `recovery.py`) | No recovery UI; blocks backup restore | `GAP`, `TESTED` |
| **Backups: Create** | `backend_backup_jobs`, `backend_backup_labels` | **Yes** (`backup`) | Snapshot creation exception; disk full; filesystem error | `resume` (Cannot cancel!) | Telegram: Backups router; NPW: Settings screen | `GAP` |
| **Backups: Restore** | `backend_backup_jobs`, `backend_backup_revocations` | **Yes** (`backup`) | Pre-restore snapshot failure; profile revocation failure; node reconnect failure | `cancel` (if queued); `resume` (if checkpoints satisfied) | Telegram: Backups router, Recovery menu | `VERIFIED_FROM_SOURCE` |
| **System Cleanup** (Reset, Remove) | `backend_system_cleanup_jobs`, `backend_system_cleanup_items`, `backend_system_cleanup_plans` | **NO** | Node removal blocked; deployment mismatch; shutdown timeout; launch uncertain | **None** in `recovery.py` (has `/retry` and `/abort` routes) | Telegram: System Cleanup router; NPW: controller cleanup Job/Retry/Abort controls (separate from Operations) | `GAP` |
| **Node Removal** | `backend_node_removals`, `backend_node_drains`, `backend_node_cleanup`, `backend_node_retirements` | **Yes** (`removal`) | Node revocations blocked; host verification failure; agent unavailable | `resume` (if node in DB); emergency: `retire-registry-only` | Telegram: Recovery menu, Node tools; NPW: Node maintenance screen | `VERIFIED_FROM_SOURCE` |
| **Controller Systemd Services** | Systemd unit state (`scripts/installation_diagnostics.py`) | **NO** | Unit stopped, crashed, failed to start | `diagnose --repair` (systemctl start via NPW/script) | NPW: `diagnose --repair`; Telegram: Controller diagnostics view | `VERIFIED_FROM_SOURCE` |

---

## Detailed Operation Traces & Analysis

### 1. Stack & Node Updates

#### Code Pointers
- Backend Service: [`app/backend/updates.py`](../../app/backend/updates.py#L20-L562)
- Recovery Logic: [`app/backend/recovery.py:83-88`](../../app/backend/recovery.py#L83-L88)
- Workstation Integration: [`rust/node-plane-cli/src/workstation.rs:18-170`](../../rust/node-plane-cli/src/workstation.rs#L18-L170), [`rust/node-plane-cli/src/operation_store.rs:21-108`](../../rust/node-plane-cli/src/operation_store.rs#L21-L108)
- Telegram Router: [`app/telegram_client/routers/admin_updates.py:236-375`](../../app/telegram_client/routers/admin_updates.py#L236-L375)

#### Lifecycle & Transitions
1. **Queueing**: `UpdateService.queue()` creates `backend_update_jobs` with status `awaiting_executor`.
   - For `stack`, it atomically inserts a row into `backend_controller_update_gate(id=1, job_id=job_id)`.
   - Items are populated into `backend_update_items`.
2. **Execution & Interruption**:
   - In `_run_stack_core()`, the update schedule is dispatched via systemd transient unit.
   - If the worker process restarts while `status == 'running'` and `result_json IS NULL`, `UpdateService.recover()` marks `backend_update_jobs.status = 'blocked'`.
   - If systemd reports unit failure, the rollback state is checked. If rollback succeeds, the job enters `rolled_back` (gate released). If rollback fails or unit evidence cannot be confirmed, it enters `blocked` (gate preserved).
3. **Recovery Paths**:
   - If `status == 'awaiting_executor'` and no child items have started: `actions: ['cancel']`.
   - If `status == 'blocked'`, `kind == 'stack'`, `phase == 'core'`, and `unit_name` is present: `actions: ['recheck']`.
   - `recheck` invokes `_run_stack_core()` to re-evaluate systemctl and journal progress files (`data/updates/<job_id>.json`).

#### Concrete Gaps
> [!WARNING]
> **GAP 1.1: Missing Recovery Actions for Non-Stack and Post-Core Updates**
> In [`app/backend/recovery.py:86-88`](../../app/backend/recovery.py#L86-L88), `actions.append('recheck')` is strictly gated on:
> ```python
> if status == 'blocked' and job['kind'] == 'stack' and result.get('phase') == 'core' and result.get('unit_name'):
>     actions.append('recheck')
> ```
> If `job['kind']` is `version`, `agents`, or `runtimes`, or if a `stack` update advanced to `phase == 'agents'` before failing, `actions` evaluates to `[]`. The job remains visibly `blocked` in the recovery overview forever with zero recovery choices.

---

### 2. Node Bootstrap Coordinator

#### Code Pointers
- Backend Service: [`app/backend/node_bootstrap.py:16-307`](../../app/backend/node_bootstrap.py#L16-L307)
- Recovery Logic: [`app/backend/recovery.py:117-125`](../../app/backend/recovery.py#L117-L125), [`app/backend/recovery.py:283-303`](../../app/backend/recovery.py#L283-L303)
- NPW Workstation Nodes: [`rust/node-plane-cli/src/nodes.rs:1437-1449`](../../rust/node-plane-cli/src/nodes.rs#L1437-L1449)

#### Lifecycle & Transitions
1. **Coordination**: `NodeBootstrapService` advances through phases: `agent` -> `docker` -> `protocols` -> `done`.
   - Phase `agent`: dispatches child `AgentRolloutService.request()` (`child_kind: 'agent-rollouts'`).
   - Phase `docker`: dispatches child `NodeOperations.queue('install_docker')` (`child_kind: 'node-jobs'`).
   - Phase `protocols`: dispatches child `NodeOperations.queue('bootstrap')` (`child_kind: 'node-jobs'`).
2. **Blocking**:
   - If a child enters `blocked`, `_advance()` sets parent `backend_node_bootstraps.status = 'blocked', error_code = 'child_operation_blocked'`.
   - If an agent probe fails with `node_agent_unavailable`, the coordinator enters `blocked`.

#### Concrete Gaps
> [!CRITICAL]
> **GAP 2.1: Rust Installation Rollout Failure Causes Unrecoverable Bootstrap Deadlock**
> **Reproducible State Sequence**:
> 1. Operator initiates bootstrap on a host without pre-built binary and without Rust installed.
> 2. `AgentRolloutService._execute()` detects `RUST_INSTALL_REQUIRED:` and sets `_failure_code = 'rust_required'`.
> 3. `backend_agent_rollouts` transitions to `blocked`; parent `backend_node_bootstraps` transitions to `blocked` (`child_operation_blocked`).
> 4. In Telegram, the operator sees the rollout failed and is offered the "Install Rust" button ([`admin_nodes.py:1894-1896`](../../app/telegram_client/routers/admin_nodes.py#L1894-L1896)).
> 5. When clicked, `rust_confirm_cb` calls `backend.rollout_agent(..., install_rust=True)` ([`admin_nodes.py:1952`](../../app/telegram_client/routers/admin_nodes.py#L1952)) without passing `bootstrap_id`.
> 6. `AgentRolloutService.request()` invokes `require_idle(conn, node_key, bootstrap_id=None)` ([`agent_rollout.py:81-82`](../../app/backend/agent_rollout.py#L81-L82)).
> 7. `require_idle()` detects the blocked parent bootstrap and raises HTTP 409 `node_operation_pending`.
> 8. The operator navigates to Recovery. For `agent`, `preflight_failed` is `False` (only True for SSH auth/key errors), so `actions` has only `['recheck']`.
> 9. For `bootstrap`, `preflight_failed` is `False`, so `actions` has only `['recheck', 'resolve']` (`cancel` is NOT offered).
> 10. `recheck` and `resolve` attempt gRPC inspection of the agent, which fails because the binary was never built.
> 11. **Outcome**: The normal bootstrap/Install Rust continuation is blocked. Whole-node registry-only retirement remains a separate explicit abandonment path, not continuation or verified remote cleanup.

> [!WARNING]
> **SAFETY LIMITATION 2.2: Offline Agent Prevents Evidence-Based Bootstrap Cancellation**
> In [`app/backend/recovery.py:286-288`](../../app/backend/recovery.py#L286-L288):
> ```python
> if row['child_kind'] == 'node-jobs':
>     from .node_operations import NodeOperations
>     NodeOperations(db, driver).resolve(actor, row['child_id'])
> ```
> `cancel` calls `NodeOperations.resolve()`, which calls `driver.node_action('resolve_' + action)`. If the target VPS crashed or the agent is offline, this gRPC call fails. The exception is caught at [`recovery.py:307`](../../app/backend/recovery.py#L307), recording audit outcome `unconfirmed` and aborting with HTTP 409 `recovery_unconfirmed`. Cancellation is refused while remote execution cannot be ruled out. This is intentional fail-closed behavior, not justification for clearing the child fence. Recovery must restore the authenticated agent, or use explicitly confirmed whole-node registry-only retirement with an unverified-runtime warning. Improve explanation and discovery; preserve the safety check.

> [!WARNING]
> **GAP 2.3: Workstation CLI Rejects Valid Bootstrap `cancel` and Rollout `resume`**
> In [`rust/node-plane-cli/src/nodes.rs:1439`](../../rust/node-plane-cli/src/nodes.rs#L1439):
> ```rust
> ensure!(["recheck", "resolve"].contains(&action.as_str()), "Unsupported recovery action");
> ```
> When viewing a node's recovery inventory in NPW, if the backend offers `action = 'cancel'` on a bootstrap job or `action = 'resume'` on an agent rollout, the workstation rejects the request locally with `"Unsupported recovery action"`, even though [`workstation.rs:601-603`](../../rust/node-plane-cli/src/workstation.rs#L601-L603) supports the endpoint path!

---

### 3. Agent Rollout

#### Code Pointers
- Backend Service: [`app/backend/agent_rollout.py:17-219`](../../app/backend/agent_rollout.py#L17-L219)
- Script Runner: [`scripts/setup_driver_agents.sh`](../../scripts/setup_driver_agents.sh)

#### Lifecycle & Transitions
1. **Invocation**: Dispatched by `NodeBootstrapService` or standalone `AgentRolloutService.request()`.
2. **Execution**: Spawns `setup_driver_agents.sh`. Captures stdout/stderr.
3. **Failure Classification**:
   - `Permission denied (publickey` -> `ssh_authentication`.
   - `Host key verification failed` -> `ssh_host_key`.
   - `SSH_PREREQUISITES_FAILED:` -> `ssh_prerequisites`.
   - `RUST_INSTALL_REQUIRED:` -> `rust_required`.
   - `Not enough free memory` / `too busy` -> `build_resources`.
4. **Recovery Predicate**:
   - `preflight_failed()` ([`agent_rollout.py:117-127`](../../app/backend/agent_rollout.py#L117-L127)) returns `True` **only if** `stage == 'check_ssh'` AND failure code in `{'ssh_authentication', 'ssh_host_key', 'ssh_prerequisites'}`.
   - If `True`: recovery offers `actions: ['recheck', 'resume']`.
   - If `False`: recovery offers only `actions: ['recheck']`.

#### Concrete Gaps
> [!WARNING]
> **RECOVERY LIMITATION 3.1: Mid-Install Failures Have No In-Place Continuation**
> If an agent rollout fails during cargo build (`build_resources`), network dropout while fetching assets, or systemd unit creation, `preflight_failed()` is `False`. The old record offers only verification, which cannot succeed until the expected authenticated agent is installed. A standalone `AgentRolloutService.request()` does not prohibit an earlier blocked rollout: a separately authorized rollout can therefore install the agent, after which rechecking the old bound record may settle it. A bootstrap parent can prohibit this replacement; that stricter case is GAP 2.1. Do not claim all standalone failures are permanent deadlocks.

---

### 4. Node Jobs (Protocol Installation, Repair, Probe, Ports)

#### Code Pointers
- Service: [`app/backend/node_operations.py:18-196`](../../app/backend/node_operations.py#L18-L196)
- Recovery: [`app/backend/recovery.py:104-107`](../../app/backend/recovery.py#L104-L107), [`app/backend/recovery.py:233-239`](../../app/backend/recovery.py#L233-L239)

#### Lifecycle & Transitions
1. **Queued Actions**: `bootstrap`, `reinstall_keep`, `reinstall_clean`, `cleanup_runtime`, `install_docker`, `check_ports`, `open_ports`, `sync_runtime`, `sync_env`, `sync_xray`, `regenerate_entropy`, `reconcile_access`.
2. **Fences**: When a node job is `awaiting_executor`, `running`, or `blocked`:
   - Rejects concurrent node jobs on the same node ([`node_operations.py:81-83`](../../app/backend/node_operations.py#L81-L83)).
   - Rejects concurrent node settings ([`node_settings.py:149-150`](../../app/backend/node_settings.py#L149-L150)).
   - Rejects concurrent profile tasks ([`executor.py:198-199`](../../app/backend/executor.py#L198-L199)).
3. **Recovery**:
   - `recheck`: calls driver with `recover=True`. If the agent's persistent journal recorded success, `_finish()` applies revision updates.
   - `resolve`: calls driver `resolve_<action>`. If `retired == True`, sets `status = 'superseded'`.

#### Concrete Gaps
> [!NOTE]
> **SAFETY LIMITATION 4.1: Unreachable or Destroyed Node Requires Explicit Retirement**
> If a managed node is completely deallocated or destroyed while a node job is `blocked`, both `recheck` and `resolve` fail with RPC transport errors. The operator cannot cancel or dismiss the job. The available abandonment path is `retire_registry_only` on the entire node; this does not prove remote cleanup. Do not implement per-job dismissal that silently removes a remote-execution fence.

---

### 5. Node Settings

#### Code Pointers
- Service & Executor: [`app/backend/node_settings.py:88-335`](../../app/backend/node_settings.py#L88-L335)
- Recovery: [`app/backend/recovery.py:104-107`](../../app/backend/recovery.py#L104-L107), [`app/backend/recovery.py:240-250`](../../app/backend/recovery.py#L240-L250)

#### Lifecycle & Transitions
1. **Queueing**: Revision-bound snapshot of node settings.
2. **Pre-flight & Execution**: In `run_one()`, if the node is uninstalled (`applied_revision == 0`), `_retire_uninstalled()` marks it `superseded` so bootstrap can take over. Otherwise calls `driver.apply_node_settings()`.
3. **Blocking**: On exception or verification failure, task transitions to `blocked`.
4. **Recovery**:
   - `recheck`: invokes `driver.recover_node_settings()`.
   - `resolve`: invokes `driver.resolve_node_settings()`. On verified observation, marks task `superseded`, bumps desired revision, and enqueues a fresh settings task.

#### Concrete Gaps
> [!WARNING]
> **ORDERING/UX 5.1: Profile Recovery Must Precede Settings Recovery**
> In [`app/backend/recovery.py:106-107`](../../app/backend/recovery.py#L106-L107):
> ```python
> if kind == 'settings' and any(conn.execute(f"SELECT 1 FROM {table} WHERE node_key=? AND status IN ('awaiting_executor','running','blocked') LIMIT 1", (row['node_key'],)).fetchone() for table in ('backend_node_jobs','backend_agent_rollouts','backend_node_bootstraps','backend_operation_tasks')):
>     actions.remove('resolve')
> ```
> And in [`app/backend/node_settings.py:215-217`](../../app/backend/node_settings.py#L215-L217):
> ```python
> if conn.execute('''SELECT 1 FROM backend_operation_tasks WHERE node_key = ?
>     AND status IN ('running', 'blocked')''', (row['node_key'],)).fetchone():
>     raise AccessDenied('profile_operation_uncertain', 409)
> ```
> Conversely, [`app/backend/executor.py:196-197`](../../app/backend/executor.py#L196-L197) prevents profile tasks from executing while settings tasks are `blocked`.
> If both a profile task and a settings task are `blocked` on the same node, the operator **must resolve the profile task first**. If they attempt to resolve the settings task first, `resolve` is stripped from actions or fails with HTTP 409. The UI does not explain this prerequisite ordering.

---

### 6. Profile Operations & Scheduled Expiry

#### Code Pointers
- Repository & Executor: [`app/backend/operations.py:14-139`](../../app/backend/operations.py#L14-L139), [`app/backend/executor.py:19-244`](../../app/backend/executor.py#L19-L244)
- Recovery: [`app/backend/recovery.py:108-109`](../../app/backend/recovery.py#L108-L109), [`app/backend/recovery.py:251-262`](../../app/backend/recovery.py#L251-L262)

#### Lifecycle & Transitions
1. **Transactional Outbox**: Granular tasks in `backend_operation_tasks` (`ensure` or `delete` per protocol/device). Parent in `backend_operations`.
2. **Crash & Blocking**:
   - `IntentExecutor.recover()` marks all `running` tasks and operations `blocked`.
   - Any driver failure in `run_one()` marks task `blocked` with sanitized error code.
   - `refresh_operation()` marks parent `backend_operations` `blocked` if any child task is `blocked`.
3. **Scheduled Expiry**:
   - `IntentExecutor.queue_expired_profiles()` runs every worker cycle. If `expires_at <= now()`, bumps desired revision and records deletion tasks.
4. **Recovery**:
   - `recheck`: reads journal via `driver.lookup()`. If confirmed, updates task to `succeeded` and refreshes parent.
   - `resolve`: calls `driver.resolve()` to confirm absence/state. Supersedes blocked tasks, bumps revision, queues replacement operation, updates backup restore dependencies, and records evidence in `backend_repairs`.

---

### 7. Temporary Configs (Issuance, Revoke, Expiry)

#### Code Pointers
- Service: [`app/backend/temporary_configs.py:18-375`](../../app/backend/temporary_configs.py#L18-L375)
- Recovery: [`app/backend/recovery.py:99-100`](../../app/backend/recovery.py#L99-L100), [`app/backend/recovery.py:226-232`](../../app/backend/recovery.py#L226-L232)

#### Lifecycle & Transitions
1. **Two-Stage Status Model**:
   - Issuance: `queued` -> `issuing` -> `active`. If interrupted/failed -> `blocked`.
   - Revocation: `active` -> `revoking` -> `expired`/`revoked`. If interrupted/failed -> `revoke_blocked`.
2. **Recovery Actions**:
   - `blocked`: offers `actions: ['recheck', 'resolve']`.
     - `recheck`: calls `reconcile_one()`.
     - `resolve`: calls `resolve_unused()` which requires `driver.node_action('temporary_resolve')` proving remote absence, then sets `status = 'cancelled'`.
   - `revoke_blocked`: offers only `actions: ['recheck']`.

#### Concrete Gaps
> [!NOTE]
> **SAFETY LIMITATION 7.1: Unconfirmed Revocation Requires Connectivity or Retirement**
> If a temporary config fails revocation because the node became unreachable, it enters `revoke_blocked`. Recovery offers only `recheck`. If the agent cannot be contacted, `recheck` cannot exit `revoke_blocked`. It must remain unconfirmed until authenticated evidence confirms revocation, or the operator explicitly retires the node without claiming remote cleanup. Absence of a generic Cancel is not by itself a bug.

---

### 8. Config Issuance (Client Artifact Generation)

#### Code Pointers
- Service: [`app/backend/config_issuance.py:28-330`](../../app/backend/config_issuance.py#L28-L330)
- Backup Interference: [`app/backend/backups.py:408-414`](../../app/backend/backups.py#L408-L414), [`app/backend/backups.py:462`](../../app/backend/backups.py#L462)

#### Lifecycle & Transitions
1. **Request & Claim**: `ConfigIssuanceService.request()` queues `awaiting_executor`.
2. **Worker Execution**: Worker claims `running`, calls `_live()` to inspect agent metadata and verify runtime endpoints.
3. **Blocking**:
   - On exception: [`config_issuance.py:271`](../../app/backend/config_issuance.py#L271):
     `UPDATE backend_config_issuances SET status = 'blocked' WHERE id = ? AND status = 'running'`
   - On worker restart: [`config_issuance.py:277`](../../app/backend/config_issuance.py#L277):
     `UPDATE backend_config_issuances SET status = 'blocked' WHERE status = 'running'`

#### Concrete Gaps
> [!CRITICAL]
> **GAP 8.1: Unlisted Blocked Config Issuance Prevents Backup Restore Admission**
> **Reproducible State Sequence**:
> 1. User or admin requests a configuration artifact (Xray/AWG) via HTTP API or Telegram.
> 2. While the worker executes `ConfigIssuanceService.run_one()`, a transient gRPC timeout or agent restart occurs during `_live()`.
> 3. The exception is caught at [`config_issuance.py:266`](../../app/backend/config_issuance.py#L266) and the task is updated to `status = 'blocked'`.
> 4. `ConfigIssuanceService.recover()` only blocks interrupted work; there is no terminal reconciliation for these rows, and is **omitted from `recovery.py` `SOURCES`**.
> 5. Later, an administrator attempts to perform a backup restore via [`BackupService.queue(action='restore')`](../../app/backend/backups.py#L458).
> 6. `queue()` checks [`BackupService.busy(conn)`](../../app/backend/backups.py#L408-L414):
> ```python
> for table in (
>     "backend_node_bootstraps", "backend_node_jobs", "backend_node_settings_tasks",
>     "backend_operation_tasks", "backend_config_issuances", "backend_update_jobs",
> ):
>     if conn.execute(f"SELECT 1 FROM {table} WHERE status IN ('awaiting_executor','running','blocked') LIMIT 1").fetchone():
>         return True
> ```
> 7. Because `backend_config_issuances` contains the blocked row, `busy(conn)` returns `True`.
> 8. `BackupService.queue()` immediately raises HTTP 409 `maintenance_busy`.
> 9. The recovery dashboard does not display the blocked config issuance, so the operator has no insight into why the backup restore is blocked and cannot resolve it from any UI or API endpoint.

---

### 9. Backups (Create, Restore, Import, Scheduled)

#### Code Pointers
- Service: [`app/backend/backups.py:95-891`](../../app/backend/backups.py#L95-L891)
- Recovery: [`app/backend/recovery.py:89-97`](../../app/backend/recovery.py#L89-L97), [`app/backend/recovery.py:217-220`](../../app/backend/recovery.py#L217-L220)

#### Lifecycle & Transitions
1. **Create**: Takes repeatable-read PostgreSQL snapshot across tables, hashes payload, writes atomically with fsync.
2. **Restore**:
   - Phase 1: `not job['phase']`. Creates `pre_restore` snapshot. Freezes profiles, records deletion tasks in `backend_backup_revocations`, transitions to `phase = 'revoking'`.
   - Phase 2: `phase == 'revoking'`. Waits for revocations to succeed. Wipes DB and restores configuration tables. Transitions to `phase = 'reconnecting'`.
   - Phase 3: `phase == 'reconnecting'`. Triggers `NodeBootstrapService.queue(recover_unreachable=True)` for each enabled node. Transitions to `phase = 'restored', status = 'succeeded'`.
3. **Recovery**:
   - `cancel`: available only while `status == 'awaiting_executor'` and `not phase`.
   - `resume`: available when `status == 'blocked'`, provided that revocations are terminal (`succeeded`/`no_targets`) or reconnecting nodes payload is intact.

#### Concrete Gaps
> [!WARNING]
> **GAP 9.1: Failed `create` Backups Cannot Be Cancelled**
> In [`app/backend/recovery.py:90-97`](../../app/backend/recovery.py#L90-L97):
> For `action == 'create'`, `phase` is always empty. If a create backup fails (e.g. disk quota exceeded) and becomes `blocked`:
> `actions` evaluates to `['resume']`. It **never** offers `cancel`. The operator cannot dismiss or cancel a failed backup creation job.

---

### 10. System Cleanup (Reset & Remove)

#### Code Pointers
- Service: [`app/backend/system_cleanup.py:33-674`](../../app/backend/system_cleanup.py#L33-L674)
- Gate Integration: [`app/backend/maintenance_gate.py:1-25`](../../app/backend/maintenance_gate.py#L1-L25), [`app/backend/http_api.py:930-940`](../../app/backend/http_api.py#L930-L940)

#### Lifecycle & Transitions
1. **Phases**: `backup` (reset only) -> `nodes` (node removal saga) -> `local_state` -> `local_files` -> `uninstall_prepare` -> `shutdown` -> `uninstall_launch` -> `complete`.
2. **Fences**: When active or blocked, `maintenance_gate.active()` returns `True`, preventing non-cleanup writes.
3. **Recovery Routes**: Dedicated endpoints `/api/v1/system/cleanup/jobs/{id}/retry` and `/abort`.

#### Concrete Gaps
> [!CRITICAL]
> **GAP 10.1: Blocked Cleanup Silently Paralyzes All Other Recovery Without Being Visible**
> In [`app/backend/recovery.py:40`](../../app/backend/recovery.py#L40):
> ```python
> other = bool(conn.execute("SELECT id FROM backend_system_cleanup_jobs WHERE status IN ('queued','running','awaiting_shutdown','blocked') LIMIT 1").fetchone())
> ```
> If a system cleanup job enters `status = 'blocked'`:
> 1. `other` evaluates to `True`.
> 2. In [`recovery.py:83, 89, 98`](../../app/backend/recovery.py#L83-L98), recovery actions for the listed operation families (updates, nodes, profiles, backups, removals) are stripped (`actions = []`).
> 3. `backend_system_cleanup_jobs` is **omitted from `recovery.py` `SOURCES`**.
> 4. The recovery overview displays `maintenance_active: True`, but **the cleanup job itself is not listed**. Dedicated cleanup Retry/Abort routes and bot/NPW controls do exist; this is a discovery gap in Operations, not absence of all recovery functionality.
> 5. Operators looking only at Operations cannot identify the blocking cleanup job or navigate directly to its dedicated recovery controls. Retry/Abort restrictions after destructive boundaries remain intentional.

---

### 11. Node Removal (Verified & Registry-Only)

#### Code Pointers
- Service: [`app/backend/node_removal.py:14-175`](../../app/backend/node_removal.py#L14-L175)
- Lifecycle Saga: [`app/backend/node_lifecycle.py:17-422`](../../app/backend/node_lifecycle.py#L17-L422)
- Verifier: [`app/backend/removal_verifier.py:1-240`](../../app/backend/removal_verifier.py#L1-L240)

#### Lifecycle & Transitions
1. **Saga Steps**:
   - `bind_verification_target`: records host fingerprint and inventory.
   - `start_drain`: stops grants, queues profile revocations.
   - `cleanup`: prepares decommissioning, deletes runtime, schedules uninstall.
   - `retire_verified`: independent SSH host verification of absent artifacts -> deletes node record.
2. **Emergency Escape**:
   - `retire_registry_only`: explicitly abandons remote host certainty, supersedes in-flight tasks, and records tombstone in `backend_node_retirements`.
3. **Recovery**:
   - `recovery.py` offers `actions: ['resume']` while the node exists in `backend_nodes`.
   - `retire_registry_only` is accessible via `/api/v1/nodes/{key}/retire-registry-only`, but **not** offered directly within the `recovery.overview()` action list.

---

### 12. Controller Diagnostics & Local Workstation Operations

#### Code Pointers
- Workstation Operation Store: [`rust/node-plane-cli/src/operation_store.rs:21-108`](../../rust/node-plane-cli/src/operation_store.rs#L21-L108)
- Installer Diagnostics: [`rust/node-plane-cli/src/installer.rs:150-200`](../../rust/node-plane-cli/src/installer.rs#L150-L200), [`scripts/installation_diagnostics.py:197-221`](../../scripts/installation_diagnostics.py#L197-L221)

#### Lifecycle & Transitions
1. **Workstation Operation Store**:
   - Durable file `operations/update-<uuid>.json` written before dispatch.
   - On interruption, prints resume instruction: `node-plane update --operation <UUID>`.
   - Binds lookup via `GET /api/v1/system/updates/jobs/<id>` without replaying remote mutations.
2. **Controller Repair**:
   - `node-plane diagnose --repair`: inspects systemd units (`node-plane-backend`, `node-plane-telegram`, `node-plane-driver`, `node-plane-backend-worker`).
   - If failed/inactive, calls `scripts/installation_diagnostics.py --repair <unit>`, which issues `systemctl start` after confirming channel validity.

---

### 13. First SSH Setup & Pre-Agent Operation Risk (Investigation)

#### Code Pointers
- SSH Connection & Enrollment: [`rust/node-plane-cli/src/ssh.rs:104-260`](../../rust/node-plane-cli/src/ssh.rs#L104-L260)
- Node Preparation: [`rust/node-plane-cli/src/workstation.rs:230-284`](../../rust/node-plane-cli/src/workstation.rs#L230-L284)
- Agent Rollout Driver Script: [`app/backend/agent_rollout.py:152-198`](../../app/backend/agent_rollout.py#L152-L198)

#### Source Investigation
When setting up a new target node:
1. **Pubkey Absence Risk**: Fresh VPS instances typically start with password authentication or a personal SSH key. The controller has an independent SSH keypair (`os.environ['SSH_KEY']`).
2. **Bot-Direct Onboarding Trap**:
   - If an administrator adds an SSH node via Telegram without running the workstation first, the backend initiates agent rollout using the controller's key.
   - The connection is rejected by sshd with `Permission denied (publickey)`.
   - `AgentRolloutService` catches this as `ssh_authentication`. Because `stage == 'check_ssh'`, `preflight_failed()` is `True`.
   - While `preflight_failed` allows `resume`, repeatedly resuming will continually fail until the controller's public key is placed in `~/.ssh/authorized_keys` on the target host.
3. **Workstation Enrollment Solution**:
   - NPW provides `prepare-node` (`workstation.rs:230`):
     - Workstation connects to target via SSH password/interactive prompt.
     - Workstation enrolls its own key (`workstation-key-enrollment`).
     - Workstation retrieves the controller's public key via `GET /api/v1/system/ssh-key`.
     - Workstation enrolls the controller's public key on the target VPS (`controller-key-enrollment`).
     - Workstation verifies direct controller-to-target SSH authentication.
     - An audit record is logged via `POST /api/v1/system/workstation-audit`.
4. **Pre-Agent Mutation Risk**:
   - If an operator issues maintenance commands (e.g. `check_ports`, `open_ports`, `sync_runtime`) on a node where the agent has not yet been rolled out:
   - `require_idle()` blocks the job if bootstrap is active.
   - If no bootstrap is running, the job is claimed by the worker and attempts gRPC to `driver.node_action()`.
   - The gRPC call fails with `StatusCode.FAILED_PRECONDITION ('no node-agent target configured')` or `StatusCode.UNAVAILABLE`.
   - The node job transitions to `blocked` (`node_agent_unconfigured`), creating a fence that blocks subsequent settings and operations on that node until recovered.

---

## Cross-Cutting Mechanisms

### Maintenance Gates & Outbox Model
1. **Controller Update Gate**:
   `backend_controller_update_gate` enforces mutual exclusion during core stack updates. When present, `http_api.py` middleware rejects all non-recovery mutations.
2. **Cleanup & Restore Gate**:
   `backend_system_cleanup_jobs` (`queued`, `running`, `awaiting_shutdown`, `blocked`) and `backend_backup_jobs` (`restore` in `awaiting_executor`, `running`) block admission (`maintenance_gate.active()` covers cleanup/update; `admit()` separately checks restore).
3. **Transactional Outbox & Worker Mutex**:
   - Exclusive lock file `/opt/node-plane/shared/data/backend-worker.lock` ensures a single active worker.
   - Daemons take lifetime lock `backend-worker.lock.daemon` while acquiring/releasing the mutation lock every 5 seconds.
   - HTTP recovery endpoints in `http_api.py` use `maintenance_lock(current)` to acquire the worker lock, ensuring no concurrent worker cycle interferes with manual recovery.
4. **Idempotency**:
   Durable command families use persisted idempotency keys; column names and key validators vary by family. This is not a claim that every HTTP write endpoint has an identical schema.

---

## Review Corrections and Verification Results — 2026-10-24

The primary agent reviewed the delegated report against source and corrected
unsafe or overbroad recommendations. No production code or remote runtime was
changed. The report covers discovered families; it is not an exhaustive live
fault-injection certificate.

- Existing backend/Telegram recovery regression modules: **29 PASS** (3.14s).
  Initial sandbox TestClient run hung and was interrupted; the bounded rerun
  outside sandbox completed. One existing Starlette/httpx deprecation warning.
- A disposable database probe confirmed that an uninstalled node
  (`enabled=0, applied_revision=0`) admits `open_ports` as `awaiting_executor`.
  No driver was called.
- A reachable update probe queued `agents` with the existing test updater/driver
  and a pure failing runner. Worker advanced the driver item and parent to
  `blocked`; inventory advertised no actions. Changing the mock runner to
  success did not advance the blocked parent. No installer was executed.
- Additional injected blocked-record probes verified absent update actions.
  The valid fleet kind is **`runtimes`**, not `runtime`; the injected `runtime`
  case alone is not evidence of a public reachable kind. The reachable agents
  probe above is the stronger evidence.
- A fourth disposable probe queued a legitimate Xray config issuance with a
  fake driver throwing `TimeoutError`. Restore `busy` changed from False to
  True; issuance became blocked, was absent from recovery and was not picked
  up by another worker cycle. First probe attempt lacked self-config scope and
  correctly failed authorization; it was rerun with the normal full admin scope
  union, without changing authorization code. No external driver was contacted.
- Delegated test execution was skipped after its attempt encountered a local
  virtualenv lock restriction. Existing test code was read, not executed by agy.
- Dedicated NPW cleanup Retry/Abort does exist:
  `controller.rs:332-344`, `tui/actions/controller.rs:1004-1038,1548-1550`.
  Main Diagnostic → Operations uses `controller::Command::Recover`; the narrower
  node diagnostics path uses `nodes::Command::Recover`. The latter rejects
  resume/cancel, so this is a **node-card path discrepancy**, not a universal
  failure of NPW recovery.
- A missing offline cancellation path is not permission to drop execution
  evidence. These cases are safety limitations unless a safe terminal
  alternative can be proved. Profile/settings ordering has a valid resolution
  order and is not a demonstrated circular deadlock.

## Actionable Recommendations & Prioritized Fixes

1. **Unlisted config issuances and restore admission**: implement an explicit,
   audited terminal/reconciliation policy for expired/failed artifact reads,
   distinguishing them from VPN access mutations. Prove that retiring an
   issuance cannot lose a remote mutation before changing `BackupService.busy`.
   Do not merely remove busy checks or delete blocked rows.
2. **Update recovery**: add verified reconciliation/continuation for non-stack
   fleet parents and controller-version jobs. Adopt existing child outcomes and
   verify expected builds; safely distinguish not-dispatched, failed and unknown
   launches. Include stack core launch-evidence loss. Do not blindly cancel a
   launched update or clear its maintenance gate.
3. **Agent/bootstrap recovery**: support evidence-bound continuation after
   known installer failures, including rust-required/build failures, retaining
   parent/child identity and SSH binding. Prove the failure boundary before
   allowing a retry or cancellation. Include deleted/demoted initiating actors
   in follow-up tests; authorization takeover is not established by this audit.
4. **Pre-agent admission**: reject runtime actions before creating durable jobs;
   retain settings edits, setup and registry-only removal. Distinguish an
   uninstalled node from a temporarily unreachable installed agent. Apply the
   same eligibility policy in bot and NPW.
5. **Client parity**: align node-card `Command::Recover` with validated backend
   actions via the existing safe path builder. Keep kind/ID/node checks and
   explicit confirmation. Test Diagnostic Operations and node diagnostics
   separately, plus `--no-tui`.
6. **Cleanup discoverability**: expose the active cleanup job and navigation to
   its existing dedicated recovery controls from Operations. Preserve ownership,
   confirmation and irreversible-phase checks rather than adding a generic
   force-unlock operation.
7. **Secondary UX**: explain profile-before-settings recovery order, offline
   evidence requirements and registry-only retirement. Consider safe terminal
   cancellation for failed snapshot creation, preserving published files and
   truthful cleanup status. This is lower priority than restore/update deadlocks.

## Follow-up Acceptance Tests

For each changed family, run one deterministic sequence: queue → known failure
or interrupted observation → documented bot/NPW recovery → terminal parent and
child state → next admitted operation. Add a negative case for missing or
mismatched evidence. Existing passing coverage is reused; do not expand into
five redundant paths for every endpoint. Live SSH/systemd/IPv6 outcomes remain
separate owner acceptance checks, not implicit passes from this source audit.

---

## Implementation Follow-up (2026-10-25)

*Recorded 2026-10-25 following primary implementation and bounded regression verification.*

### Current Recovery Invariants

1. **Global Update Gate Enforcement (`backend_controller_update_gate`)**:
   - `UpdateService.queue()` now atomically asserts and creates `backend_controller_update_gate(id=1, job_id=job_id)` for **both** `stack` and `version` update jobs.
   - Core version execution is unified with `_run_stack_core()`. Once dispatched, an uncertain launch, unit mismatch, or missing progress receipt retains the gate fence.
   - Subsequent admission is strictly blocked (`update_pending` / HTTP 409 `system_cleanup_in_progress` middleware) while prior outcome remains unconfirmed.
   - Workaround test patterns that manually deleted gate rows have been removed; tests must never clear a fence merely to let subsequent test scenarios proceed.
   - Known predispatch rejections (e.g. invalid targets, conflicting pre-checks) allow positive cancellation and release the gate.

2. **Terminal Evidence & UUID Binding Persistence**:
   - The planned UUID-bound systemd unit and launch state are persisted in `backend_update_jobs.result_json` before dispatch. The installer publishes component, status and rollback receipts at `data/updates/<job_id>.json`; the filename binds the receipt to the operation.
   - **Safety Boundary**: Do NOT claim every outcome can force-unlock! Legacy version updates can lack terminal evidence after systemd `--collect` (no stack receipt); while UUID binding is saved, absent evidence remains blocked. New controller-only (`version`) and full-stack jobs both use the atomic core installer and its receipts; controller-only jobs finish after core verification without updating the node fleet. Absent terminal evidence must remain blocked; fences are never cleared blindly.

3. **Pre-Agent Node Lifecycle Guarding**:
   - Uninstalled nodes (`applied_revision == 0`) strictly reject runtime commands (`open_ports`, probe, diagnostics, repair) prior to installation.
   - In Telegram node screens, `draft_controls` returns empty lists in submenus for uninstalled nodes; setup is exposed only on the root screen.

4. **Client Parity & Navigation Parity**:
   - **NPW Workstation Recovery**: Node card recovery is aligned with validated backend actions via the safe path builder, supporting `cancel` on pre-dispatch bootstrap and `resume` where admissible.
   - **NPW Footer Geometry**: Pinned `Back` button is preserved outside the scrollable footer bounds; verified with member cards present to prevent off-by-one reserved row regressions.
   - **NPW Requests Tab**: Tab buttons render compact `@username · DD/MM/YY` or `tg_id · DD/MM/YY` with nested `_account` fallback.
   - **Telegram Diagnostics Breadcrumbs**: `node_tools:{key}` path preserves `Technical details` parent (`node_technical:{key}`) with current breadcrumb `Diagnostics` (`node_tools.diagnostics`).
   - **Telegram Screen.rich Rendering**: Includes `Bot public key` heading before noncollapsed code blocks even when iOS `uri_rows` links are attached; normal client URI screens remain collapsed.
   - **Expiry Parser Parity**: Python rejects duration tokens with internal whitespace (`'1d 2h'`); Rust rejects signed or malformed ISO date components (`2099-+1-01`, `2099-01-+1` with reference `now2026`).

5. **Temporary Configs & Issuance Parity**:
   - Separation of artifact view and info card: artifact view provides only a `Back` button to the exact card (`temporary:card:{id}`), without `Refresh`, `Revoke`, or status/expiry caveats; breadcrumbs reflect config identity then Config.
   - VLESS warning appears on confirmation and active info cards (with leading `\n`), but is suppressed during queued/issuing states.
   - iOS link delivery uses a code entity with exact UTF-16 code unit length and retains the close button panel (`config_uri_close`).
   - Filenames and URI fragments include transport for VLESS (`{server} · VLESS · {transport}.txt`) and `Temp` markers for temporary configs (`{server} · AWG · Temp.vpn`, `{server} · VLESS · {transport} · Temp.txt`).

### Verification Status & Boundaries

- **Focused Test Execution**:
  - Python: 159 tests passed across 11 allowed test suites (`tests/test_installation_defaults.py`, `tests/test_telegram_node_templates.py`, `tests/test_telegram_temporary_configs.py`, `tests/test_backend_config_issuance.py`, `tests/test_backend_temporary_configs.py`, `tests/test_telegram_navigation.py`, `tests/test_telegram_request_setup.py`, `tests/test_telegram_admin_navigation_sections.py`, `tests/test_access_expiry.py`, `tests/test_backend_updates.py`, `tests/test_update_recovery_safety_followup.py`).
  - Rust: 3 focused TUI/CLI unit tests passed in `rust/node-plane-cli/src/tui/tests.rs`.
- **Remaining Boundaries**:
  - Final local gate:1468 Python tests passed (149 integration-fixture skips,2 existing warnings),334 Rust tests passed,12 PostgreSQL recovery tests passed separately. Strict Clippy, formatting, shell syntax and fresh NPW --no-tui version/help checks passed. This does not claim all integration environments were exercised.
  - Owner live visual verification and real-client IPv6 leak test remain pending.

Release installation now requires published Driver/Agent binaries, with no server-side Rust build fallback. Failed artifact reads and cleanup jobs are discoverable in recovery; cancellation of an artifact read preserves VPN access. Known pre-install failures may be retried after correcting their cause. Snapshot cancellation preserves already published files.
