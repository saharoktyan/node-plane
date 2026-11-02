//! Node diagnostics: runtime, configs, CPU and ports.
use std::fs;
use std::path::Path;
use std::process::Command;

use serde_json::Value;

use crate::agent::v1::{CheckPortsRequest, DiagnosticItem, PortCheckSpec, RunDiagnosticsResponse};
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn diagnostics(&self) -> RunDiagnosticsResponse {
        let xray_config_path = self.node_env_value("XRAY_CONFIG", &self.config.xray_config_path);
        let awg_config_path = self.node_env_value("AWG_CONFIG", &self.config.awg_config_path);
        let xray_exists = Path::new(&xray_config_path).is_file();
        let awg_exists = Path::new(&awg_config_path).is_file();
        let xray_reusable = xray_exists
            && fs::read_to_string(&xray_config_path)
                .ok()
                .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
                .and_then(|config| config.get("inbounds")?.as_array().cloned())
                .is_some_and(|inbounds| {
                    inbounds.iter().any(|inbound| {
                        inbound.get("protocol").and_then(Value::as_str) == Some("vless")
                    })
                });
        let awg_reusable = awg_exists
            && fs::read_to_string(&awg_config_path).is_ok_and(|raw| {
                let mut in_interface = false;
                let mut has_private_key = false;
                let mut has_listen_port = false;
                for line in raw.lines().map(str::trim) {
                    if line.starts_with('[') {
                        in_interface = line.eq_ignore_ascii_case("[Interface]");
                    } else if in_interface {
                        if let Some(value) = line
                            .strip_prefix("PrivateKey")
                            .and_then(|rest| rest.trim_start().strip_prefix('='))
                        {
                            has_private_key = !value.trim().is_empty();
                        }
                        if let Some(value) = line
                            .strip_prefix("ListenPort")
                            .and_then(|rest| rest.trim_start().strip_prefix('='))
                        {
                            has_listen_port =
                                value.trim().parse::<u16>().is_ok_and(|port| port > 0);
                        }
                    }
                }
                has_private_key && has_listen_port
            });
        let runtime_root_exists = Path::new(&self.config.runtime_root).is_dir();
        let version = Self::read_first_line(&self.runtime_version_path());
        let commit = Self::read_first_line(&self.runtime_commit_path());

        let docker_available = Command::new("docker")
            .arg("info")
            .output()
            .map(|output| output.status.success())
            .unwrap_or(false)
            || Command::new("sudo")
                .args(["-n", "docker", "info"])
                .output()
                .map(|output| output.status.success())
                .unwrap_or(false);

        let mut items = vec![
            DiagnosticItem {
                kind: "docker".to_string(),
                status: if docker_available { "ok" } else { "missing" }.to_string(),
                summary: if docker_available {
                    "Docker daemon available"
                } else {
                    "Docker daemon unavailable"
                }
                .to_string(),
                detail: String::new(),
            },
            DiagnosticItem {
                kind: "runtime_root".to_string(),
                status: if runtime_root_exists { "ok" } else { "missing" }.to_string(),
                summary: self.config.runtime_root.clone(),
                detail: String::new(),
            },
            DiagnosticItem {
                kind: "xray_config".to_string(),
                status: if !xray_exists {
                    "missing"
                } else if xray_reusable {
                    "ok"
                } else {
                    "invalid"
                }
                .to_string(),
                summary: xray_config_path.clone(),
                detail: String::new(),
            },
            DiagnosticItem {
                kind: "awg_config".to_string(),
                status: if !awg_exists {
                    "missing"
                } else if awg_reusable {
                    "ok"
                } else {
                    "invalid"
                }
                .to_string(),
                summary: awg_config_path.clone(),
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

        let cpu = self.cpu_metrics();
        let (status, summary) = if let Some(value) = cpu["cpu_used_percent"].as_f64() {
            (
                if value >= 95.0 {
                    "error"
                } else if value >= 85.0 {
                    "warning"
                } else {
                    "ok"
                },
                format!("CPU used (1-minute average): {value:.1}%; warning ≥85%, error ≥95%"),
            )
        } else {
            (
                "warning",
                "Fresh CPU average unavailable; one minute of uninterrupted samples is required"
                    .into(),
            )
        };
        items.push(DiagnosticItem {
            kind: "cpu_used_percent".into(),
            status: status.into(),
            summary,
            detail: String::new(),
        });

        let ports = diagnostic_ports(
            &fs::read_to_string(&xray_config_path).unwrap_or_default(),
            &fs::read_to_string(&awg_config_path).unwrap_or_default(),
        );
        for port in self.check_ports(CheckPortsRequest { items: ports }).items {
            items.push(DiagnosticItem {
                kind: format!(
                    "port:{}:{}",
                    if port.kind == "awg" { "udp" } else { "tcp" },
                    port.port
                ),
                status: match port.status.as_str() {
                    "managed" => "ok",
                    "free" => "warning",
                    _ => "error",
                }
                .into(),
                summary: port.summary,
                detail: port.detail,
            });
        }
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
}

pub(crate) fn diagnostic_ports(xray: &str, awg: &str) -> Vec<PortCheckSpec> {
    let mut ports = std::collections::BTreeSet::new();
    if let Ok(config) = serde_json::from_str::<Value>(xray)
        && let Some(inbounds) = config["inbounds"].as_array()
    {
        for inbound in inbounds {
            if inbound["protocol"] == "vless"
                && let Some(port) = inbound["port"].as_u64().filter(|p| *p > 0 && *p <= 65535)
            {
                ports.insert(("xray".to_string(), port as u32));
            }
        }
    }
    let mut interface = false;
    for line in awg.lines().map(str::trim) {
        if line.starts_with('[') {
            interface = line.eq_ignore_ascii_case("[Interface]");
        } else if interface
            && let Some((key, value)) = line.split_once('=')
            && key.trim() == "ListenPort"
            && let Ok(port) = value.trim().parse::<u16>()
            && port > 0
        {
            ports.insert(("awg".to_string(), u32::from(port)));
        }
    }
    ports
        .into_iter()
        .map(|(kind, port)| PortCheckSpec { kind, port })
        .collect()
}

#[cfg(test)]
mod tests {
    use crate::config::AgentConfig;
    use crate::state::AgentState;
    use std::{
        fs, process,
        time::{SystemTime, UNIX_EPOCH},
    };

    #[test]
    fn diagnostic_ports_use_configured_protocol_ports_and_ignore_peers() {
        let ports = super::diagnostic_ports(
            r#"{"inbounds":[{"protocol":"vless","port":443},{"protocol":"vless","port":443},{"protocol":"socks","port":1080}]}"#,
            "[Interface]\nListenPort = 51820\n[Peer]\nListenPort = 1111",
        );
        assert_eq!(ports.len(), 2);
        assert!(ports.iter().any(|p| p.kind == "awg" && p.port == 51820));
        assert!(ports.iter().any(|p| p.kind == "xray" && p.port == 443));
    }

    #[test]
    fn diagnostics_and_runtime_facts_use_node_env_config_paths() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("node-plane-agent-{}-{nonce}", process::id()));
        fs::create_dir_all(&root).unwrap();
        let xray = root.join("custom-xray.json");
        let awg = root.join("custom-awg.conf");
        let env_path = root.join("node.env");
        fs::write(&xray, r#"{"inbounds":[{"protocol":"vless"}]}"#).unwrap();
        fs::write(
            &awg,
            "[Interface]\nPrivateKey = test-key\nListenPort = 51820\n",
        )
        .unwrap();
        fs::write(
            &env_path,
            format!(
                "XRAY_CONFIG={}\nAWG_CONFIG={}\n",
                xray.display(),
                awg.display()
            ),
        )
        .unwrap();

        let config = AgentConfig {
            runtime_root: root.display().to_string(),
            node_env_path: env_path.display().to_string(),
            ..AgentConfig::default()
        };
        let state = AgentState::new(config);
        let facts = state.runtime_facts();
        assert!(facts.xray_config_present);
        assert!(facts.awg_config_present);
        assert_eq!(facts.xray_config_path, xray.display().to_string());
        let items = state.diagnostics().items;
        assert_eq!(
            items
                .iter()
                .find(|item| item.kind == "xray_config")
                .unwrap()
                .status,
            "ok"
        );
        assert_eq!(
            items
                .iter()
                .find(|item| item.kind == "awg_config")
                .unwrap()
                .status,
            "ok"
        );
        fs::write(&xray, "{}").unwrap();
        fs::write(&awg, "[Interface]\n").unwrap();
        let items = state.diagnostics().items;
        assert_eq!(
            items
                .iter()
                .find(|item| item.kind == "xray_config")
                .unwrap()
                .status,
            "invalid"
        );
        assert_eq!(
            items
                .iter()
                .find(|item| item.kind == "awg_config")
                .unwrap()
                .status,
            "invalid"
        );
        fs::remove_dir_all(root).unwrap();
    }
}
