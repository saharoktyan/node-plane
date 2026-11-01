//! Construction, saved connections and shared application state.
use super::*;

impl App {
    pub(super) fn new(r: &Request) -> Self {
        Self {
            manual_awg_original: None,
            text_input: std::cell::RefCell::new(Default::default()),
            focus_history: std::collections::HashMap::new(),
            navigation_context: None,
            return_focus: None,
            clipboard: None,
            temporary: crate::temporary::Browser::default(),
            temporary_sidebar: false,
            temporary_busy: false,
            nodes: crate::nodes::Browser::default(),
            nodes_requested: None,
            nodes_busy: false,
            loading_message: String::new(),
            nodes_background: false,
            controller_request_context: None,
            controller_navigation_revision: 0,
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
            quick_start_dismiss: None,
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
            controller_return_form: false,
            controller_section: None,
            requests_settings: false,
            confirmed_installation: None,
            controller_version: None,
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
            theme_wizard: None,
            themes: theme::defaults(),
            editor: None,
            profile_copy: None,
            install_roots: None,
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
            version_mismatch_dismissed: false,
            version_mismatch_deferred: false,
            version_mismatch_receiver: None,
            version_mismatch_context: None,
        }
    }
    pub(super) fn take_browser_request(
        &mut self,
        operation_active: bool,
    ) -> Option<crate::nodes::Command> {
        // All operations share one event receiver. Do not replace it while a workflow owns it.
        if operation_active || self.nodes_busy {
            return None;
        }
        self.nodes_requested.take()
    }

    pub(super) fn quick_start_identity(&self) -> Option<String> {
        self.form.saved.as_ref().map(|saved| saved.id.to_string())
    }
    pub(super) fn quick_start_completed(&self) -> bool {
        self.connections.hide_quick_start
            || !self.connections.completed_quick_starts.is_empty()
            || self
                .controller
                .onboarding
                .as_ref()
                .is_some_and(|guide| guide["workstation_completed"] == true)
            || self.quick_start_identity().is_some_and(|key| {
                self.connections
                    .completed_quick_starts
                    .iter()
                    .any(|saved| saved == &key || saved.starts_with(&format!("{key}:")))
            })
    }

    pub(super) fn onboarding_poll_ready(
        &self,
        elapsed: std::time::Duration,
        operation_active: bool,
    ) -> bool {
        !self.quick_start_completed()
            && matches!(self.screen, Screen::Idle | Screen::QuickStart)
            && self.controller_ready()
            && !operation_active
            && !self.nodes_busy
            && self.nodes_requested.is_none()
            && self.prompt.is_none()
            && !self.exit
            && elapsed >= std::time::Duration::from_secs(1)
    }

    pub(super) fn open_quick_start(&mut self) {
        if self.quick_start_completed() {
            return;
        }
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
            let previous_profile = self.form.saved.as_ref().map(|p| p.id);
            if previous_profile != Some(profile.id) {
                self.version_mismatch_receiver = None;
                self.version_mismatch_context = None;
                self.version_mismatch_deferred = false;
                if matches!(&self.prompt, Some((Prompt::ConfirmAction { title, .. }, _)) if title == "Version mismatch")
                {
                    self.prompt = None;
                }
            }
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
        let mut label = format!("Status: {status}");
        if self.ssh_state == ConnectionState::Connected
            && self.has_installation()
            && let Some((profile, version)) = &self.controller_version
            && self
                .form
                .saved
                .as_ref()
                .is_some_and(|saved| saved.id == *profile)
            && version != env!("CARGO_PKG_VERSION")
        {
            label.push_str(&format!(
                " · Version mismatch: NPW {} / controller {version}",
                env!("CARGO_PKG_VERSION")
            ));
        }
        label
    }

    pub(super) fn cancel_exit(&mut self) {
        self.exit = false;
        self.hovered = None;
        self.pointer_active = self.pointer_position.is_some();
    }

    pub(super) fn poll_version_mismatch(&mut self, operation_active: bool) {
        if self.version_mismatch_receiver.is_some()
            && !self
                .version_mismatch_context
                .as_ref()
                .is_some_and(|context| {
                    !self.controller.removed
                        && self.form.saved.as_ref().is_some_and(|saved| {
                            self.has_installation()
                                && context.profile_id == saved.id
                                && context.host == saved.host
                                && context.port == saved.port
                                && context.user == saved.user
                                && self.controller_version.as_ref().is_some_and(
                                    |(profile, version)| {
                                        *profile == saved.id
                                            && *version == context.controller_version
                                    },
                                )
                        })
                })
        {
            self.version_mismatch_receiver = None;
            self.version_mismatch_context = None;
            if matches!(&self.prompt, Some((Prompt::ConfirmAction { title, .. }, _)) if title == "Version mismatch")
            {
                self.prompt = None;
            }
            return;
        }
        let answer = self
            .version_mismatch_receiver
            .as_ref()
            .map(|receiver| receiver.try_recv());
        if matches!(&answer, Some(Err(mpsc::TryRecvError::Disconnected))) {
            self.version_mismatch_receiver = None;
            self.version_mismatch_context = None;
            return;
        }
        if let Some(Ok(answer)) = answer {
            self.version_mismatch_receiver = None;
            let context = self.version_mismatch_context.take();
            let is_current = self.form.saved.as_ref().is_some_and(|saved| {
                (saved.installed || self.installed_profile == Some(saved.id))
                    && context.as_ref().is_some_and(|c| {
                        c.profile_id == saved.id
                            && c.host == saved.host
                            && c.port == saved.port
                            && c.user == saved.user
                    })
            });
            if !is_current {
                if matches!(&self.prompt, Some((Prompt::ConfirmAction { title, .. }, _)) if title == "Version mismatch")
                {
                    self.prompt = None;
                }
                return;
            }
            match answer {
                Answer::Confirm(false) | Answer::Cancel => {
                    self.version_mismatch_dismissed = true;
                    if matches!(&self.prompt, Some((Prompt::ConfirmAction { title, .. }, _)) if title == "Version mismatch")
                    {
                        self.prompt = None;
                    }
                }
                Answer::Confirm(true) => {
                    if matches!(&self.prompt, Some((Prompt::ConfirmAction { title, .. }, _)) if title == "Version mismatch")
                    {
                        self.prompt = None;
                    }
                    self.version_mismatch_deferred = true;
                    if let Some(ctx) = context {
                        match ctx.target {
                            MismatchTarget::Controller => {
                                self.open_controller();
                                self.activate(Control::ControllerUpdates);
                            }
                            MismatchTarget::Workstation => {
                                self.activate(Control::SettingsPage(SettingsPage::Updates));
                                self.self_check_requested = true;
                            }
                        }
                    }
                }
                _ => {}
            }
            return;
        }

        if self.version_mismatch_dismissed
            || self.version_mismatch_deferred
            || self.version_mismatch_receiver.is_some()
            || self.prompt.is_some()
            || self.profile_copy.is_some()
            || self.exit
            || self.exit_confirm
            || self.discard.is_some()
            || self.nodes_busy
            || self.nodes_requested.is_some()
            || operation_active
            || self.controller.verifying
            || self.controller.removal_active()
            || self.restart_required
            || self.restart_requested
            || matches!(
                self.screen,
                Screen::Running | Screen::Finished | Screen::SettingsPage(SettingsPage::Updates)
            )
            || self.controller.updates.is_some()
            || self.form.action == Action::Update
            || !self.has_installation()
        {
            return;
        }

        let Some(saved) = &self.form.saved else {
            return;
        };
        if !saved.installed && self.installed_profile != Some(saved.id) {
            return;
        }

        let Some((profile, version)) = &self.controller_version else {
            return;
        };
        if *profile != saved.id {
            return;
        }

        let Some(target) = evaluate_version_mismatch(version, env!("CARGO_PKG_VERSION")) else {
            return;
        };

        let npw_channel = crate::config::default_branch();
        let controller_channel = crate::config::version_branch(version).unwrap_or_else(|_| {
            self.controller
                .updates
                .as_ref()
                .and_then(|u| u["branch"].as_str())
                .unwrap_or("unknown")
        });

        let description = format_version_mismatch_description(
            version,
            controller_channel,
            env!("CARGO_PKG_VERSION"),
            npw_channel,
            target,
            is_archived_migration(version, env!("CARGO_PKG_VERSION")),
        );

        let context = VersionMismatchContext {
            profile_id: saved.id,
            host: saved.host.clone(),
            port: saved.port,
            user: saved.user.clone(),
            controller_version: version.clone(),
            controller_channel: controller_channel.to_string(),
            workstation_version: env!("CARGO_PKG_VERSION").to_string(),
            workstation_channel: npw_channel.to_string(),
            target,
        };

        let (reply, answer) = mpsc::channel();
        self.prompt = Some((
            Prompt::ConfirmAction {
                title: "Version mismatch".into(),
                description,
            },
            reply,
        ));
        self.trust = true;
        self.version_mismatch_receiver = Some(answer);
        self.version_mismatch_context = Some(context);
    }
}

pub(crate) fn parse_clean_semver(raw: &str) -> Option<semver::Version> {
    let clean = raw.split_whitespace().next()?.trim_start_matches('v');
    semver::Version::parse(clean).ok()
}

pub(crate) fn clean_version_str(raw: &str) -> Option<String> {
    let clean = raw.split_whitespace().next()?.trim_start_matches('v');
    semver::Version::parse(clean).ok()?;
    Some(clean.to_string())
}

pub(crate) fn is_archived_migration(controller_raw: &str, npw_raw: &str) -> bool {
    let Some(cv) = parse_clean_semver(controller_raw) else {
        return false;
    };
    let Some(nv) = parse_clean_semver(npw_raw) else {
        return false;
    };
    nv.major == 0
        && cv.major == 1
        && cv.minor == 0
        && cv.patch == 0
        && cv
            .pre
            .as_str()
            .split_once('.')
            .is_some_and(|(stage, number)| {
                matches!(stage, "alpha" | "beta")
                    && !number.is_empty()
                    && number.bytes().all(|b| b.is_ascii_digit())
            })
}

pub(crate) fn evaluate_version_mismatch(
    controller_raw: &str,
    npw_raw: &str,
) -> Option<MismatchTarget> {
    let cv = parse_clean_semver(controller_raw)?;
    let nv = parse_clean_semver(npw_raw)?;
    if cv.major == nv.major && cv.minor == nv.minor && cv.patch == nv.patch && cv.pre == nv.pre {
        return None;
    }
    if is_archived_migration(controller_raw, npw_raw) {
        return Some(MismatchTarget::Controller);
    }
    let cv_clean = semver::Version {
        major: cv.major,
        minor: cv.minor,
        patch: cv.patch,
        pre: cv.pre,
        build: semver::BuildMetadata::EMPTY,
    };
    let nv_clean = semver::Version {
        major: nv.major,
        minor: nv.minor,
        patch: nv.patch,
        pre: nv.pre,
        build: semver::BuildMetadata::EMPTY,
    };
    if cv_clean < nv_clean {
        Some(MismatchTarget::Controller)
    } else {
        Some(MismatchTarget::Workstation)
    }
}

pub(crate) fn format_version_mismatch_description(
    controller_version: &str,
    controller_channel: &str,
    workstation_version: &str,
    workstation_channel: &str,
    target: MismatchTarget,
    is_archived: bool,
) -> String {
    let mut desc = format!(
        "Controller version: {} ({})\nWorkstation version: {} ({})\n\n",
        controller_version, controller_channel, workstation_version, workstation_channel
    );
    if is_archived {
        desc.push_str("The installed controller is from an earlier archived release line (1.0 alpha/beta). Update the controller or perform a fresh installation.\n");
    } else {
        match target {
            MismatchTarget::Controller => {
                desc.push_str("The installed controller is outdated. Update the controller to match this Workstation.\n");
            }
            MismatchTarget::Workstation => {
                desc.push_str("The installed controller is newer than this Workstation. Update Workstation to match.\n");
            }
        }
    }
    if controller_channel != workstation_channel {
        desc.push_str(&format!(
            "Channel mismatch: controller is on '{}', Workstation is on '{}'. Different channels can require matching Workstation or channel switch.\n",
            controller_channel, workstation_channel
        ));
    }
    desc.push_str("\nWarning: Continuing with different versions can cause errors or unsupported actions. Dismissal lasts only until NPW restarts.");
    desc
}
