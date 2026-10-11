//! Application state transitions and action handlers.
use super::*;

impl App {
    pub(super) fn new(r: &Request) -> Self {
        Self {
            image_picker: None,
            qr: std::cell::RefCell::new(None),
            qr_graphic: std::cell::RefCell::new(None),
            clipboard: None,
            qr_visible: false,
            temporary: crate::temporary::Browser::default(),
            temporary_sidebar: false,
            temporary_busy: false,
            nodes: crate::nodes::Browser::default(),
            nodes_requested: None,
            nodes_busy: false,
            loading_message: String::new(),
            nodes_background: false,
            node_wizard: None,
            wizard_loading: false,
            ssh_state: crate::nodes::ConnectionState::Closed,
            nodes_selected: 0,
            action_focus: None,
            nodes_sidebar: false,
            nodes_updates_origin: false,
            nodes_capacity: std::cell::Cell::new(10),
            self_release: None,
            self_status: "Checking for workstation updates…".into(),
            self_requested: false,
            self_check_requested: false,
            form: Form::new(r),
            screen: Screen::Form,
            error: String::new(),
            stage: String::new(),
            tracker: Tracker::default(),
            prompt: None,
            administrator_selected: 0,
            password: Zeroizing::new(String::new()),
            trust: false,
            outcome: None,
            update: None,
            node_page: 0,
            stop: r.workflow.stop.clone(),
            controller: crate::controller::Browser::default(),
            installed_profile: None,
            fresh_installation: false,
            controller_sidebar: false,
            controller_section: None,
            requests_settings: false,
            confirmed_installation: None,
            probe_requested: false,
            ignore_nodes_updates: false,
            access: crate::access::Browser::default(),
            access_busy: false,
            access_capacity: std::cell::Cell::new(10),
            hits: Vec::new(),
            hovered: None,
            pointer_active: false,
            pointer_position: None,
            idle_selection: 0,
            content_overflow: std::cell::Cell::new(false),
            controller_viewport: std::cell::Cell::new(0),
            controller_width: std::cell::Cell::new(64),
            manual_viewport: std::cell::Cell::new(None),
            list_viewport: std::cell::Cell::new(0),
            list_max_viewport: std::cell::Cell::new(0),
            scrollbar: std::cell::Cell::new(None),
            scroll_track: std::cell::Cell::new(Rect::default()),
            scroll_drag: None,
            confirm: true,
            connections: Connections::default(),
            state_dir: r.state_dir.clone(),
            session_id: uuid::Uuid::new_v4(),
            settings_selected: 0,
            settings_profile: 0,
            accent_input: String::new(),
            themes: theme::defaults(),
            editor: None,
            exit: false,
            exit_confirm: false,
            discard: None,
            discard_confirm: false,
            discard_bypass: false,
            quick_focus: true,
            result_scroll: 0,
            result_max_scroll: std::cell::Cell::new(0),
            result_page_size: std::cell::Cell::new(1),
        }
    }
    pub(super) fn load_connections(&mut self, request: &Request) -> Result<()> {
        self.connections = Connections::load(&self.state_dir)?;
        let catalog = theme::load(&self.state_dir)?;
        self.themes = catalog.themes;
        if !catalog.warnings.is_empty() {
            self.error = catalog.warnings.join("; ");
        }
        self.connections.import_update_history(&self.state_dir)?;
        let selected = if request.host.is_empty() {
            self.connections.active()
        } else {
            self.connections
                .installations
                .iter()
                .find(|p| p.matches(&request.host, request.port, &request.user))
        };
        if let Some(profile) = selected.cloned() {
            self.quick_focus = false;
            self.settings_profile = self
                .connections
                .installations
                .iter()
                .position(|p| p.id == profile.id)
                .unwrap();
            self.form.use_installation(&profile);
            // Explicit command options take precedence over stored preferences.
            if !request.host.is_empty() {
                self.form.fields[3] = request.branch.clone();
                self.form.fields[4] = request.tag.clone();
                if !request.admin_ids.is_empty() {
                    self.form.fields[5] = request.admin_ids.clone();
                }
                if !request.workflow.account.is_empty() {
                    self.form.fields[7] = request.workflow.account.clone();
                }
                self.form.saved = Some(self.form.installation());
            }
        }
        Ok(())
    }
    pub(super) fn open_settings(&mut self) {
        self.quick_focus = false;
        self.settings_selected = match self.screen {
            Screen::SettingsPage(page) => SettingsPage::ALL
                .iter()
                .position(|entry| *entry == page)
                .map_or(1, |index| index + 1),
            _ => 0,
        };
        self.screen = Screen::Settings;
        self.error.clear();
    }
    pub(super) fn appearance_key(&mut self, key: KeyCode) {
        if self.move_spatial(key) {
            return;
        }
        let count = self.themes.len() + 3;
        match key {
            KeyCode::Esc => self.open_settings(),
            KeyCode::Down | KeyCode::Tab => {
                self.settings_selected = (self.settings_selected + 1).min(count)
            }
            KeyCode::Up | KeyCode::BackTab => {
                self.settings_selected = self.settings_selected.saturating_sub(1).max(1)
            }
            KeyCode::Char(c)
                if self.settings_selected == self.themes.len() + 1
                    && self.accent_input.len() < 7
                    && (c == '#' || c.is_ascii_hexdigit()) =>
            {
                self.accent_input.push(c)
            }
            KeyCode::Backspace if self.settings_selected == self.themes.len() + 1 => {
                self.accent_input.pop();
            }
            KeyCode::Enter => {
                let control = match self.settings_selected {
                    i if i > 0 && i <= self.themes.len() => Control::Accent(i - 1),
                    i if i == self.themes.len() + 1 || i == self.themes.len() + 2 => {
                        Control::SaveAccent
                    }
                    _ => Control::Settings,
                };
                self.activate(control);
            }
            _ => {}
        }
    }
    pub(super) fn profile_controls(&self) -> Vec<Control> {
        let mut controls: Vec<_> = (0..self.connections.installations.len())
            .map(Control::Installation)
            .collect();
        if !controls.is_empty() {
            controls.extend([
                Control::UseInstallation,
                Control::EditInstallation,
                Control::NewInstallation,
                Control::DeleteInstallation,
            ]);
        } else {
            controls.push(Control::NewInstallation);
        }
        controls.push(Control::Settings);
        controls
    }
    pub(super) fn delete_profile(&mut self, id: uuid::Uuid) -> Result<()> {
        let mut next = self.connections.clone();
        let removed = next
            .installations
            .iter()
            .find(|profile| profile.id == id)
            .cloned();
        ensure!(
            next.installations.iter().any(|profile| profile.id == id),
            "Saved profile no longer exists."
        );
        next.installations.retain(|profile| profile.id != id);
        if next.selected == Some(id) {
            next.selected = None;
        }
        next.save(&self.state_dir)?;
        if self
            .form
            .saved
            .as_ref()
            .is_some_and(|profile| profile.id == id)
        {
            self.form.saved = None;
            self.form.fields[6].zeroize();
            self.password.zeroize();
            for index in [0, 3, 4, 5, 7, 8] {
                self.form.fields[index].clear();
            }
            self.form.fields[1] = "22".into();
            self.form.fields[2] = "root".into();
            self.form.fields[9] = "22".into();
            self.form.fields[10] = "root".into();
            self.form.selected = 0;
        }
        self.connections = next;
        self.settings_profile = self
            .settings_profile
            .min(self.connections.installations.len().saturating_sub(1));
        self.settings_selected = 1;
        self.screen = Screen::SettingsPage(SettingsPage::Profiles);
        self.error.clear();
        if let Some(removed) = removed
            && !self
                .connections
                .installations
                .iter()
                .any(|p| p.host.eq_ignore_ascii_case(&removed.host) && p.port == removed.port)
        {
            crate::ssh::forget_host(&self.state_dir, &removed.host, removed.port)?;
        }
        Ok(())
    }
    pub(super) fn navigate(&mut self, backwards: bool) {
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
    pub(super) fn temporary_command(&mut self, command: crate::temporary::Command) {
        self.nodes_requested = Some(crate::nodes::Command::Temporary(command));
        self.error.clear();
    }
    pub(super) fn open_temporary(&mut self, filter: Option<crate::nodes::Node>) {
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
    pub(super) fn controller_command(&mut self, command: crate::controller::Command) {
        if self.nodes_busy {
            return;
        }
        self.nodes_requested = Some(crate::nodes::Command::Controller(
            self.form.saved.as_ref().map(|p| p.id),
            command,
        ));
        self.error.clear();
    }
    pub(super) fn controller_ready(&self) -> bool {
        self.form
            .saved
            .as_ref()
            .is_some_and(|p| self.confirmed_installation == Some(p.id))
    }
    pub(super) fn has_installation(&self) -> bool {
        self.form
            .saved
            .as_ref()
            .is_some_and(|p| p.installed || self.installed_profile == Some(p.id))
    }
    pub(super) fn remember_installed(&mut self) {
        let Some(saved) = &self.form.saved else {
            return;
        };
        self.installed_profile = Some(saved.id);
        self.confirmed_installation = Some(saved.id);
        if saved.installed {
            return;
        }
        let mut next = self.connections.clone();
        if let Some(profile) = next.installations.iter_mut().find(|p| p.id == saved.id) {
            profile.installed = true;
            match next.save(&self.state_dir) {
                Ok(()) => {
                    self.connections = next;
                    if let Some(saved) = &mut self.form.saved {
                        saved.installed = true;
                    }
                }
                Err(error) => self.error = format!("Could not save installation state: {error}"),
            }
        }
    }
    pub(super) fn open_controller(&mut self) {
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
        self.controller_section = None;
        self.requests_settings = false;
        self.controller.updates = None;
        self.controller.versions = None;
        self.controller.components = None;
        self.controller.update_result = None;
        self.controller.branch_confirmation = false;
        self.controller.defaults = None;
        self.controller.defaults_input = None;
        self.controller.policies = None;
        self.controller.bot_title = None;
        self.controller.ssh_key = None;
        self.controller.alerts = None;
        self.controller.workstation_audit = None;
        self.controller.recovery_history = None;
        self.controller.history_detail = None;
        self.controller.backups = None;
        self.controller.backup_catalog = None;
        self.controller.backup_detail = None;
        self.controller.announcement_overview = None;
        self.controller.announcement_draft = None;
        self.controller.announcement_preview = None;
        self.controller.announcement_job = None;
        self.controller_viewport.set(0);
        self.controller.plan = None;
        self.controller.phrase.clear();
        self.controller.selected = 0;
        self.screen = Screen::Controller;
        self.quick_focus = false;
        self.controller_sidebar = false;
        if let Some(saved) = &self.form.saved
            && let Err(error) = self.controller.restore_verification(&self.state_dir, saved)
        {
            self.error = format!("Could not resume removal verification: {error}");
        }
        if let Some(command) = self.controller.poll() {
            self.controller_command(command);
        } else {
            self.controller_command(if self.controller_ready() {
                crate::controller::Command::Overview
            } else {
                crate::controller::Command::Probe
            });
        }
    }
    pub(super) fn controller_controls(&self) -> Vec<Control> {
        let state = &self.controller;
        if state.plan_pending {
            return Vec::new();
        }
        let mut controls = Vec::new();
        if state.branch_confirmation {
            return vec![
                Control::ControllerUpdateBranchConfirm(true),
                Control::ControllerUpdateBranchConfirm(false),
            ];
        }
        if state.history_detail.is_some() {
            return vec![Control::ControllerBack];
        }
        if let Some(audit) = &state.workstation_audit {
            return history_controls(audit);
        }
        if let Some(history) = &state.recovery_history {
            return history_controls(history);
        }
        if let Some(job) = &state.announcement_job {
            let mut controls = Vec::new();
            if job["status"] == "running" {
                controls.push(Control::ControllerAnnouncementRefresh);
            }
            controls.extend([
                Control::ControllerAnnouncementCompose,
                Control::ControllerBack,
            ]);
            return controls;
        }
        if state.announcement_preview.is_some() {
            return vec![
                Control::ControllerAnnouncementSend,
                Control::ControllerAnnouncementEdit,
                Control::ControllerBack,
            ];
        }
        if state.announcement_draft.is_some() {
            return vec![
                Control::ControllerAnnouncementInput,
                Control::ControllerAnnouncementPreview,
                Control::ControllerBack,
            ];
        }
        if state.announcement_overview.is_some() {
            return vec![
                Control::ControllerAnnouncementCompose,
                Control::ControllerBack,
            ];
        }
        if state.alerts.is_some() {
            return alert_controls(state);
        }
        if state.defaults_input.is_some() {
            return vec![
                Control::ControllerDefaultsInput,
                Control::ControllerDefaultsInputApply,
                Control::ControllerBack,
            ];
        }
        if state.bot_title.is_some() {
            return vec![Control::ControllerBotTitleEdit, Control::ControllerBack];
        }
        if state.ssh_key.is_some() {
            return vec![Control::ControllerSshKeyCopy, Control::ControllerBack];
        }
        if state.policies.is_some() && self.requests_settings {
            return vec![
                Control::ControllerPolicyToggle("requests"),
                Control::ControllerPolicyToggle("notifications"),
                Control::ControllerGateMessage,
                Control::ControllerBack,
            ];
        }
        if state.defaults.is_some() {
            return defaults_controls(&self.controller);
        }
        if let Some(detail) = &state.backup_detail {
            return if detail["compatible"] == true {
                vec![
                    Control::ControllerBackupRestore,
                    Control::ControllerBackupDelete,
                    Control::ControllerBack,
                ]
            } else {
                vec![Control::ControllerBackupDelete, Control::ControllerBack]
            };
        }
        if let Some(catalog) = &state.backup_catalog {
            return backup_catalog_controls(catalog);
        }
        if state.backups.is_some() {
            return vec![
                Control::ControllerBackupCreate,
                Control::ControllerBackupCatalog(0),
                Control::ControllerBackupAuto,
                Control::ControllerBackupInterval,
                Control::ControllerBackupKeep,
                Control::ControllerBackups,
                Control::ControllerBack,
            ];
        }
        if let Some(result) = &state.update_result {
            let count = result["items"].as_array().map_or(0, Vec::len);
            let mut controls = Vec::new();
            if state.components_page > 0 {
                controls.push(Control::ControllerComponentPage(false));
            }
            if (state.components_page + 1) * 5 < count {
                controls.push(Control::ControllerComponentPage(true));
            }
            if update_operation_active(&result["status"]) {
                controls.push(Control::ControllerUpdateResult);
            }
            if !matches!(
                result["status"].as_str(),
                Some("awaiting_executor" | "running")
            ) {
                controls.push(Control::ControllerUpdateDismiss);
            }
            controls.push(Control::ControllerBack);
            return controls;
        }
        if let Some(components) = &state.components {
            let count = components["nodes"].as_array().map_or(0, Vec::len);
            let start = state.components_page * 5;
            let mut controls: Vec<_> = (start..(start + 5).min(count))
                .map(Control::ControllerComponentNode)
                .collect();
            if start > 0 {
                controls.push(Control::ControllerComponentPage(false));
            }
            if start + 5 < count {
                controls.push(Control::ControllerComponentPage(true));
            }
            if components
                .get("latest_job")
                .is_some_and(|job| update_operation_active(&job["status"]))
            {
                controls.push(Control::ControllerComponents);
            }
            controls.push(Control::ControllerBack);
            return controls;
        }
        if let Some(versions) = &state.versions {
            let mut controls: Vec<_> = (0..versions["items"].as_array().map_or(0, Vec::len))
                .map(Control::ControllerVersion)
                .collect();
            let offset = versions["offset"].as_u64().unwrap_or(0) as usize;
            if offset > 0 {
                controls.push(Control::ControllerVersionsPage(offset.saturating_sub(8)));
            }
            if let Some(next) = versions["next_offset"].as_u64() {
                controls.push(Control::ControllerVersionsPage(next as usize));
            }
            controls.push(Control::ControllerBack);
            return controls;
        }
        if let Some(updates) = &state.updates {
            let mut controls = vec![
                Control::ControllerUpdateCheck,
                Control::ControllerVersions,
                Control::ControllerComponents,
                Control::ControllerUpdateBranch,
                Control::ControllerUpdateAuto,
                Control::ControllerUpdateInterval,
                Control::ControllerBack,
            ];
            if update_result_available(updates) {
                controls.insert(3, Control::ControllerUpdateResult);
            }
            return controls;
        }
        if self.form.action == Action::Update {
            return vec![Control::ControllerUpdates, Control::ControllerBack];
        }
        if state.removed || !self.controller_ready() && !state.verifying {
            return vec![
                Control::ControllerNew,
                Control::ControllerCheck,
                Control::ControllerBack,
            ];
        }
        if state.plan.is_some() {
            return vec![
                Control::ControllerPhrase,
                Control::ControllerConfirm,
                Control::ControllerBack,
            ];
        }
        if let Some(job) = &state.job {
            if job["status"] == "blocked" {
                controls.push(Control::ControllerRetry);
            }
            if job["status"] == "awaiting_shutdown" && !state.verifying {
                controls.push(Control::ControllerShutdown);
            }
        }
        let active = state.verifying
            || state.job.as_ref().is_some_and(|j| {
                matches!(
                    j["status"].as_str(),
                    Some("queued" | "running" | "blocked" | "awaiting_shutdown")
                )
            });
        controls.extend(
            self.controller_root_controls(active)
                .into_iter()
                .map(|(_, control)| control),
        );
        controls
    }
    pub(super) fn controller_root_controls(&self, active: bool) -> Vec<(String, Control)> {
        use ControllerSection::*;
        let section =
            |value: ControllerSection| (value.label().into(), Control::ControllerSection(value));
        let mut controls: Vec<(String, Control)> = match self.controller_section {
            None => vec![
                section(Installation),
                section(Policies),
                ("Backups".into(), Control::ControllerBackups),
                section(Monitoring),
                ("Announcements".into(), Control::ControllerAnnouncement),
                section(Removal),
            ],
            Some(Installation) => vec![
                ("Installation defaults".into(), Control::ControllerDefaults),
                ("Bot name".into(), Control::ControllerBotTitle),
                ("SSH key".into(), Control::ControllerSshKey),
                ("New installation".into(), Control::ControllerNew),
            ],
            Some(Policies) => {
                let mut items = Vec::new();
                if let Some(value) = &self.controller.policies {
                    items.push((
                        format!(
                            "Traffic collection: {}",
                            if value["traffic"]["enabled"] == true {
                                "On"
                            } else {
                                "Off"
                            }
                        ),
                        Control::ControllerPolicyToggle("traffic"),
                    ));
                }
                items.push(("Requests settings".into(), Control::ControllerPolicies));
                items
            }
            Some(Monitoring) => vec![
                ("Alerts".into(), Control::ControllerAlerts),
                ("Workstation audit".into(), Control::ControllerAudit),
                (
                    "Recovery history".into(),
                    Control::ControllerRecoveryHistory,
                ),
            ],
            Some(Removal) => {
                let mut items = Vec::new();
                if !active
                    && self
                        .controller
                        .overview
                        .as_ref()
                        .map_or(self.controller_ready(), |v| v["supported"] == true)
                {
                    items.extend([
                        (
                            format!(
                                "[{}] Uninstall nodes too",
                                if self.controller.cleanup_nodes {
                                    "✓"
                                } else {
                                    " "
                                }
                            ),
                            Control::ControllerScope,
                        ),
                        ("Reset controller".into(), Control::ControllerPlan(false)),
                        ("Remove Node Plane".into(), Control::ControllerPlan(true)),
                    ]);
                }
                items.push(("Refresh".into(), Control::ControllerRefresh));
                items
            }
        };
        if self.controller_section.is_some() {
            controls.push(("Back".into(), Control::ControllerBack));
        }
        controls
    }
    pub(super) fn controller_key(&mut self, key: crossterm::event::KeyEvent) {
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.exit = true;
            self.exit_confirm = false;
            return;
        }
        if self.controller_sidebar {
            match key.code {
                KeyCode::Down | KeyCode::Up => self.navigate(key.code == KeyCode::Up),
                KeyCode::Enter | KeyCode::Right | KeyCode::Tab => {
                    self.controller_sidebar = false;
                    self.quick_focus = false;
                }
                KeyCode::Esc => {
                    self.exit = true;
                    self.exit_confirm = false;
                }
                _ => {}
            }
            return;
        }
        if key.code == KeyCode::Esc {
            self.activate(Control::ControllerBack);
            return;
        }
        let controls = self.controller_controls();
        if controls.is_empty() {
            return;
        }
        let current = self.controller.selected.min(controls.len() - 1);
        if self.controller.defaults.is_some()
            && self.controller.defaults_input.is_none()
            && matches!(
                key.code,
                KeyCode::Up | KeyCode::Down | KeyCode::Left | KeyCode::Right
            )
        {
            let width = self.controller_width.get();
            let mut areas = defaults_rects(&controls, Rect::new(0, 0, width, 0));
            let height = defaults_rows(&controls, width).len() as u16 * 3;
            if let Some(index) = controls
                .iter()
                .position(|control| *control == Control::ControllerBack)
            {
                areas[index] = Rect::new(width.saturating_sub(7), height, 7, 3);
            }
            if let Some(next) = directional_index(&areas, current, key.code) {
                self.controller.selected = next;
            }
            return;
        }
        match key.code {
            KeyCode::Enter => {
                self.activate(controls[current]);
            }
            KeyCode::Down => self.controller.selected = (current + 1).min(controls.len() - 1),
            KeyCode::Up => self.controller.selected = current.saturating_sub(1),
            KeyCode::Tab => self.controller.selected = (current + 1) % controls.len(),
            KeyCode::BackTab => {
                self.controller.selected = (current + controls.len() - 1) % controls.len()
            }
            KeyCode::Left | KeyCode::Right => {
                let visible: Vec<_> = controls
                    .iter()
                    .enumerate()
                    .filter_map(|(index, c)| {
                        self.hits
                            .iter()
                            .find(|h| h.control == *c)
                            .map(|h| (index, h.area))
                    })
                    .collect();
                if let Some(position) = visible.iter().position(|(index, _)| *index == current) {
                    let rects: Vec<_> = visible.iter().map(|(_, area)| *area).collect();
                    if let Some(next) = directional_index(&rects, position, key.code) {
                        self.controller.selected = visible[next].0;
                    }
                }
            }
            KeyCode::Char(c)
                if controls[current] == Control::ControllerDefaultsInput && !c.is_control() =>
            {
                if let Some((_, value)) = &mut self.controller.defaults_input
                    && value.chars().count() < 500
                {
                    value.push(c);
                }
            }
            KeyCode::Backspace if controls[current] == Control::ControllerDefaultsInput => {
                if let Some((_, value)) = &mut self.controller.defaults_input {
                    value.pop();
                }
            }
            KeyCode::Char(c)
                if controls[current] == Control::ControllerAnnouncementInput && !c.is_control() =>
            {
                if let Some(value) = &mut self.controller.announcement_draft
                    && value.chars().count() < 3000
                {
                    value.push(c);
                }
            }
            KeyCode::Backspace if controls[current] == Control::ControllerAnnouncementInput => {
                if let Some(value) = &mut self.controller.announcement_draft {
                    value.pop();
                }
            }
            KeyCode::Char(c)
                if controls[current] == Control::ControllerPhrase && !c.is_control() =>
            {
                if self.controller.phrase.len() < 64 {
                    self.controller.phrase.push(c);
                }
            }
            KeyCode::Backspace if controls[current] == Control::ControllerPhrase => {
                self.controller.phrase.pop();
            }
            _ => {}
        }
    }
    pub(super) fn access_command(&mut self, command: crate::access::Command) {
        self.nodes_requested = Some(crate::nodes::Command::Access(command));
    }
    pub(super) fn open_access(&mut self) {
        if !self.controller_ready() {
            return;
        }
        let profile = self.form.saved.as_ref().map(|p| p.id);
        if self.access.profile != profile {
            self.access = crate::access::Browser {
                profile,
                ..Default::default()
            };
        }
        self.access.card = None;
        self.access.devices = None;
        self.access.decision = None;
        self.access.editor = None;
        self.access.profile_action = None;
        self.access.account_action = None;
        self.access.owner_filter = None;
        self.screen = Screen::Access;
        self.quick_focus = false;
        self.access_command(crate::access::Command::List(self.access.tab));
    }
    pub(super) fn access_controls(&self) -> Vec<Control> {
        self.hits
            .iter()
            .filter_map(|hit| {
                matches!(
                    hit.control,
                    Control::AccessTab(_)
                        | Control::AccessItem(_)
                        | Control::AccessSearch
                        | Control::AccessRefresh
                        | Control::AccessBack
                        | Control::AccessPage(_)
                        | Control::AccessDecision(_)
                        | Control::AccessConfirm
                        | Control::AccessEdit
                        | Control::AccessNew
                        | Control::AccessField(_)
                        | Control::AccessSave
                        | Control::AccessProfileAction(_)
                        | Control::AccessAccountAction(_)
                        | Control::AccessDevices
                        | Control::AccessDeviceItem(_)
                        | Control::AccessDeviceNew
                        | Control::AccessDeviceEdit
                        | Control::AccessDeviceName
                        | Control::AccessDeviceSave
                        | Control::AccessDeviceDelete
                        | Control::AccessDeviceConfirm
                        | Control::AccessDevicePage(_)
                        | Control::AccessExpiryMenu
                        | Control::AccessExpiry(_)
                        | Control::AccessExpiryPage(_)
                        | Control::AccessExpiryBack
                        | Control::AccessAccountConfirm
                        | Control::AccessProfileConfirm
                        | Control::AccessGrants
                        | Control::AccessGrant(_, _)
                        | Control::AccessRule(_, _)
                        | Control::AccessGrantMode(_)
                        | Control::AccessGrantPage(_)
                        | Control::AccessGrantBack
                        | Control::AccessGrantAll(_)
                )
                .then_some(hit.control)
            })
            .collect()
    }
    pub(super) fn access_key(&mut self, key: KeyEvent) {
        let controls = self.access_controls();
        if controls.get(self.access.selected) == Some(&Control::AccessDeviceName)
            && let Some(editor) = self.access.devices.as_mut().and_then(|d| d.editor.as_mut())
        {
            match key.code {
                KeyCode::Char(c)
                    if !key.modifiers.contains(KeyModifiers::CONTROL) && !c.is_control() =>
                {
                    if editor.name.chars().count() < 64 {
                        editor.name.push(c);
                        editor.key = uuid::Uuid::new_v4();
                    }
                    return;
                }
                KeyCode::Backspace => {
                    editor.name.pop();
                    editor.key = uuid::Uuid::new_v4();
                    return;
                }
                _ => {}
            }
        }
        if let Some(Control::AccessField(index)) = controls.get(self.access.selected).copied()
            && let Some(editor) = &mut self.access.editor
        {
            match key.code {
                KeyCode::Char(c)
                    if !key.modifiers.contains(KeyModifiers::CONTROL) && !c.is_control() =>
                {
                    if editor.fields[index].chars().count() < 128 {
                        editor.fields[index].push(c);
                        editor.key = uuid::Uuid::new_v4();
                    }
                    return;
                }
                KeyCode::Backspace => {
                    editor.fields[index].pop();
                    editor.key = uuid::Uuid::new_v4();
                    return;
                }
                _ => {}
            }
        }
        match key.code {
            KeyCode::Esc => {
                if self.access.devices.is_some() {
                    self.activate(Control::AccessBack);
                    return;
                }
                if let Some(editor) = &mut self.access.editor
                    && editor.expiry_view
                {
                    editor.expiry_view = false;
                    self.access.selected = 0;
                    return;
                }
                if let Some(editor) = &mut self.access.editor
                    && editor.grants_view
                {
                    editor.grants_view = false;
                    self.access.selected = 0;
                    return;
                }
                if self.access.editor.is_some() {
                    self.activate(Control::AccessBack);
                    return;
                }
                if self.access.profile_action.take().is_some()
                    || self.access.account_action.take().is_some()
                {
                    return;
                }
                if self.access.decision.take().is_some() {
                    return;
                }
                if self.access.card.take().is_some() {
                    self.access_command(crate::access::Command::List(self.access.tab));
                } else {
                    self.screen = Screen::Form;
                }
            }
            KeyCode::Tab => {
                self.access.selected =
                    (self.access.selected + 1).min(controls.len().saturating_sub(1))
            }
            KeyCode::BackTab => self.access.selected = self.access.selected.saturating_sub(1),
            KeyCode::Enter => {
                if let Some(control) = controls.get(self.access.selected) {
                    self.activate(*control);
                }
            }
            KeyCode::Char(c) if key.modifiers.contains(KeyModifiers::CONTROL) && c == 'c' => {
                self.exit = true;
                self.exit_confirm = false;
            }
            KeyCode::Char(c)
                if controls.get(self.access.selected) == Some(&Control::AccessSearch) =>
            {
                if !c.is_control() && self.access.search.len() < 128 {
                    self.access.search.push(c);
                    self.access.page = 0;
                }
            }
            KeyCode::Backspace
                if controls.get(self.access.selected) == Some(&Control::AccessSearch) =>
            {
                self.access.search.pop();
                self.access.page = 0;
            }
            KeyCode::Left | KeyCode::Right | KeyCode::Up | KeyCode::Down => {
                let areas: Vec<_> = controls
                    .iter()
                    .filter_map(|c| self.hits.iter().find(|h| h.control == *c).map(|h| h.area))
                    .collect();
                if let Some(next) = directional_index(&areas, self.access.selected, key.code) {
                    self.access.selected = next;
                }
            }
            _ => {}
        }
    }
    pub(super) fn temporary_controls(&self) -> Vec<Control> {
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
    pub(super) fn temporary_key(&mut self, key: KeyEvent) {
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.exit = true;
            self.exit_confirm = false;
            return;
        }
        if self.qr_visible {
            if matches!(key.code, KeyCode::Esc | KeyCode::Enter) {
                self.qr_visible = false;
            }
            return;
        }
        if self.temporary_sidebar {
            match key.code {
                KeyCode::Enter | KeyCode::Tab | KeyCode::Right => self.open_temporary(None),
                KeyCode::Up | KeyCode::Down => self.navigate(key.code == KeyCode::Up),
                KeyCode::Esc => {
                    self.exit = true;
                    self.exit_confirm = false;
                }
                _ => {}
            }
            return;
        }
        if self.move_spatial(key.code) {
            return;
        }
        let count = self.temporary_controls().len().max(1);
        match key.code {
            KeyCode::Esc if self.temporary.view == crate::temporary::View::List => {
                self.temporary_sidebar = true
            }
            KeyCode::Esc if self.qr_visible => self.qr_visible = false,
            KeyCode::Esc => {
                self.activate(Control::TemporaryBack);
            }
            KeyCode::Down | KeyCode::Right => {
                self.temporary.selected = (self.temporary.selected + 1).min(count - 1)
            }
            KeyCode::Up | KeyCode::Left => {
                self.temporary.selected = self.temporary.selected.saturating_sub(1)
            }
            KeyCode::Tab => self.temporary.selected = (self.temporary.selected + 1) % count,
            KeyCode::BackTab => {
                self.temporary.selected = (self.temporary.selected + count - 1) % count
            }
            KeyCode::PageDown => self.temporary.scroll = self.temporary.scroll.saturating_add(8),
            KeyCode::PageUp => self.temporary.scroll = self.temporary.scroll.saturating_sub(8),
            KeyCode::Enter => {
                if let Some(control) = self.temporary_controls().get(self.temporary.selected) {
                    self.activate(*control);
                }
            }
            _ => {}
        }
    }
    pub(super) fn temporary_choice(&mut self, index: usize) {
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
    pub(super) fn open_nodes(&mut self) {
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
    pub(super) fn nodes_controls(&self) -> Vec<Control> {
        self.nodes_hits().iter().map(|hit| hit.control).collect()
    }
    pub(super) fn nodes_hits(&self) -> Vec<&Hit> {
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
                        | Control::NodesEditPage(_)
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
    pub(super) fn wizard_key(&mut self, key: KeyEvent) {
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.exit = true;
            self.exit_confirm = false;
            return;
        }
        if self.move_spatial(key.code) {
            return;
        }
        let Some(wizard) = &mut self.node_wizard else {
            self.screen = Screen::Nodes;
            return;
        };
        let choices = wizard.choices().len();
        let created = wizard.step == crate::node_wizard::Step::Created;
        let selection = wizard.selection_step();
        let count = if created {
            2
        } else {
            choices + if selection { 2 } else { 3 }
        };
        match key.code {
            KeyCode::Esc => {
                self.activate(Control::WizardBack);
            }
            KeyCode::Down | KeyCode::Right => {
                wizard.selected = (wizard.selected + 1).min(count - 1);
            }
            KeyCode::Up | KeyCode::Left => wizard.selected = wizard.selected.saturating_sub(1),
            KeyCode::Tab => wizard.selected = (wizard.selected + 1) % count,
            KeyCode::BackTab => wizard.selected = (wizard.selected + count - 1) % count,
            KeyCode::Backspace => {
                if let Some(field) = wizard.field_mut() {
                    field.pop();
                }
            }
            KeyCode::Char(c)
                if !key.modifiers.contains(KeyModifiers::CONTROL) && wizard.field().is_some() =>
            {
                wizard.append(&c.to_string());
            }
            KeyCode::Enter | KeyCode::Char(' ') => {
                let selected = wizard.selected;
                let control = if created {
                    if selected == 0 {
                        Control::WizardOpen
                    } else {
                        Control::WizardCancel
                    }
                } else if selected < choices {
                    Control::WizardChoice(selected)
                } else if selection {
                    [Control::WizardBack, Control::WizardCancel][selected - choices]
                } else {
                    [
                        Control::WizardNext,
                        Control::WizardBack,
                        Control::WizardCancel,
                    ][selected - choices]
                };
                self.activate(control);
            }
            _ => {}
        }
    }
    pub(super) fn nodes_key(&mut self, key: KeyEvent) {
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.exit = true;
            self.exit_confirm = false;
            return;
        }
        if self.nodes_sidebar {
            match key.code {
                KeyCode::Enter | KeyCode::Tab | KeyCode::Right => self.open_nodes(),
                KeyCode::Up | KeyCode::Down => self.navigate(key.code == KeyCode::Up),
                KeyCode::Esc => {
                    self.exit = true;
                    self.exit_confirm = false;
                }
                _ => {}
            }
            return;
        }
        let controls = self.nodes_controls();
        let selected = self.nodes_selected.min(controls.len().saturating_sub(1));
        let search = matches!(controls.get(selected), Some(Control::NodesSearch));
        if let Some(Control::NodesEditField(index)) = controls.get(selected)
            && let Some(editor) = &mut self.nodes.editor
        {
            match key.code {
                KeyCode::Char(c)
                    if !key.modifiers.contains(KeyModifiers::CONTROL) && !c.is_control() =>
                {
                    if editor.fields[*index].value.chars().count() < 255 {
                        editor.fields[*index].value.push(c);
                        editor.changed();
                    }
                    return;
                }
                KeyCode::Backspace => {
                    editor.fields[*index].value.pop();
                    editor.changed();
                    return;
                }
                _ => {}
            }
        }
        if key.code == KeyCode::Esc && self.nodes.editor.is_some() {
            self.activate(Control::NodesEditBack);
            return;
        }
        if key.code == KeyCode::Esc
            && (self.nodes.diagnostics.is_some() || self.nodes.maintenance.is_some())
        {
            self.activate(Control::NodesBack);
            return;
        }
        if self.nodes.diagnostics.is_none()
            && let Some(view) = &mut self.nodes.maintenance
        {
            match key.code {
                KeyCode::PageUp => {
                    view.scroll = view.scroll.saturating_sub(5);
                    return;
                }
                KeyCode::PageDown => {
                    view.scroll = view.scroll.saturating_add(5);
                    return;
                }
                KeyCode::Home => {
                    view.scroll = 0;
                    return;
                }
                KeyCode::End => {
                    view.scroll = usize::MAX;
                    return;
                }
                _ => {}
            }
        }
        if self.nodes.card.is_some()
            && self.nodes.diagnostics.is_none()
            && self.nodes.editor.is_none()
            && self.nodes.maintenance.is_none()
        {
            match key.code {
                KeyCode::PageUp => {
                    self.nodes.card_scroll = self.nodes.card_scroll.saturating_sub(5);
                    return;
                }
                KeyCode::PageDown => {
                    self.nodes.card_scroll = self.nodes.card_scroll.saturating_add(5);
                    return;
                }
                KeyCode::Home => {
                    self.nodes.card_scroll = 0;
                    return;
                }
                KeyCode::End => {
                    self.nodes.card_scroll = usize::MAX;
                    return;
                }
                _ => {}
            }
        }
        match key.code {
            KeyCode::Up | KeyCode::Down | KeyCode::Left | KeyCode::Right => {
                self.nodes_move_direction(key.code);
            }
            KeyCode::Esc if self.nodes.card.is_some() => {
                self.nodes.card = None;
                self.nodes_selected = 0;
            }
            KeyCode::Esc => self.nodes_sidebar = true,
            KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                self.exit = true;
                self.exit_confirm = false;
            }
            KeyCode::Char('f') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                self.nodes_selected = 0
            }
            KeyCode::Tab => self.nodes_selected = (selected + 1) % controls.len().max(1),
            KeyCode::BackTab => {
                self.nodes_selected = (selected + controls.len().max(1) - 1) % controls.len().max(1)
            }
            KeyCode::Enter if !self.nodes.loaded && self.nodes.card.is_none() => self.open_nodes(),
            KeyCode::Enter if search => {
                self.nodes_selected = 1.min(controls.len().saturating_sub(1))
            }
            KeyCode::Enter => {
                if let Some(control) = controls.get(selected) {
                    self.activate(*control);
                }
            }
            KeyCode::Char(c)
                if search && !key.modifiers.contains(KeyModifiers::CONTROL) && !c.is_control() =>
            {
                if self.nodes.search.chars().count() < 128 {
                    self.nodes.search.push(c);
                    self.nodes.page = 0;
                }
            }
            KeyCode::Backspace if search => {
                self.nodes.search.pop();
                self.nodes.page = 0;
            }
            _ => {}
        }
    }
    pub(super) fn nodes_move_direction(&mut self, direction: KeyCode) {
        let areas: Vec<_> = self.nodes_hits().iter().map(|hit| hit.area).collect();
        let selected = self.nodes_selected.min(areas.len().saturating_sub(1));
        if let Some(index) = directional_index(&areas, selected, direction) {
            self.nodes_selected = index;
        }
    }
    pub(super) fn move_spatial(&mut self, direction: KeyCode) -> bool {
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
                let mut controls: Vec<_> = (0..wizard.choices().len())
                    .map(Control::WizardChoice)
                    .collect();
                if wizard.step == crate::node_wizard::Step::Created {
                    controls = vec![Control::WizardOpen, Control::WizardCancel];
                } else if wizard.selection_step() {
                    controls.extend([Control::WizardBack, Control::WizardCancel]);
                } else {
                    controls.extend([
                        Control::WizardNext,
                        Control::WizardBack,
                        Control::WizardCancel,
                    ]);
                }
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
    pub(super) fn section_navigation(&mut self, key: KeyCode) -> bool {
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
    pub(super) fn section_root(&self) -> bool {
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
    pub(super) fn idle_controls(&self) -> Vec<Control> {
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
                        | Control::CycleProfile(_)
                )
            })
            .collect();
        hits.sort_by_key(|hit| (hit.area.y, hit.area.x));
        hits.into_iter().map(|hit| hit.control).collect()
    }
    pub(super) fn idle_key(&mut self, key: KeyEvent) {
        let controls = self.idle_controls();
        if controls.is_empty() {
            return;
        }
        match key.code {
            KeyCode::Down | KeyCode::Tab => {
                self.idle_selection = (self.idle_selection + 1) % controls.len()
            }
            KeyCode::Up | KeyCode::BackTab => {
                self.idle_selection = (self.idle_selection + controls.len() - 1) % controls.len()
            }
            KeyCode::Enter | KeyCode::Right => {
                self.activate(controls[self.idle_selection.min(controls.len() - 1)]);
            }
            KeyCode::Esc => {
                self.exit = true;
                self.exit_confirm = false;
            }
            KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                self.exit = true;
                self.exit_confirm = false;
            }
            _ => {}
        }
    }
    pub(super) fn actions_enabled(&self) -> bool {
        self.form.saved.is_some()
    }
    pub(super) fn profile_index(&self) -> usize {
        self.form
            .saved
            .as_ref()
            .and_then(|profile| {
                self.connections
                    .installations
                    .iter()
                    .position(|p| p.id == profile.id)
            })
            .unwrap_or(self.connections.installations.len())
    }
    pub(super) fn cycle_profile(&mut self, backwards: bool) -> Result<()> {
        let total = self.connections.installations.len() + 1;
        let next = (self.profile_index() + if backwards { total - 1 } else { 1 }) % total;
        let mut preferences = self.connections.clone();
        preferences.selected = preferences.installations.get(next).map(|p| p.id);
        preferences.save(&self.state_dir)?;
        if let Some(profile) = preferences.installations.get(next) {
            self.form.use_installation(profile);
            self.settings_profile = next;
        } else {
            self.form.fields[6].zeroize();
            self.form.saved = None;
            for index in [0, 3, 4, 5, 7, 8] {
                self.form.fields[index].clear();
            }
            self.form.fields[1] = "22".into();
            self.form.fields[2] = "root".into();
            self.form.fields[9] = "22".into();
            self.form.fields[10] = "root".into();
            self.form.selected = 0;
        }
        self.connections = preferences;
        self.quick_focus = true;
        self.screen = Screen::Idle;
        self.hovered = None;
        self.pointer_active = false;
        self.editor = None;
        self.error.clear();
        Ok(())
    }
    pub(super) fn activate_profile(&mut self) {
        if let Some(profile) = &self.form.saved {
            if let Some(index) = self
                .connections
                .installations
                .iter()
                .position(|saved| saved.id == profile.id)
            {
                self.settings_profile = index;
                self.begin_edit(false);
            }
        } else {
            self.begin_edit(true);
        }
    }
    pub(super) fn installation_editor_key(&mut self, key: KeyEvent) {
        if key.code == KeyCode::Esc {
            self.activate(Control::CancelEdit);
            return;
        }
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.exit = true;
            self.exit_confirm = false;
            return;
        }
        let Some((_, fields, selected)) = &mut self.editor else {
            return;
        };
        match key.code {
            KeyCode::Down | KeyCode::Tab => *selected = (*selected + 1).min(8),
            KeyCode::Up | KeyCode::BackTab => *selected = selected.saturating_sub(1),
            KeyCode::Left | KeyCode::Right if *selected == 4 => {
                cycle_channel(&mut fields[4], key.code == KeyCode::Left)
            }
            KeyCode::Right if *selected == 7 => *selected = 8,
            KeyCode::Left if *selected == 8 => *selected = 7,
            KeyCode::Char(c)
                if *selected < 7
                    && *selected != 4
                    && !key.modifiers.contains(KeyModifiers::CONTROL) =>
            {
                if fields[*selected].len() < 4096 {
                    fields[*selected].push(c);
                }
            }
            KeyCode::Backspace if *selected < 7 && *selected != 4 => {
                fields[*selected].pop();
            }
            KeyCode::Enter if *selected < 7 => *selected += 1,
            KeyCode::Enter if *selected == 7 => {
                if let Err(error) = self.save_edit() {
                    self.error = error.to_string();
                }
            }
            KeyCode::Enter => {
                self.activate(Control::CancelEdit);
            }
            _ => {}
        }
    }
    pub(super) fn begin_edit(&mut self, new: bool) {
        let was_saved = self.form.saved.is_some();
        if new && was_saved {
            let mut preferences = self.connections.clone();
            preferences.selected = None;
            if let Err(error) = preferences.save(&self.state_dir) {
                self.error = error.to_string();
                return;
            }
            self.connections = preferences;
        }
        let profile = if new {
            self.form.installation()
        } else {
            let Some(profile) = self.connections.installations.get(self.settings_profile) else {
                return;
            };
            profile.clone()
        };
        let (id, mut fields) = if new && self.form.saved.is_some() {
            (
                None,
                vec![
                    String::new(),
                    String::new(),
                    "22".into(),
                    "root".into(),
                    String::new(),
                    String::new(),
                    String::new(),
                ],
            )
        } else {
            (
                if new { None } else { Some(profile.id) },
                vec![
                    profile.name,
                    profile.host,
                    profile.port.to_string(),
                    profile.user,
                    profile.branch,
                    profile.admin_ids,
                    profile.account,
                ],
            )
        };
        if new {
            fields[4] = "dev".into();
        }
        self.editor = Some((id, fields, 0));
        if new {
            self.form.saved = None;
            self.form.fields[6].zeroize();
            if was_saved {
                for index in [0, 3, 4, 5, 7, 8] {
                    self.form.fields[index].clear();
                }
                self.form.fields[1] = "22".into();
                self.form.fields[2] = "root".into();
                self.form.fields[9] = "22".into();
                self.form.fields[10] = "root".into();
            }
        }
        self.quick_focus = false;
        self.screen = Screen::EditInstallation;
        self.error.clear();
    }
    pub(super) fn save_edit(&mut self) -> Result<()> {
        let (id, fields, _) = self.editor.as_ref().unwrap();
        let profile = Installation {
            installed: id
                .and_then(|id| self.connections.installations.iter().find(|p| p.id == id))
                .is_some_and(|p| {
                    p.installed
                        && p.matches(
                            fields[1].trim(),
                            fields[2].parse().unwrap_or(0),
                            fields[3].trim(),
                        )
                }),
            id: id.unwrap_or_else(uuid::Uuid::new_v4),
            name: fields[0].trim().into(),
            host: fields[1].trim().into(),
            port: fields[2].parse()?,
            user: fields[3].trim().into(),
            branch: fields[4].trim().into(),
            admin_ids: fields[5].trim().into(),
            account: fields[6].trim().into(),
        };
        profile.validate()?;
        let mut next = self.connections.clone();
        if let Some(existing) = next.installations.iter_mut().find(|p| p.id == profile.id) {
            *existing = profile.clone();
        } else {
            next.installations.push(profile.clone());
        }
        next.selected = Some(profile.id);
        next.save(&self.state_dir)?;
        self.connections = next;
        self.form.use_installation(&profile);
        self.quick_focus = false;
        self.editor = None;
        self.screen = Screen::SettingsPage(SettingsPage::Profiles);
        self.settings_selected = self
            .connections
            .installations
            .iter()
            .position(|p| p.id == profile.id)
            .unwrap()
            + 1;
        self.settings_profile = self.settings_selected - 1;
        self.error.clear();
        Ok(())
    }
    pub(super) fn use_selected(&mut self) -> Result<()> {
        self.fresh_installation = false;
        let Some(profile) = self
            .connections
            .installations
            .get(self.settings_profile)
            .cloned()
        else {
            return Ok(());
        };
        let mut next = self.connections.clone();
        next.selected = Some(profile.id);
        next.save(&self.state_dir)?;
        self.connections = next;
        self.form.use_installation(&profile);
        self.quick_focus = false;
        self.screen = Screen::Form;
        self.error.clear();
        Ok(())
    }
    pub(super) fn remember_form(&mut self) -> Result<()> {
        let mut profile = self.form.installation();
        if self.form.saved.is_none()
            && let Some(existing) = self
                .connections
                .installations
                .iter()
                .find(|p| p.matches(&profile.host, profile.port, &profile.user))
        {
            profile.id = existing.id;
            profile.name = existing.name.clone();
        }
        profile.validate()?;
        let mut next = self.connections.clone();
        if let Some(existing) = next.installations.iter_mut().find(|p| p.id == profile.id) {
            *existing = profile.clone();
        } else {
            next.installations.push(profile.clone());
        }
        next.selected = Some(profile.id);
        next.save(&self.state_dir)?;
        self.connections = next;
        self.form.saved = Some(profile);
        Ok(())
    }
    pub(super) fn event(&mut self, event: Event) {
        match event {
            Event::Nodes(update) => {
                if self.ignore_nodes_updates {
                    return;
                }
                if !matches!(&update, crate::nodes::Update::Controller(..)) {
                    self.remember_installed();
                }
                match update {
                    crate::nodes::Update::Controller(profile, command, value) => {
                        if matches!(command, crate::controller::Command::Probe) {
                            if profile == self.form.saved.as_ref().map(|p| p.id) {
                                if value["installed"] == true {
                                    self.remember_installed();
                                    if matches!(self.screen, Screen::Controller)
                                        && !self.controller.verifying
                                    {
                                        self.nodes_requested =
                                            Some(crate::nodes::Command::Controller(
                                                profile,
                                                if self.form.action == Action::Update {
                                                    crate::controller::Command::Updates
                                                } else {
                                                    crate::controller::Command::Overview
                                                },
                                            ));
                                    }
                                    if self.controller.removed {
                                        self.controller = crate::controller::Browser {
                                            profile,
                                            ..Default::default()
                                        };
                                    }
                                } else {
                                    self.confirmed_installation = None;
                                }
                            }
                            return;
                        }
                        if profile != self.form.saved.as_ref().map(|p| p.id)
                            || profile != self.controller.profile
                        {
                            return;
                        }
                        self.remember_installed();
                        let shutdown = matches!(&command, crate::controller::Command::Shutdown(..));
                        self.controller.apply(command, value);
                        if shutdown
                            && let Some(saved) = &self.form.saved
                            && let Err(error) =
                                self.controller.save_verification(&self.state_dir, saved)
                        {
                            self.error = format!("Could not save removal verification: {error}");
                        }
                        if self.controller.removed {
                            self.installed_profile = None;
                            self.confirmed_installation = None;
                            if let Some(id) = profile
                                && let Err(error) = crate::controller::Browser::forget_verification(
                                    &self.state_dir,
                                    id,
                                )
                            {
                                self.error = error.to_string();
                            }
                        }
                    }
                    crate::nodes::Update::EditContext(source, defaults) => {
                        match crate::node_editor::Editor::new(source, &defaults) {
                            Ok(editor) => {
                                self.nodes.editor = Some(editor);
                                self.nodes_selected = 0;
                            }
                            Err(error) => self.error = error.to_string(),
                        }
                    }
                    crate::nodes::Update::EditReview(draft, preview) => {
                        if let Some(editor) = &mut self.nodes.editor
                            && editor.key == draft.key
                        {
                            editor.review = Some((draft, preview));
                            self.nodes_selected = 0;
                        }
                    }
                    crate::nodes::Update::Edited(mut item) => {
                        if let Some(old) =
                            self.nodes.card.as_ref().filter(|old| old.key == item.key)
                        {
                            item.agent = old.agent;
                            item.services = old.services.clone();
                        }
                        if let Some(existing) =
                            self.nodes.items.iter_mut().find(|old| old.key == item.key)
                        {
                            *existing = item.clone();
                        }
                        self.nodes.card = Some(item);
                        self.nodes.editor = None;
                        self.nodes_selected = 0;
                    }
                    crate::nodes::Update::Access(command, value) => {
                        self.access.apply(command, value)
                    }
                    crate::nodes::Update::Temporary(command, value) => {
                        use crate::temporary::{Command, View};
                        self.temporary.selected = 0;
                        match command {
                            Command::List { .. } => {
                                self.temporary.items =
                                    value["items"].as_array().cloned().unwrap_or_default();
                                self.temporary.total =
                                    value["total"].as_u64().unwrap_or(0) as usize;
                                self.temporary.loaded_size =
                                    value["page_size"].as_u64().unwrap_or(20) as usize;
                            }
                            Command::Servers => {
                                self.temporary.nodes = value["items"]
                                    .as_array()
                                    .into_iter()
                                    .flatten()
                                    .filter_map(|v| {
                                        serde_json::from_value::<crate::nodes::Node>(v.clone()).ok()
                                    })
                                    .filter(|n| {
                                        n.enabled
                                            && n.applied_revision > 0
                                            && n.applied_revision == n.desired_revision
                                            && !n.protocols.is_empty()
                                    })
                                    .collect();
                            }
                            Command::Card(_) | Command::Revoke(_) | Command::Create { .. } => {
                                self.temporary.card = Some(value);
                                self.temporary.artifact = None;
                                self.temporary.view = View::Card;
                                self.temporary.id = None;
                            }
                            Command::Artifact(_) => {
                                self.qr_visible = false;
                                *self.qr.borrow_mut() = None;
                                self.temporary.artifact = Some(value);
                                self.temporary.scroll = 0;
                                self.temporary.view = View::Artifact;
                            }
                        }
                    }
                    crate::nodes::Update::CreationOptions(options) => {
                        match crate::node_wizard::Wizard::new(options) {
                            Ok(wizard) => self.node_wizard = Some(wizard),
                            Err(error) => self.error = error.to_string(),
                        }
                    }
                    crate::nodes::Update::Created(item) => {
                        if let Some(existing) =
                            self.nodes.items.iter_mut().find(|n| n.key == item.key)
                        {
                            *existing = item.clone();
                        } else {
                            self.nodes.items.push(item.clone());
                        }
                        if let Some(wizard) = &mut self.node_wizard {
                            wizard.created_key = Some(item.key.clone());
                            wizard.title = item.title.clone();
                            wizard.key = item.key.clone();
                            wizard.step = crate::node_wizard::Step::Created;
                            wizard.selected = 0;
                        }
                        self.nodes.card = Some(item);
                    }
                    crate::nodes::Update::List(items) => {
                        self.nodes.items = items;
                        self.nodes.loaded = true;
                        self.nodes.card = None;
                    }
                    crate::nodes::Update::Observation(item) => {
                        if self
                            .nodes
                            .card
                            .as_ref()
                            .is_some_and(|current| current.key == item.key)
                        {
                            if let Some(existing) = self
                                .nodes
                                .items
                                .iter_mut()
                                .find(|node| node.key == item.key)
                            {
                                *existing = item.clone();
                            }
                            self.nodes.card = Some(item);
                        }
                    }
                    crate::nodes::Update::Card(item) => {
                        if self
                            .nodes
                            .card
                            .as_ref()
                            .is_none_or(|old| old.key != item.key)
                        {
                            self.nodes.card_scroll = 0;
                        }
                        if let Some(existing) =
                            self.nodes.items.iter_mut().find(|n| n.key == item.key)
                        {
                            *existing = item.clone();
                        }
                        self.nodes.card = Some(item);
                    }
                    crate::nodes::Update::Diagnostics(key, facts, inventory) => {
                        if self.nodes.card.as_ref().is_some_and(|node| node.key == key) {
                            self.nodes.diagnostics = Some((facts, inventory));
                            self.nodes.recovery_confirm = None;
                            self.nodes.diagnostic_item = None;
                            self.nodes.diagnostic_page = 0;
                            self.nodes_selected = 0;
                        }
                    }
                    crate::nodes::Update::Maintenance(mut view, observing) => {
                        if self
                            .nodes
                            .card
                            .as_ref()
                            .is_some_and(|node| node.key == view.key)
                            && (!observing
                                || self.nodes.maintenance.as_ref().is_some_and(|current| {
                                    current.key == view.key && current.confirmation.is_none()
                                }))
                        {
                            if view.removed() {
                                self.nodes.items.retain(|node| node.key != view.key);
                                self.nodes.operation = None;
                            }
                            if !observing || view.removed() {
                                self.nodes_selected = usize::from(view.confirmation.is_some());
                            } else if let Some(previous) = &self.nodes.maintenance {
                                view.scroll = previous.scroll;
                            }
                            self.nodes.maintenance = Some(view);
                        }
                    }
                    crate::nodes::Update::Operation(operation) => {
                        if operation.kind == "agent-rollouts" && operation.status == "succeeded" {
                            if let Some(node) = self
                                .nodes
                                .card
                                .as_mut()
                                .filter(|node| node.key == operation.node_key)
                            {
                                node.agent = crate::nodes::AgentState::Ready;
                            }
                            if let Some(node) = self
                                .nodes
                                .items
                                .iter_mut()
                                .find(|node| node.key == operation.node_key)
                            {
                                node.agent = crate::nodes::AgentState::Ready;
                            }
                        }
                        if !self.nodes_background
                            || self
                                .nodes
                                .card
                                .as_ref()
                                .is_some_and(|node| node.key == operation.node_key)
                        {
                            self.nodes.operation = Some(operation)
                        }
                    }
                }
            }
            Event::Stage(label) => self.stage = label,
            Event::Update(snapshot) => {
                self.update = Some(snapshot);
            }
            Event::Progress(p) => {
                if let Err(error) = self.tracker.accept(&p) {
                    self.error = error.to_string();
                }
            }
            Event::Prompt(prompt, reply) => {
                self.administrator_selected = 0;
                self.prompt = Some((prompt, reply));
                self.password.zeroize();
                self.trust = false;
            }
            Event::Finished(result) => {
                self.ignore_nodes_updates = false;
                if !self.nodes_busy && self.form.action == Action::Install && result.is_ok() {
                    self.remember_installed();
                }
                if self.nodes_busy {
                    self.nodes_busy = false;
                    if result.is_err() {
                        self.controller.plan_pending = false;
                    }
                    if self.nodes_background {
                        self.nodes_background = false;
                        self.access_busy = false;
                        if let Err(error) = result {
                            self.error = error;
                        }
                        return;
                    }
                    let failed = result.is_err();
                    self.screen = if self.access_busy {
                        self.access_busy = false;
                        Screen::Access
                    } else if self.temporary_busy {
                        self.temporary_busy = false;
                        Screen::Temporary
                    } else if self.wizard_loading && self.node_wizard.is_some() {
                        Screen::NodeWizard
                    } else {
                        Screen::Nodes
                    };
                    self.wizard_loading = false;
                    let error = result.err().unwrap_or_default();
                    if !error.is_empty() {
                        self.error = error;
                    }
                    if failed && let Some(wizard) = &mut self.node_wizard {
                        if self.error.contains("local_node_exists") {
                            wizard.options.local_available = false;
                            wizard.step = crate::node_wizard::Step::Transport;
                            wizard.selected = 0;
                        }
                        // A definitive rejection is safe to edit, while uncertain
                        // submissions remain fenced by the durable creation record.
                        wizard.command_id = uuid::Uuid::new_v4();
                    }
                    return;
                }
                self.result_scroll = 0;
                self.outcome = Some(result);
                self.screen = Screen::Finished;
            }
        }
    }
    pub(super) fn prompt_key(&mut self, key: KeyEvent) {
        let Some((prompt, _)) = &self.prompt else {
            return;
        };
        let answer = match prompt {
            Prompt::SelectAdministrator { choices } => match key.code {
                KeyCode::Up => {
                    self.administrator_selected = self.administrator_selected.saturating_sub(1);
                    None
                }
                KeyCode::Down | KeyCode::Tab => {
                    self.administrator_selected =
                        (self.administrator_selected + 1).min(choices.len().saturating_sub(1));
                    None
                }
                KeyCode::Enter if !choices.is_empty() => {
                    Some(Answer::Selection(self.administrator_selected))
                }
                KeyCode::Esc => Some(Answer::Cancel),
                _ => None,
            },
            Prompt::HostKey { .. } | Prompt::ConfirmAction { .. } => match key.code {
                KeyCode::Char('y' | 'Y') => Some(Answer::Confirm(true)),
                KeyCode::Char('n' | 'N') | KeyCode::Esc => Some(Answer::Confirm(false)),
                KeyCode::Left | KeyCode::Right | KeyCode::Tab => {
                    self.trust = !self.trust;
                    None
                }
                KeyCode::Enter => Some(Answer::Confirm(self.trust)),
                _ => None,
            },
            Prompt::Password { .. } => match key.code {
                KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                    if self.password.len() < 4096 {
                        self.password.push(c);
                    }
                    None
                }
                KeyCode::Backspace => {
                    self.password.pop();
                    None
                }
                KeyCode::Enter if self.password.is_empty() => {
                    self.error = "Enter the SSH password before connecting.".into();
                    None
                }
                KeyCode::Enter => Some(Answer::Password(std::mem::replace(
                    &mut self.password,
                    Zeroizing::new(String::new()),
                ))),
                KeyCode::Esc => Some(Answer::Cancel),
                _ => None,
            },
        };
        if let Some(answer) = answer {
            self.error.clear();
            let (_, reply) = self.prompt.take().unwrap();
            let _ = reply.send(answer);
        }
    }
    pub(super) fn scroll_result(&mut self, backwards: bool, amount: usize) {
        self.result_scroll = if backwards {
            self.result_scroll.saturating_sub(amount)
        } else {
            self.result_scroll
                .saturating_add(amount)
                .min(self.result_max_scroll.get())
        };
    }
    pub(super) fn close_result(&mut self, key: KeyEvent) -> bool {
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.exit = true;
            self.exit_confirm = false;
            return false;
        }
        if !matches!(key.code, KeyCode::Enter | KeyCode::Esc) {
            return false;
        }
        self.fresh_installation = false;
        self.screen = if self.form.action == Action::Install && self.has_installation() {
            Screen::Controller
        } else {
            Screen::Form
        };
        self.form.selected = 0;
        self.quick_focus = !self.actions_enabled();
        self.outcome = None;
        self.update = None;
        self.stage.clear();
        self.error.clear();
        self.tracker = Tracker::default();
        self.node_page = 0;
        self.result_scroll = 0;
        self.result_max_scroll.set(0);
        self.result_page_size.set(1);
        self.prompt = None;
        self.password.zeroize();
        self.form.fields[6].zeroize();
        self.hits.clear();
        self.trust = false;
        self.exit = false;
        self.exit_confirm = false;
        self.stop.store(false, Ordering::Relaxed);
        true
    }

    pub(super) fn next_request(&self) -> Request {
        Request {
            action: self.form.action,
            host: String::new(),
            port: 22,
            user: String::new(),
            state_dir: self.state_dir.clone(),
            tag: String::new(),
            branch: String::new(),
            admin_ids: String::new(),
            bot_token: Zeroizing::new(String::new()),
            workflow: WorkflowOptions {
                stop: self.stop.clone(),
                ..WorkflowOptions::default()
            },
        }
    }
    pub(super) fn viewport(&self, automatic: usize, length: usize, capacity: usize) -> usize {
        let maximum = length.saturating_sub(capacity.max(1));
        let start = self.manual_viewport.get().unwrap_or(automatic).min(maximum);
        self.list_viewport.set(start);
        self.list_max_viewport.set(maximum);
        self.register_scroll(start, maximum, capacity, ScrollTarget::List);
        if self.manual_viewport.get().is_some() {
            self.manual_viewport.set(Some(start));
        }
        self.content_overflow.set(maximum > 0);
        start
    }

    pub(super) fn register_scroll(
        &self,
        position: usize,
        maximum: usize,
        visible: usize,
        target: ScrollTarget,
    ) {
        if maximum > 0 && self.scroll_track.get().height > 0 {
            self.scrollbar.set(Some(Scrollbar::new(
                self.scroll_track.get(),
                position,
                maximum,
                visible,
                target,
            )));
        }
    }
    pub(super) fn set_scroll_position(&mut self, bar: Scrollbar, position: usize) {
        let next = position.min(bar.maximum);
        self.action_focus = None;
        match bar.target {
            ScrollTarget::Result => self.result_scroll = next,
            ScrollTarget::NodeCard => self.nodes.card_scroll = next,
            ScrollTarget::Maintenance => {
                if let Some(view) = &mut self.nodes.maintenance {
                    view.scroll = next;
                }
            }
            ScrollTarget::Temporary => self.temporary.scroll = next.min(u16::MAX as usize) as u16,
            ScrollTarget::List => {
                self.manual_viewport.set(Some(next));
                match self.screen {
                    Screen::Nodes if self.nodes.card.is_none() && self.nodes.editor.is_none() => {
                        let pages = self.nodes.pages(self.nodes_capacity.get());
                        let mut start = 0;
                        self.nodes.page = pages
                            .iter()
                            .position(|page| {
                                start += page.len();
                                next < start
                            })
                            .unwrap_or(pages.len().saturating_sub(1));
                    }
                    Screen::Nodes if self.nodes.editor.is_some() => {
                        self.nodes.editor.as_mut().unwrap().page =
                            next / self.nodes_capacity.get().max(1)
                    }
                    Screen::Access => {
                        let page = next / self.access_capacity.get().max(1);
                        if let Some(devices) = &mut self.access.devices {
                            devices.page = page;
                        } else if let Some(editor) = &mut self.access.editor {
                            if editor.grants_view {
                                editor.grants_page = page;
                            } else if editor.expiry_view {
                                editor.expiry_page = page;
                            }
                        } else {
                            self.access.page = page;
                        }
                    }
                    Screen::Temporary if self.temporary.view == crate::temporary::View::Server => {
                        self.temporary.page = next / self.temporary.capacity.get().max(1)
                    }
                    _ => {}
                }
            }
        }
    }

    pub(super) fn refresh_scroll_hover(&mut self) -> bool {
        if self.manual_viewport.get().is_none() || !self.pointer_active {
            return false;
        }
        let control = self.pointer_position.and_then(|position| {
            self.hits
                .iter()
                .rev()
                .find(|hit| hit.area.contains(position))
                .map(|hit| hit.control)
        });
        let changed = self.hovered != control;
        self.hovered = control;
        if let Some(control) = control {
            self.focus(control);
        }
        changed
    }

    pub(super) fn header_status(&self) -> String {
        use crate::nodes::{Command, ConnectionState};
        let loading_installation = self.probe_requested
            || matches!(
                self.nodes_requested,
                Some(Command::Controller(_, crate::controller::Command::Probe))
            )
            || self.nodes_busy && self.loading_message == "Loading installation…";
        let status = if self.ssh_state == ConnectionState::Connecting {
            "SSH connecting"
        } else if loading_installation {
            "Loading installation…"
        } else if self.nodes_busy && self.stage.starts_with("Authorizing") {
            "Authorizing Workstation…"
        } else if self.nodes_busy && !self.nodes_background && !self.loading_message.is_empty() {
            &self.loading_message
        } else {
            self.ssh_state.label()
        };
        format!("Status: {status}")
    }

    pub(super) fn cancel_exit(&mut self) {
        self.exit = false;
        self.hovered = None;
        self.pointer_active = self.pointer_position.is_some();
    }

    pub(super) fn mouse(&mut self, event: MouseEvent) -> Option<KeyEvent> {
        // Releasing a mouse button must not restore an unrelated keyboard focus.
        self.pointer_active = true;
        self.pointer_position = Some((event.column, event.row).into());
        let modal = self.prompt.is_some() || self.exit || self.discard.is_some() || self.qr_visible;
        if !modal && let Some(bar) = self.scrollbar.get() {
            match event.kind {
                MouseEventKind::Down(MouseButton::Left)
                    if bar.track.contains((event.column, event.row).into()) =>
                {
                    let offset = event.row.saturating_sub(bar.track.y);
                    let on_thumb =
                        offset >= bar.thumb_start && offset < bar.thumb_start + bar.thumb_length;
                    let grab = if on_thumb {
                        offset - bar.thumb_start
                    } else {
                        bar.thumb_length / 2
                    };
                    self.scroll_drag = Some(grab);
                    self.hovered = None;
                    if !on_thumb {
                        self.set_scroll_position(bar, bar.position_at(event.row, grab));
                    }
                    return None;
                }
                MouseEventKind::Drag(MouseButton::Left) if self.scroll_drag.is_some() => {
                    self.set_scroll_position(
                        bar,
                        bar.position_at(event.row, self.scroll_drag.unwrap()),
                    );
                    return None;
                }
                MouseEventKind::Up(MouseButton::Left) if self.scroll_drag.take().is_some() => {
                    return None;
                }
                MouseEventKind::Moved if bar.track.contains((event.column, event.row).into()) => {
                    self.hovered = None;
                    return None;
                }
                MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
                    self.pointer_active = true;
                    self.pointer_position = Some((event.column, event.row).into());
                    let step = if matches!(bar.target, ScrollTarget::List) {
                        1
                    } else {
                        3
                    };
                    let position = if event.kind == MouseEventKind::ScrollUp {
                        bar.position.saturating_sub(step)
                    } else {
                        bar.position.saturating_add(step)
                    };
                    self.set_scroll_position(bar, position);
                    // Update geometry immediately so a burst of input can accumulate.
                    self.scrollbar.set(Some(Scrollbar::new(
                        bar.track,
                        position,
                        bar.maximum,
                        bar.visible,
                        bar.target,
                    )));
                    return None;
                }
                _ => {}
            }
        }
        if matches!(
            event.kind,
            MouseEventKind::Up(MouseButton::Left) | MouseEventKind::Down(MouseButton::Left)
        ) {
            self.scroll_drag = None;
        }
        if matches!(
            event.kind,
            MouseEventKind::ScrollUp | MouseEventKind::ScrollDown
        ) {
            return None;
        }
        if event.kind == MouseEventKind::Moved {
            self.pointer_position = Some((event.column, event.row).into());
            let control = self
                .hits
                .iter()
                .rev()
                .find(|hit| hit.area.contains((event.column, event.row).into()))
                .map(|hit| hit.control);
            if let Some(control) = control {
                self.focus(control);
            }
            self.hovered = control;
            return None;
        }
        self.hovered = None;
        if event.kind != MouseEventKind::Down(MouseButton::Left) {
            return None;
        }
        let control = self
            .hits
            .iter()
            .rev()
            .find(|hit| hit.area.contains((event.column, event.row).into()))?
            .control;
        self.activate(control)
    }
    pub(super) fn restore_action_focus(&mut self) -> bool {
        let Some(control) = self.action_focus else {
            return false;
        };
        let control = if self.hits.iter().any(|hit| hit.control == control) {
            control
        } else {
            match control {
                Control::NodesEditPage(next) => Control::NodesEditPage(!next),
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
    pub(super) fn list_position(
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
    pub(super) fn focus(&mut self, control: Control) {
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
            Control::AccessSearch | Control::AccessField(_) => {
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
                    let mut controls: Vec<_> = (0..wizard.choices().len())
                        .map(Control::WizardChoice)
                        .collect();
                    if wizard.step == crate::node_wizard::Step::Created {
                        controls = vec![Control::WizardOpen, Control::WizardCancel];
                    } else {
                        if !wizard.selection_step() {
                            controls.push(Control::WizardNext);
                        }
                        controls.extend([Control::WizardBack, Control::WizardCancel]);
                    }
                    if let Some(index) = controls.iter().position(|c| *c == control) {
                        wizard.selected = index;
                    }
                }
            }
        }
    }

    pub(super) fn quick_start_key(&mut self, key: KeyEvent) {
        match key.code {
            KeyCode::Esc => {
                self.screen = Screen::Form;
            }
            KeyCode::Tab | KeyCode::BackTab => self.confirm = !self.confirm,
            KeyCode::Down | KeyCode::Right => self.confirm = true,
            KeyCode::Up | KeyCode::Left => self.confirm = false,
            KeyCode::Char(' ') => {
                self.activate(Control::QuickStartToggle);
            }
            KeyCode::Enter => {
                self.activate(if self.confirm {
                    Control::QuickStartClose
                } else {
                    Control::QuickStartAction
                });
            }
            KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                self.exit = true;
                self.exit_confirm = false;
            }
            _ => {}
        }
    }

    pub(super) fn unsaved_changes(&self) -> bool {
        (matches!(self.screen, Screen::Controller)
            && self.controller.defaults.is_some()
            && self.controller.defaults != self.controller.defaults_baseline)
            || (matches!(self.screen, Screen::Access)
                && (self.access.editor.as_ref().is_some_and(|e| e.dirty())
                    || self
                        .access
                        .devices
                        .as_ref()
                        .and_then(|d| d.editor.as_ref())
                        .is_some_and(|e| {
                            e.name
                                != e.source
                                    .as_ref()
                                    .and_then(|v| v["display_name"].as_str())
                                    .unwrap_or("")
                        })))
            || (matches!(self.screen, Screen::Nodes)
                && self.nodes.editor.as_ref().is_some_and(|e| e.dirty()))
    }
    pub(super) fn activate(&mut self, control: Control) -> Option<KeyEvent> {
        if control == Control::WindowClose {
            self.exit = true;
            self.exit_confirm = false;
            self.hovered = None;
            return None;
        }
        self.manual_viewport.set(None);
        if matches!(self.screen, Screen::Access)
            && !matches!(
                control,
                Control::AccessSearch | Control::AccessField(_) | Control::AccessDeviceName
            )
        {
            self.access.notice.clear();
        }
        if matches!(
            control,
            Control::NodesEditChoice(_)
                | Control::NodesEditToggle(_, _)
                | Control::NodesEditPage(_)
                | Control::AccessGrant(_, _)
                | Control::AccessRule(_, _)
                | Control::AccessGrantAll(_)
                | Control::AccessGrantPage(_)
                | Control::AccessPage(_)
                | Control::AccessDevicePage(_)
                | Control::AccessExpiryPage(_)
                | Control::ControllerPolicyToggle(_)
                | Control::ControllerDefaultsToggle(_)
                | Control::ControllerDefaultsPreset
                | Control::ControllerDefaultsMode
                | Control::ControllerDefaultsAdvanced
                | Control::ControllerUpdateAuto
                | Control::ControllerUpdateInterval
                | Control::ControllerUpdateCheck
                | Control::ControllerBackupAuto
                | Control::ControllerBackupInterval
                | Control::ControllerBackupKeep
                | Control::ControllerAlertToggle(_)
                | Control::ControllerComponentPage(_)
                | Control::ControllerVersionsPage(_)
                | Control::ControllerHistoryPage(_)
                | Control::ControllerAlertPage(_)
                | Control::SaveAccent
                | Control::Accent(_)
                | Control::TemporaryRefresh
                | Control::TemporaryPage(_)
                | Control::NodesRefresh
                | Control::ControllerBackups
                | Control::ControllerComponents
                | Control::AccessRefresh
        ) {
            self.action_focus = Some(control);
        }
        if self.discard.is_some() && !matches!(control, Control::Discard(_)) {
            return None;
        }
        let global_leave = matches!(
            control,
            Control::Action(_)
                | Control::Settings
                | Control::QuickProfile
                | Control::CycleProfile(_)
                | Control::Temporary
                | Control::Access
                | Control::Controller
        );
        let leaving = control == Control::ControllerBack
            && self.controller.defaults_input.is_none()
            || global_leave
            || matches!(control, Control::AccessTab(_) | Control::NodesBack)
            || control == Control::AccessBack
                && (self.access.editor.is_some()
                    || self
                        .access
                        .devices
                        .as_ref()
                        .is_some_and(|d| d.editor.is_some()))
            || control == Control::NodesEditBack
                && self
                    .nodes
                    .editor
                    .as_ref()
                    .is_some_and(|e| e.review.is_none());
        if leaving && self.unsaved_changes() && !self.discard_bypass {
            self.discard = Some(control);
            self.discard_confirm = false;
            return None;
        }
        self.discard_bypass = false;
        if global_leave {
            self.controller.defaults = None;
            self.controller.defaults_input = None;
            self.access.editor = None;
            self.nodes.editor = None;
            self.nodes.diagnostics = None;
            self.nodes.diagnostic_item = None;
            self.nodes.recovery_confirm = None;
            self.nodes.maintenance = None;
        }

        if matches!(self.screen, Screen::Nodes)
            && !matches!(
                control,
                Control::Action(_)
                    | Control::QuickProfile
                    | Control::CycleProfile(_)
                    | Control::Settings
            )
        {
            self.nodes_sidebar = false;
        }
        let key = match control {
            Control::Discard(leave) => {
                let pending = self.discard.take();
                if leave && let Some(pending) = pending {
                    self.discard_bypass = true;
                    return self.activate(pending);
                }
                return None;
            }
            Control::QuickStartToggle => {
                self.connections.hide_quick_start = !self.connections.hide_quick_start;
                if let Err(error) = self.connections.save(&self.state_dir) {
                    self.error = error.to_string();
                }
                return None;
            }
            Control::QuickStartClose => {
                self.screen = Screen::Idle;
                return None;
            }
            Control::QuickStartAction => {
                self.screen = Screen::Form;
                self.begin_edit(true);
                return None;
            }
            Control::AccessGrants => {
                if let Some(editor) = &mut self.access.editor {
                    editor.grants_view = true;
                    editor.grants_page = 0;
                }
                self.access.selected = 0;
                return None;
            }
            Control::AccessGrantBack => {
                if let Some(editor) = &mut self.access.editor {
                    editor.grants_view = false;
                }
                self.access.selected = 0;
                return None;
            }
            Control::AccessGrantMode(future) => {
                if let Some(editor) = &mut self.access.editor {
                    editor.future_view = future;
                    editor.grants_page = 0;
                }
                self.access.selected = 0;
                return None;
            }
            Control::AccessGrantPage(next) => {
                if let Some(editor) = &mut self.access.editor {
                    let count = if editor.future_view {
                        editor.regions.len() + 1
                    } else {
                        editor.nodes.len()
                    };
                    let pages = count.div_ceil(self.access_capacity.get().max(1)).max(1);
                    editor.grants_page = if next {
                        (editor.grants_page + 1).min(pages - 1)
                    } else {
                        editor.grants_page.saturating_sub(1)
                    };
                }
                return None;
            }
            Control::AccessGrant(index, awg) => {
                if let Some(editor) = &mut self.access.editor
                    && let Some(node) = editor
                        .nodes
                        .get(index)
                        .and_then(|n| n["key"].as_str())
                        .map(str::to_owned)
                {
                    editor.toggle_grant(&node, if awg { "awg" } else { "xray" });
                }
                return None;
            }
            Control::AccessRule(region, awg) => {
                if let Some(editor) = &mut self.access.editor {
                    let id = region
                        .and_then(|index| editor.regions.get(index))
                        .and_then(|r| r["id"].as_str())
                        .map(str::to_owned);
                    editor.toggle_rule(id.as_deref(), if awg { "awg" } else { "xray" });
                }
                return None;
            }
            Control::AccessGrantAll(grant) => {
                if let Some(editor) = &mut self.access.editor {
                    let targets: Vec<_> = editor
                        .nodes
                        .iter()
                        .flat_map(|n| {
                            n["protocols"]
                                .as_array()
                                .into_iter()
                                .flatten()
                                .filter_map(|p| {
                                    Some((n["key"].as_str()?.to_owned(), p.as_str()?.to_owned()))
                                })
                        })
                        .collect();
                    for (node, protocol) in targets {
                        if editor.granted(&node, &protocol) != grant {
                            editor.toggle_grant(&node, &protocol);
                        }
                    }
                }
                return None;
            }
            Control::AccessEdit => {
                if let Some(id) = self
                    .access
                    .card
                    .as_ref()
                    .and_then(|v| v["id"].as_str())
                    .map(str::to_owned)
                {
                    self.access_command(crate::access::Command::EditContext {
                        id: (self.access.tab != crate::access::Tab::Accounts).then(|| id.clone()),
                        owner: (self.access.tab == crate::access::Tab::Accounts).then_some(id),
                    });
                }
                return None;
            }
            Control::AccessNew => {
                if self.access.tab == crate::access::Tab::Accounts
                    || self.access.owner_filter.is_some()
                {
                    let owner = self.access.owner_filter.clone().or_else(|| {
                        self.access
                            .card
                            .as_ref()
                            .and_then(|v| v["id"].as_str())
                            .map(str::to_owned)
                    });
                    self.access_command(crate::access::Command::EditContext { id: None, owner });
                } else {
                    self.access.notice = "Select an account, then choose New profile.".into();
                    self.access.tab = crate::access::Tab::Accounts;
                    self.access_command(crate::access::Command::List(self.access.tab));
                }
                return None;
            }
            Control::AccessField(_) => {
                self.focus(control);
                return None;
            }
            Control::AccessSave => {
                if let Some(editor) = &self.access.editor {
                    match editor.save() {
                        Ok(command) => self.access_command(command),
                        Err(error) => self.error = error.to_string(),
                    }
                }
                return None;
            }
            Control::AccessDevices => {
                if let Some(id) = self.access.card.as_ref().and_then(|v| v["id"].as_str()) {
                    self.access.notice.clear();
                    self.access_command(crate::access::Command::Devices(id.into()));
                }
                return None;
            }
            Control::AccessDeviceItem(index) => {
                if let Some(devices) = &mut self.access.devices {
                    devices.card = devices.items.get(index).cloned();
                }
                self.access.selected = 0;
                return None;
            }
            Control::AccessDeviceNew | Control::AccessDeviceEdit => {
                if let Some(devices) = &mut self.access.devices {
                    devices.edit(control == Control::AccessDeviceNew);
                }
                self.error.clear();
                self.access.selected = 0;
                return None;
            }
            Control::AccessDeviceName => {
                self.focus(control);
                return None;
            }
            Control::AccessDeviceSave => {
                if let Some(devices) = &self.access.devices {
                    match devices.save() {
                        Ok(command) => self.access_command(command),
                        Err(error) => self.error = error.to_string(),
                    }
                }
                return None;
            }
            Control::AccessDeviceDelete => {
                if let Some(devices) = &mut self.access.devices {
                    devices.delete_key = Some(uuid::Uuid::new_v4());
                }
                self.access.selected = 0;
                return None;
            }
            Control::AccessDeviceConfirm => {
                if let Some(devices) = &self.access.devices
                    && let (Some(card), Some(key)) = (&devices.card, devices.delete_key)
                    && let Some(revision) = card["revision"].as_u64()
                {
                    self.access_command(crate::access::Command::DeviceMutate {
                        profile: crate::access::text(&devices.profile["id"]),
                        device: card["id"].as_str().map(str::to_owned),
                        revision,
                        name: String::new(),
                        delete: true,
                        key,
                    });
                }
                return None;
            }
            Control::AccessDevicePage(next) => {
                if let Some(devices) = &mut self.access.devices {
                    let last = devices
                        .items
                        .len()
                        .div_ceil(self.access_capacity.get().max(1))
                        .saturating_sub(1);
                    devices.page = if next {
                        (devices.page + 1).min(last)
                    } else {
                        devices.page.saturating_sub(1)
                    };
                }
                self.access.selected = 0;
                return None;
            }
            Control::AccessExpiryMenu => {
                if let Some(editor) = &mut self.access.editor {
                    editor.expiry_view = true;
                    editor.expiry_page = 0;
                }
                self.access.selected = 0;
                return None;
            }
            Control::AccessExpiry(days) => {
                if let Some(editor) = &mut self.access.editor {
                    match days
                        .map(|d| crate::access::expiry_after(d, std::time::SystemTime::now()))
                        .transpose()
                    {
                        Ok(date) => {
                            editor.fields[1] = date.unwrap_or_default();
                            editor.key = uuid::Uuid::new_v4();
                            editor.expiry_view = false;
                            self.access.selected = 0;
                        }
                        Err(error) => self.error = error.to_string(),
                    }
                }
                return None;
            }
            Control::AccessExpiryPage(next) => {
                if let Some(editor) = &mut self.access.editor {
                    let last = 6usize
                        .div_ceil(self.access_capacity.get().max(1))
                        .saturating_sub(1);
                    editor.expiry_page = if next {
                        (editor.expiry_page + 1).min(last)
                    } else {
                        editor.expiry_page.saturating_sub(1)
                    };
                }
                self.access.selected = 0;
                return None;
            }
            Control::AccessExpiryBack => {
                if let Some(editor) = &mut self.access.editor {
                    editor.expiry_view = false;
                }
                self.access.selected = 0;
                return None;
            }
            Control::AccessAccountAction(role) => {
                if let Some(card) = &self.access.card {
                    let card = card.get("_account").unwrap_or(card);
                    let body = if role {
                        serde_json::json!({"role": if card["role"] == "admin" { "member" } else { "admin" }})
                    } else {
                        serde_json::json!({"status": if card["status"] == "approved" { "disabled" } else { "approved" }})
                    };
                    self.access.account_action = Some((body, uuid::Uuid::new_v4()));
                    self.access.selected = 0;
                }
                return None;
            }
            Control::AccessAccountConfirm => {
                if let (Some(card), Some((body, key))) =
                    (&self.access.card, &self.access.account_action)
                    && let card = card.get("_account").unwrap_or(card)
                    && let (Some(id), Some(revision)) =
                        (card["id"].as_str(), card["revision"].as_u64())
                {
                    self.access_command(crate::access::Command::AccountMutate {
                        id: id.into(),
                        revision,
                        body: body.clone(),
                        key: *key,
                    });
                }
                return None;
            }
            Control::AccessProfileAction(delete) => {
                self.access.profile_action = Some(delete);
                self.access.selected = 0;
                return None;
            }
            Control::AccessProfileConfirm => {
                if let (Some(card), Some(delete)) = (&self.access.card, self.access.profile_action)
                {
                    self.access_command(crate::access::Command::Mutate {
                        id: card["id"].as_str().map(str::to_owned),
                        revision: card["desired_revision"].as_u64(),
                        body: serde_json::json!({"frozen": card["frozen"] != true}),
                        delete,
                        key: uuid::Uuid::new_v4(),
                    });
                }
                return None;
            }
            Control::Access => {
                self.open_access();
                return None;
            }
            Control::AccessTab(tab) => {
                self.access.editor = None;
                self.access.devices = None;
                self.access.tab = tab;
                self.access.owner_filter = None;
                self.access.items.clear();
                self.access.page = 0;
                self.access.search.clear();
                self.access.notice.clear();
                self.access_command(crate::access::Command::List(tab));
                return None;
            }
            Control::AccessItem(index) => {
                if let Some(id) = self.access.items.get(index).and_then(|v| v["id"].as_str()) {
                    self.access.notice.clear();
                    let tab = if self.access.items[index]["_account_only"] == true {
                        crate::access::Tab::Accounts
                    } else {
                        self.access.tab
                    };
                    self.access_command(crate::access::Command::Card(tab, id.into()));
                }
                return None;
            }
            Control::AccessSearch => {
                self.focus(control);
                return None;
            }
            Control::AccessRefresh => {
                if let Some(devices) = &self.access.devices {
                    self.access_command(crate::access::Command::Devices(crate::access::text(
                        &devices.profile["id"],
                    )));
                    return None;
                }
                let command = self
                    .access
                    .card
                    .as_ref()
                    .and_then(|v| v["id"].as_str())
                    .map(|id| crate::access::Command::Card(self.access.tab, id.into()))
                    .unwrap_or(crate::access::Command::List(self.access.tab));
                self.access_command(command);
                return None;
            }
            Control::AccessBack => {
                self.error.clear();
                self.access.selected = 0;
                if let Some(devices) = &mut self.access.devices {
                    if devices.editor.take().is_some() || devices.delete_key.take().is_some() {
                        return None;
                    }
                    if devices.card.take().is_some() {
                        return None;
                    }
                    let profile = devices.profile.clone();
                    self.access.card = Some(profile);
                    self.access.devices = None;
                    self.access.notice.clear();
                    return None;
                }
                if self.access.editor.take().is_some()
                    || self.access.profile_action.take().is_some()
                    || self.access.account_action.take().is_some()
                {
                    return None;
                }
                if self.access.decision.take().is_none() {
                    self.access.card = None;
                    if self.access.tab == crate::access::Tab::Accounts {
                        self.access.tab = crate::access::Tab::Profiles;
                    }
                    self.access_command(crate::access::Command::List(self.access.tab));
                }
                return None;
            }
            Control::AccessPage(next) => {
                let total = self
                    .access
                    .filtered()
                    .len()
                    .div_ceil(self.access_capacity.get().max(1))
                    .max(1);
                self.access.page = if next {
                    (self.access.page + 1).min(total - 1)
                } else {
                    self.access.page.saturating_sub(1)
                };
                return None;
            }
            Control::AccessDecision(approve) => {
                self.access.decision = Some(approve);
                self.access.selected = 0;
                return None;
            }
            Control::AccessConfirm => {
                if let (Some(card), Some(approve)) = (&self.access.card, self.access.decision)
                    && let Some(id) = card["id"].as_str()
                {
                    self.access_command(crate::access::Command::Decision {
                        id: id.into(),
                        approve,
                        key: uuid::Uuid::new_v4(),
                    });
                }
                return None;
            }
            Control::Temporary => {
                self.open_temporary(None);
                return None;
            }
            Control::NodeTemporary => {
                self.open_temporary(self.nodes.card.clone());
                return None;
            }
            Control::TemporaryChoice(index) => {
                self.temporary_choice(index);
                return None;
            }
            Control::TemporaryBack => {
                if self.qr_visible {
                    self.qr_visible = false;
                    return None;
                }
                self.temporary.back();
                if self.temporary.view == crate::temporary::View::List {
                    self.temporary_command(self.temporary.list());
                }
                return None;
            }
            Control::TemporaryNew => {
                let command = self.temporary.new_config();
                self.temporary_command(command);
                return None;
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
                return None;
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
                return None;
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
                return None;
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
                return None;
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
                return None;
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
                return None;
            }
            Control::TemporaryRevoke => {
                self.temporary.view = crate::temporary::View::Revoke;
                self.temporary.selected = 0;
                return None;
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
                return None;
            }
            Control::ControllerAudit => {
                self.controller_command(crate::controller::Command::WorkstationAudit(0));
                return None;
            }
            Control::ControllerRecoveryHistory => {
                self.controller_command(crate::controller::Command::RecoveryHistory(0));
                return None;
            }
            Control::ControllerAnnouncement => {
                self.controller_command(crate::controller::Command::AnnouncementOverview);
                return None;
            }
            Control::ControllerAnnouncementCompose => {
                self.controller.announcement_draft = Some(String::new());
                self.controller.announcement_key = Some(uuid::Uuid::new_v4());
                self.controller.announcement_preview = None;
                self.controller.announcement_job = None;
                self.controller.selected = 0;
                return None;
            }
            Control::ControllerAnnouncementPreview => {
                if let Some(text) = self
                    .controller
                    .announcement_draft
                    .clone()
                    .filter(|v| !v.trim().is_empty())
                {
                    self.controller_command(crate::controller::Command::AnnouncementPreview(text));
                } else {
                    self.error = "Enter announcement text first.".into();
                }
                return None;
            }
            Control::ControllerAnnouncementEdit => {
                self.controller.announcement_preview = None;
                self.controller.announcement_key = Some(uuid::Uuid::new_v4());
                self.controller.selected = 0;
                return None;
            }
            Control::ControllerAnnouncementSend => {
                if let (Some(preview), Some(text)) = (
                    &self.controller.announcement_preview,
                    &self.controller.announcement_draft,
                ) {
                    let key = *self
                        .controller
                        .announcement_key
                        .get_or_insert_with(uuid::Uuid::new_v4);
                    let recipients = preview["recipients"].as_u64().unwrap_or(0);
                    self.controller_command(crate::controller::Command::AnnouncementCreate(
                        text.clone(),
                        key,
                        recipients,
                    ));
                }
                return None;
            }
            Control::ControllerAnnouncementInput => return None,
            Control::ControllerAnnouncementRefresh => {
                let id = self
                    .controller
                    .announcement_job
                    .as_ref()
                    .or_else(|| {
                        self.controller
                            .announcement_overview
                            .as_ref()
                            .and_then(|v| v.get("last_job"))
                    })
                    .and_then(|v| v["id"].as_str())
                    .and_then(|v| uuid::Uuid::parse_str(v).ok());
                if let Some(id) = id {
                    self.controller_command(crate::controller::Command::AnnouncementStatus(id));
                }
                return None;
            }
            Control::ControllerHistoryItem(index) => {
                let (kind, item) = if let Some(page) = &self.controller.workstation_audit {
                    (
                        "audit",
                        page["items"]
                            .as_array()
                            .and_then(|items| items.get(index))
                            .cloned(),
                    )
                } else if let Some(page) = &self.controller.recovery_history {
                    (
                        "recovery",
                        page["items"]
                            .as_array()
                            .and_then(|items| items.get(index))
                            .cloned(),
                    )
                } else {
                    ("", None)
                };
                if item.is_some() {
                    self.controller.history_detail = Some((kind, index));
                    self.controller.selected = 0;
                }
                return None;
            }
            Control::ControllerHistoryPage(next) => {
                let page_request = self
                    .controller
                    .workstation_audit
                    .as_ref()
                    .map(|page| (page, true))
                    .or_else(|| {
                        self.controller
                            .recovery_history
                            .as_ref()
                            .map(|page| (page, false))
                    })
                    .map(|(page, audit)| {
                        let offset = page["offset"].as_u64().unwrap_or(0) as usize;
                        let total = page["total"].as_u64().unwrap_or(0) as usize;
                        let page_size = page["page_size"].as_u64().unwrap_or(10) as usize;
                        let last = total.saturating_sub(1) / page_size.max(1) * page_size.max(1);
                        let new_offset = if next {
                            (offset + page_size).min(last)
                        } else {
                            offset.saturating_sub(page_size)
                        };
                        (new_offset, audit)
                    });
                if let Some((offset, audit)) = page_request {
                    self.controller_command(if audit {
                        crate::controller::Command::WorkstationAudit(offset)
                    } else {
                        crate::controller::Command::RecoveryHistory(offset)
                    });
                }
                return None;
            }
            Control::ControllerAlerts => {
                self.controller_command(crate::controller::Command::Alerts);
                return None;
            }
            Control::ControllerAlertToggle(key) => {
                if let Some(value) = &self.controller.alerts {
                    let patch = if key == "interval_minutes" {
                        serde_json::json!({"interval_minutes":if value["interval_minutes"] == 5 {15}else{5}})
                    } else {
                        serde_json::json!({key:value[key] != true})
                    };
                    self.controller_command(crate::controller::Command::AlertPreferences(patch));
                }
                return None;
            }
            Control::ControllerAlertDismiss(index) => {
                if let Some(id) = self
                    .controller
                    .alerts
                    .as_ref()
                    .and_then(|v| v["active"].as_array())
                    .and_then(|v| v.get(index))
                    .and_then(|v| v["event_id"].as_str())
                    .and_then(|v| uuid::Uuid::parse_str(v).ok())
                {
                    self.controller_command(crate::controller::Command::AlertDismiss(id));
                }
                return None;
            }
            Control::ControllerAlertPage(next) => {
                let count = self
                    .controller
                    .alerts
                    .as_ref()
                    .and_then(|v| v["active"].as_array())
                    .map_or(0, Vec::len);
                self.controller.alerts_page = if next {
                    (self.controller.alerts_page + 1).min(count.saturating_sub(1) / 5)
                } else {
                    self.controller.alerts_page.saturating_sub(1)
                };
                self.controller.selected = 0;
                return None;
            }
            Control::ControllerBotTitle => {
                self.controller_command(crate::controller::Command::BotTitle);
                return None;
            }
            Control::ControllerSshKey => {
                self.controller_command(crate::controller::Command::SshKey);
                return None;
            }
            Control::ControllerGateMessage | Control::ControllerBotTitleEdit => {
                let (key, value) = if control == Control::ControllerGateMessage {
                    (
                        "gate_message",
                        self.controller
                            .policies
                            .as_ref()?
                            .pointer("/requests/gate_message")
                            .and_then(serde_json::Value::as_str)
                            .unwrap_or(""),
                    )
                } else {
                    (
                        "bot_title",
                        self.controller.bot_title.as_ref()?["title"]
                            .as_str()
                            .unwrap_or(""),
                    )
                };
                self.controller.defaults_input = Some((key, value.to_string()));
                self.controller.selected = 0;
                return None;
            }
            Control::ControllerSshKeyCopy => {
                if let Some(key) = self
                    .controller
                    .ssh_key
                    .as_ref()
                    .and_then(|v| v["public_key"].as_str())
                {
                    let result = (|| -> Result<()> {
                        if self.clipboard.is_none() {
                            self.clipboard = Some(arboard::Clipboard::new()?);
                        }
                        self.clipboard.as_mut().unwrap().set_text(key.to_string())?;
                        Ok(())
                    })();
                    if result.is_ok() {
                        self.error.clear();
                        self.controller.notice = "Public key copied.".into();
                    } else {
                        self.error = "Clipboard unavailable.".into();
                    }
                }
                return None;
            }
            Control::ControllerPolicies => {
                self.requests_settings = true;
                self.controller_command(crate::controller::Command::Policies);
                return None;
            }
            Control::ControllerPolicyToggle(choice) => {
                if let Some(value) = &self.controller.policies {
                    let (kind, patch) = match choice {
                        "traffic" => (
                            "traffic",
                            serde_json::json!({"enabled":value["traffic"]["enabled"] != true}),
                        ),
                        "requests" => (
                            "access-requests",
                            serde_json::json!({"enabled":value["requests"]["enabled"] != true}),
                        ),
                        _ => (
                            "access-requests",
                            serde_json::json!({"notify_requests":value["requests"]["notify_requests"] != true}),
                        ),
                    };
                    self.controller_command(crate::controller::Command::PolicySave(kind, patch));
                }
                return None;
            }
            Control::ControllerDefaultsInput => {
                self.controller.selected = 0;
                return None;
            }
            Control::ControllerDefaultsInputApply => {
                if let Some((key, input)) = self.controller.defaults_input.clone() {
                    if key == "gate_message" {
                        self.controller_command(crate::controller::Command::PolicySave(
                            "access-requests",
                            serde_json::json!({"gate_message":input}),
                        ));
                        return None;
                    }
                    if key == "bot_title" {
                        if input.trim().is_empty() || input.chars().count() > 64 {
                            self.error = "Enter a name from 1 to 64 characters".into();
                            return None;
                        }
                        self.controller_command(crate::controller::Command::BotTitleSave(input));
                        return None;
                    }
                }
                if let Some((key, input)) = &self.controller.defaults_input {
                    let value = if key.ends_with("port") {
                        match input.parse::<u16>() {
                            Ok(port) if port > 0 => serde_json::json!(port),
                            _ => {
                                self.error = "Enter a port from 1 to 65535".into();
                                return None;
                            }
                        }
                    } else {
                        serde_json::json!(input.trim())
                    };
                    if let Some(defaults) = &mut self.controller.defaults {
                        defaults["settings"][*key] = value;
                    }
                    self.action_focus = Some(Control::ControllerDefaultsField(key));
                    self.controller.defaults_input = None;
                    self.error.clear();
                }
                return None;
            }
            Control::ControllerDefaultsField(key) => {
                if let Some(defaults) = &self.controller.defaults {
                    let value = &defaults["settings"][key];
                    self.controller.defaults_input = Some((
                        key,
                        value.as_str().map(str::to_owned).unwrap_or_else(|| {
                            if value.is_null() {
                                String::new()
                            } else {
                                value.to_string()
                            }
                        }),
                    ));
                    self.controller.selected = 0;
                }
                return None;
            }
            Control::ControllerDefaultsAdvanced => {
                self.controller.defaults_advanced = !self.controller.defaults_advanced;
                self.controller.selected = 0;
                return None;
            }
            Control::ControllerDefaultsMode => {
                if let Some(value) = &mut self.controller.defaults {
                    if value["settings"]["awg_port_mode"] == "auto" {
                        value["settings"]["awg_port_mode"] = serde_json::json!("manual");
                        value["settings"]["awg_port"] = serde_json::json!(443);
                    } else {
                        value["settings"]["awg_port_mode"] = serde_json::json!("auto");
                        value["settings"]
                            .as_object_mut()
                            .unwrap()
                            .remove("awg_port");
                    }
                }
                return None;
            }
            Control::ControllerDefaults => {
                self.controller_command(crate::controller::Command::Defaults);
                return None;
            }
            Control::ControllerDefaultsToggle(choice) => {
                if let Some(value) = &mut self.controller.defaults {
                    let field = if matches!(choice, "tcp" | "xhttp") {
                        "xray_transports"
                    } else {
                        "protocols"
                    };
                    let mut items = value[field].as_array().cloned().unwrap_or_default();
                    if items.iter().any(|v| v == choice) {
                        items.retain(|v| v != choice);
                    } else {
                        items.push(serde_json::json!(choice));
                    }
                    value[field] = serde_json::json!(items);
                    if choice == "xray" {
                        value["xray_transports"] = if items.iter().any(|v| v == "xray") {
                            serde_json::json!(["tcp", "xhttp"])
                        } else {
                            serde_json::json!([])
                        };
                    }
                }
                return None;
            }
            Control::ControllerDefaultsPreset => {
                if let Some(value) = &mut self.controller.defaults {
                    value["settings"]["awg_i1_preset"] =
                        serde_json::json!(match value["settings"]["awg_i1_preset"].as_str() {
                            Some("quic") => "dns",
                            Some("dns") => "chaos",
                            _ => "quic",
                        });
                }
                return None;
            }
            Control::ControllerDefaultsReset => {
                self.controller.defaults = self.controller.defaults_baseline.clone();
                self.controller.selected = 0;
                return None;
            }
            Control::ControllerDefaultsSave => {
                if let Some(value) = &self.controller.defaults {
                    self.controller_command(crate::controller::Command::DefaultsSave(serde_json::json!({"protocols":value["protocols"],"xray_transports":value["xray_transports"],"settings":value["settings"]}),value["revision"].as_u64()?));
                }
                return None;
            }
            Control::ControllerBackups
            | Control::ControllerBackupCreate
            | Control::ControllerBackupAuto
            | Control::ControllerBackupInterval
            | Control::ControllerBackupKeep
            | Control::ControllerBackupCatalog(_)
            | Control::ControllerBackupDetail(_)
            | Control::ControllerBackupRestore
            | Control::ControllerBackupDelete => {
                use crate::controller::Command;
                let command = match control {
                    Control::ControllerBackups => Command::Backups,
                    Control::ControllerBackupCatalog(offset) => Command::BackupCatalog(offset),
                    Control::ControllerBackupDetail(index) => {
                        let id = self.controller.backup_catalog.as_ref()?["items"][index]["id"]
                            .as_str()
                            .and_then(|id| uuid::Uuid::parse_str(id).ok())?;
                        Command::BackupDetail(id)
                    }
                    Control::ControllerBackupCreate => Command::BackupQueue(
                        serde_json::json!({"action":"create"}),
                        uuid::Uuid::new_v4(),
                    ),
                    Control::ControllerBackupDelete => {
                        let detail = self.controller.backup_detail.as_ref()?;
                        let id = detail["id"]
                            .as_str()
                            .and_then(|v| uuid::Uuid::parse_str(v).ok())?;
                        Command::BackupDelete(id, detail["checksum"].as_str()?.into())
                    }
                    Control::ControllerBackupRestore => {
                        let detail = self.controller.backup_detail.as_ref()?;
                        Command::BackupQueue(
                            serde_json::json!({"action":"restore","backup_id":detail["id"],"checksum":detail["checksum"]}),
                            uuid::Uuid::new_v4(),
                        )
                    }
                    _ => {
                        let value = self.controller.backups.as_ref()?;
                        let patch = match control {
                            Control::ControllerBackupAuto => {
                                serde_json::json!({"enabled":value["enabled"] != true})
                            }
                            Control::ControllerBackupInterval => {
                                serde_json::json!({"interval_hours":match value["interval_hours"].as_u64().unwrap_or(24) {6 => 12,12 => 24,_ => 6}})
                            }
                            _ => {
                                serde_json::json!({"keep_count":match value["keep_count"].as_u64().unwrap_or(10) {5 => 10,10 => 20,_ => 5}})
                            }
                        };
                        Command::BackupPreferences(patch)
                    }
                };
                self.controller_command(command);
                return None;
            }
            Control::ControllerUpdateResult | Control::ControllerUpdateDismiss => {
                if self.controller.update_result.is_none() {
                    self.controller.components_page = 0;
                    self.controller.selected = 0;
                }
                if let Some(id) = self
                    .controller
                    .update_result
                    .as_ref()
                    .or_else(|| {
                        self.controller
                            .updates
                            .as_ref()
                            .and_then(|v| v.get("latest_job"))
                    })
                    .and_then(|v| v["id"].as_str())
                    .and_then(|id| uuid::Uuid::parse_str(id).ok())
                {
                    self.controller_command(if control == Control::ControllerUpdateDismiss {
                        crate::controller::Command::UpdateDismiss(id)
                    } else {
                        crate::controller::Command::UpdateResult(id)
                    });
                }
                return None;
            }
            Control::ControllerUpdateBranch => {
                self.controller.branch_confirmation = true;
                self.controller.selected = 0;
                return None;
            }
            Control::ControllerUpdateBranchConfirm(confirm) => {
                self.controller.branch_confirmation = false;
                self.controller.selected = 0;
                if confirm && let Some(value) = &self.controller.updates {
                    let branch = if value["branch"] == "main" {
                        "dev"
                    } else {
                        "main"
                    };
                    self.controller_command(crate::controller::Command::UpdatePreferences(
                        serde_json::json!({"branch":branch}),
                    ));
                }
                return None;
            }
            Control::ControllerComponents => {
                self.controller_command(crate::controller::Command::UpdateComponents);
                return None;
            }
            Control::ControllerComponentPage(next) => {
                let count = self
                    .controller
                    .update_result
                    .as_ref()
                    .and_then(|v| v["items"].as_array())
                    .or_else(|| {
                        self.controller
                            .components
                            .as_ref()
                            .and_then(|v| v["nodes"].as_array())
                    })
                    .map_or(0, Vec::len);
                self.controller.components_page = if next {
                    (self.controller.components_page + 1).min(count.saturating_sub(1) / 5)
                } else {
                    self.controller.components_page.saturating_sub(1)
                };
                self.controller.selected = 0;
                return None;
            }
            Control::ControllerComponentNode(index) => {
                if let Some(key) = self
                    .controller
                    .components
                    .as_ref()
                    .and_then(|v| v["nodes"].as_array())
                    .and_then(|v| v.get(index))
                    .and_then(|v| v["key"].as_str())
                    .map(str::to_owned)
                {
                    self.nodes_updates_origin = true;
                    self.screen = Screen::Nodes;
                    self.nodes_sidebar = false;
                    self.nodes.maintenance = None;
                    self.nodes.diagnostics = None;
                    self.nodes_requested = Some(crate::nodes::Command::Card(key));
                }
                return None;
            }
            Control::ControllerVersions | Control::ControllerVersionsPage(_) => {
                let offset = match control {
                    Control::ControllerVersionsPage(offset) => offset,
                    _ => 0,
                };
                self.controller_command(crate::controller::Command::UpdateVersions(offset));
                return None;
            }
            Control::ControllerVersion(index) => {
                if let Some(item) = self
                    .controller
                    .versions
                    .as_ref()
                    .and_then(|v| v["items"].as_array())
                    .and_then(|v| v.get(index))
                {
                    let target = item["ref"].as_str().unwrap_or("").to_string();
                    if !target.is_empty() {
                        self.form.select_action(Action::Update);
                        self.form.fields[4] = target;
                        self.form.selected = 1;
                        self.screen = Screen::Form;
                        self.controller.versions = None;
                    }
                }
                return None;
            }
            Control::ControllerUpdates
            | Control::ControllerUpdateCheck
            | Control::ControllerUpdateAuto
            | Control::ControllerUpdateInterval => {
                let command = match control {
                    Control::ControllerUpdates => crate::controller::Command::Updates,
                    Control::ControllerUpdateCheck => crate::controller::Command::UpdateCheck,
                    _ => {
                        let value = self.controller.updates.as_ref()?;
                        let patch = match control {
                            Control::ControllerUpdateAuto => {
                                serde_json::json!({"auto_check_enabled": value["auto_check_enabled"] != true})
                            }
                            _ => {
                                let current =
                                    value["auto_check_interval_minutes"].as_u64().unwrap_or(60);
                                let next = match current {
                                    15 => 60,
                                    60 => 360,
                                    360 => 1440,
                                    _ => 15,
                                };
                                serde_json::json!({"auto_check_interval_minutes":next})
                            }
                        };
                        crate::controller::Command::UpdatePreferences(patch)
                    }
                };
                self.controller_command(command);
                return None;
            }
            Control::ControllerSection(section) => {
                self.controller_section = Some(section);
                self.requests_settings = false;
                if section == ControllerSection::Policies {
                    self.controller_command(crate::controller::Command::Policies);
                }
                self.controller.selected = 0;
                self.controller_viewport.set(0);
                return None;
            }
            Control::ControllerCheck => {
                self.controller_command(crate::controller::Command::Probe);
                return None;
            }
            Control::ControllerNew => {
                if !self.nodes_busy {
                    self.screen = Screen::NewInstallation;
                    self.confirm = false;
                }
                return None;
            }
            Control::NewInstallationConfirm(yes) => {
                if yes {
                    if let Some(saved) = &self.form.saved
                        && let Err(error) = crate::controller::Browser::forget_verification(
                            &self.state_dir,
                            saved.id,
                        )
                    {
                        self.error = error.to_string();
                        return None;
                    }
                    self.controller = crate::controller::Browser::default();
                    self.fresh_installation = true;
                    self.form.select_action(Action::Install);
                    self.form.fields[7].clear();
                    self.form.fields[6].zeroize();
                    self.form.selected = 1;
                    self.screen = Screen::Form;
                    self.error.clear();
                } else {
                    self.screen = Screen::Controller;
                }
                return None;
            }
            Control::Controller => {
                self.form.select_action(Action::Install);
                self.open_controller();
                return None;
            }
            Control::ControllerScope => {
                self.controller.cleanup_nodes = !self.controller.cleanup_nodes;
                return None;
            }
            Control::ControllerPlan(remove) => {
                if self.nodes_busy {
                    return None;
                }
                self.controller.plan_pending = true;
                self.controller_command(crate::controller::Command::Plan {
                    action: if remove { "remove" } else { "reset" }.into(),
                    cleanup_nodes: self.controller.cleanup_nodes,
                });
                return None;
            }
            Control::ControllerPhrase => {
                return None;
            }
            Control::ControllerConfirm => {
                if let Some(plan) = &self.controller.plan {
                    if self.controller.phrase != plan["confirmation_phrase"].as_str().unwrap_or("")
                    {
                        self.error = "Enter the confirmation phrase exactly.".into();
                    } else if let Ok(id) = uuid::Uuid::parse_str(plan["id"].as_str().unwrap_or(""))
                    {
                        self.controller_command(crate::controller::Command::Queue {
                            plan: id,
                            phrase: self.controller.phrase.clone(),
                            key: self.controller.key.unwrap_or_default(),
                        });
                    }
                }
                return None;
            }
            Control::ControllerRefresh => {
                if self.controller.verifying
                    && let Some(deployment) = self
                        .controller
                        .overview
                        .as_ref()
                        .and_then(|v| v.get("deployment"))
                        .cloned()
                {
                    self.controller.verification_started = Some(std::time::Instant::now());
                    self.controller_command(crate::controller::Command::Verify(deployment));
                    return None;
                }
                if let Some(command) = self.controller.poll() {
                    self.controller_command(command);
                } else if let Some(id) = self
                    .controller
                    .job
                    .as_ref()
                    .and_then(|j| j["id"].as_str())
                    .and_then(|s| uuid::Uuid::parse_str(s).ok())
                {
                    self.controller_command(crate::controller::Command::Job(id));
                } else {
                    self.controller_command(crate::controller::Command::Overview);
                }
                return None;
            }
            Control::ControllerRetry | Control::ControllerShutdown => {
                if let Some(id) = self
                    .controller
                    .job
                    .as_ref()
                    .and_then(|j| j["id"].as_str())
                    .and_then(|s| uuid::Uuid::parse_str(s).ok())
                {
                    let key = uuid::Uuid::new_v4();
                    self.controller_command(if control == Control::ControllerShutdown {
                        crate::controller::Command::Shutdown(id, key)
                    } else {
                        crate::controller::Command::Retry(id, key)
                    });
                }
                return None;
            }
            Control::ControllerBack => {
                if self.controller.plan_pending {
                    return None;
                }
                if self.controller.branch_confirmation {
                    self.controller.branch_confirmation = false;
                    self.controller.selected = 0;
                    return None;
                }
                if self.controller.history_detail.take().is_some() {
                    self.controller.selected = 0;
                    return None;
                }
                if self.controller.workstation_audit.take().is_some()
                    || self.controller.recovery_history.take().is_some()
                {
                    self.controller.selected = 0;
                    return None;
                }
                if self.controller.announcement_job.take().is_some() {
                    self.controller.selected = 0;
                    return None;
                }
                if self.controller.announcement_preview.take().is_some() {
                    self.controller.selected = 0;
                    return None;
                }
                if self.controller.announcement_draft.take().is_some() {
                    self.controller.announcement_key = None;
                    self.controller.selected = 0;
                    return None;
                }
                if self.controller.announcement_overview.take().is_some() {
                    self.controller.selected = 0;
                    return None;
                }
                if self.controller.defaults_input.take().is_some() {
                    self.controller.selected = 0;
                    self.error.clear();
                    return None;
                }
                if self.form.action == Action::Update
                    && self.controller.updates.is_some()
                    && self.controller.versions.is_none()
                    && self.controller.components.is_none()
                    && self.controller.update_result.is_none()
                {
                    self.controller.updates = None;
                    self.screen = Screen::Idle;
                    return None;
                }
                if self.requests_settings {
                    self.requests_settings = false;
                    self.controller.selected = 0;
                    self.controller_viewport.set(0);
                    return None;
                }
                if self.controller.alerts.take().is_some()
                    || self.controller.bot_title.take().is_some()
                    || self.controller.ssh_key.take().is_some()
                    || self.controller.defaults.take().is_some()
                    || self.controller.backup_detail.take().is_some()
                    || self.controller.backup_catalog.take().is_some()
                    || self.controller.backups.take().is_some()
                    || self.controller.update_result.take().is_some()
                    || self.controller.components.take().is_some()
                    || self.controller.versions.take().is_some()
                    || self.controller.updates.take().is_some()
                {
                    self.controller.selected = 0;
                } else if self.controller.plan.take().is_some() {
                    self.controller.phrase.clear();
                    self.controller.selected = 0;
                } else if self.controller_section.take().is_some() {
                    self.controller.selected = 0;
                    self.controller_viewport.set(0);
                } else if self.form.action == Action::Update {
                    self.screen = Screen::Idle;
                } else {
                    self.controller.selected = 0;
                    self.controller_sidebar = true;
                }
                return None;
            }
            Control::Action(action) => {
                if !self.actions_enabled() || action != Action::Install && !self.controller_ready()
                {
                    return None;
                }
                self.quick_focus = false;
                self.temporary.artifact = None;
                self.fresh_installation = false;
                self.form.select_action(action);
                self.screen = Screen::Form;
                self.editor = None;
                self.error.clear();
                if action == Action::Install && self.has_installation() {
                    self.open_controller();
                } else if action == Action::PrepareNode {
                    self.open_nodes();
                } else if action == Action::Update {
                    self.open_controller();
                    self.activate(Control::ControllerUpdates);
                }
                return None;
            }
            Control::NodesEdit => {
                if let Some(node) = &self.nodes.card {
                    self.nodes_requested =
                        Some(crate::nodes::Command::EditContext(node.key.clone()));
                }
                return None;
            }
            Control::NodesEditTab(tab) => {
                if let Some(editor) = &mut self.nodes.editor {
                    editor.tab = tab;
                    editor.page = 0;
                }
                self.nodes_selected = 0;
                return None;
            }
            Control::NodesEditField(_) => {
                self.focus(control);
                return None;
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
                return None;
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
                return None;
            }
            Control::NodesEditPage(next) => {
                if let Some(editor) = &mut self.nodes.editor {
                    let count = if editor.tab == crate::node_editor::Tab::Protocols {
                        if editor.protocols.iter().any(|p| p == "xray") {
                            4
                        } else {
                            2
                        }
                    } else {
                        editor.field_indices().len()
                    };
                    let last = count
                        .div_ceil(self.nodes_capacity.get().max(1))
                        .saturating_sub(1);
                    editor.page = if next {
                        (editor.page + 1).min(last)
                    } else {
                        editor.page.saturating_sub(1)
                    };
                }
                self.nodes_selected = 0;
                return None;
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
                return None;
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
                return None;
            }
            Control::NodesEditReset => {
                if let Some(editor) = &mut self.nodes.editor {
                    editor.reset();
                }
                self.error.clear();
                return None;
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
                return None;
            }
            Control::NodesApply => {
                if let Some(node) = &self.nodes.card {
                    self.nodes_requested = Some(crate::nodes::Command::ApplySettings(
                        node.clone(),
                        uuid::Uuid::new_v4(),
                    ));
                }
                return None;
            }
            Control::NodesMaintenance => {
                if let Some(node) = &self.nodes.card {
                    self.nodes_requested =
                        Some(crate::nodes::Command::Maintenance(node.key.clone(), None));
                }
                return None;
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
                return None;
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
                return None;
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
                return None;
            }
            Control::NodesDiagnosticItem(index) => {
                if let Some((_, inventory)) = &self.nodes.diagnostics {
                    self.nodes.diagnostic_item = inventory["items"]
                        .as_array()
                        .and_then(|items| items.get(index))
                        .cloned();
                    self.nodes_selected = 0;
                }
                return None;
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
                return None;
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
                return None;
            }
            Control::NodesSearch => {
                self.nodes_selected = 0;
                self.nodes_sidebar = false;
                return None;
            }
            Control::NodesRegion(index) => {
                if let Some(node) = self.nodes.items.get(index) {
                    if !self.nodes.collapsed.remove(&node.region) {
                        self.nodes.collapsed.insert(node.region.clone());
                    }
                    self.nodes.page = 0;
                    self.nodes_selected = 0;
                }
                return None;
            }
            Control::NodesCard(index) => {
                if let Some(node) = self.nodes.items.get(index) {
                    self.nodes_requested = Some(crate::nodes::Command::Card(node.key.clone()));
                }
                return None;
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
                return None;
            }
            Control::NodesBack => {
                if self.nodes.diagnostics.is_none()
                    && let Some(view) = &mut self.nodes.maintenance
                {
                    self.nodes_selected = 0;
                    if view.confirmation.take().is_some() {
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
                return None;
            }
            Control::NodesNew => {
                self.node_wizard = None;
                self.wizard_loading = true;
                self.nodes_requested = Some(crate::nodes::Command::CreationOptions);
                return None;
            }
            Control::WizardChoice(index) => {
                if let Some(wizard) = &mut self.node_wizard {
                    self.error = wizard
                        .choose(index)
                        .err()
                        .map(|e| e.to_string())
                        .unwrap_or_default();
                }
                return None;
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
                return None;
            }
            Control::WizardBack => {
                self.error.clear();
                if self.node_wizard.as_mut().is_none_or(|w| !w.back()) {
                    self.node_wizard = None;
                    self.screen = Screen::Nodes;
                }
                return None;
            }
            Control::WizardCancel => {
                self.node_wizard = None;
                self.screen = Screen::Nodes;
                self.error.clear();
                return None;
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
                return None;
            }
            Control::NodesProtocolAction(action) => {
                if let Some(node) = &self.nodes.card {
                    self.nodes_requested =
                        Some(crate::nodes::Command::ProtocolAction(node.clone(), action));
                    self.nodes.maintenance = None;
                }
                return None;
            }
            Control::NodesSetup | Control::NodesBootstrap => {
                if let Some(node) = &self.nodes.card {
                    self.nodes_requested = Some(if matches!(control, Control::NodesSetup) {
                        crate::nodes::Command::Setup(node.clone())
                    } else {
                        crate::nodes::Command::Bootstrap(node.clone())
                    });
                }
                return None;
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
                return None;
            }
            Control::QuickProfile => {
                self.activate_profile();
                return None;
            }
            Control::CycleProfile(backwards) => {
                if let Err(error) = self.cycle_profile(backwards) {
                    self.error = error.to_string();
                }
                return None;
            }
            Control::Settings => {
                self.open_settings();
                return None;
            }
            Control::SettingsPage(page) => {
                if page == SettingsPage::Appearance {
                    self.accent_input = if self.connections.accent == "terminal" {
                        String::new()
                    } else {
                        self.connections.accent.clone()
                    };
                }
                self.screen = Screen::SettingsPage(page);
                self.settings_selected = 1;
                self.error.clear();
                return None;
            }
            Control::Accent(index) => {
                self.settings_selected = index + 1;
                let mut next = self.connections.clone();
                let selected = self.themes.get(index)?;
                next.accent = selected.accent.clone();
                next.theme = Some(selected.id.clone());
                match next.save(&self.state_dir) {
                    Ok(()) => {
                        self.connections = next;
                        self.error.clear();
                    }
                    Err(error) => self.error = error.to_string(),
                }
                return None;
            }
            Control::AccentInput => {
                self.settings_selected = self.themes.len() + 1;
                return None;
            }
            Control::SaveAccent => {
                self.settings_selected = self.themes.len() + 2;
                let mut next = self.connections.clone();
                next.accent = self.accent_input.trim().to_ascii_lowercase();
                next.theme = None;
                match next.save(&self.state_dir) {
                    Ok(()) => {
                        self.connections = next;
                        self.error.clear();
                    }
                    Err(error) => self.error = error.to_string(),
                }
                return None;
            }
            Control::DeleteInstallation => {
                if let Some(profile) = self.connections.installations.get(self.settings_profile) {
                    self.screen = Screen::DeleteInstallation(profile.id);
                    self.confirm = false;
                }
                return None;
            }
            Control::ConfirmDelete(confirmed) => {
                if confirmed {
                    if let Screen::DeleteInstallation(id) = self.screen
                        && let Err(error) = self.delete_profile(id)
                    {
                        self.error = error.to_string();
                    }
                } else {
                    self.screen = Screen::SettingsPage(SettingsPage::Profiles);
                }
                return None;
            }
            Control::SelfUpdate => {
                self.self_requested = true;
                return None;
            }
            Control::CheckWorkstationUpdates => {
                self.self_check_requested = true;
                return None;
            }
            Control::Installation(index) => {
                self.settings_profile = index;
                self.settings_selected = index + 1;
                return None;
            }
            Control::UseInstallation => {
                if let Err(error) = self.use_selected() {
                    self.error = error.to_string();
                }
                return None;
            }
            Control::EditInstallation => {
                self.begin_edit(false);
                return None;
            }
            Control::NewInstallation => {
                self.begin_edit(true);
                return None;
            }
            Control::RepairMode => {
                if let Some(position) = self.form.visible().iter().position(|&i| i == 11) {
                    self.quick_focus = false;
                    self.form.selected = position + 1;
                    toggle_repair(&mut self.form.fields[11]);
                }
                return None;
            }
            Control::Channel(editor, backwards) => {
                self.quick_focus = false;
                if editor {
                    if let Some((_, fields, selected)) = &mut self.editor {
                        *selected = 4;
                        cycle_channel(&mut fields[4], backwards);
                    }
                } else if let Some(position) = self.form.visible().iter().position(|&i| i == 3) {
                    self.form.selected = position + 1;
                    cycle_channel(&mut self.form.fields[3], backwards);
                }
                return None;
            }
            Control::EditField(index) => {
                if let Some(editor) = &mut self.editor {
                    editor.2 = index;
                }
                return None;
            }
            Control::SaveInstallation => {
                if let Err(error) = self.save_edit() {
                    self.error = error.to_string();
                }
                return None;
            }
            Control::CancelEdit => {
                self.editor = None;
                self.screen = Screen::SettingsPage(SettingsPage::Profiles);
                self.error.clear();
                return None;
            }
            Control::WindowClose => return None,
            Control::Exit(false) => {
                self.cancel_exit();
                return None;
            }
            Control::Exit(value) => {
                self.exit_confirm = value;
                KeyCode::Enter
            }
            Control::Field(index) => {
                self.quick_focus = false;
                self.form.selected = index;
                return None;
            }
            Control::Continue => {
                if !self.actions_enabled()
                    || self.form.action != Action::Install && !self.controller_ready()
                {
                    return None;
                }
                self.quick_focus = false;
                self.form.selected = self.form.count() - 1;
                KeyCode::Enter
            }
            Control::Confirm(value) => {
                self.confirm = value;
                KeyCode::Enter
            }
            Control::Prompt(value) => {
                self.trust = value;
                if matches!(
                    &self.prompt,
                    Some((
                        Prompt::Password { .. } | Prompt::SelectAdministrator { .. },
                        _
                    ))
                ) && !value
                {
                    KeyCode::Esc
                } else {
                    KeyCode::Enter
                }
            }
            Control::Administrator(index) => {
                self.administrator_selected = index;
                KeyCode::Enter
            }

            Control::Close => KeyCode::Enter,
            Control::Page(next) => {
                if next {
                    KeyCode::Right
                } else {
                    KeyCode::Left
                }
            }
        };
        Some(KeyEvent::new(key, KeyModifiers::NONE))
    }
}
