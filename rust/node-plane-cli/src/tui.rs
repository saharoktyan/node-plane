use crate::{
    config::{Action, Request, WorkflowOptions},
    connections::{Connections, Installation},
    events::{Answer, Event, Prompt, UiInteraction},
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
    text::Line,
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

enum Screen {
    Form,
    Settings,
    SettingsPage(SettingsPage),
    DeleteInstallation(uuid::Uuid),
    EditInstallation,
    Confirm,
    Running,
    Finished,
}
#[derive(Clone, Copy, PartialEq)]
enum SettingsPage {
    Profiles,
    Updates,
    Security,
    Session,
    About,
}
impl SettingsPage {
    const ALL: [Self; 5] = [
        Self::Profiles,
        Self::Updates,
        Self::Security,
        Self::Session,
        Self::About,
    ];
    fn label(self) -> &'static str {
        match self {
            Self::Profiles => "Installation profiles",
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
#[derive(Clone, Copy)]
enum Control {
    Action(Action),
    QuickProfile,
    CycleProfile(bool),
    Settings,
    SettingsPage(SettingsPage),
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
    Field(usize),
    Continue,
    Confirm(bool),
    Prompt(bool),
    Close,
    Page(bool),
    ScrollResult(bool),
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
            Action::Install => &[0, 1, 2, 3, 4, 5, 6],
            Action::Diagnose => &[0, 1, 2, 7, 11],
            Action::Update => &[0, 1, 2, 3, 4, 7],
            Action::PrepareNode => &[0, 1, 2, 7, 8, 9, 10],
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
        self.selected = (self.selected + 1) % self.count();
    }
    fn previous(&mut self) {
        self.selected = (self.selected + self.count() - 1) % self.count();
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
    password: Zeroizing<String>,
    trust: bool,
    outcome: Option<Result<String, String>>,
    update: Option<crate::workstation::UpdateSnapshot>,
    node_page: usize,
    stop: Arc<AtomicBool>,
    hits: Vec<Hit>,
    confirm: bool,
    connections: Connections,
    state_dir: std::path::PathBuf,
    session_id: uuid::Uuid,
    settings_selected: usize,
    settings_profile: usize,
    editor: Option<(Option<uuid::Uuid>, Vec<String>, usize)>,
    exit: bool,
    exit_confirm: bool,
    quick_focus: bool,
    result_scroll: usize,
    result_max_scroll: std::cell::Cell<usize>,
    result_page_size: std::cell::Cell<usize>,
}
impl App {
    fn new(r: &Request) -> Self {
        Self {
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
            password: Zeroizing::new(String::new()),
            trust: false,
            outcome: None,
            update: None,
            node_page: 0,
            stop: r.workflow.stop.clone(),
            hits: Vec::new(),
            confirm: true,
            connections: Connections::default(),
            state_dir: r.state_dir.clone(),
            session_id: uuid::Uuid::new_v4(),
            settings_selected: 0,
            settings_profile: 0,
            editor: None,
            exit: false,
            exit_confirm: false,
            quick_focus: true,
            result_scroll: 0,
            result_max_scroll: std::cell::Cell::new(0),
            result_page_size: std::cell::Cell::new(1),
        }
    }
    fn load_connections(&mut self, request: &Request) -> Result<()> {
        self.connections = Connections::load(&self.state_dir)?;
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
    fn open_settings(&mut self) {
        self.quick_focus = false;
        self.screen = Screen::Settings;
        self.settings_selected = 0;
        self.error.clear();
    }
    fn profile_controls(&self) -> Vec<Control> {
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
    fn delete_profile(&mut self, id: uuid::Uuid) -> Result<()> {
        let mut next = self.connections.clone();
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
        Ok(())
    }
    fn navigate(&mut self, backwards: bool) {
        let current = if self.quick_focus {
            4
        } else if self.screen.is_settings() {
            5
        } else {
            Action::ALL
                .iter()
                .position(|a| *a == self.form.action)
                .unwrap()
        };
        let mut next = (current + if backwards { 5 } else { 1 }) % 6;
        while next < 4 && !self.actions_enabled() {
            next = (next + if backwards { 5 } else { 1 }) % 6;
        }
        self.quick_focus = false;
        if next == 5 {
            self.open_settings();
        } else if next == 4 {
            self.quick_focus = true;
            self.form.selected = 0;
            self.screen = Screen::Form;
            self.editor = None;
        } else {
            self.form.select_action(Action::ALL[next]);
            self.screen = Screen::Form;
            self.editor = None;
            self.error.clear();
        }
    }
    fn actions_enabled(&self) -> bool {
        self.form.saved.is_some()
    }
    fn profile_index(&self) -> usize {
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
    fn cycle_profile(&mut self, backwards: bool) -> Result<()> {
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
        self.screen = Screen::Form;
        self.editor = None;
        self.error.clear();
        Ok(())
    }
    fn activate_profile(&mut self) {
        if self.quick_focus && !self.actions_enabled() {
            self.begin_edit(true);
        } else {
            self.quick_focus = true;
            self.form.selected = 0;
            self.screen = Screen::Form;
            self.editor = None;
        }
    }
    fn begin_edit(&mut self, new: bool) {
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
    fn save_edit(&mut self) -> Result<()> {
        let (id, fields, _) = self.editor.as_ref().unwrap();
        let profile = Installation {
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
    fn use_selected(&mut self) -> Result<()> {
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
    fn remember_form(&mut self) -> Result<()> {
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
    fn event(&mut self, event: Event) {
        match event {
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
                self.prompt = Some((prompt, reply));
                self.password.zeroize();
                self.trust = false;
            }
            Event::Finished(result) => {
                self.result_scroll = 0;
                self.outcome = Some(result);
                self.screen = Screen::Finished;
            }
        }
    }
    fn prompt_key(&mut self, key: KeyEvent) {
        let Some((prompt, _)) = &self.prompt else {
            return;
        };
        let answer = match prompt {
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
    fn scroll_result(&mut self, backwards: bool, amount: usize) {
        self.result_scroll = if backwards {
            self.result_scroll.saturating_sub(amount)
        } else {
            self.result_scroll
                .saturating_add(amount)
                .min(self.result_max_scroll.get())
        };
    }
    fn close_result(&mut self, key: KeyEvent) -> bool {
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.exit = true;
            self.exit_confirm = false;
            return false;
        }
        if !matches!(key.code, KeyCode::Enter | KeyCode::Esc) {
            return false;
        }
        self.screen = Screen::Form;
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

    fn next_request(&self) -> Request {
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
    fn mouse(&mut self, event: MouseEvent) -> Option<KeyEvent> {
        if self.prompt.is_none()
            && !self.exit
            && matches!(
                event.kind,
                MouseEventKind::ScrollUp | MouseEventKind::ScrollDown
            )
        {
            let count = match self.screen {
                Screen::Settings => SettingsPage::ALL.len(),
                Screen::SettingsPage(SettingsPage::Profiles) => self.profile_controls().len(),
                _ => 0,
            };
            if count > 0 {
                self.settings_selected = if event.kind == MouseEventKind::ScrollUp {
                    self.settings_selected.saturating_sub(1).max(1)
                } else {
                    (self.settings_selected + 1).min(count)
                };
                if matches!(self.screen, Screen::SettingsPage(SettingsPage::Profiles))
                    && self.settings_selected <= self.connections.installations.len()
                {
                    self.settings_profile = self.settings_selected - 1;
                }
                return None;
            }
        }
        if matches!(self.screen, Screen::Finished) && !self.exit && self.prompt.is_none() {
            match event.kind {
                MouseEventKind::ScrollUp => {
                    self.scroll_result(true, 3);
                    return None;
                }
                MouseEventKind::ScrollDown => {
                    self.scroll_result(false, 3);
                    return None;
                }
                _ => {}
            }
        }
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
    fn activate(&mut self, control: Control) -> Option<KeyEvent> {
        let key = match control {
            Control::Action(action) => {
                if !self.actions_enabled() {
                    return None;
                }
                self.quick_focus = false;
                self.form.select_action(action);
                self.screen = Screen::Form;
                self.editor = None;
                self.error.clear();
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
                self.screen = Screen::SettingsPage(page);
                self.settings_selected = 1;
                self.error.clear();
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
                if !self.actions_enabled() {
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
                if matches!(&self.prompt, Some((Prompt::Password { .. }, _))) && !value {
                    KeyCode::Esc
                } else {
                    KeyCode::Enter
                }
            }
            Control::ScrollResult(backwards) => {
                self.scroll_result(backwards, self.result_page_size.get());
                return None;
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
    app.load_connections(&request)?;
    let mut receiver: Option<mpsc::Receiver<Event>> = None;
    let mut worker = None;
    let (check_tx, check_rx) = mpsc::channel();
    let startup_tx = check_tx.clone();
    std::thread::spawn(move || {
        let _ = startup_tx.send(crate::self_manage::discover());
    });
    let mut checking = true;
    let mut offer = false;
    let mut self_confirmation: Option<mpsc::Receiver<Answer>> = None;
    let mut self_updating = false;
    loop {
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
                            "{} → {}\nOnly the local managed binary is replaced. Restart TUI afterwards. Server installations and saved profiles are retained.\nIf not installed yet, run `node-plane self install` first.",
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
                        app.event(value);
                    }
                    Err(mpsc::TryRecvError::Empty) => break,
                    Err(mpsc::TryRecvError::Disconnected) => {
                        if !matches!(app.screen, Screen::Finished) {
                            app.event(Event::Finished(Err("Operation worker disconnected. Diagnose the server before retrying.".into())));
                        }
                        break;
                    }
                }
            }
        }
        terminal.draw(|frame| app.hits = draw(frame, &app))?;
        if !event::poll(Duration::from_millis(100))? {
            continue;
        }
        let input = match event::read()? {
            TermEvent::Mouse(mouse) => match app.mouse(mouse) {
                Some(key) => TermEvent::Key(key),
                None => continue,
            },
            input => input,
        };
        match input {
            TermEvent::Paste(text) => {
                if matches!(&app.prompt, Some((Prompt::Password { .. }, _))) {
                    if app.password.len() + text.len() <= 4096 {
                        app.password
                            .extend(text.chars().filter(|c| !c.is_control()));
                    }
                } else if matches!(app.screen, Screen::EditInstallation) && !app.exit {
                    if let Some((_, fields, selected)) = &mut app.editor
                        && *selected < fields.len()
                        && *selected != 4
                        && fields[*selected].len() + text.len() <= 4096
                    {
                        fields[*selected].extend(text.chars().filter(|c| !c.is_control()));
                    }
                } else if matches!(app.screen, Screen::Form) && !app.exit {
                    app.form.append(&text);
                }
            }
            TermEvent::Key(key) if key.kind == KeyEventKind::Press => {
                if app.prompt.is_some() {
                    app.prompt_key(key);
                    continue;
                }
                if app.exit {
                    match key.code {
                        KeyCode::Tab | KeyCode::Left | KeyCode::Right => {
                            app.exit_confirm = !app.exit_confirm
                        }
                        KeyCode::Esc | KeyCode::Char('n') => app.exit = false,
                        KeyCode::Enter if !app.exit_confirm => app.exit = false,
                        KeyCode::Enter | KeyCode::Char('y') => {
                            app.stop.store(true, Ordering::Relaxed);
                            break;
                        }
                        _ => {}
                    }
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
                        if !app.actions_enabled() {
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
                                (app.settings_selected + 1) % (SettingsPage::ALL.len() + 1)
                        }
                        KeyCode::BackTab | KeyCode::Up => {
                            app.settings_selected = (app.settings_selected
                                + SettingsPage::ALL.len())
                                % (SettingsPage::ALL.len() + 1)
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
                                app.settings_selected = app.settings_selected % controls.len() + 1
                            }
                            KeyCode::BackTab | KeyCode::Up => {
                                app.settings_selected = (app.settings_selected + controls.len() - 2)
                                    % controls.len()
                                    + 1
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
                    Screen::EditInstallation => {
                        if app.editor.as_ref().is_some_and(|editor| editor.2 == 9)
                            && matches!(
                                key.code,
                                KeyCode::Up | KeyCode::Down | KeyCode::Left | KeyCode::Right
                            )
                        {
                            app.navigate(matches!(key.code, KeyCode::Up | KeyCode::Left));
                            continue;
                        }
                        if quit {
                            if key.code == KeyCode::Esc
                                && app.editor.as_ref().is_some_and(|editor| editor.2 != 9)
                            {
                                app.editor.as_mut().unwrap().2 = 9;
                            } else {
                                app.exit = true;
                                app.exit_confirm = false;
                            }
                            continue;
                        }
                        if let Some((_, fields, selected)) = &mut app.editor {
                            match key.code {
                                KeyCode::Tab | KeyCode::Down => *selected = (*selected + 1) % 10,
                                KeyCode::BackTab | KeyCode::Up => *selected = (*selected + 9) % 10,
                                KeyCode::Left | KeyCode::Right if *selected == 4 => {
                                    cycle_channel(&mut fields[4], key.code == KeyCode::Left);
                                }
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
                                    if let Err(error) = app.save_edit() {
                                        app.error = error.to_string();
                                    }
                                }
                                KeyCode::Enter if *selected == 9 => *selected = 0,
                                KeyCode::Enter => {
                                    app.editor = None;
                                    app.screen = Screen::SettingsPage(SettingsPage::Profiles);
                                    app.settings_selected = 0;
                                }
                                _ => {}
                            }
                        }
                    }
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
                            if let Err(error) = app.remember_form() {
                                app.error = error.to_string();
                                continue;
                            }
                            let (tx, rx) = mpsc::channel();
                            let interaction = UiInteraction::shared(tx.clone());
                            worker = Some(crate::spawn(request, interaction, tx));
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

fn draw(frame: &mut Frame, app: &App) -> Vec<Hit> {
    let mut hits = Vec::new();
    let area = frame.area();
    let rows = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(4),
        Constraint::Length(3),
    ])
    .split(area);
    frame.render_widget(
        Paragraph::new("Node Plane | Workstation assistant")
            .style(
                Style::default()
                    .fg(Color::Cyan)
                    .add_modifier(Modifier::BOLD),
            )
            .block(Block::default().borders(Borders::BOTTOM)),
        rows[0],
    );
    match app.screen {
        Screen::Form => draw_form(frame, app, rows[1], &mut hits),
        Screen::Settings => draw_settings(frame, app, rows[1], &mut hits),
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
            frame.render_widget(Paragraph::new(format!("Remove {name} from this workstation?\n\nOnly the saved connection profile is removed. The VPS, Node Plane installation, SSH keys, trusted hosts and audit history remain unchanged.")).wrap(Wrap { trim: false }), sections[0]);
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
            let buttons = Layout::horizontal(vec![Constraint::Fill(1); actions.len()])
                .spacing(1)
                .split(sections[1]);
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
                Some(Ok(message)) => (" Complete ", Color::Green, message.as_str()),
                Some(Err(error)) => (" Needs attention ", Color::Red, error.as_str()),
                None => (" Needs attention ", Color::Red, "No confirmed result"),
            };
            let (summary, details) = result_sections(text, app.form.action == Action::Diagnose);
            let block = Block::bordered()
                .title(title)
                .border_style(Style::default().fg(color));
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
            let buttons = Layout::horizontal([
                Constraint::Length(8),
                Constraint::Min(0),
                Constraint::Length(8),
            ])
            .split(sections[2]);
            if maximum > 0 {
                draw_button(frame, buttons[0], "↑", false);
                draw_button(frame, buttons[2], "↓", false);
                hits.push(Hit {
                    area: buttons[0],
                    control: Control::ScrollResult(true),
                });
                hits.push(Hit {
                    area: buttons[2],
                    control: Control::ScrollResult(false),
                });
            }
            let rect = centered(buttons[1], 16, 3);
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
            Screen::Form => {
                "Click: select   Tab: move   Arrows: actions / fields   Enter: next / continue   Esc: sidebar / exit"
            }
            Screen::SettingsPage(SettingsPage::Session) => "Enter / Esc: back   Ctrl+C: exit",
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
    if let Some((prompt, _)) = &app.prompt {
        // Modal hit targets replace background controls, including outside clicks.
        hits.clear();
        let popup = centered(area, 84, 20);
        let sections = dialog(frame, popup, " Connection confirmation ");
        let text = match prompt {
            Prompt::ConfirmAction { title, description } => format!("{title}\n\n{description}"),
            Prompt::HostKey { host, fingerprint } => format!(
                "First connection to {host}\n\nSSH fingerprint:\n{fingerprint}\n\nCompare it with a trusted provider/source before accepting."
            ),
            Prompt::Password { user, host } => format!(
                "SSH login: {user}@{host}\nThe workstation key is not authorized yet. Enter the server password below; only the public key will be enrolled after login."
            ),
        };
        if matches!(prompt, Prompt::Password { .. }) {
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
            app.trust || matches!(prompt, Prompt::Password { .. }),
            (Control::Prompt(true), Control::Prompt(false)),
            &mut hits,
        );
    }
    if app.exit && app.prompt.is_none() {
        hits.clear();
        let popup = centered(area, 78, 12);
        let sections = dialog(frame, popup, " Close Node Plane? ");
        let text = if matches!(app.screen, Screen::Running) {
            if app.update.is_some() {
                "The backend update continues independently after you close this interface. Reopen this installation to check its progress."
            } else {
                "Server work is not cancelled. The interface will close, and this process will keep the SSH connection until it can detach safely or finish. Keep this terminal open until it returns."
            }
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
            &mut hits,
        );
    }
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
fn centered(area: Rect, width: u16, height: u16) -> Rect {
    let width = width.min(area.width);
    let height = height.min(area.height);
    Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    )
}
fn draw_running(frame: &mut Frame, app: &App, area: Rect) {
    let popup = centered(area, 84, 9);
    let installing = app.form.action == Action::Install
        && !app.tracker.current.is_empty()
        && app.tracker.completed < 7;
    let (title, counter, label) = if installing {
        (
            " Installing Node Plane ".to_string(),
            format!(
                "Step {}/7 · {}/7 complete",
                app.tracker.completed + 1,
                app.tracker.completed
            ),
            app.tracker.current.as_str(),
        )
    } else {
        let (step, total) = preparation_step(app.form.action, &app.stage);
        let title = match app.form.action {
            Action::Update => " Update preparation ",
            Action::PrepareNode => " Prepare SSH access ",
            Action::Diagnose => " Diagnostics ",
            Action::Install if app.tracker.completed == 7 => " Verifying installation ",
            Action::Install => " Installation preparation ",
        };
        (
            title.into(),
            if app.tracker.completed == 7 && app.form.action == Action::Install {
                "Installation steps: 7/7 complete".into()
            } else {
                format!("Step {step}/{total}")
            },
            app.stage.as_str(),
        )
    };
    let block = Block::bordered()
        .title(title)
        .border_style(Style::default().fg(Color::Cyan));
    let inner = block.inner(popup);
    frame.render_widget(Clear, popup);
    frame.render_widget(block, popup);
    let sections = Layout::vertical([
        Constraint::Length(2),
        Constraint::Min(2),
        Constraint::Length(1),
    ])
    .split(inner);
    frame.render_widget(
        Paragraph::new(counter).style(
            Style::default()
                .fg(Color::Cyan)
                .add_modifier(Modifier::BOLD),
        ),
        sections[0],
    );
    frame.render_widget(
        Paragraph::new(label).wrap(Wrap { trim: false }),
        sections[1],
    );
    if installing {
        frame.render_widget(
            Gauge::default()
                .gauge_style(Style::default().fg(Color::Cyan))
                .ratio(app.tracker.completed as f64 / 7.0)
                .label(app.tracker.detail.as_str()),
            sections[2],
        );
    }
}
fn draw_update(
    frame: &mut Frame,
    snapshot: &crate::workstation::UpdateSnapshot,
    page: usize,
    area: Rect,
) {
    use ratatui::widgets::{Cell, Row, Table};
    let sections = Layout::vertical([
        Constraint::Length(4),
        Constraint::Length(7),
        Constraint::Min(2),
    ])
    .split(area);
    frame.render_widget(
        Paragraph::new(format!(
            "Operation: {}\n{} · {}\nController: {}/4 complete{}",
            snapshot.id,
            snapshot.status,
            snapshot.phase,
            snapshot
                .components
                .iter()
                .filter(|(_, status)| status == "succeeded")
                .count(),
            if snapshot.nodes.is_empty() {
                String::new()
            } else {
                format!(
                    " · Nodes: {}/{} finished",
                    snapshot
                        .nodes
                        .iter()
                        .filter(|node| matches!(
                            node.status.as_str(),
                            "succeeded" | "blocked" | "superseded" | "skipped"
                        ))
                        .count(),
                    snapshot.nodes.len()
                )
            }
        )),
        sections[0],
    );
    let rows = snapshot
        .components
        .iter()
        .map(|(name, status)| Row::new([Cell::from(name.as_str()), Cell::from(status.as_str())]));
    frame.render_widget(
        Table::new(
            rows,
            [Constraint::Percentage(50), Constraint::Percentage(50)],
        )
        .header(Row::new(["Component", "Status"]).style(Style::default().fg(Color::Cyan)))
        .block(Block::bordered().title(" Controller stack ")),
        sections[1],
    );
    let pages = snapshot.nodes.len().div_ceil(10).max(1);
    let page = page.min(pages - 1);
    let mut lines = Vec::new();
    let mut region = "";
    for node in snapshot.nodes.iter().skip(page * 10).take(10) {
        if node.region != region {
            region = &node.region;
            lines.push(Line::styled(
                if region.is_empty() { "Other" } else { region },
                Style::default()
                    .fg(Color::Cyan)
                    .add_modifier(Modifier::BOLD),
            ));
        }
        lines.push(Line::from(format!(
            "{} · {}{}",
            if node.title.is_empty() {
                &node.key
            } else {
                &node.title
            },
            node.status_label(),
            if node.error.is_empty() {
                String::new()
            } else {
                format!(" ({})", node.error)
            }
        )));
    }
    if snapshot.nodes.is_empty() {
        lines.push(Line::from("No registered nodes."));
    }
    let title = if pages > 1 {
        format!(" Node agents and runtimes · {}/{} ", page + 1, pages)
    } else {
        " Node agents and runtimes ".into()
    };
    frame.render_widget(
        Paragraph::new(lines)
            .wrap(Wrap { trim: false })
            .block(Block::bordered().title(title)),
        sections[2],
    );
}
fn draw_button(frame: &mut Frame, area: Rect, label: &str, selected: bool) {
    draw_button_color(frame, area, label, selected, Color::Cyan);
}
fn draw_button_color(frame: &mut Frame, area: Rect, label: &str, selected: bool, accent: Color) {
    frame.render_widget(
        Paragraph::new(label)
            .alignment(ratatui::layout::Alignment::Center)
            .style(
                Style::default()
                    .fg(if selected {
                        Color::Black
                    } else if accent == Color::Red {
                        Color::Red
                    } else {
                        Color::Gray
                    })
                    .bg(if selected { accent } else { Color::Reset }),
            )
            .block(Block::bordered()),
        area,
    );
}
fn button_pair(
    frame: &mut Frame,
    area: Rect,
    labels: (&str, &str),
    first: bool,
    controls: (Control, Control),
    hits: &mut Vec<Hit>,
) {
    let strip = centered(area, 36, 3);
    let buttons = Layout::horizontal([
        Constraint::Fill(1),
        Constraint::Length(2),
        Constraint::Fill(1),
    ])
    .split(strip);
    draw_button_color(
        frame,
        buttons[0],
        labels.0,
        first,
        if matches!(controls.0, Control::ConfirmDelete(true)) {
            Color::Red
        } else {
            Color::Cyan
        },
    );
    draw_button(frame, buttons[2], labels.1, !first);
    hits.push(Hit {
        area: buttons[0],
        control: controls.0,
    });
    hits.push(Hit {
        area: buttons[2],
        control: controls.1,
    });
}
fn dialog(frame: &mut Frame, area: Rect, title: &str) -> [Rect; 2] {
    let block = Block::bordered()
        .title(title)
        .border_style(Style::default().fg(Color::Cyan));
    let inner = block.inner(area);
    frame.render_widget(Clear, area);
    frame.render_widget(block, area);
    let parts = Layout::vertical([Constraint::Min(0), Constraint::Length(3)]).split(inner);
    [parts[0], parts[1]]
}
fn toggle_repair(value: &mut String) {
    *value = if value == "true" { "false" } else { "true" }.into();
}
fn cycle_channel(value: &mut String, backwards: bool) {
    *value = match value.as_str() {
        "main" => "dev",
        "dev" => "main",
        _ if backwards => "main",
        _ => "dev",
    }
    .into();
}
fn draw_channel(
    frame: &mut Frame,
    area: Rect,
    value: &str,
    selected: bool,
    editor: bool,
    hits: &mut Vec<Hit>,
) {
    let block = Block::bordered()
        .title(" Update channel · ← / → ")
        .border_style(Style::default().fg(if selected { Color::Cyan } else { Color::Gray }));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let parts = Layout::horizontal([
        Constraint::Length(3),
        Constraint::Min(0),
        Constraint::Length(3),
    ])
    .split(inner);
    frame.render_widget(
        Paragraph::new("←").alignment(ratatui::layout::Alignment::Center),
        parts[0],
    );
    frame.render_widget(
        Paragraph::new(if value.is_empty() { "Installed" } else { value })
            .alignment(ratatui::layout::Alignment::Center),
        parts[1],
    );
    frame.render_widget(
        Paragraph::new("→").alignment(ratatui::layout::Alignment::Center),
        parts[2],
    );
    // Arrow hits follow the field hit so clicks change the choice immediately.
    hits.push(Hit {
        area: parts[0],
        control: Control::Channel(editor, true),
    });
    hits.push(Hit {
        area: parts[2],
        control: Control::Channel(editor, false),
    });
}
fn draw_form(frame: &mut Frame, app: &App, area: Rect, hits: &mut Vec<Hit>) {
    let labels = [
        "Controller hostname / IP",
        "SSH port",
        "SSH user",
        "Update channel (main / dev)",
        "Release tag (blank = latest)",
        "Telegram administrator ID(s)",
        "BotFather token (hidden)",
        "Administrator account (UUID / Telegram ID; blank = only admin)",
        "Target node hostname / IP",
        "Target SSH port",
        "Target SSH user",
        "Offer service and operation recovery (confirmation required)",
    ];
    let right = draw_navigation(frame, app, area, hits);
    if !app.actions_enabled() {
        let sections = dialog(frame, right, " New installation profile ");
        frame.render_widget(Paragraph::new("Create or select an installation profile to enable actions.\n\nUse ← / → in the profile switcher above Settings.\nSelect New profile again, or press Enter, to create a profile.").wrap(Wrap { trim: false }), sections[0]);
        return;
    }
    let title = app.form.saved.as_ref().map_or_else(
        || " Connection settings ".to_string(),
        |profile| format!(" {} · {} ", profile.name, app.form.action.label()),
    );
    let sections = dialog(frame, right, &title);
    let visible = app.form.visible();
    if visible.is_empty() {
        frame.render_widget(Paragraph::new("This installation is ready. Continue to review the action.\nEdit its connection details in Settings.").wrap(Wrap { trim: false }), sections[0]);
    }
    let capacity = usize::from(sections[0].height / 3).max(1);
    let selected = app
        .form
        .selected
        .saturating_sub(1)
        .min(visible.len().saturating_sub(1));
    let offset = selected.saturating_add(1).saturating_sub(capacity);
    for (position, &i) in visible.iter().enumerate().skip(offset).take(capacity) {
        let y = sections[0].y + (position - offset) as u16 * 3;
        if y.saturating_add(3) > sections[0].bottom() {
            break;
        }
        let label = if i == 3 && app.form.action == Action::Update {
            "Update channel (blank = installed channel)"
        } else {
            labels[i]
        };
        let value = if i == 6 {
            "*".repeat(app.form.fields[i].len().min(48))
        } else {
            app.form.fields[i].clone()
        };
        let rect = Rect::new(sections[0].x, y, sections[0].width, 3);
        let selected = app.form.selected == position + 1;
        hits.push(Hit {
            area: rect,
            control: Control::Field(position + 1),
        });
        if i == 3 {
            draw_channel(frame, rect, &value, selected, false, hits);
        } else if i == 11 {
            draw_field(
                frame,
                rect,
                labels[i],
                if value == "true" {
                    "← Enabled →"
                } else {
                    "← Disabled · read-only →"
                },
                selected,
            );
            hits.push(Hit {
                area: rect,
                control: Control::RepairMode,
            });
        } else {
            draw_field(frame, rect, label, &value, selected);
        }
    }
    let button = centered(sections[1], 18, 3);
    draw_button(
        frame,
        button,
        "Continue",
        app.form.selected == app.form.count() - 1,
    );
    hits.push(Hit {
        area: button,
        control: Control::Continue,
    });
}
fn draw_navigation(frame: &mut Frame, app: &App, area: Rect, hits: &mut Vec<Hit>) -> Rect {
    let columns = Layout::horizontal([
        Constraint::Length(if area.width >= 80 { 26 } else { 18 }),
        Constraint::Min(0),
    ])
    .split(area);
    let sidebar = Block::bordered().title(" Actions ");
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
    let action_height = if profile.y.saturating_sub(navigation.y) >= 12 {
        3
    } else {
        2
    };
    for (index, action) in Action::ALL.into_iter().enumerate() {
        let y = navigation.y.saturating_add(index as u16 * action_height);
        if y.saturating_add(action_height) > profile.y {
            break;
        }
        let rect = Rect::new(navigation.x, y, navigation.width, action_height);
        if app.actions_enabled() {
            draw_navigation_button(
                frame,
                rect,
                action.label(),
                !app.quick_focus && matches!(app.screen, Screen::Form) && app.form.action == action,
            );
            hits.push(Hit {
                area: rect,
                control: Control::Action(action),
            });
        } else {
            let disabled = Style::default().fg(Color::DarkGray);
            let block = Block::bordered().border_style(disabled);
            let text = Paragraph::new(if action_height < 3 {
                ""
            } else {
                action.label()
            })
            .alignment(ratatui::layout::Alignment::Center)
            .style(disabled);
            frame.render_widget(
                text.block(if action_height < 3 {
                    block.title(action.label()).title_style(disabled)
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
fn draw_navigation_button(frame: &mut Frame, area: Rect, label: &str, selected: bool) {
    let style = Style::default().fg(if selected {
        Color::Cyan
    } else {
        Color::DarkGray
    });
    let block = Block::bordered().border_style(style);
    frame.render_widget(
        Paragraph::new(if area.height < 3 { "" } else { label })
            .alignment(ratatui::layout::Alignment::Center)
            .style(if selected {
                style.add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(Color::Gray)
            })
            .block(if area.height < 3 {
                block.title(label).title_style(style)
            } else {
                block
            }),
        area,
    );
}
fn draw_field(frame: &mut Frame, rect: Rect, label: &str, value: &str, selected: bool) {
    let style = Style::default().fg(if selected {
        Color::Cyan
    } else {
        Color::DarkGray
    });
    frame.render_widget(
        Paragraph::new(value).block(Block::bordered().title(label).border_style(style)),
        rect,
    );
}
fn draw_settings(frame: &mut Frame, app: &App, area: Rect, hits: &mut Vec<Hit>) {
    let right = draw_navigation(frame, app, area, hits);
    dialog(frame, right, " Settings ");
    let content = Block::bordered().inner(right);
    let sections = [content];
    let capacity = usize::from(sections[0].height / 3).max(1);
    let offset = app.settings_selected.saturating_sub(capacity);
    for (index, page) in SettingsPage::ALL
        .iter()
        .copied()
        .enumerate()
        .skip(offset)
        .take(capacity)
    {
        let value = match page {
            SettingsPage::Profiles => format!(
                "{} saved installations · select, edit or remove",
                app.connections.installations.len()
            ),
            SettingsPage::Updates => format!("{} · {}", env!("CARGO_PKG_VERSION"), app.self_status),
            SettingsPage::Security => "SSH identity and trusted hosts".into(),
            SettingsPage::Session => "Session identity and local diagnostic paths".into(),
            SettingsPage::About => "Version, documentation and license".into(),
        };
        let rect = Rect::new(
            sections[0].x,
            sections[0].y + (index - offset) as u16 * 3,
            sections[0].width,
            3,
        );
        draw_field(
            frame,
            rect,
            page.label(),
            &value,
            app.settings_selected == index + 1,
        );
        hits.push(Hit {
            area: rect,
            control: Control::SettingsPage(page),
        });
    }
}
fn draw_profiles(frame: &mut Frame, app: &App, area: Rect, hits: &mut Vec<Hit>) {
    let right = draw_navigation(frame, app, area, hits);
    let mut sections = dialog(frame, right, " Installation profiles ");
    if right.width < 60 && !app.connections.installations.is_empty() {
        let inner = Block::bordered().inner(right);
        let layout = Layout::vertical([Constraint::Min(1), Constraint::Length(6)]).split(inner);
        sections = [layout[0], layout[1]];
    }
    let count = app.connections.installations.len();
    if count == 0 {
        frame.render_widget(Paragraph::new("No saved installations yet.\nCreate one here, or fill an action form; its connection will be remembered when you continue.\n\nPasswords and bot tokens are not saved.").wrap(Wrap { trim: false }), sections[0]);
    }
    let capacity = usize::from(sections[0].height / 3).max(1);
    let offset = app
        .settings_profile
        .saturating_add(1)
        .saturating_sub(capacity);
    for (position, profile) in app
        .connections
        .installations
        .iter()
        .enumerate()
        .skip(offset)
        .take(capacity)
    {
        let y = sections[0].y + (position - offset) as u16 * 3;
        if y.saturating_add(3) > sections[0].bottom() {
            break;
        }
        let rect = Rect::new(sections[0].x, y, sections[0].width, 3);
        let label = if Some(profile.id) == app.connections.selected {
            format!("{} · selected", profile.name)
        } else {
            profile.name.clone()
        };
        let value = if profile.host.is_empty() {
            "Connection address not set".into()
        } else {
            format!("{}@{}:{}", profile.user, profile.host, profile.port)
        };
        draw_field(
            frame,
            rect,
            &label,
            &value,
            app.settings_selected == position + 1,
        );
        hits.push(Hit {
            area: rect,
            control: Control::Installation(position),
        });
    }
    let available = count > 0;
    let actions = if available {
        vec![
            ("Use", Control::UseInstallation),
            ("Edit", Control::EditInstallation),
            ("New", Control::NewInstallation),
            ("Delete", Control::DeleteInstallation),
            ("Back", Control::Settings),
        ]
    } else {
        vec![
            ("New", Control::NewInstallation),
            ("Back", Control::Settings),
        ]
    };
    let mut buttons = Vec::new();
    if sections[1].height >= 6 {
        let rows =
            Layout::vertical([Constraint::Length(3), Constraint::Length(3)]).split(sections[1]);
        for (row, count) in [(rows[0], 3), (rows[1], 2)] {
            buttons.extend(
                Layout::horizontal(vec![Constraint::Fill(1); count])
                    .spacing(1)
                    .split(row)
                    .iter()
                    .copied(),
            );
        }
    } else {
        buttons.extend(
            Layout::horizontal(vec![Constraint::Fill(1); actions.len()])
                .spacing(1)
                .split(sections[1])
                .iter()
                .copied(),
        );
    }
    for (index, (label, control)) in actions.into_iter().enumerate() {
        draw_button_color(
            frame,
            buttons[index],
            label,
            app.settings_selected == count + index + 1,
            if matches!(control, Control::DeleteInstallation) {
                Color::Red
            } else {
                Color::Cyan
            },
        );
        hits.push(Hit {
            area: buttons[index],
            control,
        });
    }
}
fn draw_settings_info(frame: &mut Frame, app: &App, area: Rect, hits: &mut Vec<Hit>) {
    let right = draw_navigation(frame, app, area, hits);
    let Screen::SettingsPage(page) = app.screen else {
        return;
    };
    let sections = dialog(frame, right, page.label());
    let text = if page == SettingsPage::Security {
        let fingerprint = std::fs::read_to_string(app.state_dir.join("id_ed25519.pub"))
            .ok()
            .and_then(|value| russh::keys::PublicKey::from_openssh(value.trim()).ok())
            .map(|key| key.fingerprint(russh::keys::HashAlg::Sha256).to_string())
            .unwrap_or_else(|| "Created on first connection".into());
        format!(
            "Workstation SSH key\n{fingerprint}\n\nTrusted hosts\n{}\n\nHost fingerprints are confirmed before authentication. Passwords and bot tokens are not saved.",
            app.state_dir.join("known_hosts.json").display()
        )
    } else {
        format!(
            "Node Plane workstation\nVersion: {}\nLicense: Apache-2.0\n\nProject and documentation\nhttps://github.com/saharoktyan/node-plane\n\nWorkstation manages installations through SSH. Its updates are separate from controller updates.",
            env!("CARGO_PKG_VERSION")
        )
    };
    frame.render_widget(Paragraph::new(text).wrap(Wrap { trim: false }), sections[0]);
    let button = centered(sections[1], 18, 3);
    draw_button(frame, button, "Back", true);
    hits.push(Hit {
        area: button,
        control: Control::Settings,
    });
}

fn draw_session_info(frame: &mut Frame, app: &App, area: Rect, hits: &mut Vec<Hit>) {
    let right = draw_navigation(frame, app, area, hits);
    let sections = dialog(frame, right, " Workstation session ");
    let profile = app.form.saved.as_ref();
    let profile_id = profile
        .map(|p| p.id.to_string())
        .unwrap_or_else(|| "None".into());
    let connection = profile
        .map(|p| format!("{}@{}:{}", p.user, p.host, p.port))
        .unwrap_or_else(|| "No installation selected".into());
    let account = profile
        .filter(|p| !p.account.is_empty())
        .map(|p| p.account.as_str())
        .unwrap_or("Automatic selection on connection");
    let text = format!(
        "TUI session UUID: {}\nLocal installation UUID: {}\nConnection: {}\nAdministrator selector: {}\nState directory: {}\n\nThe local installation UUID is independent of the backend administrator UUID. Backend audit sessions are created for each operation; this UUID identifies only this TUI launch.",
        app.session_id,
        profile_id,
        connection,
        account,
        app.state_dir.display()
    );
    frame.render_widget(Paragraph::new(text).wrap(Wrap { trim: false }), sections[0]);
    let button = centered(sections[1], 18, 3);
    draw_button(frame, button, "Back", true);
    hits.push(Hit {
        area: button,
        control: Control::Settings,
    });
}
fn draw_editor(frame: &mut Frame, app: &App, area: Rect, hits: &mut Vec<Hit>) {
    let right = draw_navigation(frame, app, area, hits);
    let Some((_, fields, selected)) = &app.editor else {
        return;
    };
    let sections = dialog(
        frame,
        right,
        if app.editor.as_ref().is_some_and(|editor| editor.0.is_none()) {
            " New installation profile "
        } else {
            " Edit installation "
        },
    );
    let labels = [
        "Installation name",
        "Controller hostname / IP (optional)",
        "SSH port",
        "SSH user (optional)",
        "Update channel (blank = installed channel)",
        "Telegram administrator ID(s) (optional)",
        "Administrator account UUID / Telegram ID (optional)",
    ];
    let capacity = usize::from(sections[0].height / 3).max(1);
    let offset = selected.min(&6).saturating_add(1).saturating_sub(capacity);
    for (i, value) in fields.iter().enumerate().skip(offset).take(capacity) {
        let y = sections[0].y + (i - offset) as u16 * 3;
        if y.saturating_add(3) > sections[0].bottom() {
            break;
        }
        let rect = Rect::new(sections[0].x, y, sections[0].width, 3);
        hits.push(Hit {
            area: rect,
            control: Control::EditField(i),
        });
        if i == 4 {
            draw_channel(frame, rect, value, *selected == i, true, hits);
        } else {
            draw_field(frame, rect, labels[i], value, *selected == i);
        }
    }
    button_pair(
        frame,
        sections[1],
        ("Save", "Cancel"),
        *selected != 8,
        (Control::SaveInstallation, Control::CancelEdit),
        hits,
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ssh_password_field_is_visible_masked_and_empty_submit_is_rejected() {
        let mut app = App::new(&test_request());
        let (tx, rx) = mpsc::channel();
        app.event(Event::Prompt(
            Prompt::Password {
                user: "root".into(),
                host: "vps.example".into(),
            },
            tx,
        ));
        for (width, height) in [(120, 40), (80, 24), (40, 16)] {
            let mut terminal =
                Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
            terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
            let content = format!("{:?}", terminal.backend().buffer());
            assert!(
                content.contains("SSH password (hidden)"),
                "{width}x{height}: {content}"
            );
        }
        let connect = app
            .hits
            .iter()
            .find(|h| matches!(h.control, Control::Prompt(true)))
            .unwrap()
            .area;
        let enter = click(&mut app, (connect.x + 1, connect.y + 1)).unwrap();
        app.prompt_key(enter);
        assert!(app.prompt.is_some());
        assert!(rx.try_recv().is_err());
        for c in "secret".chars() {
            app.prompt_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
        }
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 24)).unwrap();
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        let content = format!("{:?}", terminal.backend().buffer());
        assert!(content.contains("******"));
        assert!(!content.contains("secret"));
        let connect = app
            .hits
            .iter()
            .find(|h| matches!(h.control, Control::Prompt(true)))
            .unwrap()
            .area;
        let enter = click(&mut app, (connect.x + 1, connect.y + 1)).unwrap();
        app.prompt_key(enter);
        let Answer::Password(password) = rx.recv().unwrap() else {
            panic!("Password answer expected")
        };
        assert_eq!(password.as_str(), "secret");
        assert!(app.password.is_empty());
        assert!(app.error.is_empty());
    }
    #[test]
    fn workstation_settings_offer_update_without_starting_server_work() {
        let mut app = App::new(&test_request());
        app.open_settings();
        app.self_status = "Available: v9.0.0".into();
        app.self_release = Some(
            serde_json::from_value(serde_json::json!({
                "tag_name":"v9.0.0", "draft":false, "prerelease":false, "assets":[]
            }))
            .unwrap(),
        );
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        let content = format!("{:?}", terminal.backend().buffer());
        assert!(content.contains(env!("CARGO_PKG_VERSION")));
        assert!(content.contains("v9.0.0"));
        let button = app
            .hits
            .iter()
            .find(|h| matches!(h.control, Control::SettingsPage(SettingsPage::Updates)))
            .unwrap()
            .area;
        assert!(click(&mut app, (button.x + 1, button.y + 1)).is_none());
        assert!(!app.self_requested);
        assert!(matches!(
            app.screen,
            Screen::SettingsPage(SettingsPage::Updates)
        ));
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        let update = app
            .hits
            .iter()
            .find(|h| matches!(h.control, Control::SelfUpdate))
            .unwrap()
            .area;
        assert!(click(&mut app, (update.x + 1, update.y + 1)).is_none());
        assert!(app.self_requested);
        assert!(app.outcome.is_none());
    }
    #[test]
    fn settings_directory_opens_sections_without_profile_actions_on_home() {
        let mut app = test_profile_app();
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 24)).unwrap();
        for page in SettingsPage::ALL {
            app.open_settings();
            terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
            assert!(!app.hits.iter().any(|h| matches!(
                h.control,
                Control::UseInstallation | Control::DeleteInstallation
            )));
            let target = app
                .hits
                .iter()
                .find(|h| matches!(h.control, Control::SettingsPage(p) if p == page))
                .unwrap()
                .area;
            click(&mut app, (target.x + 1, target.y + 1));
            assert!(matches!(app.screen, Screen::SettingsPage(p) if p == page));
            terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
            assert!(
                app.hits
                    .iter()
                    .any(|h| matches!(h.control, Control::Settings))
            );
            assert!(app.screen.is_settings());
            assert!(app.outcome.is_none() && !app.self_requested);
        }
        assert_eq!(Action::Update.label(), "Update Node Plane");
    }
    #[test]
    fn deleting_saved_profiles_preserves_host_trust_and_operation_history() {
        for delete_active in [true, false] {
            let dir = tempfile::tempdir().unwrap();
            let mut app = test_profile_app();
            app.state_dir = dir.path().into();
            let active = app.connections.selected.unwrap();
            let other = saved_installation("other.example");
            app.connections.installations.push(other.clone());
            app.connections.save(&app.state_dir).unwrap();
            for name in ["id_ed25519", "known_hosts.json", "update-operation.json"] {
                std::fs::write(dir.path().join(name), "keep").unwrap();
            }
            app.delete_profile(if delete_active { active } else { other.id })
                .unwrap();
            let restored = Connections::load(dir.path()).unwrap();
            assert_eq!(restored.installations.len(), 1);
            assert_eq!(
                restored.selected,
                if delete_active { None } else { Some(active) }
            );
            assert_eq!(app.actions_enabled(), !delete_active);
            for name in ["id_ed25519", "known_hosts.json", "update-operation.json"] {
                assert_eq!(
                    std::fs::read_to_string(dir.path().join(name)).unwrap(),
                    "keep"
                );
            }
        }
    }
    #[test]
    fn profile_deletion_requires_confirmation_and_cancel_preserves_save() {
        let dir = tempfile::tempdir().unwrap();
        let mut app = test_profile_app();
        app.state_dir = dir.path().into();
        app.connections.save(&app.state_dir).unwrap();
        let saved = std::fs::read(dir.path().join("installations.json")).unwrap();
        app.activate(Control::DeleteInstallation);
        assert!(matches!(app.screen, Screen::DeleteInstallation(_)));
        assert!(!app.confirm);
        app.activate(Control::ConfirmDelete(false));
        assert_eq!(
            std::fs::read(dir.path().join("installations.json")).unwrap(),
            saved
        );
        app.activate(Control::DeleteInstallation);
        app.activate(Control::ConfirmDelete(true));
        assert!(
            Connections::load(dir.path())
                .unwrap()
                .installations
                .is_empty()
        );
        assert!(!app.actions_enabled());
        assert_eq!(app.profile_controls().len(), 2);
    }
    #[test]
    fn compact_settings_scroll_and_update_checks_keep_server_actions_idle() {
        let mut app = test_profile_app();
        app.open_settings();
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(60, 16)).unwrap();
        for _ in 0..SettingsPage::ALL.len() {
            app.mouse(MouseEvent {
                kind: MouseEventKind::ScrollDown,
                column: 50,
                row: 5,
                modifiers: KeyModifiers::NONE,
            });
        }
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        assert!(
            app.hits
                .iter()
                .any(|hit| matches!(hit.control, Control::SettingsPage(SettingsPage::About)))
        );
        app.activate(Control::SettingsPage(SettingsPage::Updates));
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        let check = app
            .hits
            .iter()
            .find(|hit| matches!(hit.control, Control::CheckWorkstationUpdates))
            .unwrap()
            .area;
        click(&mut app, (check.x + 1, check.y + 1));
        assert!(app.self_check_requested);
        assert!(!app.self_requested && app.outcome.is_none());
        assert!(matches!(
            app.screen,
            Screen::SettingsPage(SettingsPage::Updates)
        ));
    }
    #[test]
    fn finished_diagnostics_keep_summary_and_buttons_outside_scrollable_output() {
        let mut app = test_profile_app();
        app.form.action = Action::Diagnose;
        let report = format!(
            "{}\nSummary\nMode: simple\nFailures: 0\nWarnings: 0\n[OK] Setup looks healthy\nSuggested Fixes\n/data-last-line",
            (0..80)
                .map(|i| format!("check-{i:03}"))
                .collect::<Vec<_>>()
                .join("\n")
        );
        app.event(Event::Finished(Ok(report)));
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 24)).unwrap();
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        let initial = format!("{:?}", terminal.backend().buffer());
        assert!(initial.contains("Setup looks healthy") && initial.contains("check-000"));
        assert!(!initial.contains("/data-last-line"));
        app.scroll_result(false, usize::MAX);
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        let end = format!("{:?}", terminal.backend().buffer());
        assert!(end.contains("Setup looks healthy") && end.contains("/data-last-line"));
        let close = app
            .hits
            .iter()
            .find(|hit| matches!(hit.control, Control::Close))
            .unwrap()
            .area;
        let label: String = (close.x + 1..close.right() - 1)
            .map(|x| terminal.backend().buffer()[(x, close.y + 1)].symbol())
            .collect();
        assert_eq!(label.trim(), "Close");
        app.mouse(MouseEvent {
            kind: MouseEventKind::ScrollUp,
            column: 30,
            row: 10,
            modifiers: KeyModifiers::NONE,
        });
        assert_eq!(
            app.result_scroll,
            app.result_max_scroll.get().saturating_sub(3)
        );
        app.scroll_result(true, usize::MAX);
        assert_eq!(app.result_scroll, 0);
    }
    #[test]
    fn channel_selector_cycles_only_supported_channels_and_mouse_updates_both_forms() {
        let mut value = String::new();
        cycle_channel(&mut value, false);
        assert_eq!(value, "dev");
        cycle_channel(&mut value, false);
        assert_eq!(value, "main");
        cycle_channel(&mut value, true);
        assert_eq!(value, "dev");
        let mut app = test_profile_app();
        app.form.saved.as_mut().unwrap().branch.clear();
        app.form.fields[3].clear();
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        let arrow = app
            .hits
            .iter()
            .find(|hit| matches!(hit.control, Control::Channel(false, false)))
            .unwrap()
            .area;
        click(&mut app, (arrow.x, arrow.y));
        assert_eq!(app.form.fields[3], "dev");
        app.form.append("invalid-channel");
        assert_eq!(app.form.fields[3], "dev");
        app.begin_edit(false);
        app.editor.as_mut().unwrap().2 = 4;
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        let arrow = app
            .hits
            .iter()
            .find(|hit| matches!(hit.control, Control::Channel(true, true)))
            .unwrap()
            .area;
        click(&mut app, (arrow.x, arrow.y));
        assert_eq!(app.editor.as_ref().unwrap().1[4], "main");
        assert_eq!(app.editor.as_ref().unwrap().2, 4);
    }
    #[test]
    fn running_panel_centers_the_stage_and_prints_it_only_once() {
        let r = Request {
            action: Action::Update,
            host: "controller.example".into(),
            port: 22,
            user: "root".into(),
            state_dir: std::path::PathBuf::new(),
            tag: String::new(),
            branch: String::new(),
            admin_ids: String::new(),
            bot_token: Zeroizing::new(String::new()),
            workflow: WorkflowOptions::default(),
        };
        let mut app = App::new(&r);
        app.screen = Screen::Running;
        app.stage = "Checking available releases in the installed update channel".into();
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
        terminal
            .draw(|frame| {
                draw(frame, &app);
            })
            .unwrap();
        let content = format!("{:?}", terminal.backend().buffer());
        assert_eq!(content.matches(&app.stage).count(), 1);
        assert!(content.contains("Step 3/4"));
        assert!(!content.contains("Diagnostics"));
        let popup = centered(Rect::new(0, 0, 120, 40), 84, 9);
        assert_eq!(popup.x, 18);
        assert_eq!(popup.y, 15);
    }
    #[test]
    fn action_specific_fields_do_not_edit_hidden_credentials() {
        let mut form = Form {
            action: Action::Update,
            fields: vec![String::new(); 11],
            selected: 6,
            saved: None,
        };
        form.append("admin-account");
        assert_eq!(form.fields[7], "admin-account");
        assert!(form.fields[5].is_empty());
        form.action = Action::PrepareNode;
        form.selected = 5;
        form.append("target.example");
        assert_eq!(form.fields[8], "target.example");
        assert!(form.fields[6].is_empty());
        form.cycle(false);
        assert!(form.action == Action::Install);
        assert_eq!(form.fields[3], "dev");
        form.cycle(false);
        assert!(form.action == Action::Update);
        assert!(form.fields[3].is_empty());
    }
    #[test]
    fn update_node_pages_render_and_resize_without_leaking_other_pages() {
        let snapshot = crate::workstation::UpdateSnapshot {
            id: uuid::Uuid::new_v4(),
            status: "running".into(),
            phase: "agents".into(),
            components: vec![("backend".into(), "succeeded".into())],
            nodes: (0..12)
                .map(|i| crate::workstation::NodeSnapshot {
                    key: format!("n{i}"),
                    title: format!("Node{i:02}"),
                    region: "Europe".into(),
                    status: "waiting".into(),
                    phase: "agent".into(),
                    error: String::new(),
                })
                .collect(),
            error: String::new(),
            rollback: String::new(),
        };
        for (width, height) in [(100, 40), (40, 16), (20, 8)] {
            let mut terminal =
                Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
            terminal
                .draw(|frame| draw_update(frame, &snapshot, 0, frame.area()))
                .unwrap();
            let content = format!("{:?}", terminal.backend().buffer());
            assert!(!content.contains("Node10"));
        }
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 40)).unwrap();
        terminal
            .draw(|frame| draw_update(frame, &snapshot, 1, frame.area()))
            .unwrap();
        let content = format!("{:?}", terminal.backend().buffer());
        assert!(content.contains("Node10"));
        assert!(!content.contains("Node00"));
    }
    #[test]
    fn rendering_resizes_and_hides_secrets() {
        let r = Request {
            action: Action::Install,
            host: "vps.example".into(),
            port: 22,
            user: "root".into(),
            state_dir: std::path::PathBuf::new(),
            tag: String::new(),
            branch: "dev".into(),
            admin_ids: "42".into(),
            bot_token: Zeroizing::new(String::new()),
            workflow: WorkflowOptions::default(),
        };
        let mut app = App::new(&r);
        app.form.fields[6] = "VERY_SECRET_TOKEN".into();
        for (width, height) in [(80, 24), (120, 40), (40, 16), (20, 8)] {
            let mut terminal =
                Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
            terminal
                .draw(|f| {
                    draw(f, &app);
                })
                .unwrap();
            let content = format!("{:?}", terminal.backend().buffer());
            assert!(!content.contains("VERY_SECRET_TOKEN"));
        }
    }
    #[test]
    fn form_navigation_and_prompt_cancel() {
        let r = Request {
            action: Action::Diagnose,
            host: String::new(),
            port: 22,
            user: "root".into(),
            state_dir: std::path::PathBuf::new(),
            tag: String::new(),
            branch: "dev".into(),
            admin_ids: String::new(),
            bot_token: Zeroizing::new(String::new()),
            workflow: WorkflowOptions::default(),
        };
        let mut app = App::new(&r);
        app.form.previous();
        assert_eq!(app.form.selected, app.form.count() - 1);
        app.form.next();
        assert_eq!(app.form.selected, 0);
        let (tx, rx) = mpsc::channel();
        app.prompt = Some((
            Prompt::HostKey {
                host: "h".into(),
                fingerprint: "f".into(),
            },
            tx,
        ));
        app.prompt_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(matches!(rx.recv().unwrap(), Answer::Confirm(false)));
    }
    fn test_request() -> Request {
        Request {
            action: Action::Update,
            host: "controller.example".into(),
            port: 22,
            user: "root".into(),
            state_dir: std::path::PathBuf::new(),
            tag: String::new(),
            branch: String::new(),
            admin_ids: String::new(),
            bot_token: Zeroizing::new(String::new()),
            workflow: WorkflowOptions::default(),
        }
    }
    fn click(app: &mut App, point: (u16, u16)) -> Option<KeyEvent> {
        app.mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: point.0,
            row: point.1,
            modifiers: KeyModifiers::NONE,
        })
    }
    fn test_profile_app() -> App {
        let mut app = App::new(&test_request());
        let profile = saved_installation("controller.example");
        app.connections.installations.push(profile.clone());
        app.connections.selected = Some(profile.id);
        app.form.use_installation(&profile);
        app.quick_focus = false;
        app
    }
    #[test]
    fn result_close_returns_to_actions_for_success_and_failure_and_preserves_profile() {
        for action in Action::ALL {
            for outcome in [Ok("Done".into()), Err("Unconfirmed operation".into())] {
                let mut app = test_profile_app();
                app.form.select_action(action);
                let profile_id = app.connections.selected;
                let saved = app.form.saved.clone();
                app.stage = "Previous step".into();
                app.error = "Previous error".into();
                app.result_scroll = 12;
                app.password.push_str("SECRET_PASSWORD");
                app.form.fields[6] = "SECRET_TOKEN".into();
                app.event(Event::Finished(outcome));
                let mut terminal =
                    Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
                terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
                let close = app
                    .hits
                    .iter()
                    .find(|hit| matches!(hit.control, Control::Close))
                    .unwrap()
                    .area;
                let key = click(&mut app, (close.x + 1, close.y + 1)).unwrap();
                assert!(app.close_result(key));
                assert!(matches!(app.screen, Screen::Form));
                assert!(!app.exit && !app.quick_focus);
                assert_eq!(app.connections.selected, profile_id);
                assert_eq!(app.form.saved, saved);
                assert_eq!(app.form.selected, 0);
                assert!(app.outcome.is_none() && app.update.is_none());
                assert!(app.stage.is_empty() && app.error.is_empty());
                assert!(app.password.is_empty() && app.form.fields[6].is_empty());
                assert_eq!(app.result_scroll, 0);
                terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
                assert!(
                    app.hits
                        .iter()
                        .any(|hit| matches!(hit.control, Control::Action(_)))
                );
            }
        }
    }

    #[test]
    fn result_escape_returns_home_and_ctrl_c_requires_exit_confirmation() {
        let mut app = test_profile_app();
        app.event(Event::Finished(Ok("Done".into())));
        assert!(!app.close_result(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)));
        assert!(app.exit && !app.exit_confirm && matches!(app.screen, Screen::Finished));
        app.exit = false;
        assert!(app.close_result(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(matches!(app.screen, Screen::Form) && !app.exit);
    }

    #[test]
    fn subsequent_actions_keep_state_directory_and_stop_signal_without_reusing_operation() {
        let mut app = test_profile_app();
        let directory = tempfile::tempdir().unwrap();
        app.state_dir = directory.path().into();
        app.event(Event::Finished(Ok("Done".into())));
        app.close_result(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        app.form.select_action(Action::Diagnose);
        let mut request = app.next_request();
        app.form.apply(&mut request).unwrap();
        assert_eq!(request.state_dir, directory.path());
        assert!(Arc::ptr_eq(&request.workflow.stop, &app.stop));
        assert!(request.workflow.operation.is_none());
        assert!(request.bot_token.is_empty());
        assert_eq!(request.host, "controller.example");
        assert!(!request.workflow.stop.load(Ordering::Relaxed));
    }
    #[test]
    fn mouse_selects_sidebar_and_fields_and_preserves_hidden_credentials() {
        let mut app = test_profile_app();
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        let action = app
            .hits
            .iter()
            .find(|hit| matches!(hit.control, Control::Action(Action::PrepareNode)))
            .unwrap()
            .area;
        assert!(click(&mut app, (action.x + 1, action.y + 1)).is_none());
        assert!(app.form.action == Action::PrepareNode);
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        let target_position = app
            .form
            .visible()
            .iter()
            .position(|&index| index == 8)
            .unwrap()
            + 1;
        let field = app
            .hits
            .iter()
            .find(|hit| matches!(hit.control, Control::Field(index) if index == target_position))
            .unwrap()
            .area;
        click(&mut app, (field.x + 1, field.y + 1));
        app.form.append("node.example");
        assert_eq!(app.form.fields[8], "node.example");
        assert!(app.form.fields[6].is_empty());
        let button = app
            .hits
            .iter()
            .find(|hit| matches!(hit.control, Control::Continue))
            .unwrap()
            .area;
        assert_eq!(
            click(&mut app, (button.x + 1, button.y + 1)).unwrap().code,
            KeyCode::Enter
        );
        let previous = app.form.fields.clone();
        app.quick_focus = true;
        assert_eq!(
            click(&mut app, (button.x + 1, button.y + 1)).unwrap().code,
            KeyCode::Enter
        );
        assert!(!app.quick_focus);
        app.form.append("not-a-field");
        assert_eq!(app.form.fields, previous);
    }
    #[test]
    fn modal_mouse_controls_cannot_activate_background_or_implicitly_trust_host() {
        let mut app = App::new(&test_request());
        let (tx, rx) = mpsc::channel();
        app.event(Event::Prompt(
            Prompt::HostKey {
                host: "controller.example".into(),
                fingerprint: "SHA256:test".into(),
            },
            tx,
        ));
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        assert!(
            app.hits
                .iter()
                .all(|hit| matches!(hit.control, Control::Prompt(_)))
        );
        assert!(click(&mut app, (2, 5)).is_none());
        assert!(app.prompt.is_some());
        assert!(!app.trust);
        let cancel = app
            .hits
            .iter()
            .find(|hit| matches!(hit.control, Control::Prompt(false)))
            .unwrap()
            .area;
        let key = click(&mut app, (cancel.x + 1, cancel.y + 1)).unwrap();
        app.prompt_key(key);
        assert!(matches!(rx.recv().unwrap(), Answer::Confirm(false)));
    }
    #[test]
    fn selected_fields_remain_clickable_after_resize_and_keyboard_navigation() {
        let mut app = test_profile_app();
        for (width, height) in [(120, 40), (80, 24), (40, 16), (20, 8)] {
            app.form.selected = app.form.visible().len();
            let mut terminal =
                Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
            terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
            if width >= 40 && height >= 16 {
                assert!(
                    app.hits.iter().any(
                        |hit| matches!(hit.control, Control::Field(n) if n == app.form.selected)
                    )
                );
            }
            for hit in &app.hits {
                assert!(hit.area.right() <= width && hit.area.bottom() <= height);
            }
        }
    }
    fn saved_installation(host: &str) -> Installation {
        Installation {
            id: uuid::Uuid::new_v4(),
            name: "Home installation".into(),
            host: host.into(),
            port: 2222,
            user: "admin".into(),
            branch: "dev".into(),
            admin_ids: "42".into(),
            account: "42".into(),
        }
    }
    #[test]
    fn saved_installations_restore_and_hide_filled_fields_without_saving_secrets() {
        let dir = tempfile::tempdir().unwrap();
        let mut request = test_request();
        request.state_dir = dir.path().into();
        request.host.clear();
        let profile = saved_installation("one.example");
        let mut store = Connections {
            selected: Some(profile.id),
            installations: vec![profile.clone()],
            ..Connections::default()
        };
        store.save(dir.path()).unwrap();
        let mut app = App::new(&request);
        app.load_connections(&request).unwrap();
        assert_eq!(app.form.fields[0], "one.example");
        assert_eq!(app.form.visible(), vec![4]);
        app.form.fields[6] = "TOP_SECRET_TOKEN".into();
        app.form.fields[4] = "v0.4.3-alpha.48".into();
        app.remember_form().unwrap();
        let text = std::fs::read_to_string(dir.path().join("installations.json")).unwrap();
        assert!(!text.contains("TOP_SECRET_TOKEN") && !text.contains("v0.4.3-alpha.48"));
        let mut restarted = App::new(&request);
        restarted.load_connections(&request).unwrap();
        assert_eq!(restarted.form.fields[4], "");
        restarted.form.select_action(Action::Diagnose);
        assert_eq!(restarted.form.visible(), vec![11]);
        restarted.form.select_action(Action::Install);
        assert_eq!(restarted.form.visible(), vec![4, 6]);
    }
    #[test]
    fn switching_installations_drops_target_and_secret_and_does_not_reuse_another_update() {
        let mut request = test_request();
        request.workflow.operation = Some(uuid::Uuid::new_v4());
        let mut form = Form::new(&request);
        form.fields[6] = "TOKEN".into();
        form.fields[8] = "previous-target.example".into();
        let profile = saved_installation("other.example");
        form.use_installation(&profile);
        assert!(form.fields[6].is_empty() && form.fields[8].is_empty());
        form.apply(&mut request).unwrap();
        assert!(request.workflow.operation.is_none());
        assert_eq!(request.host, profile.host);
    }
    #[test]
    fn settings_is_anchored_and_navigation_selection_has_no_fill() {
        let mut app = test_profile_app();
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        let settings = app
            .hits
            .iter()
            .find(|hit| matches!(hit.control, Control::Settings))
            .unwrap()
            .area;
        assert_eq!(settings.bottom(), 36);
        let action = app
            .hits
            .iter()
            .find(|hit| matches!(hit.control, Control::Action(Action::Update)))
            .unwrap()
            .area;
        let buffer = terminal.backend().buffer();
        assert_eq!(buffer[(action.x, action.y)].fg, Color::Cyan);
        assert_eq!(buffer[(action.x + 1, action.y + 1)].bg, Color::Reset);
        app.mouse(MouseEvent {
            kind: MouseEventKind::Moved,
            column: settings.x + 1,
            row: settings.y + 1,
            modifiers: KeyModifiers::NONE,
        });
        assert!(matches!(app.screen, Screen::Form));
        assert!(click(&mut app, (settings.x + 1, settings.y + 1)).is_none());
        assert!(matches!(app.screen, Screen::Settings));
        app.navigate(false);
        assert!(matches!(app.screen, Screen::Form) && app.form.action == Action::Install);
    }
    #[test]
    fn installation_editor_can_create_modify_and_select_a_saved_connection() {
        let dir = tempfile::tempdir().unwrap();
        let mut request = test_request();
        request.state_dir = dir.path().into();
        let mut app = App::new(&request);
        app.begin_edit(true);
        app.editor.as_mut().unwrap().1[0] = "Home".into();
        app.save_edit().unwrap();
        let original_id = app.connections.active().unwrap().id;
        app.begin_edit(false);
        app.editor.as_mut().unwrap().1[1] = "changed.example".into();
        app.save_edit().unwrap();
        assert_eq!(app.connections.installations.len(), 1);
        assert_eq!(app.connections.active().unwrap().id, original_id);
        app.use_selected().unwrap();
        assert_eq!(app.form.fields[0], "changed.example");
        assert!(matches!(app.screen, Screen::Form));
        request.host.clear();
        let mut restarted = App::new(&request);
        restarted.load_connections(&request).unwrap();
        assert_eq!(restarted.form.fields[0], "changed.example");
    }
    #[test]
    fn new_profile_disables_actions_and_enter_opens_creation() {
        let mut app = App::new(&test_request());
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        assert!(!app.actions_enabled());
        assert!(app.hits.iter().all(|hit| matches!(
            hit.control,
            Control::QuickProfile | Control::CycleProfile(_) | Control::Settings
        )));
        app.navigate(false);
        assert!(matches!(app.screen, Screen::Settings));
        app.navigate(true);
        assert!(app.quick_focus && matches!(app.screen, Screen::Form));
        app.activate_profile();
        assert!(matches!(app.screen, Screen::EditInstallation));
        assert!(app.editor.as_ref().unwrap().0.is_none());
    }
    #[test]
    fn session_information_is_read_only_and_distinguishes_local_identity() {
        let dir = tempfile::tempdir().unwrap();
        let mut app = test_profile_app();
        app.state_dir = dir.path().into();
        app.form.fields[6] = "SECRET_TOKEN".into();
        app.password = Zeroizing::new("SECRET_PASSWORD".into());
        app.open_settings();
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        let info = app
            .hits
            .iter()
            .find(|hit| matches!(hit.control, Control::SettingsPage(SettingsPage::Session)))
            .unwrap()
            .area;
        click(&mut app, (info.x + 1, info.y + 1));
        assert!(matches!(
            app.screen,
            Screen::SettingsPage(SettingsPage::Session)
        ));
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect();
        assert!(text.contains(&app.session_id.to_string()));
        assert!(text.contains(&app.form.saved.as_ref().unwrap().id.to_string()));
        assert!(text.contains("Administrator selector"));
        assert!(!text.contains("SECRET_TOKEN") && !text.contains("SECRET_PASSWORD"));
        assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 0);
        let back = app
            .hits
            .iter()
            .rev()
            .find(|hit| matches!(hit.control, Control::Settings))
            .unwrap()
            .area;
        click(&mut app, (back.x + 1, back.y + 1));
        assert!(matches!(app.screen, Screen::Settings));
    }
    #[test]
    fn new_profile_defaults_to_dev_and_disabled_actions_are_dimmed() {
        let dir = tempfile::tempdir().unwrap();
        let mut app = test_profile_app();
        app.state_dir = dir.path().into();
        app.form.fields[3] = "main".into();
        app.begin_edit(true);
        assert_eq!(app.editor.as_ref().unwrap().1[4], "dev");
        app.screen = Screen::Form;
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        assert!(
            terminal
                .backend()
                .buffer()
                .content
                .iter()
                .any(|c| c.fg == Color::DarkGray)
        );
        assert!(
            !app.hits
                .iter()
                .any(|hit| matches!(hit.control, Control::Action(_)))
        );
        app.begin_edit(true);
        assert_eq!(app.editor.as_ref().unwrap().1[4], "dev");
    }
    #[test]
    fn quick_switcher_wraps_profiles_and_new_selection_survives_restart() {
        let dir = tempfile::tempdir().unwrap();
        let mut request = test_request();
        request.state_dir = dir.path().into();
        request.host.clear();
        let one = saved_installation("one.example");
        let mut two = saved_installation("two.example");
        two.name = "Second".into();
        let mut store = Connections {
            selected: Some(one.id),
            installations: vec![one.clone(), two.clone()],
            ..Connections::default()
        };
        store.save(dir.path()).unwrap();
        let mut app = App::new(&request);
        app.load_connections(&request).unwrap();
        assert_eq!(app.profile_index(), 0);
        app.form.fields[6] = "SECRET".into();
        app.form.fields[8] = "old-target.example".into();
        app.cycle_profile(false).unwrap();
        assert_eq!(app.form.saved.as_ref().unwrap().id, two.id);
        assert!(app.form.fields[6].is_empty() && app.form.fields[8].is_empty());
        app.cycle_profile(false).unwrap();
        assert!(!app.actions_enabled());
        let mut restarted = App::new(&request);
        restarted.load_connections(&request).unwrap();
        assert!(!restarted.actions_enabled());
        assert!(Connections::load(dir.path()).unwrap().selected.is_none());
        app.cycle_profile(false).unwrap();
        assert_eq!(app.form.saved.as_ref().unwrap().id, one.id);
        app.cycle_profile(true).unwrap();
        assert!(!app.actions_enabled());
        app.activate_profile();
        let editor = app.editor.as_mut().unwrap();
        editor.1[0] = "Third".into();
        editor.1[1] = "three.example".into();
        app.save_edit().unwrap();
        assert!(app.actions_enabled());
        assert_eq!(app.connections.installations.len(), 3);
        assert_eq!(app.connections.active().unwrap().host, "three.example");
    }
    #[test]
    fn quick_switcher_arrow_hits_work_and_compact_sidebar_keeps_all_actions() {
        let dir = tempfile::tempdir().unwrap();
        let mut app = test_profile_app();
        app.state_dir = dir.path().into();
        app.connections.save(dir.path()).unwrap();
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 24)).unwrap();
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        assert_eq!(
            app.hits
                .iter()
                .filter(|hit| matches!(hit.control, Control::Action(_)))
                .count(),
            4
        );
        let right = app
            .hits
            .iter()
            .find(|hit| matches!(hit.control, Control::CycleProfile(false)))
            .unwrap()
            .area;
        assert_eq!(right.bottom(), 17);
        click(&mut app, (right.x + 1, right.y + 1));
        assert!(!app.actions_enabled());
        terminal.draw(|frame| app.hits = draw(frame, &app)).unwrap();
        assert!(
            !app.hits
                .iter()
                .any(|hit| matches!(hit.control, Control::Action(_) | Control::Continue))
        );
        let label = app
            .hits
            .iter()
            .find(|hit| matches!(hit.control, Control::QuickProfile))
            .unwrap()
            .area;
        click(&mut app, (label.x + 1, label.y + 1));
        assert!(matches!(app.screen, Screen::EditInstallation));
    }
}
