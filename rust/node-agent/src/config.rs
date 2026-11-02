//! Agent configuration file and its defaults.
use std::env;
use std::fs;
use std::path::Path;

use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub(crate) struct AgentConfig {
    pub(crate) node_key: String,
    pub(crate) listen_addr: String,
    #[serde(default = "default_tls_certificate_path")]
    pub(crate) tls_certificate_path: String,
    #[serde(default = "default_tls_key_path")]
    pub(crate) tls_key_path: String,
    #[serde(default = "default_tls_client_ca_path")]
    pub(crate) tls_client_ca_path: String,
    pub(crate) heartbeat_seconds: u64,
    pub(crate) runtime_root: String,
    pub(crate) state_dir: String,
    pub(crate) log_dir: String,
    pub(crate) node_env_path: String,
    pub(crate) xray_config_path: String,
    pub(crate) awg_config_path: String,
}

fn default_tls_certificate_path() -> String {
    "/etc/node-plane/tls/server.crt".to_string()
}

fn default_tls_key_path() -> String {
    "/etc/node-plane/tls/server.key".to_string()
}

fn default_tls_client_ca_path() -> String {
    "/etc/node-plane/tls/ca.crt".to_string()
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
            tls_certificate_path: default_tls_certificate_path(),
            tls_key_path: default_tls_key_path(),
            tls_client_ca_path: default_tls_client_ca_path(),
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
    pub(crate) fn load() -> Self {
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

#[cfg(test)]
mod tests {
    use super::AgentConfig;

    #[test]
    fn accepts_installer_agent_config_with_default_runtime_paths() {
        let config: AgentConfig = toml::from_str(
            r#"
node_key = "test-node"
listen_addr = "0.0.0.0:50061"
tls_certificate_path = "/etc/node-plane/tls/server.crt"
tls_key_path = "/etc/node-plane/tls/server.key"
tls_client_ca_path = "/etc/node-plane/tls/ca.crt"
"#,
        )
        .expect("installer config must deserialize");
        assert_eq!(config.node_key, "test-node");
        assert_eq!(config.listen_addr, "0.0.0.0:50061");
        assert_eq!(config.runtime_root, "/opt/node-plane-runtime");
    }
}
