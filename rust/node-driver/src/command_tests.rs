use super::*;
use prost::Message;

fn explicit_intent() -> driver::v1::ApplyProfileIntentRequest {
    driver::v1::ApplyProfileIntentRequest {
        node_key: "node".into(),
        runtime_name: "p_test".into(),
        protocol_kind: "awg".into(),
        action: "ensure".into(),
        desired_revision: 1,
        xray: None,
    }
}

#[test]
fn command_lookup_is_read_only_and_recovers_saved_result() {
    let state = DriverState::default();
    assert!(state.get_operation_by_command("unknown").is_none());
    assert!(state.get_operation_by_command("").is_none());
    let request = install_request("lookup-command");
    let CommandStart::New(execution) = start(&state, &request) else {
        panic!()
    };
    assert_eq!(
        state
            .get_operation_by_command("lookup-command")
            .unwrap()
            .status,
        "RUNNING"
    );
    execution
        .finish_with_result("SUCCEEDED", "done", "private result")
        .unwrap();
    let found = state.get_operation_by_command("lookup-command").unwrap();
    assert_eq!(found.status, "SUCCEEDED");
    assert_eq!(found.result_json, "private result");
    assert_eq!(state.list_operations("", "", "", 10).len(), 1);
}

#[test]
fn explicit_intent_validates_identity_and_protocol_before_dispatch() {
    let mut req = explicit_intent();
    assert!(validate_profile_intent(&req).is_ok());
    req.runtime_name = "unsafe;command".into();
    assert!(validate_profile_intent(&req).is_err());
    req.runtime_name = "p_test".into();
    req.protocol_kind = "xray".into();
    assert!(validate_profile_intent(&req).is_err());
    req.xray = Some(driver::v1::XraySpec {
        profile_name: "p_test".into(),
        uuid: "12345678-1234-1234-1234-123456789abc".into(),
        short_id: "123456789abcdef0".into(),
    });
    assert!(validate_profile_intent(&req).is_ok());
    req.action = "delete".into();
    assert!(validate_profile_intent(&req).is_err());
    req.xray = None;
    assert!(validate_profile_intent(&req).is_ok());
}

#[test]
fn explicit_intent_revision_cannot_reuse_another_commands_identity() {
    let state = DriverState::default();
    let req = keyed(explicit_intent(), "backend-task-1");
    let identity = CommandIdentity::from_request(&req).unwrap();
    let CommandStart::New(execution) = state
        .begin_command("apply_profile_intent", "node", "p_test", identity)
        .unwrap()
    else {
        panic!()
    };
    execution.finish("SUCCEEDED", "applied").unwrap();
    let mut changed = explicit_intent();
    changed.desired_revision = 2;
    let changed = keyed(changed, "backend-task-1");
    assert!(
        state
            .begin_command(
                "apply_profile_intent",
                "node",
                "p_test",
                CommandIdentity::from_request(&changed).unwrap()
            )
            .is_err()
    );
}

fn keyed<T>(message: T, key: &str) -> Request<T> {
    let mut request = Request::new(message);
    request
        .metadata_mut()
        .insert("x-node-plane-command-id", key.parse().unwrap());
    request
}

fn install_request(key: &str) -> Request<ApplyBackendNodeSettingsRequest> {
    keyed(
        ApplyBackendNodeSettingsRequest {
            node_key: "node".into(),
            desired_revision: 1,
            protocols_json: "[\"awg\"]".into(),
            settings_json: "{}".into(),
        },
        key,
    )
}

fn start(state: &DriverState, request: &Request<ApplyBackendNodeSettingsRequest>) -> CommandStart {
    state
        .begin_command(
            "install_docker",
            "node",
            "",
            CommandIdentity::from_request(request).unwrap(),
        )
        .unwrap()
}

fn existing_id(start: CommandStart) -> String {
    match start {
        CommandStart::Existing(response) => response.operation_id,
        CommandStart::New(_) => panic!("duplicate command was accepted for execution"),
    }
}

#[test]
fn duplicate_returns_same_id_during_execution_and_after_completion() {
    let state = DriverState::default();
    let request = install_request("request-1");
    let CommandStart::New(execution) = start(&state, &request) else {
        panic!("new command was deduplicated")
    };
    let id = existing_id(start(&state, &request));
    assert_eq!(state.get_operation(&id).unwrap().status, "RUNNING");
    assert_eq!(
        execution.finish("SUCCEEDED", "done").unwrap().operation_id,
        id
    );
    assert_eq!(existing_id(start(&state, &request)), id);
    assert_eq!(state.list_operations("", "", "", 10).len(), 1);
}

#[test]
fn different_payload_or_method_cannot_reuse_command_key() {
    let state = DriverState::default();
    let original = keyed(
        ApplyBackendNodeSettingsRequest {
            node_key: "node".into(),
            desired_revision: 1,
            ..Default::default()
        },
        "settings-1",
    );
    let CommandStart::New(execution) = state
        .begin_command(
            "apply_backend_node_settings",
            "node",
            "",
            CommandIdentity::from_request(&original).unwrap(),
        )
        .unwrap()
    else {
        panic!()
    };
    execution.finish("SUCCEEDED", "done").unwrap();
    let changed = keyed(
        ApplyBackendNodeSettingsRequest {
            node_key: "node".into(),
            desired_revision: 2,
            ..Default::default()
        },
        "settings-1",
    );
    for (kind, request) in [
        ("apply_backend_node_settings", &changed),
        ("apply_profile_intent", &original),
    ] {
        let result = state.begin_command(
            kind,
            "node",
            "",
            CommandIdentity::from_request(request).unwrap(),
        );
        assert!(matches!(result, Err(err) if err.code() == tonic::Code::AlreadyExists));
    }
    assert_eq!(state.list_operations("", "", "", 10).len(), 1);
}

#[test]
fn parallel_duplicates_have_only_one_executor() {
    let state = DriverState::default();
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(8));
    let threads: Vec<_> = (0..8)
        .map(|_| {
            let state = state.clone();
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                barrier.wait();
                start(&state, &install_request("parallel-1"))
            })
        })
        .collect();
    let results: Vec<_> = threads
        .into_iter()
        .map(|thread| thread.join().unwrap())
        .collect();
    assert_eq!(
        results
            .iter()
            .filter(|result| matches!(result, CommandStart::New(_)))
            .count(),
        1
    );
    assert_eq!(state.list_operations("", "", "RUNNING", 10).len(), 1);
    let id = state.list_operations("", "", "", 10)[0]
        .operation_id
        .clone();
    for result in results {
        match result {
            CommandStart::New(execution) => assert_eq!(
                execution.finish("SUCCEEDED", "done").unwrap().operation_id,
                id
            ),
            CommandStart::Existing(response) => assert_eq!(response.operation_id, id),
        }
    }
}

#[test]
fn restart_preserves_deduplication_for_completed_and_interrupted_work() {
    let directory =
        std::env::temp_dir().join(format!("node-plane-command-{}", uuid::Uuid::new_v4()));
    let path = directory.join("operations.bin");
    let crash_snapshot = directory.join("crashed.bin");
    let state = DriverState::open(path.clone()).unwrap();
    let request = install_request("restart-1");
    let CommandStart::New(execution) = start(&state, &request) else {
        panic!()
    };
    // Capture the durable state a killed process would leave, before guard Drop.
    fs::copy(&path, &crash_snapshot).unwrap();
    let id = execution.finish("SUCCEEDED", "done").unwrap().operation_id;
    drop(state);
    let completed = DriverState::open(path).unwrap();
    assert_eq!(existing_id(start(&completed, &request)), id);
    assert_eq!(completed.get_operation(&id).unwrap().status, "SUCCEEDED");
    let interrupted = DriverState::open(crash_snapshot).unwrap();
    assert_eq!(existing_id(start(&interrupted, &request)), id);
    let error = interrupted.get_operation(&id).unwrap().error.unwrap();
    assert_eq!(error.code, "execution_interrupted");
    assert!(!error.retryable);
    drop((completed, interrupted));
    fs::remove_dir_all(directory).unwrap();
}

#[test]
fn legacy_history_is_readable_and_upgraded_without_losing_results() {
    let directory =
        std::env::temp_dir().join(format!("node-plane-legacy-{}", uuid::Uuid::new_v4()));
    fs::create_dir_all(&directory).unwrap();
    let path = directory.join("operations.bin");
    let legacy = Operation {
        operation_id: "legacy".into(),
        status: "SUCCEEDED".into(),
        result_json: "{\"ok\":true}".into(),
        ..Default::default()
    };
    let mut bytes = b"NODE-PLANE-OPERATIONS-v1\n".to_vec();
    legacy.encode_length_delimited(&mut bytes).unwrap();
    fs::write(&path, bytes).unwrap();
    let state = DriverState::open(path.clone()).unwrap();
    assert_eq!(state.get_operation("legacy").unwrap(), legacy);
    let CommandStart::New(execution) = start(&state, &install_request("new-1")) else {
        panic!()
    };
    execution.finish("SUCCEEDED", "done").unwrap();
    drop(state);
    assert!(
        fs::read(&path)
            .unwrap()
            .starts_with(b"NODE-PLANE-OPERATIONS-v2\n")
    );
    let upgraded = DriverState::open(path).unwrap();
    assert_eq!(upgraded.get_operation("legacy").unwrap(), legacy);
    assert_eq!(upgraded.list_operations("", "", "", 10).len(), 2);
    drop(upgraded);
    fs::remove_dir_all(directory).unwrap();
}

#[test]
fn invalid_or_repeated_metadata_is_rejected() {
    for key in ["", "contains space", &"a".repeat(129)] {
        assert!(CommandIdentity::from_request(&install_request(key)).is_err());
    }
    let mut request = install_request("first");
    request
        .metadata_mut()
        .append("x-node-plane-command-id", "second".parse().unwrap());
    assert!(CommandIdentity::from_request(&request).is_err());
    assert!(
        CommandIdentity::from_request(&Request::new(ApplyBackendNodeSettingsRequest::default()))
            .unwrap()
            .is_none()
    );
}

#[test]
fn anonymous_requests_remain_independent() {
    let state = DriverState::default();
    let request = Request::new(ApplyBackendNodeSettingsRequest {
        node_key: "node".into(),
        ..Default::default()
    });
    let CommandStart::New(first) = start(&state, &request) else {
        panic!()
    };
    let CommandStart::New(second) = start(&state, &request) else {
        panic!()
    };
    assert_ne!(
        first.finish("SUCCEEDED", "done").unwrap().operation_id,
        second.finish("SUCCEEDED", "done").unwrap().operation_id
    );
}

#[tokio::test]
async fn failed_command_is_not_reexecuted_after_configuration_changes() {
    let mut api = RuntimeApi {
        ctx: DriverContext {
            state: DriverState::default(),

            app_semver: "test".into(),
            app_commit: "test".into(),
            agent_targets: HashMap::new(),
        },
    };
    let first = api
        .apply_backend_node_settings(install_request("failed-1"))
        .await
        .unwrap()
        .into_inner();
    api.ctx
        .agent_targets
        .insert("node".into(), "127.0.0.1:1".into());
    let repeated = api
        .apply_backend_node_settings(install_request("failed-1"))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(first.operation_id, repeated.operation_id);
    assert_eq!(
        api.ctx
            .state
            .get_operation(&first.operation_id)
            .unwrap()
            .error
            .unwrap()
            .code,
        "agent_not_configured"
    );
    assert_eq!(api.ctx.state.list_operations("", "", "", 10).len(), 1);
}

#[tokio::test]
async fn every_rpc_returns_existing_command_without_execution() {
    let ctx = DriverContext {
        state: DriverState::default(),

        app_semver: "test".into(),
        app_commit: "test".into(),
        agent_targets: HashMap::new(),
    };
    let runtime = RuntimeApi { ctx: ctx.clone() };
    let provisioning = ProvisioningApi { ctx: ctx.clone() };
    macro_rules! check {
        ($api:ident, $method:ident, $node:expr, $profile:expr, $body:expr) => {{
            let request = keyed($body, stringify!($method));
            let CommandStart::New(execution) = ctx
                .state
                .begin_command(
                    stringify!($method),
                    $node,
                    $profile,
                    CommandIdentity::from_request(&request).unwrap(),
                )
                .unwrap()
            else {
                panic!()
            };
            let response = $api.$method(request).await.unwrap().into_inner();
            assert_eq!(
                ctx.state
                    .get_operation(&response.operation_id)
                    .unwrap()
                    .status,
                "RUNNING",
                stringify!($method)
            );
            assert_eq!(
                execution.finish("SUCCEEDED", "done").unwrap().operation_id,
                response.operation_id
            );
        }};
    }
    check!(
        runtime,
        apply_backend_node_settings,
        "node",
        "",
        ApplyBackendNodeSettingsRequest {
            node_key: "node".into(),
            desired_revision: 1,
            protocols_json: "[\"awg\"]".into(),
            settings_json: "{}".into(),
        }
    );
    check!(
        provisioning,
        apply_profile_intent,
        "node",
        "p_test",
        explicit_intent()
    );
    assert_eq!(ctx.state.list_operations("", "", "", 100).len(), 2);
}
