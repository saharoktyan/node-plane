//! Construction, saved connections and shared application state.
use super::*;

impl App {
    pub(super) fn new(r: &Request) -> Self {
        Self {
            focus_history: std::collections::HashMap::new(),
            navigation_context: None,
            return_focus: None,
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
            nodes_editor_width: std::cell::Cell::new(64),
            nodes_capacity: std::cell::Cell::new(10),
            self_release: None,
            self_status: "Checking for workstation updates…".into(),
            restart_required: false,
            restart_requested: false,
            self_requested: false,
            self_check_requested: false,
            form: Form::new(r),
            screen: Screen::Form,
            quick_start_return: None,
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
            appearance_viewport: std::cell::Cell::new(0),
            accent_input: String::new(),
            themes: theme::defaults(),
            editor: None,
            exit: false,
            exit_confirm: false,
            discard: None,
            discard_confirm: false,
            discard_bypass: false,
            quick_focus: true,
            profile_flash: None,
            result_scroll: 0,
            result_max_scroll: std::cell::Cell::new(0),
            result_page_size: std::cell::Cell::new(1),
        }
    }
    pub(super) fn open_quick_start(&mut self) {
        let previous = std::mem::replace(&mut self.screen, Screen::QuickStart);
        if !matches!(previous, Screen::QuickStart) {
            self.quick_start_return = Some(previous);
        }
        if self.controller_ready() {
            self.controller.onboarding = None;
            self.controller.profile = self.form.saved.as_ref().map(|p| p.id);
            self.controller_command(crate::controller::Command::Onboarding);
        }
    }

    pub(super) fn load_connections(&mut self, request: &Request) -> Result<()> {
        self.connections = Connections::load(&self.state_dir)?;
        self.form.fields[11] =
            (self.connections.diagnostic_recovery || request.workflow.repair).to_string();
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
    pub(super) fn actions_enabled(&self) -> bool {
        self.form.saved.is_some()
    }
    pub(super) fn workflow_confirmation(&self) -> (&'static str, String) {
        let host = &self.form.fields[0];
        let release = if self.form.fields[4].is_empty() {
            "latest release"
        } else {
            &self.form.fields[4]
        };
        match self.form.action {
            Action::Install => (
                "Install Node Plane?",
                format!(
                    "Install {release} on {host}.\n\nPrepare PostgreSQL, controller services, the Telegram bot and driver.\nSSH user: {}",
                    self.form.fields[2]
                ),
            ),
            Action::Update => (
                "Update Node Plane?",
                format!(
                    "Update {host} to {release}.\n\nController services restart during the update. Managed nodes update to the matching release. Failed node updates are reported separately."
                ),
            ),
            Action::Diagnose => (
                "Run installation diagnostics?",
                format!(
                    "Check controller services, database schema, registered servers and interrupted operations on {host}.\n\n{}",
                    if self.form.fields[11] == "true" {
                        "Offer recovery actions with separate confirmation."
                    } else {
                        "Show the diagnostic report."
                    }
                ),
            ),
            Action::PrepareNode => (
                "Prepare node SSH?",
                format!(
                    "Prepare SSH access to {} from controller {host}.\n\nAuthorize the controller public key on the node for installation and maintenance.",
                    self.form.fields[8]
                ),
            ),
        }
    }
    pub(super) fn toggle_diagnostic_recovery(&mut self) {
        let mut next = self.connections.clone();
        next.diagnostic_recovery = self.form.fields[11] != "true";
        match next.save(&self.state_dir) {
            Ok(()) => {
                self.form.fields[11] = next.diagnostic_recovery.to_string();
                self.connections = next;
                self.error.clear();
            }
            Err(error) => self.error = error.to_string(),
        }
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
        let updated =
            self.form.action == Action::Update && self.outcome.as_ref().is_some_and(Result::is_ok);
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
        if updated || matches!(self.screen, Screen::Controller) {
            self.open_controller();
            if updated {
                self.controller_command(crate::controller::Command::Updates);
            }
        }
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
}
