use super::super::*;

impl App {
    pub(in crate::tui) fn activate_nodes(&mut self, control: Control) -> Option<KeyEvent> {
        match control {
            Control::NodesEdit => {
                if let Some(node) = &self.nodes.card {
                    self.nodes_requested =
                        Some(crate::nodes::Command::EditContext(node.key.clone()));
                }
                None
            }
            Control::NodesEditTab(tab) => {
                if let Some(editor) = &mut self.nodes.editor {
                    if tab == crate::node_editor::Tab::AwgParameters {
                        self.manual_awg_original = editor
                            .fields
                            .iter()
                            .find(|field| field.key == "awg_parameters")
                            .map(|field| field.value.clone());
                    } else {
                        self.manual_awg_original = None;
                    }
                    editor.tab = tab;
                }
                self.nodes_selected = 0;
                None
            }
            Control::NodesEditField(_) => {
                self.focus(control);
                None
            }
            Control::NodesEditChoice(index) => {
                if let Some(editor) = &mut self.nodes.editor
                    && let Some(choices) =
                        crate::node_editor::Editor::choices(editor.fields[index].key)
                {
                    let position = choices
                        .iter()
                        .position(|v| *v == editor.fields[index].value)
                        .unwrap_or(0);
                    editor.fields[index].value = choices[(position + 1) % choices.len()].into();
                    if editor.fields[index].key == "awg_i1_preset"
                        && editor
                            .fields
                            .iter()
                            .any(|f| f.key == "awg_port_mode" && f.value == "auto")
                    {
                        let port = match editor.fields[index].value.as_str() {
                            "quic" => 443,
                            "dns" => 53,
                            _ => rand::random_range(1024u16..=9999),
                        };
                        if let Some(field) = editor.fields.iter_mut().find(|f| f.key == "awg_port")
                        {
                            field.value = port.to_string();
                        }
                    }
                    editor.changed();
                }
                None
            }
            Control::NodesEditToggle(transport, index) => {
                if let Some(editor) = &mut self.nodes.editor {
                    editor.toggle(
                        if transport {
                            ["tcp", "xhttp"][index]
                        } else {
                            ["awg", "xray"][index]
                        },
                        transport,
                    );
                }
                None
            }
            Control::NodesEditSave => {
                if let Some(editor) = &self.nodes.editor {
                    match editor.draft() {
                        Ok(draft) => {
                            self.nodes_requested = Some(crate::nodes::Command::EditReview(draft))
                        }
                        Err(error) => self.error = error.to_string(),
                    }
                }
                None
            }
            Control::NodesEditConfirm => {
                if let Some((draft, preview)) =
                    self.nodes.editor.as_ref().and_then(|e| e.review.as_ref())
                {
                    let mut draft = draft.clone();
                    if preview["affected_profiles"].as_u64().unwrap_or(0) > 0 {
                        draft.body["confirm_access_change"] = serde_json::json!(true);
                    }
                    self.nodes_requested = Some(crate::nodes::Command::EditSave(draft));
                }
                None
            }
            Control::NodesEditReset => {
                if let Some(editor) = &mut self.nodes.editor {
                    editor.reset();
                }
                self.error.clear();
                None
            }
            Control::NodesEditBack if self.manual_awg_original.is_some() => {
                if let Some(editor) = &mut self.nodes.editor {
                    if let Some(field) = editor
                        .fields
                        .iter_mut()
                        .find(|field| field.key == "awg_parameters")
                    {
                        field.value = self.manual_awg_original.take().unwrap();
                    }
                    editor.tab = crate::node_editor::Tab::Advanced;
                }
                self.nodes_selected = 0;
                None
            }
            Control::NodesEditBack => {
                if let Some(editor) = &mut self.nodes.editor
                    && editor.review.take().is_some()
                {
                    self.nodes_selected = 0;
                    return None;
                }
                self.nodes.editor = None;
                self.nodes_selected = 0;
                self.error.clear();
                None
            }
            Control::NodesApply => {
                if let Some(node) = &self.nodes.card {
                    self.nodes_requested = Some(crate::nodes::Command::ApplySettings(
                        node.clone(),
                        uuid::Uuid::new_v4(),
                    ));
                }
                None
            }
            Control::NodesMaintenanceSection(section) => {
                if let Some(view) = &mut self.nodes.maintenance {
                    view.section = section;
                    view.scroll = 0;
                    self.nodes_selected = 0;
                }
                None
            }
            Control::NodesMaintenance => {
                if let Some(node) = &self.nodes.card {
                    self.nodes_requested =
                        Some(crate::nodes::Command::Maintenance(node.key.clone(), None));
                }
                None
            }
            Control::NodesRemove(mode) => {
                if let Some(view) = &self.nodes.maintenance
                    && !view.removed()
                    && (mode == crate::node_maintenance::Mode::RegistryOnly || !view.active())
                {
                    self.nodes_requested = Some(crate::nodes::Command::Maintenance(
                        view.key.clone(),
                        Some(mode),
                    ));
                }
                None
            }
            Control::NodesRemoveConfirm => {
                if let Some(view) = &self.nodes.maintenance
                    && let Some(mode) = view.confirmation
                {
                    self.nodes_requested = Some(crate::nodes::Command::Remove(
                        view.key.clone(),
                        mode,
                        view.blocked(),
                    ));
                }
                None
            }
            Control::NodesDiagnostics | Control::NodesDiagnosticPage(_) => {
                if let Control::NodesDiagnosticPage(previous) = control
                    && let Some((_, inventory)) = &self.nodes.diagnostics
                {
                    let count = inventory["items"].as_array().map_or(0, |items| items.len());
                    let capacity = self.nodes_capacity.get().max(1);
                    if previous && self.nodes.diagnostic_page > 0 {
                        self.nodes.diagnostic_page -= 1;
                        self.nodes_selected = 0;
                        return None;
                    }
                    if !previous && (self.nodes.diagnostic_page + 1) * capacity < count {
                        self.nodes.diagnostic_page += 1;
                        self.nodes_selected = 0;
                        return None;
                    }
                }
                if let Some(node) = &self.nodes.card {
                    let offset = self.nodes.diagnostics.as_ref().map_or(0, |(_, inventory)| {
                        let current = inventory["offset"].as_u64().unwrap_or(0) as usize;
                        let size = inventory["page_size"].as_u64().unwrap_or(10) as usize;
                        match control {
                            Control::NodesDiagnosticPage(true) => current.saturating_sub(size),
                            Control::NodesDiagnosticPage(false) => current + size,
                            _ => current,
                        }
                    });
                    self.nodes_requested =
                        Some(crate::nodes::Command::Diagnostics(node.key.clone(), offset));
                }
                None
            }
            Control::NodesDiagnosticItem(index) => {
                if let Some((_, inventory)) = &self.nodes.diagnostics {
                    self.nodes.diagnostic_item = inventory["items"]
                        .as_array()
                        .and_then(|items| items.get(index))
                        .cloned();
                    self.nodes_selected = 0;
                }
                None
            }
            Control::NodesRecovery(index, resolve) => {
                if let Some((_, inventory)) = &self.nodes.diagnostics
                    && let Some(item) = inventory["items"]
                        .as_array()
                        .and_then(|items| items.get(index))
                {
                    let action = if resolve { "resolve" } else { "recheck" };
                    if item["actions"]
                        .as_array()
                        .is_some_and(|actions| actions.iter().any(|v| v == action))
                    {
                        self.nodes.recovery_confirm = Some((item.clone(), action.into()));
                        self.nodes_selected = 0;
                    }
                }
                None
            }
            Control::NodesRecoveryConfirm => {
                if let Some(node) = &self.nodes.card
                    && let Some((item, action)) = self.nodes.recovery_confirm.take()
                {
                    self.nodes_requested = Some(crate::nodes::Command::Recover(
                        node.key.clone(),
                        item,
                        action,
                    ));
                }
                None
            }
            Control::NodesSearch => {
                self.nodes_selected = 0;
                self.nodes_sidebar = false;
                None
            }
            Control::NodesRegion(index) => {
                if let Some(node) = self.nodes.items.get(index) {
                    if !self.nodes.collapsed.remove(&node.region) {
                        self.nodes.collapsed.insert(node.region.clone());
                    }
                    self.nodes.page = 0;
                    self.nodes_selected = 0;
                }
                None
            }
            Control::NodesCard(index) => {
                if let Some(node) = self.nodes.items.get(index) {
                    self.nodes_requested = Some(crate::nodes::Command::Card(node.key.clone()));
                }
                None
            }
            Control::NodesRefresh => {
                if self.nodes.maintenance.is_some() {
                    return self.activate(Control::NodesMaintenance);
                }
                if self.nodes.diagnostics.is_some() {
                    return self.activate(Control::NodesDiagnostics);
                }
                self.nodes_requested = Some(
                    self.nodes
                        .card
                        .as_ref()
                        .map_or(crate::nodes::Command::List, |node| {
                            crate::nodes::Command::Card(node.key.clone())
                        }),
                );
                None
            }
            Control::NodesBack => {
                if self.nodes.diagnostics.is_none()
                    && let Some(view) = &mut self.nodes.maintenance
                {
                    self.nodes_selected = 0;
                    if view.confirmation.take().is_some() {
                        return None;
                    }
                    if view.section != crate::node_maintenance::Section::Root && !view.removed() {
                        view.section = crate::node_maintenance::Section::Root;
                        view.scroll = 0;
                        return None;
                    }
                    let removed = view.removed();
                    self.nodes.maintenance = None;
                    if removed {
                        self.nodes.card = None;
                        self.nodes_requested = Some(crate::nodes::Command::List);
                    } else if let Some(node) = &self.nodes.card {
                        self.nodes_requested = Some(crate::nodes::Command::Card(node.key.clone()));
                    }
                    return None;
                }
                if self.nodes.recovery_confirm.take().is_some() {
                    self.nodes_selected = 0;
                    return None;
                }
                if self.nodes.diagnostic_item.take().is_some() {
                    self.nodes_selected = 0;
                    return None;
                }
                if self.nodes.diagnostics.take().is_some() {
                    self.nodes_selected = 0;
                    if self.nodes.maintenance.is_none()
                        && let Some(node) = &self.nodes.card
                    {
                        self.nodes_requested = Some(crate::nodes::Command::Card(node.key.clone()));
                    }
                    return None;
                }
                self.nodes.card = None;
                self.nodes_selected = 0;
                if self.nodes_updates_origin {
                    self.nodes_updates_origin = false;
                    self.screen = Screen::Controller;
                    self.controller_sidebar = false;
                }
                None
            }
            Control::NodesNew => {
                self.node_wizard = None;
                self.wizard_loading = true;
                self.nodes_requested = Some(crate::nodes::Command::CreationOptions);
                None
            }
            Control::WizardChoice(index) => {
                if let Some(wizard) = &mut self.node_wizard {
                    self.error = wizard
                        .choose(index)
                        .err()
                        .map(|e| e.to_string())
                        .unwrap_or_default();
                }
                None
            }
            Control::WizardNext => {
                if let Some(wizard) = &mut self.node_wizard {
                    match wizard.next() {
                        Ok(Some(draft)) => {
                            self.wizard_loading = true;
                            self.nodes_requested = Some(crate::nodes::Command::Create(draft));
                        }
                        Ok(None) => self.error.clear(),
                        Err(error) => self.error = error.to_string(),
                    }
                }
                None
            }
            Control::WizardBack => {
                self.error.clear();
                if self.node_wizard.as_mut().is_none_or(|w| !w.back()) {
                    self.node_wizard = None;
                    self.screen = Screen::Nodes;
                }
                None
            }
            Control::WizardCancel => {
                self.node_wizard = None;
                self.screen = Screen::Nodes;
                self.error.clear();
                None
            }
            Control::WizardOpen => {
                let key = self
                    .node_wizard
                    .as_ref()
                    .and_then(|w| w.created_key.clone())
                    .or_else(|| self.nodes.card.as_ref().map(|n| n.key.clone()));
                self.node_wizard = None;
                self.screen = Screen::Nodes;
                self.nodes.operation = None;
                if let Some(key) = key {
                    self.nodes_requested = Some(crate::nodes::Command::Card(key));
                }
                None
            }
            Control::NodesProtocolAction(action) => {
                if let Some(node) = &self.nodes.card {
                    self.nodes_requested =
                        Some(crate::nodes::Command::ProtocolAction(node.clone(), action));
                }
                None
            }
            Control::NodesSetup | Control::NodesBootstrap => {
                if let Some(node) = &self.nodes.card {
                    self.nodes_requested = Some(if matches!(control, Control::NodesSetup) {
                        crate::nodes::Command::Setup(node.clone())
                    } else {
                        crate::nodes::Command::Bootstrap(node.clone())
                    });
                }
                None
            }
            Control::NodesPage(backwards) => {
                let pages = self.nodes.pages(self.nodes_capacity.get()).len();
                let page = self.nodes.page.min(pages - 1);
                self.nodes.page = if backwards {
                    page.saturating_sub(1)
                } else {
                    (page + 1).min(pages - 1)
                };
                self.nodes_selected = 0;
                None
            }
            _ => None,
        }
    }
    pub(in crate::tui) fn open_nodes(&mut self) {
        self.nodes_updates_origin = false;
        let profile_id = self.form.saved.as_ref().map(|p| p.id);
        if self.nodes.profile_id != profile_id {
            self.nodes = crate::nodes::Browser {
                profile_id,
                ..crate::nodes::Browser::default()
            };
        }
        if !self.controller_ready() {
            return;
        }
        self.screen = Screen::Nodes;
        self.nodes.card = None;
        self.nodes.editor = None;
        self.nodes.diagnostics = None;
        self.nodes.diagnostic_item = None;
        self.nodes.recovery_confirm = None;
        self.nodes.maintenance = None;
        self.nodes_sidebar = false;
        self.nodes_selected = 0;
        if !self.nodes.loaded {
            self.nodes_requested = Some(crate::nodes::Command::List);
        }
    }
    pub(in crate::tui) fn nodes_controls(&self) -> Vec<Control> {
        if let Some(editor) = &self.nodes.editor
            && editor.review.is_none()
        {
            return node_editor_controls(editor);
        }
        self.nodes_hits().iter().map(|hit| hit.control).collect()
    }
}
