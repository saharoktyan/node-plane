//! The agent's check of its host before it removes itself.
//!
//! Without SSH the controller cannot look at the server after removal, so
//! the agent reports what it found while it still runs: the runtime, its
//! configs and its containers are gone. Its own files go after this check,
//! and nothing confirms that part.
use std::path::Path;
use std::process::Command;

use serde_json::json;
use tonic::Status;

use crate::state::AgentState;

pub(crate) const MACHINE_ID: &str = "/etc/machine-id";

/// /etc/machine-id, when it is a valid one.
pub(crate) fn host_id(path: &Path) -> String {
    let value = AgentState::read_first_line(path);
    if value.len() == 32
        && value
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
    {
        value
    } else {
        String::new()
    }
}

impl AgentState {
    /// What the controller records as the agent's evidence; an error when
    /// anything owned by the runtime is still on the host.
    pub(crate) fn decommission_host_check(&self) -> Result<String, Status> {
        let host = host_id(Path::new(MACHINE_ID));
        if host.is_empty() {
            return Err(Status::failed_precondition("host identity unavailable"));
        }
        // node.env is gone by now, so its overrides are too: these are the
        // configured paths, which delete_runtime removed with the runtime.
        let paths = [
            self.config.runtime_root.clone(),
            self.config.node_env_path.clone(),
            self.config.xray_config_path.clone(),
            self.config.awg_config_path.clone(),
        ];
        let present: Vec<&String> = paths
            .iter()
            .filter(|path| Path::new(path.as_str()).symlink_metadata().is_ok())
            .collect();
        if !present.is_empty() {
            return Err(Status::failed_precondition(
                "runtime cleanup is not complete",
            ));
        }
        let containers = if self.docker_available() {
            if !self.owned_runtime_containers()?.is_empty() {
                return Err(Status::failed_precondition(
                    "managed containers still present",
                ));
            }
            "absent"
        } else if Command::new("docker")
            .arg("--version")
            .output()
            .map(|output| output.status.success())
            .unwrap_or(false)
        {
            return Err(Status::failed_precondition(
                "Docker is unavailable; cannot verify runtime cleanup",
            ));
        } else {
            "docker_not_installed"
        };
        Ok(json!({
            "host_id": host,
            "absent_paths": paths,
            "containers": containers,
        })
        .to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn host_id_accepts_only_a_machine_id() {
        let dir = std::env::temp_dir().join(format!("node-plane-host-id-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("machine-id");
        fs::write(&path, "0123456789abcdef0123456789abcdef\n").unwrap();
        assert_eq!(host_id(&path), "0123456789abcdef0123456789abcdef");
        fs::write(&path, "uninitialized\n").unwrap();
        assert_eq!(host_id(&path), "");
        assert_eq!(host_id(&dir.join("missing")), "");
        fs::remove_dir_all(dir).unwrap();
    }
}
