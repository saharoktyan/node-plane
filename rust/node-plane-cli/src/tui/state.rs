//! Shared screen, form, focus and application state.
use super::*;

pub(super) enum Screen {
    Idle,
    Controller,
    NewInstallation,
    QuickStart,
    Form,
    Nodes,
    Temporary,
    Access,
    NodeWizard,
    Settings,
    SettingsPage(SettingsPage),
    DeleteInstallation(uuid::Uuid),
    EditInstallation,
    Confirm,
    Running,
    Finished,
}
#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum SettingsPage {
    Profiles,
    Appearance,
    Updates,
    Security,
    Session,
    About,
    Documentation,
}
impl SettingsPage {
    pub(super) const ALL: [Self; 7] = [
        Self::Profiles,
        Self::Updates,
        Self::Security,
        Self::Session,
        Self::Appearance,
        Self::Documentation,
        Self::About,
    ];
    pub(super) fn label(self) -> &'static str {
        match self {
            Self::Profiles => "Installation profiles",
            Self::Appearance => "Appearance",
            Self::Updates => "Workstation updates",
            Self::Security => "Connection & security",
            Self::Session => "Diagnostics & session",
            Self::About => "About",
            Self::Documentation => "Documentation",
        }
    }
}
impl Screen {
    pub(super) fn is_settings(&self) -> bool {
        matches!(
            self,
            Self::Settings
                | Self::SettingsPage(_)
                | Self::EditInstallation
                | Self::DeleteInstallation(_)
        )
    }
}
#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum ControllerSection {
    Installation,
    Policies,
    Monitoring,
    Removal,
}
impl ControllerSection {
    pub(super) fn label(self) -> &'static str {
        match self {
            Self::Installation => "Basic",
            Self::Policies => "Policies",
            Self::Monitoring => "Monitoring",
            Self::Removal => "Reset & removal",
        }
    }
}
#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Control {
    DiagnosticOverview,
    DiagnosticMenu,
    WindowClose,
    Restart,
    ControllerSection(ControllerSection),
    Controller,
    ControllerScope,
    ControllerPlan(bool),
    ControllerPhrase,
    ControllerConfirm,
    ControllerRefresh,
    ControllerRetry,
    ControllerShutdown,
    ControllerAbort,
    ControllerBack,
    ControllerNew,
    ControllerCheck,
    ControllerUpdates,
    ControllerDefaults,
    ControllerPolicies,
    ControllerAlerts,
    ControllerAudit,
    ControllerAuditFilter(bool),
    ControllerRecoveryHistory,
    ControllerRecoveryInventory(usize),
    ControllerRecoveryPage(usize),
    ControllerRecoveryAction(usize, usize),
    ControllerRecoveryNode(usize),
    ControllerAnnouncement,
    ControllerAnnouncementCompose,
    ControllerAnnouncementInput,
    ControllerAnnouncementPreview,
    ControllerAnnouncementSend,
    ControllerAnnouncementRefresh,
    ControllerHistoryItem(usize),
    ControllerHistoryPage(bool),
    ControllerAlertInterval(u32),
    ControllerAlertToggle(&'static str),
    ControllerAlertDismiss(usize),
    ControllerAlertPage(bool),
    ControllerGateMessage,
    ControllerBotTitle,
    ControllerBotTitleEdit,
    ControllerTelegram,
    ControllerTelegramToken,
    ControllerTelegramAdminAdd,
    ControllerTelegramAdminRemove(usize),
    ControllerSshKey,
    ControllerSshKeyCopy,
    ControllerPolicyToggle(&'static str),
    ControllerDefaultsToggle(&'static str),
    ControllerDefaultsPreset,
    ControllerDefaultsSave,
    ControllerDefaultsReset,
    ControllerDefaultsAdvanced,
    ControllerDefaultsMode,
    ControllerDefaultsField(&'static str),
    ControllerDefaultsInput,
    ControllerDefaultsInputApply,
    ControllerTextConfirm,
    ControllerBackups,
    ControllerBackupCreate,
    ControllerBackupAuto,
    ControllerBackupInterval(u32),
    ControllerBackupKeep(u32),
    ControllerBackupCatalog(usize),
    ControllerBackupDetail(usize),
    ControllerBackupRestore,
    ControllerBackupDelete,
    ControllerBackupExport,
    ControllerBackupImport,
    ControllerVersions,
    ControllerComponents,
    ControllerUpdateResult,
    ControllerUpdateDismiss,
    ControllerComponentNode(usize),
    ControllerComponentPage(bool),
    ControllerVersion(usize),
    ControllerVersionsPage(usize),
    ControllerUpdateCheck,
    ControllerUpdateLatest,
    ControllerUpdateBranch,
    ControllerUpdateBranchConfirm(bool),
    ControllerUpdateAuto,
    ControllerUpdateInterval(u32),
    NewInstallationConfirm(bool),
    QuickStartAction,
    QuickStartRefresh,
    Action(Action),
    NodesSearch,
    NodesRegion(usize),
    NodesCard(usize),
    NodesRefresh,
    NodesBack,
    NodesNew,
    WizardChoice(usize),
    WizardNext,
    WizardBack,
    WizardCancel,
    WizardOpen,
    NodesSetup,
    NodesBootstrap,
    NodesProtocolAction(&'static str),
    NodesEdit,
    NodesEditTab(crate::node_editor::Tab),
    NodesEditField(usize),
    NodesEditChoice(usize),
    NodesEditToggle(bool, usize),
    NodesEditSave,
    NodesEditConfirm,
    NodesEditReset,
    NodesEditBack,
    NodesApply,
    NodesDiagnostics,
    NodesDiagnosticPage(bool),
    NodesRecovery(usize, &'static str),
    NodesDiagnosticItem(usize),
    NodesRecoveryConfirm,
    NodesMaintenance,
    NodesMaintenanceSection(crate::node_maintenance::Section),
    NodesRemove(crate::node_maintenance::Mode),
    NodesRemoveConfirm,
    Temporary,
    Access,
    AccessTab(crate::access::Tab),
    AccessItem(usize),
    AccessSearch,
    AccessRefresh,
    AccessBack,
    AccessPage(bool),
    AccessDecision(bool),
    AccessConfirm,
    AccessEdit,
    AccessNew,
    AccessIdentity,
    AccessProvisionNext,
    AccessProvisionSubmit,
    AccessProvisionCancel,
    AccessResultClose,
    AccessWizardNext,
    AccessWizardBack,
    AccessField(usize),
    AccessSave,
    AccessProfileAction(bool),
    AccessProfileConfirm,
    AccessAccountAction(bool),
    AccessAccountConfirm,
    AccessDeviceItem(usize),
    AccessDeviceNew,
    AccessDeviceEdit,
    AccessDeviceName,
    AccessDeviceSave,
    AccessDeviceDelete,
    AccessDeviceConfirm,
    AccessDevicePage(bool),
    AccessExpiryMenu,
    AccessDurationCustom,
    AccessDurationInput,
    AccessDurationApply,
    AccessExpiry(Option<u32>),
    AccessExpiryBack,
    AccessExpiryPage(bool),
    AccessGrants,
    AccessGrant(usize, bool),
    AccessRule(Option<usize>, bool),
    AccessGrantMode(bool),
    AccessGrantPage(bool),
    AccessGrantBack,
    AccessGrantAll(bool),
    NodeTemporary,
    TemporaryChoice(usize),
    TemporaryBack,
    TemporaryNew,
    TemporaryRefresh,
    TemporaryPage(bool),
    TemporaryShow,
    TemporaryCopy,
    TemporarySave,
    TemporaryRevoke,
    TemporaryConfirm,
    NodesPage(bool),
    QuickProfile,
    CycleProfile(bool),
    Settings,
    SettingsPage(SettingsPage),
    Documentation(usize),
    Accent(usize),
    CreateTheme,
    ThemeDelete(usize),
    ThemeField,
    ThemeNext,
    ThemeBack,
    DeleteInstallation,
    ConfirmDelete(bool),
    CheckWorkstationUpdates,
    SelfUpdate,
    Installation(usize),
    UseInstallation,
    TransferInstallation,
    EditInstallation,
    NewInstallation,
    EditField(usize),
    RepairMode,
    SaveInstallation,
    CancelEdit,
    Exit(bool),
    Discard(bool),
    Field(usize),
    Continue,
    Confirm(bool),
    Prompt(bool),
    Administrator(usize),
    Close,
    Page(bool),
}
#[derive(Clone, Copy)]
pub(super) enum ScrollTarget {
    List,
    Result,
    NodeCard,
    NodeDiagnostics,
    Maintenance,
    Temporary,
}

#[derive(Clone, Copy)]
pub(super) struct Scrollbar {
    pub(super) track: Rect,
    pub(super) position: usize,
    pub(super) maximum: usize,
    pub(super) visible: usize,
    pub(super) thumb_start: u16,
    pub(super) thumb_length: u16,
    pub(super) target: ScrollTarget,
}
impl Scrollbar {
    pub(super) fn new(
        track: Rect,
        position: usize,
        maximum: usize,
        visible: usize,
        target: ScrollTarget,
    ) -> Self {
        let height = usize::from(track.height);
        let length = (height * visible.max(1) / (maximum + visible.max(1)))
            .max(1)
            .min(height);
        let position = position.min(maximum);
        let start = ((height - length) * position)
            .checked_div(maximum)
            .unwrap_or(0);
        Self {
            track,
            position,
            maximum,
            visible: visible.max(1),
            thumb_start: start as u16,
            thumb_length: length as u16,
            target,
        }
    }
    pub(super) fn position_at(self, row: u16, grab: u16) -> usize {
        let travel = self.track.height.saturating_sub(self.thumb_length);
        let offset = row
            .saturating_sub(self.track.y)
            .saturating_sub(grab)
            .min(travel);
        if travel == 0 {
            0
        } else {
            (usize::from(offset) * self.maximum + usize::from(travel) / 2) / usize::from(travel)
        }
    }
}

pub(super) struct Hit {
    pub(super) area: Rect,
    pub(super) control: Control,
}
pub(super) struct Form {
    pub(super) action: Action,
    pub(super) diagnostic_menu: bool,
    pub(super) fields: Vec<String>,
    pub(super) selected: usize,
    pub(super) saved: Option<Installation>,
}
impl Form {
    pub(super) fn new(r: &Request) -> Self {
        Self {
            action: r.action,
            diagnostic_menu: false,
            fields: vec![
                r.host.clone(),
                r.port.to_string(),
                r.user.clone(),
                r.branch.clone(),
                r.tag.clone(),
                r.admin_ids.clone(),
                String::new(),
                r.workflow.account.clone(),
                r.workflow.target_host.clone(),
                r.workflow.target_port.to_string(),
                r.workflow.target_user.clone(),
                r.workflow.repair.to_string(),
            ],
            selected: 0,
            saved: None,
        }
    }
    pub(super) fn count(&self) -> usize {
        if self.action == Action::Diagnose && self.diagnostic_menu {
            3
        } else {
            self.visible().len() + 2
        }
    }
    pub(super) fn visible(&self) -> Vec<usize> {
        if self.action == Action::Diagnose && self.diagnostic_menu {
            return Vec::new();
        }
        let all: &[usize] = match self.action {
            Action::Install => &[0, 1, 2, 3, 6, 5, 4],
            Action::Diagnose => &[0, 1, 2, 11],
            Action::Update => &[0, 1, 2, 3, 4],
            Action::PrepareNode => &[0, 1, 2, 8, 9, 10],
        };
        all.iter()
            .copied()
            .filter(|&i| {
                self.saved.as_ref().is_none_or(|profile| match i {
                    0 => profile.host.is_empty(),
                    1 => false,
                    2 => profile.user.is_empty(),
                    3 => profile.branch.is_empty(),
                    5 => profile.admin_ids.is_empty(),
                    7 => profile.account.is_empty(),
                    _ => true,
                })
            })
            .collect()
    }
    #[cfg(test)]
    pub(super) fn cycle(&mut self, backwards: bool) {
        let index = Action::ALL.iter().position(|a| *a == self.action).unwrap();
        self.select_action(Action::ALL[(index + if backwards { 3 } else { 1 }) % 4]);
    }
    pub(super) fn select_action(&mut self, action: Action) {
        if action == self.action {
            self.selected = 0;
            return;
        }
        self.action = action;
        self.fields[3] = if let Some(profile) = &self.saved {
            if !profile.branch.is_empty() {
                profile.branch.clone()
            } else if action == Action::Install {
                crate::config::default_branch().into()
            } else {
                String::new()
            }
        } else if self.action == Action::Install {
            crate::config::default_branch().into()
        } else {
            String::new()
        };
        self.selected = 0;
    }
    pub(super) fn next(&mut self) {
        self.selected = (self.selected + 1).min(self.count().saturating_sub(1));
    }
    pub(super) fn previous(&mut self) {
        self.selected = if self.selected == 0 {
            0
        } else {
            self.selected.saturating_sub(1).max(1)
        };
    }
    pub(super) fn apply(&self, r: &mut Request) -> Result<()> {
        if !r.host.eq_ignore_ascii_case(self.fields[0].trim())
            || r.port.to_string() != self.fields[1]
            || r.user != self.fields[2]
        {
            r.workflow.operation = None;
        }
        r.action = self.action;
        r.workflow.repair = self.fields.get(11).is_some_and(|v| v == "true");
        r.host = self.fields[0].trim().into();
        r.port = self.fields[1].parse()?;
        r.user = self.fields[2].trim().into();
        r.branch = self.fields[3].trim().into();
        r.tag = self.fields[4].trim().into();
        r.admin_ids = self.fields[5].trim().into();
        r.bot_token = Zeroizing::new(self.fields[6].clone());
        r.workflow.account = self.fields[7].trim().into();
        r.workflow.target_host = self.fields[8].trim().into();
        r.workflow.target_port = self.fields[9].parse()?;
        r.workflow.target_user = self.fields[10].trim().into();
        r.validate()
    }
    pub(super) fn installation(&self) -> Installation {
        Installation {
            installed: self.saved.as_ref().is_some_and(|p| p.installed),
            id: self
                .saved
                .as_ref()
                .map_or_else(uuid::Uuid::new_v4, |p| p.id),
            name: self
                .saved
                .as_ref()
                .map_or_else(|| self.fields[0].clone(), |p| p.name.clone()),
            host: self.fields[0].trim().into(),
            port: self.fields[1].parse().unwrap_or(22),
            user: self.fields[2].trim().into(),
            branch: self.fields[3].trim().into(),
            admin_ids: self.fields[5].trim().into(),
            account: self.fields[7].trim().into(),
        }
    }
    pub(super) fn use_installation(&mut self, profile: &Installation) {
        if !profile.matches(
            &self.fields[0],
            self.fields[1].parse().unwrap_or(22),
            &self.fields[2],
        ) {
            self.fields[8].clear();
            self.fields[9] = "22".into();
            self.fields[10] = "root".into();
        }
        self.fields[0] = profile.host.clone();
        self.fields[1] = profile.port.to_string();
        self.fields[2] = profile.user.clone();
        self.fields[3] = if profile.branch.is_empty() && self.action == Action::Install {
            crate::config::default_branch().into()
        } else {
            profile.branch.clone()
        };
        self.fields[5] = profile.admin_ids.clone();
        self.fields[7] = profile.account.clone();
        self.fields[6].zeroize();
        self.fields[4].clear();
        self.saved = Some(profile.clone());
        self.selected = 0;
    }
    pub(super) fn append(&mut self, text: &str) {
        if self.selected > 0 && self.selected <= self.visible().len() {
            let index = self.visible()[self.selected - 1];
            if matches!(index, 3 | 11) {
                return;
            }
            let field = &mut self.fields[index];
            if field.len() + text.len() <= 4096 {
                field.extend(text.chars().filter(|c| !c.is_control()));
            }
        }
    }
}
impl Drop for Form {
    fn drop(&mut self) {
        self.fields[6].zeroize();
    }
}

#[derive(Clone, Copy)]
pub(super) struct ControllerRequestContext {
    pub(super) action: Action,
    pub(super) section: Option<ControllerSection>,
    pub(super) screen: std::mem::Discriminant<Screen>,
    pub(super) revision: u64,
    pub(super) read: bool,
}
impl ControllerRequestContext {
    pub(super) fn is_current(self, app: &App) -> bool {
        self.action == app.form.action
            && self.section == app.controller_section
            && self.screen == std::mem::discriminant(&app.screen)
            && self.revision == app.controller_navigation_revision
    }
}

pub(super) struct App {
    pub(super) manual_awg_original: Option<String>,
    pub(super) text_input: std::cell::RefCell<super::text_input::TextInput>,
    pub(super) focus_history: std::collections::HashMap<String, super::input::history::Bookmark>,
    pub(super) navigation_context: Option<String>,
    pub(super) return_focus: Option<Control>,
    pub(super) clipboard: Option<arboard::Clipboard>,
    pub(super) temporary: crate::temporary::Browser,
    pub(super) temporary_sidebar: bool,
    pub(super) temporary_busy: bool,
    pub(super) nodes: crate::nodes::Browser,
    pub(super) nodes_requested: Option<crate::nodes::Command>,
    pub(super) nodes_busy: bool,
    pub(super) loading_message: String,
    pub(super) nodes_background: bool,
    pub(super) controller_navigation_revision: u64,
    pub(super) controller_request_context: Option<ControllerRequestContext>,
    pub(super) node_wizard: Option<crate::node_wizard::Wizard>,
    pub(super) wizard_loading: bool,
    pub(super) ssh_state: crate::nodes::ConnectionState,
    pub(super) nodes_selected: usize,
    pub(super) action_focus: Option<Control>,
    pub(super) nodes_sidebar: bool,
    pub(super) nodes_updates_origin: bool,
    pub(super) nodes_editor_width: std::cell::Cell<u16>,
    pub(super) nodes_capacity: std::cell::Cell<usize>,
    pub(super) self_release: Option<crate::self_manage::Release>,
    pub(super) self_status: String,
    pub(super) restart_required: bool,
    pub(super) restart_requested: bool,
    pub(super) self_requested: bool,
    pub(super) self_check_requested: bool,
    pub(super) form: Form,
    pub(super) screen: Screen,
    pub(super) quick_start_return: Option<Screen>,
    pub(super) quick_start_dismiss: Option<mpsc::Receiver<Answer>>,
    pub(super) error: String,
    pub(super) stage: String,
    pub(super) tracker: Tracker,
    pub(super) prompt: Option<(Prompt, mpsc::Sender<Answer>)>,
    pub(super) administrator_selected: usize,
    pub(super) password: Zeroizing<String>,
    pub(super) trust: bool,
    pub(super) outcome: Option<Result<String, String>>,
    pub(super) update: Option<crate::workstation::UpdateSnapshot>,
    pub(super) node_page: usize,
    pub(super) stop: Arc<AtomicBool>,
    pub(super) controller: crate::controller::Browser,
    pub(super) installed_profile: Option<uuid::Uuid>,
    pub(super) fresh_installation: bool,
    pub(super) controller_sidebar: bool,
    pub(super) controller_return_form: bool,
    pub(super) controller_section: Option<ControllerSection>,
    pub(super) requests_settings: bool,
    pub(super) confirmed_installation: Option<uuid::Uuid>,
    pub(super) controller_version: Option<(uuid::Uuid, String)>,
    pub(super) probe_requested: bool,
    pub(super) ignore_nodes_updates: bool,
    pub(super) access: crate::access::Browser,
    pub(super) access_busy: bool,
    pub(super) access_capacity: std::cell::Cell<usize>,
    pub(super) hits: Vec<Hit>,
    pub(super) hovered: Option<Control>,
    pub(super) pointer_active: bool,
    pub(super) pointer_position: Option<ratatui::layout::Position>,
    pub(super) idle_selection: usize,
    pub(super) content_overflow: std::cell::Cell<bool>,
    pub(super) controller_viewport: std::cell::Cell<usize>,
    pub(super) controller_width: std::cell::Cell<u16>,
    pub(super) manual_viewport: std::cell::Cell<Option<usize>>,
    pub(super) list_viewport: std::cell::Cell<usize>,
    pub(super) list_max_viewport: std::cell::Cell<usize>,
    pub(super) scrollbar: std::cell::Cell<Option<Scrollbar>>,
    pub(super) scroll_track: std::cell::Cell<Rect>,
    pub(super) scroll_drag: Option<u16>,
    pub(super) confirm: bool,
    pub(super) connections: Connections,
    pub(super) state_dir: std::path::PathBuf,
    pub(super) session_id: uuid::Uuid,
    pub(super) settings_selected: usize,
    pub(super) settings_profile: usize,
    pub(super) appearance_viewport: std::cell::Cell<usize>,
    pub(super) theme_wizard: Option<theme_wizard::ThemeWizard>,
    pub(super) themes: Vec<theme::Theme>,
    pub(super) editor: Option<(Option<uuid::Uuid>, Vec<String>, usize)>,
    pub(super) profile_copy: Option<(Installation, std::path::PathBuf, mpsc::Receiver<Answer>)>,
    pub(super) exit: bool,
    pub(super) exit_confirm: bool,
    pub(super) discard: Option<Control>,
    pub(super) discard_confirm: bool,
    pub(super) discard_bypass: bool,
    pub(super) quick_focus: bool,
    pub(super) profile_flash: Option<(bool, std::time::Instant)>,
    pub(super) result_scroll: usize,
    pub(super) result_max_scroll: std::cell::Cell<usize>,
    pub(super) result_page_size: std::cell::Cell<usize>,
}
