use std::collections::{HashMap, HashSet};
use std::env;
use std::fs;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use chrono::{Datelike, Utc};
use serde::Deserialize;
use tokio_postgres::{NoTls, Row};
use tonic::{Request, Response, Status, transport::Server};
use uuid::Uuid;

mod agent_transport;

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

use agent::v1::{PortCheckSpec, RuntimeFileSpec};
use driver::v1::node_service_server::{NodeService, NodeServiceServer};
use driver::v1::operation_service_server::{OperationService, OperationServiceServer};
use driver::v1::provisioning_service_server::{ProvisioningService, ProvisioningServiceServer};
use driver::v1::runtime_service_server::{RuntimeService, RuntimeServiceServer};
use driver::v1::telemetry_service_server::{TelemetryService, TelemetryServiceServer};
use driver::v1::{
    BootstrapNodeRequest, CheckPortsRequest, DeleteProfileFromNodeRequest, DeleteRuntimeRequest,
    FullCleanupNodeRequest, GetNodeDiagnosticsRequest, GetNodeDiagnosticsResponse, GetNodeRequest,
    GetOperationRequest, GetProfileUsageRequest, GetProfileUsageResponse, GetRuntimeStatusRequest,
    GetRuntimeStatusResponse, InstallDockerRequest, ListNodesNeedingRuntimeSyncRequest,
    ListNodesNeedingRuntimeSyncResponse, ListNodesRequest, ListNodesResponse,
    ListOperationsRequest, ListOperationsResponse, ListRemoteProfilesRequest,
    ListRemoteProfilesResponse, Node, NodeCapabilities, NodeHealth, NodeHealthEvent,
    OpenPortsRequest, Operation, OperationEvent, ProbeNodeRequest, ProfileSpec, ProfileUsage,
    ReconcileNodeRequest, ReconcileProfileRequest, ReinstallNodeRequest, RemoteProfileRecord,
    RuntimeStatus, ServiceStatus, StartOperationResponse, SyncNodeEnvRequest,
    SyncRuntimeRequest, SyncXrayRequest, WatchNodeHealthRequest, WatchOperationRequest,
};

#[derive(Clone, Default)]
struct DriverState {
    operations: Arc<Mutex<HashMap<String, Operation>>>,
}

impl DriverState {
    fn put_operation(&self, operation: Operation) -> StartOperationResponse {
        let operation_id = operation.operation_id.clone();
        self.operations
            .lock()
            .expect("operations lock poisoned")
            .insert(operation_id.clone(), operation);
        StartOperationResponse { operation_id }
    }

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
            result_json: String::new(),
        };
        self.put_operation(op)
    }

    fn finish_operation(
        &self,
        kind: &str,
        node_key: &str,
        profile_name: &str,
        status: &str,
        message: &str,
    ) -> StartOperationResponse {
        self.finish_operation_with_result(kind, node_key, profile_name, status, message, "")
    }

    fn finish_operation_with_result(
        &self,
        kind: &str,
        node_key: &str,
        profile_name: &str,
        status: &str,
        message: &str,
        result_json: &str,
    ) -> StartOperationResponse {
        let timestamp = Utc::now().to_rfc3339();
        self.put_operation(Operation {
            operation_id: Uuid::new_v4().to_string(),
            kind: kind.to_string(),
            status: status.to_string(),
            node_key: node_key.to_string(),
            profile_name: profile_name.to_string(),
            started_at: timestamp.clone(),
            updated_at: timestamp.clone(),
            finished_at: timestamp,
            progress_message: message.to_string(),
            error: None,
            result_json: result_json.to_string(),
        })
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
struct DriverContext {
    state: DriverState,
    postgres_dsn: Option<String>,
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
struct XraySyncGenerated {
    xray_host: String,
    xray_sni: String,
    xray_pbk: String,
    xray_sid: String,
    xray_short_id: String,
    xray_fp: String,
    xray_flow: String,
    xray_tcp_port: i32,
    xray_xhttp_port: i32,
    xray_xhttp_path_prefix: String,
}

impl DriverContext {
    fn runtime_assets_dir(&self) -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .join("runtime_assets")
    }

    fn runtime_manifest_path(&self) -> PathBuf {
        self.runtime_assets_dir().join("manifest.json")
    }

    fn load_runtime_manifest(&self) -> Result<Vec<RuntimeAssetManifestEntry>, Status> {
        let raw = fs::read_to_string(self.runtime_manifest_path())
            .map_err(|err| Status::internal(format!("failed to read runtime manifest: {err}")))?;
        serde_json::from_str::<Vec<RuntimeAssetManifestEntry>>(&raw)
            .map_err(|err| Status::internal(format!("failed to parse runtime manifest: {err}")))
    }

    fn from_env() -> Self {
        Self::load_runtime_env_file();
        let postgres_dsn = env::var("POSTGRES_DSN")
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .or_else(Self::derived_postgres_dsn);
        Self {
            state: DriverState::default(),
            postgres_dsn,
            app_semver: env::var("APP_SEMVER")
                .ok()
                .map(|value| value.trim().to_string())
                .filter(|value| !value.is_empty())
                .unwrap_or_else(|| "0.1.0".to_string()),
            app_commit: env::var("APP_COMMIT")
                .ok()
                .map(|value| value.trim().to_string())
                .filter(|value| !value.is_empty())
                .unwrap_or_else(|| "unknown".to_string()),
            agent_targets: Self::parse_agent_targets(),
        }
    }

    fn parse_agent_targets() -> HashMap<String, String> {
        env::var("NODE_AGENT_TARGETS")
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
            .collect()
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

    async fn mark_full_cleanup(
        &self,
        node_key: &str,
        notes: &str,
    ) -> Result<(), Status> {
        let client = self.db_client().await?;
        client
            .execute(
                "
                UPDATE servers
                SET notes = $1,
                    updated_at = $2
                WHERE key = $3
                ",
                &[&notes, &Utc::now().to_rfc3339(), &node_key],
            )
            .await
            .map_err(|err| Status::internal(format!("failed to mark full cleanup: {err}")))?;
        Ok(())
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

    fn derived_postgres_dsn() -> Option<String> {
        let db_name = env::var("NODE_PLANE_POSTGRES_DB")
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| "node_plane".to_string());
        let db_user = env::var("NODE_PLANE_POSTGRES_USER")
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| "node_plane".to_string());
        let db_password = env::var("NODE_PLANE_POSTGRES_PASSWORD")
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| "node_plane".to_string());
        let port = env::var("NODE_PLANE_POSTGRES_PORT")
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| "55432".to_string());
        Some(format!(
            "postgresql://{db_user}:{db_password}@127.0.0.1:{port}/{db_name}"
        ))
    }

    async fn db_client(&self) -> Result<tokio_postgres::Client, Status> {
        let dsn = self
            .postgres_dsn
            .as_deref()
            .ok_or_else(|| Status::failed_precondition("PostgreSQL DSN is not configured"))?;
        let (client, connection) = tokio_postgres::connect(dsn, NoTls)
            .await
            .map_err(|err| Status::unavailable(format!("failed to connect to postgres: {err}")))?;
        tokio::spawn(async move {
            if let Err(err) = connection.await {
                eprintln!("postgres connection error: {err}");
            }
        });
        Ok(client)
    }

    fn agent_target(&self, node_key: &str) -> Option<&str> {
        self.agent_targets.get(node_key).map(String::as_str)
    }

    fn runtime_state_from_values(&self, version: &str, commit: &str) -> String {
        let version_value = version.trim();
        let commit_value = commit.trim();
        if !commit_value.is_empty() && commit_value != "unknown" {
            if self.app_commit != "unknown" {
                return if commit_value == self.app_commit {
                    "up_to_date".to_string()
                } else {
                    "outdated".to_string()
                };
            }
            if !version_value.is_empty() {
                return if version_value == self.app_semver {
                    "up_to_date".to_string()
                } else {
                    "outdated".to_string()
                };
            }
            return "unknown".to_string();
        }
        if !version_value.is_empty() {
            return if version_value == self.app_semver {
                "up_to_date".to_string()
            } else {
                "outdated".to_string()
            };
        }
        "unknown".to_string()
    }

    fn parse_protocol_kinds(&self, value: &str) -> Vec<String> {
        value
            .split(',')
            .map(|item| item.trim().to_lowercase())
            .filter(|item| item == "awg" || item == "xray")
            .collect()
    }

    fn node_from_row(&self, row: &Row) -> Node {
        let protocol_kinds = self.parse_protocol_kinds(
            row.try_get::<_, Option<String>>("protocol_kinds")
                .ok()
                .flatten()
                .unwrap_or_default()
                .as_str(),
        );
        let enabled = row.try_get::<_, bool>("enabled").unwrap_or(true);
        let bootstrap_state = row
            .try_get::<_, Option<String>>("bootstrap_state")
            .ok()
            .flatten()
            .unwrap_or_else(|| "new".to_string());
        let connectivity = if !enabled {
            "disabled".to_string()
        } else if bootstrap_state == "bootstrapped" {
            "ready".to_string()
        } else {
            "degraded".to_string()
        };
        let summary = if !enabled {
            "server disabled in registry".to_string()
        } else if bootstrap_state == "bootstrapped" {
            "bootstrapped".to_string()
        } else {
            bootstrap_state.clone()
        };

        Node {
            node_key: row
                .try_get::<_, Option<String>>("key")
                .ok()
                .flatten()
                .unwrap_or_default(),
            transport: row
                .try_get::<_, Option<String>>("transport")
                .ok()
                .flatten()
                .unwrap_or_else(|| "ssh".to_string()),
            version: String::new(),
            state: bootstrap_state,
            title: row
                .try_get::<_, Option<String>>("title")
                .ok()
                .flatten()
                .unwrap_or_default(),
            flag: row
                .try_get::<_, Option<String>>("flag")
                .ok()
                .flatten()
                .unwrap_or_else(|| "🏳️".to_string()),
            region: row
                .try_get::<_, Option<String>>("region")
                .ok()
                .flatten()
                .unwrap_or_default(),
            public_host: row
                .try_get::<_, Option<String>>("public_host")
                .ok()
                .flatten()
                .unwrap_or_default(),
            capabilities: Some(NodeCapabilities {
                supports_awg: protocol_kinds.iter().any(|item| item == "awg"),
                supports_xray: protocol_kinds.iter().any(|item| item == "xray"),
                supports_telemetry: true,
                supports_bootstrap: true,
            }),
            health: Some(NodeHealth {
                connectivity,
                last_seen_at: row
                    .try_get::<_, Option<String>>("updated_at")
                    .ok()
                    .flatten()
                    .unwrap_or_default(),
                summary,
            }),
        }
    }

    fn apply_agent_health(&self, node: &mut Node, health: agent::v1::LocalHealth) {
        node.health = Some(NodeHealth {
            connectivity: health.state,
            last_seen_at: health.last_seen_at,
            summary: health.summary,
        });
    }

    fn parse_runtime_note(&self, note: &str) -> Option<(String, String)> {
        let prefix = "runtime synced to ";
        let trimmed = note.trim();
        let payload = trimmed.strip_prefix(prefix)?;
        let (version, commit) = payload.split_once("·")?;
        let version = version.trim().to_string();
        let commit = commit.trim().to_string();
        if version.is_empty() && commit.is_empty() {
            None
        } else {
            Some((version, commit))
        }
    }

    fn port_check_specs_from_row(&self, row: &Row) -> Vec<PortCheckSpec> {
        let mut items = Vec::new();
        for (kind, field) in [
            ("xray_tcp", "xray_tcp_port"),
            ("xray_xhttp", "xray_xhttp_port"),
            ("awg", "awg_port"),
        ] {
            let port = row
                .try_get::<_, Option<i32>>(field)
                .ok()
                .flatten()
                .unwrap_or_default();
            if port > 0 {
                items.push(PortCheckSpec {
                    kind: kind.to_string(),
                    port: port as u32,
                });
            }
        }
        items
    }

    fn default_port_check_specs(&self) -> Vec<PortCheckSpec> {
        vec![
            PortCheckSpec {
                kind: "xray_tcp".to_string(),
                port: 443,
            },
            PortCheckSpec {
                kind: "xray_xhttp".to_string(),
                port: 8443,
            },
            PortCheckSpec {
                kind: "awg".to_string(),
                port: 51820,
            },
        ]
    }

    fn row_string(&self, row: &Row, field: &str, default: &str) -> String {
        row.try_get::<_, Option<String>>(field)
            .ok()
            .flatten()
            .filter(|value| !value.trim().is_empty())
            .unwrap_or_else(|| default.to_string())
    }

    fn row_i32(&self, row: &Row, field: &str, default: i32) -> i32 {
        row.try_get::<_, Option<i32>>(field)
            .ok()
            .flatten()
            .unwrap_or(default)
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

    fn render_node_env_from_row(&self, row: &Row) -> String {
        let ssh_host = row
            .try_get::<_, Option<String>>("ssh_host")
            .ok()
            .flatten()
            .unwrap_or_default();
        let public_host = self.row_string(row, "public_host", ssh_host.as_str());
        let awg_iface = self.row_string(row, "awg_iface", "wg0");
        let awg_public_host = self.row_string(row, "awg_public_host", public_host.as_str());
        let lines = vec![
            self.shell_env_assignment("SERVER_KEY", self.row_string(row, "key", "").as_str()),
            self.shell_env_assignment(
                "XRAY_CONFIG",
                self.row_string(
                    row,
                    "xray_config_path",
                    "/opt/node-plane-runtime/xray/config.json",
                )
                .as_str(),
            ),
            self.shell_env_assignment(
                "XRAY_CONTAINER_NAME",
                self.row_string(row, "xray_service_name", "xray").as_str(),
            ),
            self.shell_env_assignment("XRAY_DOCKER_DIR", "/opt/node-plane-runtime/xray"),
            self.shell_env_assignment("XRAY_DOCKER_IMAGE", "ghcr.io/xtls/xray-core:25.12.8"),
            self.shell_env_assignment("XRAY_INBOUND_TCP_TAG", "reality-tcp"),
            self.shell_env_assignment("XRAY_INBOUND_XHTTP_TAG", "reality-xhttp"),
            self.shell_env_assignment("AWG_CONTAINER_NAME", "amnezia-awg"),
            self.shell_env_assignment("AWG_DOCKER_DIR", "/opt/node-plane-runtime/amnezia-awg"),
            self.shell_env_assignment("AWG_DOCKER_IMAGE", "node-plane-amnezia-awg:0.2.16"),
            self.shell_env_assignment("AWG_IFACE", awg_iface.as_str()),
            self.shell_env_assignment(
                "AWG_CONFIG",
                format!("/opt/node-plane-runtime/amnezia-awg/data/{awg_iface}.conf"),
            ),
            self.shell_env_assignment("AWG_SERVER_ADDRESS", "10.8.1.0/24"),
            self.shell_env_assignment("AWG_NETWORK", "10.8.1.0/24"),
            self.shell_env_assignment("AWG_DNS", "1.1.1.1"),
            self.shell_env_assignment("AWG_MTU", "1280"),
            self.shell_env_assignment("AWG_ALLOWED_IPS", "0.0.0.0/0"),
            self.shell_env_assignment("AWG_KEEPALIVE", "25"),
            self.shell_env_assignment(
                "AWG_I1_PRESET",
                self.row_string(row, "awg_i1_preset", "quic").as_str(),
            ),
            self.shell_env_assignment("AWG_SERVER_IP", awg_public_host.as_str()),
            self.shell_env_assignment("AWG_SERVER_PORT", self.row_i32(row, "awg_port", 51820)),
        ];
        format!("{}\n", lines.join("\n"))
    }

    fn render_default_node_env(&self, node_key: &str) -> String {
        let lines = vec![
            self.shell_env_assignment("SERVER_KEY", node_key),
            self.shell_env_assignment("XRAY_CONFIG", "/opt/node-plane-runtime/xray/config.json"),
            self.shell_env_assignment("XRAY_CONTAINER_NAME", "xray"),
            self.shell_env_assignment("XRAY_DOCKER_DIR", "/opt/node-plane-runtime/xray"),
            self.shell_env_assignment("XRAY_DOCKER_IMAGE", "ghcr.io/xtls/xray-core:25.12.8"),
            self.shell_env_assignment("XRAY_INBOUND_TCP_TAG", "reality-tcp"),
            self.shell_env_assignment("XRAY_INBOUND_XHTTP_TAG", "reality-xhttp"),
            self.shell_env_assignment("AWG_CONTAINER_NAME", "amnezia-awg"),
            self.shell_env_assignment("AWG_DOCKER_DIR", "/opt/node-plane-runtime/amnezia-awg"),
            self.shell_env_assignment("AWG_DOCKER_IMAGE", "node-plane-amnezia-awg:0.2.16"),
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

    fn runtime_file_bundle(
        &self,
        row: Option<&Row>,
        node_key: &str,
    ) -> Result<Vec<RuntimeFileSpec>, Status> {
        let manifest = self.load_runtime_manifest()?;
        let assets_dir = self.runtime_assets_dir();
        let mut files = Vec::new();
        for entry in manifest {
            let content = fs::read_to_string(assets_dir.join(&entry.asset_path)).map_err(|err| {
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
        let node_env = match row {
            Some(value) => self.render_node_env_from_row(value),
            None => self.render_default_node_env(node_key),
        };
        files.push(RuntimeFileSpec {
            path: "/etc/node-plane/node.env".to_string(),
            content: node_env,
            mode: "0600".to_string(),
        });
        Ok(files)
    }

    fn runtime_status_from_row(&self, row: &Row) -> RuntimeStatus {
        let node_key = row
            .try_get::<_, Option<String>>("key")
            .ok()
            .flatten()
            .unwrap_or_default();
        let bootstrap_state = row
            .try_get::<_, Option<String>>("bootstrap_state")
            .ok()
            .flatten()
            .unwrap_or_else(|| "new".to_string());
        let note = row
            .try_get::<_, Option<String>>("notes")
            .ok()
            .flatten()
            .unwrap_or_default();

        if bootstrap_state != "bootstrapped" {
            return RuntimeStatus {
                node_key,
                state: "not_bootstrapped".to_string(),
                version: String::new(),
                commit: String::new(),
                expected_version: self.app_semver.clone(),
                expected_commit: self.app_commit.clone(),
                message: "bootstrap required".to_string(),
            };
        }

        if let Some((version, commit)) = self.parse_runtime_note(&note) {
            return RuntimeStatus {
                node_key,
                state: self.runtime_state_from_values(&version, &commit),
                version,
                commit,
                expected_version: self.app_semver.clone(),
                expected_commit: self.app_commit.clone(),
                message: "derived from last sync note; remote runtime metadata is not reported yet"
                    .to_string(),
            };
        }

        RuntimeStatus {
            node_key,
            state: "unknown".to_string(),
            version: String::new(),
            commit: String::new(),
            expected_version: self.app_semver.clone(),
            expected_commit: self.app_commit.clone(),
            message: if note.trim().is_empty() {
                "runtime metadata is not reported by the driver yet".to_string()
            } else {
                note
            },
        }
    }

    async fn fetch_server_row(&self, node_key: &str) -> Result<Option<Row>, Status> {
        let client = self.db_client().await?;
        client
            .query_opt("SELECT * FROM servers WHERE key = $1", &[&node_key])
            .await
            .map_err(|err| Status::internal(format!("failed to query servers: {err}")))
    }

    async fn list_server_rows(&self, include_disabled: bool) -> Result<Vec<Row>, Status> {
        let client = self.db_client().await?;
        let sql = if include_disabled {
            "SELECT * FROM servers ORDER BY title, key"
        } else {
            "SELECT * FROM servers WHERE enabled = TRUE ORDER BY title, key"
        };
        client
            .query(sql, &[])
            .await
            .map_err(|err| Status::internal(format!("failed to list servers: {err}")))
    }

    async fn profile_usage_rows(
        &self,
        profile_name: &str,
        protocol_kind: &str,
    ) -> Result<Vec<Row>, Status> {
        let client = self.db_client().await?;
        let now = Utc::now();
        let month_start = format!("{:04}-{:02}-01T00:00:00+00:00", now.year(), now.month());
        client
            .query(
                "
                WITH scoped AS (
                    SELECT
                        server_key,
                        remote_id,
                        rx_bytes_total,
                        tx_bytes_total,
                        sampled_at,
                        ctid
                    FROM traffic_samples
                    WHERE profile_name = $1
                      AND protocol_kind = $2
                      AND sampled_at >= $3
                ),
                ranked AS (
                    SELECT
                        server_key,
                        remote_id,
                        rx_bytes_total,
                        tx_bytes_total,
                        sampled_at,
                        COUNT(*) OVER (PARTITION BY server_key, remote_id) AS sample_count,
                        ROW_NUMBER() OVER (
                            PARTITION BY server_key, remote_id
                            ORDER BY sampled_at ASC, ctid ASC
                        ) AS rn_first,
                        ROW_NUMBER() OVER (
                            PARTITION BY server_key, remote_id
                            ORDER BY sampled_at DESC, ctid DESC
                        ) AS rn_last
                    FROM scoped
                )
                SELECT
                    first.server_key,
                    first.remote_id,
                    first.sample_count,
                    first.rx_bytes_total AS rx_first,
                    last.rx_bytes_total AS rx_last,
                    first.tx_bytes_total AS tx_first,
                    last.tx_bytes_total AS tx_last
                FROM ranked first
                JOIN ranked last
                  ON last.server_key = first.server_key
                 AND last.remote_id = first.remote_id
                WHERE first.rn_first = 1
                  AND last.rn_last = 1
                ORDER BY first.server_key, first.remote_id
                ",
                &[&profile_name, &protocol_kind, &month_start],
            )
            .await
            .map_err(|err| Status::internal(format!("failed to query traffic usage: {err}")))
    }

    async fn observed_remote_profiles(
        &self,
        node_key: &str,
        protocol_kind: &str,
    ) -> Result<Vec<Row>, Status> {
        let client = self.db_client().await?;
        if protocol_kind.is_empty() {
            client
                .query(
                    "
                    SELECT profile_name, protocol_kind, remote_id, status
                    FROM profile_server_state
                    WHERE server_key = $1
                    ORDER BY profile_name, protocol_kind
                    ",
                    &[&node_key],
                )
                .await
                .map_err(|err| {
                    Status::internal(format!("failed to query profile_server_state: {err}"))
                })
        } else {
            client
                .query(
                    "
                    SELECT profile_name, protocol_kind, remote_id, status
                    FROM profile_server_state
                    WHERE server_key = $1
                      AND protocol_kind = $2
                    ORDER BY profile_name, protocol_kind
                    ",
                    &[&node_key, &protocol_kind],
                )
                .await
                .map_err(|err| {
                    Status::internal(format!("failed to query profile_server_state: {err}"))
                })
        }
    }

    async fn update_xray_server_fields(
        &self,
        node_key: &str,
        generated: &XraySyncGenerated,
    ) -> Result<(), Status> {
        let client = self.db_client().await?;
        client
            .execute(
                "
                UPDATE servers
                SET
                    xray_host = $1,
                    xray_sni = $2,
                    xray_pbk = $3,
                    xray_sid = $4,
                    xray_short_id = $5,
                    xray_fp = $6,
                    xray_flow = $7,
                    xray_tcp_port = $8,
                    xray_xhttp_port = $9,
                    xray_xhttp_path_prefix = $10,
                    updated_at = $11
                WHERE key = $12
                ",
                &[
                    &generated.xray_host,
                    &generated.xray_sni,
                    &generated.xray_pbk,
                    &generated.xray_sid,
                    &generated.xray_short_id,
                    &generated.xray_fp,
                    &generated.xray_flow,
                    &generated.xray_tcp_port,
                    &generated.xray_xhttp_port,
                    &generated.xray_xhttp_path_prefix,
                    &Utc::now().to_rfc3339(),
                    &node_key,
                ],
            )
            .await
            .map_err(|err| Status::internal(format!("failed to update xray server fields: {err}")))?;
        Ok(())
    }

    async fn upsert_profile_server_state(
        &self,
        profile_name: &str,
        node_key: &str,
        protocol_kind: &str,
        status: &str,
        remote_id: &str,
        last_error: &str,
    ) -> Result<(), Status> {
        let client = self.db_client().await?;
        let now = Utc::now().to_rfc3339();
        client
            .execute(
                "
                INSERT INTO profile_server_state(
                    profile_name, server_key, protocol_kind, desired_enabled, status,
                    remote_id, last_error, created_at, updated_at
                ) VALUES ($1, $2, $3, TRUE, $4, $5, $6, $7, $7)
                ON CONFLICT(profile_name, server_key, protocol_kind) DO UPDATE SET
                    desired_enabled = excluded.desired_enabled,
                    status = excluded.status,
                    remote_id = excluded.remote_id,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                ",
                &[
                    &profile_name,
                    &node_key,
                    &protocol_kind,
                    &status,
                    &remote_id,
                    &last_error,
                    &now,
                ],
            )
            .await
            .map_err(|err| Status::internal(format!("failed to upsert profile server state: {err}")))?;
        Ok(())
    }

    async fn delete_profile_server_state(
        &self,
        profile_name: &str,
        node_key: &str,
        protocol_kind: &str,
    ) -> Result<(), Status> {
        let client = self.db_client().await?;
        client
            .execute(
                "
                DELETE FROM profile_server_state
                WHERE profile_name = $1
                  AND server_key = $2
                  AND protocol_kind = $3
                ",
                &[&profile_name, &node_key, &protocol_kind],
            )
            .await
            .map_err(|err| Status::internal(format!("failed to delete profile server state: {err}")))?;
        Ok(())
    }

    async fn mark_runtime_deleted(
        &self,
        node_key: &str,
        preserve_config: bool,
    ) -> Result<(), Status> {
        let client = self.db_client().await?;
        let bootstrap_state = if preserve_config { "edited" } else { "new" };
        let notes = if preserve_config {
            "runtime removed; config preserved"
        } else {
            "runtime removed; config wiped"
        };
        if preserve_config {
            client
                .execute(
                    "
                    UPDATE servers
                    SET bootstrap_state = $1,
                        notes = $2,
                        updated_at = $3
                    WHERE key = $4
                    ",
                    &[&bootstrap_state, &notes, &Utc::now().to_rfc3339(), &node_key],
                )
                .await
                .map_err(|err| Status::internal(format!("failed to mark runtime deleted: {err}")))?;
        } else {
            let empty = "";
            client
                .execute(
                    "
                    UPDATE servers
                    SET bootstrap_state = $1,
                        notes = $2,
                        xray_pbk = $3,
                        xray_short_id = $4,
                        updated_at = $5
                    WHERE key = $6
                    ",
                    &[
                        &bootstrap_state,
                        &notes,
                        &empty,
                        &empty,
                        &Utc::now().to_rfc3339(),
                        &node_key,
                    ],
                )
                .await
                .map_err(|err| Status::internal(format!("failed to mark runtime deleted: {err}")))?;
        }
        Ok(())
    }

    async fn mark_bootstrap_state(
        &self,
        node_key: &str,
        state: &str,
        notes: &str,
    ) -> Result<(), Status> {
        let client = self.db_client().await?;
        client
            .execute(
                "
                UPDATE servers
                SET bootstrap_state = $1,
                    notes = $2,
                    updated_at = $3
                WHERE key = $4
                ",
                &[&state, &notes, &Utc::now().to_rfc3339(), &node_key],
            )
            .await
            .map_err(|err| Status::internal(format!("failed to update bootstrap state: {err}")))?;
        Ok(())
    }
}

#[derive(Clone)]
struct NodeApi {
    ctx: DriverContext,
}

#[derive(Clone)]
struct ProvisioningApi {
    ctx: DriverContext,
}

impl ProvisioningApi {
    fn access_codes_for_protocol(node_key: &str, protocol_kind: &str) -> Vec<String> {
        let node_key = node_key.trim();
        match protocol_kind {
            "xray" => {
                if node_key == "de" {
                    vec!["gx".to_string(), "xray_de".to_string()]
                } else {
                    vec![format!("xray_{node_key}")]
                }
            }
            "awg" => {
                if node_key == "de" {
                    vec!["ga".to_string(), "awg_de".to_string()]
                } else if node_key == "lv" {
                    vec!["la".to_string(), "awg_lv".to_string()]
                } else {
                    vec![format!("awg_{node_key}")]
                }
            }
            _ => Vec::new(),
        }
    }

    fn matches_access_code(node_key: &str, protocol_kind: &str, access_code: &str) -> bool {
        let normalized = access_code.trim().to_lowercase();
        Self::access_codes_for_protocol(node_key, protocol_kind)
            .iter()
            .any(|code| code == &normalized)
    }

    async fn desired_xray_profiles_for_node(
        &self,
        node_key: &str,
    ) -> Result<Vec<(String, String)>, Status> {
        let client = self.ctx.db_client().await?;
        let rows = client
            .query(
                "
                SELECT p.name, pam.access_code, xp.uuid
                FROM profiles p
                JOIN profile_access_methods pam
                  ON pam.profile_name = p.name
                LEFT JOIN xray_profiles xp
                  ON xp.profile_name = p.name
                ORDER BY p.name
                ",
                &[],
            )
            .await
            .map_err(|err| Status::internal(format!("failed to query desired xray profiles: {err}")))?;

        let mut seen = HashSet::new();
        let mut out = Vec::new();
        for row in rows {
            let name = row
                .try_get::<_, Option<String>>("name")
                .ok()
                .flatten()
                .unwrap_or_default()
                .trim()
                .to_string();
            let access_code = row
                .try_get::<_, Option<String>>("access_code")
                .ok()
                .flatten()
                .unwrap_or_default();
            let uuid = row
                .try_get::<_, Option<String>>("uuid")
                .ok()
                .flatten()
                .unwrap_or_default()
                .trim()
                .to_string();
            if name.is_empty() || !Self::matches_access_code(node_key, "xray", &access_code) {
                continue;
            }
            if seen.insert(name.clone()) {
                out.push((name, uuid));
            }
        }
        Ok(out)
    }

    async fn desired_awg_profiles_for_node(&self, node_key: &str) -> Result<Vec<String>, Status> {
        let client = self.ctx.db_client().await?;
        let rows = client
            .query(
                "
                SELECT p.name, pam.access_code
                FROM profiles p
                JOIN profile_access_methods pam
                  ON pam.profile_name = p.name
                ORDER BY p.name
                ",
                &[],
            )
            .await
            .map_err(|err| Status::internal(format!("failed to query desired awg profiles: {err}")))?;

        let mut seen = HashSet::new();
        let mut out = Vec::new();
        for row in rows {
            let name = row
                .try_get::<_, Option<String>>("name")
                .ok()
                .flatten()
                .unwrap_or_default()
                .trim()
                .to_string();
            let access_code = row
                .try_get::<_, Option<String>>("access_code")
                .ok()
                .flatten()
                .unwrap_or_default();
            if name.is_empty() || !Self::matches_access_code(node_key, "awg", &access_code) {
                continue;
            }
            if seen.insert(name.clone()) {
                out.push(name);
            }
        }
        Ok(out)
    }

    async fn reconcile_xray_on_node(&self, node_key: &str, target: &str) -> Result<(i32, String), Status> {
        let transport = agent_transport::AgentTransport::new(target);
        let remote_records = transport
            .list_remote_profiles("xray")
            .await
            .map_err(|err| Status::unavailable(format!("failed to list remote xray profiles: {err}")))?;
        let desired = self.desired_xray_profiles_for_node(node_key).await?;

        let remote_by_name: HashMap<String, String> = remote_records
            .into_iter()
            .filter_map(|item| {
                let name = item.profile_name.trim().to_string();
                if name.is_empty() {
                    None
                } else {
                    Some((name, item.remote_id.trim().to_string()))
                }
            })
            .collect();

        let mut desired_names = HashSet::new();
        let mut ready = 0;
        let mut attention = 0;
        let mut failed = 0;

        for (profile_name, uuid) in desired {
            desired_names.insert(profile_name.clone());
            if uuid.is_empty() {
                self.ctx
                    .upsert_profile_server_state(
                        &profile_name,
                        node_key,
                        "xray",
                        "failed",
                        "",
                        "uuid missing in database",
                    )
                    .await?;
                failed += 1;
                continue;
            }

            let Some(remote_uuid) = remote_by_name.get(&profile_name) else {
                self.ctx
                    .upsert_profile_server_state(
                        &profile_name,
                        node_key,
                        "xray",
                        "failed",
                        &uuid,
                        "missing on server",
                    )
                    .await?;
                failed += 1;
                continue;
            };

            if !remote_uuid.is_empty() && remote_uuid != &uuid {
                self.ctx
                    .upsert_profile_server_state(
                        &profile_name,
                        node_key,
                        "xray",
                        "needs_attention",
                        remote_uuid,
                        &format!("uuid mismatch: db={uuid} remote={remote_uuid}"),
                    )
                    .await?;
                attention += 1;
                continue;
            }

            self.ctx
                .upsert_profile_server_state(&profile_name, node_key, "xray", "provisioned", &uuid, "")
                .await?;
            ready += 1;
        }

        let existing_rows = self.ctx.observed_remote_profiles(node_key, "xray").await?;
        for row in existing_rows {
            let profile_name = row
                .try_get::<_, Option<String>>("profile_name")
                .ok()
                .flatten()
                .unwrap_or_default()
                .trim()
                .to_string();
            if !profile_name.is_empty() && !desired_names.contains(&profile_name) {
                self.ctx
                    .delete_profile_server_state(&profile_name, node_key, "xray")
                    .await?;
            }
        }

        let mut extra_remote: Vec<String> = remote_by_name
            .keys()
            .filter(|name| !desired_names.contains(*name))
            .cloned()
            .collect();
        extra_remote.sort();
        let mut lines = vec![
            format!("server: {node_key}"),
            format!("ready: {ready}"),
            format!("attention: {attention}"),
            format!("failed: {failed}"),
            format!("remote_only: {}", extra_remote.len()),
        ];
        if !extra_remote.is_empty() {
            lines.push(format!(
                "remote extra profiles: {}",
                extra_remote.into_iter().take(20).collect::<Vec<String>>().join(", ")
            ));
        }
        Ok((0, lines.join("\n")))
    }

    async fn reconcile_awg_on_node(&self, node_key: &str, target: &str) -> Result<(i32, String), Status> {
        let transport = agent_transport::AgentTransport::new(target);
        let remote_records = transport
            .list_remote_profiles("awg")
            .await
            .map_err(|err| Status::unavailable(format!("failed to list remote awg profiles: {err}")))?;
        let desired = self.desired_awg_profiles_for_node(node_key).await?;
        let remote_names: HashSet<String> = remote_records
            .into_iter()
            .map(|item| item.profile_name.trim().to_string())
            .filter(|name| !name.is_empty())
            .collect();

        let mut desired_names = HashSet::new();
        let mut ready = 0;
        let mut failed = 0;

        for profile_name in desired {
            desired_names.insert(profile_name.clone());
            if remote_names.contains(&profile_name) {
                self.ctx
                    .upsert_profile_server_state(&profile_name, node_key, "awg", "provisioned", "", "")
                    .await?;
                ready += 1;
            } else {
                self.ctx
                    .upsert_profile_server_state(
                        &profile_name,
                        node_key,
                        "awg",
                        "failed",
                        "",
                        "missing in awg config",
                    )
                    .await?;
                failed += 1;
            }
        }

        let existing_rows = self.ctx.observed_remote_profiles(node_key, "awg").await?;
        for row in existing_rows {
            let profile_name = row
                .try_get::<_, Option<String>>("profile_name")
                .ok()
                .flatten()
                .unwrap_or_default()
                .trim()
                .to_string();
            if !profile_name.is_empty() && !desired_names.contains(&profile_name) {
                self.ctx
                    .delete_profile_server_state(&profile_name, node_key, "awg")
                    .await?;
            }
        }

        let mut extra_remote: Vec<String> = remote_names
            .into_iter()
            .filter(|name| !desired_names.contains(name))
            .collect();
        extra_remote.sort();
        let mut lines = vec![
            format!("server: {node_key}"),
            format!("ready: {ready}"),
            format!("failed: {failed}"),
            format!("remote_only: {}", extra_remote.len()),
        ];
        if !extra_remote.is_empty() {
            lines.push(format!(
                "remote extra profiles: {}",
                extra_remote.into_iter().take(20).collect::<Vec<String>>().join(", ")
            ));
        }
        Ok((0, lines.join("\n")))
    }
}

#[derive(Clone)]
struct RuntimeApi {
    ctx: DriverContext,
}

#[derive(Clone)]
struct TelemetryApi {
    ctx: DriverContext,
}

#[derive(Clone)]
struct OperationApi {
    ctx: DriverContext,
}

fn missing_runtime_status(node_key: &str, app_semver: &str, app_commit: &str) -> RuntimeStatus {
    RuntimeStatus {
        node_key: node_key.to_string(),
        state: "missing_server".to_string(),
        version: String::new(),
        commit: String::new(),
        expected_version: app_semver.to_string(),
        expected_commit: app_commit.to_string(),
        message: format!("server {node_key} not found"),
    }
}

#[tonic::async_trait]
impl NodeService for NodeApi {
    async fn get_node(&self, request: Request<GetNodeRequest>) -> Result<Response<Node>, Status> {
        let req = request.into_inner();
        if req.node_key.trim().is_empty() {
            return Err(Status::invalid_argument("node_key is required"));
        }
        match self.ctx.fetch_server_row(&req.node_key).await? {
            Some(row) => {
                let mut node = self.ctx.node_from_row(&row);
                if let Some(target) = self.ctx.agent_target(&req.node_key) {
                    let transport = agent_transport::AgentTransport::new(target);
                    if let Ok(health) = transport.get_node_health().await {
                        self.ctx.apply_agent_health(&mut node, health);
                    }
                }
                Ok(Response::new(node))
            }
            None => Err(Status::not_found("node not found")),
        }
    }

    async fn list_nodes(
        &self,
        request: Request<ListNodesRequest>,
    ) -> Result<Response<ListNodesResponse>, Status> {
        let req = request.into_inner();
        let rows = self.ctx.list_server_rows(req.include_disabled).await?;
        let mut items = Vec::with_capacity(rows.len());
        for row in rows {
            let node_key = row
                .try_get::<_, Option<String>>("key")
                .ok()
                .flatten()
                .unwrap_or_default();
            let mut node = self.ctx.node_from_row(&row);
            if let Some(target) = self.ctx.agent_target(&node_key) {
                let transport = agent_transport::AgentTransport::new(target);
                if let Ok(health) = transport.get_node_health().await {
                    self.ctx.apply_agent_health(&mut node, health);
                }
            }
            items.push(node);
        }
        Ok(Response::new(ListNodesResponse { items }))
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
        }
        let summary = match self.ctx.fetch_server_row(&req.node_key).await? {
            Some(row) => {
                let note = row
                    .try_get::<_, Option<String>>("notes")
                    .ok()
                    .flatten()
                    .unwrap_or_default();
                if note.trim().is_empty() {
                    "diagnostics are not implemented yet".to_string()
                } else {
                    format!("latest server note: {note}")
                }
            }
            None => "server not found".to_string(),
        };
        Ok(Response::new(GetNodeDiagnosticsResponse {
            node_key: req.node_key,
            summary,
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
        if let Some(target) = self.ctx.agent_target(&req.node_key) {
            let content = match self.ctx.fetch_server_row(&req.node_key).await {
                Ok(Some(row)) => self.ctx.render_node_env_from_row(&row),
                _ => self.ctx.render_default_node_env(&req.node_key),
            };
            let transport = agent_transport::AgentTransport::new(target);
            let summary = match transport.sync_node_env(content.as_str()).await {
                Ok(result) => result.summary,
                Err(err) => format!("agent node env sync failed: {err}"),
            };
            let status = if summary.starts_with("agent node env sync failed:") {
                "FAILED"
            } else {
                "SUCCEEDED"
            };
            return Ok(Response::new(self.ctx.state.finish_operation(
                "sync_node_env",
                &req.node_key,
                "",
                status,
                &summary,
            )));
        }
        Ok(Response::new(self.ctx.state.start_operation(
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
        if let Some(target) = self.ctx.agent_target(&req.node_key) {
            let transport = agent_transport::AgentTransport::new(target);
            let summary = match (
                transport.get_node_health().await,
                transport.run_diagnostics().await,
            ) {
                (Ok(health), Ok(diagnostics)) => format!(
                    "agent probe ok\nhealth={} ({})\n{}",
                    health.state, health.summary, diagnostics.summary
                ),
                (Ok(health), Err(err)) => format!(
                    "agent health ok\nhealth={} ({})\ndiagnostics_error={}",
                    health.state, health.summary, err
                ),
                (Err(err), _) => format!("agent probe failed: {err}"),
            };
            let status = if summary.starts_with("agent probe failed:") {
                "FAILED"
            } else {
                "SUCCEEDED"
            };
            return Ok(Response::new(self.ctx.state.finish_operation(
                "probe_node",
                &req.node_key,
                "",
                status,
                &summary,
            )));
        }
        Ok(Response::new(self.ctx.state.start_operation(
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
        if let Some(target) = self.ctx.agent_target(&req.node_key) {
            let specs = match self.ctx.fetch_server_row(&req.node_key).await {
                Ok(Some(row)) => self.ctx.port_check_specs_from_row(&row),
                _ => self.ctx.default_port_check_specs(),
            };
            let transport = agent_transport::AgentTransport::new(target);
            let response = transport.check_ports(specs).await;
            let summary = match response {
                Ok(result) => {
                    let busy: Vec<String> = result
                        .items
                        .iter()
                        .filter(|item| item.status == "busy")
                        .map(|item| format!("{}={}", item.kind, item.port))
                        .collect();
                    if busy.is_empty() {
                        format!("agent port check ok\n{}", result.summary)
                    } else {
                        format!(
                            "agent port check found busy ports\n{}\nbusy={}",
                            result.summary,
                            busy.join(", ")
                        )
                    }
                }
                Err(err) => format!("agent port check failed: {err}"),
            };
            let status = if summary.starts_with("agent port check failed:")
                || summary.starts_with("agent port check found busy ports")
            {
                "FAILED"
            } else {
                "SUCCEEDED"
            };
            return Ok(Response::new(self.ctx.state.finish_operation(
                "check_ports",
                &req.node_key,
                "",
                status,
                &summary,
            )));
        }
        Ok(Response::new(self.ctx.state.start_operation(
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
        if let Some(target) = self.ctx.agent_target(&req.node_key) {
            let specs = match self.ctx.fetch_server_row(&req.node_key).await {
                Ok(Some(row)) => self.ctx.port_check_specs_from_row(&row),
                _ => self.ctx.default_port_check_specs(),
            };
            let transport = agent_transport::AgentTransport::new(target);
            let summary = match transport.open_ports(specs).await {
                Ok(result) => {
                    let failed: Vec<String> = result
                        .items
                        .iter()
                        .filter(|item| item.status == "failed")
                        .map(|item| format!("{}={}: {}", item.kind, item.port, item.detail))
                        .collect();
                    if failed.is_empty() {
                        format!("agent open ports ok\n{}", result.summary)
                    } else {
                        format!(
                            "agent open ports failed for some rules\n{}\nerrors={}",
                            result.summary,
                            failed.join(" | ")
                        )
                    }
                }
                Err(err) => format!("agent open ports failed: {err}"),
            };
            let status = if summary.starts_with("agent open ports ok\n") {
                "SUCCEEDED"
            } else {
                "FAILED"
            };
            return Ok(Response::new(self.ctx.state.finish_operation(
                "open_ports",
                &req.node_key,
                "",
                status,
                &summary,
            )));
        }
        Ok(Response::new(self.ctx.state.start_operation(
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
        if let Some(target) = self.ctx.agent_target(&req.node_key) {
            let transport = agent_transport::AgentTransport::new(target);
            let summary = match transport.install_docker().await {
                Ok(result) => result.summary,
                Err(err) => format!("agent docker install failed: {err}"),
            };
            let status = if summary.starts_with("agent docker install failed:") {
                "FAILED"
            } else {
                "SUCCEEDED"
            };
            return Ok(Response::new(self.ctx.state.finish_operation(
                "install_docker",
                &req.node_key,
                "",
                status,
                &summary,
            )));
        }
        Ok(Response::new(self.ctx.state.start_operation(
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
        let profile = req.profile.unwrap_or_else(|| ProfileSpec {
            profile_name: String::new(),
            protocol_kinds: Vec::new(),
            awg: None,
            xray: None,
        });
        let profile_name = profile.profile_name.trim().to_string();
        if let Some(target) = self.ctx.agent_target(&req.node_key) {
            let transport = agent_transport::AgentTransport::new(target);
            let mut lines = Vec::new();
            let mut failed = false;
            let mut result_json = String::new();
            for protocol in profile.protocol_kinds.iter().map(|value| value.trim().to_lowercase()) {
                if protocol == "xray" {
                    let uuid = profile
                        .xray
                        .as_ref()
                        .map(|spec| spec.uuid.trim().to_string())
                        .unwrap_or_default();
                    let short_id = profile
                        .xray
                        .as_ref()
                        .map(|spec| spec.short_id.trim().to_string())
                        .unwrap_or_default();
                    match transport.add_xray_user(&profile_name, &uuid, &short_id).await {
                        Ok(result) => {
                            lines.push(format!("xray: {}", result.summary));
                            if let Err(err) = self
                                .ctx
                                .upsert_profile_server_state(
                                    &profile_name,
                                    &req.node_key,
                                    "xray",
                                    "provisioned",
                                    &uuid,
                                    "",
                                )
                                .await
                            {
                                failed = true;
                                lines.push(format!("xray state update failed: {err}"));
                            }
                        }
                        Err(err) => {
                            failed = true;
                            let message = format!("{err}");
                            lines.push(format!("xray: {message}"));
                            if let Err(state_err) = self
                                .ctx
                                .upsert_profile_server_state(
                                    &profile_name,
                                    &req.node_key,
                                    "xray",
                                    "failed",
                                    &uuid,
                                    &message,
                                )
                                .await
                            {
                                lines.push(format!("xray state update failed: {state_err}"));
                            }
                        }
                    }
                } else if protocol == "awg" {
                    match transport.add_awg_user(&profile_name).await {
                        Ok(result) => {
                            lines.push(format!("awg: {}", result.summary));
                            if !result.payload_json.trim().is_empty() {
                                result_json = result.payload_json.trim().to_string();
                            }
                            if let Err(err) = self
                                .ctx
                                .upsert_profile_server_state(
                                    &profile_name,
                                    &req.node_key,
                                    "awg",
                                    "provisioned",
                                    "",
                                    "",
                                )
                                .await
                            {
                                failed = true;
                                lines.push(format!("awg state update failed: {err}"));
                            }
                        }
                        Err(err) => {
                            failed = true;
                            let message = format!("{err}");
                            lines.push(format!("awg: {message}"));
                            if let Err(state_err) = self
                                .ctx
                                .upsert_profile_server_state(
                                    &profile_name,
                                    &req.node_key,
                                    "awg",
                                    "failed",
                                    "",
                                    &message,
                                )
                                .await
                            {
                                lines.push(format!("awg state update failed: {state_err}"));
                            }
                        }
                    }
                }
            }
            if lines.is_empty() {
                lines.push("no supported protocol kinds requested".to_string());
                failed = true;
            }
            return Ok(Response::new(self.ctx.state.finish_operation_with_result(
                "ensure_profile_on_node",
                &req.node_key,
                &profile_name,
                if failed { "FAILED" } else { "SUCCEEDED" },
                &lines.join("\n"),
                &result_json,
            )));
        }
        Ok(Response::new(self.ctx.state.start_operation(
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
        if let Some(target) = self.ctx.agent_target(&req.node_key) {
            let transport = agent_transport::AgentTransport::new(target);
            let mut lines = Vec::new();
            let mut failed = false;
            for protocol in req.protocol_kinds.iter().map(|value| value.trim().to_lowercase()) {
                if protocol == "xray" {
                    match transport.delete_xray_user(&req.profile_name).await {
                        Ok(result) => {
                            lines.push(format!("xray: {}", result.summary));
                            if let Err(err) = self
                                .ctx
                                .delete_profile_server_state(&req.profile_name, &req.node_key, "xray")
                                .await
                            {
                                failed = true;
                                lines.push(format!("xray state delete failed: {err}"));
                            }
                        }
                        Err(err) => {
                            failed = true;
                            lines.push(format!("xray: {err}"));
                        }
                    }
                } else if protocol == "awg" {
                    match transport.delete_awg_user(&req.profile_name).await {
                        Ok(result) => {
                            lines.push(format!("awg: {}", result.summary));
                            if let Err(err) = self
                                .ctx
                                .delete_profile_server_state(&req.profile_name, &req.node_key, "awg")
                                .await
                            {
                                failed = true;
                                lines.push(format!("awg state delete failed: {err}"));
                            }
                        }
                        Err(err) => {
                            failed = true;
                            lines.push(format!("awg: {err}"));
                        }
                    }
                }
            }
            if lines.is_empty() {
                lines.push("no supported protocol kinds requested".to_string());
                failed = true;
            }
            return Ok(Response::new(self.ctx.state.finish_operation(
                "delete_profile_from_node",
                &req.node_key,
                &req.profile_name,
                if failed { "FAILED" } else { "SUCCEEDED" },
                &lines.join("\n"),
            )));
        }
        Ok(Response::new(self.ctx.state.start_operation(
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
        let node_key = req.node_key.trim().to_string();
        if node_key.is_empty() {
            return Err(Status::invalid_argument("node_key is required"));
        }
        let Some(row) = self.ctx.fetch_server_row(&node_key).await? else {
            return Ok(Response::new(self.ctx.state.finish_operation(
                "reconcile_node",
                &node_key,
                "",
                "FAILED",
                &format!("Server {node_key} not found"),
            )));
        };
        let protocol_kinds = self.ctx.parse_protocol_kinds(
            row.try_get::<_, Option<String>>("protocol_kinds")
                .ok()
                .flatten()
                .unwrap_or_default()
                .as_str(),
        );
        if protocol_kinds.is_empty() {
            return Ok(Response::new(self.ctx.state.finish_operation(
                "reconcile_node",
                &node_key,
                "",
                "SUCCEEDED",
                &format!("server: {node_key}\nno managed protocols"),
            )));
        }
        let Some(target) = self.ctx.agent_target(&node_key) else {
            return Ok(Response::new(self.ctx.state.finish_operation(
                "reconcile_node",
                &node_key,
                "",
                "FAILED",
                "no node-agent target configured",
            )));
        };

        let mut overall_code = 0;
        let mut parts: Vec<String> = Vec::new();
        if protocol_kinds.iter().any(|item| item == "xray") {
            let (code, out) = self.reconcile_xray_on_node(&node_key, target).await?;
            overall_code = overall_code.max(code);
            parts.push("[xray]".to_string());
            parts.push(out.trim().to_string());
        }
        if protocol_kinds.iter().any(|item| item == "awg") {
            let (code, out) = self.reconcile_awg_on_node(&node_key, target).await?;
            overall_code = overall_code.max(code);
            parts.push("[awg]".to_string());
            parts.push(out.trim().to_string());
        }
        let status = if overall_code == 0 { "SUCCEEDED" } else { "FAILED" };
        let message = if parts.is_empty() {
            format!("server: {node_key}\nno managed protocols")
        } else {
            parts.join("\n\n")
        };
        Ok(Response::new(self.ctx.state.finish_operation(
            "reconcile_node",
            &node_key,
            "",
            status,
            &message,
        )))
    }

    async fn reconcile_profile(
        &self,
        request: Request<ReconcileProfileRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        let profile_name = req.profile_name.trim().to_string();
        if profile_name.is_empty() {
            return Err(Status::invalid_argument("profile_name is required"));
        }
        let client = self.ctx.db_client().await?;
        let exists = client
            .query_opt("SELECT name FROM profiles WHERE name = $1", &[&profile_name])
            .await
            .map_err(|err| Status::internal(format!("failed to query profile: {err}")))?;
        if exists.is_none() {
            return Ok(Response::new(self.ctx.state.finish_operation(
                "reconcile_profile",
                "",
                &profile_name,
                "FAILED",
                &format!("profile {profile_name} not found"),
            )));
        }

        let profile_codes_rows = client
            .query(
                "SELECT access_code FROM profile_access_methods WHERE profile_name = $1 ORDER BY access_code",
                &[&profile_name],
            )
            .await
            .map_err(|err| Status::internal(format!("failed to query profile access methods: {err}")))?;
        let profile_codes: Vec<String> = profile_codes_rows
            .into_iter()
            .filter_map(|row| {
                row.try_get::<_, Option<String>>("access_code")
                    .ok()
                    .flatten()
                    .map(|value| value.trim().to_lowercase())
                    .filter(|value| !value.is_empty())
            })
            .collect();

        let servers = self.ctx.list_server_rows(false).await?;
        let mut target_node_keys = HashSet::new();
        for row in servers {
            let node_key = row
                .try_get::<_, Option<String>>("key")
                .ok()
                .flatten()
                .unwrap_or_default()
                .trim()
                .to_string();
            if node_key.is_empty() {
                continue;
            }
            let protocol_kinds = self.ctx.parse_protocol_kinds(
                row.try_get::<_, Option<String>>("protocol_kinds")
                    .ok()
                    .flatten()
                    .unwrap_or_default()
                    .as_str(),
            );
            let wants_xray = protocol_kinds.iter().any(|item| item == "xray")
                && profile_codes
                    .iter()
                    .any(|code| Self::matches_access_code(&node_key, "xray", code));
            let wants_awg = protocol_kinds.iter().any(|item| item == "awg")
                && profile_codes
                    .iter()
                    .any(|code| Self::matches_access_code(&node_key, "awg", code));
            if wants_xray || wants_awg {
                target_node_keys.insert(node_key);
            }
        }
        if target_node_keys.is_empty() {
            return Ok(Response::new(self.ctx.state.finish_operation(
                "reconcile_profile",
                "",
                &profile_name,
                "SUCCEEDED",
                &format!("profile: {profile_name}\nno managed protocols"),
            )));
        }

        let mut node_keys: Vec<String> = target_node_keys.into_iter().collect();
        node_keys.sort();
        let mut overall_failed = false;
        let mut blocks = vec![format!("profile: {profile_name}")];
        for node_key in node_keys {
            let node_result = self
                .reconcile_node(Request::new(ReconcileNodeRequest {
                    node_key: node_key.clone(),
                }))
                .await?;
            let operation_id = node_result.into_inner().operation_id;
            let operation = self
                .ctx
                .state
                .get_operation(operation_id.as_str())
                .ok_or_else(|| Status::internal("reconcile_node operation not found"))?;
            if operation.status != "SUCCEEDED" {
                overall_failed = true;
            }
            blocks.push(format!("[{node_key}]\n{}", operation.progress_message.trim()));
        }

        Ok(Response::new(self.ctx.state.finish_operation(
            "reconcile_profile",
            "",
            &profile_name,
            if overall_failed { "FAILED" } else { "SUCCEEDED" },
            &blocks.join("\n\n"),
        )))
    }

    async fn list_remote_profiles(
        &self,
        request: Request<ListRemoteProfilesRequest>,
    ) -> Result<Response<ListRemoteProfilesResponse>, Status> {
        let req = request.into_inner();
        if let Some(target) = self.ctx.agent_target(&req.node_key) {
            let transport = agent_transport::AgentTransport::new(target);
            if let Ok(items) = transport
                .list_remote_profiles(req.protocol_kind.trim())
                .await
            {
                let items = items
                    .into_iter()
                    .map(|item| RemoteProfileRecord {
                        profile_name: item.profile_name,
                        protocol_kind: item.protocol_kind,
                        remote_id: item.remote_id,
                        status: item.status,
                    })
                    .collect();
                return Ok(Response::new(ListRemoteProfilesResponse { items }));
            }
        }
        let rows = self
            .ctx
            .observed_remote_profiles(&req.node_key, req.protocol_kind.trim())
            .await?;
        let items = rows
            .into_iter()
            .map(|row| RemoteProfileRecord {
                profile_name: row
                    .try_get::<_, Option<String>>("profile_name")
                    .ok()
                    .flatten()
                    .unwrap_or_default(),
                protocol_kind: row
                    .try_get::<_, Option<String>>("protocol_kind")
                    .ok()
                    .flatten()
                    .unwrap_or_default(),
                remote_id: row
                    .try_get::<_, Option<String>>("remote_id")
                    .ok()
                    .flatten()
                    .unwrap_or_default(),
                status: row
                    .try_get::<_, Option<String>>("status")
                    .ok()
                    .flatten()
                    .unwrap_or_else(|| "unknown".to_string()),
            })
            .collect();
        Ok(Response::new(ListRemoteProfilesResponse { items }))
    }
}

#[tonic::async_trait]
impl RuntimeService for RuntimeApi {
    async fn bootstrap_node(
        &self,
        request: Request<BootstrapNodeRequest>,
    ) -> Result<Response<StartOperationResponse>, Status> {
        let req = request.into_inner();
        if let Some(target) = self.ctx.agent_target(&req.node_key) {
            let row = match self.ctx.fetch_server_row(&req.node_key).await {
                Ok(Some(row)) => row,
                Ok(None) => {
                    return Ok(Response::new(self.ctx.state.finish_operation(
                        "bootstrap_node",
                        &req.node_key,
                        "",
                        "FAILED",
                        "server not found",
                    )));
                }
                Err(err) => {
                    return Ok(Response::new(self.ctx.state.finish_operation(
                        "bootstrap_node",
                        &req.node_key,
                        "",
                        "FAILED",
                        &format!("failed to load server registry row: {err}"),
                    )));
                }
            };
            let transport = agent_transport::AgentTransport::new(target);
            let protocol_kinds = self.ctx.parse_protocol_kinds(
                row.try_get::<_, Option<String>>("protocol_kinds")
                    .ok()
                    .flatten()
                    .unwrap_or_default()
                    .as_str(),
            );
            let mut completed_parts = vec!["Base packages and helper scripts installed".to_string()];

            let port_result = transport
                .check_ports(self.ctx.port_check_specs_from_row(&row))
                .await;
            match port_result {
                Ok(result) => {
                    if result.items.iter().any(|item| item.status == "busy" || item.status == "invalid") {
                        let message = format!("port check failed before bootstrap\n{}", result.summary);
                        let _ = self.ctx.mark_bootstrap_state(&req.node_key, "bootstrap_failed", &message).await;
                        return Ok(Response::new(self.ctx.state.finish_operation(
                            "bootstrap_node",
                            &req.node_key,
                            "",
                            "FAILED",
                            &message,
                        )));
                    }
                }
                Err(err) => {
                    let message = format!("agent port check failed: {err}");
                    let _ = self.ctx.mark_bootstrap_state(&req.node_key, "bootstrap_failed", &message).await;
                    return Ok(Response::new(self.ctx.state.finish_operation(
                        "bootstrap_node",
                        &req.node_key,
                        "",
                        "FAILED",
                        &message,
                    )));
                }
            }

            if let Err(err) = transport.install_docker().await {
                let message = format!("agent docker install failed: {err}");
                let _ = self.ctx.mark_bootstrap_state(&req.node_key, "bootstrap_failed", &message).await;
                return Ok(Response::new(self.ctx.state.finish_operation(
                    "bootstrap_node",
                    &req.node_key,
                    "",
                    "FAILED",
                    &message,
                )));
            }

            let files = self.ctx.runtime_file_bundle(Some(&row), &req.node_key)?;
            if let Err(err) = transport.sync_runtime_files(files).await {
                let message = format!("agent runtime bundle sync failed: {err}");
                let _ = self.ctx.mark_bootstrap_state(&req.node_key, "bootstrap_failed", &message).await;
                return Ok(Response::new(self.ctx.state.finish_operation(
                    "bootstrap_node",
                    &req.node_key,
                    "",
                    "FAILED",
                    &message,
                )));
            }

            if protocol_kinds.iter().any(|item| item == "xray") {
                let config_path = self.ctx.row_string(
                    &row,
                    "xray_config_path",
                    "/opt/node-plane-runtime/xray/config.json",
                );
                let public_host = self.ctx.row_string(&row, "public_host", "");
                let sni_host = self.ctx.row_string(&row, "xray_sni", "www.cloudflare.com");
                let flow = self
                    .ctx
                    .row_string(&row, "xray_flow", "xtls-rprx-vision");
                let path_prefix = self.ctx.row_string(&row, "xray_xhttp_path_prefix", "/assets");
                let tcp_port = self.ctx.row_i32(&row, "xray_tcp_port", 443).max(1) as u32;
                let xhttp_port = self.ctx.row_i32(&row, "xray_xhttp_port", 8443).max(1) as u32;
                let preserve_xray_config = req.preserve_config
                    && transport.path_exists(&config_path).await.unwrap_or(false);
                if !preserve_xray_config {
                    match transport
                        .init_xray(
                            &config_path,
                            &public_host,
                            &sni_host,
                            tcp_port,
                            xhttp_port,
                            &path_prefix,
                            &flow,
                            "ghcr.io/xtls/xray-core:25.12.8",
                        )
                        .await
                    {
                        Ok(result) => match serde_json::from_str::<XraySyncGenerated>(&result.generated_json) {
                            Ok(generated) => {
                                if let Err(err) = self.ctx.update_xray_server_fields(&req.node_key, &generated).await {
                                    let message = format!("failed to persist generated xray settings: {err}");
                                    let _ = self.ctx.mark_bootstrap_state(&req.node_key, "bootstrap_failed", &message).await;
                                    return Ok(Response::new(self.ctx.state.finish_operation(
                                        "bootstrap_node",
                                        &req.node_key,
                                        "",
                                        "FAILED",
                                        &message,
                                    )));
                                }
                            }
                            Err(err) => {
                                let message = format!("agent init xray returned invalid json: {err}");
                                let _ = self.ctx.mark_bootstrap_state(&req.node_key, "bootstrap_failed", &message).await;
                                return Ok(Response::new(self.ctx.state.finish_operation(
                                    "bootstrap_node",
                                    &req.node_key,
                                    "",
                                    "FAILED",
                                    &message,
                                )));
                            }
                        },
                        Err(err) => {
                            let message = format!("agent init xray failed: {err}");
                            let _ = self.ctx.mark_bootstrap_state(&req.node_key, "bootstrap_failed", &message).await;
                            return Ok(Response::new(self.ctx.state.finish_operation(
                                "bootstrap_node",
                                &req.node_key,
                                "",
                                "FAILED",
                                &message,
                            )));
                        }
                    }
                    completed_parts.push("Xray settings generated".to_string());
                } else {
                    completed_parts.push("Xray config preserved".to_string());
                }
                if let Err(err) = transport.deploy_xray().await {
                    let message = format!("agent deploy xray failed: {err}");
                    let _ = self.ctx.mark_bootstrap_state(&req.node_key, "bootstrap_failed", &message).await;
                    return Ok(Response::new(self.ctx.state.finish_operation(
                        "bootstrap_node",
                        &req.node_key,
                        "",
                        "FAILED",
                        &message,
                    )));
                }
                completed_parts.push("Xray runtime deployed".to_string());
            }

            if protocol_kinds.iter().any(|item| item == "awg") {
                let awg_config_path = self.ctx.row_string(
                    &row,
                    "awg_config_path",
                    "/opt/node-plane-runtime/amnezia-awg/data/wg0.conf",
                );
                let preserve_awg_config = req.preserve_config
                    && transport.path_exists(&awg_config_path).await.unwrap_or(false);
                if !preserve_awg_config {
                    if let Err(err) = transport.init_awg().await {
                        let message = format!("agent init awg failed: {err}");
                        let _ = self.ctx.mark_bootstrap_state(&req.node_key, "bootstrap_failed", &message).await;
                        return Ok(Response::new(self.ctx.state.finish_operation(
                            "bootstrap_node",
                            &req.node_key,
                            "",
                            "FAILED",
                            &message,
                        )));
                    }
                }
                if let Err(err) = transport.deploy_awg().await {
                    let message = format!("agent deploy awg failed: {err}");
                    let _ = self.ctx.mark_bootstrap_state(&req.node_key, "bootstrap_failed", &message).await;
                    return Ok(Response::new(self.ctx.state.finish_operation(
                        "bootstrap_node",
                        &req.node_key,
                        "",
                        "FAILED",
                        &message,
                    )));
                }
                completed_parts.push("AWG runtime deployed".to_string());
            }

            let summary = format!("Bootstrap completed. {}.", completed_parts.join(". "));
            match self
                .ctx
                .mark_bootstrap_state(&req.node_key, "bootstrapped", &summary)
                .await
            {
                Ok(()) => {
                    return Ok(Response::new(self.ctx.state.finish_operation(
                        "bootstrap_node",
                        &req.node_key,
                        "",
                        "SUCCEEDED",
                        &summary,
                    )));
                }
                Err(err) => {
                    let message = format!("bootstrap completed on agent but registry update failed: {err}");
                    return Ok(Response::new(self.ctx.state.finish_operation(
                        "bootstrap_node",
                        &req.node_key,
                        "",
                        "FAILED",
                        &message,
                    )));
                }
            }
        }
        Ok(Response::new(self.ctx.state.start_operation(
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
        if let Some(target) = self.ctx.agent_target(&req.node_key) {
            if !req.preserve_config {
                let transport = agent_transport::AgentTransport::new(target);
                let cleanup_summary = match transport.delete_runtime(false).await {
                    Ok(result) => match self.ctx.mark_runtime_deleted(&req.node_key, false).await {
                        Ok(()) => result.summary,
                        Err(err) => {
                            return Ok(Response::new(self.ctx.state.finish_operation(
                                "reinstall_node",
                                &req.node_key,
                                "",
                                "FAILED",
                                &format!(
                                    "runtime deleted on agent but central registry update failed: {err}"
                                ),
                            )));
                        }
                    },
                    Err(err) => {
                        return Ok(Response::new(self.ctx.state.finish_operation(
                            "reinstall_node",
                            &req.node_key,
                            "",
                            "FAILED",
                            &format!("agent runtime delete failed before reinstall: {err}"),
                        )));
                    }
                };
                let bootstrap_response = self
                    .bootstrap_node(Request::new(BootstrapNodeRequest {
                        node_key: req.node_key.clone(),
                        preserve_config: false,
                    }))
                    .await?
                    .into_inner();
                let bootstrap_operation = self.ctx.state.get_operation(&bootstrap_response.operation_id);
                if let Some(op) = bootstrap_operation {
                    let status = op.status;
                    let message = format!("Clean reinstall.\n{cleanup_summary}\n\n{}", op.progress_message);
                    return Ok(Response::new(self.ctx.state.finish_operation(
                        "reinstall_node",
                        &req.node_key,
                        "",
                        &status,
                        &message,
                    )));
                }
            } else {
                let bootstrap_response = self
                    .bootstrap_node(Request::new(BootstrapNodeRequest {
                        node_key: req.node_key.clone(),
                        preserve_config: true,
                    }))
                    .await?
                    .into_inner();
                let bootstrap_operation = self.ctx.state.get_operation(&bootstrap_response.operation_id);
                if let Some(op) = bootstrap_operation {
                    let status = op.status;
                    let message = format!("Reinstall with existing config preserved.\n{}", op.progress_message);
                    return Ok(Response::new(self.ctx.state.finish_operation(
                        "reinstall_node",
                        &req.node_key,
                        "",
                        &status,
                        &message,
                    )));
                }
            }
            return Ok(Response::new(self.ctx.state.finish_operation(
                "reinstall_node",
                &req.node_key,
                "",
                "FAILED",
                "bootstrap operation result was not found",
            )));
        }
        Ok(Response::new(self.ctx.state.start_operation(
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
        if let Some(target) = self.ctx.agent_target(&req.node_key) {
            let transport = agent_transport::AgentTransport::new(target);
            let summary = match transport.delete_runtime(req.preserve_config).await {
                Ok(result) => match self
                    .ctx
                    .mark_runtime_deleted(&req.node_key, req.preserve_config)
                    .await
                {
                    Ok(()) => result.summary,
                    Err(err) => format!("runtime deleted on agent but central registry update failed: {err}"),
                },
                Err(err) => format!("agent runtime delete failed: {err}"),
            };
            let status = if summary.starts_with("managed runtime removed") {
                "SUCCEEDED"
            } else {
                "FAILED"
            };
            return Ok(Response::new(self.ctx.state.finish_operation(
                "delete_runtime",
                &req.node_key,
                "",
                status,
                &summary,
            )));
        }
        Ok(Response::new(self.ctx.state.start_operation(
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
        if let Some(target) = self.ctx.agent_target(&req.node_key) {
            let transport = agent_transport::AgentTransport::new(target);
            let cleanup_summary = match transport.delete_runtime(false).await {
                Ok(result) => match self.ctx.mark_runtime_deleted(&req.node_key, false).await {
                    Ok(()) => result.summary,
                    Err(err) => {
                        return Ok(Response::new(self.ctx.state.finish_operation(
                            "full_cleanup_node",
                            &req.node_key,
                            "",
                            "FAILED",
                            &format!(
                                "runtime deleted on agent but central registry update failed: {err}"
                            ),
                        )));
                    }
                },
                Err(err) => {
                    return Ok(Response::new(self.ctx.state.finish_operation(
                        "full_cleanup_node",
                        &req.node_key,
                        "",
                        "FAILED",
                        &format!("agent runtime delete failed: {err}"),
                    )));
                }
            };

            let mut lines = vec![cleanup_summary];
            let mut notes = vec!["full cleanup completed".to_string()];
            if req.remove_ssh_key {
                match self.ctx.bot_public_key() {
                    Ok(public_key) => match transport.remove_authorized_key(&public_key).await {
                        Ok(result) => {
                            if !result.summary.trim().is_empty() {
                                lines.push(result.summary);
                            }
                            if result.removed {
                                notes.push("ssh key removed".to_string());
                            } else {
                                notes.push("ssh key absent".to_string());
                            }
                        }
                        Err(err) => {
                            lines.push(format!("SSH key removal failed: {err}"));
                            notes.push("ssh key removal failed".to_string());
                        }
                    },
                    Err(err) => {
                        lines.push(format!("SSH key removal failed: {err}"));
                        notes.push("ssh key removal failed".to_string());
                    }
                }
            }
            let notes_text = notes.join("; ");
            if let Err(err) = self.ctx.mark_full_cleanup(&req.node_key, &notes_text).await {
                lines.push(format!("central registry notes update failed: {err}"));
                return Ok(Response::new(self.ctx.state.finish_operation(
                    "full_cleanup_node",
                    &req.node_key,
                    "",
                    "FAILED",
                    &lines.join("\n"),
                )));
            }
            return Ok(Response::new(self.ctx.state.finish_operation(
                "full_cleanup_node",
                &req.node_key,
                "",
                "SUCCEEDED",
                &lines.join("\n"),
            )));
        }
        Ok(Response::new(self.ctx.state.start_operation(
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
        if let Some(target) = self.ctx.agent_target(&req.node_key) {
            let row = match self.ctx.fetch_server_row(&req.node_key).await {
                Ok(Some(row)) => Some(row),
                _ => None,
            };
            let files = self
                .ctx
                .runtime_file_bundle(row.as_ref(), &req.node_key)?;
            let has_xray = row
                .as_ref()
                .map(|value| {
                    self.ctx
                        .parse_protocol_kinds(
                            value.try_get::<_, Option<String>>("protocol_kinds")
                                .ok()
                                .flatten()
                                .unwrap_or_default()
                                .as_str(),
                        )
                        .iter()
                        .any(|item| item == "xray")
                })
                .unwrap_or(false);
            let transport = agent_transport::AgentTransport::new(target);
            let summary = match transport.sync_runtime_files(files).await {
                Ok(result) => {
                    if has_xray {
                        format!(
                            "runtime bundle synced via agent\n{}\nXray settings still require a separate Sync Xray step",
                            result.summary
                        )
                    } else {
                        format!("runtime bundle synced via agent\n{}", result.summary)
                    }
                }
                Err(err) => format!("agent runtime sync failed: {err}"),
            };
            let status = if summary.starts_with("runtime bundle synced via agent\n") {
                "SUCCEEDED"
            } else {
                "FAILED"
            };
            return Ok(Response::new(self.ctx.state.finish_operation(
                "sync_runtime",
                &req.node_key,
                "",
                status,
                &summary,
            )));
        }
        Ok(Response::new(self.ctx.state.start_operation(
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
        if let Some(target) = self.ctx.agent_target(&req.node_key) {
            let row = match self.ctx.fetch_server_row(&req.node_key).await {
                Ok(Some(row)) => row,
                Ok(None) => {
                    return Ok(Response::new(self.ctx.state.finish_operation(
                        "sync_xray",
                        &req.node_key,
                        "",
                        "FAILED",
                        "server not found",
                    )));
                }
                Err(err) => {
                    return Ok(Response::new(self.ctx.state.finish_operation(
                        "sync_xray",
                        &req.node_key,
                        "",
                        "FAILED",
                        &format!("failed to load server registry row: {err}"),
                    )));
                }
            };
            let protocol_kinds = self.ctx.parse_protocol_kinds(
                row.try_get::<_, Option<String>>("protocol_kinds")
                    .ok()
                    .flatten()
                    .unwrap_or_default()
                    .as_str(),
            );
            if !protocol_kinds.iter().any(|item| item == "xray") {
                return Ok(Response::new(self.ctx.state.finish_operation(
                    "sync_xray",
                    &req.node_key,
                    "",
                    "FAILED",
                    "xray is not enabled on this server",
                )));
            }
            let config_path = self.ctx.row_string(
                &row,
                "xray_config_path",
                "/opt/node-plane-runtime/xray/config.json",
            );
            let public_host = self.ctx.row_string(&row, "public_host", "");
            let flow = self
                .ctx
                .row_string(&row, "xray_flow", "xtls-rprx-vision");
            let image = "ghcr.io/xtls/xray-core:25.12.8";
            let transport = agent_transport::AgentTransport::new(target);
            let summary = match transport
                .sync_xray(&config_path, &public_host, &flow, image)
                .await
            {
                Ok(result) => match serde_json::from_str::<XraySyncGenerated>(&result.generated_json)
                {
                    Ok(generated) => {
                        match self
                            .ctx
                            .update_xray_server_fields(&req.node_key, &generated)
                            .await
                        {
                            Ok(()) => result.generated_json,
                            Err(err) => format!("failed to persist xray settings: {err}"),
                        }
                    }
                    Err(err) => format!("agent xray sync returned invalid json: {err}"),
                },
                Err(err) => format!("agent xray sync failed: {err}"),
            };
            let status = if summary.trim_start().starts_with('{') {
                "SUCCEEDED"
            } else {
                "FAILED"
            };
            return Ok(Response::new(self.ctx.state.finish_operation(
                "sync_xray",
                &req.node_key,
                "",
                status,
                &summary,
            )));
        }
        Ok(Response::new(self.ctx.state.start_operation(
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
        let runtime = if let Some(target) = self.ctx.agent_target(&req.node_key) {
            let transport = agent_transport::AgentTransport::new(target);
            match transport.get_runtime_facts().await {
                Ok(facts) => RuntimeStatus {
                    node_key: facts.node_key,
                    state: self
                        .ctx
                        .runtime_state_from_values(&facts.version, &facts.commit),
                    version: facts.version,
                    commit: facts.commit,
                    expected_version: self.ctx.app_semver.clone(),
                    expected_commit: self.ctx.app_commit.clone(),
                    message: "reported by node agent".to_string(),
                },
                Err(_) => match self.ctx.fetch_server_row(&req.node_key).await? {
                    Some(row) => self.ctx.runtime_status_from_row(&row),
                    None => missing_runtime_status(
                        &req.node_key,
                        &self.ctx.app_semver,
                        &self.ctx.app_commit,
                    ),
                },
            }
        } else {
            match self.ctx.fetch_server_row(&req.node_key).await? {
                Some(row) => self.ctx.runtime_status_from_row(&row),
                None => missing_runtime_status(
                    &req.node_key,
                    &self.ctx.app_semver,
                    &self.ctx.app_commit,
                ),
            }
        };
        Ok(Response::new(GetRuntimeStatusResponse {
            runtime: Some(runtime),
            services: vec![ServiceStatus {
                service_name: "node-driver".to_string(),
                state: "running".to_string(),
                summary: if self.ctx.agent_target(&req.node_key).is_some() {
                    "agent transport enabled for this node".to_string()
                } else {
                    "postgres-backed read model, execution path still skeleton".to_string()
                },
            }],
        }))
    }

    async fn list_nodes_needing_runtime_sync(
        &self,
        _request: Request<ListNodesNeedingRuntimeSyncRequest>,
    ) -> Result<Response<ListNodesNeedingRuntimeSyncResponse>, Status> {
        let rows = self.ctx.list_server_rows(false).await?;
        let mut items = Vec::new();
        for row in rows {
            let node_key = row
                .try_get::<_, Option<String>>("key")
                .ok()
                .flatten()
                .unwrap_or_default();
            let runtime = if let Some(target) = self.ctx.agent_target(&node_key) {
                let transport = agent_transport::AgentTransport::new(target);
                match transport.get_runtime_facts().await {
                    Ok(facts) => RuntimeStatus {
                        node_key: facts.node_key,
                        state: self
                            .ctx
                            .runtime_state_from_values(&facts.version, &facts.commit),
                        version: facts.version,
                        commit: facts.commit,
                        expected_version: self.ctx.app_semver.clone(),
                        expected_commit: self.ctx.app_commit.clone(),
                        message: "reported by node agent".to_string(),
                    },
                    Err(_) => self.ctx.runtime_status_from_row(&row),
                }
            } else {
                self.ctx.runtime_status_from_row(&row)
            };
            if matches!(runtime.state.as_str(), "outdated" | "unknown") {
                let mut node = self.ctx.node_from_row(&row);
                if let Some(target) = self.ctx.agent_target(&node_key) {
                    let transport = agent_transport::AgentTransport::new(target);
                    if let Ok(health) = transport.get_node_health().await {
                        self.ctx.apply_agent_health(&mut node, health);
                    }
                }
                items.push(node);
            }
        }
        Ok(Response::new(ListNodesNeedingRuntimeSyncResponse { items }))
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
        Ok(Response::new(self.ctx.state.start_operation(
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
        let rows = self
            .ctx
            .profile_usage_rows(&req.profile_name, &req.protocol_kind)
            .await?;

        let mut rx_total: u64 = 0;
        let mut tx_total: u64 = 0;
        let mut samples: u32 = 0;
        let peers = rows.len() as u32;

        for row in rows {
            let sample_count = row.try_get::<_, i64>("sample_count").unwrap_or(0);
            let rx_first = row.try_get::<_, i64>("rx_first").unwrap_or(0);
            let rx_last = row.try_get::<_, i64>("rx_last").unwrap_or(0);
            let tx_first = row.try_get::<_, i64>("tx_first").unwrap_or(0);
            let tx_last = row.try_get::<_, i64>("tx_last").unwrap_or(0);
            rx_total += rx_last.saturating_sub(rx_first).max(0) as u64;
            tx_total += tx_last.saturating_sub(tx_first).max(0) as u64;
            samples = samples.saturating_add(sample_count.max(0) as u32);
        }

        Ok(Response::new(GetProfileUsageResponse {
            usage: Some(ProfileUsage {
                profile_name: req.profile_name,
                protocol_kind: req.protocol_kind,
                rx_bytes: rx_total,
                tx_bytes: tx_total,
                total_bytes: rx_total.saturating_add(tx_total),
                samples,
                peers,
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
        match self.ctx.state.get_operation(&req.operation_id) {
            Some(op) => Ok(Response::new(op)),
            None => Err(Status::not_found("operation not found")),
        }
    }

    type WatchOperationStream =
        tokio_stream::wrappers::ReceiverStream<Result<OperationEvent, Status>>;

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

    let ctx = DriverContext::from_env();
    let node_api = NodeApi { ctx: ctx.clone() };
    let provisioning_api = ProvisioningApi { ctx: ctx.clone() };
    let runtime_api = RuntimeApi { ctx: ctx.clone() };
    let telemetry_api = TelemetryApi { ctx: ctx.clone() };
    let operation_api = OperationApi { ctx };

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
