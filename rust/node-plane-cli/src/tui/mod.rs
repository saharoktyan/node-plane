mod state;
mod view;
use state::*;
use view::*;

mod actions;
mod app;
mod events;
mod input;
mod screens;
#[cfg(test)]
mod tests;
mod text_input;
mod theme;
mod theme_wizard;
mod widgets;
use text_input::draw_multiline;

use screens::*;
use theme::SECTION_BORDER;
#[cfg(test)]
use theme::accent_color;
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

struct TerminalGuard;
impl TerminalGuard {
    fn enter() -> Result<Self> {
        enable_raw_mode()?;
        if let Err(error) = execute!(
            io::stdout(),
            EnterAlternateScreen,
            EnableBracketedPaste,
            EnableMouseCapture,
            crossterm::cursor::SetCursorStyle::BlinkingBar
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
            LeaveAlternateScreen,
            crossterm::cursor::SetCursorStyle::DefaultUserShape
        );
    }
}

pub fn run(mut request: Request) -> Result<()> {
    let executable = std::env::current_exe()?;
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
    let mut last_presence_check = std::time::Instant::now();
    let mut last_onboarding_poll = std::time::Instant::now();
    let mut last_controller_poll = std::time::Instant::now();
    let mut last_temporary_poll = std::time::Instant::now();
    let mut last_access_poll = std::time::Instant::now();
    let mut last_node_observation = std::time::Instant::now();
    let mut qr_was_visible = false;
    let mut qr_needs_redraw = false;
    let mut previous_accent = app.connections.accent.clone();
    let mut pending_input = None;
    let mut input_timing = None;
    loop {
        app.finish_profile_copy();
        if let Some(answer) = app
            .quick_start_dismiss
            .as_ref()
            .and_then(|receiver| receiver.try_recv().ok())
        {
            app.quick_start_dismiss = None;
            if matches!(answer, Answer::Confirm(true)) {
                if app.controller_ready() {
                    app.controller_command(crate::controller::Command::OnboardingDismiss);
                } else if let Some(key) = app.quick_start_identity() {
                    app.connections.completed_quick_starts.push(key);
                    if let Err(error) = app.connections.save(&app.state_dir) {
                        app.error = error.to_string();
                    }
                    if matches!(app.screen, Screen::QuickStart) {
                        app.screen = app.quick_start_return.take().unwrap_or(Screen::Idle);
                    }
                } else {
                    app.connections.hide_quick_start = true;
                    if let Err(error) = app.connections.save(&app.state_dir) {
                        app.error = error.to_string();
                    }
                    if matches!(app.screen, Screen::QuickStart) {
                        app.screen = app.quick_start_return.take().unwrap_or(Screen::Idle);
                    }
                }
            }
        }
        if app.restart_required {
            terminal.draw(|frame| app.hits = draw(frame, &app))?;
            match event::read()? {
                TermEvent::Key(key)
                    if key.kind != KeyEventKind::Release
                        && matches!(key.code, KeyCode::Enter | KeyCode::Char('r')) =>
                {
                    app.activate(Control::Restart);
                }
                TermEvent::Mouse(mouse) => {
                    app.mouse(mouse);
                }
                _ => {}
            }
            if app.restart_requested {
                break;
            }
            continue;
        }
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
        if app.probe_requested
            && receiver.is_none()
            && !app.controller.verifying
            && !app.nodes_busy
            && app.nodes_requested.is_none()
        {
            app.probe_requested = false;
            app.controller_command(crate::controller::Command::Probe);
        }
        app.ssh_state = nodes_worker.state();
        if last_presence_check.elapsed() >= Duration::from_secs(30)
            && receiver.is_none()
            && app.ssh_state == crate::nodes::ConnectionState::Connected
            && app.has_installation()
            && !app.controller.removal_active()
            && !app.nodes_busy
            && app.nodes_requested.is_none()
            && app.prompt.is_none()
            && !app.exit
        {
            last_presence_check = std::time::Instant::now();
            app.nodes_requested = Some(crate::nodes::Command::Controller(
                app.form.saved.as_ref().map(|profile| profile.id),
                crate::controller::Command::Presence,
            ));
        }
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
            && app.nodes_requested.is_none()
            && matches!(app.screen, Screen::Temporary)
            && last_temporary_poll.elapsed() >= Duration::from_secs(3)
        {
            last_temporary_poll = std::time::Instant::now();
            if let Some(command) = app.temporary.poll() {
                app.temporary_command(command);
            }
        }
        if app.onboarding_poll_ready(last_onboarding_poll.elapsed(), receiver.is_some()) {
            last_onboarding_poll = std::time::Instant::now();
            app.controller.profile = app.form.saved.as_ref().map(|p| p.id);
            app.controller_command(crate::controller::Command::Onboarding);
        }
        if let Some(command) = app.take_browser_request(receiver.is_some()) {
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
                            | crate::nodes::Command::Temporary(crate::temporary::Command::Card(_))
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
                    let presence_check = matches!(
                        command,
                        crate::nodes::Command::Controller(_, crate::controller::Command::Presence)
                    );
                    app.controller_request_context = match &command {
                        crate::nodes::Command::Controller(_, command) => {
                            Some(ControllerRequestContext {
                                action: app.form.action,
                                section: app.controller_section,
                                screen: std::mem::discriminant(&app.screen),
                                revision: app.controller_navigation_revision,
                                read: command.read_only(),
                            })
                        }
                        _ => None,
                    };
                    nodes_worker.submit(next, command, tx)?;
                    receiver = Some(rx);
                    app.nodes_busy = true;
                    app.nodes_background = observing;
                    app.temporary_busy = temporary_command;
                    app.access_busy = access_command;
                    if !app.nodes_background {
                        app.screen = Screen::Running;
                    }
                    if !presence_check {
                        app.error.clear();
                    }
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
            && receiver.is_none()
            && self_confirmation.is_none()
            && let Some(answer) = app.offer_workstation_update()
        {
            offer = false;
            app.self_requested = false;
            self_confirmation = Some(answer);
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
                                app.restart_required = true;
                                app.scrollbar.set(None);
                                app.scroll_drag = None;
                                app.prompt = None;
                                app.exit = false;
                                app.discard = None;
                                app.qr_visible = false;
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
        if app.restart_required {
            continue;
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
            let draw_timing = crate::timings::Stage::new("tui_draw");
            app.restore_navigation_context();
            terminal.draw(|frame| app.hits = draw(frame, &app))?;
            if app.restore_return_focus()
                || app.refresh_scroll_hover()
                || app.restore_action_focus()
            {
                terminal.draw(|frame| app.hits = draw(frame, &app))?;
            }
            qr_needs_redraw = false;
            drop(draw_timing);
            drop(input_timing.take());
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
        if matches!(
            input,
            TermEvent::Key(_)
                | TermEvent::Paste(_)
                | TermEvent::Mouse(crossterm::event::MouseEvent {
                    kind: MouseEventKind::Down(_),
                    ..
                })
        ) {
            input_timing = Some(crate::timings::Stage::new("input_to_next_frame"));
        }
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
                } else if let Some(wizard) = &mut app.theme_wizard {
                    wizard.input(&text);
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
                        == Some(&Control::ControllerAnnouncementInput)
                        && let Some(value) = &mut app.controller.announcement_draft
                    {
                        app.text_input
                            .borrow_mut()
                            .paste("Message", value, &text, 3000);
                    } else if app.controller_controls().get(app.controller.selected)
                        == Some(&Control::ControllerDefaultsInput)
                        && let Some((_, value)) = &mut app.controller.defaults_input
                    {
                        let remaining = 500usize.saturating_sub(value.chars().count());
                        value.extend(text.chars().filter(|c| !c.is_control()).take(remaining));
                    }
                } else if matches!(app.screen, Screen::Access) && !app.exit {
                    if app.access_controls().get(app.access.selected)
                        == Some(&Control::AccessIdentity)
                    {
                        if let Some(draft) = &mut app.access.provision {
                            let remaining = 64usize.saturating_sub(draft.identity.chars().count());
                            draft
                                .identity
                                .extend(text.chars().filter(|c| !c.is_control()).take(remaining));
                        }
                    } else if app.access_controls().get(app.access.selected)
                        == Some(&Control::AccessDurationInput)
                    {
                        if let Some(input) = app
                            .access
                            .editor
                            .as_mut()
                            .and_then(|editor| editor.duration_input.as_mut())
                        {
                            let remaining = 32usize.saturating_sub(input.len());
                            input.extend(
                                text.chars()
                                    .filter(char::is_ascii_alphanumeric)
                                    .take(remaining),
                            );
                        }
                    } else if let Some(Control::AccessField(index)) =
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
                        if matches!(editor.fields[index].key, "notes" | "awg_parameters") {
                            let limit = editor.fields[index].input_limit();
                            app.text_input.borrow_mut().paste(
                                editor.fields[index].label,
                                &mut editor.fields[index].value,
                                &text,
                                limit,
                            );
                        } else {
                            editor.fields[index].append_paste(&text);
                        }
                        editor.changed();
                    }
                } else if matches!(app.screen, Screen::Form) && !app.exit {
                    app.form.append(&text);
                }
            }
            TermEvent::Key(mut key) if key.kind == KeyEventKind::Press => {
                app.normalize_backspace(&mut key);
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
                if app.theme_wizard.is_some() {
                    app.theme_wizard_key(key);
                    continue;
                }
                if app.qr_visible {
                    app.temporary_key(key);
                    continue;
                }
                if key.code == KeyCode::F(1) {
                    app.open_quick_start();
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
                                app.form.fields[3] = crate::config::default_branch().into();
                            }
                            KeyCode::Left | KeyCode::Right | KeyCode::Char(' ')
                                if app.form.selected > 0
                                    && app.form.visible().get(app.form.selected - 1)
                                        == Some(&11) =>
                            {
                                app.toggle_diagnostic_recovery();
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
                            KeyCode::Enter
                                if app.form.action == Action::Diagnose
                                    && app.form.diagnostic_menu
                                    && app.form.selected > 0 =>
                            {
                                app.activate(if app.form.selected == 1 {
                                    Control::DiagnosticOverview
                                } else {
                                    Control::ControllerRecoveryInventory(0)
                                });
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
                    Screen::SettingsPage(SettingsPage::Documentation) => match key.code {
                        KeyCode::Esc => app.open_settings(),
                        KeyCode::Down | KeyCode::Tab => {
                            app.settings_selected =
                                (app.settings_selected + 1).min(DOCUMENTS.len() + 1)
                        }
                        KeyCode::Up | KeyCode::BackTab => {
                            app.settings_selected = app.settings_selected.saturating_sub(1).max(1)
                        }
                        KeyCode::Enter if app.settings_selected <= DOCUMENTS.len() => {
                            app.activate(Control::Documentation(
                                app.settings_selected.saturating_sub(1),
                            ));
                        }
                        KeyCode::Enter => app.open_settings(),
                        _ if quit => {
                            app.exit = true;
                            app.exit_confirm = false;
                        }
                        _ => {}
                    },
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
    if app.restart_requested {
        return crate::self_manage::restart_workstation(&executable, &app.state_dir);
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
