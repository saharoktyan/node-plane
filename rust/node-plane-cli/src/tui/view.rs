//! Screen composition, header, dialogs and focus decoration.
use super::*;

pub(super) fn draw_qr(frame: &mut Frame, area: Rect, image: &image::DynamicImage) {
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
pub(super) fn terminal_too_small(area: Rect) -> bool {
    area.width < 70 || area.height < 30
}

pub(super) fn draw(frame: &mut Frame, app: &App) -> Vec<Hit> {
    let area = frame.area();
    if app.restart_required {
        return draw_restart(frame, app);
    }
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

pub(super) fn draw_exit_dialog(frame: &mut Frame, app: &App, hits: &mut Vec<Hit>) {
    let area = frame.area();
    hits.clear();
    let popup = centered(area, 78, 12);
    let sections = dialog(frame, popup, " Close the workstation? ");
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
        "Close the workstation? Unsaved installation changes will be discarded."
    } else {
        "Close the workstation?"
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

pub(super) fn apply_theme(frame: &mut Frame, app: &App) {
    let selected = app
        .connections
        .theme
        .as_ref()
        .and_then(|id| app.themes.iter().find(|theme| &theme.id == id));
    theme::apply(frame, &app.connections.accent, selected);
}

pub(super) fn draw_content(frame: &mut Frame, app: &App) -> Vec<Hit> {
    if app.restart_required {
        return draw_restart(frame, app);
    }
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
        Paragraph::new("Node Plane | Workstation").style(
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
            let (title, description) = app.workflow_confirmation();
            let popup = centered(rows[1], 80, 16);
            let sections = dialog(frame, popup, &format!(" {title} "));
            frame.render_widget(
                Paragraph::new(description).wrap(Wrap { trim: false }),
                sections[0],
            );
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
                Paragraph::new(diagnostic_lines(&summary))
                    .style(Style::default().fg(color))
                    .wrap(Wrap { trim: false }),
                sections[0],
            );
            let paragraph = Paragraph::new(diagnostic_lines(&details)).wrap(Wrap { trim: false });
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
                "Click: select   Tab: move   Arrows: actions / fields   Enter: continue   Esc: sidebar / exit"
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
    if let Some(control) = back.filter(|_| {
        app.controller.defaults_input.is_none() || !matches!(app.screen, Screen::Controller)
    }) {
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
        let title = match prompt {
            Prompt::SelectAdministrator { .. } => "Choose administrator",
            Prompt::ConfirmAction { title, .. } => title,
            _ => "Connection confirmation",
        };
        let sections = dialog(frame, popup, &format!(" {title} "));
        let text = match prompt {
            Prompt::SelectAdministrator { .. } => "Select the account to bind to this workstation key.\n↑ / ↓ · Enter to select · Esc to cancel".into(),
            Prompt::ConfirmAction { description, .. } => description.clone(),
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
            let chosen = choice_selected(&app.controller, hit.control);
            for y in rect.y..rect.bottom() {
                for x in rect.x..rect.right() {
                    let cell = &mut frame.buffer_mut()[(x, y)];
                    if matches!(
                        cell.symbol(),
                        "┌" | "┐" | "└" | "┘" | "╭" | "╮" | "╰" | "╯" | "─" | "│"
                    ) {
                        cell.fg = Color::White;
                    } else if !chosen
                        && y > rect.y
                        && y + 1 < rect.bottom()
                        && cell.fg == Color::Cyan
                    {
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
        let navigation = app.idle_controls();
        navigation
            .get(app.idle_selection.min(navigation.len().saturating_sub(1)))
            .copied()
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
pub(super) fn result_sections(text: &str, diagnose: bool) -> (String, String) {
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
pub(super) fn preparation_step(action: Action, stage: &str) -> (usize, usize) {
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
pub(super) fn draw_navigation(
    frame: &mut Frame,
    app: &App,
    area: Rect,
    hits: &mut Vec<Hit>,
) -> Rect {
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
    let profile_focused = matches!(app.screen, Screen::Form) && app.quick_focus;
    let flash = app
        .profile_flash
        .filter(|(_, at)| at.elapsed() < Duration::from_millis(100))
        .map(|(left, _)| left);
    draw_navigation_button(frame, parts[0], "←", flash == Some(true));
    draw_navigation_button(
        frame,
        parts[1],
        app.form
            .saved
            .as_ref()
            .map_or("New profile", |p| p.name.as_str()),
        profile_focused,
    );
    draw_navigation_button(frame, parts[2], "→", flash == Some(false));
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

fn draw_restart(frame: &mut Frame, app: &App) -> Vec<Hit> {
    let popup = centered(frame.area(), 70, 12);
    let sections = dialog(frame, popup, " Workstation updated ");
    frame.render_widget(
        Paragraph::new("Restart the workstation to use the updated version.")
            .wrap(Wrap { trim: false }),
        sections[0],
    );
    let area = centered(sections[1], 20, 3);
    draw_button(frame, area, "Restart", true);
    apply_theme(frame, app);
    vec![Hit {
        area,
        control: Control::Restart,
    }]
}

pub(super) fn diagnostic_lines(text: &str) -> Vec<Line<'_>> {
    text.lines()
        .map(|line| {
            let prefix = line.trim_start();
            let color = if prefix.starts_with("[OK]") {
                Some(Color::Green)
            } else if prefix.starts_with("[FAIL]") || prefix.starts_with("[ERROR]") {
                Some(Color::Red)
            } else if prefix.starts_with("[WARN]") || prefix.starts_with("[WARNING]") {
                Some(Color::Yellow)
            } else {
                None
            };
            if let Some(color) = color {
                let start = line.len() - prefix.len();
                let end = start + prefix.find(']').unwrap() + 1;
                Line::from(vec![
                    Span::raw(&line[..start]),
                    Span::styled(
                        &line[start..end],
                        Style::default().fg(color).add_modifier(Modifier::BOLD),
                    ),
                    Span::raw(&line[end..]),
                ])
            } else {
                Line::from(line)
            }
        })
        .collect()
}
