//! Runtime files and node.env.
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use tonic::Status;

use crate::agent::v1::{
    PathExistsResponse, RuntimeFileSpec, SyncNodeEnvResponse, SyncRuntimeFilesRequest,
    SyncRuntimeFilesResponse,
};
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn sync_node_env(&self, content: &str) -> Result<SyncNodeEnvResponse, Status> {
        let _guard = self.guard_fenced_maintenance()?;
        let path = Path::new(&self.config.node_env_path);
        let Some(parent) = path.parent() else {
            return Err(Status::internal("node env path has no parent directory"));
        };
        fs::create_dir_all(parent).map_err(|err| {
            Status::internal(format!("failed to create node env directory: {err}"))
        })?;
        fs::write(path, content)
            .map_err(|err| Status::internal(format!("failed to write node env: {err}")))?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).map_err(|err| {
            Status::internal(format!("failed to set node env permissions: {err}"))
        })?;
        Ok(SyncNodeEnvResponse {
            summary: format!("node.env written to {}", self.config.node_env_path),
            path: self.config.node_env_path.clone(),
        })
    }

    pub(crate) fn write_runtime_file(&self, spec: &RuntimeFileSpec) -> Result<(), Status> {
        let resolved = self.resolve_runtime_path(&spec.path);
        let path = Path::new(&resolved);
        let Some(parent) = path.parent() else {
            return Err(Status::internal(
                "runtime file path has no parent directory",
            ));
        };
        fs::create_dir_all(parent).map_err(|err| {
            Status::internal(format!("failed to create runtime directory: {err}"))
        })?;
        fs::write(path, &spec.content).map_err(|err| {
            Status::internal(format!("failed to write runtime file {}: {err}", resolved))
        })?;
        let mode = u32::from_str_radix(spec.mode.trim(), 8).unwrap_or(0o644);
        fs::set_permissions(path, fs::Permissions::from_mode(mode)).map_err(|err| {
            Status::internal(format!("failed to set permissions on {}: {err}", resolved))
        })?;
        Ok(())
    }

    pub(crate) fn resolve_runtime_path(&self, path: &str) -> String {
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

    pub(crate) fn sync_runtime_files(
        &self,
        request: SyncRuntimeFilesRequest,
    ) -> Result<SyncRuntimeFilesResponse, Status> {
        // Code and container assets may still be upgraded. Existing protocol
        // config and node.env must not be replaced around the revision fence.
        let xray_config = self.node_env_value("XRAY_CONFIG", &self.config.xray_config_path);
        let awg_config = self.node_env_value("AWG_CONFIG", &self.config.awg_config_path);
        let sensitive = request.files.iter().any(|spec| {
            let path = self.resolve_runtime_path(&spec.path);
            path == self.config.node_env_path
                || path == xray_config
                || path == awg_config
                || path.ends_with("/xray/config.json")
                || path.ends_with("/amnezia-awg/data/wg0.conf")
                || ([&xray_config, &awg_config].iter().any(|config| {
                    fs::canonicalize(&path).is_ok_and(|candidate| {
                        fs::canonicalize(config).is_ok_and(|target| candidate == target)
                    })
                }))
        });
        let _guard = if sensitive {
            self.guard_fenced_maintenance()?
        } else {
            None
        };
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

    pub(crate) fn path_exists(&self, path: &str) -> PathExistsResponse {
        PathExistsResponse {
            exists: Path::new(&self.resolve_runtime_path(path)).exists(),
        }
    }

    pub(crate) fn node_env_value(&self, key: &str, default: &str) -> String {
        let Ok(raw) = fs::read_to_string(&self.config.node_env_path) else {
            return default.to_string();
        };
        for line in raw.lines() {
            let trimmed = line.trim();
            if trimmed.is_empty() || trimmed.starts_with('#') {
                continue;
            }
            let Some((name, value)) = trimmed.split_once('=') else {
                continue;
            };
            if name.trim() != key {
                continue;
            }
            return value
                .trim()
                .trim_matches('"')
                .trim_matches('\'')
                .to_string();
        }
        default.to_string()
    }
}
