//! Reversible server settings drafts, bound to the revision loaded from the backend.
use anyhow::{Result, ensure};
use serde_json::{Value, json};
use uuid::Uuid;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tab {
    General,
    Protocols,
    Advanced,
}
impl Tab {
    pub const ALL: [Self; 3] = [Self::General, Self::Protocols, Self::Advanced];
    pub fn label(self) -> &'static str {
        match self {
            Self::General => "General",
            Self::Protocols => "Protocols",
            Self::Advanced => "Advanced options",
        }
    }
}
pub struct Field {
    pub key: &'static str,
    pub label: &'static str,
    pub value: String,
    pub baseline: String,
    pub setting: bool,
}
#[derive(Clone, Debug)]
pub struct Draft {
    pub node: String,
    pub revision: u64,
    pub body: Value,
    pub key: Uuid,
    pub apply_key: Uuid,
}
pub struct Editor {
    pub source: Value,
    pub fields: Vec<Field>,
    pub protocols: Vec<String>,
    pub transports: Vec<String>,
    pub tab: Tab,
    pub page: usize,
    pub key: Uuid,
    pub apply_key: Uuid,
    pub review: Option<(Draft, Value)>,
}
impl Editor {
    pub fn new(source: Value, defaults: &Value) -> Result<Self> {
        ensure!(
            source["key"].is_string() && source["desired_revision"].as_u64().is_some(),
            "Invalid server settings"
        );
        let mut fields = Vec::new();
        for (key, label, setting) in [
            ("title", "Server name", false),
            ("region", "Region", false),
            ("flag", "Flag", false),
            ("public_host", "Public address", true),
            ("ssh_target", "SSH address", false),
            ("notes", "Notes", false),
            ("xray_host", "VLESS public address", true),
            ("xray_sni", "Xray SNI", true),
            ("xray_tcp_port", "TCP port", true),
            ("xray_xhttp_port", "XHTTP port", true),
            ("xray_xhttp_path", "XHTTP path", true),
            ("xray_fingerprint", "Xray fingerprint", true),
            ("awg_public_host", "AWG public address", true),
            ("awg_port", "AWG port", true),
            ("awg_port_mode", "AWG port mode", true),
            ("awg_i1_preset", "AWG I-parameters", true),
        ] {
            let value = if setting {
                &source["settings"][key]
            } else {
                &source[key]
            };
            let value = if value.is_null() && setting {
                &defaults["settings"][key]
            } else {
                value
            };
            let value = match value {
                Value::String(s) => s.clone(),
                Value::Number(n) => n.to_string(),
                _ => String::new(),
            };
            let value = if value.is_empty() {
                match key {
                    "xray_sni" => "www.cloudflare.com",
                    "xray_tcp_port" => "443",
                    "xray_xhttp_port" => "8443",
                    "xray_xhttp_path" => "/assets",
                    "xray_fingerprint" => "chrome",
                    "awg_i1_preset" => "quic",
                    "awg_port_mode" => "auto",
                    "awg_port" => "443",
                    _ => "",
                }
                .to_owned()
            } else {
                value
            };
            let value = if value.is_empty() && matches!(key, "xray_host" | "awg_public_host") {
                source["settings"]["public_host"]
                    .as_str()
                    .unwrap_or("")
                    .into()
            } else {
                value
            };
            fields.push(Field {
                key,
                label,
                baseline: value.clone(),
                value,
                setting,
            });
        }
        Ok(Self {
            protocols: serde_json::from_value(source["protocols"].clone())?,
            transports: serde_json::from_value(source["xray_transports"].clone())?,
            source,
            fields,
            tab: Tab::General,
            page: 0,
            key: Uuid::new_v4(),
            apply_key: Uuid::new_v4(),
            review: None,
        })
    }
    pub fn dirty(&self) -> bool {
        self.fields.iter().any(|f| f.value != f.baseline)
            || !same_selection(&self.protocols, &self.source["protocols"])
            || !same_selection(&self.transports, &self.source["xray_transports"])
    }
    pub fn changed(&mut self) {
        self.key = Uuid::new_v4();
        self.apply_key = Uuid::new_v4();
        self.review = None;
    }
    pub fn field_indices(&self) -> Vec<usize> {
        self.fields
            .iter()
            .enumerate()
            .filter(|(_, field)| match self.tab {
                Tab::General => {
                    (!field.setting || field.key == "public_host")
                        && (field.key != "ssh_target" || self.source["transport"] == "ssh")
                }
                Tab::Protocols => false,
                Tab::Advanced => {
                    field.setting
                        && field.key != "public_host"
                        && (if field.key.starts_with("xray") {
                            self.protocols.iter().any(|p| p == "xray")
                        } else {
                            self.protocols.iter().any(|p| p == "awg")
                        })
                }
            })
            .map(|(index, _)| index)
            .collect()
    }
    pub fn choices(key: &str) -> Option<&'static [&'static str]> {
        match key {
            "awg_i1_preset" => Some(&["quic", "dns", "chaos"]),
            "awg_port_mode" => Some(&["auto", "manual"]),
            "xray_fingerprint" => Some(&[
                "chrome",
                "firefox",
                "safari",
                "ios",
                "android",
                "edge",
                "random",
                "randomized",
            ]),
            _ => None,
        }
    }
    pub fn toggle(&mut self, protocol: &str, transport: bool) {
        let items = if transport {
            &mut self.transports
        } else {
            &mut self.protocols
        };
        if items.iter().any(|v| v == protocol) {
            items.retain(|v| v != protocol);
        } else {
            items.push(protocol.into());
        }
        if !transport && protocol == "xray" {
            if self.protocols.iter().any(|v| v == "xray") && self.transports.is_empty() {
                self.transports = serde_json::from_value(self.source["xray_transports"].clone())
                    .unwrap_or_default();
                if self.transports.is_empty() {
                    self.transports = vec!["tcp".into(), "xhttp".into()];
                }
            }
            if !self.protocols.iter().any(|v| v == "xray") {
                self.transports.clear();
            }
        }
        self.changed();
    }
    pub fn reset(&mut self) {
        for field in &mut self.fields {
            field.value = field.baseline.clone();
        }
        self.protocols =
            serde_json::from_value(self.source["protocols"].clone()).unwrap_or_default();
        self.transports =
            serde_json::from_value(self.source["xray_transports"].clone()).unwrap_or_default();
        self.changed();
    }
    pub fn draft(&self) -> Result<Draft> {
        let mut body = json!({});
        let mut settings = self.source["settings"]
            .as_object()
            .cloned()
            .unwrap_or_default();
        let protocols_changed = !same_selection(&self.protocols, &self.source["protocols"]);
        let mut settings_changed = false;
        for field in &self.fields {
            let newly_enabled = field.setting
                && ["xray", "awg"].iter().any(|p| {
                    field.key.starts_with(p)
                        && self.protocols.iter().any(|v| v == p)
                        && !self.source["protocols"]
                            .as_array()
                            .is_some_and(|values| values.iter().any(|v| v == p))
                });
            if field.value == field.baseline && !newly_enabled {
                continue;
            }
            if field.setting {
                let input = if newly_enabled
                    && field.value.is_empty()
                    && matches!(field.key, "xray_host" | "awg_public_host")
                {
                    self.fields
                        .iter()
                        .find(|f| f.key == "public_host")
                        .map(|f| f.value.as_str())
                        .unwrap_or("")
                } else {
                    field.value.as_str()
                };
                ensure!(!input.trim().is_empty(), "{} is required", field.label);
                let value = if field.key.ends_with("_port") {
                    json!(
                        input
                            .trim()
                            .parse::<u16>()
                            .ok()
                            .filter(|p| *p > 0)
                            .ok_or_else(|| anyhow::anyhow!(
                                "{} must be between 1 and 65535",
                                field.label
                            ))?
                    )
                } else {
                    json!(input.trim())
                };
                settings.insert(field.key.into(), value);
                settings_changed = true;
            } else {
                if matches!(field.key, "title" | "region" | "ssh_target") {
                    ensure!(
                        !field.value.trim().is_empty(),
                        "{} is required",
                        field.label
                    );
                }
                body[field.key] = json!(field.value.trim());
            }
        }
        if settings_changed {
            body["settings"] = json!(settings);
        }
        if protocols_changed {
            body["protocols"] = json!(self.protocols);
        }
        if !same_selection(&self.transports, &self.source["xray_transports"]) {
            body["xray_transports"] = json!(self.transports);
        }
        ensure!(
            !self.protocols.iter().any(|p| p == "xray") || !self.transports.is_empty(),
            "Select at least one VLESS transport"
        );
        ensure!(!body.as_object().unwrap().is_empty(), "No changes to save");
        Ok(Draft {
            node: self.source["key"].as_str().unwrap().into(),
            revision: self.source["desired_revision"].as_u64().unwrap(),
            body,
            key: self.key,
            apply_key: self.apply_key,
        })
    }
}
fn same_selection(items: &[String], source: &Value) -> bool {
    source.as_array().is_some_and(|values| {
        items.len() == values.len() && items.iter().all(|v| values.iter().any(|value| value == v))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn source() -> Value {
        json!({"key":"lv1","title":"Latvia","region":"Europe","flag":"LV","notes":"", "transport":"ssh", "ssh_target":"root@host.test", "desired_revision":9,"applied_revision":9,"protocols":["awg"],"xray_transports":[],"settings":{"public_host":"vpn.test", "awg_port":443,"awg_port_mode":"auto","awg_i1_preset":"quic","awg_public_host":"vpn.test","awg_interface":"wg0"}})
    }
    fn defaults() -> Value {
        json!({"settings":{"xray_sni":"www.cloudflare.com","xray_tcp_port":443,"xray_xhttp_port":8443,"xray_xhttp_path":"/assets","xray_fingerprint":"chrome"}})
    }
    #[test]
    fn metadata_draft_contains_only_changes_and_keeps_revision_and_retry_key() {
        let mut editor = Editor::new(source(), &defaults()).unwrap();
        assert!(!editor.dirty());
        assert!(editor.draft().is_err());
        editor.fields[0].value = "New name".into();
        editor.changed();
        let first = editor.draft().unwrap();
        let second = editor.draft().unwrap();
        assert_eq!(first.body, json!({"title":"New name"}));
        assert_eq!(first.revision, 9);
        assert_eq!(first.key, second.key);
        assert_eq!(first.apply_key, second.apply_key);
        editor.reset();
        assert!(!editor.dirty());
        assert!(editor.review.is_none());
    }
    #[test]
    fn new_protocol_inherits_defaults_and_public_host_but_existing_settings_are_preserved() {
        let mut editor = Editor::new(source(), &defaults()).unwrap();
        editor.toggle("xray", false);
        let draft = editor.draft().unwrap();
        assert_eq!(draft.body["protocols"], json!(["awg", "xray"]));
        assert_eq!(draft.body["xray_transports"], json!(["tcp", "xhttp"]));
        assert_eq!(draft.body["settings"]["xray_host"], "vpn.test");
        assert_eq!(draft.body["settings"]["xray_tcp_port"], 443);
        assert_eq!(draft.body["settings"]["awg_interface"], "wg0");
        editor.toggle("xray", false);
        assert!(!editor.dirty());
    }
    #[test]
    fn invalid_port_or_missing_transport_does_not_produce_a_save() {
        let mut editor = Editor::new(source(), &defaults()).unwrap();
        editor
            .fields
            .iter_mut()
            .find(|f| f.key == "awg_port")
            .unwrap()
            .value = "65536".into();
        assert!(editor.dirty());
        assert!(editor.draft().is_err());
        editor.reset();
        editor.toggle("xray", false);
        editor.transports.clear();
        assert!(editor.draft().is_err());
    }
    #[test]
    fn unchanged_advanced_defaults_do_not_leak_into_metadata_edits() {
        let mut editor = Editor::new(source(), &defaults()).unwrap();
        editor.fields[1].value = "Asia".into();
        assert_eq!(editor.draft().unwrap().body, json!({"region":"Asia"}));
        editor.tab = Tab::Advanced;
        assert!(
            editor
                .field_indices()
                .iter()
                .all(|i| editor.fields[*i].key.starts_with("awg"))
        );
    }
}
