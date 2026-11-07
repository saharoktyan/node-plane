//! Where the Telegram bot runs, in Workstation: Settings › Basic or
//! Services › Telegram bot location. On a node it reaches the controller
//! through the relay; Back returns to the section it was opened from.
use super::super::*;
use crate::controller::Command;
use crate::relay::{candidates, status_label};

impl App {
    pub(in crate::tui) fn open_bot_placement(&mut self) {
        if !self.actions_enabled() {
            return;
        }
        self.controller_navigation_revision = self.controller_navigation_revision.wrapping_add(1);
        self.controller.scope = None;
        self.controller.defaults_input = None;
        self.controller.relay = None;
        self.controller.relay_draft = None;
        self.controller.relay_origin = None;
        self.controller.cabinet = None;
        self.controller.cabinet_draft = None;
        self.controller.bot_placement = Some(serde_json::json!({"loading": true}));
        self.controller.bot_nodes = None;
        self.controller.bot_confirm_return = false;
        self.controller.selected = 0;
        self.controller_viewport.set(0);
        if self.controller_section != Some(ControllerSection::Services) {
            self.controller_section = Some(ControllerSection::Installation);
        }
        self.requests_settings = false;
        self.error.clear();
        self.controller_command(Command::BotPlacement);
    }

    pub(in crate::tui) fn bot_placement_view_open(&self) -> bool {
        self.controller.bot_placement.is_some() && self.controller.defaults_input.is_none()
    }

    /// Handles a control of the bot's location; false for any other control.
    pub(in crate::tui) fn activate_bot_placement(&mut self, control: Control) -> bool {
        match control {
            Control::ControllerBotPlacement => self.open_bot_placement(),
            Control::ControllerBotRefresh => self.controller_command(Command::BotPlacement),
            Control::ControllerBotPick => {
                self.controller.bot_nodes = Some(serde_json::json!({"loading": true}));
                self.controller.bot_confirm_return = false;
                self.controller.selected = 0;
                self.controller_command(Command::BotNodes);
            }
            Control::ControllerBotNode(index) => {
                let nodes = self.controller.bot_nodes.as_ref().map(candidates);
                if let Some((key, _)) = nodes.and_then(|nodes| nodes.get(index).cloned()) {
                    self.controller_command(Command::BotPlace(key));
                }
            }
            Control::ControllerBotRetry => {
                let key =
                    self.controller.bot_placement.as_ref().and_then(|value| {
                        value["placement"]["node_key"].as_str().map(str::to_owned)
                    });
                if let Some(key) = key {
                    self.controller_command(Command::BotPlace(key));
                }
            }
            Control::ControllerBotReturn => {
                if self.controller.bot_confirm_return {
                    self.controller.bot_confirm_return = false;
                    self.controller_command(Command::BotReturn);
                } else {
                    self.controller.bot_confirm_return = true;
                }
            }
            _ => return false,
        }
        true
    }

    /// Back inside the page; false when it is not open.
    pub(in crate::tui) fn bot_placement_back(&mut self) -> bool {
        if self.controller.bot_placement.is_none() {
            return false;
        }
        if self.controller.bot_nodes.take().is_none() && !self.controller.bot_confirm_return {
            self.controller.bot_placement = None;
        }
        self.controller.bot_confirm_return = false;
        self.controller.selected = 0;
        true
    }

    pub(in crate::tui) fn bot_placement_view(&self) -> (String, Vec<(String, Control)>) {
        let state = &self.controller;
        let back = ("Back".to_string(), Control::ControllerBack);
        let mut text = if self.controller_section == Some(ControllerSection::Services) {
            "Settings › Services › Telegram bot location".to_string()
        } else {
            "Settings › Basic › Telegram bot location".to_string()
        };
        text.push_str("\n\n");
        if let Some(nodes) = &state.bot_nodes {
            if nodes["loading"] == true {
                text.push_str("Please wait");
                return (text, vec![back]);
            }
            let nodes = candidates(nodes);
            if nodes.is_empty() {
                text.push_str("No node has an agent yet.");
            } else {
                text.push_str("Choose the node to run the bot on. The controller's bot stops once the node runs it.");
            }
            let mut controls: Vec<_> = nodes
                .into_iter()
                .enumerate()
                .map(|(index, (_, title))| (title, Control::ControllerBotNode(index)))
                .collect();
            controls.push(("Cancel".into(), Control::ControllerBack));
            return (text, controls);
        }
        let Some(value) = &state.bot_placement else {
            return (text, vec![back]);
        };
        if value["loading"] == true {
            text.push_str("Please wait");
            return (text, vec![back]);
        }
        let mut controls = Vec::new();
        let placement = &value["placement"];
        if !placement.is_object() {
            text.push_str("Runs on: the controller\n\nThe bot can run on a node with an agent instead: it then reaches the controller through the relay, and the controller needs no access to Telegram. The node installs its packages from PyPI.");
            if value["relay_node_key"].is_string() {
                controls.push(("Run on a node".into(), Control::ControllerBotPick));
            } else {
                text.push_str("\nTo run it on a node, set up the relay first.");
                controls.push(("Relay".into(), Control::ControllerRelay));
            }
        } else {
            text.push_str(&format!(
                "Runs on: {}\nStatus: {}",
                placement["node_key"].as_str().unwrap_or("—"),
                status_label(placement["status"].as_str().unwrap_or("")),
            ));
            if let Some(applied) = placement["applied_version"].as_str()
                && placement["version"] != applied
            {
                text.push_str(&format!(
                    "\nRunning version {applied}; the controller's is being applied."
                ));
            }
            if let Some(error) = placement["error_code"].as_str() {
                text.push_str(&format!("\nLast error: {error}"));
            }
            if placement["status"] != "removing" {
                if placement["status"] == "failed" {
                    controls.push(("Try again".into(), Control::ControllerBotRetry));
                }
                controls.push((
                    if state.bot_confirm_return {
                        "Confirm: back to the controller"
                    } else {
                        "Back to the controller"
                    }
                    .into(),
                    Control::ControllerBotReturn,
                ));
            }
        }
        controls.push(("Refresh".into(), Control::ControllerBotRefresh));
        controls.push(back);
        (text, controls)
    }
}
