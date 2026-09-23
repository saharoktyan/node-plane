use super::*;
use tokio_stream::StreamExt;

fn context() -> DriverContext {
    DriverContext {
        state: DriverState::default(),
        postgres_dsn: None,
        app_semver: "test".into(),
        app_commit: "test".into(),
        agent_targets: HashMap::new(),
    }
}

fn assert_missing_agent(ctx: &DriverContext, response: Response<StartOperationResponse>) {
    let op = ctx
        .state
        .get_operation(&response.into_inner().operation_id)
        .unwrap();
    assert_eq!(op.status, "FAILED");
    assert_eq!(op.node_key, "test-node");
    assert!(!op.finished_at.is_empty());
    let error = op.error.unwrap();
    assert_eq!(error.code, "agent_not_configured");
    assert!(!error.retryable);
    assert!(op.result_json.is_empty());
}

// Missing transport must never create a phantom queued operation, including
// destructive RPCs. No database, node, or shell execution is needed here.
#[tokio::test]
async fn node_actions_without_agent_fail_explicitly() {
    let ctx = context();
    let api = NodeApi { ctx: ctx.clone() };
    macro_rules! check {
        ($method:ident, $request:ident) => {
            assert_missing_agent(
                &ctx,
                api.$method(Request::new($request {
                    node_key: "test-node".into(),
                    ..Default::default()
                }))
                .await
                .unwrap(),
            );
        };
    }
    check!(sync_node_env, SyncNodeEnvRequest);
    check!(probe_node, ProbeNodeRequest);
    check!(check_ports, CheckPortsRequest);
    check!(open_ports, OpenPortsRequest);
    check!(install_docker, InstallDockerRequest);
}

#[tokio::test]
async fn runtime_actions_without_agent_fail_explicitly() {
    let ctx = context();
    let api = RuntimeApi { ctx: ctx.clone() };
    macro_rules! check {
        ($method:ident, $request:ident) => {
            assert_missing_agent(
                &ctx,
                api.$method(Request::new($request {
                    node_key: "test-node".into(),
                    ..Default::default()
                }))
                .await
                .unwrap(),
            );
        };
    }
    check!(bootstrap_node, BootstrapNodeRequest);
    check!(reinstall_node, ReinstallNodeRequest);
    check!(delete_runtime, DeleteRuntimeRequest);
    check!(full_cleanup_node, FullCleanupNodeRequest);
    check!(sync_runtime, SyncRuntimeRequest);
    check!(sync_xray, SyncXrayRequest);
}

#[tokio::test]
async fn profile_actions_without_agent_fail_explicitly() {
    let ctx = context();
    let api = ProvisioningApi { ctx: ctx.clone() };
    let response = api
        .ensure_profile_on_node(Request::new(driver::v1::EnsureProfileOnNodeRequest {
            node_key: "test-node".into(),
            profile: Some(ProfileSpec {
                profile_name: "alice".into(),
                ..Default::default()
            }),
        }))
        .await
        .unwrap();
    let op = ctx
        .state
        .get_operation(&response.get_ref().operation_id)
        .unwrap();
    assert_eq!(op.profile_name, "alice");
    assert_missing_agent(&ctx, response);
    assert_missing_agent(
        &ctx,
        api.delete_profile_from_node(Request::new(DeleteProfileFromNodeRequest {
            node_key: "test-node".into(),
            profile_name: "alice".into(),
            ..Default::default()
        }))
        .await
        .unwrap(),
    );
}

#[tokio::test]
async fn unsupported_telemetry_does_not_enqueue_work() {
    let ctx = context();
    let api = TelemetryApi { ctx: ctx.clone() };
    let err = api
        .collect_traffic_snapshot(Request::new(Default::default()))
        .await
        .unwrap_err();
    assert_eq!(err.code(), tonic::Code::Unimplemented);
    assert!(ctx.state.list_operations("", "", "", 20).is_empty());
    let api = NodeApi { ctx };
    let err = api
        .watch_node_health(Request::new(Default::default()))
        .await
        .unwrap_err();
    assert_eq!(err.code(), tonic::Code::Unimplemented);
}

#[tokio::test]
async fn watch_returns_terminal_result_and_closes() {
    let ctx = context();
    let started = ctx
        .state
        .finish_operation_with_result(
            "ensure_profile_on_node",
            "node",
            "alice",
            "SUCCEEDED",
            "done",
            "{\"awg\":{}}",
        )
        .unwrap();
    let api = OperationApi { ctx };
    let mut stream = api
        .watch_operation(Request::new(WatchOperationRequest {
            operation_id: started.operation_id.clone(),
        }))
        .await
        .unwrap()
        .into_inner();
    let event = stream.next().await.unwrap().unwrap();
    let op = event.operation.unwrap();
    assert_eq!(op.operation_id, started.operation_id);
    assert_eq!(op.status, "SUCCEEDED");
    assert_eq!(op.result_json, "{\"awg\":{}}");
    assert!(stream.next().await.is_none());
}

#[tokio::test]
async fn unknown_operation_is_not_an_empty_successful_stream() {
    let api = OperationApi { ctx: context() };
    let err = api
        .watch_operation(Request::new(WatchOperationRequest {
            operation_id: "missing".into(),
        }))
        .await
        .unwrap_err();
    assert_eq!(err.code(), tonic::Code::NotFound);
}

#[test]
fn recent_operations_are_sorted_by_time_and_filtered_before_limit() {
    let state = DriverState::default();
    for (id, timestamp, node) in [
        ("z", "2026-09-24T00:00:00Z", "node"),
        ("a", "2026-09-24T01:00:00Z", "node"),
        ("b", "2026-09-24T02:00:00Z", "other"),
    ] {
        state
            .put_operation(Operation {
                operation_id: id.into(),
                updated_at: timestamp.into(),
                node_key: node.into(),
                profile_name: "alice".into(),
                status: "SUCCEEDED".into(),
                ..Default::default()
            })
            .unwrap();
    }
    let items = state.list_operations("node", "alice", "SUCCEEDED", 1);
    assert_eq!(items.len(), 1);
    assert_eq!(items[0].operation_id, "a");
    assert!(
        state
            .list_operations("node", "alice", "FAILED", 20)
            .is_empty()
    );
}

#[test]
fn operations_survive_reopening_storage() {
    let directory =
        std::env::temp_dir().join(format!("node-plane-operations-{}", uuid::Uuid::new_v4()));
    let path = directory.join("operations.bin");
    let state = DriverState::open(path.clone()).unwrap();
    let started = state
        .finish_operation("sync_xray", "node", "", "SUCCEEDED", "done")
        .unwrap();
    drop(state);
    let reopened = DriverState::open(path.clone()).unwrap();
    assert_eq!(
        reopened
            .get_operation(&started.operation_id)
            .unwrap()
            .progress_message,
        "done"
    );
    assert_eq!(reopened.list_operations("node", "", "", 10).len(), 1);
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(
        std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    std::fs::remove_dir_all(directory).unwrap();
}

#[test]
fn corrupt_operations_file_prevents_startup() {
    let directory =
        std::env::temp_dir().join(format!("node-plane-operations-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&directory).unwrap();
    let path = directory.join("operations.bin");
    std::fs::write(&path, b"invalid").unwrap();
    assert!(DriverState::open(path).is_err());
    std::fs::remove_dir_all(directory).unwrap();
}

#[test]
fn failed_persistence_does_not_publish_operation() {
    let directory =
        std::env::temp_dir().join(format!("node-plane-operations-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&directory).unwrap();
    let parent_file = directory.join("not-a-directory");
    let state = DriverState::open(parent_file.join("operations.bin")).unwrap();
    std::fs::rename(&parent_file, directory.join("moved")).unwrap();
    std::fs::write(&parent_file, b"").unwrap();
    assert!(
        state
            .finish_operation("sync_xray", "node", "", "SUCCEEDED", "done")
            .is_err()
    );
    assert!(state.list_operations("", "", "", 10).is_empty());
    std::fs::remove_dir_all(directory).unwrap();
}

#[test]
fn running_operation_keeps_its_identity_and_start_time() {
    let state = DriverState::default();
    let running = state.begin_operation("install_docker", "node", "").unwrap();
    let before = state
        .list_operations("node", "", "RUNNING", 10)
        .pop()
        .unwrap();
    assert!(before.finished_at.is_empty());
    let response = running.finish("SUCCEEDED", "docker installed").unwrap();
    assert_eq!(response.operation_id, before.operation_id);
    let after = state.get_operation(&response.operation_id).unwrap();
    assert_eq!(after.started_at, before.started_at);
    assert_eq!(after.status, "SUCCEEDED");
    assert!(!after.finished_at.is_empty());
    assert!(after.error.is_none());
    assert_eq!(state.list_operations("", "", "", 10).len(), 1);
}

#[test]
fn restart_marks_unfinished_work_unknown_and_preserves_terminal_results() {
    let directory =
        std::env::temp_dir().join(format!("node-plane-recovery-{}", uuid::Uuid::new_v4()));
    let path = directory.join("operations.bin");
    let state = DriverState::open(path.clone()).unwrap();
    // Model the last durable snapshot before a process crash, without invoking
    // the guard's graceful-drop path.
    for status in ["PENDING", "RUNNING"] {
        state
            .put_operation(Operation {
                operation_id: status.to_string(),
                kind: "install_docker".to_string(),
                node_key: "node".to_string(),
                status: status.to_string(),
                started_at: "2026-09-24T00:00:00Z".to_string(),
                ..Default::default()
            })
            .unwrap();
    }
    let completed = state
        .finish_operation("probe_node", "node", "", "SUCCEEDED", "ok")
        .unwrap();
    let terminal_before = state.get_operation(&completed.operation_id).unwrap();
    drop(state);
    let recovered = DriverState::open(path.clone()).unwrap();
    for id in ["PENDING", "RUNNING"] {
        let op = recovered.get_operation(id).unwrap();
        assert_eq!(op.status, "FAILED");
        assert_eq!(op.started_at, "2026-09-24T00:00:00Z");
        assert!(!op.finished_at.is_empty());
        let error = op.error.unwrap();
        assert_eq!(error.code, "execution_interrupted");
        assert!(!error.retryable);
    }
    assert_eq!(
        recovered.get_operation(&completed.operation_id).unwrap(),
        terminal_before
    );
    let first_recovery = recovered.get_operation("RUNNING").unwrap();
    drop(recovered);
    let reopened = DriverState::open(path).unwrap();
    assert_eq!(reopened.get_operation("RUNNING").unwrap(), first_recovery);
    drop(reopened);
    std::fs::remove_dir_all(directory).unwrap();
}

#[tokio::test]
async fn cancelling_execution_records_unknown_outcome() {
    let state = DriverState::default();
    let worker_state = state.clone();
    let (started_tx, started_rx) = tokio::sync::oneshot::channel();
    let worker = tokio::spawn(async move {
        let operation = worker_state
            .begin_operation("install_docker", "node", "")
            .unwrap();
        started_tx.send(()).unwrap();
        std::future::pending::<()>().await;
        operation.finish("SUCCEEDED", "unreachable").unwrap();
    });
    started_rx.await.unwrap();
    let before = state.list_operations("", "", "RUNNING", 10).pop().unwrap();
    worker.abort();
    assert!(worker.await.unwrap_err().is_cancelled());
    let after = state.get_operation(&before.operation_id).unwrap();
    assert_eq!(after.status, "FAILED");
    let error = after.error.unwrap();
    assert_eq!(error.code, "execution_interrupted");
    assert!(!error.retryable);
}

#[test]
fn history_allows_only_one_driver_until_every_clone_is_dropped() {
    let directory = std::env::temp_dir().join(format!("node-plane-lock-{}", uuid::Uuid::new_v4()));
    let path = directory.join("operations.bin");
    let state = DriverState::open(path.clone()).unwrap();
    let clone = state.clone();
    assert!(DriverState::open(path.clone()).is_err());
    drop(state);
    assert!(DriverState::open(path.clone()).is_err());
    drop(clone);
    drop(DriverState::open(path).unwrap());
    std::fs::remove_dir_all(directory).unwrap();
}

#[tokio::test]
async fn install_docker_rejects_execution_when_journal_cannot_be_written() {
    let directory =
        std::env::temp_dir().join(format!("node-plane-journal-{}", uuid::Uuid::new_v4()));
    let parent = directory.join("data");
    let mut ctx = context();
    ctx.state = DriverState::open(parent.join("operations.bin")).unwrap();
    ctx.agent_targets
        .insert("node".into(), "127.0.0.1:1".into());
    std::fs::rename(&parent, directory.join("moved")).unwrap();
    std::fs::write(&parent, b"").unwrap();
    let api = NodeApi { ctx };
    let error = api
        .install_docker(Request::new(InstallDockerRequest {
            node_key: "node".into(),
        }))
        .await
        .unwrap_err();
    assert_eq!(error.code(), tonic::Code::Internal);
    assert!(api.ctx.state.list_operations("", "", "", 10).is_empty());
    drop(api);
    std::fs::remove_dir_all(directory).unwrap();
}
