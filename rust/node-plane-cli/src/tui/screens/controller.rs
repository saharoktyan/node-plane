use super::super::*;

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
        let action = item["action"].as_str().unwrap_or("Action");
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
        let action = item["action"].as_str().filter(|v| !v.is_empty());
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

pub(in crate::tui) fn alert_controls(state: &crate::controller::Browser) -> Vec<Control> {
    let mut controls = vec![
        Control::ControllerAlertToggle("enabled"),
        Control::ControllerAlertToggle("interval_minutes"),
        Control::ControllerAlertToggle("notify_resolved"),
    ];
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
        if *control == Control::ControllerDefaultsAdvanced {
            if !pending.is_empty() {
                rows.push(std::mem::take(&mut pending));
            }
            rows.push(vec![index]);
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
    let right = if app.controller.defaults_input.is_some()
        || (app.controller.announcement_draft.is_some()
            && app.controller.announcement_preview.is_none()
            && app.controller.announcement_job.is_none())
    {
        let popup = centered(right, 76, 13);
        frame.render_widget(Clear, popup);
        popup
    } else {
        right
    };
    let parts = dialog(
        frame,
        right,
        if app.controller_ready() {
            if app.form.action == Action::Update {
                " Node Plane Updates "
            } else {
                app.controller_section
                    .map_or("Node Plane Settings", ControllerSection::label)
            }
        } else {
            " Node Plane "
        },
    );
    let state = &app.controller;
    let mut text = String::new();
    let mut controls: Vec<(String, Control)> = Vec::new();
    if state.plan_pending {
        text = "Preparing confirmation…".into();
    } else if state.branch_confirmation {
        let current = state
            .updates
            .as_ref()
            .and_then(|value| value["branch"].as_str())
            .unwrap_or("main");
        let next = if current == "main" { "dev" } else { "main" };
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
                    "{}\n{} · {}\nAccount: {} · {}\nSSH: {} · {}\nRequest: {}\nCommand: {}\nHTTP: {}\nTarget: {}\nKey: {}\nOutcome: {}",
                    field("action"),
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
                    field("outcome")
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
            (
                "Send announcement".into(),
                Control::ControllerAnnouncementSend,
            ),
            ("Edit".into(), Control::ControllerAnnouncementEdit),
            ("Back".into(), Control::ControllerBack),
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
            "Active: {} · Unread: {}\nLast scan: {}",
            value["active_count"],
            value["unacknowledged_count"],
            value["last_scan"].as_str().unwrap_or("Never")
        );
        if let Some(items) = value["active"].as_array() {
            for item in items.iter().skip(state.alerts_page * 5).take(5) {
                text.push_str(&format!(
                    "\n\n{} · {}{}\nSince: {}",
                    item["title"].as_str().unwrap_or("Server"),
                    item["kind"].as_str().unwrap_or("Alert"),
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
                    "interval_minutes" => format!("Check every {} minutes", value[key]),
                    "enabled" => {
                        format!("Alerts: {}", if value[key] == true { "On" } else { "Off" })
                    }
                    _ => format!(
                        "Notify when resolved: {}",
                        if value[key] == true { "On" } else { "Off" }
                    ),
                },
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
    } else if let Some((key, _)) = &state.defaults_input {
        text = match *key {
            "gate_message" => "Message for users requesting access".into(),
            "bot_title" => "Bot name".into(),
            _ => format!("Installation defaults · {key}"),
        };
        controls.extend([
            ("Value".into(), Control::ControllerDefaultsInput),
            (
                "Apply to draft".into(),
                Control::ControllerDefaultsInputApply,
            ),
        ]);
    } else if let Some(value) = state.policies.as_ref().filter(|_| app.requests_settings) {
        text = "Requests settings".into();
        let status = |enabled: bool| if enabled { "On" } else { "Off" };
        controls.extend([
            (
                format!(
                    "Access requests: {}",
                    status(value["requests"]["enabled"] == true)
                ),
                Control::ControllerPolicyToggle("requests"),
            ),
            (
                format!(
                    "Request notifications: {}",
                    status(value["requests"]["notify_requests"] == true)
                ),
                Control::ControllerPolicyToggle("notifications"),
            ),
        ]);

        controls.push(("Request message".into(), Control::ControllerGateMessage));
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
        controls.push(("Delete backup".into(), Control::ControllerBackupDelete));
    } else if let Some(catalog) = &state.backup_catalog {
        text = format!("Backups: {}", catalog["total"]);
        for control in backup_catalog_controls(catalog) {
            if let Control::ControllerBackupDetail(index) = control {
                let item = &catalog["items"][index];
                controls.push((
                    format!(
                        "{} · {}",
                        item["created_at"].as_str().unwrap_or(""),
                        item["app_version"].as_str().unwrap_or("")
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
    } else if let Some(value) = &state.backups {
        text = format!(
            "Backups: {} · {} bytes",
            value["count"], value["size_bytes"]
        );
        if let Some(job) = &state.backup_job {
            text.push_str(&format!(
                "\n{} · {}",
                job["action"].as_str().unwrap_or(""),
                job["status"].as_str().unwrap_or("")
            ));
            if let Some(error) = job
                .pointer("/result/error_code")
                .and_then(serde_json::Value::as_str)
            {
                text.push_str(&format!("\n{error}"));
            }
        }
        controls.extend([
            ("Create backup".into(), Control::ControllerBackupCreate),
            ("Saved backups".into(), Control::ControllerBackupCatalog(0)),
            (
                format!(
                    "Auto backups: {}",
                    if value["enabled"] == true {
                        "On"
                    } else {
                        "Off"
                    }
                ),
                Control::ControllerBackupAuto,
            ),
            (
                format!("Interval: {} hours", value["interval_hours"]),
                Control::ControllerBackupInterval,
            ),
            (
                format!("Keep: {} backups", value["keep_count"]),
                Control::ControllerBackupKeep,
            ),
            ("Refresh".into(), Control::ControllerBackups),
        ]);
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
        let status = |value: &serde_json::Value| match value.as_str() {
            Some("current") => "Up to date",
            Some("required") => "Outdated",
            _ => "Unknown",
        };
        text = format!("Driver · {}", status(&components["driver_status"]));
        if let Some(job) = components.get("latest_job").filter(|v| v.is_object()) {
            text.push_str(&format!(
                "\nLast update · {}",
                job["status"].as_str().unwrap_or("Unknown")
            ));
        }
        if let Some(items) = components["nodes"].as_array() {
            let start = state.components_page * 5;
            for (index, item) in items.iter().enumerate().skip(start).take(5) {
                let current =
                    item["agent_status"] == "current" && item["runtime_status"] == "current";
                controls.push((
                    format!(
                        "{} · {}",
                        item["title"].as_str().unwrap_or("Server"),
                        if current {
                            "Up to date"
                        } else if item["agent_status"] == "unknown"
                            || item["runtime_status"] == "unknown"
                        {
                            "Unknown"
                        } else {
                            "Outdated"
                        }
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
        if components
            .get("latest_job")
            .is_some_and(|job| update_operation_active(&job["status"]))
        {
            controls.push(("Refresh".into(), Control::ControllerComponents));
        }
    } else if let Some(versions) = &state.versions {
        text = "Select an update version. Older versions may be incompatible and break the installation. The update requires confirmation before starting.".into();
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
            "{} · {}\nLatest: {}\nLast check: {}\n{}",
            value["branch"].as_str().unwrap_or("—"),
            value["current_version"].as_str().unwrap_or("—"),
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
        controls.push(("Check for updates".into(), Control::ControllerUpdateCheck));
        controls.push((
            "Choose version / rollback".into(),
            Control::ControllerVersions,
        ));
        controls.push(("Components".into(), Control::ControllerComponents));
        if update_result_available(value) {
            controls.push(("Update result".into(), Control::ControllerUpdateResult));
        }
        controls.push(("Change branch".into(), Control::ControllerUpdateBranch));
        controls.push((
            format!(
                "Auto-check: {}",
                if value["auto_check_enabled"] == true {
                    "On"
                } else {
                    "Off"
                }
            ),
            Control::ControllerUpdateAuto,
        ));
        controls.push((
            format!("Interval: {} minutes", value["auto_check_interval_minutes"]),
            Control::ControllerUpdateInterval,
        ));
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
    controls.retain(|(_, control)| *control != Control::ControllerBack);
    let text_height = (text.lines().count() as u16 + 1).min(parts[0].height.saturating_sub(3));
    let lines: Vec<Line> = text
        .lines()
        .enumerate()
        .map(|(index, line)| {
            if state.history_detail.is_some() && index == 0 || state.plan.is_some() && index == 1 {
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
        (0..controls.len()).map(|index| vec![index]).collect()
    };
    let selected_row = rows
        .iter()
        .position(|row| row.contains(&state.selected))
        .unwrap_or(rows.len().saturating_sub(1));
    let automatic = viewport_start(app.controller_viewport.get(), selected_row, capacity);
    let offset = app.viewport(automatic, rows.len(), capacity);
    app.controller_viewport.set(offset);
    for (row, indices) in rows.iter().enumerate().skip(offset).take(capacity) {
        let cells = Layout::horizontal(vec![Constraint::Fill(1); indices.len()])
            .spacing(1)
            .split(Rect::new(
                parts[0].x,
                parts[0].y + text_height + (row - offset) as u16 * 3,
                parts[0].width,
                3,
            ));
        for (column, &index) in indices.iter().enumerate() {
            let (label, control) = &controls[index];
            let control = *control;
            let rect = cells[column];
            if rect.bottom() > parts[0].bottom() {
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
            } else {
                draw_button_color(
                    frame,
                    rect,
                    label,
                    index == state.selected && !app.controller_sidebar,
                    if control == Control::ControllerAlerts && state.alerts_unread > 0
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
                    } else if control == Control::ControllerUpdateAuto
                        && state
                            .updates
                            .as_ref()
                            .is_some_and(|v| v["auto_check_enabled"] == true)
                    {
                        Color::Green
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
