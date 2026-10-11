use super::super::*;

pub(in crate::tui) fn draw_temporary(
    frame: &mut Frame,
    app: &App,
    area: Rect,
    hits: &mut Vec<Hit>,
) {
    use crate::temporary::View;
    let right = draw_navigation(frame, app, area, hits);
    let block = section_block(" Temporary configurations ");
    let inner = block.inner(right);
    frame.render_widget(block, right);
    let rows = Layout::vertical([
        Constraint::Length(2),
        Constraint::Min(1),
        Constraint::Length(6),
    ])
    .split(inner);
    let capacity = usize::from(rows[1].height).max(1);
    app.temporary.capacity.set(capacity);
    let browser = &app.temporary;
    let selected = |i| !app.temporary_sidebar && browser.selected == i;
    let mut choices: Vec<(String, Control)> = Vec::new();
    let mut actions: Vec<(&str, Control)> = Vec::new();
    let mut lines: Vec<Line> = Vec::new();
    frame.render_widget(
        Paragraph::new(
            browser
                .filter
                .as_ref()
                .map(|n| n.title.as_str())
                .unwrap_or("All servers")
                .to_owned()
                + "\n"
                + &browser.notice,
        ),
        rows[0],
    );
    match browser.view {
        View::List => {
            for (i, item) in browser.items.iter().enumerate() {
                choices.push((
                    format!(
                        "{} · {} {} · {} · {}",
                        item["node_title"]
                            .as_str()
                            .or_else(|| item["node_key"].as_str())
                            .unwrap_or(""),
                        if item["protocol"] == "xray" {
                            "VLESS"
                        } else {
                            "AWG"
                        },
                        item["transport"].as_str().unwrap_or(""),
                        item["status"].as_str().unwrap_or("unknown"),
                        item["expires_at"].as_str().unwrap_or("Pending expiry")
                    ),
                    Control::TemporaryChoice(i),
                ));
            }
            if choices.is_empty() {
                lines.push(Line::from("No active configurations."));
            }
            actions.extend([
                ("Create", Control::TemporaryNew),
                ("Refresh", Control::TemporaryRefresh),
            ]);
            if browser.page > 0 {
                actions.push(("←", Control::TemporaryPage(true)));
            }
            if (browser.page + 1) * capacity < browser.total {
                actions.push(("→", Control::TemporaryPage(false)));
            }
        }
        View::Server => {
            for (i, node) in browser.nodes.iter().enumerate() {
                choices.push((
                    format!("{} {} · {}", node.flag, node.title, node.region),
                    Control::TemporaryChoice(i),
                ));
            }
            if choices.is_empty() {
                lines.push(Line::from("No ready servers."));
            }
            if browser.page > 0 {
                actions.push(("←", Control::TemporaryPage(true)));
            }
            if (browser.page + 1) * capacity < browser.nodes.len() {
                actions.push(("→", Control::TemporaryPage(false)));
            }
        }
        View::Protocol => {
            if let Some(node) = &browser.node {
                for (i, p) in node.protocols.iter().enumerate() {
                    choices.push((
                        if p == "xray" {
                            "VLESS".into()
                        } else {
                            "AmneziaWG".into()
                        },
                        Control::TemporaryChoice(i),
                    ));
                }
            }
        }
        View::Transport => {
            if let Some(node) = &browser.node {
                for (i, p) in node.xray_transports.iter().enumerate() {
                    choices.push((p.to_uppercase(), Control::TemporaryChoice(i)));
                }
            }
        }
        View::Duration => {
            for (i, label) in ["12 hours", "1 day", "3 days"].iter().enumerate() {
                choices.push(((*label).into(), Control::TemporaryChoice(i)));
            }
        }
        View::Confirm => {
            lines.push(Line::from(format!(
                "{} · {} {} · {}h",
                browser
                    .node
                    .as_ref()
                    .map(|n| n.title.as_str())
                    .unwrap_or(""),
                browser.protocol,
                browser.transport,
                browser.seconds / 3600
            )));
            lines.push(Line::from(""));
            if browser.protocol == "xray" {
                lines.push(Line::from(crate::temporary::NOTICE));
            }
            actions.push(("Create", Control::TemporaryConfirm));
        }
        View::Card | View::Revoke | View::Artifact => {
            if let Some(card) = &browser.card {
                lines.push(Line::from(format!(
                    "{} · {} {}",
                    card["node_title"]
                        .as_str()
                        .or_else(|| card["node_key"].as_str())
                        .unwrap_or(""),
                    if card["protocol"] == "xray" {
                        "VLESS"
                    } else {
                        "AWG"
                    },
                    card["transport"].as_str().unwrap_or("")
                )));
                lines.push(Line::from(format!(
                    "Status: {}",
                    card["status"].as_str().unwrap_or("unknown")
                )));
                if let Some(expiry) = card["expires_at"].as_str() {
                    lines.push(Line::from(format!("Expires: {expiry}")));
                }
                if card["unavailable_reason"] == "node_changed" {
                    lines.push(Line::from(
                        "Server settings changed. Revoke this configuration and issue a new one.",
                    ));
                }
                if card["protocol"] == "xray" {
                    lines.push(Line::from(crate::temporary::NOTICE));
                }
                match browser.view {
                    View::Revoke => {
                        lines.push(Line::from("Revoke access to this configuration?"));
                        actions.push(("Revoke", Control::TemporaryConfirm));
                    }
                    View::Artifact => {
                        if browser.artifact.is_some() {
                            actions.push(("Copy URI", Control::TemporaryCopy));
                            if app.image_picker.is_some() {
                                actions.push(("QR code", Control::TemporaryQr));
                            }
                            actions.push(("Save files", Control::TemporarySave));
                        }
                    }
                    _ => {
                        if card["status"] == "active" && card["artifact_available"] != false {
                            actions.push(("Show configuration", Control::TemporaryShow));
                        }
                        if !["expired", "revoked", "cancelled", "revoking"]
                            .contains(&card["status"].as_str().unwrap_or(""))
                        {
                            actions.push(("Revoke early", Control::TemporaryRevoke));
                        }
                        actions.push(("Refresh", Control::TemporaryRefresh));
                    }
                }
            }
        }
    }
    if browser.view != View::List {
        actions.push(("Back", Control::TemporaryBack));
    }
    if !choices.is_empty() {
        app.content_overflow.set(choices.len() > capacity);
        let first = browser
            .selected
            .min(choices.len() - 1)
            .saturating_add(1)
            .saturating_sub(capacity)
            .min(choices.len().saturating_sub(capacity));
        let first = if browser.view == View::Server && browser.selected == 0 {
            browser.page * capacity
        } else {
            first
        };
        let first = app.viewport(first, choices.len(), capacity);
        for (index, (label, control)) in choices.iter().enumerate().skip(first).take(capacity) {
            let i = index - first;
            let rect = Rect::new(rows[1].x, rows[1].y + i as u16, rows[1].width, 1);
            frame.render_widget(
                Paragraph::new(label.as_str()).style(Style::default().fg(if selected(index) {
                    Color::Cyan
                } else {
                    Color::White
                })),
                rect,
            );
            hits.push(Hit {
                area: rect,
                control: *control,
            });
        }
    } else if app.qr_visible && browser.view == View::Artifact {
        let layout = Layout::vertical([Constraint::Length(lines.len() as u16), Constraint::Min(0)])
            .split(rows[1]);
        frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), layout[0]);
        if let Some(qr) = app.qr.borrow_mut().as_mut() {
            draw_qr(frame, layout[1], qr);
        }
    } else {
        let paragraph = Paragraph::new(lines).wrap(Wrap { trim: false });
        let maximum = paragraph
            .line_count(rows[1].width)
            .saturating_sub(usize::from(rows[1].height));
        app.register_scroll(
            usize::from(browser.scroll),
            maximum,
            usize::from(rows[1].height),
            ScrollTarget::Temporary,
        );
        frame.render_widget(
            paragraph.scroll((browser.scroll.min(maximum.min(u16::MAX as usize) as u16), 0)),
            rows[1],
        );
    }
    let areas = action_areas(
        frame.area(),
        rows[2],
        actions.iter().map(|(_, control)| *control),
        2,
    );
    // Keep action indexes after every choice, including choices outside the viewport.
    for (i, (label, control)) in actions.into_iter().enumerate() {
        let index = choices.len() + i;
        draw_button_color(
            frame,
            areas[i],
            label,
            selected(index),
            if label.starts_with("Revoke") {
                Color::Red
            } else {
                Color::Cyan
            },
        );
        hits.push(Hit {
            area: areas[i],
            control,
        });
    }
}
