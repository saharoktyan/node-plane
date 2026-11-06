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
        let area = sections[2];
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
            && app.access.editor.as_ref().is_none_or(|editor| {
                editor.creation_step.is_none() && editor.duration_input.is_none()
            })
            && !app.access.creating_profile
            && app.access.decision.is_none()
    });
    let mut remaining: Vec<_> = buttons
        .iter()
        .copied()
        .filter(|(_, control)| back.is_none_or(|(_, back_control)| control != back_control))
        .collect();
    let profile_card = app.access.tab == crate::access::Tab::Profiles
        && app.access.card.is_some()
        && app.access.editor.is_none()
        && app.access.result.is_none()
        && app.access.provision.is_none()
        && app.access.account_action.is_none()
        && app.access.profile_action.is_none()
        && app.access.devices.is_none();
    if profile_card
        && let Some(index) = remaining
            .iter()
            .position(|(_, control)| *control == Control::AccessRefresh)
    {
        let refresh = remaining.remove(index);
        remaining.push(refresh);
    }
    let buttons = remaining.as_slice();
    let pages: Vec<_> = buttons
        .iter()
        .enumerate()
        .filter(|(_, (_, control))| is_page_control(*control))
        .map(|(index, _)| index)
        .collect();
    let cells = if profile_card {
        let mut cells = Vec::new();
        for (row, group) in buttons.chunks(2).enumerate() {
            let rect = Rect::new(area.x, area.y + row as u16 * 3, area.width, 3);
            cells.extend(
                Layout::horizontal(vec![Constraint::Fill(1); group.len()])
                    .spacing(1)
                    .split(rect)
                    .iter()
                    .copied(),
            );
        }
        cells
    } else if pages.len() == 2 && buttons.len() > 2 && area.height >= 6 {
        let page_cells = button_areas(Rect::new(area.x, area.y, area.width, 3), 2, 1);
        let other_labels: Vec<_> = buttons
            .iter()
            .filter(|(_, control)| !is_page_control(*control))
            .map(|(label, _)| *label)
            .collect();
        let mut others = dialog_button_areas(
            Rect::new(area.x, area.y + 3, area.width, area.height - 3),
            &other_labels,
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
        dialog_button_areas(
            area,
            &buttons.iter().map(|(label, _)| *label).collect::<Vec<_>>(),
            usize::from(area.height / 3).max(1),
        )
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
            Paragraph::new("Enter date as YYYY-MM-DD or duration like 12H, 7D, 3D5M1Y. M = 30 days; Y = 365 days."),
            sections[0],
        );
        let field = Rect::new(sections[1].x, sections[1].y, sections[1].width, 3);
        draw_field(
            frame,
            field,
            "Custom",
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
        ("Custom", Control::AccessDurationCustom),
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
        if let Some(networks) = card["networks"].as_object() {
            lines.push(Line::from(format!(
                "VLESS connections now: {} of {} on the busiest node",
                networks
                    .get("active")
                    .and_then(serde_json::Value::as_u64)
                    .unwrap_or(0),
                networks
                    .get("limit")
                    .and_then(serde_json::Value::as_u64)
                    .unwrap_or(0)
            )));
        }
        if devices.confirming() {
            lines.push(Line::from(device_question(Some(devices))));
            buttons = vec![
                (
                    if devices.rotate_key.is_some() {
                        "Reissue"
                    } else {
                        "Delete"
                    },
                    Control::AccessDeviceConfirm,
                ),
                ("Cancel", Control::AccessBack),
            ];
        } else {
            if card["status"] == "active" {
                buttons.extend([
                    ("Rename", Control::AccessDeviceEdit),
                    ("Limits", Control::AccessLimits),
                    ("Reissue VLESS", Control::AccessDeviceRotate(false)),
                    ("Reissue AWG", Control::AccessDeviceRotate(true)),
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
        let count = devices
            .items
            .iter()
            .filter(|item| item["status"] == "active")
            .count();
        let status = if !app.error.is_empty() {
            app.error.clone()
        } else if let Some(limit) = devices.limit
            && devices.operation.is_null()
        {
            format!("Devices: {count} of {limit}")
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
    if app.access.result.is_some() || app.access.provision.is_some() {
        let popup = centered(right, 78, 14);
        frame.render_widget(Clear, popup);
        let block = section_block(if app.access.result.is_some() {
            " Result "
        } else {
            " New profile "
        });
        let inner = block.inner(popup);
        frame.render_widget(block, popup);
        let rows = Layout::vertical([Constraint::Min(0), Constraint::Length(3)]).split(inner);
        if let Some(result) = &app.access.result {
            frame.render_widget(
                Paragraph::new(result.as_str()).wrap(Wrap { trim: false }),
                rows[0],
            );
            draw_access_buttons(
                frame,
                app,
                rows[1],
                &[(
                    if result.starts_with("Profile ") {
                        "Open profile"
                    } else if result.starts_with("Could not create profile:") {
                        "Cancel"
                    } else {
                        "Close"
                    },
                    Control::AccessResultClose,
                )],
                hits,
            );
        } else if let Some(draft) = &app.access.provision {
            if draft.confirm {
                frame.render_widget(
                    Paragraph::new(format!("Create profile for {}?", draft.identity))
                        .wrap(Wrap { trim: false }),
                    rows[0],
                );
                draw_access_buttons(
                    frame,
                    app,
                    rows[1],
                    &[
                        ("Back", Control::AccessProvisionCancel),
                        ("Create", Control::AccessProvisionSubmit),
                    ],
                    hits,
                );
            } else {
                let content = Layout::vertical([
                    Constraint::Length(2),
                    Constraint::Length(3),
                    Constraint::Min(0),
                ])
                .split(rows[0]);
                frame.render_widget(
                    Paragraph::new(
                        "Enter a Telegram ID or a known @username, or a name for a person without Telegram.",
                    )
                    .wrap(Wrap { trim: false }),
                    content[0],
                );
                draw_field(
                    frame,
                    content[1],
                    "Telegram ID / @username / name",
                    &draft.identity,
                    app.access.selected == 0,
                );
                hits.push(Hit {
                    area: content[1],
                    control: Control::AccessIdentity,
                });
                if !app.error.is_empty() {
                    frame.render_widget(
                        Paragraph::new(app.error.as_str()).wrap(Wrap { trim: false }),
                        content[2],
                    );
                }
                draw_access_buttons(
                    frame,
                    app,
                    rows[1],
                    &[
                        ("Cancel", Control::AccessProvisionCancel),
                        ("Continue", Control::AccessProvisionNext),
                    ],
                    hits,
                );
            }
        }
        return;
    }
    if app
        .access
        .editor
        .as_ref()
        .is_some_and(|editor| editor.grants_view && editor.permanent_access)
    {
        let popup = centered(right, 78, 12);
        frame.render_widget(Clear, popup);
        let block = section_block(" Access ");
        let inner = block.inner(popup);
        frame.render_widget(block, popup);
        let rows = Layout::vertical([Constraint::Min(0), Constraint::Length(3)]).split(inner);
        frame.render_widget(
            Paragraph::new("Administrators have permanent access to all servers.")
                .wrap(Wrap { trim: false }),
            rows[0],
        );
        draw_access_buttons(
            frame,
            app,
            rows[1],
            &[("Close", Control::AccessGrantBack)],
            hits,
        );
        return;
    }
    let confirmation = app.access.decision.is_some()
        || app.access.account_action.is_some()
        || app.access.profile_action.is_some()
        || app
            .access
            .devices
            .as_ref()
            .is_some_and(|devices| devices.confirming());
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
                    _ if body["roles"].is_array() => role_question(&body["roles"]),
                    (Some("admin"), _) => "Grant administrator rights?",
                    (Some(_), _) => "Remove administrator rights?",
                    (_, Some("disabled")) => {
                        "Block this account? Bot and API access will be disabled.\nVPN access is retained; use Freeze to suspend it."
                    }
                    (_, Some("approved")) => "Unblock this account?",
                    _ => "Approve this account?",
                },
                Control::AccessAccountConfirm,
            )
        } else if let Some(delete) = app.access.profile_action {
            (
                if delete {
                    "Delete this account, its VPN profile and all devices?"
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
                device_question(app.access.devices.as_ref()),
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
        } else if editor.creation_step.is_some() {
            6
        } else {
            3
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
            if card["deleting"] != true
                && (app.access.tab == Tab::Accounts || card.get("_account").is_some())
            {
                let account = card.get("_account").unwrap_or(card);
                count += 1 + usize::from(account["role"] != "admin");
                if app.access.tab == Tab::Accounts {
                    // Edit access, Login, Sign-in link, the own Password, End
                    // sessions, Remove passkeys and Unlink Workstation while
                    // there are any.
                    count += 3
                        + usize::from(app.own_account() == account["id"].as_str())
                        + usize::from(account["sessions"].as_u64().unwrap_or(0) > 0)
                        + usize::from(account["passkeys"].as_u64().unwrap_or(0) > 0)
                        + usize::from(account["workstations"].as_u64().unwrap_or(0) > 0);
                }
            }
            if app.access.tab == Tab::Invites && card["status"] == "active" {
                count += 1;
            }
            if app.access.tab == Tab::Merges && card["status"] == "pending" {
                count += 3;
            }
            if app.access.tab == Tab::Profiles && card["deleting"] != true {
                // Edit, Limits, Tariff, Renewal while requested, Traffic when collected,
                // and Freeze and Delete for members.
                count += 3
                    + usize::from(card["_summary"]["renewal"]["status"] == "pending")
                    + usize::from(card["_summary"]["traffic"].is_object())
                    + 2 * usize::from(card["_account"]["role"] != "admin");
            }
            if app.access.tab == Tab::Requests && card["status"] == "pending" {
                count += 2;
            }
            count
        };
        count.div_ceil(if inner.width >= 32 { 2 } else { 1 }) as u16 * 3
    } else if app.access.registration.is_some() {
        // Open on and off, four cleanup choices and Back.
        7u16.div_ceil(if inner.width >= 32 { 2 } else { 1 }) * 3
    } else if app.access.bot_username_input.is_some() {
        3u16.div_ceil(if inner.width >= 32 { 2 } else { 1 }) * 3
    } else if let Some(tariffs) = &app.access.invite_templates {
        // Nothing, VPN, the tariffs and Cancel.
        (tariffs.len().min(20) + 3).div_ceil(if inner.width >= 32 { 2 } else { 1 }) as u16 * 3
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
        hits.push(Hit {
            area: pinned_back_area(frame.area()),
            control: Control::AccessBack,
        });
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
                if index == 0 && editor.name_editable {
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
        ];
        if !editor.permanent_access {
            buttons.insert(0, ("Duration", Control::AccessExpiryMenu));
        }
        let columns = dialog_button_areas(
            sections[2],
            &buttons.iter().map(|(label, _)| *label).collect::<Vec<_>>(),
            1,
        );
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
    let tabs = Layout::horizontal([Constraint::Fill(1); Tab::ALL.len()]).split(sections[0]);
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
        if let (Tab::Profiles, Some(page)) = (app.access.tab, app.access.traffic) {
            let capacity = usize::from(sections[1].height.saturating_sub(3)).max(1);
            let (lines, page, pages) = profile_traffic(card, page, capacity);
            frame.render_widget(
                Paragraph::new(lines.into_iter().map(Line::from).collect::<Vec<_>>())
                    .wrap(Wrap { trim: false }),
                sections[1],
            );
            let mut buttons = Vec::new();
            if page > 0 {
                buttons.push(("Previous page", Control::AccessTrafficPage(false)));
            }
            if page + 1 < pages {
                buttons.push(("Next page", Control::AccessTrafficPage(true)));
            }
            buttons.push(("Back", Control::AccessBack));
            draw_access_buttons(frame, app, sections[2], &buttons, hits);
            return;
        }
        let mut lines = vec![Line::from(label(app.access.tab, card))];
        if app.access.tab != Tab::Requests
            && let Some(account) = card.get("_account")
        {
            lines.push(Line::from(format!(
                "{} · {}",
                label(Tab::Accounts, account),
                text(&account["status"])
            )));
        }
        for (key, title) in [
            ("login", "Login"),
            ("display_name", "Name"),
            ("telegram_user_id", "Telegram ID"),
            ("username", "Username"),
            ("first_name", "First name"),
            ("last_name", "Last name"),
            ("role", "Role"),
            ("roles", "Roles"),
            ("status", "Status"),
            ("owner_account_id", "Account"),
            ("expires_at", "Expires"),
        ] {
            if !(card[key].is_null() || app.access.tab == Tab::Requests && key == "status") {
                lines.push(Line::from(format!("{title}: {}", text(&card[key]))));
            }
        }
        if app.access.tab == Tab::Merges {
            lines.extend(crate::access::merge_lines(card).into_iter().map(Line::from));
        }
        if app.access.tab == Tab::Invites {
            lines.push(Line::from(crate::access::invite_template(card)));
            if let Some(token) = card["token"].as_str() {
                lines.push(Line::from(
                    "Send this to the person; it is shown only now and works once.",
                ));
                if let Some(link) = card["cabinet_link"].as_str() {
                    lines.push(Line::from(format!("Web cabinet: {link}")));
                }
                if let Some(link) = card["telegram_link"].as_str() {
                    lines.push(Line::from(format!("Telegram: {link}")));
                    lines.push(Line::from(format!(
                        "The link works while the bot is @{}. If the bot's username changes, it stops opening: make a new one.",
                        crate::access::text(&card["telegram_bot_username"])
                    )));
                } else {
                    lines.push(Line::from(format!("Token: {token}")));
                }
            }
        }
        if app.access.tab == Tab::Accounts && card["has_password"].is_boolean() {
            lines.push(Line::from(format!(
                "Password: {} · Sessions: {} · Passkeys: {}",
                if card["has_password"] == true {
                    "set"
                } else {
                    "not set"
                },
                card["sessions"].as_u64().unwrap_or(0),
                card["passkeys"].as_u64().unwrap_or(0)
            )));
            if let Some(count) = card["workstations"].as_u64().filter(|count| *count > 0) {
                lines.push(Line::from(format!(
                    "Workstation through the relay: {count}"
                )));
            }
        }
        if app.access.tab == Tab::Profiles {
            if card["deleting"] == true {
                let status = card["operation"]["status"]
                    .as_str()
                    .unwrap_or("awaiting_executor");
                lines.push(Line::from(
                    if matches!(status, "succeeded" | "no_targets") {
                        "Account and VPN profile deleted.".into()
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
            if card["deleting"] != true {
                lines.extend(profile_details(card).into_iter().map(Line::from));
            }
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
                    _ if body["roles"].is_array() => role_question(&body["roles"]),
                    (Some("admin"), _) => "Grant administrator access to this account?",
                    (Some(_), _) => "Remove administrator access from this account?",
                    (_, Some("disabled")) => "Block this account? Bot and API access will be disabled. VPN access is retained; use Freeze to suspend it.",
                    (_, Some("approved")) => "Unblock this account?",
                    _ => "Approve this account?",
                },
            ));
            buttons = vec![
                ("Confirm", Control::AccessAccountConfirm),
                ("Cancel", Control::AccessBack),
            ];
        } else if let Some(login) = &app.access.login_input {
            lines.push(Line::from(
                "Login: 3–32 Latin letters, digits, _ . or -; case does not matter for signing in. The old login stays reserved for 30 days.",
            ));
            lines.push(Line::from(format!("New login: {login}▏")));
            buttons = vec![
                ("Type the login", Control::AccessLoginInput),
                ("Save", Control::AccessLoginSave),
                ("Cancel", Control::AccessBack),
            ];
        } else if app.access.workstations_confirm {
            lines.push(Line::from(
                "Unlink every Workstation this account linked through the relay? They stop working at once.",
            ));
            buttons = vec![
                ("Unlink", Control::AccessUnlinkWorkstationsConfirm),
                ("Cancel", Control::AccessBack),
            ];
        } else if app.access.passkeys_confirm {
            lines.push(Line::from(
                "Remove every passkey of this account? It signs in with a password or Telegram until it adds a new one in the web cabinet.",
            ));
            buttons = vec![
                ("Remove", Control::AccessRemovePasskeysConfirm),
                ("Cancel", Control::AccessBack),
            ];
        } else if let Some(fields) = &app.access.password_input {
            lines.push(Line::from(
                "Password: 10–256 characters, not the same as the login. Changing it ends the account's other sessions.",
            ));
            let titles = ["Current password", "New password", "Repeat it"];
            let first = usize::from(card["has_password"] != true);
            for (index, field) in fields.iter().enumerate().skip(first) {
                let cursor = if app.access.password_field == index {
                    "▏"
                } else {
                    ""
                };
                lines.push(Line::from(format!(
                    "{}: {}{cursor}",
                    titles[index],
                    "•".repeat(field.chars().count())
                )));
            }
            buttons = (first..3)
                .map(|index| {
                    (
                        ["Type current", "Type new", "Type again"][index],
                        Control::AccessPasswordField(index),
                    )
                })
                .collect();
            buttons.push(("Save", Control::AccessPasswordSave));
            buttons.push(("Cancel", Control::AccessBack));
        } else if app.access.role_picker {
            lines.push(Line::from("Choose the role of this account:"));
            buttons = vec![
                ("Member", Control::AccessRole(Some("member"))),
                ("Support", Control::AccessRole(Some("support"))),
                ("Operator", Control::AccessRole(Some("operator"))),
                ("Administrator", Control::AccessRole(Some("admin"))),
                ("Owner", Control::AccessRole(Some("owner"))),
                ("Cancel", Control::AccessBack),
            ];
        } else if let Some(delete) = app.access.profile_action {
            lines.push(Line::from(if delete {
                "Delete this account, its VPN profile and all devices?"
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
            if card["deleting"] != true
                && (app.access.tab == Tab::Accounts || card.get("_account").is_some())
            {
                let account = card.get("_account").unwrap_or(card);
                // The picker covers member and every staff preset; the
                // backend lets only owners assign roles other than admin.
                buttons.push(("Role", Control::AccessRole(None)));
                if app.access.tab == Tab::Accounts {
                    buttons.push(("Login", Control::AccessLogin));
                    // Only the own password is set here, never through Telegram.
                    if app.own_account() == account["id"].as_str() {
                        buttons.push(("Password", Control::AccessPassword));
                    }
                    if account["sessions"].as_u64().unwrap_or(0) > 0 {
                        buttons.push(("End sessions", Control::AccessEndSessions));
                    }
                    if account["passkeys"].as_u64().unwrap_or(0) > 0 {
                        buttons.push(("Remove passkeys", Control::AccessRemovePasskeys));
                    }
                    if account["workstations"].as_u64().unwrap_or(0) > 0 {
                        buttons.push(("Unlink Workstation", Control::AccessUnlinkWorkstations));
                    }
                    buttons.push(("Sign-in link", Control::AccessInviteAccount));
                }
                if account["role"] != "admin" {
                    buttons.push((
                        if account["status"] == "approved" {
                            "Block"
                        } else if account["status"] == "disabled" {
                            "Unblock"
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
                buttons.push(("Limits", Control::AccessLimits));
                buttons.push(("Tariff", Control::AccessTariff));
                if card["_summary"]["renewal"]["status"] == "pending" {
                    buttons.push(("Renewal", Control::AccessRenewal));
                }
                if card["_summary"]["traffic"].is_object() {
                    buttons.push(("Traffic", Control::AccessTraffic));
                }
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
            if app.access.tab == Tab::Invites && card["status"] == "active" {
                buttons.push(("Revoke", Control::AccessInviteRevoke));
            }
            if app.access.tab == Tab::Merges && card["status"] == "pending" {
                let result = &card["preview"]["result"];
                if result["vpn"] == "choose" {
                    buttons.push(("Merge, keep main's VPN", Control::AccessMerge(Some(true))));
                    if result["keep_main_only"] != true {
                        buttons
                            .push(("Merge, keep other's VPN", Control::AccessMerge(Some(false))));
                    }
                } else {
                    buttons.push(("Merge", Control::AccessMerge(None)));
                }
                buttons.push(("Reject", Control::AccessMergeReject));
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
    } else if let Some(registration) = &app.access.registration {
        let open = registration["open"] == true;
        let cleanup = registration["cleanup_days"].as_u64();
        let lines = vec![
            Line::from(format!(
                "Open registration: {}",
                if open { "on" } else { "off" }
            )),
            Line::from(
                "Anyone who reaches the web cabinet can make an account. It gets no access and no request: the person asks for a service. Invites work either way.",
            ),
            Line::from(format!(
                "Remove registered accounts left without access: {}",
                cleanup.map_or("never".to_owned(), |days| format!(
                    "after {days} days without a sign-in"
                ))
            )),
        ];
        frame.render_widget(
            Paragraph::new(lines).wrap(Wrap { trim: false }),
            sections[1],
        );
        buttons = vec![
            (
                if open {
                    "Close registration"
                } else {
                    "Open registration"
                },
                Control::AccessRegistrationOpen(!open),
            ),
            ("Never remove", Control::AccessRegistrationCleanup(0)),
            ("After 7 days", Control::AccessRegistrationCleanup(7)),
            ("After 30 days", Control::AccessRegistrationCleanup(30)),
            ("After 90 days", Control::AccessRegistrationCleanup(90)),
            ("Back", Control::AccessBack),
        ];
    } else if let Some(username) = &app.access.bot_username_input {
        let mut lines = vec![
            Line::from(
                if crate::access::bot_username_missing(&app.access.telegram_bot) {
                    "Invite links open the bot by its username. Set it before making an invite."
                } else {
                    "Invite links open the bot by its username."
                },
            ),
            Line::from(
                "Links made before the bot's username changes stop opening; make new ones after a change.",
            ),
            Line::from(format!("Bot username: @{username}▏")),
        ];
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
        buttons = vec![
            ("Type the username", Control::AccessBotUsernameInput),
            ("Save", Control::AccessBotUsernameSave),
            ("Cancel", Control::AccessBack),
        ];
    } else if let Some(tariffs) = &app.access.invite_templates {
        frame.render_widget(
            Paragraph::new(
                "What does the new account get? The invite works once and for 7 days. \
                 A tariff gives VPN with the tariff's access and term.",
            )
            .wrap(Wrap { trim: false }),
            sections[1],
        );
        buttons = vec![
            ("Nothing yet", Control::AccessInviteTemplate(0)),
            ("VPN", Control::AccessInviteTemplate(1)),
        ];
        for (index, tariff) in tariffs.iter().enumerate().take(20) {
            buttons.push((
                tariff["title"].as_str().unwrap_or("Tariff"),
                Control::AccessInviteTemplate(index + 2),
            ));
        }
        buttons.push(("Cancel", Control::AccessBack));
    } else {
        let content =
            Layout::vertical([Constraint::Length(3), Constraint::Min(0)]).split(sections[1]);
        draw_field(
            frame,
            content[0],
            &if app.access.tab == Tab::Accounts && !app.access.notice.is_empty() {
                "Create profile · Choose account · Search".to_owned()
            } else if app.access.tab == Tab::Invites {
                match app.access.telegram_bot["username"].as_str() {
                    Some(username) => format!("Search · Bot @{username}"),
                    None if app.access.telegram_bot["telegram_enabled"] == true => {
                        "Search · Bot username not set: invites need it".to_owned()
                    }
                    None => "Search".to_owned(),
                }
            } else {
                "Search".to_owned()
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
        if app.access.tab == Tab::Profiles && !app.access.without_access {
            buttons.push(("New profile", Control::AccessNew));
            buttons.push(("Without access", Control::AccessWithoutAccess));
        }
        if app.access.without_access {
            buttons.push(("All profiles", Control::AccessTab(Tab::Profiles)));
        }
        if app.access.tab == Tab::Invites {
            buttons.push(("New invite", Control::AccessInviteNew));
            buttons.push(("Bot username", Control::AccessBotUsername));
            buttons.push(("Registration", Control::AccessRegistration));
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

/// Identity, servers and synchronization of a profile card, as the bot's
/// technical information.
fn profile_details(card: &serde_json::Value) -> Vec<String> {
    use crate::access::text;
    let mut lines = vec![format!(
        "ID: {} · revision {}",
        text(&card["id"]),
        text(&card["desired_revision"])
    )];
    if let Some(count) = card["_summary"]["node_count"].as_u64() {
        lines.push(format!("Servers: {count}"));
    }
    if let Some(tariff) = card["_summary"]["tariff"]["title"].as_str() {
        lines.push(format!("Tariff: {tariff}"));
    }
    if card["_summary"]["renewal"]["status"] == "pending" {
        lines.push("Renewal requested".into());
    }
    let operation = &card["operation"];
    if let Some(status) = operation["status"].as_str() {
        lines.push(format!("Sync: {}", status.replace('_', " ")));
        if !matches!(status, "succeeded" | "no_targets" | "superseded") {
            for task in operation["tasks"].as_array().into_iter().flatten() {
                if task["status"] != "succeeded" {
                    lines.push(format!(
                        "{} · {} · {}",
                        text(&task["node_key"]),
                        text(&task["protocol"]),
                        text(&task["status"])
                    ));
                }
            }
        }
    }
    lines
}

pub(in crate::tui) fn traffic_size(bytes: u64) -> String {
    let mut amount = bytes as f64;
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"] {
        if amount < 1024.0 || unit == "TiB" {
            return format!("{amount:.1} {unit}");
        }
        amount /= 1024.0;
    }
    unreachable!()
}

/// One page of a profile's traffic by server: lines, page and page count.
pub(in crate::tui) fn profile_traffic(
    card: &serde_json::Value,
    page: usize,
    capacity: usize,
) -> (Vec<String>, usize, usize) {
    use crate::access::text;
    let summary = &card["_summary"];
    let traffic = &summary["traffic"];
    let nodes = summary["nodes"].as_array().cloned().unwrap_or_default();
    let pages = nodes.len().div_ceil(capacity).max(1);
    let page = page.min(pages - 1);
    let mut lines = vec![
        format!("Traffic by server: {}", text(&card["display_name"])),
        format!("Usage · {}", traffic["month"].as_str().unwrap_or("—")),
    ];
    for node in nodes.iter().skip(page * capacity).take(capacity) {
        let mut stale = false;
        let usage: Vec<String> = node["protocols"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|protocol| {
                let item = traffic["nodes"].as_array().and_then(|items| {
                    items.iter().find(|item| {
                        item["node_key"] == node["key"] && item["protocol"] == *protocol
                    })
                });
                stale |= item.is_some_and(|item| item["status"] != "current");
                let total = item.and_then(|item| {
                    Some(item["uplink_bytes"].as_u64()? + item["downlink_bytes"].as_u64()?)
                });
                format!(
                    "{}: {}",
                    if protocol == "xray" { "VLESS" } else { "AWG" },
                    total.map_or("waiting".into(), traffic_size)
                )
            })
            .collect();
        let name = [text(&node["flag"]), text(&node["title"])]
            .into_iter()
            .filter(|part| !part.is_empty() && part != "—")
            .collect::<Vec<_>>()
            .join(" ");
        lines.push(format!(
            "{name} · {}{}",
            usage.join(" · "),
            if stale { " · counters stale" } else { "" }
        ));
    }
    if nodes.is_empty() {
        lines.push("No servers.".into());
    }
    (lines, page, pages)
}

fn device_question(devices: Option<&crate::access::Devices>) -> &'static str {
    match devices {
        Some(devices) if devices.rotate_key.is_some() && devices.rotate_awg => {
            "Reissue this device's AWG key? Its current AWG configurations stop working once nodes apply the change; VLESS links do not change."
        }
        Some(devices) if devices.rotate_key.is_some() => {
            "Reissue this device's VLESS key? Its current VLESS links stop working; AWG configurations do not change."
        }
        _ => "Delete this device and revoke its access?",
    }
}

/// The confirmation for assigning a preset role (`roles` of the change).
pub(in crate::tui) fn role_question(roles: &serde_json::Value) -> &'static str {
    match roles
        .as_array()
        .and_then(|roles| roles.first())
        .and_then(|role| role.as_str())
    {
        None => "Make this account a member? All its roles are removed.",
        Some("owner") => {
            "Make this account an owner? Owners may change every role, including other owners."
        }
        Some("admin") => "Make this account an administrator?",
        Some("operator") => "Make this account an operator? Operators manage servers only.",
        Some("support") => {
            "Make this account support? Support handles requests and people's profiles."
        }
        Some(_) => "Assign this role?",
    }
}
