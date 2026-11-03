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
    /// Refuses to touch a VPN runtime that node.env assigns to another node,
    /// for example one left by another installation. Nothing is written.
    pub(crate) fn ensure_runtime_owner(&self) -> Result<(), Status> {
        if !Path::new(&self.config.node_env_path).exists() {
            return Ok(());
        }
        let owner = self.node_env_value("SERVER_KEY", "");
        if owner == self.config.node_key {
            return Ok(());
        }
        let owner: String = owner
            .chars()
            .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
            .take(64)
            .collect();
        Err(Status::failed_precondition(format!(
            "runtime_owner_conflict: {}",
            if owner.is_empty() { "unknown" } else { &owner }
        )))
    }

    pub(crate) fn sync_node_env(&self, content: &str) -> Result<SyncNodeEnvResponse, Status> {
        self.ensure_runtime_owner()?;
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
        self.ensure_runtime_owner()?;
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
        fs::read_to_string(&self.config.node_env_path)
            .ok()
            .and_then(|raw| env_value(&raw, key))
            .unwrap_or_else(|| default.to_string())
    }
}

/// `key` of a node.env text, without running it.
pub(crate) fn env_value(raw: &str, key: &str) -> Option<String> {
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
        return Some(
            value
                .trim()
                .trim_matches('"')
                .trim_matches('\'')
                .to_string(),
        );
    }
    None
}

#[cfg(test)]
mod tests {
    use crate::agent::v1::{RuntimeFileSpec, SyncRuntimeFilesRequest};
    use crate::config::AgentConfig;
    use crate::state::AgentState;
    use std::fs;

    #[test]
    fn a_runtime_of_another_node_is_never_written() {
        let root = std::env::temp_dir().join(format!("np-owner-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("runtime")).unwrap();
        let node_env = root.join("node.env");
        let state = AgentState::new(AgentConfig {
            node_key: "de1".into(),
            runtime_root: root.join("runtime").display().to_string(),
            node_env_path: node_env.display().to_string(),
            ..AgentConfig::default()
        });
        let request = || SyncRuntimeFilesRequest {
            files: vec![RuntimeFileSpec {
                path: "/opt/node-plane-runtime/VERSION".into(),
                content: "0.5.0\n".into(),
                mode: "0644".into(),
            }],
        };
        // A fresh server has no node.env: files may be written.
        state.sync_runtime_files(request()).unwrap();
        for (content, owner) in [
            ("SERVER_KEY=ger2\n", "ger2"),
            ("XRAY_CONFIG=/x\n", "unknown"),
            ("SERVER_KEY='$(touch x)'\n", "touchx"),
        ] {
            fs::write(&node_env, content).unwrap();
            fs::write(root.join("runtime/VERSION"), "0.3.9\n").unwrap();
            let error = state.sync_runtime_files(request()).unwrap_err();
            assert_eq!(error.code(), tonic::Code::FailedPrecondition);
            assert_eq!(error.message(), format!("runtime_owner_conflict: {owner}"));
            assert_eq!(
                fs::read_to_string(root.join("runtime/VERSION")).unwrap(),
                "0.3.9\n"
            );
            assert!(state.sync_node_env("SERVER_KEY=de1\n").is_err());
            assert_eq!(fs::read_to_string(&node_env).unwrap(), content);
        }
        fs::write(&node_env, "SERVER_KEY=\"de1\"\n").unwrap();
        state.sync_runtime_files(request()).unwrap();
        let _ = fs::remove_dir_all(root);
    }
}
