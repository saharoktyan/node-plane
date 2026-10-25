use super::super::*;

fn operation_kind_label(kind: &str) -> &str {
    match kind {
        "bootstrap" => "Node setup",
        "agent" => "Agent setup",
        "node" => "Node maintenance",
        "settings" => "Node settings",
        "profile" => "Profile access",
        "update" => "Controller update",
        "backup" => "Backup",
        "removal" => "Node removal",
        "temporary" => "Temporary configuration",
        "issuance" => "Configuration request",
        "cleanup" => "Controller cleanup",
        _ => "Operation",
    }
}

pub(in crate::tui) fn recovery_controls(page: &serde_json::Value) -> Vec<(String, Control)> {
    let mut controls = Vec::new();
    if page["grouped"] == true {
        for (index, group) in page["groups"].as_array().into_iter().flatten().enumerate() {
            let flag = group["flag"].as_str().unwrap_or("");
            let title = group["title"].as_str().unwrap_or("Controller");
            controls.push((
                format!(
                    "{flag} {title} · {} tasks · {} blocked",
                    group["total"], group["blocked"]
                )
                .trim()
                .to_owned(),
                Control::ControllerRecoveryNode(index),
            ));
        }
    }
    for (index, item) in page["items"].as_array().into_iter().flatten().enumerate() {
        for (action, value) in item["actions"].as_array().into_iter().flatten().enumerate() {
            let label = match value.as_str().unwrap_or("") {
                "retry" if item["kind"] == "cleanup" => "Continue cleanup",
                "abort" if item["kind"] == "cleanup" => "Cancel cleanup",
                "recheck" => "Check result",
                "resolve" if item["kind"] == "bootstrap" => "Resume setup",
                "resolve" if item["kind"] == "settings" => "Reapply settings",
                "resolve" if item["kind"] == "profile" => "Restore profile access",
                "resolve" if item["kind"] == "temporary" => "Cancel unused configuration",
                "resolve" => "Resolve blocked step",
                "resume" => "Continue",
                "cancel" if item["kind"] == "bootstrap" => "Cancel setup",
                "cancel" if item["kind"] == "issuance" => "Close failed configuration request",
                "cancel" if item["kind"] == "backup" && item["status"] == "blocked" => {
                    "Cancel failed backup"
                }
                "cancel" => "Cancel operation",
                _ => continue,
            };
            controls.push((
                format!(
                    "{} · {}: {label}",
                    operation_kind_label(item["kind"].as_str().unwrap_or("")),
                    item["node_key"]
                        .as_str()
                        .filter(|v| !v.is_empty())
                        .unwrap_or("Controller")
                ),
                Control::ControllerRecoveryAction(index, action),
            ));
        }
    }
    let offset = page["offset"].as_u64().unwrap_or(0) as usize;
    let size = page["page_size"].as_u64().unwrap_or(10) as usize;
    if offset > 0 {
        controls.push((
            "Previous page".into(),
            Control::ControllerRecoveryPage(offset.saturating_sub(size)),
        ));
    }
    if offset + size < page["total"].as_u64().unwrap_or(0) as usize {
        controls.push((
            "Next page".into(),
            Control::ControllerRecoveryPage(offset + size),
        ));
    }
    controls.push(("Refresh".into(), Control::ControllerRecoveryPage(offset)));
    controls.push(("Back".into(), Control::ControllerBack));
    controls
}

pub(in crate::tui) fn history_controls(page: &serde_json::Value) -> Vec<Control> {
    let mut controls: Vec<_> = (0..page["items"].as_array().map_or(0, Vec::len))
        .map(Control::ControllerHistoryItem)
        .collect();
    let offset = page["offset"].as_u64().unwrap_or(0) as usize;
    let total = page["total"].as_u64().unwrap_or(0) as usize;
    if offset > 0 {
        controls.push(Control::ControllerHistoryPage(false));
    }
    if offset + (page["page_size"].as_u64().unwrap_or(10) as usize) < total {
        controls.push(Control::ControllerHistoryPage(true));
    }
    controls.push(Control::ControllerBack);
    controls
}

pub(in crate::tui) fn audit_title(item: &serde_json::Value) -> String {
    if let Some(title) = item["title"].as_str().filter(|v| !v.is_empty()) {
        return title.into();
    }
    let action = item["action"].as_str().unwrap_or("Unknown operation");
    let Some((method, path)) = action.split_once(" /api/v1/") else {
        return action.into();
    };
    let verb = match method {
        "POST" => "Create",
        "PATCH" | "PUT" => "Update",
        "DELETE" => "Delete",
        _ => "Inspect",
    };
    let parts: Vec<_> = path.split('/').collect();
    match path {
        "system/updates/check" => "Check controller updates".into(),
        "system/updates/run" => "Run controller update".into(),
        "system/alerts/preferences" => "Change alert settings".into(),
        "system/backups/preferences" => "Change automatic backup settings".into(),
        "system/backups/jobs" => "Run backup operation".into(),
        "system/traffic/preferences" => "Change traffic collection settings".into(),
        "system/bot-title" => "Change bot name".into(),
        "announcements" => "Send announcement".into(),
        "announcements/preview" => "Preview announcement".into(),
        _ => {
            let resource = match parts[0] {
                "profiles" => "profile",
                "accounts" => "account",
                "nodes" => "server",
                "access-requests" => "access request",
                "devices" => "device",
                "system" => "controller settings",
                "operations" => "operation",
                "temporary-configs" => "temporary configuration",
                _ => "controller settings",
            };
            let detail = parts
                .get(2)
                .map(|s| format!(" · {}", s.replace('-', " ")))
                .unwrap_or_default();
            format!("{verb} {resource}{detail}")
        }
    }
}

pub(in crate::tui) fn compact_history_item(
    item: &serde_json::Value,
    audit: bool,
) -> (String, String) {
    if audit {
        let actor = item["account_label"]
            .as_str()
            .filter(|v| !v.is_empty())
            .or_else(|| item["account_id"].as_str())
            .unwrap_or("Administrator");
        let action = audit_title(item);
        let phase = item["phase"]
            .as_str()
            .filter(|v| !v.is_empty())
            .unwrap_or("-");
        let time = item["occurred_at"].as_str().unwrap_or("");
        (format!("{actor} → {action} · {phase}"), time.to_string())
    } else {
        let kind = item["kind"].as_str().unwrap_or("operation");
        let status = item["status"].as_str().unwrap_or("unknown");
        let node = item["node_title"]
            .as_str()
            .or_else(|| item["node_key"].as_str());
        let action = item["title"].as_str().filter(|v| !v.is_empty());
        let head = match (node, action) {
            (Some(node), Some(action)) => format!("{kind} · {node} → {action} · {status}"),
            (Some(node), None) => format!("{kind} · {node} · {status}"),
            (None, Some(action)) => format!("{kind} → {action} · {status}"),
            (None, None) => format!("{kind} · {status}"),
        };
        let time = item["created_at"].as_str().unwrap_or("").to_string();
        (head, time)
    }
}

pub(in crate::tui) fn alert_description(item: &serde_json::Value) -> String {
    let kind = item["kind"].as_str().unwrap_or("");
    let label = match kind {
        "cpu_high" => "Sustained high CPU usage",
        "disk_low" => "Low disk space",
        "ram_high" => "High RAM usage",
        "load_high" => "High load average",
        "node_unreachable" => "Node agent is unavailable",
        "xray_down" => "VLESS is not running",
        "awg_down" => "AmneziaWG is not running",
        _ => "Server needs attention",
    };
    let Some(value) = item["payload"]["value"].as_f64().filter(|v| v.is_finite()) else {
        return label.into();
    };
    let measurement = match kind {
        "disk_low" => format!("Disk space available: {value:.1}%"),
        "ram_high" => format!("RAM used: {value:.1}%"),
        "cpu_high" => format!("CPU used (1-minute average): {value:.1}%"),
        "load_high" => format!(
            "Load average (1 minute): {value:.2}; logical CPUs: {}",
            item["payload"]["cpus"]
        ),
        _ => return label.into(),
    };
    format!("{label} · {measurement}")
}

pub(in crate::tui) fn alert_controls(state: &crate::controller::Browser) -> Vec<Control> {
    let mut controls = vec![Control::ControllerAlertToggle("enabled")];
    if state.alerts.as_ref().is_some_and(|v| v["enabled"] == true) {
        controls.extend([
            Control::ControllerAlertInterval(3),
            Control::ControllerAlertInterval(5),
            Control::ControllerAlertInterval(10),
            Control::ControllerAlertToggle("notify_resolved"),
        ]);
    }
    if let Some(items) = state.alerts.as_ref().and_then(|v| v["active"].as_array()) {
        let start = state.alerts_page * 5;
        for (index, item) in items.iter().enumerate().skip(start).take(5) {
            if item["dismissed"] != true && item["event_id"].is_string() {
                controls.push(Control::ControllerAlertDismiss(index));
            }
        }
        if start > 0 {
            controls.push(Control::ControllerAlertPage(false));
        }
        if start + 5 < items.len() {
            controls.push(Control::ControllerAlertPage(true));
        }
    }
    controls.extend([Control::ControllerAlerts, Control::ControllerBack]);
    controls
}

pub(in crate::tui) fn defaults_controls(state: &crate::controller::Browser) -> Vec<Control> {
    let mut controls = vec![
        Control::ControllerDefaultsToggle("awg"),
        Control::ControllerDefaultsToggle("xray"),
    ];
    if state.defaults.as_ref().is_some_and(|v| {
        v["protocols"]
            .as_array()
            .is_some_and(|items| items.iter().any(|v| v == "xray"))
    }) {
        controls.extend([
            Control::ControllerDefaultsToggle("tcp"),
            Control::ControllerDefaultsToggle("xhttp"),
        ]);
    }
    controls.push(Control::ControllerDefaultsAdvanced);
    if state.defaults_advanced {
        controls.push(Control::ControllerDefaultsPreset);
        controls.push(Control::ControllerDefaultsMode);
        if state
            .defaults
            .as_ref()
            .is_some_and(|v| v["settings"]["awg_port_mode"] == "manual")
        {
            controls.push(Control::ControllerDefaultsField("awg_port"));
        }
        controls.extend(
            [
                "xray_sni",
                "xray_fingerprint",
                "xray_tcp_port",
                "xray_xhttp_port",
                "xray_xhttp_path",
            ]
            .into_iter()
            .map(Control::ControllerDefaultsField),
        );
        controls.push(Control::ControllerDefaultsToggle("ipv6_outbound"));
    }
    if state.defaults != state.defaults_baseline {
        controls.extend([
            Control::ControllerDefaultsSave,
            Control::ControllerDefaultsReset,
        ]);
    }
    controls.push(Control::ControllerBack);
    controls
}

pub(in crate::tui) fn defaults_rows(controls: &[Control], width: u16) -> Vec<Vec<usize>> {
    let columns = if width >= 64 { 2 } else { 1 };
    let mut rows = Vec::new();
    let mut pending = Vec::new();
    for (index, control) in controls.iter().enumerate() {
        if *control == Control::ControllerBack {
            continue;
        }
        if matches!(
            control,
            Control::ControllerDefaultsAdvanced | Control::ControllerDefaultsSave
        ) {
            if !pending.is_empty() {
                rows.push(std::mem::take(&mut pending));
            }
            if *control == Control::ControllerDefaultsSave {
                pending.push(index);
            } else {
                rows.push(vec![index]);
            }
        } else {
            pending.push(index);
            if pending.len() == columns {
                rows.push(std::mem::take(&mut pending));
            }
        }
    }
    if !pending.is_empty() {
        rows.push(pending);
    }
    rows
}

pub(in crate::tui) fn defaults_rects(controls: &[Control], area: Rect) -> Vec<Rect> {
    let mut rects = vec![Rect::default(); controls.len()];
    for (row, indices) in defaults_rows(controls, area.width).iter().enumerate() {
        let cells = Layout::horizontal(vec![Constraint::Fill(1); indices.len()])
            .spacing(1)
            .split(Rect::new(area.x, area.y + row as u16 * 3, area.width, 3));
        for (column, &index) in indices.iter().enumerate() {
            rects[index] = cells[column];
        }
    }
    rects
}

pub(in crate::tui) fn backup_catalog_controls(value: &serde_json::Value) -> Vec<Control> {
    let mut controls: Vec<_> = (0..value["items"].as_array().map_or(0, Vec::len))
        .map(Control::ControllerBackupDetail)
        .collect();
    let offset = value["offset"].as_u64().unwrap_or(0) as usize;
    if offset > 0 {
        controls.push(Control::ControllerBackupCatalog(offset.saturating_sub(8)));
    }
    if let Some(next) = value["next_offset"].as_u64() {
        controls.push(Control::ControllerBackupCatalog(next as usize));
    }
    controls.push(Control::ControllerBack);
    controls
}

pub(in crate::tui) fn update_result_available(value: &serde_json::Value) -> bool {
    if update_operation_active(&value["latest_job"]["status"]) {
        return false;
    }
    let Some(id) = value
        .pointer("/latest_job/id")
        .and_then(serde_json::Value::as_str)
    else {
        return false;
    };
    !value["dismissed_job_ids"]
        .as_array()
        .is_some_and(|items| items.iter().any(|item| item == id))
}
pub(in crate::tui) fn failed_update_nodes(value: &serde_json::Value) -> Vec<serde_json::Value> {
    value
        .pointer("/latest_job/items")
        .and_then(serde_json::Value::as_array)
        .into_iter()
        .flatten()
        .filter(|item| {
            item["node_key"]
                .as_str()
                .is_some_and(|key| key != "@driver")
                && matches!(
                    item["status"].as_str(),
                    Some("blocked" | "failed" | "superseded")
                )
        })
        .map(|item| {
            let mut item = item.clone();
            item["key"] = item["node_key"].clone();
            item
        })
        .collect()
}

pub(in crate::tui) fn update_operation_active(value: &serde_json::Value) -> bool {
    matches!(value.as_str(), Some("awaiting_executor" | "running"))
}

pub(in crate::tui) fn draw_controller(
    frame: &mut Frame,
    app: &App,
    area: Rect,
    hits: &mut Vec<Hit>,
) {
    let right = draw_navigation(frame, app, area, hits);
    if let Some(result) = &app.controller.telegram_result {
        let popup = centered(right, 78, 12);
        frame.render_widget(Clear, popup);
        let block = section_block(" Result ");
        let inner = block.inner(popup);
        frame.render_widget(block, popup);
        let rows = Layout::vertical([Constraint::Min(0), Constraint::Length(3)]).split(inner);
        frame.render_widget(
            Paragraph::new(result.as_str()).wrap(Wrap { trim: false }),
            rows[0],
        );
        draw_button(frame, rows[1], "Close", true);
        hits.push(Hit {
            area: rows[1],
            control: Control::ControllerBack,
        });
        return;
    }

    let right = if app.controller.defaults_input.is_some()
        || (app.controller.announcement_draft.is_some()
            && app.controller.announcement_preview.is_none()
            && app.controller.announcement_job.is_none())
    {
        let popup = centered(
            right,
            if app.controller.text_confirmation {
                78
            } else {
                76
            },
            if app.controller.text_confirmation {
                12
            } else if app.controller.defaults_input.is_some() {
                10
            } else {
                18
            },
        );
        frame.render_widget(Clear, popup);
        popup
    } else {
        right
    };
    let mut parts = dialog(
        frame,
        right,
        if app.controller_ready() || app.controller.verifying {
            if app.form.action == Action::Update {
                " Node Plane Updates "
            } else if app.form.action == Action::Diagnose {
                " Diagnostic · Operations "
            } else {
                app.controller_section
                    .map_or("Node Plane Settings", ControllerSection::label)
            }
        } else {
            " Node Plane "
        },
    );
    parts[0].height += parts[1].height;
    let state = &app.controller;
    let mut text = String::new();
    let mut controls: Vec<(String, Control)> = Vec::new();
    if state
        .recovery_inventory
        .as_ref()
        .is_some_and(|page| page["loading"] == true)
    {
        text = "Loading operations…".into();
        controls = recovery_controls(state.recovery_inventory.as_ref().unwrap());
    } else if state.removed {
        text = "Bye-bye! Node Plane has been removed.".into();
        controls.extend([
            ("New installation".into(), Control::ControllerNew),
            ("Check installation".into(), Control::ControllerCheck),
        ]);
    } else if state.plan_pending {
        text = "Preparing confirmation…".into();
    } else if state.removal_active() {
        let empty = serde_json::json!({});
        let job = state.job.as_ref().unwrap_or(&empty);
        text = format!(
            "Removal · {}\nStep: {}",
            job["status"].as_str().unwrap_or("—"),
            job["phase"].as_str().unwrap_or("—")
        );
        if let Some(items) = job["items"].as_array() {
            for item in items {
                text.push_str(&format!(
                    "\n{} · {}",
                    item["node_key"].as_str().unwrap_or("—"),
                    item["status"].as_str().unwrap_or("—")
                ));
            }
        }
        if let Some(error) = job["error_code"].as_str() {
            text.push_str(&format!("\n{error}"));
        }
        text.push_str(if state.verifying {
            if state.verification_timed_out() {
                "\nRemoval is not yet confirmed. Refresh to check again."
            } else {
                "\nChecking that Node Plane has been removed…"
            }
        } else if job["cancel_requested"] == true {
            "\nCancelling after the current preparation step…"
        } else if state.removal_ready() {
            "\nNode cleanup is finished. Finish removal to delete the controller."
        } else {
            "\nPreparing removal. This screen updates automatically."
        });
        text.push_str(
            "\nCancellation keeps the controller; nodes already removed are not restored.",
        );
        controls = app.controller_removal_controls();
    } else if let Some(page) = &state.recovery_inventory {
        text = if page["grouped"] == true {
            format!(
                "Operations · {} active tasks\nChoose a node or the controller to view its tasks and actions.\n",
                page["total_tasks"]
            )
        } else if let Some(key) = page["node_key"].as_str() {
            let title = page["node_title"]
                .as_str()
                .filter(|v| !v.is_empty())
                .unwrap_or(if key.is_empty() { "Controller" } else { key });
            format!("Operations › {title}\n{} active tasks\n", page["total"])
        } else {
            format!("Operations · {} active tasks\n", page["total"])
        };
        if let Some(result) = page["recovery_result"].as_str() {
            text.push_str(&format!("\n{result}\n"));
        }
        if page["total"] == 0 {
            text.push_str("\nNo queued, running or blocked operations.");
        }
        for item in page["items"].as_array().into_iter().flatten() {
            text.push_str(&format!(
                "\n{} · {} · {}\nOperation: {}\n{}\n{}\n",
                operation_kind_label(item["kind"].as_str().unwrap_or("")),
                item["node_key"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .unwrap_or("Controller"),
                item["status"].as_str().unwrap_or("unknown"),
                item["id"].as_str().unwrap_or(""),
                crate::workstation::recovery_reason(item["error_code"].as_str().unwrap_or("")),
                crate::workstation::recovery_hint(item["next_step"].as_str().unwrap_or(""))
            ));
            if let Some(phase) = item["phase"].as_str().filter(|s| !s.is_empty()) {
                text.push_str(&format!("Phase: {phase}\n"));
            }
            if let Some(child) = item["related_id"].as_str().filter(|s| !s.is_empty()) {
                text.push_str(&format!("Dependent operation: {child}\n"));
            }
        }
        controls = recovery_controls(page);
    } else if state.branch_confirmation {
        let current = state
            .updates
            .as_ref()
            .and_then(|value| value["branch"].as_str())
            .unwrap_or("main");
        let next = crate::config::default_branch();
        text = format!(
            "Switch update branch from {current} to {next}? This can make an update incompatible and break the installation."
        );
        controls.extend([
            (
                "Switch branch".into(),
                Control::ControllerUpdateBranchConfirm(true),
            ),
            (
                "Cancel".into(),
                Control::ControllerUpdateBranchConfirm(false),
            ),
        ]);
    } else if let Some((kind, index)) = state.history_detail {
        let audit = kind == "audit";
        let item = if audit {
            state
                .workstation_audit
                .as_ref()
                .and_then(|v| v["items"].as_array())
                .and_then(|v| v.get(index))
        } else {
            state
                .recovery_history
                .as_ref()
                .and_then(|v| v["items"].as_array())
                .and_then(|v| v.get(index))
        };
        if let Some(item) = item {
            let field = |key: &str| {
                item[key]
                    .as_str()
                    .filter(|value| !value.is_empty())
                    .unwrap_or("—")
            };
            text = if audit {
                format!(
                    "{}\n{} · {}\nAccount: {} · {}\nSSH: {} · {}\nRequest: {}\nCommand: {}\nHTTP: {}\nTarget: {}\nKey: {}\nOutcome: {}\nEndpoint: {}",
                    audit_title(item),
                    field("phase"),
                    field("occurred_at"),
                    field("account_label"),
                    field("account_id"),
                    field("ssh_user"),
                    field("device_fingerprint"),
                    field("request_id"),
                    field("command_id"),
                    item["http_status"]
                        .as_u64()
                        .map(|v| v.to_string())
                        .unwrap_or_else(|| "—".into()),
                    field("target"),
                    field("key_fingerprint"),
                    field("outcome"),
                    field("action")
                )
            } else {
                let subject = item["node_title"]
                    .as_str()
                    .filter(|value| !value.is_empty())
                    .or_else(|| item["node_key"].as_str())
                    .unwrap_or("Controller");
                format!(
                    "{} · {}\n{}\nOperation: {}\nAction: {}\nOutcome: {}\nStatus: {}",
                    field("kind"),
                    subject,
                    field("created_at"),
                    field("operation_id"),
                    field("action"),
                    field("outcome"),
                    field("status")
                )
            };
        }
    } else if let Some(page) = &state.workstation_audit {
        controls.extend([
            ("All".into(), Control::ControllerAuditFilter(false)),
            ("Errors".into(), Control::ControllerAuditFilter(true)),
        ]);
        text = format!(
            "Workstation audit · {} events · page {}",
            page["total"],
            page["offset"].as_u64().unwrap_or(0) / 10 + 1
        );
        if let Some(items) = page["items"].as_array() {
            for (index, item) in items.iter().enumerate() {
                let (head, time) = compact_history_item(item, true);
                controls.push((
                    format!("{head} · {time}"),
                    Control::ControllerHistoryItem(index),
                ));
            }
        }
        for control in history_controls(page) {
            if let Control::ControllerHistoryPage(next) = control {
                controls.push((
                    if next { "Next page" } else { "Previous page" }.into(),
                    control,
                ));
            }
        }
    } else if let Some(page) = &state.recovery_history {
        text = format!(
            "Recovery history · {} events · page {}",
            page["total"],
            page["offset"].as_u64().unwrap_or(0) / 10 + 1
        );
        if let Some(items) = page["items"].as_array() {
            for (index, item) in items.iter().enumerate() {
                let (head, time) = compact_history_item(item, false);
                controls.push((
                    format!("{head} · {time}"),
                    Control::ControllerHistoryItem(index),
                ));
            }
        }
        for control in history_controls(page) {
            if let Control::ControllerHistoryPage(next) = control {
                controls.push((
                    if next { "Next page" } else { "Previous page" }.into(),
                    control,
                ));
            }
        }
    } else if let Some(job) = &state.announcement_job {
        let counts = &job["counts"];
        text = format!(
            "Announcement delivery · {}\nSent: {} · Failed: {} · Unknown: {} · Skipped: {}\nQueued: {} · Sending: {}",
            job["status"].as_str().unwrap_or("unknown"),
            counts["sent"],
            counts["failed"],
            counts["unknown"],
            counts["skipped"],
            counts["queued"],
            counts["claimed"]
        );
        if job["status"] == "running" {
            controls.push((
                "Refresh delivery".into(),
                Control::ControllerAnnouncementRefresh,
            ));
        }
        controls.extend([
            (
                "New announcement".into(),
                Control::ControllerAnnouncementCompose,
            ),
            ("Back".into(), Control::ControllerBack),
        ]);
    } else if let Some(preview) = &state.announcement_preview {
        text = format!(
            "Announcement preview · {} recipients\n\n{}",
            preview["recipients"],
            preview["text"].as_str().unwrap_or("")
        );
        controls.extend([
            ("Back".into(), Control::ControllerBack),
            (
                "Send announcement".into(),
                Control::ControllerAnnouncementSend,
            ),
        ]);
    } else if let Some(draft) = &state.announcement_draft {
        text = "Write an announcement (up to 3000 characters).\nThe message will be sent to eligible users.".into();
        controls.extend([
            ("Message".into(), Control::ControllerAnnouncementInput),
            ("Preview".into(), Control::ControllerAnnouncementPreview),
            ("Back".into(), Control::ControllerBack),
        ]);
        let _ = draft;
    } else if let Some(overview) = &state.announcement_overview {
        if let Some(last) = overview.get("last_job").filter(|v| v.is_object()) {
            let counts = &last["counts"];
            text = format!(
                "Last announcement · {}\nSent: {} · Failed: {} · Unknown: {} · Skipped: {}",
                last["status"].as_str().unwrap_or("unknown"),
                counts["sent"],
                counts["failed"],
                counts["unknown"],
                counts["skipped"]
            );
            if last["status"] == "running"
                && let Some(id) = last["id"]
                    .as_str()
                    .and_then(|v| uuid::Uuid::parse_str(v).ok())
            {
                controls.push((
                    "Refresh delivery".into(),
                    Control::ControllerAnnouncementRefresh,
                ));
                let _ = id;
            }
        } else {
            text = "No announcements yet.".into();
        }
        controls.extend([
            (
                "New announcement".into(),
                Control::ControllerAnnouncementCompose,
            ),
            ("Back".into(), Control::ControllerBack),
        ]);
    } else if let Some(value) = &state.alerts {
        text = format!(
            "Active: {} · Unread: {}\nLast full check: {}\nLast CPU check: {}",
            value["active_count"],
            value["unacknowledged_count"],
            value["last_scan"]["at"].as_str().unwrap_or("Never"),
            value["last_cpu_scan"]["at"].as_str().unwrap_or("Never")
        );
        if let Some(items) = value["active"].as_array() {
            for item in items.iter().skip(state.alerts_page * 5).take(5) {
                text.push_str(&format!(
                    "\n\n{} · {}{}\nSince: {}",
                    item["title"].as_str().unwrap_or("Server"),
                    alert_description(item),
                    if item["dismissed"] == true {
                        " · Dismissed"
                    } else {
                        ""
                    },
                    item["first_seen_at"].as_str().unwrap_or("")
                ));
            }
        }
        for control in alert_controls(state) {
            let label = match control {
                Control::ControllerAlertToggle(key) => match key {
                    "interval_minutes" => format!("Service checks every {} minutes", value[key]),
                    "enabled" => {
                        format!("Alerts: {}", if value[key] == true { "On" } else { "Off" })
                    }
                    _ => format!(
                        "Notify when resolved: {}",
                        if value[key] == true { "On" } else { "Off" }
                    ),
                },
                Control::ControllerAlertInterval(minutes) => format!("{minutes} min"),
                Control::ControllerAlertDismiss(index) => format!(
                    "Dismiss · {}",
                    value["active"][index]["title"].as_str().unwrap_or("Server")
                ),
                Control::ControllerAlertPage(next) => {
                    if next { "Next page" } else { "Previous page" }.into()
                }
                Control::ControllerAlerts => "Refresh".into(),
                _ => continue,
            };
            controls.push((label, control));
        }
    } else if state.text_confirmation {
        text = format!(
            "Save this text?\n\n{}",
            state
                .defaults_input
                .as_ref()
                .map(|(_, value)| value.as_str())
                .unwrap_or("")
        );
        controls.push(("Cancel".into(), Control::ControllerBack));
        controls.push(("Confirm".into(), Control::ControllerTextConfirm));
    } else if let Some((key, _)) = &state.defaults_input {
        text = match *key {
            "gate_message" => "Stub message for users when access requests are disabled".into(),
            "welcome_message" => "Welcome message for users requesting access".into(),
            "bot_title" => "Bot name".into(),
            "telegram_token" => "New bot token from BotFather. The token is hidden; saving restarts the bot.".into(),
            "telegram_admin" => "Telegram user ID of the administrator to add".into(),
            "backup_name" => "Optional name for this manual backup (up to 128 characters). Skip to create it without a name.".into(),
            "backup_import_path" => "Import backup · local JSON file path. Import adds it to the catalog; restore is a separate action.".into(),
            "backup_export_path" => "Download backup · local JSON file path. Existing files are not overwritten.".into(),
            _ => format!("Installation defaults · {key}"),
        };
        controls.push(("Value".into(), Control::ControllerDefaultsInput));
        if defaults_input_changed(state) {
            controls.push((
                (if *key == "backup_name" {
                    if state.defaults_input.as_ref().unwrap().1.trim().is_empty() {
                        "Skip"
                    } else {
                        "Continue"
                    }
                } else if key.starts_with("backup_") {
                    "Transfer file"
                } else {
                    "Continue"
                })
                .into(),
                Control::ControllerDefaultsInputApply,
            ));
        }
        controls.push(("Cancel".into(), Control::ControllerBack));
    } else if let Some(value) = state.policies.as_ref().filter(|_| app.requests_settings) {
        let requests_enabled = value["requests"]["enabled"] == true;
        let status = |enabled: bool| if enabled { "On" } else { "Off" };
        controls.push((
            format!("Access requests: {}", status(requests_enabled)),
            Control::ControllerPolicyToggle("requests"),
        ));
        if requests_enabled {
            controls.push((
                format!(
                    "Request notifications: {}",
                    status(value["requests"]["notify_requests"] == true)
                ),
                Control::ControllerPolicyToggle("notifications"),
            ));
            let welcome = value["requests"]["welcome_message"]
                .as_str()
                .unwrap_or("Request access to use this bot.");
            text = format!("Requests settings\n\nWelcome message:\n{welcome}");
            controls.push(("Welcome message".into(), Control::ControllerGateMessage));
        } else {
            let stub = value["requests"]["gate_message"].as_str().unwrap_or("");
            text = format!("Requests settings\n\nStub message:\n{stub}");
            controls.push(("Stub message".into(), Control::ControllerGateMessage));
        }
    } else if let Some(value) = &state.telegram_settings {
        text = format!(
            "Bot token: {}",
            if value["token_configured"] == true {
                "configured"
            } else {
                "missing"
            }
        );
        if value["restart_pending"] == true {
            text.push_str("\nConfiguration saved. Restart the Telegram service to apply it.");
        }
        controls.extend([
            ("Change bot token".into(), Control::ControllerTelegramToken),
            (
                "Add administrator".into(),
                Control::ControllerTelegramAdminAdd,
            ),
        ]);
        if let Some(admins) = value["admins"].as_array() {
            for (index, admin) in admins.iter().enumerate() {
                let id = admin["telegram_user_id"].as_str().unwrap_or("");
                let name = admin["username"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .map(|name| format!("@{name} · "))
                    .unwrap_or_default();
                controls.push((
                    format!("Remove · {name}{id}"),
                    Control::ControllerTelegramAdminRemove(index),
                ));
            }
        }
    } else if let Some(value) = &state.bot_title {
        text = format!("Bot name: {}", value["title"].as_str().unwrap_or(""));
        controls.push(("Edit name".into(), Control::ControllerBotTitleEdit));
    } else if let Some(value) = &state.ssh_key {
        text = format!(
            "Controller public SSH key\n{}\n{}",
            value["public_key"].as_str().unwrap_or(""),
            state.notice
        );
        controls.push(("Copy public key".into(), Control::ControllerSshKeyCopy));
    } else if let Some(value) = &state.defaults {
        let preset = value["settings"]["awg_i1_preset"]
            .as_str()
            .unwrap_or("quic");
        text = format!(
            "Defaults for new nodes\nAWG: {} · Port: {}\nPriority: {}",
            preset,
            if value["settings"]["awg_port_mode"] == "auto" {
                "Auto".to_string()
            } else {
                value["settings"]["awg_port"].to_string()
            },
            match preset {
                "dns" => "UDP 53",
                "chaos" => "UDP 1024–9999",
                _ => "UDP 443",
            }
        );
        for control in defaults_controls(state) {
            let label = match control {
                Control::ControllerDefaultsToggle("ipv6_outbound") => format!(
                    "IPv6 outbound: {}",
                    if value["settings"]["ipv6_outbound"] == "off" {
                        "Off"
                    } else {
                        "On"
                    }
                ),
                Control::ControllerDefaultsToggle(choice) => {
                    let field = if matches!(choice, "tcp" | "xhttp") {
                        "xray_transports"
                    } else {
                        "protocols"
                    };
                    format!(
                        "{} {}",
                        if value[field]
                            .as_array()
                            .is_some_and(|items| items.iter().any(|v| v == choice))
                        {
                            "✓"
                        } else {
                            "○"
                        },
                        match choice {
                            "awg" => "AmneziaWG",
                            "xray" => "VLESS",
                            "tcp" => "TCP",
                            _ => "XHTTP",
                        }
                    )
                }
                Control::ControllerDefaultsPreset => format!("AWG I-parameters: {preset}"),
                Control::ControllerDefaultsAdvanced => if state.defaults_advanced {
                    "Hide advanced options"
                } else {
                    "Advanced options"
                }
                .into(),
                Control::ControllerDefaultsMode => format!(
                    "AWG port mode: {}",
                    value["settings"]["awg_port_mode"]
                        .as_str()
                        .unwrap_or("auto")
                ),
                Control::ControllerDefaultsField(key) => format!(
                    "{}: {}",
                    match key {
                        "xray_sni" => "Xray SNI",
                        "xray_fingerprint" => "Xray fingerprint",
                        "xray_tcp_port" => "VLESS TCP port",
                        "xray_xhttp_port" => "XHTTP TCP port",
                        "xray_xhttp_path" => "XHTTP path",
                        _ => "AWG UDP port",
                    },
                    value["settings"][key]
                        .as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| value["settings"][key].to_string())
                ),
                Control::ControllerDefaultsSave => "Save".into(),
                Control::ControllerDefaultsReset => "Reset draft".into(),
                _ => continue,
            };
            controls.push((label, control));
        }
    } else if let Some(detail) = &state.backup_detail {
        text = format!(
            "{} · {}\nAccounts: {} · Profiles: {} · Nodes: {}\n{}",
            detail["created_at"].as_str().unwrap_or(""),
            detail["app_version"].as_str().unwrap_or(""),
            detail["accounts"],
            detail["profiles"],
            detail["nodes"],
            if detail["compatible"] == true {
                "Compatible"
            } else {
                "Incompatible"
            }
        );
        if detail["compatible"] == true {
            controls.push(("Restore backup".into(), Control::ControllerBackupRestore));
        }
        if let Some(path) = detail["exported_path"].as_str() {
            text.push_str(&format!("\nDownloaded: {path}"));
        }
        controls.push(("Download backup".into(), Control::ControllerBackupExport));
        controls.push(("Delete backup".into(), Control::ControllerBackupDelete));
        text.push_str(&format!(
            "\nType: {}",
            if detail["kind"] == "manual" {
                "Manual"
            } else {
                "Automatic"
            }
        ));
        if let Some(path) = detail["exported_path"].as_str() {
            text.push_str(&format!("\nSaved to: {path}"));
        }
        if let Some(name) = detail["name"].as_str().filter(|name| !name.is_empty()) {
            text.push_str(&format!("\nName: {name}"));
        }
    } else if let Some(catalog) = &state.backup_catalog {
        text = format!("Backups: {}", catalog["total"]);
        for control in backup_catalog_controls(catalog) {
            if let Control::ControllerBackupDetail(index) = control {
                let item = &catalog["items"][index];
                controls.push((
                    format!(
                        "{} · {} · {}{}",
                        if item["kind"] == "manual" {
                            "Manual"
                        } else {
                            "Automatic"
                        },
                        item["created_at"].as_str().unwrap_or(""),
                        item["app_version"]
                            .as_str()
                            .unwrap_or("")
                            .split(" · ")
                            .next()
                            .unwrap_or(""),
                        item["name"]
                            .as_str()
                            .filter(|s| !s.is_empty())
                            .map(|s| format!(" · {s}"))
                            .unwrap_or_default()
                    ),
                    control,
                ));
            } else if let Control::ControllerBackupCatalog(offset) = control {
                controls.push((
                    if offset < catalog["offset"].as_u64().unwrap_or(0) as usize {
                        "Previous page"
                    } else {
                        "Next page"
                    }
                    .into(),
                    control,
                ));
            }
        }
    } else if state.backups.is_some() || state.backup_job.is_some() {
        text = state
            .backups
            .as_ref()
            .map(|value| {
                format!(
                    "Backups: {} · {}",
                    value["count"],
                    format_backup_size(value["size_bytes"].as_u64().unwrap_or(0))
                )
            })
            .unwrap_or_else(|| "Backup operation".into());
        if let Some(job) = &state.backup_job {
            text.push_str(&format!(
                "\n{} · {}",
                job["action"].as_str().unwrap_or(""),
                job["status"].as_str().unwrap_or("")
            ));
            if job
                .pointer("/result/status")
                .and_then(serde_json::Value::as_str)
                == Some("partial")
            {
                text.push_str("\nPartially restored: some nodes need attention.");
            }
            if job["phase"] == "reconnecting" {
                text.push_str("\nReconnecting nodes and applying their settings…");
            }
            if let Some(nodes) = job
                .pointer("/result/nodes")
                .and_then(serde_json::Value::as_array)
            {
                for node in nodes {
                    text.push_str(&format!(
                        "\n{}: {}{}",
                        node["node_key"].as_str().unwrap_or("—"),
                        node["status"].as_str().unwrap_or("—"),
                        node["error_code"]
                            .as_str()
                            .map(|error| format!(" · {error}"))
                            .unwrap_or_default()
                    ));
                }
            }
            if let Some(error) = job
                .pointer("/result/error_code")
                .and_then(serde_json::Value::as_str)
            {
                text.push_str(&format!("\n{error}"));
            }
        }
        if let Some(value) = &state.backups {
            for control in backup_preferences_controls(value) {
                let label = match control {
                    Control::ControllerBackupCreate => "Create backup".into(),
                    Control::ControllerBackupCatalog(_) => "Backups list".into(),
                    Control::ControllerBackupImport => "Import backup from file".into(),
                    Control::ControllerBackupAuto => format!(
                        "Auto backups: {}",
                        if value["enabled"] == true {
                            "On"
                        } else {
                            "Off"
                        }
                    ),
                    Control::ControllerBackupInterval(hours) => {
                        if hours == 24 {
                            "1 day".into()
                        } else {
                            format!("{hours} hours")
                        }
                    }
                    Control::ControllerBackupKeep(count) => count.to_string(),
                    Control::ControllerBackups => "Refresh".into(),
                    _ => continue,
                };
                controls.push((label, control));
            }
        } else {
            controls.push(("Refresh".into(), Control::ControllerRefresh));
        }
    } else if let Some(result) = &state.update_result {
        text = format!(
            "{} · {}\n{}",
            result["target_ref"].as_str().unwrap_or("Update"),
            result["status"].as_str().unwrap_or("Unknown"),
            result["created_at"].as_str().unwrap_or("")
        );
        if let Some(error) = result
            .pointer("/result/error_code")
            .and_then(serde_json::Value::as_str)
        {
            text.push_str(&format!("\n{error}"));
        }
        if let Some(components) = result
            .pointer("/result/components")
            .and_then(serde_json::Value::as_object)
        {
            for (name, status) in components {
                text.push_str(&format!(
                    "\n{name} · {}",
                    status.as_str().unwrap_or("Unknown")
                ));
            }
        }
        if let Some(items) = result["items"].as_array() {
            for item in items.iter().skip(state.components_page * 5).take(5) {
                text.push_str(&format!(
                    "\n{} · {}{}",
                    item["title"]
                        .as_str()
                        .or_else(|| item["node_key"].as_str())
                        .unwrap_or("Server"),
                    item["status"].as_str().unwrap_or("Unknown"),
                    item["error_code"]
                        .as_str()
                        .map(|v| format!(" · {v}"))
                        .unwrap_or_default()
                ));
            }
        }
        let count = result["items"].as_array().map_or(0, Vec::len);
        if state.components_page > 0 {
            controls.push((
                "Previous page".into(),
                Control::ControllerComponentPage(false),
            ));
        }
        if (state.components_page + 1) * 5 < count {
            controls.push(("Next page".into(), Control::ControllerComponentPage(true)));
        }
        if update_operation_active(&result["status"]) {
            controls.push(("Refresh".into(), Control::ControllerUpdateResult));
        }
        if !matches!(
            result["status"].as_str(),
            Some("awaiting_executor" | "running")
        ) {
            controls.push(("Dismiss".into(), Control::ControllerUpdateDismiss));
        }
    } else if let Some(components) = &state.components {
        text = "Failed node updates. Open a server to inspect its failure and retry setup for the installed release.".into();
        if let Some(items) = components["nodes"].as_array() {
            let start = state.components_page * 5;
            for (index, item) in items.iter().enumerate().skip(start).take(5) {
                controls.push((
                    format!(
                        "{} · {}",
                        item["title"].as_str().unwrap_or("Server"),
                        item["status"].as_str().unwrap_or("Failed")
                    ),
                    Control::ControllerComponentNode(index),
                ));
            }
            if start > 0 {
                controls.push((
                    "Previous page".into(),
                    Control::ControllerComponentPage(false),
                ));
            }
            if start + 5 < items.len() {
                controls.push(("Next page".into(), Control::ControllerComponentPage(true)));
            }
        }
    } else if let Some(versions) = &state.versions {
        text = "Select an update version. Older versions may be incompatible and break the installation.".into();
        if let Some(items) = versions["items"].as_array() {
            for (index, item) in items.iter().enumerate() {
                controls.push((
                    item["version"]
                        .as_str()
                        .unwrap_or("Unknown version")
                        .to_string(),
                    Control::ControllerVersion(index),
                ));
            }
        }
        let offset = versions["offset"].as_u64().unwrap_or(0) as usize;
        if offset > 0 {
            controls.push((
                "Previous page".into(),
                Control::ControllerVersionsPage(offset.saturating_sub(8)),
            ));
        }
        if let Some(next) = versions["next_offset"].as_u64() {
            controls.push((
                "Next page".into(),
                Control::ControllerVersionsPage(next as usize),
            ));
        }
    } else if let Some(value) = &state.updates {
        text = format!(
            "{} · {}\nLatest: {} · {}\nLast check: {}\n{}",
            if value["branch"] == "main" {
                "stable"
            } else {
                "dev"
            },
            value["current_version"].as_str().unwrap_or("—"),
            if value["branch"] == "main" {
                "stable"
            } else {
                "dev"
            },
            value["remote_version"].as_str().unwrap_or("—"),
            value["last_checked_at"].as_str().unwrap_or("—"),
            value["last_error"].as_str().unwrap_or("")
        );
        if !value["last_checked_at"].as_str().unwrap_or("").is_empty() {
            text.push_str(if value["update_available"] == true {
                "\nNew version available"
            } else if value["last_error"].as_str().unwrap_or("").is_empty() {
                "\nUp to date"
            } else {
                "\nCheck failed"
            });
        }
        if let Some(job) = value.get("latest_job").filter(|job| job.is_object()) {
            text.push_str(&format!(
                "\n\nLast update: {} · {}",
                job["target_ref"].as_str().unwrap_or("—"),
                job["status"].as_str().unwrap_or("—")
            ));
            if let Some(error) = job["error_code"].as_str() {
                text.push_str(&format!("\n{error}"));
            }
        }
        if value["branch"]
            .as_str()
            .is_some_and(|branch| branch != crate::config::default_branch())
        {
            text.push_str(&format!("\n\nThis NPW manages {} controllers. Switch the controller to this channel before using other sections.", crate::config::workstation_channel()));
        }
        controls = app.controller_updates_actions();
    } else if state.removed || !app.controller_ready() && !state.verifying {
        text.push_str(if state.removed {
            "Bye-bye! Node Plane has been removed."
        } else if app.nodes_busy {
            "Checking installation…"
        } else {
            "Node Plane installation is not confirmed."
        });
        controls.push(("New installation".into(), Control::ControllerNew));
        controls.push(("Check installation".into(), Control::ControllerCheck));
    } else if let Some(plan) = &state.plan {
        let remove = plan["action"] == "remove";
        text.push_str("Type this phrase to confirm:\n");
        text.push_str(plan["confirmation_phrase"].as_str().unwrap_or(""));
        text.push_str(if remove { "\n\nNode Plane and its database will be deleted." } else { "\n\nController settings and access grants will be reset. Your administrator account and profiles are kept. A backup is created first." });
        if plan["cleanup_nodes"] == true {
            text.push_str(&format!(
                "\n{} nodes will be uninstalled.",
                plan["nodes"].as_array().map_or(0, Vec::len)
            ));
        } else {
            text.push_str("\nVPN services on nodes are kept.");
        }
        controls.push(("Confirmation phrase".into(), Control::ControllerPhrase));
        controls.push(("Confirm".into(), Control::ControllerConfirm));
    } else if app.form.action == Action::Update {
        text = "Loading updates…".into();
        controls.push(("Refresh".into(), Control::ControllerUpdates));
    } else {
        if let Some(overview) = &state.overview {
            text.push_str(&format!(
                "Accounts: {} · Profiles: {} · Nodes: {}",
                overview["counts"]["accounts"],
                overview["counts"]["profiles"],
                overview["counts"]["nodes"]
            ));
            if overview["supported"] != true {
                text.push_str("\nMaintenance is unavailable for this installation.");
            }
        } else {
            text.push_str("Loading installation…");
        }
        if let Some(job) = &state.job {
            text.push_str(&format!(
                "\n\n{} · {}\nStep: {}",
                job["action"].as_str().unwrap_or(""),
                job["status"].as_str().unwrap_or(""),
                job["phase"].as_str().unwrap_or("")
            ));
            if let Some(error) = job["error_code"].as_str() {
                text.push_str(&format!("\n{error}"));
            }
            if job["status"] == "aborted" {
                text.push_str("\nRemoval cancelled. Nodes already removed are not restored.");
            }
            if state.verifying {
                text.push_str(if state.verification_timed_out() {
                    "\nRemoval is not yet confirmed. Refresh to check again."
                } else {
                    "\nChecking that Node Plane has been removed…"
                });
            }
            if job["status"] == "blocked" {
                controls.push(("Retry".into(), Control::ControllerRetry));
            }
            if job["status"] == "awaiting_shutdown" && !state.verifying {
                text.push_str("\nFinish removal to stop and delete the controller.");
                controls.push(("Finish removal".into(), Control::ControllerShutdown));
            }
        }
        let active = state.verifying
            || state.job.as_ref().is_some_and(|j| {
                matches!(
                    j["status"].as_str(),
                    Some("queued" | "running" | "blocked" | "awaiting_shutdown")
                )
            });
        controls.extend(app.controller_root_controls(active));
    }
    if app.controller_controls().contains(&Control::ControllerBack)
        && !controls
            .iter()
            .any(|(_, control)| *control == Control::ControllerBack)
    {
        controls.push(("Back".into(), Control::ControllerBack));
    }
    // Back is pinned in the common footer, outside this scrollable list.
    controls.retain(|(_, control)| {
        *control != Control::ControllerBack
            || state.defaults_input.is_some()
            || state.plan.is_some()
            || state.announcement_preview.is_some()
    });
    if state.branch_confirmation
        || (state.plan.is_some()
            && controls
                .iter()
                .any(|(_, c)| *c == Control::ControllerPhrase))
    {
        let footer = Rect::new(
            parts[0].x,
            parts[0].bottom().saturating_sub(3),
            parts[0].width,
            3,
        );
        let field_height = if state.branch_confirmation { 0 } else { 3 };
        let body = Rect::new(
            parts[0].x,
            parts[0].y,
            parts[0].width,
            parts[0].height.saturating_sub(3 + field_height),
        );
        let lines: Vec<_> = text
            .lines()
            .enumerate()
            .map(|(index, line)| {
                if state.plan.is_some() && index == 1 {
                    Line::styled(
                        line.to_owned(),
                        Style::default()
                            .fg(Color::Cyan)
                            .add_modifier(Modifier::BOLD),
                    )
                } else {
                    Line::from(line.to_owned())
                }
            })
            .collect();
        frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), body);
        if field_height > 0 {
            let rect = Rect::new(body.x, body.bottom(), body.width, 3);
            let index = controls
                .iter()
                .position(|(_, c)| *c == Control::ControllerPhrase)
                .unwrap();
            draw_field(
                frame,
                rect,
                "Confirmation phrase",
                &state.phrase,
                state.selected == index,
            );
            hits.push(Hit {
                area: rect,
                control: Control::ControllerPhrase,
            });
        }
        let buttons: Vec<_> = controls
            .iter()
            .filter(|(_, c)| *c != Control::ControllerPhrase)
            .collect();
        let labels: Vec<_> = buttons.iter().map(|(label, _)| label.as_str()).collect();
        let cells = dialog_button_areas(footer, &labels, 1);
        for ((label, control), rect) in buttons.into_iter().zip(cells) {
            let index = controls.iter().position(|(_, c)| c == control).unwrap();
            draw_button_color(
                frame,
                rect,
                label,
                state.selected == index,
                if matches!(
                    control,
                    Control::ControllerConfirm | Control::ControllerUpdateBranchConfirm(true)
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
        return;
    }
    if state.announcement_preview.is_some() && state.announcement_job.is_none() {
        let rows = Layout::vertical([Constraint::Min(0), Constraint::Length(3)]).split(parts[0]);
        frame.render_widget(Paragraph::new(text).wrap(Wrap { trim: false }), rows[0]);
        let cells = button_areas(rows[1], 2, 1);
        for (index, (label, control)) in [
            ("Back", Control::ControllerBack),
            ("Send announcement", Control::ControllerAnnouncementSend),
        ]
        .into_iter()
        .enumerate()
        {
            draw_button(
                frame,
                cells[index],
                label,
                app.controller_controls().get(state.selected) == Some(&control),
            );
            hits.push(Hit {
                area: cells[index],
                control,
            });
        }
        return;
    }
    if let Some(draft) = &state.announcement_draft
        && state.announcement_preview.is_none()
        && state.announcement_job.is_none()
    {
        let rows = Layout::vertical([
            Constraint::Length(2),
            Constraint::Min(3),
            Constraint::Length(3),
        ])
        .spacing(1)
        .split(parts[0]);
        frame.render_widget(Paragraph::new(text).wrap(Wrap { trim: false }), rows[0]);
        draw_multiline(
            frame,
            rows[1],
            "Message",
            draft,
            app.controller_controls().get(state.selected)
                == Some(&Control::ControllerAnnouncementInput),
            app,
        );
        hits.push(Hit {
            area: rows[1],
            control: Control::ControllerAnnouncementInput,
        });
        let buttons = Layout::horizontal([Constraint::Fill(1); 2])
            .spacing(1)
            .split(rows[2]);
        for (index, (label, control)) in [
            ("Cancel", Control::ControllerBack),
            ("Preview", Control::ControllerAnnouncementPreview),
        ]
        .into_iter()
        .enumerate()
        {
            draw_button(
                frame,
                buttons[index],
                label,
                app.controller_controls().get(state.selected) == Some(&control),
            );
            hits.push(Hit {
                area: buttons[index],
                control,
            });
        }
        return;
    }
    if let Some((key, input)) = &state.defaults_input {
        let text_height = (text.lines().count() as u16 + 1).min(
            parts[0]
                .height
                .saturating_sub(if state.text_confirmation { 3 } else { 6 }),
        );
        frame.render_widget(
            Paragraph::new(text).wrap(Wrap { trim: false }),
            Rect::new(parts[0].x, parts[0].y, parts[0].width, text_height),
        );
        if !state.text_confirmation {
            let index = controls
                .iter()
                .position(|(_, c)| *c == Control::ControllerDefaultsInput)
                .unwrap_or(0);
            let rect = Rect::new(parts[0].x, parts[0].y + text_height, parts[0].width, 3);
            let hidden;
            let value = if *key == "telegram_token" {
                hidden = "•".repeat(input.chars().count());
                hidden.as_str()
            } else {
                input.as_str()
            };
            draw_field(frame, rect, "Value", value, state.selected == index);
            hits.push(Hit {
                area: rect,
                control: Control::ControllerDefaultsInput,
            });
        }
        let footer_y = parts[0].y + text_height + if state.text_confirmation { 0 } else { 3 };
        let footer = Rect::new(
            parts[0].x,
            footer_y.min(parts[0].bottom().saturating_sub(3)),
            parts[0].width,
            3,
        );
        let mut buttons = vec![(
            if state.text_confirmation {
                "Back"
            } else {
                "Cancel"
            },
            Control::ControllerBack,
        )];
        let apply = if state.text_confirmation {
            Control::ControllerTextConfirm
        } else {
            Control::ControllerDefaultsInputApply
        };
        if controls.iter().any(|(_, c)| *c == apply) {
            buttons.push((
                if state.text_confirmation {
                    "Confirm"
                } else if *key == "backup_name" && input.trim().is_empty() {
                    "Skip"
                } else {
                    "Continue"
                },
                apply,
            ));
        }
        let labels: Vec<_> = buttons.iter().map(|(label, _)| *label).collect();
        let cells = dialog_button_areas(footer, &labels, 1);
        for ((label, control), rect) in buttons.into_iter().zip(cells) {
            let index = controls.iter().position(|(_, c)| *c == control).unwrap();
            draw_button(frame, rect, label, state.selected == index);
            hits.push(Hit {
                area: rect,
                control,
            });
        }
        return;
    }
    if state.recovery_inventory.is_some() {
        // Operations is one document: the text and action buttons share one
        // viewport, so a long report cannot pin all but one button off screen.
        let body = parts[0];
        let paragraph = Paragraph::new(text).wrap(Wrap { trim: false });
        let text_lines = paragraph.line_count(body.width) + 1;
        let mut rows: Vec<Vec<usize>> = Vec::new();
        for (index, (_, control)) in controls.iter().enumerate() {
            if *control == Control::ControllerBack {
                continue;
            }
            if matches!(control, Control::ControllerRecoveryPage(_))
                && rows.last().is_some_and(|row| {
                    row.len() == 1
                        && matches!(controls[row[0]].1, Control::ControllerRecoveryPage(_))
                })
            {
                rows.last_mut().unwrap().push(index);
            } else {
                rows.push(vec![index]);
            }
        }
        let total = text_lines + rows.len() * 3;
        let capacity = usize::from(body.height).max(1);
        let selected_row = rows.iter().position(|row| row.contains(&state.selected));
        let selected_line =
            selected_row.map_or(app.controller_viewport.get(), |row| text_lines + row * 3);
        let previous = app.controller_viewport.get();
        let automatic = if previous == 0 && state.selected == 0 {
            0
        } else if selected_line + 3 > previous + capacity {
            (selected_line + 3).saturating_sub(capacity)
        } else if selected_line < previous {
            selected_line
        } else {
            previous
        };
        let offset = app.viewport(automatic, total, capacity);
        app.controller_viewport.set(offset);
        let remaining_text = text_lines.saturating_sub(offset).min(capacity);
        if remaining_text > 0 {
            frame.render_widget(
                paragraph.scroll((offset.min(u16::MAX as usize) as u16, 0)),
                Rect::new(body.x, body.y, body.width, remaining_text as u16),
            );
        }
        for (row, indices) in rows.iter().enumerate() {
            let y = text_lines + row * 3;
            if y < offset || y + 3 > offset + capacity {
                continue;
            }
            let cells = Layout::horizontal(vec![Constraint::Fill(1); indices.len()])
                .spacing(1)
                .split(Rect::new(
                    body.x,
                    body.y + (y - offset) as u16,
                    body.width,
                    3,
                ));
            for (column, &index) in indices.iter().enumerate() {
                let (label, control) = &controls[index];
                draw_button(frame, cells[column], label, state.selected == index);
                hits.push(Hit {
                    area: cells[column],
                    control: *control,
                });
            }
        }
        return;
    }
    let text_height = (text.lines().count() as u16 + 1).min(parts[0].height.saturating_sub(3));
    let lines: Vec<Line> = text
        .lines()
        .enumerate()
        .map(|(index, line)| {
            if state.removed && index == 0 {
                Line::from(vec![
                    Span::styled("Bye-bye!", Style::default().fg(Color::Red)),
                    Span::styled(
                        line.strip_prefix("Bye-bye!").unwrap_or(line).to_owned(),
                        Style::default().fg(Color::White),
                    ),
                ])
            } else if state.history_detail.is_some() && index == 0
                || state.plan.is_some() && index == 1
            {
                Line::styled(
                    line.to_owned(),
                    Style::default()
                        .fg(Color::Cyan)
                        .add_modifier(Modifier::BOLD),
                )
            } else {
                Line::from(line.to_owned())
            }
        })
        .collect();
    frame.render_widget(
        Paragraph::new(lines).wrap(Wrap { trim: false }),
        Rect::new(parts[0].x, parts[0].y, parts[0].width, text_height),
    );
    let capacity = usize::from(parts[0].height.saturating_sub(text_height) / 3).max(1);
    app.controller_width.set(parts[0].width);
    let control_ids: Vec<_> = controls.iter().map(|(_, control)| *control).collect();
    let grid = state.defaults.is_some() && state.defaults_input.is_none();
    let rows = if grid {
        defaults_rows(&control_ids, parts[0].width)
    } else {
        let mut rows = Vec::new();
        let mut index = 0;
        while index < controls.len() {
            let mut row = vec![index];
            if matches!(controls[index].1, Control::ControllerAuditFilter(_))
                && index + 1 < controls.len()
                && matches!(controls[index + 1].1, Control::ControllerAuditFilter(_))
            {
                index += 1;
                row.push(index);
            }
            if is_page_control(controls[index].1)
                && index + 1 < controls.len()
                && is_page_control(controls[index + 1].1)
            {
                index += 1;
                row.push(index);
            }
            if choice_row_label(controls[index].1).is_some()
                || state.defaults_input.is_some()
                    && matches!(
                        controls[index].1,
                        Control::ControllerDefaultsInputApply | Control::ControllerTextConfirm
                    )
            {
                while index + 1 < controls.len()
                    && (choice_row_label(controls[index].1).is_some()
                        && choice_row_label(controls[index + 1].1)
                            == choice_row_label(controls[index].1)
                        || state.defaults_input.is_some()
                            && controls[index + 1].1 == Control::ControllerBack)
                {
                    index += 1;
                    row.push(index);
                }
            }
            rows.push(row);
            index += 1;
        }
        rows
    };
    let selected_row = rows
        .iter()
        .position(|row| row.contains(&state.selected))
        .unwrap_or(rows.len().saturating_sub(1));
    let automatic = viewport_start(app.controller_viewport.get(), selected_row, capacity);
    let offset = app.viewport(automatic, rows.len(), capacity);
    app.controller_viewport.set(offset);
    for (row, indices) in rows.iter().enumerate().skip(offset).take(capacity) {
        let row_label = choice_row_label(controls[indices[0]].1);
        let cells = Layout::horizontal(vec![
            Constraint::Fill(1);
            indices.len() + usize::from(row_label.is_some())
        ])
        .spacing(1)
        .split(Rect::new(
            parts[0].x,
            parts[0].y + text_height + (row - offset) as u16 * 3,
            parts[0].width,
            3,
        ));
        if let Some(label) = row_label {
            draw_button_color(frame, cells[0], label, false, Color::DarkGray);
            let inner = Block::bordered().inner(cells[0]);
            frame.render_widget(
                Paragraph::new(label)
                    .alignment(ratatui::layout::Alignment::Center)
                    .style(Style::default().fg(Color::DarkGray)),
                inner,
            );
        }
        for (column, &index) in indices.iter().enumerate() {
            let (label, control) = &controls[index];
            let control = *control;
            let rect = cells[column + usize::from(row_label.is_some())];
            if rect.bottom() > parts[0].bottom() {
                continue;
            }
            if control == Control::ControllerPolicyToggle("traffic") && state.policies.is_none() {
                draw_button_color(frame, rect, label, false, Color::DarkGray);
                frame.render_widget(
                    Paragraph::new(label.as_str())
                        .alignment(ratatui::layout::Alignment::Center)
                        .style(Style::default().fg(Color::DarkGray)),
                    Block::bordered().inner(rect),
                );
                continue;
            }
            if control == Control::ControllerDefaultsInput {
                draw_field(
                    frame,
                    rect,
                    label,
                    &state.defaults_input.as_ref().unwrap().1,
                    index == state.selected,
                );
            } else if control == Control::ControllerAnnouncementInput {
                draw_field(
                    frame,
                    rect,
                    label,
                    state.announcement_draft.as_deref().unwrap_or(""),
                    index == state.selected,
                );
            } else if control == Control::ControllerPhrase {
                draw_field(frame, rect, label, &state.phrase, index == state.selected);
            } else if choice_row_label(control).is_some()
                || matches!(
                    control,
                    Control::ControllerAuditFilter(_)
                        | Control::ControllerUpdateAuto
                        | Control::ControllerBackupAuto
                        | Control::ControllerAlertToggle(_)
                        | Control::ControllerPolicyToggle(_)
                )
            {
                draw_choice_button(
                    frame,
                    rect,
                    label,
                    index == state.selected && !app.controller_sidebar,
                    match control {
                        Control::ControllerAuditFilter(errors) => state
                            .workstation_audit
                            .as_ref()
                            .is_some_and(|page| (page["errors_only"] == true) == errors),
                        _ => choice_selected(state, control),
                    },
                );
            } else {
                draw_button_color(
                    frame,
                    rect,
                    label,
                    index == state.selected && !app.controller_sidebar,
                    if control == Control::ControllerShutdown && !state.removal_ready() {
                        Color::DarkGray
                    } else if control == Control::ControllerAlerts && state.alerts_unread > 0
                        || matches!(
                            control,
                            Control::ControllerPlan(_)
                                | Control::ControllerBackupDelete
                                | Control::ControllerConfirm
                                | Control::ControllerShutdown
                                | Control::ControllerUpdateBranchConfirm(true)
                        )
                    {
                        Color::Red
                    } else {
                        Color::Cyan
                    },
                );
            }
            hits.push(Hit {
                area: rect,
                control,
            });
        }
    }
}

pub(in crate::tui) fn format_backup_size(bytes: u64) -> String {
    for (unit, divisor) in [
        ("GiB", 1u64 << 30),
        ("MiB", 1u64 << 20),
        ("KiB", 1u64 << 10),
    ] {
        if bytes >= divisor {
            return format!("{:.1} {unit}", bytes as f64 / divisor as f64);
        }
    }
    format!("{bytes} B")
}
pub(in crate::tui) fn backup_preferences_controls(value: &serde_json::Value) -> Vec<Control> {
    let mut controls = vec![
        Control::ControllerBackupCreate,
        Control::ControllerBackupCatalog(0),
        Control::ControllerBackupImport,
        Control::ControllerBackupAuto,
    ];
    if value["enabled"] == true {
        controls.extend([6, 12, 24].map(Control::ControllerBackupInterval));
        controls.extend([5, 10, 20].map(Control::ControllerBackupKeep));
    }
    controls.extend([Control::ControllerBackups, Control::ControllerBack]);
    controls
}
pub(in crate::tui) fn choice_row_label(control: Control) -> Option<&'static str> {
    match control {
        Control::ControllerUpdateInterval(_)
        | Control::ControllerBackupInterval(_)
        | Control::ControllerAlertInterval(_) => Some("Interval:"),
        Control::ControllerBackupKeep(_) => Some("Keep:"),
        _ => None,
    }
}
pub(in crate::tui) fn choice_selected(
    state: &crate::controller::Browser,
    control: Control,
) -> bool {
    match control {
        Control::ControllerAuditFilter(errors) => state
            .workstation_audit
            .as_ref()
            .is_some_and(|v| (v["errors_only"] == true) == errors),
        Control::ControllerUpdateInterval(n) => state
            .updates
            .as_ref()
            .is_some_and(|v| v["auto_check_interval_minutes"] == n),
        Control::ControllerUpdateAuto => state
            .updates
            .as_ref()
            .is_some_and(|v| v["auto_check_enabled"] == true),
        Control::ControllerBackupInterval(n) => state
            .backups
            .as_ref()
            .is_some_and(|v| v["interval_hours"] == n),
        Control::ControllerBackupKeep(n) => {
            state.backups.as_ref().is_some_and(|v| v["keep_count"] == n)
        }
        Control::ControllerBackupAuto => {
            state.backups.as_ref().is_some_and(|v| v["enabled"] == true)
        }
        Control::ControllerAlertInterval(n) => state
            .alerts
            .as_ref()
            .is_some_and(|v| v["interval_minutes"] == n),
        Control::ControllerAlertToggle("enabled") => {
            state.alerts.as_ref().is_some_and(|v| v["enabled"] == true)
        }
        _ => false,
    }
}
pub(in crate::tui) fn defaults_input_changed(state: &crate::controller::Browser) -> bool {
    if state
        .defaults_input
        .as_ref()
        .is_some_and(|(key, _)| *key == "backup_name")
    {
        return true;
    }
    let Some((key, input)) = &state.defaults_input else {
        return false;
    };
    if key.starts_with("backup_") || key.starts_with("telegram_") {
        return !input.trim().is_empty();
    }
    let value = match *key {
        "bot_title" => state.bot_title.as_ref().map(|v| &v["title"]),
        "gate_message" => state
            .policies
            .as_ref()
            .map(|v| &v["requests"]["gate_message"]),
        "welcome_message" => state
            .policies
            .as_ref()
            .map(|v| &v["requests"]["welcome_message"]),
        _ => state.defaults.as_ref().map(|v| &v["settings"][key]),
    };
    value.is_some_and(|v| {
        if key.ends_with("port") && input.trim().parse::<u64>().ok() == v.as_u64() {
            return false;
        }
        input.trim()
            != v.as_str().map(str::to_owned).unwrap_or_else(|| {
                if v.is_null() {
                    String::new()
                } else {
                    v.to_string()
                }
            })
    })
}
