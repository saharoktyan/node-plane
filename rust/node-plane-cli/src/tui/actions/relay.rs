//! The web cabinet's relay in Workstation. One page with two ways in:
//! Settings › Basic › Relay and the node card. The page says which way it
//! was opened, and Back returns there.
use super::super::*;
use crate::controller::Command;
use crate::relay::{Draft, candidates, link_label, status_label, tls_label};

impl App {
    /// Opens the relay page; `origin` is the node card it opens from.
    pub(in crate::tui) fn open_relay(&mut self, origin: Option<(String, String)>) {
        if !self.actions_enabled() {
            return;
        }
        let profile = self.form.saved.as_ref().map(|p| p.id);
        if self.controller.profile != profile {
            self.controller = crate::controller::Browser {
                profile,
                ..Default::default()
            };
        }
        self.controller_navigation_revision = self.controller_navigation_revision.wrapping_add(1);
        self.controller.scope = None;
        self.controller.defaults_input = None;
        self.controller.relay = Some(serde_json::json!({"loading": true}));
        self.controller.relay_draft = None;
        self.controller.relay_nodes = None;
        self.controller.relay_confirm_remove = false;
        self.controller.relay_origin = origin;
        self.controller.selected = 0;
        self.controller_viewport.set(0);
        self.controller_section = Some(ControllerSection::Installation);
        self.requests_settings = false;
        self.screen = Screen::Controller;
        self.quick_focus = false;
        self.controller_sidebar = false;
        self.error.clear();
        self.controller_command(Command::Relay);
    }

    pub(in crate::tui) fn relay_view_open(&self) -> bool {
        self.controller.relay.is_some() && self.controller.defaults_input.is_none()
    }

    /// Where the page was opened from, as its first line.
    fn relay_path(&self) -> String {
        match &self.controller.relay_origin {
            Some((_, title)) => format!("Nodes › {title} › Relay settings"),
            None => "Settings › Basic › Relay".into(),
        }
    }

    fn relay_node_title(&self, key: &str) -> String {
        if let Some((origin, title)) = &self.controller.relay_origin
            && origin == key
        {
            return title.clone();
        }
        self.controller
            .relay_nodes
            .as_ref()
            .and_then(|nodes| candidates(nodes).into_iter().find(|(k, _)| k == key))
            .map_or_else(|| key.to_owned(), |(_, title)| title)
    }

    /// Handles a relay control; false for any other control.
    pub(in crate::tui) fn activate_relay(&mut self, control: Control) -> bool {
        match control {
            Control::ControllerRelay => self.open_relay(None),
            Control::ControllerRelayRefresh => self.controller_command(Command::Relay),
            Control::ControllerRelaySetup => {
                let current = self
                    .controller
                    .relay
                    .as_ref()
                    .map(|v| v["relay"].clone())
                    .filter(serde_json::Value::is_object);
                self.controller.relay_draft = Some(match current {
                    Some(relay) => {
                        let title = self.relay_node_title(relay["node_key"].as_str().unwrap_or(""));
                        Draft::from_relay(&relay, title)
                    }
                    None => match self.controller.relay_origin.clone() {
                        Some((key, title)) => Draft::new(key, title, false),
                        None => Draft::new(String::new(), String::new(), true),
                    },
                });
                self.controller.relay_confirm_remove = false;
                self.controller.selected = 0;
                if self
                    .controller
                    .relay_draft
                    .as_ref()
                    .is_some_and(|d| d.node_choice)
                {
                    self.controller_command(Command::RelayNodes);
                }
            }
            Control::ControllerRelayNode => {
                let nodes = self
                    .controller
                    .relay_nodes
                    .as_ref()
                    .map(candidates)
                    .unwrap_or_default();
                if let Some(draft) = &mut self.controller.relay_draft
                    && draft.node_choice
                    && !nodes.is_empty()
                {
                    let next = nodes
                        .iter()
                        .position(|(key, _)| *key == draft.node_key)
                        .map_or(0, |index| (index + 1) % nodes.len());
                    (draft.node_key, draft.node_title) = nodes[next].clone();
                }
            }
            Control::ControllerRelayTls => {
                if let Some(draft) = &mut self.controller.relay_draft {
                    draft.next_tls();
                }
            }
            Control::ControllerRelayField(field) => {
                if let Some(draft) = &self.controller.relay_draft {
                    let value = match field {
                        "domain" => draft.domain.clone(),
                        "port" => draft.port.clone(),
                        "acme_contact" => draft.acme_contact.clone(),
                        "certificate_path" => draft.certificate_path.clone(),
                        _ => draft.key_path.clone(),
                    };
                    let key = match field {
                        "domain" => "relay_domain",
                        "port" => "relay_port",
                        "acme_contact" => "relay_acme_contact",
                        "certificate_path" => "relay_certificate_path",
                        _ => "relay_key_path",
                    };
                    self.controller.defaults_input = Some((key, value));
                }
            }
            Control::ControllerRelaySave => {
                let Some(draft) = &self.controller.relay_draft else {
                    return true;
                };
                match draft.missing() {
                    Some(reason) => self.error = reason.into(),
                    None => self.controller_command(Command::RelaySave(draft.body())),
                }
            }
            Control::ControllerRelayRemove => {
                if self.controller.relay_confirm_remove {
                    self.controller.relay_confirm_remove = false;
                    self.controller_command(Command::RelayRemove);
                } else {
                    self.controller.relay_confirm_remove = true;
                }
            }
            _ => return false,
        }
        true
    }

    /// A typed relay field; false for other inputs.
    pub(in crate::tui) fn relay_input(&mut self, key: &str, input: &str) -> bool {
        let Some(field) = key.strip_prefix("relay_") else {
            return false;
        };
        if let Some(draft) = &mut self.controller.relay_draft {
            match draft.set(field, input) {
                Ok(()) => {
                    self.controller.defaults_input = None;
                    self.error.clear();
                }
                Err(error) => self.error = error.into(),
            }
        }
        true
    }

    /// Back inside the relay page; false when it is not open.
    pub(in crate::tui) fn relay_back(&mut self) -> bool {
        if self.controller.relay.is_none() {
            return false;
        }
        if self.controller.relay_draft.take().is_some() || self.controller.relay_confirm_remove {
            self.controller.relay_nodes = None;
            self.controller.relay_confirm_remove = false;
            self.controller.selected = 0;
            return true;
        }
        self.controller.relay = None;
        self.controller.selected = 0;
        if self.controller.relay_origin.take().is_some() {
            // Opened from a node card: back to that card.
            self.controller_section = None;
            self.screen = Screen::Nodes;
        }
        true
    }

    pub(in crate::tui) fn relay_view(&self) -> (String, Vec<(String, Control)>) {
        let state = &self.controller;
        let back = ("Back".to_string(), Control::ControllerBack);
        let mut text = self.relay_path();
        text.push_str("\n\n");
        if let Some(draft) = &state.relay_draft {
            text.push_str(if state.relay.as_ref().is_some_and(|v| v["relay"].is_object()) {
                "Change the relay. The node's agent applies it; the link reconnects."
            } else {
                "The relay is the web cabinet's public address. It runs on a node, next to its agent; the controller connects out to it and is never exposed."
            });
            let node = if draft.node_key.is_empty() {
                if state.relay_nodes.is_none() {
                    "loading…".to_string()
                } else {
                    "choose".to_string()
                }
            } else {
                draft.node_title.clone()
            };
            let mut controls = vec![(
                format!("Node: {node}"),
                // The node of an existing relay or a node card stays.
                Control::ControllerRelayNode,
            )];
            let shown = |value: &str| {
                if value.is_empty() {
                    "—".to_string()
                } else {
                    value.to_string()
                }
            };
            controls.push((
                format!("Domain: {}", shown(&draft.domain)),
                Control::ControllerRelayField("domain"),
            ));
            controls.push((
                format!("TLS: {}", tls_label(draft.tls)),
                Control::ControllerRelayTls,
            ));
            if draft.tls != "acme" {
                controls.push((
                    format!("Port: {}", draft.port),
                    Control::ControllerRelayField("port"),
                ));
            }
            match draft.tls {
                "acme" => controls.push((
                    format!("Contact email: {}", shown(&draft.acme_contact)),
                    Control::ControllerRelayField("acme_contact"),
                )),
                "files" => controls.extend([
                    (
                        format!("Certificate: {}", shown(&draft.certificate_path)),
                        Control::ControllerRelayField("certificate_path"),
                    ),
                    (
                        format!("Key: {}", shown(&draft.key_path)),
                        Control::ControllerRelayField("key_path"),
                    ),
                ]),
                _ => text.push_str("\n\nThe proxy on the node answers on 443 for the domain and passes HTTP/2 (h2c) to the relay's port on 127.0.0.1."),
            }
            if draft.tls == "acme" {
                text.push_str("\n\nLet's Encrypt checks the domain on port 443: point the domain at the node, and keep 443 free of Xray there.");
            }
            controls.push(("Save".into(), Control::ControllerRelaySave));
            controls.push(("Cancel".into(), Control::ControllerBack));
            return (text, controls);
        }
        let Some(value) = &state.relay else {
            return (text, vec![back]);
        };
        if value["loading"] == true {
            text.push_str("Please wait");
            return (text, vec![back]);
        }
        let relay = &value["relay"];
        if !relay.is_object() {
            text.push_str("No relay yet. Without it the web cabinet has no public address.");
            let label = if state.relay_origin.is_some() {
                "Set up relay on this node"
            } else {
                "Set up relay"
            };
            return (
                text,
                vec![(label.into(), Control::ControllerRelaySetup), back],
            );
        }
        let elsewhere = state
            .relay_origin
            .as_ref()
            .is_some_and(|(key, _)| relay["node_key"] != key.as_str());
        if elsewhere {
            text.push_str("The relay runs on another node; one relay per installation.\n");
        }
        text.push_str(&format!(
            "Address: {}\nNode: {}\nStatus: {}\nTLS: {}\n{}",
            relay["url"].as_str().unwrap_or(""),
            self.relay_node_title(relay["node_key"].as_str().unwrap_or("")),
            status_label(relay["status"].as_str().unwrap_or("")),
            tls_label(relay["tls"].as_str().unwrap_or("")),
            link_label(relay),
        ));
        if let Some(error) = relay["error_code"].as_str() {
            text.push_str(&format!("\nLast error: {error}"));
        }
        let mut controls = Vec::new();
        if relay["status"] != "removing" {
            controls.push(("Change settings".into(), Control::ControllerRelaySetup));
            controls.push((
                if state.relay_confirm_remove {
                    "Confirm: remove relay"
                } else {
                    "Remove relay"
                }
                .into(),
                Control::ControllerRelayRemove,
            ));
        }
        controls.push(("Refresh".into(), Control::ControllerRelayRefresh));
        controls.push(back);
        (text, controls)
    }
}
