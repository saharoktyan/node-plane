use super::super::*;

pub(in crate::tui) const DOCUMENTS: &[(&str, &str, &str)] = &[
    (
        "install",
        "Installation and Quick Start",
        "Set up your first controller and node",
    ),
    (
        "workstation",
        "Workstation",
        "Profiles, SSH connections and installation management",
    ),
    ("telegram", "Telegram bot", "Navigation and daily use"),
    (
        "nodes",
        "Nodes and server setup",
        "Create nodes and install agents and protocols",
    ),
    (
        "protocols",
        "VLESS and AmneziaWG",
        "Protocol settings and client configurations",
    ),
    (
        "access",
        "Accounts, profiles and access",
        "Manage users, devices and permissions",
    ),
    ("settings", "Settings", "Controller defaults and policies"),
    (
        "maintenance",
        "Updates, backups and recovery",
        "Maintain and restore an installation",
    ),
    (
        "temporary-configs",
        "Temporary configurations",
        "Issue access with a limited lifetime",
    ),
    (
        "troubleshooting",
        "Diagnostics and troubleshooting",
        "Investigate connection and installation problems",
    ),
];

pub(in crate::tui) fn draw_documentation(
    frame: &mut Frame,
    app: &App,
    area: Rect,
    hits: &mut Vec<Hit>,
) {
    let right = draw_navigation(frame, app, area, hits);
    let sections = dialog(frame, right, " Documentation ");
    let capacity = usize::from(sections[0].height / 3).max(1);
    let selected = app.settings_selected.saturating_sub(1);
    let offset = app.viewport(
        selected.saturating_add(1).saturating_sub(capacity),
        DOCUMENTS.len(),
        capacity,
    );
    for (index, (_, title, description)) in DOCUMENTS.iter().enumerate().skip(offset).take(capacity)
    {
        let rect = Rect::new(
            sections[0].x,
            sections[0].y + (index - offset) as u16 * 3,
            sections[0].width,
            3,
        );
        draw_field(
            frame,
            rect,
            &format!("↗ {title}"),
            description,
            selected == index,
        );
        hits.push(Hit {
            area: rect,
            control: Control::Documentation(index),
        });
    }
    let back = centered(sections[1], 18, 3);
    draw_button(frame, back, "Back", selected == DOCUMENTS.len());
    hits.push(Hit {
        area: back,
        control: Control::Settings,
    });
}

pub(in crate::tui) fn draw_settings(frame: &mut Frame, app: &App, area: Rect, hits: &mut Vec<Hit>) {
    let right = draw_navigation(frame, app, area, hits);
    dialog(frame, right, " Settings ");
    let content = Block::bordered().inner(right);
    let sections = [content];
    let capacity = usize::from(sections[0].height / 3).max(1);
    let offset = app
        .settings_selected
        .saturating_sub(capacity)
        .min(SettingsPage::ALL.len().saturating_sub(capacity));
    let offset = app.viewport(offset, SettingsPage::ALL.len(), capacity);
    for (index, page) in SettingsPage::ALL
        .iter()
        .copied()
        .enumerate()
        .skip(offset)
        .take(capacity)
    {
        let value = match page {
            SettingsPage::Appearance => format!("Accent: {}", app.connections.accent),
            SettingsPage::Profiles => format!(
                "{} saved installations · select, edit or remove",
                app.connections.installations.len()
            ),
            SettingsPage::Updates => format!(
                "{} · {} · {}",
                crate::config::workstation_channel(),
                env!("CARGO_PKG_VERSION"),
                app.self_status
            ),
            SettingsPage::Security => "SSH identity and trusted hosts".into(),
            SettingsPage::Session => "Session identity and local diagnostic paths".into(),
            SettingsPage::About => "Version and license".into(),
            SettingsPage::Documentation => {
                "Guides and troubleshooting · opens in your browser".into()
            }
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
pub(in crate::tui) fn draw_appearance(
    frame: &mut Frame,
    app: &App,
    area: Rect,
    hits: &mut Vec<Hit>,
) {
    let right = draw_navigation(frame, app, area, hits);
    frame.render_widget(Block::bordered().title(" Appearance · Themes "), right);
    let content = Block::bordered().inner(right);
    let capacity = usize::from(content.height / 3).max(1);
    let selected = app.settings_selected.saturating_sub(1);
    let offset = if selected < app.themes.len() + 2 {
        viewport_start(app.appearance_viewport.get(), selected, capacity)
    } else {
        app.appearance_viewport.get()
    };
    let offset = app.viewport(offset, app.themes.len() + 2, capacity);
    app.appearance_viewport.set(offset);
    let mut entries: Vec<(String, String, Control)> = app
        .themes
        .iter()
        .enumerate()
        .map(|(i, selected)| {
            let name = &selected.name;
            let value = &selected.accent;
            (
                format!(
                    "{}{}",
                    name,
                    if selected.selected(&app.connections) {
                        " · Selected"
                    } else {
                        ""
                    }
                ),
                if value == "terminal" {
                    "Use the terminal's Cyan palette color".into()
                } else {
                    value.to_string()
                },
                Control::Accent(i),
            )
        })
        .collect();
    entries.extend([
        (
            "Custom color · #RRGGBB".into(),
            app.accent_input.clone(),
            Control::AccentInput,
        ),
        (
            "Apply custom color".into(),
            "Saved for all installation profiles".into(),
            Control::SaveAccent,
        ),
    ]);
    for (index, (label, value, control)) in
        entries.into_iter().enumerate().skip(offset).take(capacity)
    {
        let rect = Rect::new(
            content.x,
            content.y + (index - offset) as u16 * 3,
            content.width,
            3,
        );
        if matches!(control, Control::Settings | Control::SaveAccent) {
            draw_button(frame, rect, &label, app.settings_selected == index + 1);
        } else {
            draw_field(
                frame,
                rect,
                &label,
                &value,
                app.settings_selected == index + 1,
            );
        }
        hits.push(Hit {
            area: rect,
            control,
        });
    }
    hits.push(Hit {
        area: pinned_back_area(frame.area()),
        control: Control::Settings,
    });
}
pub(in crate::tui) fn draw_profiles(frame: &mut Frame, app: &App, area: Rect, hits: &mut Vec<Hit>) {
    let right = draw_navigation(frame, app, area, hits);
    let mut sections = dialog(frame, right, " Installation profiles ");
    if !app.connections.installations.is_empty() {
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
        .saturating_sub(capacity)
        .min(app.connections.installations.len().saturating_sub(capacity));
    app.content_overflow
        .set(app.connections.installations.len() > capacity);
    let offset = app.viewport(offset, app.connections.installations.len(), capacity);
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
            (
                if crate::config::workstation_channel() == "dev" {
                    "Copy to Node Plane"
                } else {
                    "Copy to Node Plane (Dev)"
                },
                Control::TransferInstallation,
            ),
            ("Delete", Control::DeleteInstallation),
            ("Back", Control::Settings),
        ]
    } else {
        vec![
            ("New", Control::NewInstallation),
            ("Back", Control::Settings),
        ]
    };
    let buttons = action_areas(
        frame.area(),
        sections[1],
        actions.iter().map(|(_, control)| *control),
        2,
    );
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
pub(in crate::tui) fn draw_settings_info(
    frame: &mut Frame,
    app: &App,
    area: Rect,
    hits: &mut Vec<Hit>,
) {
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

pub(in crate::tui) fn draw_session_info(
    frame: &mut Frame,
    app: &App,
    area: Rect,
    hits: &mut Vec<Hit>,
) {
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
pub(in crate::tui) fn draw_editor(frame: &mut Frame, app: &App, area: Rect, hits: &mut Vec<Hit>) {
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
    let offset = selected
        .min(&6)
        .saturating_add(1)
        .saturating_sub(capacity)
        .min(fields.len().saturating_sub(capacity));
    app.content_overflow.set(fields.len() > capacity);
    let offset = app.viewport(offset, fields.len(), capacity);
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
    let buttons = button_areas(sections[1], 2, 2);
    for (index, (label, control)) in [
        ("Save", Control::SaveInstallation),
        ("Cancel", Control::CancelEdit),
    ]
    .into_iter()
    .enumerate()
    {
        draw_button(frame, buttons[index], label, *selected == index + 7);
        hits.push(Hit {
            area: buttons[index],
            control,
        });
    }
}
