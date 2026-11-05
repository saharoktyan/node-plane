//! The web cabinet's relay in Workstation: what the controller reports and
//! the settings being edited. Screens live in `tui/actions/relay.rs`.
use serde_json::{Value, json};

pub const TLS_MODES: [&str; 3] = ["acme", "files", "proxy"];

pub fn tls_label(mode: &str) -> &'static str {
    match mode {
        "acme" => "Let's Encrypt (port 443)",
        "files" => "Certificate files on the server",
        "proxy" => "Behind a reverse proxy",
        _ => "Unknown",
    }
}

pub fn status_label(status: &str) -> &'static str {
    match status {
        "applying" => "Applying",
        "active" => "Active",
        "failed" => "Failed",
        "removing" => "Removing",
        _ => "Unknown",
    }
}

/// The link line: whether the controller is attached to the relay now.
pub fn link_label(relay: &Value) -> String {
    let link = &relay["link"];
    if !link.is_object() {
        return "Link: not checked".into();
    }
    if link["agent_reachable"] != true {
        return "Link: the node's agent does not answer".into();
    }
    if link["running"] != true {
        return "Link: the relay is not running on the node".into();
    }
    if link["connected"] == true {
        return "Link: connected".into();
    }
    match link["error"].as_str().filter(|e| !e.is_empty()) {
        Some(error) => format!("Link: not connected · {error}"),
        None => "Link: not connected".into(),
    }
}

/// Relay settings being edited, before they are sent.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Draft {
    pub node_key: String,
    pub node_title: String,
    pub domain: String,
    pub port: String,
    pub tls: &'static str,
    pub acme_contact: String,
    pub certificate_path: String,
    pub key_path: String,
    /// Whether the node may still be chosen: only for a new relay set up
    /// from the controller's settings.
    pub node_choice: bool,
}

impl Draft {
    pub fn new(node_key: String, node_title: String, node_choice: bool) -> Self {
        Self {
            node_key,
            node_title,
            domain: String::new(),
            port: "443".into(),
            tls: "acme",
            acme_contact: String::new(),
            certificate_path: String::new(),
            key_path: String::new(),
            node_choice,
        }
    }

    /// The current settings of a relay, to change them.
    pub fn from_relay(relay: &Value, node_title: String) -> Self {
        let text = |key: &str| relay[key].as_str().unwrap_or("").to_owned();
        Self {
            node_key: text("node_key"),
            node_title,
            domain: text("domain"),
            port: relay["port"].as_u64().unwrap_or(443).to_string(),
            tls: TLS_MODES
                .into_iter()
                .find(|mode| relay["tls"] == *mode)
                .unwrap_or("acme"),
            acme_contact: text("acme_contact"),
            certificate_path: text("certificate_path"),
            key_path: text("key_path"),
            node_choice: false,
        }
    }

    pub fn next_tls(&mut self) {
        let index = TLS_MODES.iter().position(|m| *m == self.tls).unwrap_or(0);
        self.tls = TLS_MODES[(index + 1) % TLS_MODES.len()];
        if self.tls == "acme" {
            // Let's Encrypt checks the domain on port 443.
            self.port = "443".into();
        }
    }

    /// Sets a typed field; the error to show when the value is not usable.
    pub fn set(&mut self, field: &str, value: &str) -> Result<(), &'static str> {
        let value = value.trim();
        match field {
            "domain" => {
                let domain = value.to_ascii_lowercase();
                if !domain.contains('.')
                    || domain.len() > 253
                    || domain.split('.').any(|label| {
                        label.is_empty()
                            || label.starts_with('-')
                            || label.ends_with('-')
                            || !label
                                .bytes()
                                .all(|c| c.is_ascii_alphanumeric() || c == b'-')
                    })
                {
                    return Err("Enter a domain such as cab.example.com.");
                }
                self.domain = domain;
            }
            "port" => match value.parse::<u16>() {
                Ok(port) if port > 0 => {
                    if self.tls == "acme" && port != 443 {
                        return Err(
                            "Let's Encrypt needs port 443; choose another TLS mode for other ports.",
                        );
                    }
                    self.port = port.to_string();
                }
                _ => return Err("Enter a port from 1 to 65535."),
            },
            "acme_contact" => {
                if !value.is_empty() && !value.contains('@') {
                    return Err("Enter an email address, or leave it empty.");
                }
                self.acme_contact = value.to_owned();
            }
            "certificate_path" | "key_path" => {
                if !value.starts_with('/') {
                    return Err("Enter an absolute path on the server.");
                }
                if field == "certificate_path" {
                    self.certificate_path = value.to_owned();
                } else {
                    self.key_path = value.to_owned();
                }
            }
            _ => return Err("Unknown field."),
        }
        Ok(())
    }

    /// What is still missing before the settings can be sent.
    pub fn missing(&self) -> Option<&'static str> {
        if self.node_key.is_empty() {
            Some("Choose a node with an installed agent.")
        } else if self.domain.is_empty() {
            Some("Enter the cabinet's domain.")
        } else if self.tls == "files"
            && (self.certificate_path.is_empty() || self.key_path.is_empty())
        {
            Some("Enter the certificate and key paths on the server.")
        } else {
            None
        }
    }

    pub fn body(&self) -> Value {
        let mut body = json!({
            "node_key": self.node_key,
            "domain": self.domain,
            "port": self.port.parse::<u16>().unwrap_or(443),
            "tls": self.tls,
        });
        match self.tls {
            "acme" if !self.acme_contact.is_empty() => {
                body["acme_contact"] = json!(self.acme_contact)
            }
            "files" => {
                body["certificate_path"] = json!(self.certificate_path);
                body["key_path"] = json!(self.key_path);
            }
            _ => {}
        }
        body
    }
}

/// The nodes a relay can run on: those with an installed agent.
pub fn candidates(nodes: &Value) -> Vec<(String, String)> {
    nodes["items"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|node| node["stage"] == "node")
        .filter_map(|node| {
            Some((
                node["key"].as_str()?.to_owned(),
                format!(
                    "{} {}",
                    node["flag"].as_str().unwrap_or(""),
                    node["title"].as_str().unwrap_or("")
                )
                .trim()
                .to_owned(),
            ))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_draft_checks_what_is_typed() {
        let mut draft = Draft::new("n1".into(), "Node".into(), true);
        assert_eq!(draft.missing(), Some("Enter the cabinet's domain."));
        assert!(draft.set("domain", "localhost").is_err());
        assert!(draft.set("domain", "-a.example").is_err());
        draft.set("domain", " Cab.Example.com ").unwrap();
        assert_eq!(draft.domain, "cab.example.com");
        assert!(draft.set("port", "8443").is_err(), "ACME keeps 443");
        draft.next_tls();
        assert_eq!(draft.tls, "files");
        draft.set("port", "8443").unwrap();
        assert_eq!(
            draft.missing(),
            Some("Enter the certificate and key paths on the server.")
        );
        assert!(draft.set("certificate_path", "cert.pem").is_err());
        draft.set("certificate_path", "/etc/ssl/c.pem").unwrap();
        draft.set("key_path", "/etc/ssl/c.key").unwrap();
        assert_eq!(draft.missing(), None);
        assert_eq!(
            draft.body(),
            json!({"node_key":"n1","domain":"cab.example.com","port":8443,"tls":"files",
                   "certificate_path":"/etc/ssl/c.pem","key_path":"/etc/ssl/c.key"})
        );
        draft.next_tls();
        draft.next_tls();
        assert_eq!((draft.tls, draft.port.as_str()), ("acme", "443"));
        draft.set("acme_contact", "ops@example.com").unwrap();
        assert_eq!(draft.body()["acme_contact"], "ops@example.com");
    }

    #[test]
    fn the_link_line_says_what_is_wrong() {
        let relay = |link: Value| json!({"link": link});
        assert_eq!(link_label(&json!({})), "Link: not checked");
        assert_eq!(
            link_label(&relay(json!({"agent_reachable":false}))),
            "Link: the node's agent does not answer"
        );
        assert_eq!(
            link_label(&relay(
                json!({"agent_reachable":true,"running":true,"connected":true})
            )),
            "Link: connected"
        );
        assert_eq!(
            link_label(&relay(
                json!({"agent_reachable":true,"running":true,"connected":false,"error":"dns"})
            )),
            "Link: not connected · dns"
        );
    }

    #[test]
    fn existing_settings_are_edited_in_place() {
        let relay = json!({"node_key":"n1","domain":"cab.example.com","port":9443,"tls":"proxy"});
        let draft = Draft::from_relay(&relay, "Node".into());
        assert_eq!(
            (draft.tls, draft.port.as_str(), draft.node_choice),
            ("proxy", "9443", false)
        );
        let nodes = json!({"items":[{"key":"a","title":"A","flag":"🇩🇪","stage":"node"},
                                    {"key":"b","title":"B","stage":"invitation"}]});
        assert_eq!(candidates(&nodes), [("a".to_string(), "🇩🇪 A".to_string())]);
    }
}
