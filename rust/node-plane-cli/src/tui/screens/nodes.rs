use super::super::*;

pub(in crate::tui) fn draw_node_editor(
    frame: &mut Frame,
    app: &App,
    area: Rect,
    hits: &mut Vec<Hit>,
) {
    use crate::node_editor::{Editor, Tab};
    let Some(editor) = &app.nodes.editor else {
        return;
    };
    let right = draw_navigation(frame, app, area, hits);
    let block = section_block(format!(
        " {} · Settings ",
        editor.source["title"].as_str().unwrap_or("Server")
    ));
    let inner = block.inner(right);
    frame.render_widget(block, right);
    let sections = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(0),
        Constraint::Length(9),
    ])
    .split(inner);
    let selected = |control| app.nodes_controls().get(app.nodes_selected) == Some(&control);
    let mut buttons = Vec::new();
    if let Some((draft, preview)) = &editor.review {
        let mut lines = vec![Line::styled(
            "Review changes",
            Style::default()
                .fg(Color::Cyan)
                .add_modifier(Modifier::BOLD),
        )];
        for (key, value) in draft.body.as_object().unwrap() {
            if key == "settings" {
                for (name, value) in value.as_object().unwrap() {
                    if &editor.source["settings"][name] != value {
                        lines.push(Line::from(format!(
                            "{name}: {}",
                            crate::access::text(value)
                        )));
                    }
                }
            } else {
                lines.push(Line::from(format!("{key}: {}", crate::access::text(value))));
            }
        }
        let affected = preview["affected_profiles"].as_u64().unwrap_or(0);
        if affected > 0 {
            lines.push(Line::styled(
                format!("Region access changes: {affected} profiles"),
                Style::default().fg(Color::Red),
            ));
        }
        lines.push(Line::from(
            if editor.source["applied_revision"].as_u64().unwrap_or(0) > 0 {
                "Save and apply these changes?"
            } else {
                "Save these settings?"
            },
        ));
        if !app.error.is_empty() {
            lines.push(Line::styled(
                app.error.as_str(),
                Style::default().fg(Color::Red),
            ));
        }
        let content = Rect::new(
            sections[0].x,
            sections[0].y,
            sections[0].width,
            sections[0].height + sections[1].height,
        );
        frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), content);
        buttons.extend([
            ("Confirm", Control::NodesEditConfirm),
            ("Back", Control::NodesEditBack),
        ]);
    } else {
        let tabs = Layout::horizontal([Constraint::Fill(1); 3]).split(sections[0]);
        for (index, tab) in Tab::ALL.iter().enumerate() {
            let control = Control::NodesEditTab(*tab);
            draw_button(frame, tabs[index], tab.label(), selected(control));
            hits.push(Hit {
                area: tabs[index],
                control,
            });
        }
        let content = Layout::vertical([
            Constraint::Min(0),
            Constraint::Length(if app.error.is_empty() { 0 } else { 2 }),
        ])
        .split(sections[1]);
        if !app.error.is_empty() {
            frame.render_widget(
                Paragraph::new(app.error.as_str())
                    .style(Style::default().fg(Color::Red))
                    .wrap(Wrap { trim: false }),
                content[1],
            );
        }
        let fields = editor.field_indices();
        let capacity = usize::from(content[0].height / 3).max(1);
        app.nodes_capacity.set(capacity);
        let pages = if editor.tab == Tab::Protocols {
            (if editor.protocols.iter().any(|p| p == "xray") {
                4usize
            } else {
                2
            })
            .div_ceil(capacity)
        } else {
            fields.len().div_ceil(capacity)
        }
        .max(1);
        let page = editor.page.min(pages - 1);
        if editor.tab == Tab::Protocols {
            let count = if editor.protocols.iter().any(|p| p == "xray") {
                4
            } else {
                2
            };
            let first = app.viewport(page * capacity, count, capacity);
            for (row, index) in (0..count).skip(first).take(capacity).enumerate() {
                let (value, name) = [
                    ("awg", "AmneziaWG"),
                    ("xray", "VLESS"),
                    ("tcp", "TCP"),
                    ("xhttp", "XHTTP"),
                ][index];
                let enabled = if index < 2 {
                    &editor.protocols
                } else {
                    &editor.transports
                }
                .iter()
                .any(|v| v == value);
                let control = Control::NodesEditToggle(index >= 2, index % 2);
                let rect = Rect::new(
                    content[0].x,
                    content[0].y + row as u16 * 3,
                    content[0].width,
                    3,
                );
                if rect.bottom() > content[0].bottom() {
                    break;
                }
                draw_button(
                    frame,
                    rect,
                    &format!("{} {name}", if enabled { "✓" } else { "○" }),
                    selected(control),
                );
                hits.push(Hit {
                    area: rect,
                    control,
                });
            }
        } else {
            let first = app.viewport(page * capacity, fields.len(), capacity);
            for (row, &index) in fields.iter().skip(first).take(capacity).enumerate() {
                let field = &editor.fields[index];
                let rect = Rect::new(
                    content[0].x,
                    content[0].y + row as u16 * 3,
                    content[0].width,
                    3,
                );
                if rect.bottom() > content[0].bottom() {
                    break;
                }
                if let Some(_choices) = Editor::choices(field.key) {
                    let control = Control::NodesEditChoice(index);
                    draw_button(
                        frame,
                        rect,
                        &format!("{}: {}", field.label, field.value),
                        selected(control),
                    );
                    hits.push(Hit {
                        area: rect,
                        control,
                    });
                } else {
                    let control = Control::NodesEditField(index);
                    draw_field(frame, rect, field.label, &field.value, selected(control));
                    hits.push(Hit {
                        area: rect,
                        control,
                    });
                }
            }
        }
        if editor.dirty() {
            buttons.extend([
                ("Save", Control::NodesEditSave),
                ("Reset draft", Control::NodesEditReset),
            ]);
        }
        if editor.page > 0 {
            buttons.push(("Previous page", Control::NodesEditPage(false)));
        }
        if editor.page + 1 < pages {
            buttons.push(("Next page", Control::NodesEditPage(true)));
        }
        buttons.push(("Back", Control::NodesEditBack));
    }
    let cells = action_areas(
        frame.area(),
        sections[2],
        buttons.iter().map(|(_, control)| *control),
        2,
    );
    for (index, (label, control)) in buttons.iter().enumerate() {
        draw_button(frame, cells[index], label, selected(*control));
        hits.push(Hit {
            area: cells[index],
            control: *control,
        });
    }
}

pub(in crate::tui) fn draw_node_diagnostics(
    frame: &mut Frame,
    app: &App,
    area: Rect,
    hits: &mut Vec<Hit>,
) {
    let right = draw_navigation(frame, app, area, hits);
    let title = app
        .nodes
        .card
        .as_ref()
        .map_or("Server", |node| node.title.as_str());
    let block = section_block(format!(" {title} · Diagnostic & recovery "));
    let inner = block.inner(right);
    frame.render_widget(block, right);
    let Some((facts, inventory)) = &app.nodes.diagnostics else {
        return;
    };
    let rows = Layout::vertical([Constraint::Min(1), Constraint::Length(6)]).split(inner);
    let selected = |i| !app.nodes_sidebar && app.nodes_selected == i;
    let mut index = 0;
    let mut actions = Vec::new();
    if let Some((item, action)) = &app.nodes.recovery_confirm {
        let explanation = if action == "resolve" {
            "Recover this operation using the current server state?"
        } else {
            "Check the recorded result of this operation?"
        };
        frame.render_widget(
            Paragraph::new(format!(
                "{} · {}\n{}\n\n{explanation}",
                item["kind"].as_str().unwrap_or("Operation"),
                item["status"].as_str().unwrap_or(""),
                item["error_code"].as_str().unwrap_or("")
            ))
            .wrap(Wrap { trim: false }),
            rows[0],
        );
        actions.push(("Confirm", Control::NodesRecoveryConfirm));
    } else if let Some(item) = &app.nodes.diagnostic_item {
        let hint = match item["next_step"].as_str().unwrap_or("worker") {
            "maintenance" => "Wait for controller maintenance to finish.",
            "child" => "Check the related installation operation.",
            "agent" => "Check the agent connection and installation logs.",
            "bootstrap" => "Check the agent connection, then recheck this setup.",
            "journal" => "Recheck the recorded result before resolving the block.",
            "removal" => "Check which server resources remain after removal.",
            _ => "Wait for the worker or inspect its logs.",
        };
        frame.render_widget(
            Paragraph::new(format!(
                "{} · {}\n{}\nPhase: {}\n\n{hint}\n\nOperation: {}",
                item["kind"].as_str().unwrap_or("Operation"),
                item["status"].as_str().unwrap_or(""),
                item["error_code"].as_str().unwrap_or(""),
                item["phase"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .unwrap_or("—"),
                item["id"].as_str().unwrap_or("")
            ))
            .wrap(Wrap { trim: false }),
            rows[0],
        );
        if let Some(items) = inventory["items"].as_array()
            && let Some(position) = items
                .iter()
                .position(|value| value["id"] == item["id"] && value["kind"] == item["kind"])
        {
            if item["actions"]
                .as_array()
                .is_some_and(|a| a.iter().any(|v| v == "recheck"))
            {
                actions.push(("Recheck", Control::NodesRecovery(position, false)));
            }
            if item["actions"]
                .as_array()
                .is_some_and(|a| a.iter().any(|v| v == "resolve"))
            {
                actions.push(("Resolve block", Control::NodesRecovery(position, true)));
            }
        }
        actions.push(("Refresh", Control::NodesDiagnostics));
    } else {
        let diagnostic_height = facts["checks"]
            .as_array()
            .filter(|items| !items.is_empty())
            .map_or(6, |items| {
                (items.len() + 2).min(usize::from(rows[0].height.saturating_sub(5)))
            }) as u16;
        let content = Layout::vertical([Constraint::Length(diagnostic_height), Constraint::Min(1)])
            .split(rows[0]);
        let lines = if let Some(error) = facts["error_code"].as_str() {
            vec![
                Line::from(format!(
                    "Unavailable · {}",
                    match error {
                        "node_agent_unconfigured" => "Agent is not configured",
                        "node_agent_unavailable" => "Agent is unreachable",
                        _ => "Driver is unreachable",
                    }
                )),
                Line::from("Recovery operations are listed below."),
            ]
        } else if let Some(checks) = facts["checks"].as_array().filter(|items| !items.is_empty()) {
            let mut checks: Vec<_> = checks.iter().collect();
            checks.sort_by_key(|check| match check["status"].as_str() {
                Some("error") => 0,
                Some("warning") => 1,
                _ => 2,
            });
            let capacity = usize::from(content[0].height).saturating_sub(1);
            let visible = if checks.len() > capacity {
                capacity.saturating_sub(1)
            } else {
                capacity
            };
            let mut lines = vec![Line::from(format!(
                "Errors: {} · Warnings: {}",
                checks
                    .iter()
                    .filter(|check| check["status"] == "error")
                    .count(),
                checks
                    .iter()
                    .filter(|check| check["status"] == "warning")
                    .count()
            ))];
            lines.extend(checks.iter().take(visible).map(|check| {
                Line::from(format!(
                    "{} · {}",
                    check["status"].as_str().unwrap_or("unknown").to_uppercase(),
                    check["id"]
                        .as_str()
                        .unwrap_or("Check")
                        .replace(':', " · ")
                        .replace('_', " ")
                ))
            }));
            if checks.len() > visible {
                lines.push(Line::from(format!(
                    "{} more checks · enlarge the terminal or run installation Diagnostics",
                    checks.len() - visible
                )));
            }
            lines
        } else {
            [
                ("docker", "Docker"),
                ("runtime_root", "Runtime files"),
                ("xray_config", "VLESS config"),
                ("awg_config", "AWG config"),
            ]
            .into_iter()
            .map(|(key, label)| {
                Line::from(format!(
                    "{} · {label}",
                    facts[key].as_str().unwrap_or("unknown").to_uppercase()
                ))
            })
            .chain(std::iter::once(Line::from(format!(
                "Version: {}",
                facts["runtime_version"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .unwrap_or("—")
            ))))
            .collect()
        };
        frame.render_widget(Paragraph::new(lines), content[0]);
        let capacity = usize::from(content[1].height).max(1);
        app.nodes_capacity.set(capacity);
        let items = inventory["items"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        let page = app
            .nodes
            .diagnostic_page
            .min(items.len().div_ceil(capacity).saturating_sub(1));
        if items.is_empty() {
            frame.render_widget(Paragraph::new("No operations need attention."), content[1]);
        }
        let first = app.viewport(page * capacity, items.len(), capacity);
        for (position, item) in items.iter().enumerate().skip(first).take(capacity) {
            let y = content[1].y + (position - first) as u16;
            let rect = Rect::new(content[1].x, y, content[1].width, 1);
            frame.render_widget(
                Paragraph::new(format!(
                    "→ {} · {} · {}",
                    item["kind"].as_str().unwrap_or("Operation"),
                    item["status"].as_str().unwrap_or(""),
                    item["error_code"].as_str().unwrap_or("")
                ))
                .style(
                    Style::default()
                        .fg(if selected(index) {
                            Color::Cyan
                        } else {
                            Color::White
                        })
                        .add_modifier(if selected(index) {
                            Modifier::BOLD | Modifier::UNDERLINED
                        } else {
                            Modifier::empty()
                        }),
                ),
                rect,
            );
            hits.push(Hit {
                area: rect,
                control: Control::NodesDiagnosticItem(position),
            });
            index += 1;
        }
        let offset = inventory["offset"].as_u64().unwrap_or(0);
        if page > 0 || offset > 0 {
            actions.push(("←", Control::NodesDiagnosticPage(true)));
        }
        if (page + 1) * capacity < items.len()
            || offset + (items.len() as u64) < inventory["total"].as_u64().unwrap_or(0)
        {
            actions.push(("→", Control::NodesDiagnosticPage(false)));
        }
        actions.push(("Refresh", Control::NodesDiagnostics));
    }
    actions.push(("Back", Control::NodesBack));
    let buttons = action_areas(
        frame.area(),
        rows[1],
        actions.iter().map(|(_, control)| *control),
        2,
    );
    for ((label, control), rect) in actions.into_iter().zip(buttons) {
        draw_button(frame, rect, label, selected(index));
        hits.push(Hit {
            area: rect,
            control,
        });
        index += 1;
    }
}

pub(in crate::tui) fn draw_node_maintenance(
    frame: &mut Frame,
    app: &App,
    area: Rect,
    hits: &mut Vec<Hit>,
) {
    use crate::node_maintenance::Mode;
    let Some(view) = &app.nodes.maintenance else {
        return;
    };
    let right = draw_navigation(frame, app, area, hits);
    let title = app
        .nodes
        .card
        .as_ref()
        .map_or(view.key.as_str(), |node| node.title.as_str());
    let block = section_block(format!(" {title} · Node maintenance "));
    let inner = block.inner(right);
    frame.render_widget(block, right);
    let mut actions = Vec::new();
    if let Some(mode) = view.confirmation {
        actions.push((
            if mode == Mode::Full {
                "Remove server"
            } else {
                "Remove record"
            },
            Control::NodesRemoveConfirm,
            true,
        ));
    } else if !view.removed() {
        if !view.active() {
            actions.push((
                if view.blocked() {
                    "Retry removal"
                } else {
                    "Full cleanup"
                },
                Control::NodesRemove(Mode::Full),
                true,
            ));
        }
        actions.push((
            "Remove record only",
            Control::NodesRemove(Mode::RegistryOnly),
            true,
        ));
        if !view.active()
            && !view.blocked()
            && app.nodes.card.as_ref().is_some_and(|node| {
                node.agent == crate::nodes::AgentState::Ready
                    && !node.protocols.is_empty()
                    && !node.needs_observation(app.nodes.operation.as_ref())
                    && app.nodes.operation.as_ref().is_none_or(|op| {
                        op.node_key != node.key
                            || matches!(op.status.as_str(), "succeeded" | "failed" | "rejected")
                    })
            })
        {
            actions.push((
                "Reinstall · keep configs",
                Control::NodesProtocolAction("reinstall_keep"),
                false,
            ));
            actions.push((
                "Reinstall · new configs",
                Control::NodesProtocolAction("reinstall_clean"),
                true,
            ));
            actions.push((
                "Check ports",
                Control::NodesProtocolAction("check_ports"),
                false,
            ));
        }
        actions.push(("Diagnostic", Control::NodesDiagnostics, false));
        actions.push(("Refresh", Control::NodesRefresh, false));
    }
    actions.push((
        if view.removed() { "Nodes" } else { "Back" },
        Control::NodesBack,
        false,
    ));
    let labels: Vec<_> = actions
        .iter()
        .map(|(label, control, _)| (*label, *control))
        .collect();
    let button_rows = adaptive_button_rows(inner.width, &labels);
    if usize::from(inner.height) < button_rows * 3 + 2 {
        frame.render_widget(
            Paragraph::new("Enlarge the terminal to manage this server."),
            inner,
        );
        return;
    }
    let rows = Layout::vertical([
        Constraint::Min(1),
        Constraint::Length((button_rows * 3) as u16),
    ])
    .split(inner);
    let mut lines = Vec::new();
    if let Some(mode) = view.confirmation {
        lines.push(Line::styled(
            if mode == Mode::Full {
                "Remove this server?"
            } else {
                "Remove only the server record?"
            },
            Style::default().fg(Color::Red).add_modifier(Modifier::BOLD),
        ));
        lines.push(Line::from(""));
    }
    lines.extend(view.lines().into_iter().map(Line::from));
    let paragraph = Paragraph::new(lines).wrap(Wrap { trim: false });
    let maximum = paragraph
        .line_count(rows[0].width)
        .saturating_sub(usize::from(rows[0].height));
    app.register_scroll(
        view.scroll,
        maximum,
        usize::from(rows[0].height),
        ScrollTarget::Maintenance,
    );
    frame.render_widget(
        paragraph.scroll((view.scroll.min(maximum).min(u16::MAX as usize) as u16, 0)),
        rows[0],
    );
    let buttons = action_areas(
        frame.area(),
        rows[1],
        actions.iter().map(|(_, control, _)| *control),
        button_rows,
    );
    for (index, ((label, control, danger), rect)) in actions.into_iter().zip(buttons).enumerate() {
        draw_button_color(
            frame,
            rect,
            label,
            !app.nodes_sidebar && app.nodes_selected == index,
            if danger { Color::Red } else { Color::Cyan },
        );
        hits.push(Hit {
            area: rect,
            control,
        });
    }
}

pub(in crate::tui) fn adaptive_button_rows(width: u16, actions: &[(&str, Control)]) -> usize {
    let columns = if width >= 32 { 2 } else { 1 };
    actions
        .iter()
        .filter(|(_, control)| !is_back_control(*control))
        .count()
        .div_ceil(columns)
        .max(1)
}

pub(in crate::tui) fn node_card_actions(
    node: &crate::nodes::Node,
    operation: Option<&crate::nodes::Operation>,
) -> Vec<(&'static str, Control)> {
    let mut actions = Vec::new();
    let busy = matches!(
        node.state(),
        "deleting" | "deletion_blocked" | "applying" | "needs_attention"
    ) || operation.as_ref().is_some_and(|o| {
        o.node_key == node.key
            && !matches!(
                o.status.as_str(),
                "succeeded" | "failed" | "cancelled" | "rejected"
            )
    });
    if !busy {
        if node.protocols.is_empty() {
            if matches!(
                node.agent,
                crate::nodes::AgentState::Missing | crate::nodes::AgentState::Unknown
            ) {
                actions.push(("Setup agent", Control::NodesSetup));
            }
        } else if node.needs_setup() {
            let label = if node.agent == crate::nodes::AgentState::Ready && node.can_bootstrap() {
                "Install protocols"
            } else {
                "Server setup"
            };
            actions.push((label, Control::NodesBootstrap));
        }
    }
    if !busy {
        actions.push(("Settings", Control::NodesEdit));
        if node.applied_revision > 0
            && node.desired_revision > node.applied_revision
            && !node.protocols.is_empty()
        {
            actions.push(("Apply changes", Control::NodesApply));
        }
    }
    actions.push(("Temporary configs", Control::NodeTemporary));
    actions.push(("Diagnostic", Control::NodesDiagnostics));
    actions.push(("Maintenance", Control::NodesMaintenance));
    actions.extend([
        ("Refresh", Control::NodesRefresh),
        ("Back", Control::NodesBack),
    ]);
    actions
}

pub(in crate::tui) fn draw_nodes(frame: &mut Frame, app: &App, area: Rect, hits: &mut Vec<Hit>) {
    if app.nodes.diagnostics.is_some() {
        draw_node_diagnostics(frame, app, area, hits);
        return;
    }
    if app.nodes.maintenance.is_some() {
        draw_node_maintenance(frame, app, area, hits);
        return;
    }
    if app.nodes.editor.is_some() {
        draw_node_editor(frame, app, area, hits);
        return;
    }
    let right = draw_navigation(frame, app, area, hits);
    let card = app.nodes.card.as_ref();
    let title = app.form.saved.as_ref().map_or("Nodes", |p| p.name.as_str());
    let block = section_block(format!(" {title} · Nodes "));
    let inner = block.inner(right);
    frame.render_widget(block, right);
    if inner.height < 8 || inner.width < 20 {
        frame.render_widget(
            Paragraph::new("Enlarge the terminal to browse nodes."),
            inner,
        );
        return;
    }
    let mut actions = card.map_or_else(Vec::new, |node| {
        node_card_actions(node, app.nodes.operation.as_ref())
    });
    let button_rows = if card.is_some() {
        adaptive_button_rows(inner.width, &actions)
    } else {
        2
    };
    if usize::from(inner.height) < button_rows * 3 + 2 {
        frame.render_widget(
            Paragraph::new("Enlarge the terminal to browse nodes."),
            inner,
        );
        return;
    }
    let rows = Layout::vertical([
        Constraint::Length(if card.is_some() { 0 } else { 3 }),
        Constraint::Min(2),
        Constraint::Length((button_rows * 3) as u16),
    ])
    .split(inner);
    let mut index = 0usize;
    let selected = |index| !app.nodes_sidebar && app.nodes_selected == index;
    if card.is_none() {
        draw_field(
            frame,
            rows[0],
            "Search · name / region / code",
            &app.nodes.search,
            selected(index),
        );
        hits.push(Hit {
            area: rows[0],
            control: Control::NodesSearch,
        });
        index += 1;
    }
    if let Some(node) = card {
        let protocols = node
            .protocols
            .iter()
            .map(|p| if p == "awg" { "AmneziaWG" } else { "VLESS" })
            .collect::<Vec<_>>()
            .join(" · ");
        let mut lines = vec![
            Line::styled(
                format!("{} {} · {}", node.flag, node.region, node.title),
                Style::default()
                    .fg(Color::Cyan)
                    .add_modifier(Modifier::BOLD),
            ),
            Line::from(format!("Code: {} · {}", node.key, node.state_label())),
            Line::from(format!("Agent: {}", node.agent.label())),
            Line::from(format!(
                "Connection: {}{}",
                node.transport.as_deref().unwrap_or("Not configured"),
                node.ssh_target
                    .as_ref()
                    .map(|s| format!(" · {s}"))
                    .unwrap_or_default()
            )),
            Line::from(format!(
                "Protocols: {}",
                if protocols.is_empty() {
                    "Not selected"
                } else {
                    &protocols
                }
            )),
        ];
        if !node.xray_transports.is_empty() {
            lines.push(Line::from(format!(
                "VLESS: {}",
                node.xray_transports.join(" · ")
            )));
        }
        if let Some(overview) = &node.overview {
            lines.extend(
                crate::nodes::traffic_lines(overview)
                    .into_iter()
                    .map(Line::from),
            );
        }
        if let Some(operation) = app
            .nodes
            .operation
            .as_ref()
            .filter(|o| o.node_key == node.key)
        {
            lines.push(Line::from(""));
            lines.push(Line::from(format!(
                "{}: {}{}",
                operation.label(),
                operation.status,
                if operation.error.is_empty() {
                    String::new()
                } else {
                    format!(" · {}", operation.error)
                }
            )));
            lines.push(Line::from(operation.id.to_string()));
        }
        if let Some(facts) = &node.services {
            lines.push(Line::from(format!(
                "Docker: {} · AWG: {} · VLESS: {}",
                if facts["docker"] == true {
                    "ready"
                } else {
                    "missing"
                },
                node.protocol_status("awg"),
                node.protocol_status("xray")
            )));
        }
        let paragraph = Paragraph::new(lines).wrap(Wrap { trim: false });
        let maximum = paragraph
            .line_count(rows[1].width)
            .saturating_sub(usize::from(rows[1].height));
        app.register_scroll(
            app.nodes.card_scroll,
            maximum,
            usize::from(rows[1].height),
            ScrollTarget::NodeCard,
        );
        frame.render_widget(
            paragraph.scroll((
                app.nodes.card_scroll.min(maximum).min(u16::MAX as usize) as u16,
                0,
            )),
            rows[1],
        );
    } else {
        let capacity = usize::from(rows[1].height).max(2);
        app.nodes_capacity.set(capacity);
        let pages = app.nodes.pages(capacity);
        app.content_overflow.set(pages.len() > 1);
        let page = app.nodes.page.min(pages.len() - 1);
        if !app.nodes.loaded {
            frame.render_widget(Paragraph::new("Open Nodes to load the registry.\nRefresh connects to the selected installation."), rows[1]);
        } else if pages[page].is_empty() {
            frame.render_widget(
                Paragraph::new(if app.nodes.items.is_empty() {
                    "No registered servers. Choose New node to create one."
                } else {
                    "No servers match this search."
                })
                .wrap(Wrap { trim: false }),
                rows[1],
            );
        } else {
            let all_rows: Vec<_> = pages.iter().flatten().collect();
            let start = pages.iter().take(page).map(Vec::len).sum();
            let first = app.viewport(start, all_rows.len(), capacity);
            for (offset, row) in all_rows.iter().skip(first).take(capacity).enumerate() {
                let rect = Rect::new(rows[1].x, rows[1].y + offset as u16, rows[1].width, 1);
                let (text, control, color) = match row {
                    crate::nodes::Row::Region(region, count) => {
                        let node_index = app
                            .nodes
                            .items
                            .iter()
                            .position(|n| &n.region == region)
                            .unwrap();
                        (
                            format!(
                                "{} {region} · {count}",
                                if app.nodes.collapsed.contains(region) {
                                    "▸"
                                } else {
                                    "▾"
                                }
                            ),
                            Control::NodesRegion(node_index),
                            Color::Cyan,
                        )
                    }
                    crate::nodes::Row::Node(node_index) => {
                        let node = &app.nodes.items[*node_index];
                        (
                            format!(
                                "  › {} {} · {} · {}",
                                node.flag,
                                node.title,
                                node.key,
                                node.state_label()
                            ),
                            Control::NodesCard(*node_index),
                            Color::White,
                        )
                    }
                };
                frame.render_widget(
                    Paragraph::new(text).style(Style::default().fg(color).add_modifier(
                        if selected(index) {
                            Modifier::REVERSED
                        } else {
                            Modifier::empty()
                        },
                    )),
                    rect,
                );
                hits.push(Hit {
                    area: rect,
                    control,
                });
                index += 1;
            }
        }
        if pages.len() > 1 {
            if page > 0 {
                actions.push(("←", Control::NodesPage(true)));
            }
            if page + 1 < pages.len() {
                actions.push(("→", Control::NodesPage(false)));
            }
            frame.render_widget(
                Paragraph::new(format!(" {}/{} ", page + 1, pages.len()))
                    .alignment(ratatui::layout::Alignment::Right)
                    .style(Style::default().fg(Color::Gray)),
                Rect::new(
                    rows[0].right().saturating_sub(10),
                    rows[0].y,
                    9.min(rows[0].width),
                    1,
                ),
            );
        }
        actions.extend([
            ("Refresh", Control::NodesRefresh),
            ("New node", Control::NodesNew),
        ]);
    }
    let areas = action_areas(
        frame.area(),
        rows[2],
        actions.iter().map(|(_, control)| *control),
        button_rows,
    );
    for (position, (label, control)) in actions.into_iter().enumerate() {
        draw_button(frame, areas[position], label, selected(index));
        hits.push(Hit {
            area: areas[position],
            control,
        });
        index += 1;
    }
}

pub(in crate::tui) fn draw_node_wizard(
    frame: &mut Frame,
    app: &App,
    area: Rect,
    hits: &mut Vec<Hit>,
) {
    use crate::node_wizard::Step;
    let Some(wizard) = &app.node_wizard else {
        return;
    };
    let right = draw_navigation(frame, app, area, hits);
    let right = if wizard.field().is_some() {
        let popup = centered(right, 76, 13);
        frame.render_widget(Clear, popup);
        popup
    } else {
        right
    };
    let outer = section_block(format!(" New node · {} ", wizard.title()));
    let inner = outer.inner(right);
    frame.render_widget(outer, right);
    let rows = Layout::vertical([
        Constraint::Length(2),
        Constraint::Min(1),
        Constraint::Length(3),
    ])
    .split(inner);
    let note = match wizard.step {
        Step::Transport if !wizard.options.local_available => {
            "A local node already exists. Add another host using SSH."
        }
        Step::Transport => "Local runs beside the controller. SSH uses a separate VPS.",
        Step::Template => "Presets fill the name, region, flag and next available server code.",
        Step::Target => {
            "Root access or passwordless sudo is required. Example: root@node.example.com"
        }
        Step::Host => "VPN address: a domain or IP. Examples: vpn.example.com or 203.0.113.10",
        Step::Protocols => "Select protocols, or leave both off for an agent-only node.",
        Step::Created => "The node is saved. Open its card to install the agent and VPN protocols.",
        _ => "Back keeps your inputs. Creation does not install anything on the host.",
    };
    frame.render_widget(
        Paragraph::new(note)
            .wrap(Wrap { trim: true })
            .style(Style::default().fg(Color::Gray)),
        rows[0],
    );
    let choices = wizard.choices();
    if matches!(wizard.step, Step::Review) {
        frame.render_widget(
            Paragraph::new(wizard.summary()).wrap(Wrap { trim: false }),
            rows[1],
        );
    } else if wizard.step == Step::Created {
        frame.render_widget(
            Paragraph::new(format!(
                "{}
Code: {}",
                wizard.title, wizard.key
            ))
            .style(Style::default().fg(Color::Cyan)),
            rows[1],
        );
    } else {
        let capacity = (rows[1].height / 3).max(1) as usize;
        app.content_overflow.set(choices.len() > capacity);
        let first = wizard
            .selected
            .min(choices.len().saturating_sub(1))
            .saturating_sub(capacity - 1);
        let first = app.viewport(first, choices.len(), capacity);
        for (index, label) in choices.iter().enumerate().skip(first).take(capacity) {
            let area = Rect::new(
                rows[1].x,
                rows[1].y + ((index - first) * 3) as u16,
                rows[1].width,
                3.min(rows[1].height),
            );
            draw_button(frame, area, label, wizard.selected == index);
            hits.push(Hit {
                area,
                control: Control::WizardChoice(index),
            });
        }
        if let Some(text) = wizard.field() {
            let area = if choices.is_empty() {
                Rect::new(rows[1].x, rows[1].y, rows[1].width, 3.min(rows[1].height))
            } else {
                Rect::new(
                    rows[1].x,
                    rows[1].bottom().saturating_sub(3),
                    rows[1].width,
                    3.min(rows[1].height),
                )
            };
            draw_field(frame, area, wizard.title(), text, true);
        }
    }
    let buttons = if wizard.step == Step::Created {
        vec![
            ("Open node card", Control::WizardOpen),
            ("Nodes", Control::WizardCancel),
        ]
    } else if wizard.selection_step() {
        vec![
            ("← Back", Control::WizardBack),
            ("Cancel", Control::WizardCancel),
        ]
    } else {
        vec![
            (
                if wizard.step == Step::Review {
                    "Create node"
                } else if wizard.step == Step::Flag {
                    "Next / Skip"
                } else {
                    "Continue"
                },
                Control::WizardNext,
            ),
            ("← Back", Control::WizardBack),
            ("Cancel", Control::WizardCancel),
        ]
    };
    let columns = action_areas(
        frame.area(),
        rows[2],
        buttons.iter().map(|(_, control)| *control),
        1,
    );
    for (index, (label, control)) in buttons.into_iter().enumerate() {
        let selected = wizard.selected == choices.len() + index;
        draw_button(frame, columns[index], label, selected);
        hits.push(Hit {
            area: columns[index],
            control,
        });
    }
}
