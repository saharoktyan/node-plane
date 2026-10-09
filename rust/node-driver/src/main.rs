use std::collections::HashMap;
use std::env;
use std::fs;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};

use serde::Deserialize;
use tonic::{Request, Response, Status, transport::Server};

mod agent_transport;
mod operations;

use operations::{CommandIdentity, CommandStart, DriverState};

#[cfg(test)]
mod tests;

#[cfg(test)]
mod command_tests;

pub mod agent {
    pub mod v1 {
        tonic::include_proto!("nodeplane.agent.v1");
    }
}

pub mod driver {
    pub mod v1 {
        tonic::include_proto!("nodeplane.driver.v1");
    }
}

use agent::v1::RuntimeFileSpec;
use driver::v1::node_service_server::{NodeService, NodeServiceServer};
use driver::v1::operation_service_server::{OperationService, OperationServiceServer};
use driver::v1::provisioning_service_server::{ProvisioningService, ProvisioningServiceServer};
use driver::v1::runtime_service_server::{RuntimeService, RuntimeServiceServer};
use driver::v1::{
    ApplyBackendNodeSettingsRequest, BackendNodeObservation, BackendNodeSettingsResult,
    DecommissionNodeRequest, DecommissionNodeResponse, GetNodeDiagnosticsRequest,
    GetNodeDiagnosticsResponse, GetOperationRequest, InspectBackendNodeRequest,
    ListOperationsRequest, ListOperationsResponse, Operation, OperationEvent,
    RefreshAwgConfigRequest, RefreshAwgConfigResponse, StartOperationResponse,
    WatchOperationRequest,
};

#[derive(Clone)]
struct DriverContext {
    state: DriverState,
    app_semver: String,
    app_commit: String,
    agent_targets: HashMap<String, String>,
}

#[derive(Debug, Clone, Deserialize)]
struct RuntimeAssetManifestEntry {
    target_path: String,
    asset_path: String,
    mode: String,
}

#[derive(Debug, Clone, Deserialize)]
struct XrayPublicMetadata {
    xray_sni: String,
    xray_pbk: String,
    xray_sid: String,
    xray_short_id: String,
    xray_flow: String,
    xray_fp: String,
    xray_tcp_port: i32,
    xray_xhttp_port: i32,
    xray_xhttp_path_prefix: String,
}

impl XrayPublicMetadata {
    fn parse_public(raw: &str) -> Result<Self, Status> {
        let generated: Self = serde_json::from_str(raw)
            .map_err(|err| Status::internal(format!("invalid Xray public metadata: {err}")))?;
        // Xray Reality public keys are 32-byte URL-safe base64 values without padding.
        // Reject old helper output such as "(Public Key): ..." before it reaches the backend.
        if generated.xray_pbk.len() != 43
            || !generated
                .xray_pbk
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
        {
            return Err(Status::internal(
                "invalid Reality public key returned by runtime helper",
            ));
        }
        Ok(generated)
    }
}

// Only a loopback agent can be removed without an SSH public key.
fn decommission_public_key(target: &str, key: Result<String, Status>) -> Result<String, Status> {
    let endpoint = target
        .strip_prefix("http://")
        .or_else(|| target.strip_prefix("https://"))
        .unwrap_or(target);
    let local = endpoint
        .parse::<std::net::SocketAddr>()
        .map(|address| address.ip().is_loopback())
        .unwrap_or(false)
        || endpoint
            .strip_prefix("localhost:")
            .and_then(|port| port.parse::<u16>().ok())
            .is_some();
    match key {
        Err(_) if local => Ok(String::new()),
        other => other,
    }
}

impl DriverContext {
    fn runtime_assets_dir(&self) -> PathBuf {
        let app_root = env::var_os("NODE_PLANE_APP_DIR").map(PathBuf::from);
        runtime_assets_dir_from(app_root, Path::new(env!("CARGO_MANIFEST_DIR")))
    }

    fn runtime_manifest_path(&self) -> PathBuf {
        self.runtime_assets_dir().join("manifest.json")
    }

    fn load_runtime_manifest(&self) -> Result<Vec<RuntimeAssetManifestEntry>, Status> {
        let path = self.runtime_manifest_path();
        let raw = fs::read_to_string(&path).map_err(|err| {
            Status::internal(format!(
                "failed to read runtime manifest at {}: {err}",
                path.display()
            ))
        })?;
        serde_json::from_str::<Vec<RuntimeAssetManifestEntry>>(&raw)
            .map_err(|err| Status::internal(format!("failed to parse runtime manifest: {err}")))
    }

    fn from_env() -> std::io::Result<Self> {
        Self::load_runtime_env_file();
        let (app_semver, app_commit) =
            runtime_release_identity(env::var("APP_SEMVER").ok(), env::var("APP_COMMIT").ok());
        let operations_path = env::var("NODE_DRIVER_OPERATIONS_PATH")
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                PathBuf::from(Self::candidate_shared_root()).join("data/node-driver-operations.bin")
            });
        Ok(Self {
            state: DriverState::open(operations_path)?,
            app_semver,
            app_commit,
            agent_targets: Self::parse_agent_targets(),
        })
    }

    fn parse_agent_targets() -> HashMap<String, String> {
        let mut targets: HashMap<String, String> = env::var("NODE_AGENT_TARGETS")
            .ok()
            .unwrap_or_default()
            .split(',')
            .filter_map(|item| {
                let trimmed = item.trim();
                let (node_key, target) = trimmed.split_once('=')?;
                let node_key = node_key.trim();
                let target = target.trim();
                if node_key.is_empty() || target.is_empty() {
                    None
                } else {
                    Some((node_key.to_string(), target.to_string()))
                }
            })
            .collect();
        let local_config = env::var("NODE_AGENT_CONFIG_PATH")
            .unwrap_or_else(|_| "/etc/node-plane/agent.toml".to_string());
        if let Some((node_key, target)) = local_agent_target_from_config(Path::new(&local_config)) {
            targets.entry(node_key).or_insert(target);
        }
        targets
    }

    fn bot_public_key(&self) -> Result<String, Status> {
        if let Ok(value) = env::var("NODE_PLANE_SSH_PUBLIC_KEY") {
            let trimmed = value.trim();
            if !trimmed.is_empty() {
                return Ok(trimmed.to_string());
            }
        }
        let private_key_path = env::var("SSH_KEY")
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .or_else(|| {
                env::var("NODE_PLANE_SSH_KEY")
                    .ok()
                    .map(|value| value.trim().to_string())
                    .filter(|value| !value.is_empty())
            })
            .unwrap_or_else(|| {
                let ssh_dir = env::var("SSH_DIR")
                    .or_else(|_| env::var("NODE_PLANE_SSH_DIR"))
                    .ok()
                    .map(|value| value.trim().to_string())
                    .filter(|value| !value.is_empty())
                    .unwrap_or_else(|| "/opt/node-plane/ssh".to_string());
                format!("{ssh_dir}/id_ed25519")
            });
        let public_key_path = format!("{private_key_path}.pub");
        fs::read_to_string(&public_key_path)
            .map(|value| value.trim().to_string())
            .map_err(|err| {
                Status::failed_precondition(format!(
                    "failed to read bot public key from {public_key_path}: {err}"
                ))
            })
    }

    fn candidate_shared_root() -> String {
        let install_root = env::var("NODE_PLANE_BASE_DIR")
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| "/opt/node-plane".to_string());
        let app_root = env::var("NODE_PLANE_APP_DIR")
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| install_root.clone());
        env::var("NODE_PLANE_SHARED_DIR")
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .unwrap_or(app_root)
    }

    fn load_runtime_env_file() {
        let env_path = format!("{}/.env", Self::candidate_shared_root());
        let Ok(content) = fs::read_to_string(&env_path) else {
            return;
        };
        for raw_line in content.lines() {
            let line = raw_line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let Some((key, value)) = line.split_once('=') else {
                continue;
            };
            let key = key.trim();
            if key.is_empty() || env::var_os(key).is_some() {
                continue;
            }
            let value = value.trim();
            // SAFETY: this process updates env only during startup before worker tasks are spawned.
            unsafe { env::set_var(key, value) };
        }
    }

    fn agent_target(&self, node_key: &str) -> Option<&str> {
        self.agent_targets.get(node_key).map(String::as_str)
    }

    fn shell_quote(&self, value: &str) -> String {
        if value.is_empty() {
            return "''".to_string();
        }
        format!("'{}'", value.replace('\'', "'\"'\"'"))
    }

    fn shell_env_assignment(&self, name: &str, value: impl ToString) -> String {
        format!("{name}={}", self.shell_quote(value.to_string().as_str()))
    }

    fn render_default_node_env(&self, node_key: &str) -> String {
        let lines = vec![
            self.shell_env_assignment("SERVER_KEY", node_key),
            self.shell_env_assignment("XRAY_CONFIG", "/opt/node-plane-runtime/xray/config.json"),
            self.shell_env_assignment("XRAY_CONTAINER_NAME", "xray"),
            self.shell_env_assignment("XRAY_DOCKER_DIR", "/opt/node-plane-runtime/xray"),
            self.shell_env_assignment("XRAY_DOCKER_IMAGE", "ghcr.io/xtls/xray-core:26.3.27"),
            self.shell_env_assignment("XRAY_INBOUND_TCP_TAG", "reality-tcp"),
            self.shell_env_assignment("XRAY_INBOUND_XHTTP_TAG", "reality-xhttp"),
            self.shell_env_assignment("AWG_CONTAINER_NAME", "amnezia-awg"),
            self.shell_env_assignment("AWG_DOCKER_DIR", "/opt/node-plane-runtime/amnezia-awg"),
            self.shell_env_assignment("AWG_DOCKER_IMAGE", "node-plane-amnezia-awg:3.1.20260828"),
            self.shell_env_assignment("AWG_IFACE", "wg0"),
            self.shell_env_assignment(
                "AWG_CONFIG",
                "/opt/node-plane-runtime/amnezia-awg/data/wg0.conf",
            ),
            self.shell_env_assignment("AWG_SERVER_ADDRESS", "10.8.1.0/24"),
            self.shell_env_assignment("AWG_NETWORK", "10.8.1.0/24"),
            self.shell_env_assignment("AWG_DNS", "1.1.1.1"),
            self.shell_env_assignment("AWG_MTU", "1280"),
            self.shell_env_assignment("AWG_ALLOWED_IPS", "0.0.0.0/0"),
            self.shell_env_assignment("AWG_KEEPALIVE", "25"),
            self.shell_env_assignment("AWG_I1_PRESET", "quic"),
            self.shell_env_assignment("AWG_SERVER_IP", ""),
            self.shell_env_assignment("AWG_SERVER_PORT", 51820),
        ];
        format!("{}\n", lines.join("\n"))
    }

    fn runtime_file_bundle(&self, node_key: &str) -> Result<Vec<RuntimeFileSpec>, Status> {
        let manifest = self.load_runtime_manifest()?;
        let assets_dir = self.runtime_assets_dir();
        let mut files = Vec::new();
        for entry in manifest {
            let content =
                fs::read_to_string(assets_dir.join(&entry.asset_path)).map_err(|err| {
                    Status::internal(format!(
                        "failed to read runtime asset {}: {err}",
                        entry.asset_path
                    ))
                })?;
            files.push(RuntimeFileSpec {
                path: entry.target_path,
                content,
                mode: entry.mode,
            });
        }
        files.push(RuntimeFileSpec {
            path: "/opt/node-plane-runtime/VERSION".to_string(),
            content: format!("{}\n", self.app_semver),
            mode: "0644".to_string(),
        });
        files.push(RuntimeFileSpec {
            path: "/opt/node-plane-runtime/BUILD_COMMIT".to_string(),
            content: format!("{}\n", self.app_commit),
            mode: "0644".to_string(),
        });
        let node_env = self.render_default_node_env(node_key);
        files.push(RuntimeFileSpec {
            path: "/etc/node-plane/node.env".to_string(),
            content: node_env,
            mode: "0600".to_string(),
        });
        Ok(files)
    }
}

fn runtime_release_identity(version: Option<String>, commit: Option<String>) -> (String, String) {
    let resolve = |value: Option<String>, fallback: &str| {
        value
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty() && v != "unknown")
            .unwrap_or_else(|| fallback.to_string())
    };
    (
        resolve(version, env!("NODE_PLANE_BINARY_VERSION")),
        resolve(commit, env!("NODE_PLANE_BINARY_COMMIT")),
    )
}

fn runtime_assets_dir_from(app_root: Option<PathBuf>, manifest_dir: &Path) -> PathBuf {
    app_root
        .map(|root| root.join("runtime_assets"))
        .unwrap_or_else(|| manifest_dir.join("../..").join("runtime_assets"))
}

fn local_agent_target_from_config(path: &Path) -> Option<(String, String)> {
    let content = fs::read_to_string(path).ok()?;
    local_agent_target_from_config_content(&content)
}

fn local_agent_target_from_config_content(content: &str) -> Option<(String, String)> {
    #[derive(serde::Deserialize)]
    struct LocalTarget {
        node_key: String,
        listen_addr: std::net::SocketAddr,
    }
    let config: LocalTarget = toml::from_str(content).ok()?;
    if config.node_key.trim().is_empty()
        || !config.listen_addr.ip().is_loopback()
        || config.listen_addr.port() == 0
    {
        return None;
    }
    Some((config.node_key, config.listen_addr.to_string()))
}

#[derive(Clone)]
struct NodeApi {
    ctx: DriverContext,
}

#[derive(Clone)]
struct ProvisioningApi {
    ctx: DriverContext,
}

#[derive(Clone)]
struct RuntimeApi {
    ctx: DriverContext,
}

#[derive(Clone)]
struct OperationApi {
    ctx: DriverContext,
}

#[tonic::async_trait]
impl NodeService for NodeApi {
    async fn inspect_backend_node(
        &self,
        request: Request<InspectBackendNodeRequest>,
    ) -> Result<Response<BackendNodeObservation>, Status> {
        let node_key = request.into_inner().node_key;
        if node_key.trim().is_empty() {
            return Err(Status::invalid_argument("node_key is required"));
        }
        let target = self
            .ctx
            .agent_target(&node_key)
            .ok_or_else(|| Status::failed_precondition("no node-agent target configured"))?;
        let transport = agent_transport::AgentTransport::new(target);
        let facts = transport.get_runtime_facts().await?;
        if facts.node_key != node_key {
            return Err(Status::failed_precondition("agent node identity mismatch"));
        }
        let health = transport.get_node_health().await?;
        Ok(Response::new(BackendNodeObservation {
            node_key,
            health_state: health.state,
            runtime_version: facts.version,
            runtime_commit: facts.commit,
            xray_config_present: facts.xray_config_present,
            awg_config_present: facts.awg_config_present,
            agent_version: facts.binary_version,
            agent_commit: facts.binary_commit,
        }))
    }

    async fn get_node_diagnostics(
        &self,
        request: Request<GetNodeDiagnosticsRequest>,
    ) -> Result<Response<GetNodeDiagnosticsResponse>, Status> {
        let req = request.into_inner();
        if let Some(target) = self.ctx.agent_target(&req.node_key) {
            let transport = agent_transport::AgentTransport::new(target);
            if let Ok(agent_response) = transport.run_diagnostics().await {
                let items = agent_response
                    .items
                    .into_iter()
                    .map(|item| driver::v1::DiagnosticItem {
                        kind: item.kind,
                        status: item.status,
                        summary: item.summary,
                        detail: item.detail,
                    })
                    .collect();
                return Ok(Response::new(GetNodeDiagnosticsResponse {
                    node_key: req.node_key,
                    summary: agent_response.summary,
                    items,
                }));
            }
            return Err(Status::unavailable(
                "node agent diagnostics are unavailable",
            ));
        }
        Err(Status::failed_precondition(
            "no node-agent target configured",
        ))
    }
}

fn validate_profile_intent(req: &driver::v1::ApplyProfileIntentRequest) -> Result<(), Status> {
    let valid_name = |value: &str| {
        !value.is_empty()
            && value.len() <= 64
            && value
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
    };
    if !valid_name(&req.node_key)
        || !valid_name(&req.runtime_name)
        || req.desired_revision == 0
        || !matches!(req.protocol_kind.as_str(), "awg" | "xray")
        || !matches!(req.action.as_str(), "ensure" | "delete")
    {
        return Err(Status::invalid_argument("invalid profile intent"));
    }
    if req.protocol_kind == "xray" && req.action == "ensure" {
        let spec = req
            .xray
            .as_ref()
            .ok_or_else(|| Status::invalid_argument("xray identity is required"))?;
        if spec.profile_name != req.runtime_name
            || uuid::Uuid::parse_str(&spec.uuid).is_err()
            || spec.short_id.len() != 16
            || !spec.short_id.bytes().all(|c| c.is_ascii_hexdigit())
        {
            return Err(Status::invalid_argument("invalid xray identity"));
        }
    } else if req.xray.is_some() {
        return Err(Status::invalid_argument("unexpected xray identity"));
    }
    Ok(())
}

#[tonic::async_trait]
impl ProvisioningService for ProvisioningApi {
    async fn recover_profile_intent(
        &self,
        request: Request<driver::v1::ApplyProfileIntentRequest>,
    ) -> Result<Response<driver::v1::RecoverProfileIntentResponse>, Status> {
        validate_profile_intent(request.get_ref())?;
        let identity = CommandIdentity::from_request(&request)?
            .ok_or_else(|| Status::invalid_argument("command identity is required"))?;
        let command_id = identity.command_id().to_string();
        let req = request.into_inner();
        let target = self
            .ctx
            .agent_target(&req.node_key)
            .ok_or_else(|| Status::failed_precondition("agent not configured"))?;
        let spec = req.xray.as_ref();
        let result = agent_transport::AgentTransport::new(target)
            .recover_profile_intent(agent::v1::ApplyProfileIntentRequest {
                command_id,
                protocol_kind: req.protocol_kind,
                profile_name: req.runtime_name,
                desired_revision: req.desired_revision,
                action: req.action,
                uuid: spec.map_or_else(String::new, |s| s.uuid.clone()),
                short_id: spec.map_or_else(String::new, |s| s.short_id.clone()),
            })
            .await?;
        Ok(Response::new(driver::v1::RecoverProfileIntentResponse {
            payload_json: result.payload_json,
        }))
    }

    async fn resolve_profile_intent(
        &self,
        request: Request<driver::v1::ApplyProfileIntentRequest>,
    ) -> Result<Response<driver::v1::ProfileInspection>, Status> {
        validate_profile_intent(request.get_ref())?;
        let identity = CommandIdentity::from_request(&request)?
            .ok_or_else(|| Status::invalid_argument("command identity is required"))?;
        let command_id = identity.command_id().to_string();
        let req = request.into_inner();
        let target = self
            .ctx
            .agent_target(&req.node_key)
            .ok_or_else(|| Status::failed_precondition("agent not configured"))?;
        let spec = req.xray.as_ref();
        let result = agent_transport::AgentTransport::new(target)
            .resolve_profile_intent(agent::v1::ApplyProfileIntentRequest {
                command_id,
                protocol_kind: req.protocol_kind,
                profile_name: req.runtime_name,
                desired_revision: req.desired_revision,
                action: req.action,
                uuid: spec.map_or_else(String::new, |s| s.uuid.clone()),
                short_id: spec.map_or_else(String::new, |s| s.short_id.clone()),
            })
            .await?;
        Ok(Response::new(driver::v1::ProfileInspection {
            disk_present: result.disk_present,
            live_present: result.live_present,
            identity_matches: result.identity_matches,
            config_available: result.config_available,
        }))
    }

    async fn inspect_profile_intent(
        &self,
        request: Request<driver::v1::ApplyProfileIntentRequest>,
    ) -> Result<Response<driver::v1::ProfileInspection>, Status> {
        let req = request.into_inner();
        validate_profile_intent(&req)?;
        let target = self
            .ctx
            .agent_target(&req.node_key)
            .ok_or_else(|| Status::failed_precondition("agent not configured"))?;
        let expected = req.xray.as_ref().map_or("", |s| s.uuid.as_str());
        let result = agent_transport::AgentTransport::new(target)
            .inspect_profile(&req.protocol_kind, &req.runtime_name, expected)
            .await?;
        Ok(Response::new(driver::v1::ProfileInspection {
            disk_present: result.disk_present,
            live_present: result.live_present,
            identity_matches: result.identity_matches,
            config_available: result.config_available,
        }))
    }

    async fn apply_profile_intent(
        &self,
        request: Request<driver::v1::ApplyProfileIntentRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        validate_profile_intent(request.get_ref())?;
        let identity = CommandIdentity::from_request(&request)?
            .ok_or_else(|| Status::invalid_argument("command identity is required"))?;
        let command_id = identity.command_id().to_string();
        let req = request.into_inner();
        let execution = match self.ctx.state.begin_command(
            "apply_profile_intent",
            &req.node_key,
            &req.runtime_name,
            Some(identity),
        )? {
            CommandStart::New(operation) => operation,
            CommandStart::Existing(response) => return Ok(Response::new(response)),
        };
        let Some(target) = self.ctx.agent_target(&req.node_key) else {
            return Ok(Response::new(execution.missing_agent()?));
        };
        let transport = agent_transport::AgentTransport::new(target);
        let spec = req.xray.as_ref();
        let result = transport
            .apply_profile_intent(agent::v1::ApplyProfileIntentRequest {
                command_id,
                protocol_kind: req.protocol_kind,
                profile_name: req.runtime_name,
                desired_revision: req.desired_revision,
                action: req.action,
                uuid: spec.map_or_else(String::new, |s| s.uuid.clone()),
                short_id: spec.map_or_else(String::new, |s| s.short_id.clone()),
            })
            .await;
        let response = match result {
            Ok(result) => execution.finish_with_result(
                "SUCCEEDED",
                "profile intent applied",
                &result.payload_json,
            )?,
            // A transport error does not establish whether the agent applied the
            // command. Do not automatically issue another identity after failure.
            Err(_) => execution.fail_with_error(
                "outcome_unknown",
                "agent execution outcome is unknown; reconcile node state",
            )?,
        };
        Ok(Response::new(response))
    }
}

#[tonic::async_trait]
impl RuntimeService for RuntimeApi {
    async fn backend_node_action(
        &self,
        request: Request<driver::v1::BackendNodeActionRequest>,
    ) -> Result<Response<BackendNodeSettingsResult>, Status> {
        let req = request.into_inner();
        if req.action == "driver_info" {
            return Ok(Response::new(BackendNodeSettingsResult {
                result_json: serde_json::json!({
                    "version": env!("NODE_PLANE_BINARY_VERSION"),
                    "commit": env!("NODE_PLANE_BINARY_COMMIT")
                })
                .to_string(),
            }));
        }
        let target = self
            .ctx
            .agent_target(&req.node_key)
            .ok_or_else(|| Status::failed_precondition("no node-agent target configured"))?;
        let transport = agent_transport::AgentTransport::new(target);
        let facts = transport.get_runtime_facts().await?;
        if facts.node_key != req.node_key {
            return Err(Status::failed_precondition("agent node identity mismatch"));
        }
        if req.action == "install_docker" && !req.recover {
            transport.install_docker().await?;
        }
        if req.action == "inspect"
            && !transport
                .path_exists("/opt/node-plane-runtime/backend-node-operation.py")
                .await?
        {
            let diagnostics = transport.run_diagnostics().await?;
            let docker = diagnostics
                .items
                .iter()
                .any(|item| item.kind == "docker" && item.status == "ok");
            return Ok(Response::new(BackendNodeSettingsResult { result_json: serde_json::json!({
                "docker": docker, "xray_config_valid": false, "awg_config_valid": false,
                "xray_installed": false, "awg_installed": false,
                "xray_running": false, "awg_running": false, "entropy": [],
                "runtime_version": facts.version, "runtime_commit": facts.commit,
                "desired_runtime_version": self.ctx.app_semver, "desired_runtime_commit": self.ctx.app_commit,
                "agent_version": facts.binary_version, "agent_commit": facts.binary_commit,
                "runtime_drift": true,
            }).to_string() }));
        }
        if !matches!(req.action.as_str(), "inspect" | "traffic") && !req.recover {
            let mut files = self.ctx.runtime_file_bundle(&req.node_key)?;
            if transport.path_exists("/etc/node-plane/node.env").await? {
                files.retain(|file| file.path != "/etc/node-plane/node.env");
            }
            transport.sync_runtime_files(files).await?;
        }
        let response = transport
            .backend_node_action(agent::v1::BackendNodeActionRequest {
                node_key: req.node_key,
                command_id: req.command_id,
                action: req.action.clone(),
                intent_json: req.intent_json,
                recover: req.recover,
            })
            .await?;
        let mut result: serde_json::Value = serde_json::from_str(&response.payload_json)
            .map_err(|_| Status::internal("invalid backend node response"))?;
        if req.action == "inspect" {
            result["agent_version"] = serde_json::json!(facts.binary_version);
            result["agent_commit"] = serde_json::json!(facts.binary_commit);
            result["runtime_version"] = serde_json::json!(facts.version);
            result["runtime_commit"] = serde_json::json!(facts.commit);
            result["desired_runtime_version"] = serde_json::json!(self.ctx.app_semver);
            result["desired_runtime_commit"] = serde_json::json!(self.ctx.app_commit);
            result["runtime_drift"] = serde_json::json!(
                facts.commit != self.ctx.app_commit || facts.version != self.ctx.app_semver
            );
        }
        Ok(Response::new(BackendNodeSettingsResult {
            result_json: result.to_string(),
        }))
    }
    async fn get_backend_xray_public(
        &self,
        request: Request<driver::v1::GetBackendXrayPublicRequest>,
    ) -> Result<Response<driver::v1::BackendXrayPublicResult>, Status> {
        let node_key = request.into_inner().node_key;
        if node_key.is_empty() {
            return Err(Status::invalid_argument("node key is required"));
        }
        let target = self
            .ctx
            .agent_target(&node_key)
            .ok_or_else(|| Status::failed_precondition("no node-agent target configured"))?;
        let transport = agent_transport::AgentTransport::new(target);
        let facts = transport.get_runtime_facts().await?;
        if facts.node_key != node_key {
            return Err(Status::failed_precondition("agent node identity mismatch"));
        }
        let response = transport.get_backend_xray_public().await?;
        let metadata = XrayPublicMetadata::parse_public(&response.metadata_json)?;
        if metadata.xray_short_id.len() != 16
            || !metadata
                .xray_short_id
                .bytes()
                .all(|b| b.is_ascii_hexdigit())
            || metadata.xray_sid != metadata.xray_short_id
            || metadata.xray_sni.is_empty()
            || metadata.xray_tcp_port <= 0
            || metadata.xray_xhttp_port <= 0
        {
            return Err(Status::internal(
                "invalid Xray public metadata returned by agent",
            ));
        }
        let public = serde_json::json!({
            "sni": metadata.xray_sni,
            "public_key": metadata.xray_pbk,
            "short_id": metadata.xray_short_id,
            "tcp_port": metadata.xray_tcp_port,
            "xhttp_port": metadata.xray_xhttp_port,
            "xhttp_path": metadata.xray_xhttp_path_prefix,
            "flow": metadata.xray_flow,
            "fingerprint": metadata.xray_fp,
        });
        Ok(Response::new(driver::v1::BackendXrayPublicResult {
            node_key,
            metadata_json: public.to_string(),
        }))
    }

    async fn prepare_backend_node(
        &self,
        request: Request<driver::v1::PrepareBackendNodeRequest>,
    ) -> Result<Response<BackendNodeSettingsResult>, Status> {
        let node_key = request.into_inner().node_key;
        if node_key.is_empty()
            || node_key.len() > 64
            || !node_key
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
        {
            return Err(Status::invalid_argument("node key is required"));
        }
        let target = self
            .ctx
            .agent_target(&node_key)
            .ok_or_else(|| Status::failed_precondition("no node-agent target configured"))?;
        let transport = agent_transport::AgentTransport::new(target);
        let facts = transport.get_runtime_facts().await?;
        if facts.node_key != node_key {
            return Err(Status::failed_precondition("agent node identity mismatch"));
        }
        if transport
            .path_exists("/etc/node-plane/profile-intents.sqlite3")
            .await?
        {
            return Err(Status::failed_precondition(
                "managed runtime cannot be prepared as a fresh node",
            ));
        }
        let env_exists = transport.path_exists("/etc/node-plane/node.env").await?;
        let mut files = self.ctx.runtime_file_bundle(&node_key)?;
        if env_exists {
            // Preserve an existing environment, especially when preparation is
            // retried after an interrupted Docker installation.
            files.retain(|file| file.path != "/etc/node-plane/node.env");
        }
        transport.sync_runtime_files(files).await?;
        transport.install_docker().await?;
        Ok(Response::new(BackendNodeSettingsResult {
            result_json: format!("{{\"node_key\":\"{}\",\"prepared\":true}}", node_key),
        }))
    }

    async fn apply_backend_node_settings(
        &self,
        request: Request<ApplyBackendNodeSettingsRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let identity = CommandIdentity::from_request(&request)?
            .ok_or_else(|| Status::invalid_argument("stable command id is required"))?;
        let command_id = identity.command_id().to_string();
        let req = request.into_inner();
        if req.node_key.is_empty() || req.desired_revision == 0 {
            return Err(Status::invalid_argument(
                "node key and revision are required",
            ));
        }
        let execution = match self.ctx.state.begin_command(
            "apply_backend_node_settings",
            &req.node_key,
            "",
            Some(identity),
        )? {
            CommandStart::New(operation) => operation,
            CommandStart::Existing(response) => return Ok(Response::new(response)),
        };
        let Some(target) = self.ctx.agent_target(&req.node_key) else {
            return Ok(Response::new(execution.missing_agent()?));
        };
        let transport = agent_transport::AgentTransport::new(target);
        let result: Result<String, Status> = async {
            let facts = transport.get_runtime_facts().await?;
            if facts.node_key != req.node_key {
                return Err(Status::failed_precondition("agent node identity mismatch"));
            }
            let response = transport
                .apply_backend_node_settings(agent::v1::ApplyBackendNodeSettingsRequest {
                    command_id,
                    desired_revision: req.desired_revision,
                    protocols_json: req.protocols_json,
                    settings_json: req.settings_json,
                    node_key: req.node_key,
                })
                .await?;
            Ok(response.payload_json)
        }
        .await;
        Ok(Response::new(match result {
            Ok(payload) => execution.finish_with_result(
                "SUCCEEDED",
                "backend node settings applied",
                &payload,
            )?,
            Err(error) => execution.finish("FAILED", &error.to_string())?,
        }))
    }

    async fn recover_backend_node_settings(
        &self,
        request: Request<ApplyBackendNodeSettingsRequest>,
    ) -> Result<Response<BackendNodeSettingsResult>, Status> {
        let command_id = CommandIdentity::from_request(&request)?
            .ok_or_else(|| Status::invalid_argument("stable command id is required"))?
            .command_id()
            .to_string();
        let req = request.into_inner();
        let target = self
            .ctx
            .agent_target(&req.node_key)
            .ok_or_else(|| Status::failed_precondition("no node-agent target configured"))?;
        let transport = agent_transport::AgentTransport::new(target);
        let facts = transport.get_runtime_facts().await?;
        if facts.node_key != req.node_key {
            return Err(Status::failed_precondition("agent node identity mismatch"));
        }
        let response = transport
            .recover_backend_node_settings(agent::v1::ApplyBackendNodeSettingsRequest {
                command_id,
                desired_revision: req.desired_revision,
                protocols_json: req.protocols_json,
                settings_json: req.settings_json,
                node_key: req.node_key,
            })
            .await?;
        Ok(Response::new(BackendNodeSettingsResult {
            result_json: response.payload_json,
        }))
    }

    async fn resolve_backend_node_settings(
        &self,
        request: Request<ApplyBackendNodeSettingsRequest>,
    ) -> Result<Response<BackendNodeSettingsResult>, Status> {
        let command_id = CommandIdentity::from_request(&request)?
            .ok_or_else(|| Status::invalid_argument("stable command id is required"))?
            .command_id()
            .to_string();
        let req = request.into_inner();
        let target = self
            .ctx
            .agent_target(&req.node_key)
            .ok_or_else(|| Status::failed_precondition("no node-agent target configured"))?;
        let transport = agent_transport::AgentTransport::new(target);
        let facts = transport.get_runtime_facts().await?;
        if facts.node_key != req.node_key {
            return Err(Status::failed_precondition("agent node identity mismatch"));
        }
        let response = transport
            .resolve_backend_node_settings(agent::v1::ApplyBackendNodeSettingsRequest {
                command_id,
                desired_revision: req.desired_revision,
                protocols_json: req.protocols_json,
                settings_json: req.settings_json,
                node_key: req.node_key,
            })
            .await?;
        Ok(Response::new(BackendNodeSettingsResult {
            result_json: response.payload_json,
        }))
    }

    async fn decommission_node(
        &self,
        request: Request<DecommissionNodeRequest>,
    ) -> Result<Response<DecommissionNodeResponse>, Status> {
        let req = request.into_inner();
        if uuid::Uuid::parse_str(&req.command_id)
            .map(|value| value.to_string() != req.command_id)
            .unwrap_or(true)
        {
            return Err(Status::invalid_argument(
                "invalid decommission command identity",
            ));
        }
        let target = self
            .ctx
            .agent_target(&req.node_key)
            .ok_or_else(|| Status::failed_precondition("no node-agent target configured"))?;
        let transport = agent_transport::AgentTransport::new(target);
        let summary = match req.phase.as_str() {
            "prepare" => {
                transport.prepare_decommission(&req.command_id).await?;
                "profile mutations fenced"
            }
            "delete_runtime" => {
                transport
                    .delete_runtime_for_decommission(&req.command_id)
                    .await?;
                "runtime cleanup verified by agent"
            }
            "uninstall" => {
                let public_key = decommission_public_key(target, self.ctx.bot_public_key())?;
                transport
                    .uninstall_agent_for_decommission(&req.command_id, &public_key)
                    .await?;
                "agent uninstall scheduled; independent verification still required"
            }
            _ => return Err(Status::invalid_argument("invalid decommission phase")),
        };
        Ok(Response::new(DecommissionNodeResponse {
            phase: req.phase,
            summary: summary.to_string(),
        }))
    }
    async fn refresh_awg_config(
        &self,
        request: Request<RefreshAwgConfigRequest>,
    ) -> Result<Response<RefreshAwgConfigResponse>, Status> {
        let req = request.into_inner();
        if req.wg_conf.is_empty() {
            return Err(Status::invalid_argument("stored AWG config is missing"));
        }
        let target = self
            .ctx
            .agent_target(&req.node_key)
            .ok_or_else(|| Status::unavailable("node agent is unavailable"))?;
        let refreshed = agent_transport::AgentTransport::new(target)
            .refresh_awg_config(&req.wg_conf, &req.profile_name)
            .await?;
        Ok(Response::new(RefreshAwgConfigResponse {
            wg_conf: refreshed.wg_conf,
            vpn_key: refreshed.vpn_key,
        }))
    }
}

#[tonic::async_trait]
impl OperationService for OperationApi {
    async fn get_operation_by_command(
        &self,
        request: Request<driver::v1::GetOperationByCommandRequest>,
    ) -> Result<Response<Operation>, Status> {
        let command_id = request.into_inner().command_id;
        if command_id.is_empty()
            || command_id.len() > 128
            || !command_id
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"-_.:".contains(&c))
        {
            return Err(Status::invalid_argument("invalid command identity"));
        }
        self.ctx
            .state
            .get_operation_by_command(&command_id)
            .map(Response::new)
            .ok_or_else(|| Status::not_found("command operation not found"))
    }

    async fn get_operation(
        &self,
        request: Request<GetOperationRequest>,
    ) -> Result<Response<Operation>, Status> {
        let req = request.into_inner();
        match self.ctx.state.get_operation(&req.operation_id) {
            Some(op) => Ok(Response::new(op)),
            None => Err(Status::not_found("operation not found")),
        }
    }

    type WatchOperationStream =
        tokio_stream::wrappers::ReceiverStream<Result<OperationEvent, Status>>;

    async fn watch_operation(
        &self,
        request: Request<WatchOperationRequest>,
    ) -> Result<Response<Self::WatchOperationStream>, Status> {
        let operation = self
            .ctx
            .state
            .get_operation(&request.into_inner().operation_id)
            .ok_or_else(|| Status::not_found("operation not found"))?;
        // Current execution is synchronous: publish the recorded terminal result.
        // Do not imply that a background executor exists for unfinished operations.
        if !matches!(
            operation.status.as_str(),
            "SUCCEEDED" | "FAILED" | "CANCELLED"
        ) {
            return Err(Status::unimplemented(
                "live operation progress is not implemented",
            ));
        }
        let (tx, rx) = tokio::sync::mpsc::channel(1);
        tx.send(Ok(OperationEvent {
            operation: Some(operation),
            log_line: String::new(),
        }))
        .await
        .map_err(|_| Status::internal("operation stream closed"))?;
        Ok(Response::new(tokio_stream::wrappers::ReceiverStream::new(
            rx,
        )))
    }

    async fn list_operations(
        &self,
        request: Request<ListOperationsRequest>,
    ) -> Result<Response<ListOperationsResponse>, Status> {
        let req = request.into_inner();
        let items = self.ctx.state.list_operations(
            &req.node_key,
            &req.profile_name,
            &req.status,
            req.limit,
        );
        Ok(Response::new(ListOperationsResponse { items }))
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let addr: SocketAddr = env::var("NODE_DRIVER_LISTEN_ADDR")
        .unwrap_or_else(|_| "127.0.0.1:50051".to_string())
        .parse()?;

    let ctx = DriverContext::from_env()?;
    let node_api = NodeApi { ctx: ctx.clone() };
    let provisioning_api = ProvisioningApi { ctx: ctx.clone() };
    let runtime_api = RuntimeApi { ctx: ctx.clone() };
    let operation_api = OperationApi { ctx };

    println!("node-plane-driver listening on {}", addr);

    Server::builder()
        .add_service(NodeServiceServer::new(node_api))
        .add_service(ProvisioningServiceServer::new(provisioning_api))
        .add_service(RuntimeServiceServer::new(runtime_api))
        .add_service(OperationServiceServer::new(operation_api))
        .serve(addr)
        .await?;

    Ok(())
}
