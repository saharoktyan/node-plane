use super::super::*;

pub(in crate::tui) fn draw_running(frame: &mut Frame, app: &App, area: Rect) {
    if app.nodes_busy
        && matches!(app.ssh_state, crate::nodes::ConnectionState::Connected)
        && !app.stage.starts_with("Authorizing")
    {
        let popup = centered(area, 64, 5);
        let block = section_block(" Please wait ");
        let inner = block.inner(popup);
        frame.render_widget(Clear, popup);
        frame.render_widget(block, popup);
        frame.render_widget(
            Paragraph::new(if app.loading_message.is_empty() {
                "Loading…"
            } else {
                &app.loading_message
            })
            .wrap(Wrap { trim: false }),
            inner,
        );
        return;
    }
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
        let title = if app.nodes_busy { " Nodes " } else { title };
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
    let block = section_block(title);
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
        // Reverse accent/default-background colors on the filled label. ANSI
        // reverse video uses the terminal's actual background rather than a
        // guessed RGB value or its default foreground (often white).
        for x in sections[2].left()..sections[2].right() {
            let cell = &mut frame.buffer_mut()[(x, sections[2].top())];
            if cell.bg == Color::Cyan {
                cell.set_style(
                    Style::default()
                        .fg(Color::Cyan)
                        .bg(Color::Reset)
                        .add_modifier(Modifier::REVERSED),
                );
            }
        }
    }
}
pub(in crate::tui) fn draw_update(
    frame: &mut Frame,
    snapshot: &crate::workstation::UpdateSnapshot,
    page: usize,
    area: Rect,
) {
    use ratatui::widgets::{Cell, Row, Table};
    let sections = Layout::vertical([
        Constraint::Length(5),
        Constraint::Length(7),
        Constraint::Min(2),
    ])
    .split(area);
    frame.render_widget(
        Paragraph::new(format!(
            "{}\nOperation: {}\n{} · {}\nController: {}/4 complete{}",
            snapshot.target,
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
        .block(section_block(" Controller stack ")),
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
            .block(section_block(title)),
        sections[2],
    );
}
