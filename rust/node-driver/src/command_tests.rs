use super::*;
use prost::Message;

fn keyed<T>(message: T, key: &str) -> Request<T> {
    let mut request = Request::new(message);
    request
        .metadata_mut()
        .insert("x-node-plane-command-id", key.parse().unwrap());
    request
}

fn install_request(key: &str) -> Request<InstallDockerRequest> {
    keyed(
        InstallDockerRequest {
            node_key: "node".into(),
        },
        key,
    )
}

fn start(state: &DriverState, request: &Request<InstallDockerRequest>) -> CommandStart {
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
        DeleteRuntimeRequest {
            node_key: "node".into(),
            preserve_config: true,
        },
        "delete-1",
    );
    let CommandStart::New(execution) = state
        .begin_command(
            "delete_runtime",
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
        DeleteRuntimeRequest {
            node_key: "node".into(),
            preserve_config: false,
        },
        "delete-1",
    );
    for (kind, request) in [("delete_runtime", &changed), ("bootstrap_node", &original)] {
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
        CommandIdentity::from_request(&Request::new(InstallDockerRequest::default()))
            .unwrap()
            .is_none()
    );
}

#[test]
fn anonymous_requests_remain_independent() {
    let state = DriverState::default();
    let request = Request::new(InstallDockerRequest {
        node_key: "node".into(),
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
    let mut api = NodeApi {
        ctx: DriverContext {
            state: DriverState::default(),
            postgres_dsn: None,
            app_semver: "test".into(),
            app_commit: "test".into(),
            agent_targets: HashMap::new(),
        },
    };
    let first = api
        .install_docker(install_request("failed-1"))
        .await
        .unwrap()
        .into_inner();
    api.ctx
        .agent_targets
        .insert("node".into(), "127.0.0.1:1".into());
    let repeated = api
        .install_docker(install_request("failed-1"))
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
        postgres_dsn: None,
        app_semver: "test".into(),
        app_commit: "test".into(),
        agent_targets: HashMap::new(),
    };
    let node = NodeApi { ctx: ctx.clone() };
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
    macro_rules! check_node {
        ($api:ident, $method:ident, $body:ident) => {
            check!(
                $api,
                $method,
                "node",
                "",
                $body {
                    node_key: "node".into(),
                    ..Default::default()
                }
            );
        };
    }
    check_node!(node, sync_node_env, SyncNodeEnvRequest);
    check_node!(node, probe_node, ProbeNodeRequest);
    check_node!(node, check_ports, CheckPortsRequest);
    check_node!(node, open_ports, OpenPortsRequest);
    check_node!(node, install_docker, InstallDockerRequest);
    check_node!(runtime, bootstrap_node, BootstrapNodeRequest);
    check_node!(runtime, reinstall_node, ReinstallNodeRequest);
    check_node!(runtime, delete_runtime, DeleteRuntimeRequest);
    check_node!(runtime, full_cleanup_node, FullCleanupNodeRequest);
    check_node!(runtime, regenerate_awg_entropy, RegenerateAwgEntropyRequest);
    check_node!(runtime, sync_runtime, SyncRuntimeRequest);
    check_node!(runtime, sync_xray, SyncXrayRequest);
    check_node!(provisioning, reconcile_node, ReconcileNodeRequest);
    check!(
        provisioning,
        reconcile_profile,
        "",
        "alice",
        ReconcileProfileRequest {
            profile_name: "alice".into()
        }
    );
    check!(
        provisioning,
        ensure_profile_on_node,
        "node",
        "alice",
        driver::v1::EnsureProfileOnNodeRequest {
            node_key: "node".into(),
            profile: Some(ProfileSpec {
                profile_name: "alice".into(),
                ..Default::default()
            }),
        }
    );
    check!(
        provisioning,
        delete_profile_from_node,
        "node",
        "alice",
        DeleteProfileFromNodeRequest {
            node_key: "node".into(),
            profile_name: "alice".into(),
            ..Default::default()
        }
    );
    assert_eq!(ctx.state.list_operations("", "", "", 100).len(), 16);
}
