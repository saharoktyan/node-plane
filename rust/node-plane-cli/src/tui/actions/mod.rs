mod access;
mod controller;
mod nodes;
mod settings;
mod temporary;

use super::*;

impl App {
    pub(in crate::tui) fn unsaved_changes(&self) -> bool {
        let screen = if matches!(self.screen, Screen::QuickStart) {
            self.quick_start_return.as_ref().unwrap_or(&self.screen)
        } else {
            &self.screen
        };
        (matches!(screen, Screen::Controller)
            && self.controller.defaults.is_some()
            && self.controller.defaults != self.controller.defaults_baseline)
            || (matches!(screen, Screen::Access)
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
            || (matches!(screen, Screen::Nodes)
                && self.nodes.editor.as_ref().is_some_and(|e| e.dirty()))
    }
    pub(in crate::tui) fn activate(&mut self, control: Control) -> Option<KeyEvent> {
        let previous_key = self.navigation_key();
        let previous_viewport = self.manual_viewport.get();
        self.remember_navigation_focus(Some(control));
        let result = self.activate_inner(control);
        self.restore_navigation_context();
        if previous_key.is_some()
            && previous_key == self.navigation_key()
            && !is_page_control(control)
        {
            self.manual_viewport.set(previous_viewport);
        }
        result
    }

    fn activate_inner(&mut self, control: Control) -> Option<KeyEvent> {
        if self.restart_required {
            self.restart_requested |= control == Control::Restart;
            return None;
        }
        if control == Control::WindowClose {
            self.exit = true;
            self.exit_confirm = false;
            self.hovered = None;
            return None;
        }
        if !matches!(
            control,
            Control::ControllerDefaultsReset
                | Control::NodesEditReset
                | Control::Accent(_)
                | Control::AccentInput
                | Control::SaveAccent
                | Control::Documentation(_)
        ) {
            self.manual_viewport.set(None);
        }
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
                | Control::ControllerUpdateInterval(_)
                | Control::ControllerUpdateCheck
                | Control::ControllerUpdateLatest
                | Control::ControllerBackupAuto
                | Control::ControllerBackupInterval(_)
                | Control::ControllerBackupKeep(_)
                | Control::ControllerAlertToggle(_)
                | Control::ControllerAlertInterval(_)
                | Control::ControllerComponentPage(_)
                | Control::ControllerVersionsPage(_)
                | Control::ControllerRecoveryPage(_)
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
                && self.manual_awg_original.is_none()
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
            Control::QuickStartRefresh => {
                let (reply, answer) = mpsc::channel();
                self.prompt = Some((
                    Prompt::ConfirmAction {
                        title: "Dismiss Quick Start?".into(),
                        description: "This guide will no longer appear for this installation."
                            .into(),
                    },
                    reply,
                ));
                self.trust = false;
                self.quick_start_dismiss = Some(answer);
                return None;
            }
            Control::QuickStartAction => {
                if self.quick_start_completed() {
                    return None;
                }
                if self.unsaved_changes() {
                    self.screen = self.quick_start_return.take().unwrap_or(Screen::Form);
                    self.error =
                        "Save or discard the current draft before continuing Quick Start.".into();
                    return None;
                }
                let return_screen = self.quick_start_return.take();
                if self.controller_ready() {
                    if let Some(guide) = &self.controller.onboarding {
                        let step = guide["next_step"].as_str().unwrap_or("done");
                        let node = guide["node_key"].as_str().map(str::to_owned);
                        match step {
                            "defaults" => self
                                .controller_command(crate::controller::Command::OnboardingDefaults),
                            "node" => {
                                self.open_nodes();
                                self.activate(Control::NodesNew);
                            }
                            "protocols" => {
                                self.open_nodes();
                                if let Some(key) = node {
                                    self.nodes_requested = Some(crate::nodes::Command::Card(key));
                                }
                            }
                            "access" => self.open_access(),
                            "config" | "done" => {
                                self.quick_start_return = return_screen;
                                self.controller_command(
                                    crate::controller::Command::OnboardingComplete,
                                );
                            }
                            _ => {
                                self.screen = Screen::Controller;
                            }
                        }
                    } else {
                        self.controller.profile = self.form.saved.as_ref().map(|p| p.id);
                        self.controller_command(crate::controller::Command::Onboarding);
                    }
                    return None;
                }
                self.form.select_action(Action::Install);
                self.screen = Screen::Form;
                self.form.selected = 1;
                if self.form.saved.is_none() {
                    self.begin_edit(true);
                }
                return None;
            }
            Control::AccessGrants
            | Control::AccessGrantBack
            | Control::AccessGrantMode(..)
            | Control::AccessGrantPage(..)
            | Control::AccessGrant(..)
            | Control::AccessRule(..)
            | Control::AccessGrantAll(..)
            | Control::AccessEdit
            | Control::AccessIdentity
            | Control::AccessProvisionNext
            | Control::AccessProvisionSubmit
            | Control::AccessProvisionCancel
            | Control::AccessResultClose
            | Control::AccessNew
            | Control::AccessWizardNext
            | Control::AccessWizardBack
            | Control::AccessField(..)
            | Control::AccessSave
            | Control::AccessDeviceItem(..)
            | Control::AccessDeviceNew
            | Control::AccessDeviceEdit
            | Control::AccessDeviceName
            | Control::AccessDeviceSave
            | Control::AccessDeviceDelete
            | Control::AccessDeviceConfirm
            | Control::AccessDevicePage(..)
            | Control::AccessDurationCustom
            | Control::AccessDurationInput
            | Control::AccessDurationApply
            | Control::AccessExpiryMenu
            | Control::AccessExpiry(..)
            | Control::AccessExpiryPage(..)
            | Control::AccessExpiryBack
            | Control::AccessAccountAction(..)
            | Control::AccessAccountConfirm
            | Control::AccessProfileAction(..)
            | Control::AccessProfileConfirm
            | Control::Access
            | Control::AccessTab(..)
            | Control::AccessItem(..)
            | Control::AccessSearch
            | Control::AccessRefresh
            | Control::AccessBack
            | Control::AccessPage(..)
            | Control::AccessDecision(..)
            | Control::AccessConfirm => return self.activate_access(control),
            Control::Temporary
            | Control::NodeTemporary
            | Control::TemporaryChoice(..)
            | Control::TemporaryBack
            | Control::TemporaryNew
            | Control::TemporaryRefresh
            | Control::TemporaryPage(..)
            | Control::TemporaryShow
            | Control::TemporaryCopy
            | Control::TemporaryQr
            | Control::TemporarySave
            | Control::TemporaryRevoke
            | Control::TemporaryConfirm => return self.activate_temporary(control),
            Control::ControllerAudit
            | Control::ControllerAuditFilter(_)
            | Control::ControllerRecoveryPage(_)
            | Control::ControllerRecoveryInventory(_)
            | Control::ControllerRecoveryAction(..)
            | Control::ControllerRecoveryHistory
            | Control::ControllerAnnouncement
            | Control::ControllerAnnouncementCompose
            | Control::ControllerAnnouncementPreview
            | Control::ControllerAnnouncementSend
            | Control::ControllerAnnouncementInput
            | Control::ControllerAnnouncementRefresh
            | Control::ControllerHistoryItem(..)
            | Control::ControllerHistoryPage(..)
            | Control::ControllerAlerts
            | Control::ControllerAlertToggle(..)
            | Control::ControllerAlertInterval(_)
            | Control::ControllerAlertDismiss(..)
            | Control::ControllerAlertPage(..)
            | Control::ControllerBotTitle
            | Control::ControllerSshKey
            | Control::ControllerGateMessage
            | Control::ControllerBotTitleEdit
            | Control::ControllerTelegram
            | Control::ControllerTelegramToken
            | Control::ControllerTelegramAdminAdd
            | Control::ControllerTelegramAdminRemove(_)
            | Control::ControllerSshKeyCopy
            | Control::ControllerPolicies
            | Control::ControllerPolicyToggle(..)
            | Control::ControllerDefaultsInput
            | Control::ControllerTextConfirm
            | Control::ControllerDefaultsInputApply
            | Control::ControllerDefaultsField(..)
            | Control::ControllerDefaultsAdvanced
            | Control::ControllerDefaultsMode
            | Control::ControllerDefaults
            | Control::ControllerDefaultsToggle(..)
            | Control::ControllerDefaultsPreset
            | Control::ControllerDefaultsReset
            | Control::ControllerDefaultsSave
            | Control::ControllerBackups
            | Control::ControllerBackupCreate
            | Control::ControllerBackupAuto
            | Control::ControllerBackupInterval(_)
            | Control::ControllerBackupKeep(_)
            | Control::ControllerBackupCatalog(..)
            | Control::ControllerBackupDetail(..)
            | Control::ControllerBackupRestore
            | Control::ControllerBackupDelete
            | Control::ControllerBackupExport
            | Control::ControllerBackupImport
            | Control::ControllerUpdateResult
            | Control::ControllerUpdateDismiss
            | Control::ControllerUpdateBranch
            | Control::ControllerUpdateBranchConfirm(..)
            | Control::ControllerComponents
            | Control::ControllerComponentPage(..)
            | Control::ControllerComponentNode(..)
            | Control::ControllerVersions
            | Control::ControllerVersionsPage(..)
            | Control::ControllerVersion(..)
            | Control::ControllerUpdates
            | Control::ControllerUpdateCheck
            | Control::ControllerUpdateLatest
            | Control::ControllerUpdateAuto
            | Control::ControllerUpdateInterval(_)
            | Control::ControllerSection(..)
            | Control::ControllerCheck
            | Control::ControllerNew
            | Control::NewInstallationConfirm(..)
            | Control::Controller
            | Control::ControllerScope
            | Control::ControllerPlan(..)
            | Control::ControllerPhrase
            | Control::ControllerConfirm
            | Control::ControllerRefresh
            | Control::ControllerRetry
            | Control::ControllerShutdown
            | Control::ControllerAbort
            | Control::ControllerBack => return self.activate_controller(control),
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
            Control::NodesEdit
            | Control::NodesEditTab(..)
            | Control::NodesEditField(..)
            | Control::NodesEditChoice(..)
            | Control::NodesEditToggle(..)
            | Control::NodesEditSave
            | Control::NodesEditConfirm
            | Control::NodesEditReset
            | Control::NodesEditBack
            | Control::NodesApply
            | Control::NodesMaintenanceSection(_)
            | Control::NodesMaintenance
            | Control::NodesRemove(..)
            | Control::NodesRemoveConfirm
            | Control::NodesDiagnostics
            | Control::NodesDiagnosticPage(..)
            | Control::NodesDiagnosticItem(..)
            | Control::NodesRecovery(..)
            | Control::NodesRecoveryConfirm
            | Control::NodesSearch
            | Control::NodesRegion(..)
            | Control::NodesCard(..)
            | Control::NodesRefresh
            | Control::NodesBack
            | Control::NodesNew
            | Control::WizardChoice(..)
            | Control::WizardNext
            | Control::WizardBack
            | Control::WizardCancel
            | Control::WizardOpen
            | Control::NodesProtocolAction(..)
            | Control::NodesSetup
            | Control::NodesBootstrap
            | Control::NodesPage(..) => return self.activate_nodes(control),
            Control::QuickProfile
            | Control::CycleProfile(..)
            | Control::Settings
            | Control::SettingsPage(..)
            | Control::Documentation(..)
            | Control::Accent(..)
            | Control::AccentInput
            | Control::SaveAccent
            | Control::DeleteInstallation
            | Control::ConfirmDelete(..)
            | Control::SelfUpdate
            | Control::CheckWorkstationUpdates
            | Control::Installation(..)
            | Control::TransferInstallation
            | Control::UseInstallation
            | Control::EditInstallation
            | Control::NewInstallation
            | Control::RepairMode
            | Control::EditField(..)
            | Control::SaveInstallation
            | Control::CancelEdit => return self.activate_settings(control),
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

            Control::Close | Control::Restart => KeyCode::Enter,
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
