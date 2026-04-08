# Node Driver Architecture

## Goal

Introduce a Rust node driver service that keeps long-lived connections to nodes and becomes the single execution layer for node operations.

After this change:

- Python bot owns Telegram UI, business rules, and desired state.
- Rust driver owns node connectivity, runtime execution, reconciliation, and telemetry collection.
- Direct SSH/runtime manipulation from the bot is gradually removed.

This is meant to be the target architecture for:

- migration away from `python-telegram-bot` v13-era sync assumptions;
- removal of SSH/shell-heavy logic from Telegram handlers;
- future async integration via gRPC from the bot to the driver.

## Current State

Today the bot mixes several responsibilities:

1. Telegram update handling and UI rendering.
2. Domain state and SQLite persistence.
3. Node transport and command execution over SSH/local shell.
4. Runtime reconciliation against AWG/Xray state on nodes.
5. Telemetry collection and operational retries.

Examples of the current execution layer inside Python:

- `app/services/server_runtime.py`
- `app/services/server_bootstrap.py`
- `app/services/awg.py`
- `app/services/xray.py`
- `app/services/traffic_usage.py`

This makes the bot process carry both control-plane and data-plane responsibilities.

## Target Split

### Python Bot Responsibilities

The Python app remains the control plane and user-facing surface:

- Telegram handlers, callbacks, wizards, keyboards, localization.
- Business entities:
  - profiles
  - users
  - access methods
  - server registry
- Desired state:
  - which profile should exist on which server
  - which protocols are enabled
  - what the admin requested
- Read models for UI and admin summaries.
- Persistence of product/business state in SQLite.
- Recording operational summaries returned by the driver.

Python should not be responsible for directly executing remote node changes once the driver is introduced.

### Rust Driver Responsibilities

The Rust service becomes the execution and runtime layer:

- maintain connections to nodes;
- handle SSH or any future transport implementation;
- perform bootstrap and runtime management;
- provision and delete AWG/Xray entities on nodes;
- reconcile desired and observed state on nodes;
- collect telemetry and traffic snapshots;
- emit structured operation progress and final results;
- centralize retries, backoff, health tracking, and concurrency control.

Rust should not know about Telegram flows, callback payloads, wizard state, or UI strings.

## Ownership Rule

There must be exactly one owner of node mutations.

Once the driver exists, all node-changing operations should go through it:

- bootstrap;
- sync env;
- create/delete remote profiles;
- telemetry setup;
- diagnostics;
- reconciliation;
- traffic collection.

Avoid a hybrid mode where both Python and Rust can independently mutate node runtime for the same feature. That will create state drift quickly.

## Recommended Control Flow

### Fast Operations

For short operations:

1. Bot receives Telegram update.
2. Bot validates permissions and business constraints.
3. Bot calls gRPC method on driver.
4. Driver executes.
5. Bot renders result.

### Long Operations

For slow operations:

1. Bot submits an operation request.
2. Driver returns `operation_id` immediately.
3. Driver runs the operation asynchronously.
4. Bot polls or subscribes to progress.
5. Bot updates wizard/progress message.
6. Bot stores final result into local state tables if needed.

This is preferable to a single RPC blocking for tens of seconds, because Telegram UX and retries become much easier to manage.

## Data Ownership

### Python as Source of Truth

Python should remain the source of truth for business/domain data:

- profiles;
- profile protocol selection;
- access policy;
- registered servers;
- local metadata used by the admin UI.

### Rust as Source of Truth

Rust should own operational/runtime state:

- current node session state;
- transport health;
- last observed remote runtime facts;
- operation logs and progress;
- capability discovery;
- low-level telemetry collection state.

### Synchronization Model

The synchronization contract should be:

- Python sends desired state or commands.
- Rust returns observed state, remote identifiers, operation status, and structured errors.
- Python stores summarized operational state in local tables such as `profile_server_state`.

## Suggested Internal Python Boundary

Before or during the Rust rollout, add a single abstraction in Python:

- `NodeDriverClient`

This client should become the only entry point used by bot services that need node execution.

Existing Python modules like these:

- `app/services/server_runtime.py`
- `app/services/server_bootstrap.py`
- `app/services/awg.py`
- `app/services/xray.py`
- `app/services/traffic_usage.py`

should gradually stop performing transport work directly and instead delegate to `NodeDriverClient`.

That keeps the handler and domain layers stable while the execution backend changes underneath.

## Service Layout

The gRPC surface can be split into four main areas.

### 1. NodeService

Purpose:

- node registration status;
- connectivity;
- health;
- capabilities;
- diagnostics.

Typical methods:

- `GetNode`
- `ListNodes`
- `WatchNodeHealth`
- `GetNodeDiagnostics`
- `SyncNodeEnv`

### 2. ProvisioningService

Purpose:

- manage desired-vs-actual profile/runtime state on nodes.

Typical methods:

- `EnsureProfileOnNode`
- `DeleteProfileFromNode`
- `ReconcileNode`
- `ReconcileProfile`
- `ListRemoteProfiles`

### 3. RuntimeService

Purpose:

- runtime/bootstrap/container-level operations.

Typical methods:

- `BootstrapNode`
- `RestartService`
- `GetRuntimeStatus`
- `FullCleanupNode`

### 4. TelemetryService

Purpose:

- traffic and health reporting.

Typical methods:

- `CollectTrafficSnapshot`
- `GetProfileUsage`
- `GetNodeTelemetry`
- `WatchOperationEvents`

## Operation Model

Every slow or failure-prone action should be represented as an operation.

Suggested shape:

- operation id
- type
- target node
- target profile
- status
- started at / updated at / finished at
- progress message
- structured error
- machine-readable result payload

Suggested statuses:

- `PENDING`
- `RUNNING`
- `SUCCEEDED`
- `FAILED`
- `CANCELLED`

This model should be uniform across bootstrap, reconcile, profile creation, cleanup, and telemetry setup.

## Error Model

Do not return only raw shell text.

Return structured errors with:

- code
- summary
- detail
- retryable flag
- node key
- protocol kind if relevant

Raw logs can still be attached as an optional detail field, but bot-facing logic should not need to parse shell output.

## Desired API Shape

Below is a draft protobuf sketch. It is intentionally compact and should be treated as a starting point, not a final schema.

```proto
syntax = "proto3";

package nodeplane.driver.v1;

service NodeService {
  rpc GetNode(GetNodeRequest) returns (Node);
  rpc ListNodes(ListNodesRequest) returns (ListNodesResponse);
  rpc GetNodeDiagnostics(GetNodeDiagnosticsRequest) returns (GetNodeDiagnosticsResponse);
  rpc WatchNodeHealth(WatchNodeHealthRequest) returns (stream NodeHealthEvent);
  rpc SyncNodeEnv(SyncNodeEnvRequest) returns (StartOperationResponse);
}

service ProvisioningService {
  rpc EnsureProfileOnNode(EnsureProfileOnNodeRequest) returns (StartOperationResponse);
  rpc DeleteProfileFromNode(DeleteProfileFromNodeRequest) returns (StartOperationResponse);
  rpc ReconcileNode(ReconcileNodeRequest) returns (StartOperationResponse);
  rpc ReconcileProfile(ReconcileProfileRequest) returns (StartOperationResponse);
  rpc ListRemoteProfiles(ListRemoteProfilesRequest) returns (ListRemoteProfilesResponse);
}

service RuntimeService {
  rpc BootstrapNode(BootstrapNodeRequest) returns (StartOperationResponse);
  rpc FullCleanupNode(FullCleanupNodeRequest) returns (StartOperationResponse);
  rpc GetRuntimeStatus(GetRuntimeStatusRequest) returns (GetRuntimeStatusResponse);
}

service TelemetryService {
  rpc CollectTrafficSnapshot(CollectTrafficSnapshotRequest) returns (StartOperationResponse);
  rpc GetProfileUsage(GetProfileUsageRequest) returns (GetProfileUsageResponse);
}

service OperationService {
  rpc GetOperation(GetOperationRequest) returns (Operation);
  rpc WatchOperation(WatchOperationRequest) returns (stream OperationEvent);
  rpc ListOperations(ListOperationsRequest) returns (ListOperationsResponse);
}

message Node {
  string node_key = 1;
  string transport = 2;
  string version = 3;
  string state = 4;
  NodeCapabilities capabilities = 5;
  NodeHealth health = 6;
}

message NodeCapabilities {
  bool supports_awg = 1;
  bool supports_xray = 2;
  bool supports_telemetry = 3;
  bool supports_bootstrap = 4;
}

message NodeHealth {
  string connectivity = 1;
  string last_seen_at = 2;
  string summary = 3;
}

message ProfileSpec {
  string profile_name = 1;
  repeated string protocol_kinds = 2;
  optional AwgSpec awg = 3;
  optional XraySpec xray = 4;
}

message AwgSpec {
  string profile_name = 1;
  optional string peer_name = 2;
}

message XraySpec {
  string profile_name = 1;
  string uuid = 2;
  optional string short_id = 3;
}

message EnsureProfileOnNodeRequest {
  string node_key = 1;
  ProfileSpec profile = 2;
}

message DeleteProfileFromNodeRequest {
  string node_key = 1;
  string profile_name = 2;
  repeated string protocol_kinds = 3;
}

message ReconcileNodeRequest {
  string node_key = 1;
}

message ReconcileProfileRequest {
  string profile_name = 1;
}

message ListRemoteProfilesRequest {
  string node_key = 1;
}

message RemoteProfileRecord {
  string profile_name = 1;
  string protocol_kind = 2;
  string remote_id = 3;
  string status = 4;
}

message ListRemoteProfilesResponse {
  repeated RemoteProfileRecord items = 1;
}

message BootstrapNodeRequest {
  string node_key = 1;
}

message FullCleanupNodeRequest {
  string node_key = 1;
}

message SyncNodeEnvRequest {
  string node_key = 1;
}

message GetRuntimeStatusRequest {
  string node_key = 1;
}

message RuntimeServiceStatus {
  string service_name = 1;
  string state = 2;
  string summary = 3;
}

message GetRuntimeStatusResponse {
  string node_key = 1;
  repeated RuntimeServiceStatus services = 2;
}

message CollectTrafficSnapshotRequest {
  repeated string node_keys = 1;
}

message GetProfileUsageRequest {
  string profile_name = 1;
  string protocol_kind = 2;
  string period = 3;
}

message ProfileUsage {
  string profile_name = 1;
  string protocol_kind = 2;
  uint64 rx_bytes = 3;
  uint64 tx_bytes = 4;
  uint64 total_bytes = 5;
  uint32 samples = 6;
  uint32 peers = 7;
}

message GetProfileUsageResponse {
  ProfileUsage usage = 1;
}

message GetNodeRequest {
  string node_key = 1;
}

message ListNodesRequest {}

message ListNodesResponse {
  repeated Node items = 1;
}

message GetNodeDiagnosticsRequest {
  string node_key = 1;
}

message GetNodeDiagnosticsResponse {
  string node_key = 1;
  string summary = 2;
  repeated DiagnosticItem items = 3;
}

message DiagnosticItem {
  string kind = 1;
  string status = 2;
  string summary = 3;
  string detail = 4;
}

message WatchNodeHealthRequest {
  repeated string node_keys = 1;
}

message NodeHealthEvent {
  string node_key = 1;
  NodeHealth health = 2;
}

message StartOperationResponse {
  string operation_id = 1;
}

message GetOperationRequest {
  string operation_id = 1;
}

message WatchOperationRequest {
  string operation_id = 1;
}

message ListOperationsRequest {
  string node_key = 1;
  string profile_name = 2;
  string status = 3;
  uint32 limit = 4;
}

message ListOperationsResponse {
  repeated Operation items = 1;
}

message Operation {
  string operation_id = 1;
  string kind = 2;
  string status = 3;
  string node_key = 4;
  string profile_name = 5;
  string started_at = 6;
  string updated_at = 7;
  string finished_at = 8;
  string progress_message = 9;
  DriverError error = 10;
}

message OperationEvent {
  Operation operation = 1;
  string log_line = 2;
}

message DriverError {
  string code = 1;
  string summary = 2;
  string detail = 3;
  bool retryable = 4;
}
```

## Mapping from Current Python Modules

Suggested migration targets:

- `server_runtime.py`
  - direct execution layer
  - replace with driver client calls
- `server_bootstrap.py`
  - move node bootstrap orchestration into Rust
- `awg.py`
  - replace create/delete/list transfer commands with RPCs
- `xray.py`
  - replace add/list/telemetry setup calls with RPCs
- `traffic_usage.py`
  - driver collects remote counters
  - Python keeps monthly aggregation/read model unless moved later
- `provisioning_state.py`
  - keep as local read-model/state summary layer
  - feed it from driver results instead of direct SSH inspection

## Recommended Migration Sequence

### Phase 1: Stabilize the Boundary

1. Add a Python `NodeDriverClient` interface.
2. Refactor service modules to depend on that interface.
3. Keep a temporary implementation backed by existing Python SSH logic.

This isolates the rest of the bot from the transport layer before Rust lands.

### Phase 2: Define Proto and Driver Skeleton

1. Freeze the first protobuf contract.
2. Implement Rust driver skeleton with health and operation model.
3. Add gRPC client implementation in Python.

### Phase 3: Move Read-Only Operations First

Move these first because they are safer:

- diagnostics;
- runtime status;
- list remote profiles;
- telemetry reads;
- traffic snapshots.

### Phase 4: Move Mutating Operations

Then move:

- bootstrap;
- sync env;
- add/delete profile on node;
- cleanup;
- reconcile operations.

### Phase 5: Switch Ownership

1. Disable direct node mutation paths in Python.
2. Route all node changes through the driver.
3. Keep Python as orchestration/UI only.

### Phase 6: Migrate Telegram Runtime

After node logic no longer depends on sync SSH-heavy handlers:

1. migrate bot runtime from PTB v13 style to PTB v22;
2. use async gRPC from handlers;
3. make long-running admin flows operation-based.

This order reduces duplicate rewrites.

## PTB v22 Impact

The Rust driver makes the Telegram migration easier.

Without the driver:

- async handlers would still call a lot of blocking SSH and subprocess code.

With the driver:

- handlers mostly validate, call async gRPC, and render responses;
- long operations become operation polling/streaming;
- the bot becomes a much better fit for PTB v22 async architecture.

## Non-Goals

The Rust driver should not initially absorb:

- Telegram-specific formatting;
- localization;
- keyboard construction;
- admin wizard logic;
- full business database ownership.

Those belong to the Python control plane.

## Open Questions

These need explicit decisions before implementation:

1. Will the driver initiate outbound connections to nodes, or will nodes dial back to the driver?
2. Is SSH only a bootstrap transport, or a permanent fallback transport?
3. Will telemetry be pushed from nodes, pulled by the driver, or both?
4. Does the driver need its own durable database, or is in-memory plus Python persistence enough for phase one?
5. Should operation logs be persisted in Rust, Python, or both?
6. Will reconcile semantics remain profile-oriented, node-oriented, or both?
7. Which fields are authoritative in Python vs discovered from nodes?

## Immediate Next Step

The next concrete artifact should be:

1. a minimal `proto/driver/v1/*.proto` set;
2. a Python `NodeDriverClient` interface;
3. a temporary in-process implementation backed by existing Python services.

That gives a clean seam for introducing the Rust driver without rewriting the whole bot in one step.

## Current Scaffold Status

The repository now contains:

1. `proto/driver/v1/*.proto`
2. `app/services/node_driver_client.py`
3. `app/services/node_driver_inprocess.py`
4. `app/services/node_driver_grpc.py`
5. backend selection in `app/services/node_driver.py`
6. `scripts/gen_driver_proto.sh` for Python stub generation

At the moment:

- `inprocess` is the working backend;
- `grpc` is a scaffold backend selected through config;
- generated Python gRPC bindings are still expected to be produced from the proto files before the gRPC backend can be wired end-to-end.
