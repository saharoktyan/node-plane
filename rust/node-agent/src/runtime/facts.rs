//! Runtime facts and agent health.
use std::fs;
use std::path::{Path, PathBuf};

use crate::agent::v1::{LocalHealth, RuntimeFacts};
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn read_first_line(path: &Path) -> String {
        let Ok(raw) = fs::read_to_string(path) else {
            return String::new();
        };
        raw.lines().next().unwrap_or("").trim().to_string()
    }

    pub(crate) fn runtime_version_path(&self) -> PathBuf {
        Path::new(&self.config.runtime_root).join("VERSION")
    }

    pub(crate) fn runtime_commit_path(&self) -> PathBuf {
        Path::new(&self.config.runtime_root).join("BUILD_COMMIT")
    }

    pub(crate) fn runtime_facts(&self) -> RuntimeFacts {
        let xray_config_path = self.node_env_value("XRAY_CONFIG", &self.config.xray_config_path);
        let awg_config_path = self.node_env_value("AWG_CONFIG", &self.config.awg_config_path);
        let xray_config_present = Path::new(&xray_config_path).is_file();
        let awg_config_present = Path::new(&awg_config_path).is_file();
        RuntimeFacts {
            node_key: self.config.node_key.clone(),
            version: Self::read_first_line(&self.runtime_version_path()),
            commit: Self::read_first_line(&self.runtime_commit_path()),
            binary_version: env!("NODE_PLANE_BINARY_VERSION").to_string(),
            binary_commit: env!("NODE_PLANE_BINARY_COMMIT").to_string(),
            update_rolled_back: self.update_rolled_back(),
            host_id: crate::lifecycle::host_check::host_id(Path::new(
                crate::lifecycle::host_check::MACHINE_ID,
            )),
            runtime_root: self.config.runtime_root.clone(),
            xray_config_path,
            awg_config_path,
            xray_config_present,
            awg_config_present,
        }
    }

    pub(crate) fn health(&self) -> LocalHealth {
        let runtime_root = Path::new(&self.config.runtime_root);
        // A responding agent is healthy even before VPN runtime provisioning.
        // Runtime readiness is reported separately by RuntimeFacts/diagnostics.
        let state = "running";
        let summary = if runtime_root.is_dir() {
            "runtime root present"
        } else {
            "agent ready; runtime not installed"
        };
        LocalHealth {
            state: state.to_string(),
            summary: summary.to_string(),
            last_seen_at: self.last_seen_at(),
        }
    }
}

#[cfg(test)]
mod tests {
    use crate::config::AgentConfig;
    use crate::state::AgentState;
    use std::{
        process,
        time::{SystemTime, UNIX_EPOCH},
    };

    #[test]
    fn agent_health_does_not_require_provisioned_runtime() {
        let root = std::env::temp_dir().join(format!(
            "node-plane-health-{}-{}",
            process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let state = AgentState::new(AgentConfig {
            runtime_root: root.display().to_string(),
            xray_config_path: root.join("xray.json").display().to_string(),
            ..AgentConfig::default()
        });
        assert!(!root.exists());
        assert_eq!(state.health().state, "running");
        assert_eq!(state.health().summary, "agent ready; runtime not installed");
        assert!(!state.runtime_facts().xray_config_present);
    }
}
