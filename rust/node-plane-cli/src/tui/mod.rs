mod app;
mod screens;
#[cfg(test)]
mod tests;
mod theme;
mod widgets;

use screens::*;
use theme::SECTION_BORDER;
#[cfg(test)]
use theme::{accent_color, section_border_color};
use widgets::*;

use crate::{
    config::{Action, Request, WorkflowOptions},
    connections::{Connections, Installation},
    events::{Answer, Event, Prompt},
    progress::Tracker,
};
use anyhow::{Result, ensure};
use crossterm::{
    event::{
        self, DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
        Event as TermEvent, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent,
        MouseEventKind,
    },
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use ratatui::{
    Frame, Terminal,
    backend::CrosstermBackend,
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Gauge, Paragraph, Wrap},
};
use std::{
    io,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    time::Duration,
};
use zeroize::{Zeroize, Zeroizing};

fn draw_qr(frame: &mut Frame, area: Rect, image: &image::DynamicImage) {
    // qr_image uses six pixels per module, including its four-module quiet zone.
    // Preserve every module; averaging or downsampling makes QR codes unreadable.
    let pixels = image.to_luma8();
    let side = (pixels.width() / 6) as u16;
    let height = side.div_ceil(2);
    if area.width < side || area.height < height {
        frame.render_widget(Paragraph::new(format!(
            "Enlarge the terminal to show the QR code ({side} columns × {height} rows required)."
        )).wrap(Wrap { trim: false }), area);
        return;
    }
    for row in 0..height {
        let spans: Vec<Span> = (0..side)
            .map(|column| {
                let color = |module_row: u16| {
                    if module_row < side
                        && pixels.get_pixel(u32::from(column) * 6, u32::from(module_row) * 6)[0]
                            == 0
                    {
                        Color::Black
                    } else {
                        Color::White
                    }
                };
                Span::styled(
                    "▀",
                    Style::default().fg(color(row * 2)).bg(color(row * 2 + 1)),
                )
            })
            .collect();
        frame.render_widget(
            Paragraph::new(Line::from(spans)),
            Rect::new(area.x, area.y + row, side, 1),
        );
    }
}
enum Screen {
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
enum SettingsPage {
    Profiles,
    Appearance,
    Updates,
    Security,
    Session,
    About,
}
impl SettingsPage {
    const ALL: [Self; 6] = [
        Self::Profiles,
        Self::Updates,
        Self::Security,
        Self::Session,
        Self::Appearance,
        Self::About,
    ];
    fn label(self) -> &'static str {
        match self {
            Self::Profiles => "Installation profiles",
            Self::Appearance => "Appearance",
            Self::Updates => "Workstation updates",
            Self::Security => "Connection & security",
            Self::Session => "Diagnostics & session",
            Self::About => "About",
        }
    }
}
impl Screen {
    fn is_settings(&self) -> bool {
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
enum ControllerSection {
    Installation,
    Policies,
    Monitoring,
    Removal,
}
impl ControllerSection {
    fn label(self) -> &'static str {
        match self {
            Self::Installation => "Installation",
            Self::Policies => "Policies",
            Self::Monitoring => "Monitoring",
            Self::Removal => "Reset & removal",
        }
    }
}
#[derive(Clone, Copy, PartialEq, Eq)]
enum Control {
    WindowClose,
    ControllerSection(ControllerSection),
    Controller,
    ControllerScope,
    ControllerPlan(bool),
    ControllerPhrase,
    ControllerConfirm,
    ControllerRefresh,
    ControllerRetry,
    ControllerShutdown,
    ControllerBack,
    ControllerNew,
    ControllerCheck,
    ControllerUpdates,
    ControllerDefaults,
    ControllerPolicies,
    ControllerAlerts,
    ControllerAudit,
    ControllerRecoveryHistory,
    ControllerAnnouncement,
    ControllerAnnouncementCompose,
    ControllerAnnouncementInput,
    ControllerAnnouncementPreview,
    ControllerAnnouncementEdit,
    ControllerAnnouncementSend,
    ControllerAnnouncementRefresh,
    ControllerHistoryItem(usize),
    ControllerHistoryPage(bool),
    ControllerAlertToggle(&'static str),
    ControllerAlertDismiss(usize),
    ControllerAlertPage(bool),
    ControllerGateMessage,
    ControllerBotTitle,
    ControllerBotTitleEdit,
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
    ControllerBackups,
    ControllerBackupCreate,
    ControllerBackupAuto,
    ControllerBackupInterval,
    ControllerBackupKeep,
    ControllerBackupCatalog(usize),
    ControllerBackupDetail(usize),
    ControllerBackupRestore,
    ControllerBackupDelete,
    ControllerVersions,
    ControllerComponents,
    ControllerUpdateResult,
    ControllerUpdateDismiss,
    ControllerComponentNode(usize),
    ControllerComponentPage(bool),
    ControllerVersion(usize),
    ControllerVersionsPage(usize),
    ControllerUpdateCheck,
    ControllerUpdateBranch,
    ControllerUpdateBranchConfirm(bool),
    ControllerUpdateAuto,
    ControllerUpdateInterval,
    NewInstallationConfirm(bool),
    QuickStartToggle,
    QuickStartClose,
    QuickStartAction,
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
    NodesEditPage(bool),
    NodesEditSave,
    NodesEditConfirm,
    NodesEditReset,
    NodesEditBack,
    NodesApply,
    NodesDiagnostics,
    NodesDiagnosticPage(bool),
    NodesRecovery(usize, bool),
    NodesDiagnosticItem(usize),
    NodesRecoveryConfirm,
    NodesMaintenance,
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
    AccessField(usize),
    AccessSave,
    AccessProfileAction(bool),
    AccessProfileConfirm,
    AccessAccountAction(bool),
    AccessAccountConfirm,
    AccessDevices,
    AccessDeviceItem(usize),
    AccessDeviceNew,
    AccessDeviceEdit,
    AccessDeviceName,
    AccessDeviceSave,
    AccessDeviceDelete,
    AccessDeviceConfirm,
    AccessDevicePage(bool),
    AccessExpiryMenu,
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
    TemporaryQr,
    TemporarySave,
    TemporaryRevoke,
    TemporaryConfirm,
    NodesPage(bool),
    QuickProfile,
    CycleProfile(bool),
    Settings,
    SettingsPage(SettingsPage),
    Accent(usize),
    AccentInput,
    SaveAccent,
    DeleteInstallation,
    ConfirmDelete(bool),
    CheckWorkstationUpdates,
    SelfUpdate,
    Installation(usize),
    UseInstallation,
    EditInstallation,
    NewInstallation,
    EditField(usize),
    Channel(bool, bool),
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
enum ScrollTarget {
    List,
    Result,
    NodeCard,
    Maintenance,
    Temporary,
}

#[derive(Clone, Copy)]
struct Scrollbar {
    track: Rect,
    position: usize,
    maximum: usize,
    visible: usize,
    thumb_start: u16,
    thumb_length: u16,
    target: ScrollTarget,
}
impl Scrollbar {
    fn new(
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
    fn position_at(self, row: u16, grab: u16) -> usize {
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

struct Hit {
    area: Rect,
    control: Control,
}
struct Form {
    action: Action,
    fields: Vec<String>,
    selected: usize,
    saved: Option<Installation>,
}
impl Form {
    fn new(r: &Request) -> Self {
        Self {
            action: r.action,
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
    fn count(&self) -> usize {
        self.visible().len() + 2
    }
    fn visible(&self) -> Vec<usize> {
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
    fn cycle(&mut self, backwards: bool) {
        let index = Action::ALL.iter().position(|a| *a == self.action).unwrap();
        self.select_action(Action::ALL[(index + if backwards { 3 } else { 1 }) % 4]);
    }
    fn select_action(&mut self, action: Action) {
        if action == self.action {
            self.selected = 0;
            return;
        }
        self.action = action;
        self.fields[3] = if let Some(profile) = &self.saved {
            if !profile.branch.is_empty() {
                profile.branch.clone()
            } else if action == Action::Install {
                "dev".into()
            } else {
                String::new()
            }
        } else if self.action == Action::Install {
            "dev".into()
        } else {
            String::new()
        };
        self.selected = 0;
    }
    fn next(&mut self) {
        self.selected = (self.selected + 1).min(self.count().saturating_sub(1));
    }
    fn previous(&mut self) {
        self.selected = if self.selected == 0 {
            0
        } else {
            self.selected.saturating_sub(1).max(1)
        };
    }
    fn apply(&self, r: &mut Request) -> Result<()> {
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
    fn installation(&self) -> Installation {
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
    fn use_installation(&mut self, profile: &Installation) {
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
            "dev".into()
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
    fn append(&mut self, text: &str) {
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

struct App {
    image_picker: Option<ratatui_image::picker::Picker>,
    qr: std::cell::RefCell<Option<image::DynamicImage>>,
    qr_graphic: std::cell::RefCell<Option<ratatui_image::protocol::StatefulProtocol>>,
    clipboard: Option<arboard::Clipboard>,
    qr_visible: bool,
    temporary: crate::temporary::Browser,
    temporary_sidebar: bool,
    temporary_busy: bool,
    nodes: crate::nodes::Browser,
    nodes_requested: Option<crate::nodes::Command>,
    nodes_busy: bool,
    loading_message: String,
    nodes_background: bool,
    node_wizard: Option<crate::node_wizard::Wizard>,
    wizard_loading: bool,
    ssh_state: crate::nodes::ConnectionState,
    nodes_selected: usize,
    action_focus: Option<Control>,
    nodes_sidebar: bool,
    nodes_updates_origin: bool,
    nodes_capacity: std::cell::Cell<usize>,
    self_release: Option<crate::self_manage::Release>,
    self_status: String,
    self_requested: bool,
    self_check_requested: bool,
    form: Form,
    screen: Screen,
    error: String,
    stage: String,
    tracker: Tracker,
    prompt: Option<(Prompt, mpsc::Sender<Answer>)>,
    administrator_selected: usize,
    password: Zeroizing<String>,
    trust: bool,
    outcome: Option<Result<String, String>>,
    update: Option<crate::workstation::UpdateSnapshot>,
    node_page: usize,
    stop: Arc<AtomicBool>,
    controller: crate::controller::Browser,
    installed_profile: Option<uuid::Uuid>,
    fresh_installation: bool,
    controller_sidebar: bool,
    controller_section: Option<ControllerSection>,
    requests_settings: bool,
    confirmed_installation: Option<uuid::Uuid>,
    probe_requested: bool,
    ignore_nodes_updates: bool,
    access: crate::access::Browser,
    access_busy: bool,
    access_capacity: std::cell::Cell<usize>,
    hits: Vec<Hit>,
    hovered: Option<Control>,
    pointer_active: bool,
    pointer_position: Option<ratatui::layout::Position>,
    idle_selection: usize,
    content_overflow: std::cell::Cell<bool>,
    controller_viewport: std::cell::Cell<usize>,
    controller_width: std::cell::Cell<u16>,
    manual_viewport: std::cell::Cell<Option<usize>>,
    list_viewport: std::cell::Cell<usize>,
    list_max_viewport: std::cell::Cell<usize>,
    scrollbar: std::cell::Cell<Option<Scrollbar>>,
    scroll_track: std::cell::Cell<Rect>,
    scroll_drag: Option<u16>,
    confirm: bool,
    connections: Connections,
    state_dir: std::path::PathBuf,
    session_id: uuid::Uuid,
    settings_selected: usize,
    settings_profile: usize,
    accent_input: String,
    themes: Vec<theme::Theme>,
    editor: Option<(Option<uuid::Uuid>, Vec<String>, usize)>,
    exit: bool,
    exit_confirm: bool,
    discard: Option<Control>,
    discard_confirm: bool,
    discard_bypass: bool,
    quick_focus: bool,
    result_scroll: usize,
    result_max_scroll: std::cell::Cell<usize>,
    result_page_size: std::cell::Cell<usize>,
}
struct TerminalGuard;
impl TerminalGuard {
    fn enter() -> Result<Self> {
        enable_raw_mode()?;
        if let Err(error) = execute!(
            io::stdout(),
            EnterAlternateScreen,
            EnableBracketedPaste,
            EnableMouseCapture
        ) {
            let _ = execute!(
                io::stdout(),
                DisableMouseCapture,
                DisableBracketedPaste,
                LeaveAlternateScreen
            );
            let _ = disable_raw_mode();
            return Err(error.into());
        }
        let previous = std::panic::take_hook();
        std::panic::set_hook(Box::new(move |info| {
            let _ = disable_raw_mode();
            let _ = execute!(
                io::stdout(),
                DisableMouseCapture,
                DisableBracketedPaste,
                LeaveAlternateScreen
            );
            previous(info);
        }));
        Ok(Self)
    }
}
impl Drop for TerminalGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(
            io::stdout(),
            DisableMouseCapture,
            DisableBracketedPaste,
            LeaveAlternateScreen
        );
    }
}

pub fn run(mut request: Request) -> Result<()> {
    let guard = TerminalGuard::enter()?;
    let mut terminal = Terminal::new(CrosstermBackend::new(io::stdout()))?;
    let mut app = App::new(&request);
    // Terminal graphics can truncate the image or erase surrounding cells in
    // Kitty and Windows Terminal. Keep QR disabled until rendering is reliable.
    // Do not probe image capabilities: copy/save work in every terminal.
    app.load_connections(&request)?;
    app.screen = Screen::Idle;
    let mut receiver: Option<mpsc::Receiver<Event>> = None;
    let mut worker = None;
    let mut nodes_worker = crate::nodes::Worker::new()?;
    let mut nodes_profile = None;
    let (check_tx, check_rx) = mpsc::channel();
    let startup_tx = check_tx.clone();
    std::thread::spawn(move || {
        let _ = startup_tx.send(crate::self_manage::discover());
    });
    let mut checking = true;
    let mut offer = false;
    let mut self_confirmation: Option<mpsc::Receiver<Answer>> = None;
    let mut self_updating = false;
    let mut last_controller_poll = std::time::Instant::now();
    let mut last_access_poll = std::time::Instant::now();
    let mut last_node_observation = std::time::Instant::now();
    let mut qr_was_visible = false;
    let mut qr_needs_redraw = false;
    let mut previous_accent = app.connections.accent.clone();
    let mut pending_input = None;
    loop {
        let selected = app.form.saved.as_ref().map(|p| {
            (
                p.id,
                p.host.clone(),
                p.port,
                p.user.clone(),
                p.account.clone(),
            )
        });
        if selected != nodes_profile {
            nodes_worker.reset();
            nodes_profile = selected;
            app.confirmed_installation = None;
            app.ignore_nodes_updates = app.nodes_busy;
            app.nodes_requested = None;
            app.probe_requested = app.form.saved.is_some();
            app.controller = crate::controller::Browser {
                profile: app.form.saved.as_ref().map(|p| p.id),
                ..Default::default()
            };
            app.nodes = crate::nodes::Browser::default();
            app.access = crate::access::Browser::default();
            app.temporary = crate::temporary::Browser::default();
        }
        if app.probe_requested && !app.nodes_busy && app.nodes_requested.is_none() {
            app.probe_requested = false;
            app.controller_command(crate::controller::Command::Probe);
        }
        app.ssh_state = nodes_worker.state();
        if matches!(app.screen, Screen::Temporary)
            && app.temporary.view == crate::temporary::View::List
            && app.temporary.loaded_size > 0
            && app.temporary.loaded_size != app.temporary.capacity.get().clamp(1, 100)
            && !app.nodes_busy
            && app.nodes_requested.is_none()
        {
            app.temporary.page = 0;
            app.temporary_command(app.temporary.list());
        }
        if matches!(app.screen, Screen::Nodes)
            && !app.nodes_busy
            && app.nodes_requested.is_none()
            && !app.nodes_sidebar
            && app.prompt.is_none()
            && !app.exit
            && last_node_observation.elapsed() >= Duration::from_secs(3)
            && app.ssh_state == crate::nodes::ConnectionState::Connected
            && let Some(node) = &app.nodes.card
            && app.nodes.maintenance.is_none()
            && node.needs_observation(app.nodes.operation.as_ref())
        {
            app.nodes_requested = Some(crate::nodes::Command::Observe(
                node.clone(),
                app.nodes
                    .operation
                    .clone()
                    .filter(|operation| operation.node_key == node.key),
            ));
            last_node_observation = std::time::Instant::now();
        }
        if matches!(app.screen, Screen::Nodes)
            && !app.nodes_busy
            && app.nodes_requested.is_none()
            && app.prompt.is_none()
            && !app.exit
            && last_node_observation.elapsed() >= Duration::from_secs(3)
            && app.nodes.diagnostics.is_none()
            && let Some(view) = &app.nodes.maintenance
            && view.active()
        {
            app.nodes_requested = Some(crate::nodes::Command::MaintenancePoll(view.key.clone()));
            last_node_observation = std::time::Instant::now();
        }
        if !app.nodes_busy
            && app.nodes_requested.is_none()
            && matches!(app.screen, Screen::Controller)
            && last_controller_poll.elapsed() >= std::time::Duration::from_secs(3)
        {
            last_controller_poll = std::time::Instant::now();
            if let Some(command) = app.controller.poll() {
                app.controller_command(command);
            }
        }
        if !app.nodes_busy
            && app.nodes_requested.is_none()
            && matches!(app.screen, Screen::Access)
            && last_access_poll.elapsed() >= std::time::Duration::from_secs(3)
        {
            last_access_poll = std::time::Instant::now();
            if let Some(command) = app.access.poll() {
                app.access_command(command);
            }
        }
        if !app.nodes_busy
            && let Some(command) = app.nodes_requested.take()
        {
            let mut next = app.next_request();
            next.action = Action::Diagnose;
            next.host = app.form.fields[0].clone();
            next.port = app.form.fields[1].parse().unwrap_or(0);
            next.user = app.form.fields[2].clone();
            next.workflow.account = app.form.fields[7].clone();
            match next.validate() {
                Ok(()) => {
                    // Browsing has no target-node fields and never runs the old preparation workflow.
                    next.action = Action::Diagnose;
                    let (tx, rx) = mpsc::channel();
                    let access_command = matches!(command, crate::nodes::Command::Access(_));
                    let temporary_command = matches!(command, crate::nodes::Command::Temporary(_));
                    let observing = matches!(
                        command,
                        crate::nodes::Command::Observe(..)
                            | crate::nodes::Command::MaintenancePoll(_)
                            | crate::nodes::Command::Controller(..)
                            | crate::nodes::Command::Access(
                                crate::access::Command::ProfileProgress(_)
                            )
                    );
                    app.stage.clear();
                    app.loading_message = match &command {
                        crate::nodes::Command::Controller(_, crate::controller::Command::Probe) => {
                            "Loading installation…"
                        }
                        crate::nodes::Command::Access(_) => "Loading access…",
                        crate::nodes::Command::Temporary(_) => "Loading temporary configs…",
                        crate::nodes::Command::List => "Loading nodes…",
                        crate::nodes::Command::Card(_) => "Loading server…",
                        crate::nodes::Command::CreationOptions => "Loading node options…",
                        crate::nodes::Command::EditContext(_) => "Loading server settings…",
                        crate::nodes::Command::Diagnostics(_, _) => "Loading diagnostics…",
                        crate::nodes::Command::Maintenance(_, _) => "Loading maintenance…",
                        _ => "Processing your request…",
                    }
                    .into();
                    nodes_worker.submit(next, command, tx)?;
                    receiver = Some(rx);
                    app.nodes_busy = true;
                    app.nodes_background = observing;
                    app.temporary_busy = temporary_command;
                    app.access_busy = access_command;
                    if !app.nodes_background {
                        app.screen = Screen::Running;
                    }
                    app.error.clear();
                    app.update = None;
                    app.tracker = Tracker::default();
                }
                Err(error) => app.error = error.to_string(),
            }
        }
        if app.self_check_requested {
            app.self_check_requested = false;
            if !checking {
                checking = true;
                app.self_status = "Checking for workstation updates…".into();
                let tx = check_tx.clone();
                std::thread::spawn(move || {
                    let _ = tx.send(crate::self_manage::discover());
                });
            }
        }
        if let Ok(result) = check_rx.try_recv() {
            checking = false;
            match result {
                Ok(release) => {
                    app.self_status = release
                        .as_ref()
                        .map(|r| format!("Available: {}", r.tag_name))
                        .unwrap_or_else(|| "Workstation is up to date.".into());
                    offer = release.is_some();
                    app.self_release = release;
                }
                Err(error) => app.self_status = format!("Update check unavailable: {error}"),
            }
        }
        if (offer || app.self_requested)
            && app.prompt.is_none()
            && !app.exit
            && matches!(
                app.screen,
                Screen::Form | Screen::Settings | Screen::SettingsPage(SettingsPage::Updates)
            )
        {
            offer = false;
            app.self_requested = false;
            if let Some(release) = &app.self_release {
                let (reply, answer) = mpsc::channel();
                app.prompt = Some((
                    Prompt::ConfirmAction {
                        title: "Update workstation?".into(),
                        description: format!(
                            "{} → {}\nWorkstation is updated on this computer. Restart TUI afterwards. Server installations and saved profiles are retained.",
                            env!("CARGO_PKG_VERSION"),
                            release.tag_name
                        ),
                    },
                    reply,
                ));
                app.trust = false;
                self_confirmation = Some(answer);
            }
        }
        if let Some(answer) = &self_confirmation
            && let Ok(value) = answer.try_recv()
        {
            self_confirmation = None;
            if matches!(value, Answer::Confirm(true))
                && let Some(release) = app.self_release.clone()
            {
                let (tx, rx) = mpsc::channel();
                receiver = Some(rx);
                app.stage = "Downloading and verifying workstation update".into();
                app.screen = Screen::Running;
                app.tracker = Tracker::default();
                app.update = None;
                worker = Some(std::thread::spawn(move || {
                    let result = crate::self_manage::update(&release).map_err(|e| format!("{e:#}"));
                    let _ = tx.send(Event::Finished(result));
                }));
                self_updating = true;
                app.self_status = "Updating workstation…".into();
            }
        }
        let mut finished = false;
        if let Some(rx) = &receiver {
            loop {
                match rx.try_recv() {
                    Ok(value) => {
                        if self_updating && let Event::Finished(result) = &value {
                            self_updating = false;
                            if result.is_ok() {
                                app.self_release = None;
                                app.self_status =
                                    "Updated; restart TUI to use the new version.".into();
                            } else {
                                app.self_status =
                                    "Workstation update failed; retry from Settings.".into();
                            }
                        }
                        finished |= matches!(&value, Event::Finished(_));
                        app.event(value);
                    }
                    Err(mpsc::TryRecvError::Empty) => break,
                    Err(mpsc::TryRecvError::Disconnected) => {
                        if !finished && !matches!(app.screen, Screen::Finished) {
                            app.event(Event::Finished(Err("Operation worker disconnected. Diagnose the server before retrying.".into())));
                        }
                        finished = true;
                        break;
                    }
                }
            }
        }
        if finished {
            receiver = None;
        }
        if qr_was_visible != app.qr_visible || previous_accent != app.connections.accent {
            terminal.clear()?;
            qr_needs_redraw = true;
            qr_was_visible = app.qr_visible;
            previous_accent = app.connections.accent.clone();
        }
        // Graphics persist outside the cell buffer. Sending the same image on
        // every idle tick clears and uploads it again, causing visible flicker.
        if !app.qr_visible || qr_needs_redraw {
            terminal.draw(|frame| app.hits = draw(frame, &app))?;
            if app.refresh_scroll_hover() || app.restore_action_focus() {
                terminal.draw(|frame| app.hits = draw(frame, &app))?;
            }
            qr_needs_redraw = false;
        }
        if pending_input.is_none() && !event::poll(Duration::from_millis(100))? {
            continue;
        }
        nodes_worker.touch();
        app.action_focus = None;
        let mut input = match pending_input.take() {
            Some(input) => input,
            None => event::read()?,
        };
        if matches!(input, TermEvent::Mouse(mouse) if mouse.kind == MouseEventKind::Moved) {
            // Keep only the latest queued pointer position. Preserve clicks,
            // keys and resize events in order instead of consuming them here.
            for _ in 0..256 {
                if !event::poll(Duration::ZERO)? {
                    break;
                }
                let next = event::read()?;
                if matches!(next, TermEvent::Mouse(mouse) if mouse.kind == MouseEventKind::Moved) {
                    input = next;
                } else {
                    pending_input = Some(next);
                    break;
                }
            }
        }
        if terminal_too_small(terminal.size()?.into()) && !app.exit {
            app.hovered = None;
            app.scroll_drag = None;
            match input {
                TermEvent::Key(key)
                    if key.kind == KeyEventKind::Press && key.code == KeyCode::Esc =>
                {
                    app.exit = true;
                    app.exit_confirm = false;
                }
                TermEvent::Resize(_, _) => {
                    terminal.clear()?;
                    qr_needs_redraw = true;
                }
                _ => {}
            }
            continue;
        }
        let input = match input {
            TermEvent::Mouse(mouse) => match app.mouse(mouse) {
                Some(key) => TermEvent::Key(key),
                None => continue,
            },
            TermEvent::Key(key) if key.kind == KeyEventKind::Press => {
                app.pointer_active = false;
                if matches!(
                    key.code,
                    KeyCode::Up
                        | KeyCode::Down
                        | KeyCode::Left
                        | KeyCode::Right
                        | KeyCode::Tab
                        | KeyCode::BackTab
                        | KeyCode::Home
                        | KeyCode::End
                        | KeyCode::Esc
                ) {
                    app.manual_viewport.set(None);
                }
                let hovered = app.hovered.take();
                if key.code == KeyCode::Enter
                    && let Some(control) = hovered
                {
                    match app.activate(control) {
                        Some(key) => TermEvent::Key(key),
                        None => continue,
                    }
                } else {
                    TermEvent::Key(key)
                }
            }
            input => input,
        };
        match input {
            TermEvent::Resize(_, _) => {
                // Image protocols draw outside ratatui's cell diff. Invalidate
                // both buffers so resizing repaints every cell around the QR.
                terminal.clear()?;
                qr_needs_redraw = true;
            }
            TermEvent::Paste(text) => {
                if matches!(&app.prompt, Some((Prompt::Password { .. }, _))) {
                    if app.password.len() + text.len() <= 4096 {
                        app.password
                            .extend(text.chars().filter(|c| !c.is_control()));
                    }
                } else if matches!(app.screen, Screen::SettingsPage(SettingsPage::Appearance))
                    && !app.exit
                    && app.settings_selected == app.themes.len() + 1
                {
                    app.accent_input.extend(
                        text.chars()
                            .filter(|c| *c == '#' || c.is_ascii_hexdigit())
                            .take(7usize.saturating_sub(app.accent_input.len())),
                    );
                } else if matches!(app.screen, Screen::EditInstallation) && !app.exit {
                    if let Some((_, fields, selected)) = &mut app.editor
                        && *selected < fields.len()
                        && *selected != 4
                        && fields[*selected].len() + text.len() <= 4096
                    {
                        fields[*selected].extend(text.chars().filter(|c| !c.is_control()));
                    }
                } else if matches!(app.screen, Screen::Nodes)
                    && !app.exit
                    && app.nodes_selected == 0
                    && app.nodes.card.is_none()
                {
                    let remaining = 128usize.saturating_sub(app.nodes.search.chars().count());
                    app.nodes
                        .search
                        .extend(text.chars().filter(|c| !c.is_control()).take(remaining));
                    app.nodes.page = 0;
                } else if matches!(app.screen, Screen::NodeWizard) && !app.exit {
                    if let Some(wizard) = &mut app.node_wizard {
                        wizard.append(&text);
                    }
                } else if matches!(app.screen, Screen::Controller) && !app.exit {
                    if app.controller_controls().get(app.controller.selected)
                        == Some(&Control::ControllerPhrase)
                    {
                        let remaining =
                            64usize.saturating_sub(app.controller.phrase.chars().count());
                        app.controller
                            .phrase
                            .extend(text.chars().filter(|c| !c.is_control()).take(remaining));
                    } else if app.controller_controls().get(app.controller.selected)
                        == Some(&Control::ControllerDefaultsInput)
                        && let Some((_, value)) = &mut app.controller.defaults_input
                    {
                        let remaining = 500usize.saturating_sub(value.chars().count());
                        value.extend(text.chars().filter(|c| !c.is_control()).take(remaining));
                    }
                } else if matches!(app.screen, Screen::Access) && !app.exit {
                    if let Some(Control::AccessField(index)) =
                        app.access_controls().get(app.access.selected).copied()
                    {
                        if let Some(editor) = &mut app.access.editor {
                            let remaining =
                                128usize.saturating_sub(editor.fields[index].chars().count());
                            editor.fields[index]
                                .extend(text.chars().filter(|c| !c.is_control()).take(remaining));
                            editor.key = uuid::Uuid::new_v4();
                        }
                    } else if app.access_controls().get(app.access.selected)
                        == Some(&Control::AccessDeviceName)
                    {
                        if let Some(editor) =
                            app.access.devices.as_mut().and_then(|d| d.editor.as_mut())
                        {
                            let remaining = 64usize.saturating_sub(editor.name.chars().count());
                            editor
                                .name
                                .extend(text.chars().filter(|c| !c.is_control()).take(remaining));
                            editor.key = uuid::Uuid::new_v4();
                        }
                    } else if app.access_controls().get(app.access.selected)
                        == Some(&Control::AccessSearch)
                    {
                        let remaining = 128usize.saturating_sub(app.access.search.chars().count());
                        app.access
                            .search
                            .extend(text.chars().filter(|c| !c.is_control()).take(remaining));
                        app.access.page = 0;
                    }
                } else if matches!(app.screen, Screen::Nodes) && !app.exit {
                    if let Some(Control::NodesEditField(index)) =
                        app.nodes_controls().get(app.nodes_selected).copied()
                        && let Some(editor) = &mut app.nodes.editor
                    {
                        let remaining =
                            255usize.saturating_sub(editor.fields[index].value.chars().count());
                        editor.fields[index]
                            .value
                            .extend(text.chars().filter(|c| !c.is_control()).take(remaining));
                        editor.changed();
                    }
                } else if matches!(app.screen, Screen::Form) && !app.exit {
                    app.form.append(&text);
                }
            }
            TermEvent::Key(key) if key.kind == KeyEventKind::Press => {
                if app.discard.is_some() && !app.exit {
                    match key.code {
                        KeyCode::Left | KeyCode::Right | KeyCode::Tab | KeyCode::BackTab => {
                            app.discard_confirm = !app.discard_confirm
                        }
                        KeyCode::Esc => {
                            app.activate(Control::Discard(false));
                        }
                        KeyCode::Enter => {
                            app.activate(Control::Discard(app.discard_confirm));
                        }
                        _ => {}
                    }
                    continue;
                }
                if app.prompt.is_some() && !app.exit {
                    app.prompt_key(key);
                    continue;
                }
                if app.exit {
                    match key.code {
                        KeyCode::Tab | KeyCode::Left | KeyCode::Right => {
                            app.exit_confirm = !app.exit_confirm
                        }
                        KeyCode::Esc | KeyCode::Char('n') => app.cancel_exit(),
                        KeyCode::Enter if !app.exit_confirm => app.cancel_exit(),
                        KeyCode::Enter | KeyCode::Char('y') => {
                            app.stop.store(true, Ordering::Relaxed);
                            break;
                        }
                        _ => {}
                    }
                    continue;
                }
                if app.qr_visible {
                    app.temporary_key(key);
                    continue;
                }
                if key.code == KeyCode::F(1) {
                    app.screen = Screen::QuickStart;
                    continue;
                }
                if matches!(app.screen, Screen::Idle) {
                    app.idle_key(key);
                    continue;
                }
                if app.section_navigation(key.code) {
                    continue;
                }
                if matches!(app.screen, Screen::Access) {
                    app.access_key(key);
                    continue;
                }
                if app.section_navigation(key.code) {
                    continue;
                }
                if !key.modifiers.contains(KeyModifiers::CONTROL) && app.move_spatial(key.code) {
                    continue;
                }
                let quit = key.code == KeyCode::Esc
                    || (key.code == KeyCode::Char('c')
                        && key.modifiers.contains(KeyModifiers::CONTROL));
                if app.quick_focus && matches!(app.screen, Screen::Form) {
                    match key.code {
                        KeyCode::Left | KeyCode::Right => {
                            if let Err(error) = app.cycle_profile(key.code == KeyCode::Left) {
                                app.error = error.to_string();
                            }
                        }
                        KeyCode::Enter => app.activate_profile(),
                        KeyCode::Up | KeyCode::Down => app.navigate(key.code == KeyCode::Up),
                        KeyCode::Tab => {
                            if app.actions_enabled() {
                                app.quick_focus = false;
                                app.form.next();
                            } else {
                                app.open_settings();
                            }
                        }
                        KeyCode::BackTab => app.navigate(true),
                        _ if quit => {
                            app.exit = true;
                            app.exit_confirm = false;
                        }
                        _ => {}
                    }
                    continue;
                }
                match app.screen {
                    Screen::Idle => {}
                    Screen::NewInstallation => match key.code {
                        KeyCode::Left | KeyCode::Right | KeyCode::Tab | KeyCode::BackTab => {
                            app.confirm = !app.confirm
                        }
                        KeyCode::Enter => {
                            app.activate(Control::NewInstallationConfirm(app.confirm));
                        }
                        KeyCode::Esc => {
                            app.activate(Control::NewInstallationConfirm(false));
                        }
                        _ if quit => {
                            app.exit = true;
                            app.exit_confirm = false;
                        }
                        _ => {}
                    },
                    Screen::Controller => app.controller_key(key),
                    Screen::Access => app.access_key(key),
                    Screen::QuickStart => app.quick_start_key(key),
                    Screen::Temporary => app.temporary_key(key),
                    Screen::Nodes => app.nodes_key(key),
                    Screen::NodeWizard => app.wizard_key(key),
                    Screen::SettingsPage(SettingsPage::Updates) => {
                        let controls = if app.self_release.is_some() {
                            vec![
                                Control::CheckWorkstationUpdates,
                                Control::SelfUpdate,
                                Control::Settings,
                            ]
                        } else {
                            vec![Control::CheckWorkstationUpdates, Control::Settings]
                        };
                        match key.code {
                            KeyCode::Esc => app.open_settings(),
                            KeyCode::Left | KeyCode::Right | KeyCode::Tab | KeyCode::BackTab => {
                                let backwards =
                                    matches!(key.code, KeyCode::Left | KeyCode::BackTab);
                                app.settings_selected = (app.settings_selected.saturating_sub(1)
                                    + if backwards { controls.len() - 1 } else { 1 })
                                    % controls.len()
                                    + 1;
                            }
                            KeyCode::Char('u') if app.self_release.is_some() => {
                                app.self_requested = true
                            }
                            KeyCode::Char('c') => app.self_check_requested = true,
                            KeyCode::Enter => {
                                app.activate(
                                    controls[app
                                        .settings_selected
                                        .saturating_sub(1)
                                        .min(controls.len() - 1)],
                                );
                            }
                            _ if quit => {
                                app.exit = true;
                                app.exit_confirm = false;
                            }
                            _ => {}
                        }
                    }
                    Screen::Form => {
                        if app.form.action == Action::Install
                            && app.has_installation()
                            && !app.fresh_installation
                        {
                            app.open_controller();
                            continue;
                        }
                        if !app.actions_enabled()
                            || app.form.action != Action::Install && !app.controller_ready()
                        {
                            app.quick_focus = true;
                            continue;
                        }
                        if quit {
                            if app.form.selected > 0 && key.code == KeyCode::Esc {
                                app.form.selected = 0;
                            } else {
                                app.exit = true;
                                app.exit_confirm = false;
                            }
                            continue;
                        }
                        match key.code {
                            KeyCode::Down | KeyCode::Up if app.form.selected == 0 => {
                                app.navigate(key.code == KeyCode::Up)
                            }
                            KeyCode::Char('m')
                                if app.form.action == Action::Install && app.form.selected == 0 =>
                            {
                                app.open_controller()
                            }
                            KeyCode::Tab | KeyCode::Down => app.form.next(),
                            KeyCode::BackTab | KeyCode::Up => app.form.previous(),
                            KeyCode::Left | KeyCode::Right | KeyCode::Char(' ')
                                if app.form.selected == 0 =>
                            {
                                app.navigate(key.code == KeyCode::Left);
                            }
                            KeyCode::Left | KeyCode::Right
                                if app.form.selected > 0
                                    && app.form.visible().get(app.form.selected - 1)
                                        == Some(&3) =>
                            {
                                cycle_channel(&mut app.form.fields[3], key.code == KeyCode::Left);
                            }
                            KeyCode::Left | KeyCode::Right | KeyCode::Char(' ')
                                if app.form.selected > 0
                                    && app.form.visible().get(app.form.selected - 1)
                                        == Some(&11) =>
                            {
                                toggle_repair(&mut app.form.fields[11]);
                            }
                            KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                                app.form.append(&c.to_string())
                            }
                            KeyCode::Backspace
                                if app.form.selected > 0
                                    && app.form.selected <= app.form.visible().len() =>
                            {
                                let index = app.form.visible()[app.form.selected - 1];
                                if !matches!(index, 3 | 11) {
                                    app.form.fields[index].pop();
                                }
                            }
                            KeyCode::Enter => {
                                if app.form.selected + 1 < app.form.count() {
                                    app.form.next();
                                } else {
                                    match app.form.apply(&mut request) {
                                        Ok(()) => {
                                            app.error.clear();
                                            app.confirm = true;
                                            app.screen = Screen::Confirm;
                                        }
                                        Err(error) => app.error = error.to_string(),
                                    }
                                }
                            }
                            _ => {}
                        }
                    }
                    Screen::SettingsPage(SettingsPage::Appearance) => {
                        if quit {
                            app.exit = true;
                            app.exit_confirm = false;
                        } else if !key.modifiers.contains(KeyModifiers::CONTROL) {
                            app.appearance_key(key.code);
                        }
                    }
                    Screen::SettingsPage(SettingsPage::Session) => match key.code {
                        KeyCode::Esc | KeyCode::Enter => app.open_settings(),
                        _ if quit => {
                            app.exit = true;
                            app.exit_confirm = false;
                        }
                        _ => {}
                    },
                    Screen::Settings => match key.code {
                        KeyCode::Esc if app.settings_selected > 0 => app.settings_selected = 0,
                        KeyCode::Esc => {
                            app.exit = true;
                            app.exit_confirm = false;
                        }
                        KeyCode::Up | KeyCode::Down if app.settings_selected == 0 => {
                            app.navigate(key.code == KeyCode::Up)
                        }
                        KeyCode::Tab | KeyCode::Down => {
                            app.settings_selected =
                                (app.settings_selected + 1).min(SettingsPage::ALL.len())
                        }
                        KeyCode::BackTab | KeyCode::Up => {
                            app.settings_selected = app.settings_selected.saturating_sub(1).max(1)
                        }
                        KeyCode::Enter => {
                            if app.settings_selected == 0 {
                                app.settings_selected = 1;
                            } else {
                                app.activate(Control::SettingsPage(
                                    SettingsPage::ALL[app.settings_selected - 1],
                                ));
                            }
                        }
                        KeyCode::Char('u') => {
                            app.activate(Control::SettingsPage(SettingsPage::Updates));
                        }
                        _ if quit => {
                            app.exit = true;
                            app.exit_confirm = false;
                        }
                        _ => {}
                    },
                    Screen::SettingsPage(SettingsPage::Profiles) => {
                        let controls = app.profile_controls();
                        match key.code {
                            KeyCode::Esc => app.open_settings(),
                            KeyCode::Tab | KeyCode::Down => {
                                app.settings_selected =
                                    (app.settings_selected + 1).min(controls.len())
                            }
                            KeyCode::BackTab | KeyCode::Up => {
                                app.settings_selected =
                                    app.settings_selected.saturating_sub(1).max(1)
                            }
                            KeyCode::Enter => {
                                app.activate(
                                    controls[app
                                        .settings_selected
                                        .saturating_sub(1)
                                        .min(controls.len() - 1)],
                                );
                            }
                            KeyCode::Char('n') => app.begin_edit(true),
                            KeyCode::Char('e') if !app.connections.installations.is_empty() => {
                                app.begin_edit(false)
                            }
                            KeyCode::Delete if !app.connections.installations.is_empty() => {
                                app.activate(Control::DeleteInstallation);
                            }
                            _ if quit => {
                                app.exit = true;
                                app.exit_confirm = false;
                            }
                            _ => {}
                        }
                        if app.settings_selected > 0
                            && app.settings_selected <= app.connections.installations.len()
                        {
                            app.settings_profile = app.settings_selected - 1;
                        }
                    }
                    Screen::SettingsPage(SettingsPage::Security | SettingsPage::About) => {
                        match key.code {
                            KeyCode::Esc | KeyCode::Enter => app.open_settings(),
                            _ if quit => {
                                app.exit = true;
                                app.exit_confirm = false;
                            }
                            _ => {}
                        }
                    }
                    Screen::DeleteInstallation(_) => match key.code {
                        KeyCode::Esc => {
                            app.activate(Control::ConfirmDelete(false));
                        }
                        KeyCode::Left | KeyCode::Right | KeyCode::Tab | KeyCode::BackTab => {
                            app.confirm = !app.confirm
                        }
                        KeyCode::Enter => {
                            app.activate(Control::ConfirmDelete(app.confirm));
                        }
                        _ => {}
                    },
                    Screen::EditInstallation => app.installation_editor_key(key),
                    Screen::Confirm => {
                        if !app.actions_enabled() {
                            app.screen = Screen::Form;
                            app.quick_focus = true;
                            continue;
                        }
                        if matches!(
                            key.code,
                            KeyCode::Left | KeyCode::Right | KeyCode::Tab | KeyCode::BackTab
                        ) {
                            app.confirm = !app.confirm;
                            continue;
                        }
                        if quit
                            || key.code == KeyCode::Char('n')
                            || key.code == KeyCode::Enter && !app.confirm
                        {
                            app.screen = Screen::Form;
                            continue;
                        }
                        if key.code == KeyCode::Enter || key.code == KeyCode::Char('y') {
                            if app.form.action == Action::Install
                                && app.has_installation()
                                && !app.fresh_installation
                            {
                                app.open_controller();
                                continue;
                            }
                            if let Err(error) = app.remember_form() {
                                app.error = error.to_string();
                                continue;
                            }
                            let (tx, rx) = mpsc::channel();
                            nodes_worker.workflow(request, tx)?;
                            receiver = Some(rx);
                            app.screen = Screen::Running;
                            // The request has moved into the worker and is never logged/debugged.
                            request = app.next_request();
                            app.form.fields[6].zeroize();
                        }
                    }
                    Screen::Running => {
                        if let Some(snapshot) = &app.update {
                            let pages = snapshot.nodes.len().div_ceil(10).max(1);
                            if key.code == KeyCode::Right {
                                app.node_page = (app.node_page + 1) % pages;
                            }
                            if key.code == KeyCode::Left {
                                app.node_page = (app.node_page + pages - 1) % pages;
                            }
                        }
                        if quit {
                            app.exit = true;
                            app.exit_confirm = false;
                        }
                    }
                    Screen::Finished => {
                        match key.code {
                            KeyCode::Up => app.scroll_result(true, 1),
                            KeyCode::Down => app.scroll_result(false, 1),
                            KeyCode::PageUp => app.scroll_result(true, app.result_page_size.get()),
                            KeyCode::PageDown => {
                                app.scroll_result(false, app.result_page_size.get())
                            }
                            KeyCode::Home => app.result_scroll = 0,
                            KeyCode::End => app.result_scroll = app.result_max_scroll.get(),
                            _ => {}
                        }
                        if app.close_result(key) {
                            receiver = None;
                            if let Some(finished) = worker.take()
                                && finished.join().is_err()
                            {
                                app.error =
                                    "Operation worker failed; diagnose the server before retrying."
                                        .into();
                            }
                        }
                    }
                }
            }
            _ => {}
        }
    }
    drop(terminal);
    drop(guard);
    if matches!(app.screen, Screen::Running)
        && let Some(rx) = receiver.take()
    {
        while let Ok(value) = rx.recv() {
            match value {
                Event::Prompt(_, reply) => {
                    let _ = reply.send(Answer::Cancel);
                }
                Event::Finished(result) => {
                    app.event(Event::Finished(result));
                    break;
                }
                value => app.event(value),
            }
        }
    }
    nodes_worker.shutdown()?;
    if let Some(worker) = worker {
        if app.exit && app.exit_confirm {
            println!(
                "The interface is closed. Waiting for the operation to detach or finish safely; server work is not cancelled."
            );
            if let Some(rx) = receiver {
                while let Ok(event) = rx.recv() {
                    match event {
                        Event::Prompt(_, reply) => {
                            let _ = reply.send(Answer::Cancel);
                        }
                        event => app.event(event),
                    }
                }
            }
        }
        worker.join().map_err(|_| {
            anyhow::anyhow!("Operation worker failed; diagnose the server before retrying.")
        })?;
    }
    if let Some(result) = app.outcome {
        match result {
            Ok(_) => {}
            Err(error) => {
                ensure!(false, "{error}");
            }
        }
    }
    Ok(())
}

fn terminal_too_small(area: Rect) -> bool {
    area.width < 70 || area.height < 30
}

fn draw(frame: &mut Frame, app: &App) -> Vec<Hit> {
    let area = frame.area();
    if !terminal_too_small(area) {
        return draw_content(frame, app);
    }
    app.content_overflow.set(false);
    app.list_max_viewport.set(0);
    app.list_viewport.set(0);
    app.scrollbar.set(None);
    let mut hits = Vec::new();
    frame.render_widget(
        Paragraph::new(
            "Terminal window is too small.\nMinimum size: 70 columns × 30 rows.\nPress Esc to close Workstation.",
        )
        .alignment(ratatui::layout::Alignment::Center)
        .wrap(Wrap { trim: false }),
        centered(area, area.width, 5.min(area.height)),
    );
    if app.exit {
        draw_exit_dialog(frame, app, &mut hits);
    }
    apply_theme(frame, app);
    hits
}

fn draw_exit_dialog(frame: &mut Frame, app: &App, hits: &mut Vec<Hit>) {
    let area = frame.area();
    hits.clear();
    let popup = centered(area, 78, 12);
    let sections = dialog(frame, popup, " Close Node Plane? ");
    let text = if app.controller.verifying
        || app
            .controller
            .job
            .as_ref()
            .is_some_and(|j| matches!(j["status"].as_str(), Some("queued" | "running")))
    {
        "The operation continues on the server after you close Workstation. Reopen this profile to check its result."
    } else if matches!(app.screen, Screen::Running) {
        if app.update.is_some() {
            "The backend update continues independently after you close this interface. Reopen this installation to check its progress."
        } else {
            "Server work is not cancelled. The interface will close, and this process will keep the SSH connection until it can detach safely or finish. Keep this terminal open until it returns."
        }
    } else if app.unsaved_changes() {
        "Close Workstation? Unsaved profile or server changes will be discarded."
    } else if matches!(app.screen, Screen::EditInstallation) {
        "Close the assistant? Unsaved installation changes will be discarded."
    } else {
        "Close the assistant? Your saved installations will be available next time."
    };
    frame.render_widget(Paragraph::new(text).wrap(Wrap { trim: false }), sections[0]);
    button_pair(
        frame,
        sections[1],
        ("Close", "Cancel"),
        app.exit_confirm,
        (Control::Exit(true), Control::Exit(false)),
        hits,
    );
}

fn apply_theme(frame: &mut Frame, app: &App) {
    let selected = app
        .connections
        .theme
        .as_ref()
        .and_then(|id| app.themes.iter().find(|theme| &theme.id == id));
    theme::apply(frame, &app.connections.accent, selected);
}

fn draw_content(frame: &mut Frame, app: &App) -> Vec<Hit> {
    app.content_overflow.set(false);
    app.list_max_viewport.set(0);
    app.list_viewport.set(0);
    app.scrollbar.set(None);
    let mut hits = Vec::new();
    let area = frame.area();
    if app.qr_visible {
        frame.render_widget(ratatui::widgets::Clear, area);
        // Keep all controls above the image: some graphics protocols erase cells
        // below their output. Closing restores the entire underlying screen.
        let modal = Rect::new(
            area.x + 1,
            area.y + 1,
            area.width.saturating_sub(2),
            area.height.saturating_sub(2),
        );
        frame.render_widget(
            Block::default()
                .borders(Borders::ALL)
                .title("QR code")
                .title_style(Style::default().fg(Color::Cyan))
                .border_style(Style::default().fg(SECTION_BORDER)),
            modal,
        );
        let close = Rect::new(
            modal.x + 2,
            modal.y + 1,
            modal.width.saturating_sub(4).min(22),
            3,
        );
        draw_button(frame, close, "Close", true);
        hits.push(Hit {
            area: close,
            control: Control::TemporaryBack,
        });
        let image_area = Rect::new(
            modal.x + 2,
            modal.y + 5,
            modal.width.saturating_sub(4),
            modal.height.saturating_sub(6),
        );
        if let Some(qr) = app.qr_graphic.borrow_mut().as_mut() {
            frame.render_stateful_widget(
                ratatui_image::StatefulImage::default().resize(ratatui_image::Resize::Scale(Some(
                    image::imageops::FilterType::Nearest,
                ))),
                image_area,
                qr,
            );
        } else if let Some(qr) = app.qr.borrow().as_ref() {
            draw_qr(frame, image_area, qr);
        }
        return hits;
    }
    let rows = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(4),
        Constraint::Length(3),
    ])
    .split(area);
    app.scroll_track.set(Rect::new(
        rows[1].right().saturating_sub(1),
        rows[1].y + 1,
        1,
        rows[1].height.saturating_sub(2),
    ));
    app.nodes_capacity
        .set(usize::from(rows[1].height.saturating_sub(10)).max(1));
    let header = Layout::horizontal([Constraint::Min(0), Constraint::Length(6)]).split(rows[0]);
    frame.render_widget(
        Paragraph::new("Node Plane | Workstation assistant").style(
            Style::default()
                .fg(Color::Cyan)
                .add_modifier(Modifier::BOLD),
        ),
        header[0],
    );
    frame.render_widget(
        Paragraph::new(app.header_status()).style(Style::default().fg(Color::Gray)),
        Rect::new(
            header[0].x + 2,
            header[0].y + 1,
            header[0].width.saturating_sub(2),
            1,
        ),
    );
    frame.render_widget(
        Block::default()
            .borders(Borders::BOTTOM)
            .border_style(Style::default().fg(Color::Cyan)),
        rows[0],
    );
    match app.screen {
        Screen::Idle => {
            draw_navigation(frame, app, rows[1], &mut hits);
        }
        Screen::QuickStart => draw_quick_start(frame, app, rows[1], &mut hits),
        Screen::Form => draw_form(frame, app, rows[1], &mut hits),
        Screen::NewInstallation => {
            let popup = centered(rows[1], 80, 14);
            let sections = dialog(frame, popup, " New installation ");
            frame.render_widget(Paragraph::new(format!("Create a new Node Plane installation on {} using this saved profile.\n\nUse this after the previous installation was removed. A new administrator binding will be created. Existing installations are not overwritten.",app.form.fields[0])).wrap(Wrap{trim:false}),sections[0]);
            button_pair(
                frame,
                sections[1],
                ("Continue", "Cancel"),
                app.confirm,
                (
                    Control::NewInstallationConfirm(true),
                    Control::NewInstallationConfirm(false),
                ),
                &mut hits,
            );
        }
        Screen::Controller => draw_controller(frame, app, rows[1], &mut hits),
        Screen::Access => draw_access(frame, app, rows[1], &mut hits),
        Screen::Temporary => draw_temporary(frame, app, rows[1], &mut hits),
        Screen::Nodes => draw_nodes(frame, app, rows[1], &mut hits),
        Screen::NodeWizard => draw_node_wizard(frame, app, rows[1], &mut hits),
        Screen::Settings => draw_settings(frame, app, rows[1], &mut hits),
        Screen::SettingsPage(SettingsPage::Appearance) => {
            draw_appearance(frame, app, rows[1], &mut hits)
        }
        Screen::SettingsPage(SettingsPage::Profiles) => {
            draw_profiles(frame, app, rows[1], &mut hits)
        }
        Screen::SettingsPage(SettingsPage::Security | SettingsPage::About) => {
            draw_settings_info(frame, app, rows[1], &mut hits)
        }
        Screen::DeleteInstallation(id) => {
            let right = draw_navigation(frame, app, rows[1], &mut hits);
            let sections = dialog(frame, centered(right, 72, 13), " Delete saved profile ");
            let name = app
                .connections
                .installations
                .iter()
                .find(|p| p.id == id)
                .map(|p| p.name.as_str())
                .unwrap_or("Profile");
            frame.render_widget(Paragraph::new(format!("Remove {name} from this workstation?\n\nThe saved profile and its host trust entry are removed. Host trust is kept if another saved profile uses the same address and port. The VPS, installation, SSH keys and operation history remain unchanged.")).wrap(Wrap { trim: false }), sections[0]);
            button_pair(
                frame,
                sections[1],
                ("Delete profile", "Cancel"),
                app.confirm,
                (Control::ConfirmDelete(true), Control::ConfirmDelete(false)),
                &mut hits,
            );
        }
        Screen::SettingsPage(SettingsPage::Session) => {
            draw_session_info(frame, app, rows[1], &mut hits)
        }
        Screen::SettingsPage(SettingsPage::Updates) => {
            let right = draw_navigation(frame, app, rows[1], &mut hits);
            let sections = dialog(frame, right, " Workstation updates ");
            frame.render_widget(
                Paragraph::new(format!(
                    "Installed: {}\n{}\n\nUpdates affect this workstation only.",
                    env!("CARGO_PKG_VERSION"),
                    app.self_status
                ))
                .wrap(Wrap { trim: false }),
                sections[0],
            );
            let actions = if app.self_release.is_some() {
                vec![
                    ("Check", Control::CheckWorkstationUpdates),
                    ("Update", Control::SelfUpdate),
                    ("Back", Control::Settings),
                ]
            } else {
                vec![
                    ("Check", Control::CheckWorkstationUpdates),
                    ("Back", Control::Settings),
                ]
            };
            let buttons = action_areas(
                frame.area(),
                sections[1],
                actions.iter().map(|(_, control)| *control),
                1,
            );
            for (index, (label, control)) in actions.into_iter().enumerate() {
                draw_button(
                    frame,
                    buttons[index],
                    label,
                    app.settings_selected == index + 1,
                );
                hits.push(Hit {
                    area: buttons[index],
                    control,
                });
            }
        }
        Screen::EditInstallation => draw_editor(frame, app, rows[1], &mut hits),
        Screen::Confirm => {
            let action = app.form.action.label();
            let target = if app.form.action == Action::PrepareNode {
                format!(
                    "Prepare {} using controller {}?",
                    app.form.fields[8], app.form.fields[0]
                )
            } else {
                format!("{action} on {}?", app.form.fields[0])
            };
            let popup = centered(rows[1], 80, 16);
            let sections = dialog(frame, popup, " Confirm ");
            frame.render_widget(Paragraph::new(format!("{target}\n\nSSH user: {}\nFirst connection: confirm the host fingerprint, then enter the SSH password if needed.\nPrivate keys stay on the machine that owns them.",app.form.fields[2])).wrap(Wrap{trim:false}),sections[0]);
            button_pair(
                frame,
                sections[1],
                ("Continue", "Cancel"),
                app.confirm,
                (Control::Confirm(true), Control::Confirm(false)),
                &mut hits,
            );
        }
        Screen::Running => {
            if let Some(snapshot) = &app.update {
                let popup = centered(rows[1], 90, 32);
                draw_update(frame, snapshot, app.node_page, popup);
                if snapshot.nodes.len() > 10 {
                    let buttons = Rect::new(
                        popup.x,
                        popup.bottom().saturating_sub(3),
                        popup.width,
                        3.min(popup.height),
                    );
                    button_pair(
                        frame,
                        buttons,
                        ("←", "→"),
                        true,
                        (Control::Page(false), Control::Page(true)),
                        &mut hits,
                    );
                }
            } else {
                draw_running(frame, app, rows[1]);
            }
        }
        Screen::Finished => {
            let (title, color, text) = match &app.outcome {
                Some(Ok(message)) => (" Complete ", Color::Cyan, message.as_str()),
                Some(Err(error)) => (" Needs attention ", Color::Red, error.as_str()),
                None => (" Needs attention ", Color::Red, "No confirmed result"),
            };
            let (summary, details) = result_sections(text, app.form.action == Action::Diagnose);
            let block = Block::bordered()
                .title(title)
                .title_style(Style::default().fg(color))
                .border_style(Style::default().fg(SECTION_BORDER));
            let inner = block.inner(rows[1]);
            frame.render_widget(block, rows[1]);
            let sections = Layout::vertical([
                Constraint::Length(if summary.is_empty() {
                    0
                } else {
                    4.min(inner.height.saturating_sub(5))
                }),
                Constraint::Min(0),
                Constraint::Length(3),
            ])
            .split(inner);
            frame.render_widget(
                Paragraph::new(summary)
                    .style(Style::default().fg(color))
                    .wrap(Wrap { trim: false }),
                sections[0],
            );
            let paragraph = Paragraph::new(details).wrap(Wrap { trim: false });
            let maximum = paragraph
                .line_count(sections[1].width)
                .saturating_sub(usize::from(sections[1].height));
            app.result_max_scroll.set(maximum);
            app.result_page_size
                .set(usize::from(sections[1].height).max(1));
            frame.render_widget(
                paragraph.scroll((
                    app.result_scroll.min(maximum).min(u16::MAX as usize) as u16,
                    0,
                )),
                sections[1],
            );
            app.register_scroll(
                app.result_scroll,
                maximum,
                usize::from(sections[1].height),
                ScrollTarget::Result,
            );
            let rect = centered(sections[2], 16, 3);
            draw_button(frame, rect, "Close", true);
            hits.push(Hit {
                area: rect,
                control: Control::Close,
            });
        }
    }
    let hint = if !app.error.is_empty() {
        app.error.as_str()
    } else {
        match app.screen {
            Screen::Idle => "↑ / ↓: select section   Enter / →: open   F1: Quick Start   Esc: exit",
            Screen::QuickStart => {
                "Tab / arrows: select   Enter: action   Space: don't show again   Esc: close"
            }
            Screen::NodeWizard => {
                "Tab / arrows: select   Enter / click: continue   Esc: previous step   Ctrl+C: exit"
            }
            Screen::NewInstallation => "← / → / Tab: select   Enter: confirm   Esc: cancel",
            Screen::Controller => "Tab / arrows: select   Enter / click: action   Esc: back",
            Screen::Access => {
                "Tab / arrows: select   Enter / click: open   Search: type   Esc: back"
            }
            Screen::Temporary => {
                "Tab / arrows: select   Enter / click: open   PgUp / PgDn: scroll   Esc: back / sidebar"
            }
            Screen::Nodes => {
                "Tab / ↑ / ↓: select   Enter / click: open   Ctrl+F: search   ← / →: pages   Esc: list / sidebar"
            }
            Screen::Form => {
                "Click: select   Tab: move   Arrows: actions / fields   Enter: continue   M: manage installation   Esc: sidebar / exit"
            }
            Screen::SettingsPage(SettingsPage::Session) => "Enter / Esc: back   Ctrl+C: exit",
            Screen::SettingsPage(SettingsPage::Appearance) => {
                "Tab / arrows: select   Enter / click: apply   Custom color: #RRGGBB   Esc: settings"
            }
            Screen::SettingsPage(SettingsPage::Updates) => {
                "Tab / arrows: select   Enter / click: activate   C: check   U: update   Esc: settings"
            }
            Screen::Settings => "Tab / arrows: move   Enter / click: open   Esc: sidebar / exit",
            Screen::SettingsPage(SettingsPage::Profiles) => {
                "Tab / arrows: move   Enter / click: select   N: new   E: edit   Delete: remove saved profile   Esc: settings"
            }
            Screen::SettingsPage(SettingsPage::Security | SettingsPage::About) => {
                "Enter / Esc: settings"
            }
            Screen::DeleteInstallation(_) => "Left / right: select   Enter: confirm   Esc: cancel",
            Screen::EditInstallation => {
                "Tab / arrows: move   Enter: next / save   Esc: sidebar / exit"
            }
            Screen::Confirm => "Click / ← / → / Tab: select   Enter: confirm selection   Esc: back",
            Screen::Running => {
                if app.update.is_some() {
                    "← / →: node pages   Esc: close interface"
                } else {
                    "Wait for the operation result. Do not close the terminal during installation."
                }
            }
            Screen::Finished => {
                "↑ / ↓ / wheel: scroll   PgUp / PgDn   Home / End   Enter / Esc: close"
            }
        }
    };
    frame.render_widget(
        Paragraph::new(hint)
            .style(Style::default().fg(if app.error.is_empty() {
                Color::Gray
            } else {
                Color::Yellow
            }))
            .wrap(Wrap { trim: false }),
        rows[2],
    );
    let sidebar_width = if area.width >= 80 { 26 } else { 18 };
    let is_back = |hit: &Hit| {
        hit.area.x >= sidebar_width
            && matches!(
                hit.control,
                Control::ControllerBack
                    | Control::NodesBack
                    | Control::NodesEditBack
                    | Control::WizardBack
                    | Control::AccessBack
                    | Control::AccessExpiryBack
                    | Control::AccessGrantBack
                    | Control::TemporaryBack
                    | Control::Settings
            )
    };
    let back = hits
        .iter()
        .rev()
        .find(|hit| is_back(hit))
        .map(|hit| hit.control)
        .or_else(|| {
            if matches!(app.screen, Screen::Controller)
                && app.controller_controls().contains(&Control::ControllerBack)
            {
                Some(Control::ControllerBack)
            } else if matches!(app.screen, Screen::SettingsPage(_)) {
                Some(Control::Settings)
            } else if matches!(app.screen, Screen::Nodes) && app.nodes.editor.is_some() {
                Some(Control::NodesEditBack)
            } else {
                None
            }
        });
    if let Some(control) = back {
        let mut selected = match app.screen {
            Screen::Controller => {
                !app.controller_sidebar
                    && app.controller_controls().get(app.controller.selected) == Some(&control)
            }
            Screen::Access => app.access_controls().get(app.access.selected) == Some(&control),
            Screen::Nodes => {
                !app.nodes_sidebar && app.nodes_controls().get(app.nodes_selected) == Some(&control)
            }
            Screen::Temporary => {
                !app.temporary_sidebar
                    && app.temporary_controls().get(app.temporary.selected) == Some(&control)
            }
            Screen::SettingsPage(SettingsPage::Profiles) => {
                app.profile_controls()
                    .get(app.settings_selected.saturating_sub(1))
                    == Some(&control)
            }
            Screen::SettingsPage(SettingsPage::Appearance) => {
                app.settings_selected == app.themes.len() + 3
            }
            Screen::SettingsPage(SettingsPage::Updates) => {
                app.settings_selected == 2 + usize::from(app.self_release.is_some())
            }
            Screen::NodeWizard => app.node_wizard.as_ref().is_some_and(|wizard| {
                wizard.step != crate::node_wizard::Step::Created
                    && wizard.selected
                        == wizard.choices().len() + usize::from(!wizard.selection_step())
            }),
            _ => false,
        };
        for hit in hits.iter().filter(|hit| is_back(hit)) {
            selected |= frame.buffer_mut()[(hit.area.x, hit.area.y)].fg == Color::Cyan;
            frame.render_widget(Clear, hit.area);
        }
        hits.retain(|hit| !is_back(hit));
        let button = pinned_back_area(area);
        frame.render_widget(Clear, button);
        draw_button(frame, button, "←", selected);
        hits.push(Hit {
            area: button,
            control,
        });
    }
    if let Some((prompt, _)) = &app.prompt {
        // Modal hit targets replace background controls, including outside clicks.
        hits.clear();
        let popup = centered(area, 84, 20);
        let sections = dialog(
            frame,
            popup,
            if matches!(prompt, Prompt::SelectAdministrator { .. }) {
                " Choose administrator "
            } else {
                " Connection confirmation "
            },
        );
        let text = match prompt {
            Prompt::SelectAdministrator { .. } => "Select the account to bind to this workstation key.\n↑ / ↓ · Enter to select · Esc to cancel".into(),
            Prompt::ConfirmAction { title, description } => format!("{title}\n\n{description}"),
            Prompt::HostKey { host, fingerprint } => format!(
                "First connection to {host}\n\nSSH fingerprint:\n{fingerprint}\n\nCompare it with a trusted provider/source before accepting."
            ),
            Prompt::Password { user, host } => format!(
                "SSH login: {user}@{host}\nThe workstation key is not authorized yet. Enter the server password below; only the public key will be enrolled after login."
            ),
        };
        if let Prompt::SelectAdministrator { choices } = prompt {
            let body =
                Layout::vertical([Constraint::Length(3), Constraint::Min(0)]).split(sections[0]);
            frame.render_widget(Paragraph::new(text).wrap(Wrap { trim: false }), body[0]);
            let capacity = usize::from(body[1].height).max(1);
            let start = app.administrator_selected.saturating_sub(capacity - 1);
            for (row, (index, (_, label))) in choices
                .iter()
                .enumerate()
                .skip(start)
                .take(capacity)
                .enumerate()
            {
                let area = Rect::new(body[1].x, body[1].y + row as u16, body[1].width, 1);
                frame.render_widget(
                    Paragraph::new(format!(
                        "{} {label}",
                        if index == app.administrator_selected {
                            "→"
                        } else {
                            " "
                        }
                    ))
                    .style(Style::default().fg(
                        if index == app.administrator_selected {
                            Color::Cyan
                        } else {
                            Color::Gray
                        },
                    )),
                    area,
                );
                hits.push(Hit {
                    area,
                    control: Control::Administrator(index),
                });
            }
        } else if matches!(prompt, Prompt::Password { .. }) {
            let body =
                Layout::vertical([Constraint::Min(0), Constraint::Length(3)]).split(sections[0]);
            frame.render_widget(Paragraph::new(text).wrap(Wrap { trim: false }), body[0]);
            let value = if app.password.is_empty() {
                "Type or paste the SSH password".into()
            } else {
                "*".repeat(
                    app.password
                        .chars()
                        .count()
                        .min(body[1].width.saturating_sub(3) as usize),
                )
            };
            frame.render_widget(
                Paragraph::new(value)
                    .style(Style::default().fg(if app.password.is_empty() {
                        Color::Gray
                    } else {
                        Color::White
                    }))
                    .block(
                        Block::bordered()
                            .title(" SSH password (hidden) ")
                            .border_style(Style::default().fg(Color::Cyan)),
                    ),
                body[1],
            );
            if body[1].height >= 3 && body[1].width >= 3 {
                let x = body[1].x
                    + 1
                    + (app.password.chars().count() as u16).min(body[1].width.saturating_sub(3));
                frame.set_cursor_position((x, body[1].y + 1));
            }
        } else {
            frame.render_widget(Paragraph::new(text).wrap(Wrap { trim: false }), sections[0]);
        }
        let label = match prompt {
            Prompt::HostKey { .. } => "Trust host",
            Prompt::Password { .. } => "Connect",
            _ => "Continue",
        };
        button_pair(
            frame,
            sections[1],
            (label, "Cancel"),
            app.trust
                || matches!(
                    prompt,
                    Prompt::Password { .. } | Prompt::SelectAdministrator { .. }
                ),
            (Control::Prompt(true), Control::Prompt(false)),
            &mut hits,
        );
    }
    if app.exit && app.prompt.is_none() {
        draw_exit_dialog(frame, app, &mut hits);
    }
    if app.discard.is_some() {
        hits.clear();
        let popup = centered(area, 68, 10);
        frame.render_widget(Clear, popup);
        let sections = dialog(frame, popup, " Unsaved changes ");
        frame.render_widget(
            Paragraph::new("Leave without saving? Your changes will be discarded.")
                .wrap(Wrap { trim: false }),
            sections[0],
        );
        button_pair(
            frame,
            sections[1],
            ("Discard", "Keep editing"),
            app.discard_confirm,
            (Control::Discard(true), Control::Discard(false)),
            &mut hits,
        );
    }
    if app.prompt.is_none() && !app.exit && app.discard.is_none() {
        let close = Rect::new(header[1].x, header[1].y, header[1].width, 2);
        let hovered = app.pointer_active && app.hovered == Some(Control::WindowClose);
        frame.render_widget(
            Paragraph::new("×")
                .alignment(ratatui::layout::Alignment::Center)
                .style(Style::default().fg(if hovered { Color::Red } else { Color::Gray })),
            Rect::new(close.right().saturating_sub(3), close.y, 3, 1),
        );
        hits.push(Hit {
            area: close,
            control: Control::WindowClose,
        });
    }
    let hovered_hit = app.pointer_position.and_then(|position| {
        hits.iter()
            .rev()
            .find(|hit| app.hovered == Some(hit.control) && hit.area.contains(position))
    });
    // A pointer hover replaces the keyboard highlight. Clear the old outline
    // before drawing the current one, including when the pointer leaves controls.
    if app.pointer_active {
        for hit in &hits {
            if hit.control == Control::WindowClose {
                continue;
            }
            let rect = hit.area;
            if rect.is_empty()
                || !matches!(frame.buffer_mut()[(rect.x, rect.y)].symbol(), "┌" | "╭")
            {
                continue;
            }
            if hovered_hit.is_some_and(|hovered| hovered.area == hit.area) {
                continue;
            }
            for y in rect.y..rect.bottom() {
                for x in rect.x..rect.right() {
                    let cell = &mut frame.buffer_mut()[(x, y)];
                    if matches!(
                        cell.symbol(),
                        "┌" | "┐" | "└" | "┘" | "╭" | "╮" | "╰" | "╯" | "─" | "│"
                    ) {
                        cell.fg = Color::White;
                    } else if y > rect.y && y + 1 < rect.bottom() && cell.fg == Color::Cyan {
                        cell.fg = Color::Gray;
                        cell.modifier.remove(Modifier::BOLD);
                    }
                }
            }
        }
    }
    // An open sidebar section retains its label color, but only focus owns its outline.
    let sidebar_focus =
        if app.pointer_active || app.exit || app.prompt.is_some() || app.discard.is_some() {
            None
        } else {
            match app.screen {
                Screen::Controller if app.controller_sidebar => {
                    Some(Control::Action(app.form.action))
                }
                Screen::Nodes if app.nodes_sidebar => Some(Control::Action(Action::PrepareNode)),
                Screen::Temporary if app.temporary_sidebar => Some(Control::Temporary),
                Screen::Form if app.form.selected == 0 && !app.quick_focus => {
                    Some(Control::Action(app.form.action))
                }
                Screen::Settings | Screen::SettingsPage(_) if app.settings_selected == 0 => {
                    Some(Control::Settings)
                }
                _ => None,
            }
        };
    for hit in hits
        .iter()
        .filter(|hit| hit.area.x < if area.width >= 80 { 26 } else { 18 })
    {
        if !matches!(
            hit.control,
            Control::Action(_) | Control::Access | Control::Temporary | Control::Settings
        ) {
            continue;
        }
        let color = if sidebar_focus == Some(hit.control) {
            Color::Cyan
        } else {
            Color::White
        };
        for y in hit.area.y..hit.area.bottom() {
            for x in hit.area.x..hit.area.right() {
                let cell = &mut frame.buffer_mut()[(x, y)];
                if matches!(
                    cell.symbol(),
                    "┌" | "┐" | "└" | "┘" | "╭" | "╮" | "╰" | "╯" | "─" | "│"
                ) {
                    cell.fg = color;
                }
            }
        }
    }
    let idle_control = if matches!(app.screen, Screen::Idle)
        && !app.pointer_active
        && !app.exit
        && app.discard.is_none()
        && app.prompt.is_none()
    {
        let mut navigation: Vec<_> = hits
            .iter()
            .filter(|hit| hit.control != Control::WindowClose)
            .collect();
        navigation.sort_by_key(|hit| (hit.area.y, hit.area.x));
        navigation
            .get(app.idle_selection.min(navigation.len().saturating_sub(1)))
            .map(|hit| hit.control)
    } else {
        None
    };
    if let Some(hit) = hovered_hit
        .or_else(|| idle_control.and_then(|control| hits.iter().find(|hit| hit.control == control)))
    {
        let rect = hit.area;
        let red = (rect.y..rect.bottom())
            .any(|y| (rect.x..rect.right()).any(|x| frame.buffer_mut()[(x, y)].fg == Color::Red));
        let color = if red { Color::Red } else { Color::Cyan };
        for y in rect.y..rect.bottom() {
            for x in rect.x..rect.right() {
                if frame.buffer_mut()[(x, y)].fg == SECTION_BORDER {
                    continue;
                }
                if x == rect.x || x + 1 == rect.right() || y == rect.y || y + 1 == rect.bottom() {
                    frame.buffer_mut()[(x, y)].fg = color;
                }
            }
        }
    }
    // The open section is independent of keyboard/pointer focus.
    for hit in hits
        .iter()
        .filter(|hit| hit.area.x < if area.width >= 80 { 26 } else { 18 })
    {
        let active = match hit.control {
            Control::Action(action) => {
                matches!(
                    app.screen,
                    Screen::Form | Screen::Nodes | Screen::Controller
                ) && app.form.action == action
            }
            Control::Access => matches!(app.screen, Screen::Access),
            Control::Temporary => matches!(app.screen, Screen::Temporary),
            Control::Settings => app.screen.is_settings(),
            _ => false,
        };
        if active {
            for y in hit.area.y..hit.area.bottom() {
                for x in hit.area.x..hit.area.right() {
                    let cell = &mut frame.buffer_mut()[(x, y)];
                    if !matches!(
                        cell.symbol(),
                        "┌" | "┐" | "└" | "┘" | "╭" | "╮" | "╰" | "╯" | "─" | "│"
                    ) && !cell.symbol().trim().is_empty()
                    {
                        cell.fg = Color::Cyan;
                    }
                }
            }
        }
    }
    // The selected Access tab remains visible independently of focus.
    for hit in &hits {
        if matches!(hit.control, Control::AccessTab(tab) if tab == app.access.tab) {
            let inner = Block::bordered().inner(hit.area);
            for y in inner.y..inner.bottom() {
                for x in inner.x..inner.right() {
                    let cell = &mut frame.buffer_mut()[(x, y)];
                    if !cell.symbol().trim().is_empty() {
                        cell.fg = Color::Cyan;
                    }
                }
            }
        }
    }
    let all_controls = match app.screen {
        Screen::Controller => app.controller_controls(),
        Screen::Access => app.access_controls(),
        Screen::Nodes => app.nodes_controls(),
        Screen::Temporary => app.temporary_controls(),
        Screen::SettingsPage(SettingsPage::Profiles) => app.profile_controls(),
        Screen::Form => app.form.visible().into_iter().map(Control::Field).collect(),
        Screen::EditInstallation => app
            .editor
            .as_ref()
            .map(|(_, fields, _)| (0..fields.len()).map(Control::EditField).collect())
            .unwrap_or_default(),
        Screen::NodeWizard => app
            .node_wizard
            .as_ref()
            .map(|wizard| {
                (0..wizard.choices().len())
                    .map(Control::WizardChoice)
                    .collect()
            })
            .unwrap_or_default(),
        Screen::Settings => SettingsPage::ALL
            .iter()
            .copied()
            .map(Control::SettingsPage)
            .collect(),
        Screen::SettingsPage(SettingsPage::Appearance) => (0..app.themes.len())
            .map(Control::Accent)
            .chain([Control::AccentInput, Control::SaveAccent, Control::Settings])
            .collect(),
        _ => Vec::new(),
    };
    let selected = match app.screen {
        Screen::Controller => Some(app.controller.selected),
        Screen::Access => Some(app.access.selected),
        Screen::Nodes => Some(app.nodes_selected),
        Screen::Temporary => Some(app.temporary.selected),
        Screen::Form => Some(app.form.selected.saturating_sub(1)),
        Screen::EditInstallation => app.editor.as_ref().map(|(_, _, selected)| *selected),
        Screen::NodeWizard => app.node_wizard.as_ref().map(|wizard| wizard.selected),
        Screen::Settings | Screen::SettingsPage(_) => Some(app.settings_selected.saturating_sub(1)),
        _ => None,
    };
    let overflow = app.content_overflow.get()
        || all_controls
            .iter()
            .any(|control| !hits.iter().any(|hit| hit.control == *control));
    let position = selected
        .and_then(|index| all_controls.get(index))
        .and_then(|control| app.list_position(*control, &all_controls));
    if position.is_some() || overflow {
        let mut label = position
            .map(|(index, total)| format!(" {index}/{total} "))
            .unwrap_or_default();
        if overflow {
            label.push_str(" ↑ / ↓: select · wheel: scroll ");
        }
        let width = (label.chars().count() as u16).min(rows[1].width.saturating_sub(4));
        frame.render_widget(
            Paragraph::new(label).style(Style::default().fg(Color::Cyan)),
            Rect::new(
                rows[1].right().saturating_sub(width + 2),
                rows[1].bottom().saturating_sub(1),
                width,
                1,
            ),
        );
    }
    if app.prompt.is_none()
        && !app.exit
        && app.discard.is_none()
        && let Some(bar) = app.scrollbar.get()
    {
        for offset in 0..bar.track.height {
            let thumb = offset >= bar.thumb_start && offset < bar.thumb_start + bar.thumb_length;
            let cell = &mut frame.buffer_mut()[(bar.track.x, bar.track.y + offset)];
            cell.set_symbol(if thumb { "█" } else { "│" });
            cell.fg = if thumb { Color::Cyan } else { SECTION_BORDER };
        }
    }
    apply_theme(frame, app);
    hits
}
fn result_sections(text: &str, diagnose: bool) -> (String, String) {
    if diagnose {
        let lines: Vec<&str> = text.lines().collect();
        if let Some(start) = lines.iter().rposition(|line| line.trim() == "Summary") {
            let end = lines
                .iter()
                .enumerate()
                .skip(start + 1)
                .find(|(_, line)| line.trim() == "Suggested Fixes")
                .map_or(lines.len(), |(index, _)| index);
            let summary_lines: Vec<_> = lines[start + 1..end]
                .iter()
                .copied()
                .filter(|line| !line.trim().is_empty())
                .collect();
            let summary = summary_lines.last().map_or(String::new(), |result| {
                format!(
                    "{}\n{}",
                    result,
                    summary_lines[..summary_lines.len() - 1].join(" · ")
                )
            });
            let mut details = lines[..start].to_vec();
            details.extend_from_slice(&lines[end..]);
            return (summary.trim().into(), details.join("\n"));
        }
    }
    (String::new(), text.into())
}
fn preparation_step(action: Action, stage: &str) -> (usize, usize) {
    let step = match action {
        Action::Update => {
            if stage.starts_with("Connecting") {
                1
            } else if stage.starts_with("Authorizing") {
                2
            } else if stage.starts_with("Checking") {
                3
            } else {
                4
            }
        }
        Action::PrepareNode => {
            if stage.starts_with("Connecting to the controller") {
                1
            } else if stage.starts_with("Authorizing") {
                2
            } else if stage.starts_with("Connecting to the target") {
                3
            } else {
                4
            }
        }
        Action::Diagnose => {
            if stage.starts_with("Connecting") {
                1
            } else if stage.starts_with("Checking the server") {
                2
            } else {
                3
            }
        }
        Action::Install => {
            if stage.starts_with("Connecting") {
                1
            } else if stage.starts_with("Checking the server") {
                2
            } else if stage.starts_with("Checking installation") {
                3
            } else if stage.starts_with("Fetching") {
                4
            } else {
                5
            }
        }
    };
    (
        step,
        match action {
            Action::Diagnose => 3,
            Action::Install => 5,
            _ => 4,
        },
    )
}
fn draw_navigation(frame: &mut Frame, app: &App, area: Rect, hits: &mut Vec<Hit>) -> Rect {
    let columns = Layout::horizontal([
        Constraint::Length(if area.width >= 80 { 26 } else { 18 }),
        Constraint::Min(0),
    ])
    .split(area);
    let compact = area.width < 100;
    let temporary_label = if compact {
        "Temp. Configs"
    } else {
        "Temporary configs"
    };
    let sidebar = section_block(" Actions ");
    let navigation = sidebar.inner(columns[0]);
    frame.render_widget(sidebar, columns[0]);
    let settings = Rect::new(
        navigation.x,
        navigation.bottom().saturating_sub(3).max(navigation.y),
        navigation.width,
        3.min(navigation.height),
    );
    let profile_height = settings.y.saturating_sub(navigation.y).min(3);
    let profile = Rect::new(
        navigation.x,
        settings.y.saturating_sub(profile_height),
        navigation.width,
        profile_height,
    );
    let action_height = if profile.y.saturating_sub(navigation.y) >= 18 {
        3
    } else {
        2
    };
    let y = navigation.y.saturating_add(5 * action_height);
    if y.saturating_add(action_height) <= profile.y {
        let rect = Rect::new(navigation.x, y, navigation.width, action_height);
        if app.controller_ready() {
            draw_navigation_button(
                frame,
                rect,
                temporary_label,
                matches!(app.screen, Screen::Temporary),
            );
            hits.push(Hit {
                area: rect,
                control: Control::Temporary,
            });
        } else {
            let disabled = Style::default().fg(Color::DarkGray);
            let block = Block::bordered().border_style(disabled);
            let label = format!("× {temporary_label}");
            frame.render_widget(
                Paragraph::new(if rect.height < 3 { "" } else { label.as_str() })
                    .alignment(ratatui::layout::Alignment::Center)
                    .style(disabled)
                    .block(if rect.height < 3 {
                        block.title(label.as_str()).title_style(disabled)
                    } else {
                        block
                    }),
                rect,
            );
        }
    }
    let access_y = navigation.y.saturating_add(4 * action_height);
    if access_y.saturating_add(action_height) <= profile.y {
        let rect = Rect::new(navigation.x, access_y, navigation.width, action_height);
        if app.controller_ready() {
            draw_navigation_button(frame, rect, "Access", matches!(app.screen, Screen::Access));
        } else {
            frame.render_widget(
                Paragraph::new("× Access")
                    .alignment(ratatui::layout::Alignment::Center)
                    .style(Style::default().fg(Color::DarkGray))
                    .block(if rect.height < 3 {
                        Block::bordered()
                            .title("× Access")
                            .title_style(Style::default().fg(Color::DarkGray))
                            .border_style(Style::default().fg(Color::DarkGray))
                    } else {
                        Block::bordered().border_style(Style::default().fg(Color::DarkGray))
                    }),
                rect,
            );
        }
        if app.controller_ready() {
            hits.push(Hit {
                area: rect,
                control: Control::Access,
            });
        }
    }
    for (index, action) in Action::ALL.into_iter().enumerate() {
        let y = navigation.y.saturating_add(index as u16 * action_height);
        if y.saturating_add(action_height) > profile.y {
            break;
        }
        let rect = Rect::new(navigation.x, y, navigation.width, action_height);
        if app.actions_enabled() && (action == Action::Install || app.controller_ready()) {
            draw_navigation_button(
                frame,
                rect,
                if action == Action::Install && app.controller_ready() {
                    if compact {
                        "NP Settings"
                    } else {
                        "Node Plane Settings"
                    }
                } else {
                    sidebar_action_label(action, compact)
                },
                matches!(
                    app.screen,
                    Screen::Form | Screen::Nodes | Screen::Controller
                ) && app.form.action == action,
            );
            hits.push(Hit {
                area: rect,
                control: Control::Action(action),
            });
        } else {
            let disabled = Style::default().fg(Color::DarkGray);
            let block = Block::bordered().border_style(disabled);
            let label = format!("× {}", sidebar_action_label(action, compact));
            let text = Paragraph::new(if action_height < 3 { "" } else { &label })
                .alignment(ratatui::layout::Alignment::Center)
                .style(disabled);
            frame.render_widget(
                text.block(if action_height < 3 {
                    block.title(label.as_str()).title_style(disabled)
                } else {
                    block
                }),
                rect,
            );
        }
    }
    let parts = Layout::horizontal([
        Constraint::Length(3),
        Constraint::Min(0),
        Constraint::Length(3),
    ])
    .split(profile);
    draw_navigation_button(frame, parts[0], "←", app.quick_focus);
    draw_navigation_button(
        frame,
        parts[1],
        app.form
            .saved
            .as_ref()
            .map_or("New profile", |p| p.name.as_str()),
        app.quick_focus,
    );
    draw_navigation_button(frame, parts[2], "→", app.quick_focus);
    hits.push(Hit {
        area: parts[0],
        control: Control::CycleProfile(true),
    });
    hits.push(Hit {
        area: parts[1],
        control: Control::QuickProfile,
    });
    hits.push(Hit {
        area: parts[2],
        control: Control::CycleProfile(false),
    });
    draw_navigation_button(frame, settings, "Settings", app.screen.is_settings());
    hits.push(Hit {
        area: settings,
        control: Control::Settings,
    });
    columns[1]
}
