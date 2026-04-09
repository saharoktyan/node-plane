use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use tonic::{Request, Response, Status, transport::Server};
use uuid::Uuid;

pub mod driver {
    pub mod v1 {
        tonic::include_proto!("nodeplane.driver.v1");
    }
}

use driver::v1::node_service_server::{NodeService, NodeServiceServer};
use driver::v1::operation_service_server::{OperationService, OperationServiceServer};
use driver::v1::provisioning_service_server::{ProvisioningService, ProvisioningServiceServer};
use driver::v1::runtime_service_server::{RuntimeService, RuntimeServiceServer};
use driver::v1::telemetry_service_server::{TelemetryService, TelemetryServiceServer};
use driver::v1::{
    BootstrapNodeRequest, CheckPortsRequest, DeleteProfileFromNodeRequest, DeleteRuntimeRequest,
    FullCleanupNodeRequest, GetNodeDiagnosticsRequest, GetNodeRequest, GetOperationRequest,
    GetProfileUsageRequest, GetProfileUsageResponse, GetRuntimeStatusRequest,
    GetRuntimeStatusResponse, InstallDockerRequest, ListNodesNeedingRuntimeSyncRequest,
    ListNodesNeedingRuntimeSyncResponse, ListNodesRequest, ListNodesResponse,
    ListOperationsRequest, ListOperationsResponse, ListRemoteProfilesRequest,
    ListRemoteProfilesResponse, Node, NodeCapabilities, NodeHealth, NodeHealthEvent,
    OpenPortsRequest, Operation, ProbeNodeRequest, ProfileUsage, ReconcileNodeRequest,
    ReconcileProfileRequest, ReinstallNodeRequest, RuntimeStatus, ServiceStatus,
    StartOperationResponse, SyncNodeEnvRequest, SyncRuntimeRequest, SyncXrayRequest,
    WatchNodeHealthRequest, WatchOperationRequest,
};

#[derive(Clone, Default)]
struct DriverState {
    operations: Arc<Mutex<HashMap<String, Operation>>>,
}

impl DriverState {
    fn start_operation(
        &self,
        kind: &str,
        node_key: &str,
        profile_name: &str,
        message: &str,
    ) -> StartOperationResponse {
        let operation_id = Uuid::new_v4().to_string();
        let op = Operation {
            operation_id: operation_id.clone(),
            kind: kind.to_string(),
            status: "PENDING".to_string(),
            node_key: node_key.to_string(),
            profile_name: profile_name.to_string(),
            started_at: String::new(),
            updated_at: String::new(),
            finished_at: String::new(),
            progress_message: message.to_string(),
            error: None,
        };
        self.operations
            .lock()
            .expect("operations lock poisoned")
            .insert(operation_id.clone(), op);
        StartOperationResponse { operation_id }
    }

    fn get_operation(&self, operation_id: &str) -> Option<Operation> {
        self.operations
            .lock()
            .expect("operations lock poisoned")
            .get(operation_id)
            .cloned()
    }

    fn list_operations(
        &self,
        node_key: &str,
        profile_name: &str,
        status: &str,
        limit: u32,
    ) -> Vec<Operation> {
        let mut items: Vec<Operation> = self
            .operations
            .lock()
            .expect("operations lock poisoned")
            .values()
            .filter(|op| node_key.is_empty() || op.node_key == node_key)
            .filter(|op| profile_name.is_empty() || op.profile_name == profile_name)
            .filter(|op| status.is_empty() || op.status == status)
            .cloned()
            .collect();
        items.sort_by(|a, b| a.operation_id.cmp(&b.operation_id));
        items.truncate(limit.max(1) as usize);
        items
    }
}

#[derive(Clone)]
struct NodeApi {
    state: DriverState,
}

#[derive(Clone)]
struct ProvisioningApi {
    state: DriverState,
}

#[derive(Clone)]
struct RuntimeApi {
    state: DriverState,
}

#[derive(Clone)]
struct TelemetryApi {
    state: DriverState,
}

#[derive(Clone)]
struct OperationApi {
    state: DriverState,
}

fn empty_node(node_key: &str) -> Node {
    Node {
        node_key: node_key.to_string(),
        transport: String::new(),
        version: String::new(),
        state: "unknown".to_string(),
        title: String::new(),
        flag: String::new(),
        region: String::new(),
        public_host: String::new(),
        capabilities: Some(NodeCapabilities {
            supports_awg: true,
            supports_xray: true,
            supports_telemetry: true,
            supports_bootstrap: true,
        }),
        health: Some(NodeHealth {
            connectivity: "unknown".to_string(),
            last_seen_at: String::new(),
            summary: "skeleton driver".to_string(),
        }),
    }
}

#[tonic::async_trait]
impl NodeService for NodeApi {
    async fn get_node(&self, request: Request<GetNodeRequest>) -> Result<Response<Node>, Status> {
        let req = request.into_inner();
        if req.node_key.trim().is_empty() {
            return Err(Status::invalid_argument("node_key is required"));
        }
        Ok(Response::new(empty_node(&req.node_key)))
    }

    async fn list_nodes(
        &self,
        _request: Request<ListNodesRequest>,
    ) -> Result<Response<ListNodesResponse>, Status> {
        Ok(Response::new(ListNodesResponse { items: Vec::new() }))
    }

    async fn get_node_diagnostics(
        &self,
        request: Request<GetNodeDiagnosticsRequest>,
    ) -> Result<Response<driver::v1::GetNodeDiagnosticsResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(driver::v1::GetNodeDiagnosticsResponse {
            node_key: req.node_key,
            summary: "diagnostics not implemented yet".to_string(),
            items: Vec::new(),
        }))
    }

    type WatchNodeHealthStream =
        tokio_stream::wrappers::ReceiverStream<Result<NodeHealthEvent, Status>>;

    async fn watch_node_health(
        &self,
        _request: Request<WatchNodeHealthRequest>,
    ) -> Result<Response<Self::WatchNodeHealthStream>, Status> {
        let (_tx, rx) = tokio::sync::mpsc::channel(1);
        Ok(Response::new(tokio_stream::wrappers::ReceiverStream::new(
            rx,
        )))
    }

    async fn sync_node_env(
        &self,
        request: Request<SyncNodeEnvRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(self.state.start_operation(
            "sync_node_env",
            &req.node_key,
            "",
            "sync_node_env queued by skeleton driver",
        )))
    }

    async fn probe_node(
        &self,
        request: Request<ProbeNodeRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(self.state.start_operation(
            "probe_node",
            &req.node_key,
            "",
            "probe_node queued by skeleton driver",
        )))
    }

    async fn check_ports(
        &self,
        request: Request<CheckPortsRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(self.state.start_operation(
            "check_ports",
            &req.node_key,
            "",
            "check_ports queued by skeleton driver",
        )))
    }

    async fn open_ports(
        &self,
        request: Request<OpenPortsRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(self.state.start_operation(
            "open_ports",
            &req.node_key,
            "",
            "open_ports queued by skeleton driver",
        )))
    }

    async fn install_docker(
        &self,
        request: Request<InstallDockerRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(self.state.start_operation(
            "install_docker",
            &req.node_key,
            "",
            "install_docker queued by skeleton driver",
        )))
    }
}

#[tonic::async_trait]
impl ProvisioningService for ProvisioningApi {
    async fn ensure_profile_on_node(
        &self,
        request: Request<driver::v1::EnsureProfileOnNodeRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        let profile_name = req.profile.map(|p| p.profile_name).unwrap_or_default();
        Ok(Response::new(self.state.start_operation(
            "ensure_profile_on_node",
            &req.node_key,
            &profile_name,
            "ensure_profile_on_node queued by skeleton driver",
        )))
    }

    async fn delete_profile_from_node(
        &self,
        request: Request<DeleteProfileFromNodeRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(self.state.start_operation(
            "delete_profile_from_node",
            &req.node_key,
            &req.profile_name,
            "delete_profile_from_node queued by skeleton driver",
        )))
    }

    async fn reconcile_node(
        &self,
        request: Request<ReconcileNodeRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(self.state.start_operation(
            "reconcile_node",
            &req.node_key,
            "",
            "reconcile_node queued by skeleton driver",
        )))
    }

    async fn reconcile_profile(
        &self,
        request: Request<ReconcileProfileRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(self.state.start_operation(
            "reconcile_profile",
            "",
            &req.profile_name,
            "reconcile_profile queued by skeleton driver",
        )))
    }

    async fn list_remote_profiles(
        &self,
        request: Request<ListRemoteProfilesRequest>,
    ) -> Result<Response<ListRemoteProfilesResponse>, Status> {
        let req = request.into_inner();
        let _ = req;
        Ok(Response::new(ListRemoteProfilesResponse {
            items: Vec::new(),
        }))
    }
}

#[tonic::async_trait]
impl RuntimeService for RuntimeApi {
    async fn bootstrap_node(
        &self,
        request: Request<BootstrapNodeRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(self.state.start_operation(
            "bootstrap_node",
            &req.node_key,
            "",
            "bootstrap_node queued by skeleton driver",
        )))
    }

    async fn reinstall_node(
        &self,
        request: Request<ReinstallNodeRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(self.state.start_operation(
            "reinstall_node",
            &req.node_key,
            "",
            "reinstall_node queued by skeleton driver",
        )))
    }

    async fn delete_runtime(
        &self,
        request: Request<DeleteRuntimeRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(self.state.start_operation(
            "delete_runtime",
            &req.node_key,
            "",
            "delete_runtime queued by skeleton driver",
        )))
    }

    async fn full_cleanup_node(
        &self,
        request: Request<FullCleanupNodeRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(self.state.start_operation(
            "full_cleanup_node",
            &req.node_key,
            "",
            "full_cleanup_node queued by skeleton driver",
        )))
    }

    async fn sync_runtime(
        &self,
        request: Request<SyncRuntimeRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(self.state.start_operation(
            "sync_runtime",
            &req.node_key,
            "",
            "sync_runtime queued by skeleton driver",
        )))
    }

    async fn sync_xray(
        &self,
        request: Request<SyncXrayRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(self.state.start_operation(
            "sync_xray",
            &req.node_key,
            "",
            "sync_xray queued by skeleton driver",
        )))
    }

    async fn get_runtime_status(
        &self,
        request: Request<GetRuntimeStatusRequest>,
    ) -> Result<Response<GetRuntimeStatusResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(GetRuntimeStatusResponse {
            runtime: Some(RuntimeStatus {
                node_key: req.node_key,
                state: "unknown".to_string(),
                version: String::new(),
                commit: String::new(),
                expected_version: String::new(),
                expected_commit: String::new(),
                message: "runtime status not implemented yet".to_string(),
            }),
            services: vec![ServiceStatus {
                service_name: "node-driver".to_string(),
                state: "running".to_string(),
                summary: "skeleton service".to_string(),
            }],
        }))
    }

    async fn list_nodes_needing_runtime_sync(
        &self,
        _request: Request<ListNodesNeedingRuntimeSyncRequest>,
    ) -> Result<Response<ListNodesNeedingRuntimeSyncResponse>, Status> {
        Ok(Response::new(ListNodesNeedingRuntimeSyncResponse {
            items: Vec::new(),
        }))
    }
}

#[tonic::async_trait]
impl TelemetryService for TelemetryApi {
    async fn collect_traffic_snapshot(
        &self,
        request: Request<driver::v1::CollectTrafficSnapshotRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        let node_key = req.node_keys.first().cloned().unwrap_or_default();
        Ok(Response::new(self.state.start_operation(
            "collect_traffic_snapshot",
            &node_key,
            "",
            "collect_traffic_snapshot queued by skeleton driver",
        )))
    }

    async fn get_profile_usage(
        &self,
        request: Request<GetProfileUsageRequest>,
    ) -> Result<Response<GetProfileUsageResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(GetProfileUsageResponse {
            usage: Some(ProfileUsage {
                profile_name: req.profile_name,
                protocol_kind: req.protocol_kind,
                rx_bytes: 0,
                tx_bytes: 0,
                total_bytes: 0,
                samples: 0,
                peers: 0,
            }),
        }))
    }
}

#[tonic::async_trait]
impl OperationService for OperationApi {
    async fn get_operation(
        &self,
        request: Request<GetOperationRequest>,
    ) -> Result<Response<Operation>, Status> {
        let req = request.into_inner();
        match self.state.get_operation(&req.operation_id) {
            Some(op) => Ok(Response::new(op)),
            None => Err(Status::not_found("operation not found")),
        }
    }

    type WatchOperationStream =
        tokio_stream::wrappers::ReceiverStream<Result<driver::v1::OperationEvent, Status>>;

    async fn watch_operation(
        &self,
        _request: Request<WatchOperationRequest>,
    ) -> Result<Response<Self::WatchOperationStream>, Status> {
        let (_tx, rx) = tokio::sync::mpsc::channel(1);
        Ok(Response::new(tokio_stream::wrappers::ReceiverStream::new(
            rx,
        )))
    }

    async fn list_operations(
        &self,
        request: Request<ListOperationsRequest>,
    ) -> Result<Response<ListOperationsResponse>, Status> {
        let req = request.into_inner();
        let items =
            self.state
                .list_operations(&req.node_key, &req.profile_name, &req.status, req.limit);
        Ok(Response::new(ListOperationsResponse { items }))
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let addr: SocketAddr = std::env::var("NODE_DRIVER_LISTEN_ADDR")
        .unwrap_or_else(|_| "127.0.0.1:50051".to_string())
        .parse()?;

    let state = DriverState::default();
    let node_api = NodeApi {
        state: state.clone(),
    };
    let provisioning_api = ProvisioningApi {
        state: state.clone(),
    };
    let runtime_api = RuntimeApi {
        state: state.clone(),
    };
    let telemetry_api = TelemetryApi {
        state: state.clone(),
    };
    let operation_api = OperationApi { state };

    println!("node-plane-driver listening on {}", addr);

    Server::builder()
        .add_service(NodeServiceServer::new(node_api))
        .add_service(ProvisioningServiceServer::new(provisioning_api))
        .add_service(RuntimeServiceServer::new(runtime_api))
        .add_service(TelemetryServiceServer::new(telemetry_api))
        .add_service(OperationServiceServer::new(operation_api))
        .serve(addr)
        .await?;

    Ok(())
}
