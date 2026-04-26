use std::env;
use std::fs;
use std::net::TcpListener;
use std::net::SocketAddr;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use chrono::Utc;
use serde::Deserialize;
use serde_json::Value;
use tonic::{Request, Response, Status, transport::Server};

pub mod agent {
    pub mod v1 {
        tonic::include_proto!("nodeplane.agent.v1");
    }
}

use agent::v1::node_agent_service_server::{NodeAgentService, NodeAgentServiceServer};
use agent::v1::{
    AgentEmpty, CheckPortsRequest, CheckPortsResponse, DiagnosticItem, ListRemoteProfilesRequest,
    ListRemoteProfilesResponse, LocalHealth, PortStatus, RemoteProfileRecord,
    RunDiagnosticsRequest, RunDiagnosticsResponse, RuntimeFacts, SyncNodeEnvRequest,
    SyncNodeEnvResponse, OpenPortsRequest, OpenPortsResponse, RuntimeFileSpec,
    SyncRuntimeFilesRequest, SyncRuntimeFilesResponse, SyncXrayRequest, SyncXrayResponse,
};

#[derive(Debug, Clone, Deserialize)]
struct AgentConfig {
    node_key: String,
    listen_addr: String,
    heartbeat_seconds: u64,
    runtime_root: String,
    state_dir: String,
    log_dir: String,
    node_env_path: String,
    xray_config_path: String,
    awg_config_path: String,
}

impl Default for AgentConfig {
    fn default() -> Self {
        let runtime_root = "/opt/node-plane-runtime".to_string();
        let xray_config_path = format!("{runtime_root}/xray/config.json");
        let awg_config_path = format!("{runtime_root}/amnezia-awg/data/wg0.conf");
        Self {
            node_key: env::var("NODE_AGENT_NODE_KEY")
                .ok()
                .filter(|value| !value.trim().is_empty())
                .unwrap_or_else(|| "unknown-node".to_string()),
            listen_addr: env::var("NODE_AGENT_LISTEN_ADDR")
                .ok()
                .filter(|value| !value.trim().is_empty())
                .unwrap_or_else(|| "127.0.0.1:50061".to_string()),
            heartbeat_seconds: env::var("NODE_AGENT_HEARTBEAT_SECONDS")
                .ok()
                .and_then(|value| value.parse::<u64>().ok())
                .filter(|value| *value > 0)
                .unwrap_or(30),
            runtime_root,
            state_dir: "/var/lib/node-plane-agent".to_string(),
            log_dir: "/var/log/node-plane-agent".to_string(),
            node_env_path: "/etc/node-plane/node.env".to_string(),
            xray_config_path,
            awg_config_path,
        }
    }
}

impl AgentConfig {
    fn load() -> Self {
        let path = env::var("NODE_AGENT_CONFIG_PATH")
            .ok()
            .filter(|value| !value.trim().is_empty())
            .unwrap_or_else(|| "/etc/node-plane/agent.toml".to_string());
        if !Path::new(&path).is_file() {
            return Self::default();
        }
        let Ok(raw) = fs::read_to_string(&path) else {
            return Self::default();
        };
        toml::from_str(&raw).unwrap_or_else(|_| Self::default())
    }
}

#[derive(Clone)]
struct AgentState {
    config: AgentConfig,
    last_seen_at: Arc<Mutex<String>>,
}

impl AgentState {
    fn new(config: AgentConfig) -> Self {
        Self {
            config,
            last_seen_at: Arc::new(Mutex::new(Utc::now().to_rfc3339())),
        }
    }

    fn mark_heartbeat(&self) {
        if let Ok(mut guard) = self.last_seen_at.lock() {
            *guard = Utc::now().to_rfc3339();
        }
    }

    fn last_seen_at(&self) -> String {
        self.last_seen_at
            .lock()
            .map(|guard| guard.clone())
            .unwrap_or_default()
    }

    fn read_first_line(path: &Path) -> String {
        let Ok(raw) = fs::read_to_string(path) else {
            return String::new();
        };
        raw.lines().next().unwrap_or("").trim().to_string()
    }

    fn runtime_version_path(&self) -> PathBuf {
        Path::new(&self.config.runtime_root).join("VERSION")
    }

    fn runtime_commit_path(&self) -> PathBuf {
        Path::new(&self.config.runtime_root).join("BUILD_COMMIT")
    }

    fn runtime_facts(&self) -> RuntimeFacts {
        let xray_config = Path::new(&self.config.xray_config_path);
        let awg_config = Path::new(&self.config.awg_config_path);
        RuntimeFacts {
            node_key: self.config.node_key.clone(),
            version: Self::read_first_line(&self.runtime_version_path()),
            commit: Self::read_first_line(&self.runtime_commit_path()),
            runtime_root: self.config.runtime_root.clone(),
            xray_config_path: self.config.xray_config_path.clone(),
            awg_config_path: self.config.awg_config_path.clone(),
            xray_config_present: xray_config.is_file(),
            awg_config_present: awg_config.is_file(),
        }
    }

    fn health(&self) -> LocalHealth {
        let runtime_root = Path::new(&self.config.runtime_root);
        let state = if runtime_root.is_dir() {
            "running"
        } else {
            "degraded"
        };
        let summary = if runtime_root.is_dir() {
            "runtime root present"
        } else {
            "runtime root missing"
        };
        LocalHealth {
            state: state.to_string(),
            summary: summary.to_string(),
            last_seen_at: self.last_seen_at(),
        }
    }

    fn awg_profiles(&self) -> Vec<RemoteProfileRecord> {
        let Ok(raw) = fs::read_to_string(&self.config.awg_config_path) else {
            return Vec::new();
        };
        raw.lines()
            .filter_map(|line| {
                let trimmed = line.trim();
                if !trimmed.starts_with('#') {
                    return None;
                }
                let name = trimmed.trim_start_matches('#').trim();
                if name.is_empty() {
                    return None;
                }
                Some(RemoteProfileRecord {
                    profile_name: name.to_string(),
                    protocol_kind: "awg".to_string(),
                    remote_id: name.to_string(),
                    status: "present".to_string(),
                })
            })
            .collect()
    }

    fn xray_profiles(&self) -> Vec<RemoteProfileRecord> {
        let Ok(raw) = fs::read_to_string(&self.config.xray_config_path) else {
            return Vec::new();
        };
        let Ok(payload) = serde_json::from_str::<Value>(&raw) else {
            return Vec::new();
        };
        let mut items = Vec::new();
        let Some(inbounds) = payload.get("inbounds").and_then(Value::as_array) else {
            return items;
        };
        for inbound in inbounds {
            let Some(clients) = inbound
                .get("settings")
                .and_then(|value| value.get("clients"))
                .and_then(Value::as_array)
            else {
                continue;
            };
            for client in clients {
                let profile_name = client
                    .get("email")
                    .and_then(Value::as_str)
                    .or_else(|| client.get("name").and_then(Value::as_str))
                    .unwrap_or("")
                    .trim()
                    .to_string();
                if profile_name.is_empty() {
                    continue;
                }
                let remote_id = client
                    .get("id")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim()
                    .to_string();
                items.push(RemoteProfileRecord {
                    profile_name,
                    protocol_kind: "xray".to_string(),
                    remote_id,
                    status: "present".to_string(),
                });
            }
        }
        items
    }

    fn diagnostics(&self) -> RunDiagnosticsResponse {
        let xray_exists = Path::new(&self.config.xray_config_path).is_file();
        let awg_exists = Path::new(&self.config.awg_config_path).is_file();
        let runtime_root_exists = Path::new(&self.config.runtime_root).is_dir();
        let version = Self::read_first_line(&self.runtime_version_path());
        let commit = Self::read_first_line(&self.runtime_commit_path());

        let items = vec![
            DiagnosticItem {
                kind: "runtime_root".to_string(),
                status: if runtime_root_exists { "ok" } else { "missing" }.to_string(),
                summary: self.config.runtime_root.clone(),
                detail: String::new(),
            },
            DiagnosticItem {
                kind: "xray_config".to_string(),
                status: if xray_exists { "ok" } else { "missing" }.to_string(),
                summary: self.config.xray_config_path.clone(),
                detail: String::new(),
            },
            DiagnosticItem {
                kind: "awg_config".to_string(),
                status: if awg_exists { "ok" } else { "missing" }.to_string(),
                summary: self.config.awg_config_path.clone(),
                detail: String::new(),
            },
            DiagnosticItem {
                kind: "runtime_version".to_string(),
                status: if version.is_empty() { "unknown" } else { "ok" }.to_string(),
                summary: if version.is_empty() {
                    "missing"
                } else {
                    &version
                }
                .to_string(),
                detail: if commit.is_empty() {
                    String::new()
                } else {
                    format!("commit={commit}")
                },
            },
        ];

        let summary = format!(
            "runtime_root={}, xray_config={}, awg_config={}, version={}",
            if runtime_root_exists {
                "present"
            } else {
                "missing"
            },
            if xray_exists { "present" } else { "missing" },
            if awg_exists { "present" } else { "missing" },
            if version.is_empty() {
                "unknown"
            } else {
                version.as_str()
            },
        );
        RunDiagnosticsResponse { summary, items }
    }

    fn check_ports(&self, request: CheckPortsRequest) -> CheckPortsResponse {
        let mut items = Vec::new();
        for spec in request.items {
            let port = spec.port;
            let kind = spec.kind.trim().to_string();
            if port == 0 || port > u16::MAX as u32 {
                items.push(PortStatus {
                    kind,
                    port,
                    status: "invalid".to_string(),
                    summary: "invalid port".to_string(),
                    detail: String::new(),
                });
                continue;
            }
            let bind_addr = format!("0.0.0.0:{port}");
            match TcpListener::bind(&bind_addr) {
                Ok(listener) => {
                    drop(listener);
                    items.push(PortStatus {
                        kind,
                        port,
                        status: "free".to_string(),
                        summary: format!("port {port} is free"),
                        detail: String::new(),
                    });
                }
                Err(err) => {
                    items.push(PortStatus {
                        kind,
                        port,
                        status: "busy".to_string(),
                        summary: format!("port {port} is not available"),
                        detail: err.to_string(),
                    });
                }
            }
        }
        let busy = items.iter().filter(|item| item.status == "busy").count();
        let invalid = items.iter().filter(|item| item.status == "invalid").count();
        let free = items.iter().filter(|item| item.status == "free").count();
        let summary = format!("checked={} free={} busy={} invalid={invalid}", items.len(), free, busy);
        CheckPortsResponse { summary, items }
    }

    fn sync_node_env(&self, content: &str) -> Result<SyncNodeEnvResponse, Status> {
        let path = Path::new(&self.config.node_env_path);
        let Some(parent) = path.parent() else {
            return Err(Status::internal("node env path has no parent directory"));
        };
        fs::create_dir_all(parent)
            .map_err(|err| Status::internal(format!("failed to create node env directory: {err}")))?;
        fs::write(path, content)
            .map_err(|err| Status::internal(format!("failed to write node env: {err}")))?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .map_err(|err| Status::internal(format!("failed to set node env permissions: {err}")))?;
        Ok(SyncNodeEnvResponse {
            summary: format!("node.env written to {}", self.config.node_env_path),
            path: self.config.node_env_path.clone(),
        })
    }

    fn write_runtime_file(&self, spec: &RuntimeFileSpec) -> Result<(), Status> {
        let resolved = self.resolve_runtime_path(&spec.path);
        let path = Path::new(&resolved);
        let Some(parent) = path.parent() else {
            return Err(Status::internal("runtime file path has no parent directory"));
        };
        fs::create_dir_all(parent)
            .map_err(|err| Status::internal(format!("failed to create runtime directory: {err}")))?;
        fs::write(path, &spec.content)
            .map_err(|err| Status::internal(format!("failed to write runtime file {}: {err}", resolved)))?;
        let mode = u32::from_str_radix(spec.mode.trim(), 8).unwrap_or(0o644);
        fs::set_permissions(path, fs::Permissions::from_mode(mode))
            .map_err(|err| Status::internal(format!("failed to set permissions on {}: {err}", resolved)))?;
        Ok(())
    }

    fn resolve_runtime_path(&self, path: &str) -> String {
        if path == "/etc/node-plane/node.env" {
            return self.config.node_env_path.clone();
        }
        if path == "/etc/node-plane/node.env.example" {
            return format!("{}.example", self.config.node_env_path);
        }
        if let Some(suffix) = path.strip_prefix("/opt/node-plane-runtime/") {
            return format!(
                "{}/{}",
                self.config.runtime_root.trim_end_matches('/'),
                suffix
            );
        }
        if path == "/opt/node-plane-runtime" {
            return self.config.runtime_root.clone();
        }
        path.to_string()
    }

    fn sync_runtime_files(
        &self,
        request: SyncRuntimeFilesRequest,
    ) -> Result<SyncRuntimeFilesResponse, Status> {
        let mut written_files = 0u32;
        for spec in request.files {
            self.write_runtime_file(&spec)?;
            written_files += 1;
        }
        Ok(SyncRuntimeFilesResponse {
            summary: format!("runtime files written: {written_files}"),
            written_files,
        })
    }

    fn sync_xray(&self, request: SyncXrayRequest) -> Result<SyncXrayResponse, Status> {
        let script_path = self.resolve_runtime_path("/opt/node-plane-runtime/sync-xray.sh");
        let config_path = self.resolve_runtime_path(&request.config_path);
        let output = Command::new(&script_path)
            .arg(&config_path)
            .arg(request.public_host)
            .arg(request.flow)
            .arg(request.image)
            .output()
            .map_err(|err| Status::internal(format!("failed to execute sync-xray.sh: {err}")))?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
            let detail = if !stderr.is_empty() { stderr } else { stdout };
            return Err(Status::failed_precondition(format!(
                "sync-xray.sh failed: {}",
                if detail.is_empty() { "unknown error".to_string() } else { detail }
            )));
        }
        let generated_json = String::from_utf8_lossy(&output.stdout).trim().to_string();
        Ok(SyncXrayResponse {
            summary: "xray settings synced".to_string(),
            generated_json,
        })
    }

    fn open_ports(&self, request: OpenPortsRequest) -> Result<OpenPortsResponse, Status> {
        let ufw_check = Command::new("ufw").arg("status").output();
        if ufw_check.is_err() {
            return Err(Status::failed_precondition(
                "ufw is not installed or not available in PATH",
            ));
        }

        let mut items = Vec::new();
        let mut failed = 0usize;
        for spec in request.items {
            let port = spec.port;
            let kind = spec.kind.trim().to_string();
            let proto = if kind == "awg" { "udp" } else { "tcp" };
            let rule = format!("{port}/{proto}");
            match Command::new("ufw").arg("allow").arg(&rule).output() {
                Ok(output) if output.status.success() => items.push(PortStatus {
                    kind,
                    port,
                    status: "opened".to_string(),
                    summary: format!("opened {rule}"),
                    detail: String::new(),
                }),
                Ok(output) => {
                    failed += 1;
                    let detail = String::from_utf8_lossy(&output.stderr).trim().to_string();
                    items.push(PortStatus {
                        kind,
                        port,
                        status: "failed".to_string(),
                        summary: format!("failed to open {rule}"),
                        detail,
                    });
                }
                Err(err) => {
                    failed += 1;
                    items.push(PortStatus {
                        kind,
                        port,
                        status: "failed".to_string(),
                        summary: format!("failed to open {rule}"),
                        detail: err.to_string(),
                    });
                }
            }
        }

        let _ = Command::new("ufw").arg("reload").output();
        let opened = items.iter().filter(|item| item.status == "opened").count();
        let summary = format!("requested={} opened={} failed={failed}", items.len(), opened);
        Ok(OpenPortsResponse { summary, items })
    }
}

#[derive(Clone)]
struct NodeAgentApi {
    state: AgentState,
}

#[tonic::async_trait]
impl NodeAgentService for NodeAgentApi {
    async fn get_runtime_facts(
        &self,
        _request: Request<AgentEmpty>,
    ) -> Result<Response<RuntimeFacts>, Status> {
        Ok(Response::new(self.state.runtime_facts()))
    }

    async fn get_node_health(
        &self,
        _request: Request<AgentEmpty>,
    ) -> Result<Response<LocalHealth>, Status> {
        Ok(Response::new(self.state.health()))
    }

    async fn list_remote_profiles(
        &self,
        request: Request<ListRemoteProfilesRequest>,
    ) -> Result<Response<ListRemoteProfilesResponse>, Status> {
        let protocol_kind = request.into_inner().protocol_kind.trim().to_lowercase();
        let mut items = Vec::new();
        if protocol_kind.is_empty() || protocol_kind == "awg" {
            items.extend(self.state.awg_profiles());
        }
        if protocol_kind.is_empty() || protocol_kind == "xray" {
            items.extend(self.state.xray_profiles());
        }
        Ok(Response::new(ListRemoteProfilesResponse { items }))
    }

    async fn run_diagnostics(
        &self,
        _request: Request<RunDiagnosticsRequest>,
    ) -> Result<Response<RunDiagnosticsResponse>, Status> {
        Ok(Response::new(self.state.diagnostics()))
    }

    async fn check_ports(
        &self,
        request: Request<CheckPortsRequest>,
    ) -> Result<Response<CheckPortsResponse>, Status> {
        Ok(Response::new(self.state.check_ports(request.into_inner())))
    }

    async fn sync_node_env(
        &self,
        request: Request<SyncNodeEnvRequest>,
    ) -> Result<Response<SyncNodeEnvResponse>, Status> {
        Ok(Response::new(
            self.state.sync_node_env(request.into_inner().content.as_str())?,
        ))
    }

    async fn open_ports(
        &self,
        request: Request<OpenPortsRequest>,
    ) -> Result<Response<OpenPortsResponse>, Status> {
        Ok(Response::new(self.state.open_ports(request.into_inner())?))
    }

    async fn sync_runtime_files(
        &self,
        request: Request<SyncRuntimeFilesRequest>,
    ) -> Result<Response<SyncRuntimeFilesResponse>, Status> {
        Ok(Response::new(
            self.state.sync_runtime_files(request.into_inner())?,
        ))
    }

    async fn sync_xray(
        &self,
        request: Request<SyncXrayRequest>,
    ) -> Result<Response<SyncXrayResponse>, Status> {
        Ok(Response::new(self.state.sync_xray(request.into_inner())?))
    }
}

fn print_startup(config: &AgentConfig) {
    println!("node-plane-agent starting");
    println!("node_key={}", config.node_key);
    println!("listen_addr={}", config.listen_addr);
    println!("runtime_root={}", config.runtime_root);
    println!("state_dir={}", config.state_dir);
    println!("log_dir={}", config.log_dir);
    println!("node_env_path={}", config.node_env_path);
    println!("xray_config_path={}", config.xray_config_path);
    println!("awg_config_path={}", config.awg_config_path);
    println!("heartbeat_seconds={}", config.heartbeat_seconds);
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let config = AgentConfig::load();
    let state = AgentState::new(config.clone());
    let api = NodeAgentApi {
        state: state.clone(),
    };
    let addr: SocketAddr = config.listen_addr.parse()?;
    print_startup(&config);

    let mut ticker = tokio::time::interval(Duration::from_secs(config.heartbeat_seconds));

    tokio::spawn(async move {
        loop {
            ticker.tick().await;
            state.mark_heartbeat();
            println!(
                "heartbeat node_key={} ts={}",
                state.config.node_key,
                Utc::now().to_rfc3339(),
            );
        }
    });

    Server::builder()
        .add_service(NodeAgentServiceServer::new(api))
        .serve_with_shutdown(addr, async {
            let _ = tokio::signal::ctrl_c().await;
            println!("node-plane-agent stopping");
        })
        .await?;

    Ok(())
}
