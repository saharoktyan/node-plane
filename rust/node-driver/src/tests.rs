use super::*;
use tokio_stream::StreamExt;

#[test]
fn agent_public_xray_response_does_not_require_backend_owned_host() {
    let value = serde_json::json!({
        "xray_sni": "www.cloudflare.com", "xray_pbk": "a".repeat(43),
        "xray_sid": "0123456789abcdef", "xray_short_id": "0123456789abcdef",
        "xray_flow": "xtls-rprx-vision", "xray_fp": "chrome",
        "xray_tcp_port": 443, "xray_xhttp_port": 8443,
        "xray_xhttp_path_prefix": "/assets"
    });
    let public = XrayPublicMetadata::parse_public(&value.to_string()).unwrap();
    assert_eq!(public.xray_pbk, "a".repeat(43));
    assert_eq!(public.xray_tcp_port, 443);
    let mut invalid = value.clone();
    invalid["xray_pbk"] = serde_json::json!("(Public Key): broken");
    assert!(XrayPublicMetadata::parse_public(&invalid.to_string()).is_err());
    invalid.as_object_mut().unwrap().remove("xray_sid");
    assert!(XrayPublicMetadata::parse_public(&invalid.to_string()).is_err());
}

fn context() -> DriverContext {
    DriverContext {
        state: DriverState::default(),

        app_semver: "test".into(),
        app_commit: "test".into(),
        agent_targets: HashMap::new(),
    }
}

#[test]
fn runtime_assets_use_active_app_root_when_configured() {
    let app_root = std::path::PathBuf::from("/opt/node-plane/current");
    let manifest_dir = std::path::Path::new("/build/rust/node-driver");

    assert_eq!(
        runtime_assets_dir_from(Some(app_root), manifest_dir),
        std::path::PathBuf::from("/opt/node-plane/current/runtime_assets")
    );
    assert_eq!(
        runtime_assets_dir_from(None, manifest_dir),
        std::path::PathBuf::from("/build/rust/node-driver/../../runtime_assets")
    );
}

#[test]
fn local_agent_config_recovers_missing_target_without_routing_remote_agents_locally() {
    let local = "node_key = \"msk1\"\nlisten_addr = \"127.0.0.1:50061\"\n";
    assert_eq!(
        local_agent_target_from_config_content(local),
        Some(("msk1".into(), "127.0.0.1:50061".into()))
    );
    let remote = "node_key = \"lv1\"\nlisten_addr = \"0.0.0.0:50061\"\n";
    assert_eq!(local_agent_target_from_config_content(remote), None);
}

#[test]
fn local_target_uses_toml_syntax_and_rejects_invalid_addresses() {
    assert_eq!(
        local_agent_target_from_config_content(
            "node_key = 'msk1' # local node\nlisten_addr = '[::1]:50061' # listener\n[extra]\nvalue = 1\n"
        ),
        Some(("msk1".into(), "[::1]:50061".into()))
    );
    for value in [
        "node_key = 'x'\nlisten_addr = '127.0.0.1:abc'",
        "node_key = ''\nlisten_addr = '127.0.0.1:50061'",
        "node_key = 'x'\nlisten_addr = '127.0.0.1:0'",
        "node_key = 'x'\nlisten_addr = '127.0.0.1:50061'\nnode_key = 'y'",
        "node_key = 'x'\nlisten_addr = '127.0.0.1:50061'\nbroken = [",
    ] {
        assert_eq!(local_agent_target_from_config_content(value), None);
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

#[tokio::test]
async fn backend_node_inspection_does_not_fall_back_to_legacy_server_rows() {
    let api = NodeApi { ctx: context() };
    let error = api
        .inspect_backend_node(Request::new(InspectBackendNodeRequest {
            node_key: "test-node".into(),
        }))
        .await
        .unwrap_err();
    assert_eq!(error.code(), tonic::Code::FailedPrecondition);
    assert_eq!(error.message(), "no node-agent target configured");
}

#[tokio::test]
async fn diagnostics_without_agent_do_not_read_legacy_database() {
    let api = NodeApi { ctx: context() };
    let error = api
        .get_node_diagnostics(Request::new(GetNodeDiagnosticsRequest {
            node_key: "test-node".into(),
        }))
        .await
        .unwrap_err();
    assert_eq!(error.code(), tonic::Code::FailedPrecondition);
    assert_eq!(error.message(), "no node-agent target configured");
}

#[tokio::test]
async fn backend_settings_apply_requires_identity_and_never_uses_legacy_server_rows() {
    let ctx = context();
    let api = RuntimeApi { ctx: ctx.clone() };
    let request = ApplyBackendNodeSettingsRequest {
        node_key: "test-node".into(),
        desired_revision: 1,
        protocols_json: "[\"awg\"]".into(),
        settings_json: "{}".into(),
    };
    let missing = api
        .apply_backend_node_settings(Request::new(request.clone()))
        .await
        .unwrap_err();
    assert_eq!(missing.code(), tonic::Code::InvalidArgument);
    let mut keyed = Request::new(request);
    keyed.metadata_mut().insert(
        "x-node-plane-command-id",
        "stable-node-task".parse().unwrap(),
    );
    assert_missing_agent(&ctx, api.apply_backend_node_settings(keyed).await.unwrap());
}

#[test]
fn typed_cleanup_failure_is_available_to_backend() {
    let state = DriverState::default();
    let running = state
        .begin_operation("full_cleanup_node", "offline", "")
        .unwrap();
    let response = running
        .fail_with_error("agent_unreachable", "connection refused")
        .unwrap();
    let operation = state.get_operation(&response.operation_id).unwrap();
    assert_eq!(operation.status, "FAILED");
    assert_eq!(operation.error.unwrap().code, "agent_unreachable");
}

// Missing transport must never create a phantom queued operation, including
// destructive RPCs. No database, node, or shell execution is needed here.
#[tokio::test]
async fn watch_returns_terminal_result_and_closes() {
    let ctx = context();
    let started = ctx
        .state
        .begin_operation("ensure_profile_on_node", "node", "alice")
        .unwrap()
        .finish_with_result("SUCCEEDED", "done", "{\"awg\":{}}")
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
    let payload = "{\"awg\":{\"config\":\"test-config\"}}";
    let started = state
        .begin_operation("ensure_profile_on_node", "node", "alice")
        .unwrap()
        .finish_with_result("SUCCEEDED", "done", payload)
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
    let recovered = reopened.get_operation(&started.operation_id).unwrap();
    assert_eq!(recovered.result_json, payload);
    assert_eq!(recovered.profile_name, "alice");
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
    assert!(state.begin_operation("sync_xray", "node", "").is_err());
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
        .begin_operation("probe_node", "node", "")
        .unwrap()
        .finish("SUCCEEDED", "ok")
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

fn keyed_request<T>(body: T, command_id: &str) -> Request<T> {
    let mut request = Request::new(body);
    request
        .metadata_mut()
        .insert("x-node-plane-command-id", command_id.parse().unwrap());
    request
}

#[tokio::test]
async fn profile_intents_without_agent_fail_for_both_protocols_and_actions() {
    let ctx = context();
    let api = ProvisioningApi { ctx: ctx.clone() };
    for protocol in ["awg", "xray"] {
        for action in ["ensure", "delete"] {
            let body = driver::v1::ApplyProfileIntentRequest {
                node_key: "test-node".into(),
                runtime_name: "p_test".into(),
                protocol_kind: protocol.into(),
                action: action.into(),
                desired_revision: 1,
                xray: if protocol == "xray" && action == "ensure" {
                    Some(driver::v1::XraySpec {
                        profile_name: "p_test".into(),
                        uuid: "01234567-89ab-cdef-0123-456789abcdef".into(),
                        short_id: "0123456789abcdef".into(),
                    })
                } else {
                    None
                },
            };
            assert_missing_agent(
                &ctx,
                api.apply_profile_intent(keyed_request(body, &format!("{protocol}-{action}")))
                    .await
                    .unwrap(),
            );
        }
    }
    assert_eq!(ctx.state.list_operations("", "", "", 10).len(), 4);
}

#[tokio::test]
async fn backend_mutations_reject_execution_when_journal_cannot_be_written() {
    let directory =
        std::env::temp_dir().join(format!("node-plane-journal-{}", uuid::Uuid::new_v4()));
    let parent = directory.join("data");
    let mut ctx = context();
    ctx.state = DriverState::open(parent.join("operations.bin")).unwrap();
    ctx.agent_targets
        .insert("node".into(), "127.0.0.1:1".into());
    std::fs::rename(&parent, directory.join("moved")).unwrap();
    std::fs::write(&parent, b"").unwrap();
    let runtime = RuntimeApi { ctx: ctx.clone() };
    let provisioning = ProvisioningApi { ctx: ctx.clone() };
    let settings = runtime
        .apply_backend_node_settings(keyed_request(
            ApplyBackendNodeSettingsRequest {
                node_key: "node".into(),
                desired_revision: 1,
                protocols_json: "[\"awg\"]".into(),
                settings_json: "{}".into(),
            },
            "settings",
        ))
        .await
        .unwrap_err();
    let profile = provisioning
        .apply_profile_intent(keyed_request(
            driver::v1::ApplyProfileIntentRequest {
                node_key: "node".into(),
                runtime_name: "p_test".into(),
                protocol_kind: "awg".into(),
                action: "ensure".into(),
                desired_revision: 1,
                xray: None,
            },
            "profile",
        ))
        .await
        .unwrap_err();
    for error in [settings, profile] {
        assert_eq!(error.code(), tonic::Code::Internal);
        assert!(error.message().starts_with("failed to persist operation:"));
    }
    assert!(ctx.state.list_operations("", "", "", 10).is_empty());
    drop((runtime, provisioning, ctx));
    std::fs::remove_dir_all(directory).unwrap();
}
