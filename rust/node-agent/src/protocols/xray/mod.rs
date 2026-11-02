//! Xray (VLESS) on this node.
pub(crate) mod admission;
pub(crate) mod api;
pub(crate) mod runtime;
pub(crate) mod users;

use std::fs;

use serde_json::Value;

use crate::agent::v1::RemoteProfileRecord;
use crate::state::AgentState;

pub(crate) fn xray_config_uses_port(raw: &str, kind: &str, port: u32) -> bool {
    let tag = match kind {
        "xray" => "",
        "xray_tcp" => "reality-tcp",
        "xray_xhttp" => "reality-xhttp",
        _ => return false,
    };
    let Ok(config) = serde_json::from_str::<Value>(raw) else {
        return false;
    };
    config["inbounds"].as_array().is_some_and(|inbounds| {
        inbounds.iter().any(|inbound| {
            (if kind == "xray" {
                inbound["protocol"] == "vless"
            } else {
                inbound["tag"].as_str() == Some(tag)
            }) && inbound["port"].as_u64() == Some(u64::from(port))
        })
    })
}

impl AgentState {
    pub(crate) fn xray_profiles(&self) -> Vec<RemoteProfileRecord> {
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
}

#[cfg(test)]
mod tests {
    #[test]
    fn generic_xray_diagnostic_ports_recognize_vless_but_not_other_listeners() {
        let raw = r#"{"inbounds":[{"protocol":"vless","tag":"reality-tcp","port":443},{"protocol":"vless","tag":"reality-xhttp","port":8443},{"protocol":"socks","tag":"","port":1080}]}"#;
        assert!(super::xray_config_uses_port(raw, "xray", 443));
        assert!(super::xray_config_uses_port(raw, "xray", 8443));
        assert!(!super::xray_config_uses_port(raw, "xray", 1080));
        assert!(!super::xray_config_uses_port(raw, "xray", 9443));
    }
}
