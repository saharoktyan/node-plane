//! The web cabinet in Workstation: Settings › Basic or Services › Web
//! cabinet. It is served by the relay's node or by the controller itself;
//! Back returns to the section it was opened from.
use super::super::*;
use crate::cabinet::{Draft, place};
use crate::controller::Command;
use crate::relay::status_label;

impl App {
    pub(in crate::tui) fn open_cabinet(&mut self) {
        if !self.actions_enabled() {
            return;
        }
        self.controller_navigation_revision = self.controller_navigation_revision.wrapping_add(1);
        self.controller.scope = None;
        self.controller.defaults_input = None;
        self.controller.relay = None;
        self.controller.relay_draft = None;
        self.controller.relay_origin = None;
        self.controller.cabinet = Some(serde_json::json!({"loading": true}));
        self.controller.cabinet_draft = None;
        self.controller.cabinet_confirm_remove = false;
        self.controller.bot_placement = None;
        self.controller.bot_nodes = None;
        self.controller.selected = 0;
        self.controller_viewport.set(0);
        if self.controller_section != Some(ControllerSection::Services) {
            self.controller_section = Some(ControllerSection::Installation);
        }
        self.requests_settings = false;
        self.error.clear();
        self.controller_command(Command::Cabinet);
    }

    pub(in crate::tui) fn cabinet_view_open(&self) -> bool {
        self.controller.cabinet.is_some() && self.controller.defaults_input.is_none()
    }

    /// Handles a cabinet control; false for any other control.
    pub(in crate::tui) fn activate_cabinet(&mut self, control: Control) -> bool {
        match control {
            Control::ControllerCabinet => self.open_cabinet(),
            Control::ControllerCabinetRefresh => self.controller_command(Command::Cabinet),
            Control::ControllerCabinetRelay => {
                self.controller_command(Command::CabinetSave(serde_json::json!({"mode": "relay"})))
            }
            Control::ControllerCabinetLocal => {
                let current = self
                    .controller
                    .cabinet
                    .as_ref()
                    .map_or(serde_json::Value::Null, |v| v["cabinet"].clone());
                self.controller.cabinet_draft = Some(Draft::from_cabinet(&current));
                self.controller.cabinet_confirm_remove = false;
                self.controller.selected = 0;
            }
            Control::ControllerCabinetField(field) => {
                if let Some(draft) = &self.controller.cabinet_draft {
                    self.controller.defaults_input = Some(match field {
                        "listen" => ("cabinet_listen", draft.listen.clone()),
                        _ => ("cabinet_public_url", draft.public_url.clone()),
                    });
                }
            }
            Control::ControllerCabinetSave => {
                let Some(draft) = &self.controller.cabinet_draft else {
                    return true;
                };
                match draft.missing() {
                    Some(reason) => self.error = reason.into(),
                    None => self.controller_command(Command::CabinetSave(draft.body())),
                }
            }
            Control::ControllerCabinetRemove => {
                if self.controller.cabinet_confirm_remove {
                    self.controller.cabinet_confirm_remove = false;
                    self.controller_command(Command::CabinetRemove);
                } else {
                    self.controller.cabinet_confirm_remove = true;
                }
            }
            _ => return false,
        }
        true
    }

    /// A typed cabinet field; false for other inputs.
    pub(in crate::tui) fn cabinet_input(&mut self, key: &str, input: &str) -> bool {
        let Some(field) = key.strip_prefix("cabinet_") else {
            return false;
        };
        if let Some(draft) = &mut self.controller.cabinet_draft {
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

    /// Back inside the cabinet page; false when it is not open.
    pub(in crate::tui) fn cabinet_back(&mut self) -> bool {
        if self.controller.cabinet.is_none() {
            return false;
        }
        if self.controller.cabinet_draft.take().is_none() && !self.controller.cabinet_confirm_remove
        {
            self.controller.cabinet = None;
        }
        self.controller.cabinet_confirm_remove = false;
        self.controller.selected = 0;
        true
    }

    pub(in crate::tui) fn cabinet_view(&self) -> (String, Vec<(String, Control)>) {
        let state = &self.controller;
        let back = ("Back".to_string(), Control::ControllerBack);
        let mut text = if self.controller_section == Some(ControllerSection::Services) {
            "Settings › Services › Web cabinet".to_string()
        } else {
            "Settings › Basic › Web cabinet".to_string()
        };
        text.push_str("\n\n");
        const WARNING: &str =
            "The controller listens on the network for the cabinet: open only the port you chose.";
        if let Some(draft) = &state.cabinet_draft {
            text.push_str("The controller serves the cabinet itself and passes its API requests through. Set where it listens and the address people open it at.\n\n");
            text.push_str(WARNING);
            let shown = |value: &str| {
                if value.is_empty() {
                    "—".to_string()
                } else {
                    value.to_string()
                }
            };
            return (
                text,
                vec![
                    (
                        format!("Listen: {}", draft.listen),
                        Control::ControllerCabinetField("listen"),
                    ),
                    (
                        format!("Address: {}", shown(&draft.public_url)),
                        Control::ControllerCabinetField("public_url"),
                    ),
                    ("Save".into(), Control::ControllerCabinetSave),
                    ("Cancel".into(), Control::ControllerBack),
                ],
            );
        }
        let Some(value) = &state.cabinet else {
            return (text, vec![back]);
        };
        if value["loading"] == true {
            text.push_str("Please wait");
            return (text, vec![back]);
        }
        let mut controls = Vec::new();
        if value["available"] == false {
            text.push_str("This controller release has no web cabinet build.\n");
        }
        let cabinet = &value["cabinet"];
        if !cabinet.is_object() {
            text.push_str("The web cabinet is not served yet.");
            if value["available"] == true {
                if value["relay_node_key"].is_string() {
                    controls.push((
                        "Serve on the relay's node".into(),
                        Control::ControllerCabinetRelay,
                    ));
                } else {
                    text.push_str("\nTo serve it on the relay's domain, set up the relay first.");
                    controls.push(("Relay".into(), Control::ControllerRelay));
                }
                controls.push((
                    "Serve from the controller".into(),
                    Control::ControllerCabinetLocal,
                ));
            }
        } else {
            text.push_str(&format!(
                "Address: {}\n{}\nStatus: {}",
                cabinet["url"].as_str().unwrap_or("—"),
                place(cabinet),
                status_label(cabinet["status"].as_str().unwrap_or("")),
            ));
            if let Some(applied) = cabinet["applied_version"].as_str()
                && cabinet["version"] != applied
            {
                text.push_str(&format!(
                    "\nRunning version {applied}; the controller's is being applied."
                ));
            }
            if let Some(error) = cabinet["error_code"].as_str() {
                text.push_str(&format!("\nLast error: {error}"));
            }
            if cabinet["mode"] == "local" {
                text.push_str("\n\n");
                text.push_str(WARNING);
            }
            if cabinet["status"] != "removing" {
                if cabinet["mode"] == "local" {
                    controls.push(("Change settings".into(), Control::ControllerCabinetLocal));
                }
                controls.push((
                    if state.cabinet_confirm_remove {
                        "Confirm: stop serving"
                    } else {
                        "Stop serving"
                    }
                    .into(),
                    Control::ControllerCabinetRemove,
                ));
            }
        }
        controls.push(("Refresh".into(), Control::ControllerCabinetRefresh));
        controls.push(back);
        (text, controls)
    }
}
