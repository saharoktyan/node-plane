use super::super::*;

impl App {
    pub(in crate::tui) fn activate_temporary(&mut self, control: Control) -> Option<KeyEvent> {
        match control {
            Control::Temporary => {
                self.open_temporary(None);
                None
            }
            Control::NodeTemporary => {
                self.open_temporary(self.nodes.card.clone());
                None
            }
            Control::TemporaryChoice(index) => {
                self.temporary_choice(index);
                None
            }
            Control::TemporaryBack => {
                if self.qr_visible {
                    self.qr_visible = false;
                    return None;
                }
                if self.temporary.view == crate::temporary::View::List
                    && self.temporary.filter.is_some()
                {
                    self.screen = Screen::Nodes;
                    self.nodes_sidebar = false;
                    self.action_focus = Some(Control::NodeTemporary);
                    return None;
                }
                self.temporary.back();
                if self.temporary.view == crate::temporary::View::List {
                    self.temporary_command(self.temporary.list());
                }
                None
            }
            Control::TemporaryNew => {
                let command = self.temporary.new_config();
                self.temporary_command(command);
                None
            }
            Control::TemporaryRefresh => {
                let command = if self.temporary.view == crate::temporary::View::Card {
                    self.temporary
                        .card
                        .as_ref()
                        .and_then(|v| v["id"].as_str())
                        .map(|id| crate::temporary::Command::Card(id.into()))
                } else {
                    Some(self.temporary.list())
                };
                if let Some(command) = command {
                    self.temporary_command(command);
                }
                None
            }
            Control::TemporaryPage(backwards) => {
                self.temporary.page = if backwards {
                    self.temporary.page.saturating_sub(1)
                } else {
                    self.temporary.page + 1
                };
                if self.temporary.view != crate::temporary::View::Server {
                    self.temporary_command(self.temporary.list());
                }
                self.temporary.selected = 0;
                None
            }
            Control::TemporaryShow => {
                if let Some(id) = self
                    .temporary
                    .card
                    .as_ref()
                    .and_then(|v| v["id"].as_str())
                    .map(str::to_owned)
                {
                    self.temporary_command(crate::temporary::Command::Artifact(id));
                }
                None
            }
            Control::TemporaryCopy => {
                if let Some(uri) = self
                    .temporary
                    .artifact
                    .as_ref()
                    .and_then(|v| v["content"].as_str())
                {
                    let result = (|| -> Result<()> {
                        if self.clipboard.is_none() {
                            self.clipboard = Some(arboard::Clipboard::new()?);
                        }
                        self.clipboard.as_mut().unwrap().set_text(uri.to_owned())?;
                        Ok(())
                    })();
                    if result.is_ok() {
                        self.error.clear();
                        self.temporary.notice = "Configuration link copied.".into();
                    } else {
                        self.error =
                            "Clipboard unavailable. Save the configuration to a file.".into();
                    }
                }
                None
            }
            Control::TemporaryQr => {
                if let (Some(picker), Some(uri)) = (
                    &self.image_picker,
                    self.temporary
                        .artifact
                        .as_ref()
                        .and_then(|v| v["content"].as_str()),
                ) {
                    match crate::temporary::qr_image(uri) {
                        Ok(image) => {
                            *self.qr_graphic.borrow_mut() =
                                Some(picker.new_resize_protocol(image.clone()));
                            *self.qr.borrow_mut() = Some(image);
                            self.qr_visible = true;
                            self.error.clear();
                        }
                        Err(_) => {
                            self.error =
                                "The link is too large for a QR code. Copy it or save the files."
                                    .into()
                        }
                    }
                }
                None
            }
            Control::TemporarySave => {
                if let (Some(card), Some(artifact)) =
                    (&self.temporary.card, &self.temporary.artifact)
                {
                    let result = crate::temporary::save_artifact(
                        &self.state_dir,
                        card["id"].as_str().unwrap_or(""),
                        artifact,
                    );
                    match result {
                        Ok(path) => {
                            self.error.clear();
                            self.temporary.notice = format!("Saved: {}", path.display());
                        }
                        Err(error) => self.error = error.to_string(),
                    }
                }
                None
            }
            Control::TemporaryRevoke => {
                self.temporary.view = crate::temporary::View::Revoke;
                self.temporary.selected = 0;
                None
            }
            Control::TemporaryConfirm => {
                let command = if self.temporary.view == crate::temporary::View::Revoke {
                    crate::temporary::Command::Revoke(
                        self.temporary.card.as_ref().unwrap()["id"]
                            .as_str()
                            .unwrap()
                            .into(),
                    )
                } else {
                    self.temporary.create()
                };
                self.temporary_command(command);
                None
            }
            _ => None,
        }
    }
    pub(in crate::tui) fn temporary_command(&mut self, command: crate::temporary::Command) {
        self.nodes_requested = Some(crate::nodes::Command::Temporary(command));
        self.error.clear();
    }
    pub(in crate::tui) fn open_temporary(&mut self, filter: Option<crate::nodes::Node>) {
        if !self.controller_ready() {
            return;
        }
        let profile = self.form.saved.as_ref().map(|p| p.id);
        if self.temporary.profile != profile {
            self.temporary = crate::temporary::Browser {
                profile,
                ..Default::default()
            };
        }
        self.temporary.filter = filter;
        self.temporary.view = crate::temporary::View::List;
        self.temporary
            .capacity
            .set(self.nodes_capacity.get().max(1));
        self.temporary.page = 0;
        self.temporary.selected = 0;
        self.temporary.artifact = None;
        self.temporary.notice.clear();
        self.qr_visible = false;
        *self.qr.borrow_mut() = None;
        self.screen = Screen::Temporary;
        self.temporary_sidebar = false;
        self.quick_focus = false;
        self.temporary_command(self.temporary.list());
    }
    pub(in crate::tui) fn temporary_controls(&self) -> Vec<Control> {
        use crate::temporary::View;
        let (first, count) = match self.temporary.view {
            View::List => (0, self.temporary.items.len()),
            View::Server => (0, self.temporary.nodes.len()),
            View::Protocol => (
                0,
                self.temporary
                    .node
                    .as_ref()
                    .map_or(0, |node| node.protocols.len()),
            ),
            View::Transport => (
                0,
                self.temporary
                    .node
                    .as_ref()
                    .map_or(0, |node| node.xray_transports.len()),
            ),
            View::Duration => (0, 3),
            _ => (0, 0),
        };
        let mut controls: Vec<_> = (first..first + count)
            .map(Control::TemporaryChoice)
            .collect();
        controls.extend(self.hits.iter().filter_map(|h| match h.control {
            Control::TemporaryBack
            | Control::TemporaryNew
            | Control::TemporaryRefresh
            | Control::TemporaryPage(_)
            | Control::TemporaryCopy
            | Control::TemporaryQr
            | Control::TemporaryShow
            | Control::TemporarySave
            | Control::TemporaryRevoke
            | Control::TemporaryConfirm => Some(h.control),
            _ => None,
        }));
        controls
    }
    pub(in crate::tui) fn temporary_choice(&mut self, index: usize) {
        use crate::temporary::View;
        match self.temporary.view {
            View::List => {
                if let Some(id) = self
                    .temporary
                    .items
                    .get(index)
                    .and_then(|v| v["id"].as_str())
                    .map(str::to_owned)
                {
                    self.temporary_command(crate::temporary::Command::Card(id));
                }
            }
            View::Server => {
                if let Some(node) = self.temporary.nodes.get(index).cloned() {
                    self.temporary.choose_node(node);
                }
            }
            View::Protocol => {
                if let Some(protocol) = self
                    .temporary
                    .node
                    .as_ref()
                    .and_then(|n| n.protocols.get(index))
                    .cloned()
                {
                    self.temporary.protocol = protocol;
                    self.temporary.choose_protocol();
                }
            }
            View::Transport => {
                if let Some(transport) = self
                    .temporary
                    .node
                    .as_ref()
                    .and_then(|n| n.xray_transports.get(index))
                    .cloned()
                {
                    self.temporary.transport = transport;
                    self.temporary.view = View::Duration;
                }
            }
            View::Duration => {
                if let Some(seconds) = [43200, 86400, 259200].get(index) {
                    self.temporary.seconds = *seconds;
                    self.temporary.view = View::Confirm;
                }
            }
            _ => {}
        }
        self.temporary.selected = 0;
    }
}
