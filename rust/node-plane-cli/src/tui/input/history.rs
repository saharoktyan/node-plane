//! Remember content focus independently for each navigation level.
use super::super::*;

#[derive(Clone, Copy)]
pub(in crate::tui) struct Bookmark {
    index: usize,
    control: Option<Control>,
    viewport: Option<usize>,
    controller_viewport: usize,
}

impl App {
    pub(in crate::tui) fn navigation_key(&self) -> Option<String> {
        let profile = self.form.saved.as_ref().map(|p| p.id);
        let value_id = |value: &Option<serde_json::Value>| {
            value
                .as_ref()
                .and_then(|v| v["id"].as_str())
                .unwrap_or("")
                .to_owned()
        };
        let view = match &self.screen {
            Screen::Controller => {
                let state = &self.controller;
                let detail = if state.branch_confirmation {
                    "branch"
                } else if state.history_detail.is_some() {
                    "history-detail"
                } else if state.workstation_audit.is_some() {
                    "audit"
                } else if state.recovery_history.is_some() {
                    "recovery"
                } else if state.announcement_job.is_some() {
                    "announcement-job"
                } else if state.announcement_preview.is_some() {
                    "announcement-preview"
                } else if state.announcement_draft.is_some() {
                    "announcement-draft"
                } else if state.announcement_overview.is_some() {
                    "announcements"
                } else if state.defaults_input.is_some() {
                    "defaults-input"
                } else if state.alerts.is_some() {
                    "alerts"
                } else if state.bot_title.is_some() {
                    "bot-title"
                } else if state.ssh_key.is_some() {
                    "ssh-key"
                } else if self.requests_settings {
                    "requests"
                } else if state.defaults.is_some() {
                    "defaults"
                } else if state.backup_detail.is_some() {
                    "backup-detail"
                } else if state.backup_catalog.is_some() {
                    "backup-catalog"
                } else if state.backups.is_some() {
                    "backups"
                } else if state.update_result.is_some() {
                    "update-result"
                } else if state.components.is_some() {
                    "components"
                } else if state.versions.is_some() {
                    "versions"
                } else if state.updates.is_some() {
                    "updates"
                } else if state.plan.is_some() {
                    "cleanup-plan"
                } else {
                    "root"
                };
                format!(
                    "controller:{:?}:{:?}:{detail}",
                    std::mem::discriminant(&self.form.action),
                    self.controller_section.map(|v| std::mem::discriminant(&v))
                )
            }
            Screen::Access => {
                let state = &self.access;
                let detail = if let Some(devices) = &state.devices {
                    if devices.editor.is_some() {
                        "device-editor"
                    } else if devices.delete_key.is_some() {
                        "device-delete"
                    } else if devices.card.is_some() {
                        "device-card"
                    } else {
                        "devices"
                    }
                } else if let Some(editor) = &state.editor {
                    if editor.expiry_view {
                        "expiry"
                    } else if editor.grants_view {
                        "grants"
                    } else if editor.future_view {
                        "future"
                    } else {
                        "editor"
                    }
                } else if state.account_action.is_some() {
                    "account-action"
                } else if state.profile_action.is_some() {
                    "profile-action"
                } else if state.decision.is_some() {
                    "decision"
                } else if state.card.is_some() {
                    "card"
                } else {
                    "list"
                };
                let detail = if let Some(step) = state
                    .editor
                    .as_ref()
                    .and_then(|editor| editor.creation_step)
                {
                    format!("{detail}:{step}")
                } else {
                    detail.to_owned()
                };
                format!(
                    "access:{:?}:{}:{detail}",
                    std::mem::discriminant(&state.tab),
                    value_id(&state.card)
                )
            }
            Screen::Nodes => {
                let state = &self.nodes;
                let detail = if state.editor.is_some() {
                    "editor"
                } else if state.recovery_confirm.is_some() {
                    "recovery"
                } else if state.diagnostic_item.is_some() {
                    "diagnostic-item"
                } else if state.diagnostics.is_some() {
                    "diagnostics"
                } else if state
                    .maintenance
                    .as_ref()
                    .is_some_and(|m| m.confirmation.is_some())
                {
                    "maintenance-confirm"
                } else if state.maintenance.is_some() {
                    "maintenance"
                } else if state.card.is_some() {
                    "card"
                } else {
                    "list"
                };
                format!(
                    "nodes:{}:{detail}",
                    state.card.as_ref().map_or("", |n| &n.key)
                )
            }
            Screen::Temporary => format!(
                "temporary:{:?}:{}:{}",
                std::mem::discriminant(&self.temporary.view),
                value_id(&self.temporary.card),
                self.qr_visible
            ),
            Screen::Settings => "settings".into(),
            Screen::SettingsPage(page) => format!("settings:{:?}", std::mem::discriminant(page)),
            Screen::EditInstallation => "installation-editor".into(),
            Screen::DeleteInstallation(id) => format!("installation-delete:{id}"),
            Screen::NodeWizard => format!(
                "wizard:{:?}",
                self.node_wizard
                    .as_ref()
                    .map(|w| std::mem::discriminant(&w.step))
            ),
            Screen::Form => format!("form:{:?}", std::mem::discriminant(&self.form.action)),
            Screen::NewInstallation => "new-installation".into(),
            Screen::Idle
            | Screen::QuickStart
            | Screen::Confirm
            | Screen::Running
            | Screen::Finished => return None,
        };
        Some(format!("{profile:?}:{view}"))
    }

    pub(in crate::tui) fn remember_navigation_focus(&mut self, activated: Option<Control>) {
        if (self.return_focus.is_some() && activated.is_none())
            || self.prompt.is_some()
            || self.exit
            || self.discard.is_some()
        {
            return;
        }
        let Some(key) = self.navigation_key() else {
            return;
        };
        let (index, controls) = match self.screen {
            Screen::Controller => (self.controller.selected, self.controller_controls()),
            Screen::Access => (self.access.selected, self.access_controls()),
            Screen::Nodes => (self.nodes_selected, self.nodes_controls()),
            Screen::Temporary => (self.temporary.selected, self.temporary_controls()),
            Screen::Settings => (
                self.settings_selected.saturating_sub(1),
                SettingsPage::ALL
                    .iter()
                    .copied()
                    .map(Control::SettingsPage)
                    .collect(),
            ),
            Screen::SettingsPage(SettingsPage::Profiles) => (
                self.settings_selected.saturating_sub(1),
                self.profile_controls(),
            ),
            Screen::SettingsPage(SettingsPage::Appearance) => (
                self.settings_selected.saturating_sub(1),
                (0..self.themes.len())
                    .map(Control::Accent)
                    .chain([Control::AccentInput, Control::SaveAccent, Control::Settings])
                    .collect(),
            ),
            Screen::SettingsPage(SettingsPage::Updates) => (
                self.settings_selected.saturating_sub(1),
                std::iter::once(Control::CheckWorkstationUpdates)
                    .chain(self.self_release.as_ref().map(|_| Control::SelfUpdate))
                    .chain([Control::Settings])
                    .collect(),
            ),
            Screen::SettingsPage(SettingsPage::Documentation) => (
                self.settings_selected.saturating_sub(1),
                (0..DOCUMENTS.len())
                    .map(Control::Documentation)
                    .chain([Control::Settings])
                    .collect(),
            ),
            Screen::SettingsPage(_) => (
                self.settings_selected.saturating_sub(1),
                vec![Control::Settings],
            ),
            Screen::EditInstallation => (self.editor.as_ref().map_or(0, |e| e.2), Vec::new()),
            Screen::NodeWizard => (
                self.node_wizard.as_ref().map_or(0, |w| w.selected),
                Vec::new(),
            ),
            Screen::Form => (self.form.selected, Vec::new()),
            _ => (0, Vec::new()),
        };
        let index = activated
            .and_then(|c| controls.iter().position(|v| *v == c))
            .unwrap_or(index);
        let control = controls.get(index).copied();
        let index = if self.screen.is_settings() {
            index + 1
        } else {
            index
        };
        self.focus_history.insert(
            key.clone(),
            Bookmark {
                index,
                control,
                viewport: self.manual_viewport.get(),
                controller_viewport: self.controller_viewport.get(),
            },
        );
        self.navigation_context = Some(key);
    }

    pub(in crate::tui) fn restore_navigation_context(&mut self) {
        self.restore_navigation_focus_after_event(false);
    }

    pub(in crate::tui) fn restore_navigation_focus_after_event(&mut self, refresh: bool) {
        let Some(key) = self.navigation_key() else {
            return;
        };
        let changed = self.navigation_context.as_ref() != Some(&key);
        if !changed && !refresh {
            return;
        }
        self.navigation_context = Some(key.clone());
        if changed {
            self.hovered = None;
            self.action_focus = None;
        }
        let Some(saved) = self.focus_history.get(&key).copied() else {
            return;
        };
        self.manual_viewport.set(saved.viewport);
        self.controller_viewport.set(saved.controller_viewport);
        match self.screen {
            Screen::Controller => {
                self.controller.selected = saved.index;
                self.controller_sidebar = false;
            }
            Screen::Access => self.access.selected = saved.index,
            Screen::Nodes => {
                self.nodes_selected = saved.index;
                self.nodes_sidebar = false;
            }
            Screen::Temporary => {
                self.temporary.selected = saved.index;
                self.temporary_sidebar = false;
            }
            Screen::Settings | Screen::SettingsPage(_) => self.settings_selected = saved.index,
            Screen::EditInstallation => {
                if let Some(editor) = &mut self.editor {
                    editor.2 = saved.index;
                }
            }
            Screen::NodeWizard => {
                if let Some(wizard) = &mut self.node_wizard {
                    wizard.selected = saved.index;
                }
            }
            Screen::Form => {
                self.form.selected = saved.index;
                self.quick_focus = false;
            }
            _ => {}
        }
        self.return_focus = saved.control;
    }

    pub(in crate::tui) fn restore_return_focus(&mut self) -> bool {
        let Some(control) = self.return_focus.take() else {
            return false;
        };
        if !self.hits.iter().any(|hit| hit.control == control) {
            return false;
        }
        self.focus(control);
        true
    }
}
