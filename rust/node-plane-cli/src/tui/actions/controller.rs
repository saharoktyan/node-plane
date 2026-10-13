use super::super::*;

impl App {
    pub(in crate::tui) fn activate_controller(&mut self, control: Control) -> Option<KeyEvent> {
        match control {
            Control::ControllerAudit => {
                self.controller_command(crate::controller::Command::WorkstationAudit(0));
                None
            }
            Control::ControllerRecoveryHistory => {
                self.controller_command(crate::controller::Command::RecoveryHistory(0));
                None
            }
            Control::ControllerAnnouncement => {
                self.controller_command(crate::controller::Command::AnnouncementOverview);
                None
            }
            Control::ControllerAnnouncementCompose => {
                self.controller.announcement_draft = Some(String::new());
                self.controller.announcement_key = Some(uuid::Uuid::new_v4());
                self.controller.announcement_preview = None;
                self.controller.announcement_job = None;
                self.controller.selected = 0;
                None
            }
            Control::ControllerAnnouncementPreview => {
                if let Some(text) = self
                    .controller
                    .announcement_draft
                    .clone()
                    .filter(|v| !v.trim().is_empty())
                {
                    self.controller_command(crate::controller::Command::AnnouncementPreview(text));
                } else {
                    self.error = "Enter announcement text first.".into();
                }
                None
            }
            Control::ControllerAnnouncementSend => {
                if let (Some(preview), Some(text)) = (
                    &self.controller.announcement_preview,
                    &self.controller.announcement_draft,
                ) {
                    let key = *self
                        .controller
                        .announcement_key
                        .get_or_insert_with(uuid::Uuid::new_v4);
                    let recipients = preview["recipients"].as_u64().unwrap_or(0);
                    self.controller_command(crate::controller::Command::AnnouncementCreate(
                        text.clone(),
                        key,
                        recipients,
                    ));
                }
                None
            }
            Control::ControllerAnnouncementInput => None,
            Control::ControllerAnnouncementRefresh => {
                let id = self
                    .controller
                    .announcement_job
                    .as_ref()
                    .or_else(|| {
                        self.controller
                            .announcement_overview
                            .as_ref()
                            .and_then(|v| v.get("last_job"))
                    })
                    .and_then(|v| v["id"].as_str())
                    .and_then(|v| uuid::Uuid::parse_str(v).ok());
                if let Some(id) = id {
                    self.controller_command(crate::controller::Command::AnnouncementStatus(id));
                }
                None
            }
            Control::ControllerHistoryItem(index) => {
                let (kind, item) = if let Some(page) = &self.controller.workstation_audit {
                    (
                        "audit",
                        page["items"]
                            .as_array()
                            .and_then(|items| items.get(index))
                            .cloned(),
                    )
                } else if let Some(page) = &self.controller.recovery_history {
                    (
                        "recovery",
                        page["items"]
                            .as_array()
                            .and_then(|items| items.get(index))
                            .cloned(),
                    )
                } else {
                    ("", None)
                };
                if item.is_some() {
                    self.controller.history_detail = Some((kind, index));
                    self.controller.selected = 0;
                }
                None
            }
            Control::ControllerHistoryPage(next) => {
                let page_request = self
                    .controller
                    .workstation_audit
                    .as_ref()
                    .map(|page| (page, true))
                    .or_else(|| {
                        self.controller
                            .recovery_history
                            .as_ref()
                            .map(|page| (page, false))
                    })
                    .map(|(page, audit)| {
                        let offset = page["offset"].as_u64().unwrap_or(0) as usize;
                        let total = page["total"].as_u64().unwrap_or(0) as usize;
                        let page_size = page["page_size"].as_u64().unwrap_or(10) as usize;
                        let last = total.saturating_sub(1) / page_size.max(1) * page_size.max(1);
                        let new_offset = if next {
                            (offset + page_size).min(last)
                        } else {
                            offset.saturating_sub(page_size)
                        };
                        (new_offset, audit)
                    });
                if let Some((offset, audit)) = page_request {
                    self.controller_command(if audit {
                        crate::controller::Command::WorkstationAudit(offset)
                    } else {
                        crate::controller::Command::RecoveryHistory(offset)
                    });
                }
                None
            }
            Control::ControllerAlerts => {
                self.controller_command(crate::controller::Command::Alerts);
                None
            }
            Control::ControllerAlertInterval(minutes) => {
                if matches!(minutes, 5 | 15)
                    && self
                        .controller
                        .alerts
                        .as_ref()
                        .is_some_and(|v| v["enabled"] == true)
                {
                    self.controller_command(crate::controller::Command::AlertPreferences(
                        serde_json::json!({"interval_minutes":minutes}),
                    ));
                }
                None
            }
            Control::ControllerAlertToggle(key) => {
                if let Some(value) = &self.controller.alerts {
                    let patch = if key == "interval_minutes" {
                        serde_json::json!({"interval_minutes":if value["interval_minutes"] == 5 {15}else{5}})
                    } else {
                        serde_json::json!({key:value[key] != true})
                    };
                    self.controller_command(crate::controller::Command::AlertPreferences(patch));
                }
                None
            }
            Control::ControllerAlertDismiss(index) => {
                if let Some(id) = self
                    .controller
                    .alerts
                    .as_ref()
                    .and_then(|v| v["active"].as_array())
                    .and_then(|v| v.get(index))
                    .and_then(|v| v["event_id"].as_str())
                    .and_then(|v| uuid::Uuid::parse_str(v).ok())
                {
                    self.controller_command(crate::controller::Command::AlertDismiss(id));
                }
                None
            }
            Control::ControllerAlertPage(next) => {
                let count = self
                    .controller
                    .alerts
                    .as_ref()
                    .and_then(|v| v["active"].as_array())
                    .map_or(0, Vec::len);
                self.controller.alerts_page = if next {
                    (self.controller.alerts_page + 1).min(count.saturating_sub(1) / 5)
                } else {
                    self.controller.alerts_page.saturating_sub(1)
                };
                self.controller.selected = 0;
                None
            }
            Control::ControllerBotTitle => {
                self.controller_command(crate::controller::Command::BotTitle);
                None
            }
            Control::ControllerTelegram => {
                self.controller_command(crate::controller::Command::TelegramSettings);
                None
            }
            Control::ControllerTelegramToken | Control::ControllerTelegramAdminAdd => {
                self.controller.defaults_input = Some((
                    if control == Control::ControllerTelegramToken {
                        "telegram_token"
                    } else {
                        "telegram_admin"
                    },
                    String::new(),
                ));
                self.controller.selected = 0;
                None
            }
            Control::ControllerTelegramAdminRemove(index) => {
                let telegram_id = self.controller.telegram_settings.as_ref()?["admins"]
                    .as_array()?
                    .get(index)?["telegram_user_id"]
                    .as_str()?
                    .parse::<u64>()
                    .ok()?;
                self.controller_command(crate::controller::Command::TelegramAdmin(
                    false,
                    telegram_id,
                    uuid::Uuid::new_v4(),
                ));
                None
            }
            Control::ControllerSshKey => {
                self.controller_command(crate::controller::Command::SshKey);
                None
            }
            Control::ControllerGateMessage | Control::ControllerBotTitleEdit => {
                let (key, value) = if control == Control::ControllerGateMessage {
                    (
                        "gate_message",
                        self.controller
                            .policies
                            .as_ref()?
                            .pointer("/requests/gate_message")
                            .and_then(serde_json::Value::as_str)
                            .unwrap_or(""),
                    )
                } else {
                    (
                        "bot_title",
                        self.controller.bot_title.as_ref()?["title"]
                            .as_str()
                            .unwrap_or(""),
                    )
                };
                self.controller.defaults_input = Some((key, value.to_string()));
                self.controller.selected = 0;
                None
            }
            Control::ControllerSshKeyCopy => {
                if let Some(key) = self
                    .controller
                    .ssh_key
                    .as_ref()
                    .and_then(|v| v["public_key"].as_str())
                {
                    let result = (|| -> Result<()> {
                        if self.clipboard.is_none() {
                            self.clipboard = Some(arboard::Clipboard::new()?);
                        }
                        self.clipboard.as_mut().unwrap().set_text(key.to_string())?;
                        Ok(())
                    })();
                    if result.is_ok() {
                        self.error.clear();
                        self.controller.notice = "Public key copied.".into();
                    } else {
                        self.error = "Clipboard unavailable.".into();
                    }
                }
                None
            }
            Control::ControllerPolicies => {
                self.requests_settings = true;
                self.controller_command(crate::controller::Command::Policies);
                None
            }
            Control::ControllerPolicyToggle(choice) => {
                if let Some(value) = &self.controller.policies {
                    let (kind, patch) = match choice {
                        "traffic" => (
                            "traffic",
                            serde_json::json!({"enabled":value["traffic"]["enabled"] != true}),
                        ),
                        "requests" => (
                            "access-requests",
                            serde_json::json!({"enabled":value["requests"]["enabled"] != true}),
                        ),
                        _ => (
                            "access-requests",
                            serde_json::json!({"notify_requests":value["requests"]["notify_requests"] != true}),
                        ),
                    };
                    self.controller_command(crate::controller::Command::PolicySave(kind, patch));
                }
                None
            }
            Control::ControllerDefaultsInput => {
                self.controller.selected = 0;
                None
            }
            Control::ControllerTextConfirm => {
                if self.controller.text_confirmation {
                    self.controller.text_confirmation = false;
                    if let Some((key, input)) = self.controller.defaults_input.clone() {
                        if key == "gate_message" {
                            self.controller_command(crate::controller::Command::PolicySave(
                                "access-requests",
                                serde_json::json!({"gate_message":input}),
                            ));
                        } else if key == "bot_title" {
                            self.controller_command(crate::controller::Command::BotTitleSave(
                                input,
                            ));
                        }
                    }
                }
                None
            }
            Control::ControllerDefaultsInputApply => {
                if !defaults_input_changed(&self.controller) {
                    return None;
                }
                if let Some((key, input)) = self.controller.defaults_input.clone() {
                    if key == "telegram_token" {
                        if let Some((_, value)) = &mut self.controller.defaults_input {
                            zeroize::Zeroize::zeroize(value);
                        }
                        self.controller.defaults_input = None;
                        self.controller_command(crate::controller::Command::TelegramToken(
                            zeroize::Zeroizing::new(input),
                        ));
                        return None;
                    }
                    if key == "telegram_admin" {
                        match input.trim().parse::<u64>() {
                            Ok(id) if id > 0 => {
                                self.controller_command(crate::controller::Command::TelegramAdmin(
                                    true,
                                    id,
                                    uuid::Uuid::new_v4(),
                                ))
                            }
                            _ => self.error = "Enter a positive Telegram user ID.".into(),
                        }
                        return None;
                    }
                    if key == "backup_import_path" {
                        self.controller_command(crate::controller::Command::BackupImport(
                            input.trim().into(),
                        ));
                        return None;
                    }
                    if key == "backup_export_path" {
                        let id = self.controller.backup_detail.as_ref()?["id"]
                            .as_str()?
                            .parse()
                            .ok()?;
                        self.controller_command(crate::controller::Command::BackupExport(
                            id,
                            input.trim().into(),
                        ));
                        return None;
                    }
                    if key == "gate_message" {
                        if input.trim().is_empty() || input.chars().count() > 500 {
                            self.error = "Enter a message from 1 to 500 characters".into();
                            return None;
                        }
                        self.controller.text_confirmation = true;
                        return None;
                    }
                    if key == "bot_title" {
                        if input.trim().is_empty() || input.chars().count() > 64 {
                            self.error = "Enter a name from 1 to 64 characters".into();
                            return None;
                        }
                        self.controller.text_confirmation = true;
                        return None;
                    }
                }
                if let Some((key, input)) = &self.controller.defaults_input {
                    let value = if key.ends_with("port") {
                        match input.parse::<u16>() {
                            Ok(port) if port > 0 => serde_json::json!(port),
                            _ => {
                                self.error = "Enter a port from 1 to 65535".into();
                                return None;
                            }
                        }
                    } else {
                        serde_json::json!(input.trim())
                    };
                    if let Some(defaults) = &mut self.controller.defaults {
                        defaults["settings"][*key] = value;
                    }
                    self.action_focus = Some(Control::ControllerDefaultsField(key));
                    self.controller.defaults_input = None;
                    self.error.clear();
                }
                None
            }
            Control::ControllerDefaultsField(key) => {
                if let Some(defaults) = &self.controller.defaults {
                    let value = &defaults["settings"][key];
                    self.controller.defaults_input = Some((
                        key,
                        value.as_str().map(str::to_owned).unwrap_or_else(|| {
                            if value.is_null() {
                                String::new()
                            } else {
                                value.to_string()
                            }
                        }),
                    ));
                    self.controller.selected = 0;
                }
                None
            }
            Control::ControllerDefaultsAdvanced => {
                self.controller.defaults_advanced = !self.controller.defaults_advanced;
                self.controller.selected = 0;
                None
            }
            Control::ControllerDefaultsMode => {
                if let Some(value) = &mut self.controller.defaults {
                    if value["settings"]["awg_port_mode"] == "auto" {
                        value["settings"]["awg_port_mode"] = serde_json::json!("manual");
                        value["settings"]["awg_port"] = serde_json::json!(443);
                    } else {
                        value["settings"]["awg_port_mode"] = serde_json::json!("auto");
                        value["settings"]
                            .as_object_mut()
                            .unwrap()
                            .remove("awg_port");
                    }
                }
                None
            }
            Control::ControllerDefaults => {
                self.controller_command(crate::controller::Command::Defaults);
                None
            }
            Control::ControllerDefaultsToggle(choice) => {
                if let Some(value) = &mut self.controller.defaults {
                    let field = if matches!(choice, "tcp" | "xhttp") {
                        "xray_transports"
                    } else {
                        "protocols"
                    };
                    let mut items = value[field].as_array().cloned().unwrap_or_default();
                    if items.iter().any(|v| v == choice) {
                        items.retain(|v| v != choice);
                    } else {
                        items.push(serde_json::json!(choice));
                    }
                    value[field] = serde_json::json!(items);
                    if choice == "xray" {
                        value["xray_transports"] = if items.iter().any(|v| v == "xray") {
                            serde_json::json!(["tcp", "xhttp"])
                        } else {
                            serde_json::json!([])
                        };
                    }
                }
                None
            }
            Control::ControllerDefaultsPreset => {
                if let Some(value) = &mut self.controller.defaults {
                    value["settings"]["awg_i1_preset"] =
                        serde_json::json!(match value["settings"]["awg_i1_preset"].as_str() {
                            Some("quic") => "dns",
                            Some("dns") => "chaos",
                            _ => "quic",
                        });
                }
                None
            }
            Control::ControllerDefaultsReset => {
                let viewport = self
                    .manual_viewport
                    .get()
                    .unwrap_or(self.controller_viewport.get());
                self.controller.defaults = self.controller.defaults_baseline.clone();
                self.controller.selected = self
                    .controller
                    .selected
                    .min(defaults_controls(&self.controller).len().saturating_sub(2));
                self.manual_viewport.set(Some(viewport));
                None
            }
            Control::ControllerDefaultsSave => {
                if let Some(value) = &self.controller.defaults {
                    self.controller_command(crate::controller::Command::DefaultsSave(serde_json::json!({"protocols":value["protocols"],"xray_transports":value["xray_transports"],"settings":value["settings"]}),value["revision"].as_u64()?));
                }
                None
            }
            Control::ControllerBackupExport | Control::ControllerBackupImport => {
                let (key, path) = if control == Control::ControllerBackupImport {
                    ("backup_import_path", String::new())
                } else {
                    let id = self.controller.backup_detail.as_ref()?["id"].as_str()?;
                    ("backup_export_path", format!("node-plane-backup-{id}.json"))
                };
                self.controller.defaults_input = Some((key, path));
                self.controller.selected = 0;
                None
            }
            Control::ControllerBackups
            | Control::ControllerBackupCreate
            | Control::ControllerBackupAuto
            | Control::ControllerBackupInterval(_)
            | Control::ControllerBackupKeep(_)
            | Control::ControllerBackupCatalog(_)
            | Control::ControllerBackupDetail(_)
            | Control::ControllerBackupRestore
            | Control::ControllerBackupDelete => {
                use crate::controller::Command;
                let command = match control {
                    Control::ControllerBackups => Command::Backups,
                    Control::ControllerBackupCatalog(offset) => Command::BackupCatalog(offset),
                    Control::ControllerBackupDetail(index) => {
                        let id = self.controller.backup_catalog.as_ref()?["items"][index]["id"]
                            .as_str()
                            .and_then(|id| uuid::Uuid::parse_str(id).ok())?;
                        Command::BackupDetail(id)
                    }
                    Control::ControllerBackupCreate => Command::BackupQueue(
                        serde_json::json!({"action":"create"}),
                        uuid::Uuid::new_v4(),
                    ),
                    Control::ControllerBackupDelete => {
                        let detail = self.controller.backup_detail.as_ref()?;
                        let id = detail["id"]
                            .as_str()
                            .and_then(|v| uuid::Uuid::parse_str(v).ok())?;
                        Command::BackupDelete(id, detail["checksum"].as_str()?.into())
                    }
                    Control::ControllerBackupRestore => {
                        let detail = self.controller.backup_detail.as_ref()?;
                        Command::BackupQueue(
                            serde_json::json!({"action":"restore","backup_id":detail["id"],"checksum":detail["checksum"]}),
                            uuid::Uuid::new_v4(),
                        )
                    }
                    _ => {
                        let value = self.controller.backups.as_ref()?;
                        let patch = match control {
                            Control::ControllerBackupAuto => {
                                serde_json::json!({"enabled":value["enabled"] != true})
                            }
                            Control::ControllerBackupInterval(hours)
                                if value["enabled"] == true && matches!(hours, 6 | 12 | 24) =>
                            {
                                serde_json::json!({"interval_hours":hours})
                            }
                            Control::ControllerBackupKeep(count)
                                if value["enabled"] == true && matches!(count, 5 | 10 | 20) =>
                            {
                                serde_json::json!({"keep_count":count})
                            }
                            _ => return None,
                        };
                        Command::BackupPreferences(patch)
                    }
                };
                self.controller_command(command);
                None
            }
            Control::ControllerUpdateResult | Control::ControllerUpdateDismiss => {
                if self.controller.update_result.is_none() {
                    self.controller.components_page = 0;
                    self.controller.selected = 0;
                }
                if let Some(id) = self
                    .controller
                    .update_result
                    .as_ref()
                    .or_else(|| {
                        self.controller
                            .updates
                            .as_ref()
                            .and_then(|v| v.get("latest_job"))
                    })
                    .and_then(|v| v["id"].as_str())
                    .and_then(|id| uuid::Uuid::parse_str(id).ok())
                {
                    self.controller_command(if control == Control::ControllerUpdateDismiss {
                        crate::controller::Command::UpdateDismiss(id)
                    } else {
                        crate::controller::Command::UpdateResult(id)
                    });
                }
                None
            }
            Control::ControllerUpdateBranch => {
                self.controller.branch_confirmation = true;
                self.controller.selected = 0;
                None
            }
            Control::ControllerUpdateBranchConfirm(confirm) => {
                self.controller.branch_confirmation = false;
                self.controller.selected = 0;
                if confirm && let Some(value) = &self.controller.updates {
                    let branch = crate::config::default_branch();
                    if value["branch"] == branch {
                        self.error = "Copy this profile to the other NPW channel in Settings → Profiles, then switch the controller from that NPW.".into();
                        return None;
                    }
                    self.controller_command(crate::controller::Command::UpdatePreferences(
                        serde_json::json!({"branch":branch}),
                    ));
                }
                None
            }
            Control::ControllerComponents => {
                let nodes = failed_update_nodes(self.controller.updates.as_ref()?);
                if !nodes.is_empty() {
                    self.controller.components = Some(serde_json::json!({"nodes":nodes}));
                    self.controller.components_page = 0;
                    self.controller.selected = 0;
                }
                None
            }
            Control::ControllerComponentPage(next) => {
                let count = self
                    .controller
                    .update_result
                    .as_ref()
                    .and_then(|v| v["items"].as_array())
                    .or_else(|| {
                        self.controller
                            .components
                            .as_ref()
                            .and_then(|v| v["nodes"].as_array())
                    })
                    .map_or(0, Vec::len);
                self.controller.components_page = if next {
                    (self.controller.components_page + 1).min(count.saturating_sub(1) / 5)
                } else {
                    self.controller.components_page.saturating_sub(1)
                };
                self.controller.selected = 0;
                None
            }
            Control::ControllerComponentNode(index) => {
                if let Some(key) = self
                    .controller
                    .components
                    .as_ref()
                    .and_then(|v| v["nodes"].as_array())
                    .and_then(|v| v.get(index))
                    .and_then(|v| v["key"].as_str())
                    .map(str::to_owned)
                {
                    self.nodes_updates_origin = true;
                    self.screen = Screen::Nodes;
                    self.nodes_sidebar = false;
                    self.nodes.maintenance = None;
                    self.nodes.diagnostics = None;
                    self.nodes_requested = Some(crate::nodes::Command::Card(key));
                }
                None
            }
            Control::ControllerVersions | Control::ControllerVersionsPage(_) => {
                let offset = match control {
                    Control::ControllerVersionsPage(offset) => offset,
                    _ => 0,
                };
                self.controller_command(crate::controller::Command::UpdateVersions(offset));
                None
            }
            Control::ControllerUpdateLatest => {
                let updates = self.controller.updates.as_ref()?;
                if updates["update_available"] != true {
                    return None;
                }
                let target = updates["upstream_ref"].as_str().filter(|v| !v.is_empty())?;
                let target = target.to_string();
                self.form.select_action(Action::Update);
                self.form.fields[4] = target;
                self.form.selected = 1;
                self.screen = Screen::Form;
                None
            }
            Control::ControllerVersion(index) => {
                if let Some(item) = self
                    .controller
                    .versions
                    .as_ref()
                    .and_then(|v| v["items"].as_array())
                    .and_then(|v| v.get(index))
                {
                    let target = item["ref"].as_str().unwrap_or("").to_string();
                    if !target.is_empty() {
                        self.form.select_action(Action::Update);
                        self.form.fields[4] = target;
                        self.form.selected = 1;
                        self.screen = Screen::Form;
                        self.controller.versions = None;
                    }
                }
                None
            }
            Control::ControllerUpdates
            | Control::ControllerUpdateCheck
            | Control::ControllerUpdateAuto
            | Control::ControllerUpdateInterval(_) => {
                let command = match control {
                    Control::ControllerUpdates => crate::controller::Command::Updates,
                    Control::ControllerUpdateCheck => crate::controller::Command::UpdateCheck,
                    _ => {
                        let value = self.controller.updates.as_ref()?;
                        let patch = match control {
                            Control::ControllerUpdateAuto => {
                                serde_json::json!({"auto_check_enabled": value["auto_check_enabled"] != true})
                            }
                            Control::ControllerUpdateInterval(minutes) => {
                                if value["auto_check_enabled"] != true
                                    || ![60, 360, 1440].contains(&minutes)
                                {
                                    return None;
                                }
                                serde_json::json!({"auto_check_interval_minutes":minutes})
                            }
                            _ => return None,
                        };
                        crate::controller::Command::UpdatePreferences(patch)
                    }
                };
                self.controller_command(command);
                None
            }
            Control::ControllerSection(section) => {
                self.controller_section = Some(section);
                self.requests_settings = false;
                if section == ControllerSection::Policies {
                    self.controller_command(crate::controller::Command::Policies);
                }
                self.controller.selected = 0;
                self.controller_viewport.set(0);
                None
            }
            Control::ControllerCheck => {
                self.controller_command(crate::controller::Command::Probe);
                None
            }
            Control::ControllerNew => {
                if !self.nodes_busy {
                    self.screen = Screen::NewInstallation;
                    self.confirm = false;
                }
                None
            }
            Control::NewInstallationConfirm(yes) => {
                if yes {
                    if let Some(saved) = &self.form.saved
                        && let Err(error) = crate::controller::Browser::forget_verification(
                            &self.state_dir,
                            saved.id,
                        )
                    {
                        self.error = error.to_string();
                        return None;
                    }
                    self.controller = crate::controller::Browser::default();
                    self.fresh_installation = true;
                    self.form.select_action(Action::Install);
                    self.form.fields[7].clear();
                    self.form.fields[6].zeroize();
                    self.form.selected = 1;
                    self.screen = Screen::Form;
                    self.error.clear();
                } else {
                    self.screen = Screen::Controller;
                }
                None
            }
            Control::Controller => {
                let return_form = matches!(self.screen, Screen::Form) && !self.controller_ready();
                self.form.select_action(Action::Install);
                self.open_controller();
                self.controller_return_form = return_form;
                None
            }
            Control::ControllerScope => {
                self.controller.cleanup_nodes = !self.controller.cleanup_nodes;
                None
            }
            Control::ControllerPlan(remove) => {
                if self.nodes_busy {
                    return None;
                }
                self.controller.plan_pending = true;
                self.controller_command(crate::controller::Command::Plan {
                    action: if remove { "remove" } else { "reset" }.into(),
                    cleanup_nodes: self.controller.cleanup_nodes,
                });
                None
            }
            Control::ControllerPhrase => None,
            Control::ControllerConfirm => {
                if let Some(plan) = &self.controller.plan {
                    if self.controller.phrase != plan["confirmation_phrase"].as_str().unwrap_or("")
                    {
                        self.error = "Enter the confirmation phrase exactly.".into();
                    } else if let Ok(id) = uuid::Uuid::parse_str(plan["id"].as_str().unwrap_or(""))
                    {
                        self.controller_command(crate::controller::Command::Queue {
                            plan: id,
                            phrase: self.controller.phrase.clone(),
                            key: self.controller.key.unwrap_or_default(),
                        });
                    }
                }
                None
            }
            Control::ControllerRefresh => {
                if self.controller.verifying
                    && let Some(deployment) = self
                        .controller
                        .overview
                        .as_ref()
                        .and_then(|v| v.get("deployment"))
                        .cloned()
                {
                    self.controller.verification_started = Some(std::time::Instant::now());
                    self.controller_command(crate::controller::Command::Verify(deployment));
                    return None;
                }
                if let Some(command) = self.controller.poll() {
                    self.controller_command(command);
                } else if let Some(id) = self
                    .controller
                    .job
                    .as_ref()
                    .and_then(|j| j["id"].as_str())
                    .and_then(|s| uuid::Uuid::parse_str(s).ok())
                {
                    self.controller_command(crate::controller::Command::Job(id));
                } else {
                    self.controller_command(crate::controller::Command::Overview);
                }
                None
            }
            Control::ControllerRetry | Control::ControllerShutdown | Control::ControllerAbort => {
                if control == Control::ControllerShutdown && !self.controller.removal_ready() {
                    self.error =
                        "Wait for node cleanup to finish before removing the controller.".into();
                    return None;
                }
                if let Some(id) = self
                    .controller
                    .job
                    .as_ref()
                    .and_then(|j| j["id"].as_str())
                    .and_then(|s| uuid::Uuid::parse_str(s).ok())
                {
                    if control == Control::ControllerShutdown {
                        if self.nodes_busy {
                            return None;
                        }
                        let saved = self.form.saved.clone()?;
                        if let Err(error) =
                            self.controller.save_verification(&self.state_dir, &saved)
                        {
                            self.error = format!("Could not save removal verification: {error}");
                            return None;
                        }
                        self.controller.verifying = true;
                        self.controller.verification_started = Some(std::time::Instant::now());
                        self.probe_requested = false;
                    }
                    let key = uuid::Uuid::new_v4();
                    self.controller_command(if control == Control::ControllerShutdown {
                        crate::controller::Command::Shutdown(id, key)
                    } else if control == Control::ControllerAbort {
                        crate::controller::Command::Abort(id)
                    } else {
                        crate::controller::Command::Retry(id, key)
                    });
                }
                None
            }
            Control::ControllerBack => {
                if self.controller_return_form
                    && !self.controller_ready()
                    && !self.controller.removal_active()
                {
                    self.controller_return_form = false;
                    self.screen = Screen::Form;
                    self.nodes_requested = None;
                    return None;
                }
                if self.controller.text_confirmation {
                    self.controller.text_confirmation = false;
                    return None;
                }
                if self.controller.plan_pending {
                    return None;
                }
                if self.controller.branch_confirmation {
                    self.controller.branch_confirmation = false;
                    self.controller.selected = 0;
                    return None;
                }
                if self.controller.history_detail.take().is_some() {
                    self.controller.selected = 0;
                    return None;
                }
                if self.controller.workstation_audit.take().is_some()
                    || self.controller.recovery_history.take().is_some()
                {
                    self.controller.selected = 0;
                    return None;
                }
                if self.controller.announcement_job.take().is_some() {
                    self.controller.selected = 0;
                    return None;
                }
                if self.controller.announcement_preview.take().is_some() {
                    self.controller.selected = 0;
                    return None;
                }
                if self.controller.announcement_draft.take().is_some() {
                    self.controller.announcement_key = None;
                    self.controller.selected = 0;
                    return None;
                }
                if self.controller.announcement_overview.take().is_some() {
                    self.controller.selected = 0;
                    return None;
                }
                if let Some((key, mut value)) = self.controller.defaults_input.take() {
                    if key == "telegram_token" {
                        zeroize::Zeroize::zeroize(&mut value);
                    }
                    self.action_focus = Some(Control::ControllerDefaultsField(key));
                    self.error.clear();
                    return None;
                }
                if self.form.action == Action::Update
                    && self.controller.updates.is_some()
                    && self.controller.versions.is_none()
                    && self.controller.components.is_none()
                    && self.controller.update_result.is_none()
                {
                    self.controller.updates = None;
                    self.screen = Screen::Idle;
                    return None;
                }
                if self.requests_settings {
                    self.requests_settings = false;
                    self.controller.selected = 0;
                    self.controller_viewport.set(0);
                    return None;
                }
                if self.controller.alerts.take().is_some()
                    || self.controller.telegram_settings.take().is_some()
                    || self.controller.bot_title.take().is_some()
                    || self.controller.ssh_key.take().is_some()
                    || self.controller.defaults.take().is_some()
                    || self.controller.backup_detail.take().is_some()
                    || self.controller.backup_catalog.take().is_some()
                    || self.controller.backups.take().is_some_and(|_| {
                        self.controller.backup_job = None;
                        true
                    })
                    || self.controller.backup_job.take().is_some()
                    || self.controller.update_result.take().is_some()
                    || self.controller.components.take().is_some()
                    || self.controller.versions.take().is_some()
                    || self.controller.updates.take().is_some()
                {
                    self.controller.selected = 0;
                } else if self.controller.plan.take().is_some() {
                    self.controller.phrase.clear();
                    self.controller.selected = 0;
                } else if self.controller_section.take().is_some() {
                    self.controller.selected = 0;
                    self.controller_viewport.set(0);
                } else if self.form.action == Action::Update {
                    self.screen = Screen::Idle;
                } else {
                    self.controller.selected = 0;
                    self.controller_sidebar = true;
                }
                None
            }
            _ => None,
        }
    }
    pub(in crate::tui) fn controller_command(&mut self, command: crate::controller::Command) {
        if self.nodes_busy
            || self.controller.removed && !matches!(command, crate::controller::Command::Probe)
            || self.controller.verifying
                && !matches!(
                    command,
                    crate::controller::Command::Verify(_)
                        | crate::controller::Command::Shutdown(..)
                )
        {
            return;
        }
        self.nodes_requested = Some(crate::nodes::Command::Controller(
            self.form.saved.as_ref().map(|p| p.id),
            command,
        ));
        self.error.clear();
    }
    pub(in crate::tui) fn controller_ready(&self) -> bool {
        self.form
            .saved
            .as_ref()
            .is_some_and(|p| self.confirmed_installation == Some(p.id))
            && !self.controller.verifying
            && !self.controller.removed
    }
    pub(in crate::tui) fn has_installation(&self) -> bool {
        self.form
            .saved
            .as_ref()
            .is_some_and(|p| p.installed || self.installed_profile == Some(p.id))
    }
    pub(in crate::tui) fn remember_installed(&mut self) {
        let Some(saved) = &self.form.saved else {
            return;
        };
        self.installed_profile = Some(saved.id);
        self.confirmed_installation = Some(saved.id);
        if saved.installed {
            return;
        }
        let mut next = self.connections.clone();
        if let Some(profile) = next.installations.iter_mut().find(|p| p.id == saved.id) {
            profile.installed = true;
            match next.save(&self.state_dir) {
                Ok(()) => {
                    self.connections = next;
                    if let Some(saved) = &mut self.form.saved {
                        saved.installed = true;
                    }
                }
                Err(error) => self.error = format!("Could not save installation state: {error}"),
            }
        }
    }
    pub(in crate::tui) fn open_controller(&mut self) {
        self.controller_return_form = false;
        if !self.actions_enabled() {
            return;
        }
        let profile = self.form.saved.as_ref().map(|p| p.id);
        if self.controller.profile != profile {
            self.controller = crate::controller::Browser {
                profile,
                ..Default::default()
            };
        }
        self.controller_section = None;
        self.requests_settings = false;
        self.controller.updates = None;
        self.controller.versions = None;
        self.controller.components = None;
        self.controller.update_result = None;
        self.controller.branch_confirmation = false;
        self.controller.defaults = None;
        self.controller.defaults_input = None;
        self.controller.policies = None;
        self.controller.bot_title = None;
        self.controller.telegram_settings = None;
        self.controller.ssh_key = None;
        self.controller.alerts = None;
        self.controller.workstation_audit = None;
        self.controller.recovery_history = None;
        self.controller.history_detail = None;
        self.controller.backups = None;
        self.controller.backup_job = None;
        self.controller.backup_catalog = None;
        self.controller.backup_detail = None;
        self.controller.announcement_overview = None;
        self.controller.announcement_draft = None;
        self.controller.announcement_preview = None;
        self.controller.announcement_job = None;
        self.controller_viewport.set(0);
        self.controller.plan = None;
        self.controller.phrase.clear();
        self.controller.selected = 0;
        self.screen = Screen::Controller;
        self.quick_focus = false;
        self.controller_sidebar = false;
        if let Some(saved) = &self.form.saved
            && let Err(error) = self.controller.restore_verification(&self.state_dir, saved)
        {
            self.error = format!("Could not resume removal verification: {error}");
        }
        if let Some(command) = self.controller.poll() {
            self.controller_command(command);
        } else {
            self.controller_command(if self.controller_ready() {
                crate::controller::Command::Overview
            } else {
                crate::controller::Command::Probe
            });
        }
    }
    pub(in crate::tui) fn controller_controls(&self) -> Vec<Control> {
        let state = &self.controller;
        if state.removed {
            return vec![
                Control::ControllerNew,
                Control::ControllerCheck,
                Control::ControllerBack,
            ];
        }
        if state.plan_pending {
            return Vec::new();
        }
        if state.removal_active() {
            return self
                .controller_removal_controls()
                .into_iter()
                .map(|(_, control)| control)
                .collect();
        }
        let mut controls = Vec::new();
        if state.branch_confirmation {
            return vec![
                Control::ControllerUpdateBranchConfirm(true),
                Control::ControllerUpdateBranchConfirm(false),
            ];
        }
        if state.history_detail.is_some() {
            return vec![Control::ControllerBack];
        }
        if let Some(audit) = &state.workstation_audit {
            return history_controls(audit);
        }
        if let Some(history) = &state.recovery_history {
            return history_controls(history);
        }
        if let Some(job) = &state.announcement_job {
            let mut controls = Vec::new();
            if job["status"] == "running" {
                controls.push(Control::ControllerAnnouncementRefresh);
            }
            controls.extend([
                Control::ControllerAnnouncementCompose,
                Control::ControllerBack,
            ]);
            return controls;
        }
        if state.announcement_preview.is_some() {
            return vec![Control::ControllerBack, Control::ControllerAnnouncementSend];
        }
        if state.announcement_draft.is_some() {
            return vec![
                Control::ControllerAnnouncementInput,
                Control::ControllerAnnouncementPreview,
                Control::ControllerBack,
            ];
        }
        if state.announcement_overview.is_some() {
            return vec![
                Control::ControllerAnnouncementCompose,
                Control::ControllerBack,
            ];
        }
        if state.alerts.is_some() {
            return alert_controls(state);
        }
        if state.defaults_input.is_some() {
            let mut controls = vec![Control::ControllerDefaultsInput];
            if defaults_input_changed(state) {
                controls.push(Control::ControllerDefaultsInputApply);
            }
            controls.push(Control::ControllerBack);
            return controls;
        }
        if state.bot_title.is_some() {
            return vec![Control::ControllerBotTitleEdit, Control::ControllerBack];
        }
        if let Some(value) = &state.telegram_settings {
            let mut controls = vec![
                Control::ControllerTelegramToken,
                Control::ControllerTelegramAdminAdd,
            ];
            controls.extend(
                (0..value["admins"].as_array().map_or(0, Vec::len))
                    .map(Control::ControllerTelegramAdminRemove),
            );
            controls.push(Control::ControllerBack);
            return controls;
        }
        if state.ssh_key.is_some() {
            return vec![Control::ControllerSshKeyCopy, Control::ControllerBack];
        }
        if state.policies.is_some() && self.requests_settings {
            return vec![
                Control::ControllerPolicyToggle("requests"),
                Control::ControllerPolicyToggle("notifications"),
                Control::ControllerGateMessage,
                Control::ControllerBack,
            ];
        }
        if state.defaults.is_some() {
            return defaults_controls(&self.controller);
        }
        if let Some(detail) = &state.backup_detail {
            return if detail["compatible"] == true {
                vec![
                    Control::ControllerBackupRestore,
                    Control::ControllerBackupExport,
                    Control::ControllerBackupDelete,
                    Control::ControllerBack,
                ]
            } else {
                vec![
                    Control::ControllerBackupExport,
                    Control::ControllerBackupDelete,
                    Control::ControllerBack,
                ]
            };
        }
        if let Some(catalog) = &state.backup_catalog {
            return backup_catalog_controls(catalog);
        }
        if let Some(value) = &state.backups {
            return backup_preferences_controls(value);
        }
        if state.backup_job.is_some() {
            return vec![Control::ControllerRefresh, Control::ControllerBack];
        }
        if let Some(result) = &state.update_result {
            let count = result["items"].as_array().map_or(0, Vec::len);
            let mut controls = Vec::new();
            if state.components_page > 0 {
                controls.push(Control::ControllerComponentPage(false));
            }
            if (state.components_page + 1) * 5 < count {
                controls.push(Control::ControllerComponentPage(true));
            }
            if update_operation_active(&result["status"]) {
                controls.push(Control::ControllerUpdateResult);
            }
            if !matches!(
                result["status"].as_str(),
                Some("awaiting_executor" | "running")
            ) {
                controls.push(Control::ControllerUpdateDismiss);
            }
            controls.push(Control::ControllerBack);
            return controls;
        }
        if let Some(components) = &state.components {
            let count = components["nodes"].as_array().map_or(0, Vec::len);
            let start = state.components_page * 5;
            let mut controls: Vec<_> = (start..(start + 5).min(count))
                .map(Control::ControllerComponentNode)
                .collect();
            if start > 0 {
                controls.push(Control::ControllerComponentPage(false));
            }
            if start + 5 < count {
                controls.push(Control::ControllerComponentPage(true));
            }
            controls.push(Control::ControllerBack);
            return controls;
        }
        if let Some(versions) = &state.versions {
            let mut controls: Vec<_> = (0..versions["items"].as_array().map_or(0, Vec::len))
                .map(Control::ControllerVersion)
                .collect();
            let offset = versions["offset"].as_u64().unwrap_or(0) as usize;
            if offset > 0 {
                controls.push(Control::ControllerVersionsPage(offset.saturating_sub(8)));
            }
            if let Some(next) = versions["next_offset"].as_u64() {
                controls.push(Control::ControllerVersionsPage(next as usize));
            }
            controls.push(Control::ControllerBack);
            return controls;
        }
        if state.updates.is_some() {
            return self
                .controller_updates_actions()
                .into_iter()
                .map(|(_, control)| control)
                .collect();
        }
        if self.form.action == Action::Update {
            return vec![Control::ControllerUpdates];
        }
        if state.removed || !self.controller_ready() && !state.verifying {
            return vec![
                Control::ControllerNew,
                Control::ControllerCheck,
                Control::ControllerBack,
            ];
        }
        if state.plan.is_some() {
            return vec![
                Control::ControllerPhrase,
                Control::ControllerConfirm,
                Control::ControllerBack,
            ];
        }
        if let Some(job) = &state.job {
            if job["status"] == "blocked" {
                controls.push(Control::ControllerRetry);
            }
            if job["status"] == "awaiting_shutdown" && !state.verifying {
                controls.push(Control::ControllerShutdown);
            }
        }
        let active = state.verifying
            || state.job.as_ref().is_some_and(|j| {
                matches!(
                    j["status"].as_str(),
                    Some("queued" | "running" | "blocked" | "awaiting_shutdown")
                )
            });
        controls.extend(
            self.controller_root_controls(active)
                .into_iter()
                .map(|(_, control)| control),
        );
        controls
    }
    pub(in crate::tui) fn controller_updates_actions(&self) -> Vec<(String, Control)> {
        let Some(value) = &self.controller.updates else {
            return Vec::new();
        };
        if value["branch"]
            .as_str()
            .is_some_and(|branch| branch != crate::config::default_branch())
        {
            return vec![
                (
                    format!(
                        "Switch controller to {} NPW channel",
                        crate::config::workstation_channel()
                    ),
                    Control::ControllerUpdateBranch,
                ),
                ("Back".into(), Control::ControllerBack),
            ];
        }
        let active = value
            .get("latest_job")
            .is_some_and(|job| update_operation_active(&job["status"]));
        let mut controls = vec![("Check for updates".into(), Control::ControllerUpdateCheck)];
        if value["update_available"] == true && !active {
            controls.push(("To latest".into(), Control::ControllerUpdateLatest));
        }
        controls.extend([
            (
                "Choose version / rollback".into(),
                Control::ControllerVersions,
            ),
            (
                format!(
                    "Auto-check: {}",
                    if value["auto_check_enabled"] == true {
                        "On"
                    } else {
                        "Off"
                    }
                ),
                Control::ControllerUpdateAuto,
            ),
        ]);
        if value["auto_check_enabled"] == true {
            controls.extend([(60, "1 hour"), (360, "6 hours"), (1440, "1 day")].map(
                |(minutes, label)| (label.into(), Control::ControllerUpdateInterval(minutes)),
            ));
        }
        if active {
            controls.push(("Update progress".into(), Control::ControllerUpdateResult));
        } else if update_result_available(value) {
            controls.push(("Update results".into(), Control::ControllerUpdateResult));
        }
        if !failed_update_nodes(value).is_empty() {
            controls.push(("Failed node updates".into(), Control::ControllerComponents));
        }
        if self.form.action != Action::Update {
            controls.push(("Back".into(), Control::ControllerBack));
        }
        controls
    }
    pub(in crate::tui) fn controller_root_controls(&self, active: bool) -> Vec<(String, Control)> {
        use ControllerSection::*;
        let section =
            |value: ControllerSection| (value.label().into(), Control::ControllerSection(value));
        let mut controls: Vec<(String, Control)> = match self.controller_section {
            None => vec![
                section(Installation),
                section(Policies),
                ("Backups".into(), Control::ControllerBackups),
                section(Monitoring),
                ("Announcements".into(), Control::ControllerAnnouncement),
                section(Removal),
            ],
            Some(Installation) => vec![
                ("Installation defaults".into(), Control::ControllerDefaults),
                ("Bot name".into(), Control::ControllerBotTitle),
                ("Telegram".into(), Control::ControllerTelegram),
                ("SSH key".into(), Control::ControllerSshKey),
                ("New installation".into(), Control::ControllerNew),
            ],
            Some(Policies) => {
                let mut items = Vec::new();
                if let Some(value) = &self.controller.policies {
                    items.push((
                        format!(
                            "Traffic collection: {}",
                            if value["traffic"]["enabled"] == true {
                                "On"
                            } else {
                                "Off"
                            }
                        ),
                        Control::ControllerPolicyToggle("traffic"),
                    ));
                } else {
                    items.push((
                        "Please wait".into(),
                        Control::ControllerPolicyToggle("traffic"),
                    ));
                }
                items.push(("Requests settings".into(), Control::ControllerPolicies));
                items
            }
            Some(Monitoring) => vec![
                ("Alerts".into(), Control::ControllerAlerts),
                ("Workstation audit".into(), Control::ControllerAudit),
                (
                    "Recovery history".into(),
                    Control::ControllerRecoveryHistory,
                ),
            ],
            Some(Removal) => {
                let mut items = Vec::new();
                if !active
                    && self
                        .controller
                        .overview
                        .as_ref()
                        .map_or(self.controller_ready(), |v| v["supported"] == true)
                {
                    items.extend([
                        (
                            format!(
                                "[{}] Uninstall nodes too",
                                if self.controller.cleanup_nodes {
                                    "✓"
                                } else {
                                    " "
                                }
                            ),
                            Control::ControllerScope,
                        ),
                        ("Reset controller".into(), Control::ControllerPlan(false)),
                        ("Remove Node Plane".into(), Control::ControllerPlan(true)),
                    ]);
                }
                items.push(("Refresh".into(), Control::ControllerRefresh));
                items
            }
        };
        if self.controller_section.is_some() {
            controls.push(("Back".into(), Control::ControllerBack));
        }
        controls
    }

    pub(in crate::tui) fn controller_removal_controls(&self) -> Vec<(String, Control)> {
        if self.controller.verifying {
            return vec![("Refresh".into(), Control::ControllerRefresh)];
        }
        if self
            .controller
            .job
            .as_ref()
            .is_some_and(|job| job["cancel_requested"] == true)
        {
            return Vec::new();
        }
        vec![
            (
                if self.controller.removal_ready() {
                    "Finish removal"
                } else {
                    "✕ Finish removal (preparing)"
                }
                .into(),
                Control::ControllerShutdown,
            ),
            ("Cancel removal".into(), Control::ControllerAbort),
        ]
    }
}
