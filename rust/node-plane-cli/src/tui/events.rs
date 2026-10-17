use super::*;

impl App {
    pub(in crate::tui) fn event(&mut self, event: Event) {
        let refresh = matches!(&event, Event::Nodes(_) | Event::Finished(_));
        if refresh {
            self.remember_navigation_focus(None);
        }
        self.event_inner(event);
        self.restore_navigation_focus_after_event(refresh);
    }

    fn event_inner(&mut self, event: Event) {
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
                            if profile == self.form.saved.as_ref().map(|p| p.id)
                                || value["external_removal"] == true
                                    && self.form.saved.as_ref().is_some_and(|saved| {
                                        value["host"] == saved.host
                                            && value["port"] == saved.port
                                            && value["user"] == saved.user
                                    })
                            {
                                if value["installed"] == true {
                                    self.remember_installed();
                                    if matches!(self.screen, Screen::Controller)
                                        && self.nodes_requested.is_none()
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
                                    if value["external_removal"] != true {
                                        return;
                                    }
                                    self.installed_profile = None;
                                    self.error.clear();
                                    if let Some(saved) = &mut self.form.saved {
                                        saved.installed = false;
                                        if let Some(entry) = self
                                            .connections
                                            .installations
                                            .iter_mut()
                                            .find(|entry| entry.id == saved.id)
                                        {
                                            entry.installed = false;
                                        }
                                    }
                                    self.controller = crate::controller::Browser {
                                        profile: self.form.saved.as_ref().map(|p| p.id),
                                        removed: true,
                                        ..Default::default()
                                    };
                                    self.screen = Screen::Controller;
                                    if let Err(error) = self.connections.save(&self.state_dir) {
                                        self.error = error.to_string();
                                    }
                                }
                            }
                            return;
                        }
                        if profile != self.form.saved.as_ref().map(|p| p.id)
                            || profile != self.controller.profile
                        {
                            return;
                        }
                        if matches!(command, crate::controller::Command::Presence) {
                            self.controller_version = profile.and_then(|profile| {
                                let raw = value["current_version"].as_str()?;
                                let version =
                                    raw.split_whitespace().next()?.trim_start_matches('v');
                                semver::Version::parse(version).ok()?;
                                Some((profile, version.to_owned()))
                            });
                            return;
                        }
                        if command.read_only()
                            && self
                                .controller_request_context
                                .is_some_and(|context| !context.is_current(self))
                        {
                            return;
                        }
                        if !matches!(&command, crate::controller::Command::Verify(_)) {
                            self.remember_installed();
                        }
                        let shutdown = matches!(&command, crate::controller::Command::Shutdown(..));
                        let guide = matches!(
                            &command,
                            crate::controller::Command::Onboarding
                                | crate::controller::Command::OnboardingDefaults
                                | crate::controller::Command::OnboardingComplete
                                | crate::controller::Command::OnboardingDismiss
                        );
                        self.controller.apply(command, value);
                        if guide && self.quick_start_completed() {
                            if let Some(key) = self.quick_start_identity()
                                && !self.connections.completed_quick_starts.contains(&key)
                            {
                                self.connections.completed_quick_starts.push(key);
                                if let Err(error) = self.connections.save(&self.state_dir) {
                                    self.error = error.to_string();
                                }
                            }
                            if matches!(self.screen, Screen::QuickStart) {
                                self.screen =
                                    self.quick_start_return.take().unwrap_or(Screen::Idle);
                            }
                        }
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
                            self.probe_requested = false;
                            if let Some(saved) = &mut self.form.saved {
                                saved.installed = false;
                            }
                            if let Some(saved) = self
                                .connections
                                .installations
                                .iter_mut()
                                .find(|saved| Some(saved.id) == profile)
                            {
                                saved.installed = false;
                            }
                            if let Err(error) = self.connections.save(&self.state_dir) {
                                self.error = error.to_string();
                            }
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
                        self.manual_awg_original = None;
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
                        if !matches!(&command, Command::Card(id) if self.temporary.card.as_ref().is_some_and(|v|v["id"] == id.as_str()))
                        {
                            self.temporary.selected = 0;
                        }
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
                            self.nodes.diagnostic_scroll.set(0);
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
                                view.section = previous.section;
                            }
                            if view.confirmation.is_none()
                                && let Some(previous) = &self.nodes.maintenance
                                && previous.key == view.key
                            {
                                view.section = previous.section;
                            }
                            self.nodes.maintenance = Some(view);
                        }
                    }
                    crate::nodes::Update::Operation(operation) => {
                        let finished_setup =
                            matches!(
                                operation.kind.as_str(),
                                "agent-rollouts" | "node-bootstraps"
                            ) && matches!(
                                operation.status.as_str(),
                                "succeeded" | "failed" | "blocked"
                            ) && self.nodes.operation.as_ref().is_some_and(|old| {
                                old.id == operation.id
                                    && matches!(
                                        old.status.as_str(),
                                        "queued" | "awaiting_executor" | "running"
                                    )
                            }) && self
                                .nodes
                                .card
                                .as_ref()
                                .is_some_and(|n| n.key == operation.node_key);
                        if finished_setup {
                            self.nodes_requested =
                                Some(crate::nodes::Command::Card(operation.node_key.clone()));
                        }
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
                if self.restart_required {
                    self.outcome = Some(result);
                    self.screen = Screen::Finished;
                    return;
                }
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
                            if self
                                .controller_request_context
                                .is_some_and(|context| context.read && !context.is_current(self))
                            {
                                return;
                            }
                            if self.controller.telegram_role_pending.take().is_some() {
                                self.controller.telegram_result =
                                    Some(crate::access::role_error(&error));
                                self.controller.selected = 0;
                                self.error.clear();
                                self.screen = Screen::Controller;
                            } else {
                                self.error = error;
                            }
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
                    if self.controller.telegram_role_pending.take().is_some() && !error.is_empty() {
                        self.controller.telegram_result = Some(crate::access::role_error(&error));
                        self.screen = Screen::Controller;
                        self.error.clear();
                        return;
                    }

                    if self.access.role_pending.take().is_some() && !error.is_empty() {
                        self.access.account_action = None;
                        self.access.result = Some(crate::access::role_error(&error));
                        self.error.clear();
                        return;
                    }

                    if !error.is_empty() {
                        if self.access.provision.take().is_some() {
                            self.access.result = Some(crate::access::provision_error(&error));
                            self.access.selected = 0;
                            self.error.clear();
                        } else {
                            self.error = error;
                        }
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
}
