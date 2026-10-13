use super::super::*;

pub(in crate::tui) fn draw_access_grants(
    frame: &mut Frame,
    app: &App,
    sections: [Rect; 3],
    hits: &mut Vec<Hit>,
) {
    use crate::access::text;
    let editor = app.access.editor.as_ref().unwrap();
    if editor.permanent_access {
        frame.render_widget(
            Paragraph::new("Administrator access to all current and future servers is permanent.")
                .wrap(Wrap { trim: false }),
            sections[1],
        );
        let area = centered(sections[2], 18, 3);
        draw_button(frame, area, "Back", true);
        hits.push(Hit {
            area,
            control: Control::AccessGrantBack,
        });
        return;
    }
    let tabs = Layout::horizontal([Constraint::Fill(1); 2]).split(sections[0]);
    for (index, (label, future)) in [("Servers", false), ("Current & future servers", true)]
        .into_iter()
        .enumerate()
    {
        draw_choice_button(
            frame,
            tabs[index],
            label,
            app.access_controls().get(app.access.selected)
                == Some(&Control::AccessGrantMode(future)),
            editor.future_view == future,
        );
        hits.push(Hit {
            area: tabs[index],
            control: Control::AccessGrantMode(future),
        });
    }
    let capacity = usize::from(sections[1].height / 3).max(1);
    app.access_capacity.set(capacity);
    let count = if editor.future_view {
        editor.regions.len() + 1
    } else {
        editor.nodes.len()
    };
    let pages = count.div_ceil(capacity).max(1);
    let page = editor.grants_page.min(pages - 1);
    let first = app.viewport(page * capacity, count, capacity);
    for index in (first..count).take(capacity) {
        let rect = Rect::new(
            sections[1].x,
            sections[1].y + (index - first) as u16 * 3,
            sections[1].width,
            3,
        );
        if rect.bottom() > sections[1].bottom() {
            break;
        }
        let parts = Layout::horizontal([
            Constraint::Fill(2),
            Constraint::Fill(1),
            Constraint::Fill(1),
        ])
        .split(rect);
        let label = if editor.future_view {
            if index == 0 {
                "All regions".into()
            } else {
                text(&editor.regions[index - 1]["title"])
            }
        } else {
            format!(
                "{} · {}",
                text(&editor.nodes[index]["region"]),
                text(&editor.nodes[index]["title"])
            )
        };
        frame.render_widget(Paragraph::new(label).wrap(Wrap { trim: false }), parts[0]);
        for (column, (protocol, name, awg)) in [("xray", "VLESS", false), ("awg", "AWG", true)]
            .into_iter()
            .enumerate()
        {
            if !editor.future_view
                && !editor.nodes[index]["protocols"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|p| p == protocol)
            {
                continue;
            }
            let (granted, control) = if editor.future_view {
                let region = index.checked_sub(1);
                (
                    editor.rule(
                        region.and_then(|i| editor.regions[i]["id"].as_str()),
                        protocol,
                    ),
                    Control::AccessRule(region, awg),
                )
            } else {
                (
                    editor.granted(editor.nodes[index]["key"].as_str().unwrap(), protocol),
                    Control::AccessGrant(index, awg),
                )
            };
            draw_button(
                frame,
                parts[column + 1],
                &format!("[{}] {name}", if granted { "✓" } else { " " }),
                app.access_controls().get(app.access.selected) == Some(&control),
            );
            hits.push(Hit {
                area: parts[column + 1],
                control,
            });
        }
    }
    let mut buttons = vec![
        ("Grant all", Control::AccessGrantAll(true)),
        ("Revoke all", Control::AccessGrantAll(false)),
    ];
    if editor.future_view {
        buttons.clear();
    }
    if page > 0 {
        buttons.push(("Previous page", Control::AccessGrantPage(false)));
    }
    if page + 1 < pages {
        buttons.push(("Next page", Control::AccessGrantPage(true)));
    }
    buttons.push(("Back", Control::AccessGrantBack));
    draw_access_buttons(frame, app, sections[2], &buttons, hits);
}

pub(in crate::tui) fn draw_access_buttons(
    frame: &mut Frame,
    app: &App,
    area: Rect,
    buttons: &[(&str, Control)],
    hits: &mut Vec<Hit>,
) {
    let back = buttons.iter().find(|(label, control)| {
        *label == "Back"
            && *control != Control::AccessWizardBack
            && app.access.editor.is_none()
            && !app.access.creating_profile
            && app.access.decision.is_none()
    });
    let remaining: Vec<_> = buttons
        .iter()
        .copied()
        .filter(|(_, control)| back.is_none_or(|(_, back_control)| control != back_control))
        .collect();
    let buttons = remaining.as_slice();
    let pages: Vec<_> = buttons
        .iter()
        .enumerate()
        .filter(|(_, (_, control))| is_page_control(*control))
        .map(|(index, _)| index)
        .collect();
    let cells = if pages.len() == 2 && buttons.len() > 2 && area.height >= 6 {
        let page_cells = button_areas(Rect::new(area.x, area.y, area.width, 3), 2, 1);
        let mut others = button_areas(
            Rect::new(area.x, area.y + 3, area.width, area.height - 3),
            buttons.len() - 2,
            usize::from((area.height - 3) / 3).max(1),
        )
        .into_iter();
        (0..buttons.len())
            .map(|index| {
                if let Some(slot) = pages.iter().position(|page| *page == index) {
                    page_cells[slot]
                } else {
                    others.next().unwrap()
                }
            })
            .collect()
    } else {
        button_areas(area, buttons.len(), usize::from(area.height / 3).max(1))
    };
    for (index, (label, control)) in buttons.iter().enumerate() {
        let rect = cells[index];
        if rect.height < 3 {
            continue;
        }
        draw_button_color(
            frame,
            rect,
            label,
            app.access_controls().get(app.access.selected) == Some(control),
            if matches!(
                control,
                Control::AccessDeviceDelete
                    | Control::AccessDeviceConfirm
                    | Control::AccessProfileAction(true)
                    | Control::AccessProfileConfirm
            ) {
                Color::Red
            } else {
                Color::Cyan
            },
        );
        hits.push(Hit {
            area: rect,
            control: *control,
        });
    }
    if let Some((_, control)) = back {
        hits.push(Hit {
            area: pinned_back_area(frame.area()),
            control: *control,
        });
    }
}
pub(in crate::tui) fn draw_access_expiry(
    frame: &mut Frame,
    app: &App,
    sections: [Rect; 3],
    hits: &mut Vec<Hit>,
) {
    if let Some(input) = app
        .access
        .editor
        .as_ref()
        .and_then(|e| e.duration_input.as_ref())
    {
        frame.render_widget(
            Paragraph::new("Enter duration: 12H, 7D, 1M or 1Y. M = 30 days; Y = 365 days."),
            sections[0],
        );
        let field = Rect::new(sections[1].x, sections[1].y, sections[1].width, 3);
        draw_field(
            frame,
            field,
            "Duration",
            input,
            app.access_controls().get(app.access.selected) == Some(&Control::AccessDurationInput),
        );
        hits.push(Hit {
            area: field,
            control: Control::AccessDurationInput,
        });
        if !app.error.is_empty() {
            frame.render_widget(
                Paragraph::new(app.error.as_str()).style(Style::default().fg(Color::Red)),
                Rect::new(field.x, field.bottom(), field.width, 2),
            );
        }
        draw_access_buttons(
            frame,
            app,
            sections[2],
            &[
                ("Continue", Control::AccessDurationApply),
                ("Back", Control::AccessExpiryBack),
            ],
            hits,
        );
        return;
    }
    frame.render_widget(
        Paragraph::new("Access duration · starts now").style(Style::default().fg(Color::Cyan)),
        sections[0],
    );
    let choices = [
        ("Unlimited", Control::AccessExpiry(None)),
        ("1 day", Control::AccessExpiry(Some(1))),
        ("7 days", Control::AccessExpiry(Some(7))),
        ("30 days", Control::AccessExpiry(Some(30))),
        ("90 days", Control::AccessExpiry(Some(90))),
        ("Custom duration", Control::AccessDurationCustom),
    ];
    let capacity = (usize::from(sections[1].height / 3).max(1) * 2).min(6);
    app.access_capacity.set(capacity);
    let pages = choices.len().div_ceil(capacity);
    let page = app
        .access
        .editor
        .as_ref()
        .map_or(0, |e| e.expiry_page)
        .min(pages - 1);
    let first = app.viewport(page * capacity, choices.len(), capacity);
    for (index, (label, control)) in choices.iter().skip(first).take(capacity).enumerate() {
        let row = Rect::new(
            sections[1].x,
            sections[1].y + (index / 2) as u16 * 3,
            sections[1].width,
            3,
        );
        if row.bottom() > sections[1].bottom() {
            break;
        }
        let cells = Layout::horizontal([Constraint::Fill(1); 2]).split(row);
        let rect = cells[index % 2];
        draw_button(
            frame,
            rect,
            label,
            app.access_controls().get(app.access.selected) == Some(control),
        );
        hits.push(Hit {
            area: rect,
            control: *control,
        });
    }
    let mut buttons = vec![("Back", Control::AccessExpiryBack)];
    if page > 0 {
        buttons.push(("Previous page", Control::AccessExpiryPage(false)));
    }
    if page + 1 < pages {
        buttons.push(("Next page", Control::AccessExpiryPage(true)));
    }
    draw_access_buttons(frame, app, sections[2], &buttons, hits);
}
pub(in crate::tui) fn draw_access_devices(
    frame: &mut Frame,
    app: &App,
    sections: [Rect; 3],
    hits: &mut Vec<Hit>,
) {
    use crate::access::text;
    let Some(devices) = &app.access.devices else {
        return;
    };
    frame.render_widget(
        Paragraph::new(format!(
            "{} · Devices",
            text(&devices.profile["display_name"])
        ))
        .style(Style::default().fg(Color::Cyan)),
        sections[0],
    );
    let mut buttons = vec![("Refresh", Control::AccessRefresh)];
    if let Some(editor) = &devices.editor {
        let field = Rect::new(
            sections[1].x,
            sections[1].y,
            sections[1].width,
            sections[1].height.min(3),
        );
        draw_field(
            frame,
            field,
            if editor.source.is_some() {
                "Device name"
            } else {
                "New device name"
            },
            &editor.name,
            app.access_controls().get(app.access.selected) == Some(&Control::AccessDeviceName),
        );
        hits.push(Hit {
            area: field,
            control: Control::AccessDeviceName,
        });
        if sections[1].height > 3 {
            frame.render_widget(
                Paragraph::new(app.error.as_str())
                    .style(Style::default().fg(Color::Red))
                    .wrap(Wrap { trim: false }),
                Rect::new(
                    sections[1].x,
                    sections[1].y + 3,
                    sections[1].width,
                    sections[1].height - 3,
                ),
            );
        }
        buttons = vec![
            ("Save", Control::AccessDeviceSave),
            ("Cancel", Control::AccessBack),
        ];
    } else if let Some(card) = &devices.card {
        let mut lines = vec![
            Line::from(text(&card["display_name"])),
            Line::from(format!("Status: {}", text(&card["status"]))),
        ];
        if devices.delete_key.is_some() {
            lines.push(Line::from("Delete this device and revoke its access?"));
            buttons = vec![
                ("Delete", Control::AccessDeviceConfirm),
                ("Cancel", Control::AccessBack),
            ];
        } else {
            if card["status"] == "active" {
                buttons.extend([
                    ("Rename", Control::AccessDeviceEdit),
                    ("Delete", Control::AccessDeviceDelete),
                ]);
            }
            buttons.push(("Back", Control::AccessBack));
        }
        if !app.error.is_empty() {
            lines.push(Line::styled(
                app.error.as_str(),
                Style::default().fg(Color::Red),
            ));
        }
        frame.render_widget(
            Paragraph::new(lines).wrap(Wrap { trim: false }),
            sections[1],
        );
    } else {
        let status = if !app.error.is_empty() {
            app.error.clone()
        } else if !devices.operation.is_null() {
            format!("Access changes: {}", text(&devices.operation["status"]))
        } else {
            app.access.notice.clone()
        };
        let content = Layout::vertical([
            Constraint::Length(if status.is_empty() { 0 } else { 2 }),
            Constraint::Min(0),
        ])
        .split(sections[1]);
        frame.render_widget(
            Paragraph::new(status).wrap(Wrap { trim: false }),
            content[0],
        );
        let capacity = usize::from(content[1].height / 3).max(1);
        app.access_capacity.set(capacity);
        let pages = devices.items.len().div_ceil(capacity).max(1);
        let page = devices.page.min(pages - 1);
        let first = app.viewport(page * capacity, devices.items.len(), capacity);
        for (row, (index, item)) in devices
            .items
            .iter()
            .enumerate()
            .skip(first)
            .take(capacity)
            .enumerate()
        {
            let rect = Rect::new(
                content[1].x,
                content[1].y + row as u16 * 3,
                content[1].width,
                3,
            );
            if rect.bottom() > content[1].bottom() {
                break;
            }
            let control = Control::AccessDeviceItem(index);
            draw_navigation_button(
                frame,
                rect,
                &format!(
                    "{} · {}",
                    text(&item["display_name"]),
                    text(&item["status"])
                ),
                app.access_controls().get(app.access.selected) == Some(&control),
            );
            hits.push(Hit {
                area: rect,
                control,
            });
        }
        if devices.items.is_empty() {
            frame.render_widget(Paragraph::new("No devices."), content[1]);
        }
        if devices.profile["frozen"] != true && devices.profile["deleting"] != true {
            buttons.push(("New device", Control::AccessDeviceNew));
        }
        if page > 0 {
            buttons.push(("Previous page", Control::AccessDevicePage(false)));
        }
        if page + 1 < pages {
            buttons.push(("Next page", Control::AccessDevicePage(true)));
        }
        buttons.push(("Back", Control::AccessBack));
    }
    draw_access_buttons(frame, app, sections[2], &buttons, hits);
}

pub(in crate::tui) fn draw_access(frame: &mut Frame, app: &App, area: Rect, hits: &mut Vec<Hit>) {
    use crate::access::{Tab, label, text};
    let right = draw_navigation(frame, app, area, hits);
    let confirmation = app.access.decision.is_some()
        || app.access.account_action.is_some()
        || app.access.profile_action.is_some()
        || app
            .access
            .devices
            .as_ref()
            .is_some_and(|devices| devices.delete_key.is_some());
    let right = if confirmation {
        let popup = centered(right, 78, 12);
        frame.render_widget(Clear, popup);
        popup
    } else if app
        .access
        .editor
        .as_ref()
        .is_some_and(|editor| editor.duration_input.is_some())
    {
        let popup = centered(right, 76, if app.error.is_empty() { 11 } else { 13 });
        frame.render_widget(Clear, popup);
        popup
    } else {
        right
    };
    let block = section_block(" Access ");
    let inner = block.inner(right);
    frame.render_widget(block, right);
    if confirmation {
        let (question, confirm) = if let Some(approve) = app.access.decision {
            (
                if approve {
                    "Approve this access request?"
                } else {
                    "Reject this access request?"
                },
                Control::AccessConfirm,
            )
        } else if let Some((body, _)) = &app.access.account_action {
            (
                match (body["role"].as_str(), body["status"].as_str()) {
                    (Some("admin"), _) => "Grant administrator rights?",
                    (Some(_), _) => "Remove administrator rights?",
                    (_, Some("disabled")) => {
                        "Block this account? Bot and API access will be disabled.\nVPN access is retained; use Freeze to suspend it."
                    }
                    _ => "Approve this account?",
                },
                Control::AccessAccountConfirm,
            )
        } else if let Some(delete) = app.access.profile_action {
            (
                if delete {
                    "Delete this profile and revoke its access?"
                } else if app
                    .access
                    .card
                    .as_ref()
                    .is_some_and(|card| card["frozen"] == true)
                {
                    "Unfreeze this profile?"
                } else {
                    "Freeze this profile and suspend its access?"
                },
                Control::AccessProfileConfirm,
            )
        } else {
            (
                "Delete this device and revoke its access?",
                Control::AccessDeviceConfirm,
            )
        };
        let rows = Layout::vertical([Constraint::Min(0), Constraint::Length(3)]).split(inner);
        frame.render_widget(Paragraph::new(question).wrap(Wrap { trim: false }), rows[0]);
        draw_access_buttons(
            frame,
            app,
            rows[1],
            &[("Cancel", Control::AccessBack), ("Confirm", confirm)],
            hits,
        );
        return;
    }
    let footer_height = if let Some(editor) = &app.access.editor {
        if editor.expiry_view {
            if editor.duration_input.is_some() || inner.height < 12 {
                3
            } else {
                0
            }
        } else if editor.grants_view {
            let count = if editor.future_view {
                editor.regions.len() + 1
            } else {
                editor.nodes.len()
            };
            let capacity = usize::from(inner.height.saturating_sub(6) / 3).max(1);
            if editor.future_view {
                if count > capacity { 3 } else { 0 }
            } else if count > capacity {
                6
            } else {
                3
            }
        } else {
            6
        }
    } else if let Some(card) = &app.access.card {
        let count = if app.access.decision.is_some()
            || app.access.account_action.is_some()
            || app.access.profile_action.is_some()
        {
            1
        } else {
            let mut count =
                usize::from(app.access.tab != Tab::Requests || card["status"] == "pending");
            if app.access.tab == Tab::Accounts || card.get("_account").is_some() {
                count += 2;
            }
            if app.access.tab == Tab::Accounts {
                count += 1;
            }
            if app.access.tab == Tab::Profiles && card["deleting"] != true {
                count += 3;
            }
            if app.access.tab == Tab::Requests && card["status"] == "pending" {
                count += 2;
            }
            count
        };
        count.div_ceil(if inner.width >= 32 { 2 } else { 1 }) as u16 * 3
    } else if app.access.filtered().len() <= usize::from(inner.height.saturating_sub(9) / 3).max(1)
    {
        3
    } else {
        6
    };
    let sections = Layout::vertical([
        Constraint::Length(
            if app.access.card.is_some()
                && app.access.editor.is_none()
                && app.access.devices.is_none()
            {
                0
            } else if app
                .access
                .editor
                .as_ref()
                .is_some_and(|editor| !editor.expiry_view && !editor.grants_view)
            {
                1
            } else {
                3
            },
        ),
        Constraint::Min(0),
        Constraint::Length(footer_height),
    ])
    .split(inner);
    if app.access.devices.is_some() {
        draw_access_devices(frame, app, [sections[0], sections[1], sections[2]], hits);
        return;
    }
    if let Some(editor) = &app.access.editor {
        if editor.expiry_view {
            draw_access_expiry(frame, app, [sections[0], sections[1], sections[2]], hits);
            return;
        }
        if editor.grants_view {
            draw_access_grants(frame, app, [sections[0], sections[1], sections[2]], hits);
            return;
        }
        if let Some(step) = editor.creation_step {
            frame.render_widget(
                Paragraph::new(format!("Create profile · Step {}/3", step + 1))
                    .style(Style::default().fg(Color::Cyan)),
                sections[0],
            );
            if step == 0 {
                let field = Rect::new(sections[1].x, sections[1].y, sections[1].width, 3);
                draw_field(
                    frame,
                    field,
                    "Profile name",
                    &editor.fields[0],
                    app.access_controls().get(app.access.selected)
                        == Some(&Control::AccessField(0)),
                );
                hits.push(Hit {
                    area: field,
                    control: Control::AccessField(0),
                });
            } else {
                let text = if step == 1 {
                    "Configure access and duration, then continue to review.".to_owned()
                } else {
                    format!(
                        "Name: {}\nExpires: {}\nAccess rules: {}\nExplicit grants: {}\n\nCreate this profile?",
                        editor.fields[0],
                        if editor.fields[1].is_empty() {
                            "Unlimited"
                        } else {
                            &editor.fields[1]
                        },
                        editor.policy["rules"].as_array().map_or(0, Vec::len),
                        editor.policy["explicit_grants"]
                            .as_array()
                            .map_or(0, Vec::len)
                    )
                };
                frame.render_widget(Paragraph::new(text).wrap(Wrap { trim: false }), sections[1]);
            }
            let mut buttons = vec![("Back", Control::AccessWizardBack)];
            if step == 1 && !editor.permanent_access {
                buttons.extend([
                    ("Duration", Control::AccessExpiryMenu),
                    ("Access", Control::AccessGrants),
                ]);
            }
            buttons.push(if step == 2 {
                ("Create profile", Control::AccessSave)
            } else {
                ("Continue", Control::AccessWizardNext)
            });
            draw_access_buttons(frame, app, sections[2], &buttons, hits);
            return;
        }
        let labels = ["Profile name", "Expires at (UTC)"];
        frame.render_widget(
            Paragraph::new(if editor.source.is_some() {
                "Edit profile"
            } else {
                "New profile"
            })
            .style(Style::default().fg(Color::Cyan)),
            sections[0],
        );
        for (index, label) in labels.iter().enumerate() {
            let rect = Rect::new(
                sections[1].x,
                sections[1].y + index as u16 * 3,
                sections[1].width,
                3,
            );
            if rect.bottom() <= sections[1].bottom() {
                draw_field(
                    frame,
                    rect,
                    label,
                    if index == 1 && editor.fields[index].is_empty() {
                        "Unlimited"
                    } else {
                        &editor.fields[index]
                    },
                    app.access_controls().get(app.access.selected)
                        == Some(&Control::AccessField(index)),
                );
                if index == 0 {
                    hits.push(Hit {
                        area: rect,
                        control: Control::AccessField(index),
                    });
                }
            }
        }
        if sections[1].height > 6 {
            let summary = format!(
                "{}{}",
                editor.access_summary(),
                if app.error.is_empty() {
                    String::new()
                } else {
                    format!("\n{}", app.error)
                }
            );
            frame.render_widget(
                Paragraph::new(summary).wrap(Wrap { trim: false }),
                Rect::new(
                    sections[1].x,
                    sections[1].y + 6,
                    sections[1].width,
                    sections[1].height - 6,
                ),
            );
        }
        let mut buttons = vec![
            ("Access", Control::AccessGrants),
            ("Save", Control::AccessSave),
            ("Cancel", Control::AccessBack),
        ];
        if !editor.permanent_access {
            buttons.insert(0, ("Duration", Control::AccessExpiryMenu));
        }
        let columns = button_areas(sections[2], buttons.len(), 2);
        for (index, (label, control)) in buttons.into_iter().enumerate() {
            draw_button(
                frame,
                columns[index],
                label,
                app.access_controls().get(app.access.selected) == Some(&control),
            );
            hits.push(Hit {
                area: columns[index],
                control,
            });
        }
        return;
    }
    let tabs = Layout::horizontal([Constraint::Fill(1); 2]).split(sections[0]);
    if app.access.card.is_none() && !app.access.creating_profile {
        for (index, tab) in Tab::ALL.into_iter().enumerate() {
            draw_button(
                frame,
                tabs[index],
                tab.label(),
                app.access_controls().get(app.access.selected) == Some(&Control::AccessTab(tab)),
            );
            hits.push(Hit {
                area: tabs[index],
                control: Control::AccessTab(tab),
            });
        }
    }
    let mut buttons = vec![("Refresh", Control::AccessRefresh)];
    if let Some(card) = &app.access.card {
        if app.access.tab == Tab::Requests && card["status"] != "pending" {
            buttons.clear();
        }
        let mut lines = vec![Line::from(label(app.access.tab, card))];
        if let Some(account) = card.get("_account") {
            lines.push(Line::from(format!(
                "{} · {}",
                label(Tab::Accounts, account),
                text(&account["status"])
            )));
        }
        for (key, title) in [
            ("telegram_user_id", "Telegram ID"),
            ("username", "Username"),
            ("first_name", "First name"),
            ("last_name", "Last name"),
            ("role", "Role"),
            ("status", "Status"),
            ("owner_account_id", "Account"),
            ("expires_at", "Expires"),
        ] {
            if !(card[key].is_null() || app.access.tab == Tab::Requests && key == "status") {
                lines.push(Line::from(format!("{title}: {}", text(&card[key]))));
            }
        }
        if app.access.tab == Tab::Profiles {
            if card["deleting"] == true {
                let status = card["operation"]["status"]
                    .as_str()
                    .unwrap_or("awaiting_executor");
                lines.push(Line::from(
                    if matches!(status, "succeeded" | "no_targets") {
                        "Profile deleted.".into()
                    } else {
                        format!("Removal: {status}")
                    },
                ));
                if let Some(tasks) = card["operation"]["tasks"].as_array() {
                    for task in tasks {
                        lines.push(Line::from(format!(
                            "{} · {} · {}",
                            text(&task["node_key"]),
                            text(&task["protocol"]),
                            text(&task["status"])
                        )));
                    }
                }
            }
            lines.push(Line::from(format!("Frozen: {}", card["frozen"] == true)));
        }
        if let Some(approve) = app.access.decision {
            lines.push(Line::from(if approve {
                "Approve this request?"
            } else {
                "Reject this request?"
            }));
            buttons = vec![
                ("Confirm", Control::AccessConfirm),
                ("Cancel", Control::AccessBack),
            ];
        } else if let Some((body, _)) = &app.access.account_action {
            lines.push(Line::from(
                match (body["role"].as_str(), body["status"].as_str()) {
                    (Some("admin"), _) => "Grant administrator access to this account?",
                    (Some(_), _) => "Remove administrator access from this account?",
                    (_, Some("disabled")) => "Block this account? Bot and API access will be disabled. VPN access is retained; use Freeze to suspend it.",
                    _ => "Approve this account?",
                },
            ));
            buttons = vec![
                ("Confirm", Control::AccessAccountConfirm),
                ("Cancel", Control::AccessBack),
            ];
        } else if let Some(delete) = app.access.profile_action {
            lines.push(Line::from(if delete {
                "Delete this profile and revoke its access?"
            } else if card["frozen"] == true {
                "Unfreeze this profile?"
            } else {
                "Freeze this profile and suspend its access?"
            }));
            buttons = vec![
                ("Confirm", Control::AccessProfileConfirm),
                ("Cancel", Control::AccessBack),
            ];
        } else {
            if app.access.tab == Tab::Accounts || card.get("_account").is_some() {
                let account = card.get("_account").unwrap_or(card);
                buttons.push((
                    if account["role"] == "admin" {
                        "Make member"
                    } else {
                        "Make admin"
                    },
                    Control::AccessAccountAction(true),
                ));
                if account["role"] != "admin" {
                    buttons.push((
                        if account["status"] == "approved" {
                            "Block"
                        } else {
                            "Approve"
                        },
                        Control::AccessAccountAction(false),
                    ));
                }
                if app.access.tab == Tab::Accounts {
                    buttons.push(("Edit access", Control::AccessEdit));
                }
            }
            if app.access.tab == Tab::Profiles && card["deleting"] != true {
                buttons.push(("Edit", Control::AccessEdit));
                if card["_account"]["role"] != "admin" {
                    buttons.push((
                        if card["frozen"] == true {
                            "Unfreeze"
                        } else {
                            "Freeze"
                        },
                        Control::AccessProfileAction(false),
                    ));
                    buttons.push(("Delete", Control::AccessProfileAction(true)));
                }
            }
            if app.access.tab == Tab::Requests && card["status"] == "pending" {
                buttons.extend([
                    ("Approve", Control::AccessDecision(true)),
                    ("Reject", Control::AccessDecision(false)),
                ]);
            }
            buttons.push(("Back", Control::AccessBack));
        }
        if !app.access.notice.is_empty() {
            lines.push(Line::from(app.access.notice.as_str()));
        }
        if !app.error.is_empty() {
            lines.push(Line::styled(
                app.error.as_str(),
                Style::default().fg(Color::Red),
            ));
        }
        frame.render_widget(
            Paragraph::new(lines).wrap(Wrap { trim: false }),
            sections[1],
        );
    } else {
        let content =
            Layout::vertical([Constraint::Length(3), Constraint::Min(0)]).split(sections[1]);
        draw_field(
            frame,
            content[0],
            if app.access.tab == Tab::Accounts && !app.access.notice.is_empty() {
                "Create profile · Choose account · Search"
            } else {
                "Search"
            },
            &app.access.search,
            app.access_controls().get(app.access.selected) == Some(&Control::AccessSearch),
        );
        hits.push(Hit {
            area: content[0],
            control: Control::AccessSearch,
        });
        let capacity = usize::from(content[1].height / 3).max(1);
        app.access_capacity.set(capacity);
        let indices = app.access.filtered();
        let pages = indices.len().div_ceil(capacity).max(1);
        let page = app.access.page.min(pages - 1);
        let first = app.viewport(page * capacity, indices.len(), capacity);
        for (row, &index) in indices.iter().skip(first).take(capacity).enumerate() {
            let rect = Rect::new(
                content[1].x,
                content[1].y + row as u16 * 3,
                content[1].width,
                3.min(content[1].height),
            );
            if rect.bottom() > content[1].bottom() {
                break;
            }
            draw_navigation_button(
                frame,
                rect,
                &label(app.access.tab, &app.access.items[index]),
                app.access_controls().get(app.access.selected) == Some(&Control::AccessItem(index)),
            );
            hits.push(Hit {
                area: rect,
                control: Control::AccessItem(index),
            });
        }
        if indices.is_empty() {
            frame.render_widget(
                Paragraph::new(if app.error.is_empty() {
                    "No records."
                } else {
                    &app.error
                }),
                content[1],
            );
        }
        if app.access.creating_profile {
            buttons.push(("Cancel", Control::AccessBack));
        }
        if app.access.tab == Tab::Profiles {
            buttons.push(("New profile", Control::AccessNew));
        }
        if page > 0 {
            buttons.push(("Previous page", Control::AccessPage(false)));
        }
        if page + 1 < pages {
            buttons.push(("Next page", Control::AccessPage(true)));
        }
    }
    draw_access_buttons(frame, app, sections[2], &buttons, hits);
}
