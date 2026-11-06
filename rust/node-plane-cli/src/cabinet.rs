//! The web cabinet in Workstation: where it is served from and the settings
//! of the controller's own server being edited. Screens live in
//! `tui/actions/cabinet.rs`.
use std::net::SocketAddr;

use serde_json::{Value, json};

pub const DEFAULT_LISTEN: &str = "0.0.0.0:8090";

/// Where the cabinet is served from, as one line.
pub fn place(cabinet: &Value) -> String {
    match cabinet["mode"].as_str() {
        Some("relay") => "Served by: the relay's node, beside the API".into(),
        _ => format!(
            "Served by: the controller, on {}",
            cabinet["listen"].as_str().unwrap_or("—")
        ),
    }
}

/// Settings of the controller's own cabinet server, before they are sent.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Draft {
    pub listen: String,
    pub public_url: String,
}

impl Draft {
    /// A new server, or the current one's settings to change them.
    pub fn from_cabinet(cabinet: &Value) -> Self {
        let local = cabinet["mode"] == "local";
        let text = |key: &str| {
            local
                .then(|| cabinet[key].as_str())
                .flatten()
                .unwrap_or("")
                .to_owned()
        };
        let listen = text("listen");
        Self {
            listen: if listen.is_empty() {
                DEFAULT_LISTEN.into()
            } else {
                listen
            },
            public_url: text("public_url"),
        }
    }

    /// Sets a typed field; the error to show when the value is not usable.
    pub fn set(&mut self, field: &str, value: &str) -> Result<(), &'static str> {
        let value = value.trim();
        match field {
            "listen" => match value.parse::<SocketAddr>() {
                Ok(address) if address.port() != 0 && address.port() != 8080 => {
                    self.listen = address.to_string()
                }
                _ => {
                    return Err(
                        "Enter an IP address and a port, such as 0.0.0.0:8090; 8080 is the API's.",
                    );
                }
            },
            "public_url" => {
                let lower = value.to_ascii_lowercase();
                let host = lower
                    .strip_prefix("https://")
                    .or_else(|| lower.strip_prefix("http://"));
                if host.is_none_or(|host| host.trim_end_matches('/').is_empty())
                    || value.contains(char::is_whitespace)
                {
                    return Err("Enter an address starting with https:// or http://.");
                }
                self.public_url = value.to_owned();
            }
            _ => return Err("Unknown field."),
        }
        Ok(())
    }

    pub fn missing(&self) -> Option<&'static str> {
        self.public_url
            .is_empty()
            .then_some("Enter the address people open the cabinet at.")
    }

    pub fn body(&self) -> Value {
        json!({"mode": "local", "listen": self.listen, "public_url": self.public_url})
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_draft_checks_what_is_typed() {
        let mut draft = Draft::from_cabinet(&Value::Null);
        assert_eq!(draft.listen, DEFAULT_LISTEN);
        assert!(draft.missing().is_some());
        assert!(draft.set("listen", "0.0.0.0:8080").is_err());
        assert!(draft.set("listen", "panel:80").is_err());
        draft.set("listen", " 127.0.0.1:81 ").unwrap();
        assert!(draft.set("public_url", "panel.example.com").is_err());
        assert!(draft.set("public_url", "https://").is_err());
        draft
            .set("public_url", "https://panel.example.com")
            .unwrap();
        assert_eq!(draft.missing(), None);
        assert_eq!(
            draft.body(),
            json!({"mode":"local","listen":"127.0.0.1:81","public_url":"https://panel.example.com"})
        );
    }

    #[test]
    fn current_settings_are_edited_and_the_place_is_named() {
        let local =
            json!({"mode":"local","listen":"0.0.0.0:9000","public_url":"https://p.example.com"});
        assert_eq!(
            Draft::from_cabinet(&local),
            Draft {
                listen: "0.0.0.0:9000".into(),
                public_url: "https://p.example.com".into()
            }
        );
        assert_eq!(place(&local), "Served by: the controller, on 0.0.0.0:9000");
        let relay = json!({"mode":"relay","listen":null});
        assert_eq!(Draft::from_cabinet(&relay).listen, DEFAULT_LISTEN);
        assert_eq!(place(&relay), "Served by: the relay's node, beside the API");
    }
}
