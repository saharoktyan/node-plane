//! AmneziaWG on this node.
pub(crate) mod addresses;
pub(crate) mod client;
pub(crate) mod entropy;
pub(crate) mod inspection;
pub(crate) mod peer_issue;
pub(crate) mod peer_sync;
pub(crate) mod peers;
pub(crate) mod rate;
pub(crate) mod refresh;
pub(crate) mod runtime;
pub(crate) mod settings;
pub(crate) mod setup;
pub(crate) mod traffic;
pub(crate) mod users;

use std::fs;
use std::path::PathBuf;
use std::process::Command;

use crate::agent::v1::RemoteProfileRecord;
use crate::state::AgentState;

pub(crate) fn awg_config_uses_port(raw: &str, port: u32) -> bool {
    raw.lines().any(|line| {
        let line = line.trim();
        !line.starts_with('#')
            && line.split_once('=').is_some_and(|(key, value)| {
                key.trim() == "ListenPort" && value.trim().parse::<u32>().ok() == Some(port)
            })
    })
}

/// The AWG runtime of this node, from node.env.
#[derive(Debug, Clone)]
pub(crate) struct Node {
    pub(crate) config: PathBuf,
    pub(crate) clients: PathBuf,
    pub(crate) container: String,
    pub(crate) interface: String,
    pub(crate) server_key: String,
}

impl Node {
    pub(crate) fn read(setting: super::Setting<'_>) -> Self {
        Self {
            config: setting(
                "AWG_CONFIG",
                "/opt/node-plane-runtime/amnezia-awg/data/wg0.conf",
            )
            .into(),
            clients: setting("AWG_CLIENTS_DIR", "/opt/node-plane-runtime/awg-clients").into(),
            container: setting("AWG_CONTAINER_NAME", "amnezia-awg"),
            interface: setting("AWG_IFACE", "wg0"),
            server_key: setting("SERVER_KEY", ""),
        }
    }

    /// The peer name in wg0.conf and the clients directory.
    pub(crate) fn peer_name(&self, profile: &str) -> String {
        if self.server_key.is_empty() {
            profile.to_string()
        } else {
            format!("{}-{profile}", self.server_key)
        }
    }

    /// `wg show <interface> <what>` in the container.
    pub(crate) fn wg_show(&self, what: &str) -> Result<String, String> {
        let output = Command::new("docker")
            .args(["exec", &self.container, "wg", "show", &self.interface, what])
            .output()
            .map_err(|err| format!("docker failed: {err}"))?;
        if !output.status.success() {
            return Err(format!(
                "wg show failed: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
        Ok(String::from_utf8_lossy(&output.stdout).into_owned())
    }
}

impl AgentState {
    pub(crate) fn extract_awg_payload_json(summary: &str) -> String {
        let vpn_uri = summary
            .lines()
            .map(str::trim)
            .find(|line| line.starts_with("vpn://"))
            .unwrap_or_default()
            .to_string();
        let wg_conf = if let Some(idx) = summary.find("[Interface]") {
            let config_and_key = &summary[idx..];
            let end = config_and_key
                .find("\n===========")
                .unwrap_or(config_and_key.len());
            config_and_key[..end].trim().to_string()
        } else {
            String::new()
        };
        if vpn_uri.is_empty() && wg_conf.is_empty() {
            return String::new();
        }
        serde_json::json!({
            "vpn_uri": vpn_uri,
            "wg_conf": wg_conf,
        })
        .to_string()
    }

    pub(crate) fn awg_profiles(&self) -> Vec<RemoteProfileRecord> {
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
}

#[cfg(test)]
mod tests {
    use crate::state::AgentState;

    #[test]
    fn awg_payload_keeps_vpn_key_out_of_wireguard_config() {
        let summary = "[Interface]\nPrivateKey = client\n\n[Peer]\nPublicKey = server\n\n=========== AMNEZIA TEXT KEY (vpn://) ===========\nvpn://abc123\n=================================================";
        let payload = AgentState::extract_awg_payload_json(summary);
        let value: serde_json::Value = serde_json::from_str(&payload).unwrap();
        assert_eq!(value["vpn_uri"], "vpn://abc123");
        assert_eq!(
            value["wg_conf"],
            "[Interface]\nPrivateKey = client\n\n[Peer]\nPublicKey = server"
        );
    }
}
