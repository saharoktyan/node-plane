use super::super::*;

impl App {
    pub(in crate::tui) fn navigate(&mut self, backwards: bool) {
        self.fresh_installation = false;
        let current = if self.quick_focus {
            5
        } else if self.screen.is_settings() {
            6
        } else if matches!(self.screen, Screen::Temporary) {
            4
        } else {
            Action::ALL
                .iter()
                .position(|a| *a == self.form.action)
                .unwrap()
        };
        let mut next = (current + if backwards { 6 } else { 1 }) % 7;
        while next < 5 && (!self.actions_enabled() || next > 0 && !self.controller_ready()) {
            next = (next + if backwards { 6 } else { 1 }) % 7;
        }
        if self.unsaved_changes() {
            let control = match next {
                6 => Control::Settings,
                5 => Control::QuickProfile,
                4 => Control::Temporary,
                _ => Control::Action(Action::ALL[next]),
            };
            self.activate(control);
            return;
        }
        self.quick_focus = false;
        if next == 6 {
            self.open_settings();
        } else if next == 5 {
            self.quick_focus = true;
            self.form.selected = 0;
            self.screen = Screen::Form;
            self.editor = None;
        } else if next == 4 {
            self.screen = Screen::Temporary;
            self.temporary_sidebar = true;
        } else {
            self.form.select_action(Action::ALL[next]);
            if self.form.action == Action::Install && self.has_installation() {
                self.open_controller();
                return;
            }
            if self.form.action == Action::Update {
                self.open_controller();
                self.activate(Control::ControllerUpdates);
                return;
            }
            self.screen = if self.form.action == Action::PrepareNode {
                Screen::Nodes
            } else {
                Screen::Form
            };
            self.nodes_sidebar = true;
            if self.form.action == Action::PrepareNode
                && self.nodes.profile_id != self.form.saved.as_ref().map(|p| p.id)
            {
                self.nodes = crate::nodes::Browser::default();
            }
            self.editor = None;
            self.error.clear();
        }
    }
    pub(in crate::tui) fn nodes_hits(&self) -> Vec<&Hit> {
        self.hits
            .iter()
            .filter(|hit| {
                matches!(
                    hit.control,
                    Control::NodesSearch
                        | Control::NodesRegion(_)
                        | Control::NodesCard(_)
                        | Control::NodesRefresh
                        | Control::NodesBack
                        | Control::NodesNew
                        | Control::NodesSetup
                        | Control::NodesEdit
                        | Control::NodesEditTab(_)
                        | Control::NodesEditField(_)
                        | Control::NodesEditChoice(_)
                        | Control::NodesEditToggle(_, _)
                        | Control::NodesEditSave
                        | Control::NodesEditConfirm
                        | Control::NodesEditReset
                        | Control::NodesEditBack
                        | Control::NodesApply
                        | Control::NodesDiagnostics
                        | Control::NodesDiagnosticPage(_)
                        | Control::NodesRecovery(_, _)
                        | Control::NodesDiagnosticItem(_)
                        | Control::NodesRecoveryConfirm
                        | Control::NodesMaintenanceSection(_)
                        | Control::NodesMaintenance
                        | Control::NodesRemove(_)
                        | Control::NodesRemoveConfirm
                        | Control::NodesBootstrap
                        | Control::NodesProtocolAction(_)
                        | Control::NodeTemporary
                        | Control::NodesPage(_)
                )
            })
            .collect()
    }
    pub(in crate::tui) fn nodes_move_direction(&mut self, direction: KeyCode) {
        if let Some(editor) = &self.nodes.editor
            && editor.review.is_none()
        {
            let width = self.nodes_editor_width.get();
            let controls = node_editor_controls(editor);
            let items = node_editor_items(editor);
            let columns = if width >= 64 { 2 } else { 1 };
            let mut areas: Vec<_> = Layout::horizontal([Constraint::Fill(1); 3])
                .spacing(1)
                .split(Rect::new(0, 0, width, 3))
                .iter()
                .copied()
                .collect();
            for row in 0..items.len().div_ceil(columns) {
                let count = (items.len() - row * columns).min(columns);
                areas.extend(
                    Layout::horizontal(vec![Constraint::Fill(1); count])
                        .spacing(1)
                        .split(Rect::new(0, 3 + row as u16 * 3, width, 3))
                        .iter()
                        .copied(),
                );
            }
            let y = 3 + items.len().div_ceil(columns) as u16 * 3;
            if editor.dirty() {
                areas.extend(
                    Layout::horizontal([Constraint::Fill(1); 2])
                        .split(Rect::new(0, y, width, 3))
                        .iter()
                        .copied(),
                );
            }
            areas.push(Rect::new(width.saturating_sub(7), y + 3, 7, 3));
            if let Some(index) = directional_index(
                &areas,
                self.nodes_selected.min(controls.len() - 1),
                direction,
            ) {
                self.nodes_selected = index;
                self.manual_viewport.set(None);
            }
            return;
        }
        let areas: Vec<_> = self.nodes_hits().iter().map(|hit| hit.area).collect();
        let selected = self.nodes_selected.min(areas.len().saturating_sub(1));
        if let Some(index) = directional_index(&areas, selected, direction) {
            self.nodes_selected = index;
        }
    }
    pub(in crate::tui) fn move_spatial(&mut self, direction: KeyCode) -> bool {
        if !matches!(
            direction,
            KeyCode::Up | KeyCode::Down | KeyCode::Left | KeyCode::Right
        ) {
            return false;
        }
        let (controls, selected, offset) = match self.screen {
            Screen::Temporary if !self.temporary_sidebar => {
                (self.temporary_controls(), self.temporary.selected, 0)
            }
            Screen::NodeWizard => {
                let Some(wizard) = &self.node_wizard else {
                    return false;
                };
                let controls = wizard_controls(wizard);
                (controls, wizard.selected, 0)
            }
            Screen::Settings if self.settings_selected > 0 => (
                SettingsPage::ALL
                    .into_iter()
                    .map(Control::SettingsPage)
                    .collect(),
                self.settings_selected - 1,
                1,
            ),
            Screen::SettingsPage(SettingsPage::Profiles) if self.settings_selected > 0 => {
                (self.profile_controls(), self.settings_selected - 1, 1)
            }
            Screen::SettingsPage(SettingsPage::Appearance) if self.settings_selected > 0 => {
                let mut controls: Vec<_> = (0..self.themes.len()).map(Control::Accent).collect();
                controls.extend([Control::AccentInput, Control::SaveAccent, Control::Settings]);
                (controls, self.settings_selected - 1, 1)
            }
            Screen::SettingsPage(SettingsPage::Updates) if self.settings_selected > 0 => {
                let mut controls = vec![Control::CheckWorkstationUpdates];
                if self.self_release.is_some() {
                    controls.push(Control::SelfUpdate);
                }
                controls.push(Control::Settings);
                (controls, self.settings_selected - 1, 1)
            }
            _ => return false,
        };
        // Vertical lists must include entries outside the viewport. In particular,
        // the sidebar Settings button is not the hidden content Back button.
        if matches!(
            self.screen,
            Screen::Settings | Screen::SettingsPage(SettingsPage::Appearance)
        ) && matches!(direction, KeyCode::Up | KeyCode::Down)
        {
            let next = if direction == KeyCode::Down {
                (selected + 1).min(controls.len().saturating_sub(1))
            } else {
                selected.saturating_sub(1)
            };
            self.settings_selected = next + offset;
            return true;
        }
        let visible: Vec<_> = controls
            .iter()
            .enumerate()
            .filter_map(|(index, control)| {
                if *control == Control::Settings
                    && self
                        .hits
                        .iter()
                        .filter(|hit| hit.control == *control)
                        .count()
                        < 2
                {
                    return None;
                }
                self.hits
                    .iter()
                    .rfind(|hit| hit.control == *control)
                    .map(|hit| (index, hit.area))
            })
            .collect();
        if matches!(direction, KeyCode::Up | KeyCode::Down) {
            let next = if direction == KeyCode::Down {
                selected.checked_add(1)
            } else {
                selected.checked_sub(1)
            };
            if let Some(next) = next.filter(|&next| {
                next < controls.len() && !visible.iter().any(|(index, _)| *index == next)
            }) {
                match self.screen {
                    Screen::Temporary => self.temporary.selected = next,
                    Screen::NodeWizard => self.node_wizard.as_mut().unwrap().selected = next,
                    _ => {
                        self.settings_selected = next + offset;
                        if let Control::Installation(index) = controls[next] {
                            self.settings_profile = index;
                        }
                    }
                }
                return true;
            }
        }
        let Some(current) = visible.iter().position(|(index, _)| *index == selected) else {
            return false;
        };
        let areas: Vec<_> = visible.iter().map(|(_, area)| *area).collect();
        if let Some(next) = directional_index(&areas, current, direction) {
            let next = visible[next].0;
            match self.screen {
                Screen::Temporary => self.temporary.selected = next,
                Screen::NodeWizard => self.node_wizard.as_mut().unwrap().selected = next,
                _ => {
                    self.settings_selected = next + offset;
                    if let Some(Control::Installation(index)) = controls.get(next) {
                        self.settings_profile = *index;
                    }
                }
            }
        } else if matches!(direction, KeyCode::Up | KeyCode::Down) {
            // Lists scroll by selecting the next off-screen entry.
            let next = if direction == KeyCode::Down {
                selected.checked_add(1)
            } else {
                selected.checked_sub(1)
            };
            if next.is_some_and(|next| {
                next < controls.len() && !visible.iter().any(|(index, _)| *index == next)
            }) {
                return false;
            }
        }
        true
    }
    pub(in crate::tui) fn section_navigation(&mut self, key: KeyCode) -> bool {
        self.remember_navigation_focus(None);
        let result = self.section_navigation_inner(key);
        self.restore_navigation_context();
        result
    }

    fn section_navigation_inner(&mut self, key: KeyCode) -> bool {
        if key == KeyCode::Esc && self.section_root() {
            let control = match self.screen {
                Screen::Settings => Control::Settings,
                Screen::Access => Control::Access,
                Screen::Temporary => Control::Temporary,
                _ => Control::Action(self.form.action),
            };
            self.idle_selection = self
                .idle_controls()
                .iter()
                .position(|candidate| *candidate == control)
                .unwrap_or(0);
            self.screen = Screen::Idle;
            self.hovered = None;
            self.action_focus = None;
            self.quick_focus = false;
            return true;
        }
        if key == KeyCode::Esc && matches!(self.screen, Screen::SettingsPage(_)) {
            self.open_settings();
            return true;
        }
        if key == KeyCode::Right {
            match self.screen {
                Screen::Form
                    if !self.quick_focus && self.form.selected == 0 && self.actions_enabled() =>
                {
                    self.form.next();
                    return true;
                }
                Screen::Settings if self.settings_selected == 0 => {
                    self.settings_selected = 1;
                    return true;
                }
                _ => {}
            }
        }
        false
    }
    pub(in crate::tui) fn section_root(&self) -> bool {
        match self.screen {
            Screen::Form | Screen::Settings => true,
            Screen::Controller => {
                !self
                    .controller_controls()
                    .contains(&Control::ControllerBack)
                    && !self.controller.plan_pending
            }
            Screen::Nodes => self.nodes.card.is_none() && self.nodes.editor.is_none(),
            Screen::Access => {
                self.access.card.is_none()
                    && self.access.editor.is_none()
                    && self.access.devices.is_none()
            }
            Screen::Temporary => self.temporary.view == crate::temporary::View::List,
            _ => false,
        }
    }
    pub(in crate::tui) fn idle_controls(&self) -> Vec<Control> {
        let mut hits: Vec<_> = self
            .hits
            .iter()
            .filter(|hit| {
                matches!(
                    hit.control,
                    Control::Action(_)
                        | Control::Access
                        | Control::Temporary
                        | Control::Settings
                        | Control::QuickProfile
                )
            })
            .collect();
        hits.sort_by_key(|hit| (hit.area.y, hit.area.x));
        hits.into_iter().map(|hit| hit.control).collect()
    }
    pub(in crate::tui) fn restore_action_focus(&mut self) -> bool {
        let Some(control) = self.action_focus else {
            return false;
        };
        let control = if self.hits.iter().any(|hit| hit.control == control) {
            control
        } else {
            match control {
                Control::AccessGrantPage(next) => Control::AccessGrantPage(!next),
                Control::AccessPage(next) => Control::AccessPage(!next),
                Control::AccessDevicePage(next) => Control::AccessDevicePage(!next),
                Control::AccessExpiryPage(next) => Control::AccessExpiryPage(!next),
                _ => control,
            }
        };
        let before = (
            self.nodes_selected,
            self.access.selected,
            self.controller.selected,
            self.temporary.selected,
            self.settings_selected,
        );
        self.focus(control);
        before
            != (
                self.nodes_selected,
                self.access.selected,
                self.controller.selected,
                self.temporary.selected,
                self.settings_selected,
            )
    }
    pub(in crate::tui) fn list_position(
        &self,
        control: Control,
        controls: &[Control],
    ) -> Option<(usize, usize)> {
        if let Control::NodesRegion(index) | Control::NodesCard(index) = control {
            let node = self.nodes.items.get(index)?;
            let term = self.nodes.search.trim().to_lowercase();
            let mut nodes: Vec<_> = self
                .nodes
                .items
                .iter()
                .enumerate()
                .filter(|(_, n)| {
                    [&n.title, &n.region, &n.key]
                        .iter()
                        .any(|s| s.to_lowercase().contains(&term))
                })
                .collect();
            if matches!(control, Control::NodesRegion(_)) {
                let regions: std::collections::BTreeSet<_> =
                    nodes.iter().map(|(_, n)| &n.region).collect();
                return Some((
                    regions.iter().position(|region| **region == node.region)? + 1,
                    regions.len(),
                ));
            }
            nodes.retain(|(_, n)| n.region == node.region);
            nodes.sort_by_key(|(_, n)| (&n.title, &n.key));
            return Some((
                nodes.iter().position(|(i, _)| *i == index)? + 1,
                nodes.len(),
            ));
        }
        if let Control::AccessItem(index) = control {
            let indices = self.access.filtered();
            return Some((indices.iter().position(|i| *i == index)? + 1, indices.len()));
        }
        if let Control::AccessDeviceItem(index) = control {
            let devices = self.access.devices.as_ref()?;
            return (index < devices.items.len()).then_some((index + 1, devices.items.len()));
        }
        if let Control::TemporaryChoice(index) = control {
            use crate::temporary::View;
            return match self.temporary.view {
                View::List => Some((
                    self.temporary.page * self.temporary.loaded_size + index + 1,
                    self.temporary.total,
                )),
                View::Server => Some((index + 1, self.temporary.nodes.len())),
                _ => {
                    let count = controls
                        .iter()
                        .filter(|c| matches!(c, Control::TemporaryChoice(_)))
                        .count();
                    Some((index + 1, count))
                }
            };
        }
        if matches!(self.screen, Screen::Controller)
            && self.controller.defaults.is_some()
            && self.controller.defaults_input.is_none()
        {
            let fields: Vec<_> = controls
                .iter()
                .copied()
                .filter(|c| {
                    matches!(
                        c,
                        Control::ControllerDefaultsToggle(_)
                            | Control::ControllerDefaultsPreset
                            | Control::ControllerDefaultsMode
                            | Control::ControllerDefaultsField(_)
                            | Control::ControllerDefaultsAdvanced
                    )
                })
                .collect();
            return Some((fields.iter().position(|c| *c == control)? + 1, fields.len()));
        }
        let indexed = matches!(
            control,
            Control::Accent(_)
                | Control::Installation(_)
                | Control::SettingsPage(_)
                | Control::Field(_)
                | Control::EditField(_)
                | Control::WizardChoice(_)
                | Control::NodesEditField(_)
                | Control::NodesEditChoice(_)
                | Control::AccessField(_)
                | Control::AccessDeviceItem(_)
                | Control::AccessExpiry(_)
                | Control::ControllerDefaultsField(_)
        );
        let group: Vec<_> = controls
            .iter()
            .copied()
            .filter(|candidate| {
                if indexed {
                    std::mem::discriminant(candidate) == std::mem::discriminant(&control)
                } else if matches!(self.screen, Screen::Controller) {
                    !matches!(
                        candidate,
                        Control::ControllerBack
                            | Control::ControllerRefresh
                            | Control::ControllerScope
                            | Control::ControllerPhrase
                    )
                } else {
                    false
                }
            })
            .collect();
        Some((
            group.iter().position(|candidate| *candidate == control)? + 1,
            group.len(),
        ))
    }
    pub(in crate::tui) fn focus(&mut self, control: Control) {
        if control == Control::WindowClose {
            return;
        }
        // Modal controls own focus regardless of the underlying screen.
        if self.exit {
            if let Control::Exit(value) = control {
                self.exit_confirm = value;
            }
            return;
        }
        if self.discard.is_some() {
            if let Control::Discard(value) = control {
                self.discard_confirm = value;
            }
            return;
        }
        if matches!(self.screen, Screen::Idle) {
            if let Some(index) = self
                .idle_controls()
                .iter()
                .position(|candidate| *candidate == control)
            {
                self.idle_selection = index;
            }
            return;
        }
        if matches!(self.screen, Screen::Controller)
            && let Some(index) = self
                .controller_controls()
                .iter()
                .position(|c| *c == control)
        {
            self.controller.selected = index;
            self.controller_sidebar = false;
            return;
        }
        // Focus is deliberately separate from activation: no network requests,
        // toggles, page changes or writes happen when the pointer moves.
        match control {
            Control::AccessSearch | Control::AccessField(_) | Control::AccessDurationInput => {
                if let Some(index) = self.access_controls().iter().position(|c| *c == control) {
                    self.access.selected = index;
                }
            }
            Control::Field(index) => {
                self.quick_focus = false;
                self.form.selected = index;
            }
            Control::EditField(index) => {
                if let Some(editor) = &mut self.editor {
                    editor.2 = index;
                }
            }
            Control::Channel(editor, _) => {
                if editor {
                    if let Some(editor) = &mut self.editor {
                        editor.2 = 4;
                    }
                } else if let Some(index) = self.form.visible().iter().position(|&i| i == 3) {
                    self.quick_focus = false;
                    self.form.selected = index + 1;
                }
            }
            Control::RepairMode => {
                if let Some(index) = self.form.visible().iter().position(|&i| i == 11) {
                    self.quick_focus = false;
                    self.form.selected = index + 1;
                }
            }
            Control::Continue => {
                self.quick_focus = false;
                self.form.selected = self.form.count() - 1;
            }
            Control::Accent(index) => self.settings_selected = index + 1,
            Control::AccentInput => self.settings_selected = self.themes.len() + 1,
            Control::Settings if matches!(self.screen, Screen::SettingsPage(_)) => {
                self.settings_selected =
                    match self.screen {
                        Screen::SettingsPage(SettingsPage::Appearance) => self.themes.len() + 3,
                        Screen::SettingsPage(SettingsPage::Profiles) => {
                            self.profile_controls().len()
                        }
                        Screen::SettingsPage(SettingsPage::Updates) => {
                            if self.self_release.is_some() { 3 } else { 2 }
                        }
                        _ => 1,
                    };
            }
            Control::SaveAccent => self.settings_selected = self.themes.len() + 2,
            Control::Installation(index) => self.settings_selected = index + 1,
            Control::SettingsPage(page) => {
                if matches!(self.screen, Screen::Settings) {
                    self.settings_selected =
                        SettingsPage::ALL.iter().position(|p| *p == page).unwrap() + 1;
                }
            }
            Control::Confirm(value) | Control::ConfirmDelete(value) => self.confirm = value,
            Control::Prompt(value) => self.trust = value,
            Control::Administrator(index) => self.administrator_selected = index,
            Control::WindowClose => {}
            Control::Exit(value) => self.exit_confirm = value,
            Control::Discard(value) => self.discard_confirm = value,
            _ => {
                if matches!(self.screen, Screen::Access) {
                    if let Some(index) = self.access_controls().iter().position(|c| *c == control) {
                        self.access.selected = index;
                    }
                } else if matches!(self.screen, Screen::Nodes) {
                    if let Some(index) = self.nodes_controls().iter().position(|c| *c == control) {
                        self.nodes_selected = index;
                        self.nodes_sidebar = false;
                    }
                } else if matches!(self.screen, Screen::Temporary) {
                    if let Some(index) =
                        self.temporary_controls().iter().position(|c| *c == control)
                    {
                        self.temporary.selected = index;
                        self.temporary_sidebar = false;
                    }
                } else if matches!(self.screen, Screen::NodeWizard)
                    && let Some(wizard) = &mut self.node_wizard
                {
                    let controls = wizard_controls(wizard);
                    if let Some(index) = controls.iter().position(|c| *c == control) {
                        wizard.selected = index;
                    }
                }
            }
        }
    }
}
