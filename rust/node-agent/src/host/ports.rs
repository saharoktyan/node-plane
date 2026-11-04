//! Port availability checks and firewall opening.
use std::fs;
use std::net::{TcpListener, UdpSocket};
use std::process::Command;

use tonic::Status;

use crate::agent::v1::{
    CheckPortsRequest, CheckPortsResponse, OpenPortsRequest, OpenPortsResponse, PortStatus,
};
use crate::protocols::awg::awg_config_uses_port;
use crate::protocols::xray::xray_config_uses_port;
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn port_is_managed(&self, kind: &str, port: u32) -> bool {
        match kind {
            "xray" | "xray_tcp" | "xray_xhttp" => {
                let container = self.node_env_value("XRAY_CONTAINER_NAME", "node-plane-xray");
                let config_path = self.node_env_value("XRAY_CONFIG", &self.config.xray_config_path);
                Self::container_running(&container)
                    && fs::read_to_string(&config_path)
                        .is_ok_and(|raw| xray_config_uses_port(&raw, kind, port))
            }
            "awg" => {
                let container = self.node_env_value("AWG_CONTAINER_NAME", "node-plane-awg");
                let config_path = self.node_env_value("AWG_CONFIG", &self.config.awg_config_path);
                Self::container_running(&container)
                    && fs::read_to_string(&config_path)
                        .is_ok_and(|raw| awg_config_uses_port(&raw, port))
            }
            _ => false,
        }
    }

    pub(crate) fn check_ports(&self, request: CheckPortsRequest) -> CheckPortsResponse {
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
            let bind_result = if kind == "awg" {
                UdpSocket::bind(&bind_addr).map(|socket| drop(socket))
            } else {
                TcpListener::bind(&bind_addr).map(|listener| drop(listener))
            };
            match bind_result {
                Ok(()) => {
                    items.push(PortStatus {
                        kind,
                        port,
                        status: "free".to_string(),
                        summary: format!("port {port} is free"),
                        detail: String::new(),
                    });
                }
                Err(err) => {
                    let managed = self.port_is_managed(&kind, port);
                    items.push(PortStatus {
                        kind,
                        port,
                        status: if managed { "managed" } else { "busy" }.to_string(),
                        summary: if managed {
                            format!("port {port} is used by this node's managed runtime")
                        } else {
                            format!("port {port} is not available")
                        },
                        detail: err.to_string(),
                    });
                }
            }
        }
        let busy = items.iter().filter(|item| item.status == "busy").count();
        let invalid = items.iter().filter(|item| item.status == "invalid").count();
        let free = items.iter().filter(|item| item.status == "free").count();
        let summary = format!(
            "checked={} free={} busy={} invalid={invalid}",
            items.len(),
            free,
            busy
        );
        CheckPortsResponse { summary, items }
    }

    pub(crate) fn open_ports(
        &self,
        request: OpenPortsRequest,
    ) -> Result<OpenPortsResponse, Status> {
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
        let summary = format!(
            "requested={} opened={} failed={failed}",
            items.len(),
            opened
        );
        Ok(OpenPortsResponse { summary, items })
    }
}

#[cfg(test)]
mod tests {
    use crate::protocols::awg::awg_config_uses_port;
    use crate::protocols::xray::xray_config_uses_port;

    #[test]
    fn recognizes_ports_owned_by_managed_runtime_configs() {
        let xray = r#"{"inbounds":[{"tag":"reality-tcp","port":443},{"tag":"reality-xhttp","port":8443}]}"#;
        assert!(xray_config_uses_port(xray, "xray_tcp", 443));
        assert!(xray_config_uses_port(xray, "xray_xhttp", 8443));
        assert!(!xray_config_uses_port(xray, "xray_tcp", 9443));
        assert!(!xray_config_uses_port(xray, "awg", 443));
        assert!(awg_config_uses_port(
            "[Interface]\nListenPort = 51820\n",
            51820
        ));
        assert!(!awg_config_uses_port(
            "[Interface]\nListenPort = 51820\n",
            51821
        ));
    }
}
