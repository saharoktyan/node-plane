use super::super::*;

impl App {
    /// The account the active installation signs in as.
    pub(in crate::tui) fn own_account(&self) -> Option<&str> {
        self.connections
            .active()
            .map(|installation| installation.account.as_str())
            .filter(|account| !account.is_empty())
    }
    pub(in crate::tui) fn activate_access(&mut self, control: Control) -> Option<KeyEvent> {
        match control {
            Control::AccessGrants => {
                if let Some(editor) = &mut self.access.editor {
                    editor.grants_view = true;
                    editor.grants_page = 0;
                }
                self.access.selected = 0;
                None
            }
            Control::AccessGrantBack => {
                if let Some(editor) = &mut self.access.editor {
                    editor.grants_view = false;
                }
                self.access.selected = 0;
                None
            }
            Control::AccessGrantMode(future) => {
                if let Some(editor) = &mut self.access.editor {
                    editor.future_view = future;
                    editor.grants_page = 0;
                }
                self.access.selected = 0;
                None
            }
            Control::AccessGrantPage(next) => {
                if let Some(editor) = &mut self.access.editor {
                    let count = if editor.future_view {
                        editor.regions.len() + 1
                    } else {
                        editor.nodes.len()
                    };
                    let pages = count.div_ceil(self.access_capacity.get().max(1)).max(1);
                    editor.grants_page = if next {
                        (editor.grants_page + 1).min(pages - 1)
                    } else {
                        editor.grants_page.saturating_sub(1)
                    };
                }
                None
            }
            Control::AccessGrant(index, awg) => {
                if let Some(editor) = &mut self.access.editor
                    && let Some(node) = editor
                        .nodes
                        .get(index)
                        .and_then(|n| n["key"].as_str())
                        .map(str::to_owned)
                {
                    editor.toggle_grant(&node, if awg { "awg" } else { "xray" });
                }
                None
            }
            Control::AccessRule(region, awg) => {
                if let Some(editor) = &mut self.access.editor {
                    let id = region
                        .and_then(|index| editor.regions.get(index))
                        .and_then(|r| r["id"].as_str())
                        .map(str::to_owned);
                    editor.toggle_rule(id.as_deref(), if awg { "awg" } else { "xray" });
                }
                None
            }
            Control::AccessGrantAll(grant) => {
                if let Some(editor) = &mut self.access.editor {
                    let targets: Vec<_> = editor
                        .nodes
                        .iter()
                        .flat_map(|n| {
                            n["protocols"]
                                .as_array()
                                .into_iter()
                                .flatten()
                                .filter_map(|p| {
                                    Some((n["key"].as_str()?.to_owned(), p.as_str()?.to_owned()))
                                })
                        })
                        .collect();
                    for (node, protocol) in targets {
                        if editor.granted(&node, &protocol) != grant {
                            editor.toggle_grant(&node, &protocol);
                        }
                    }
                }
                None
            }
            Control::AccessEdit => {
                if let Some(id) = self
                    .access
                    .card
                    .as_ref()
                    .and_then(|v| v["id"].as_str())
                    .map(str::to_owned)
                {
                    self.access_command(crate::access::Command::EditContext {
                        id: (self.access.tab != crate::access::Tab::Accounts).then(|| id.clone()),
                        owner: (self.access.tab == crate::access::Tab::Accounts).then_some(id),
                    });
                }
                None
            }
            Control::AccessWizardNext => {
                if let Some(editor) = &mut self.access.editor {
                    if editor.fields[0].trim().is_empty() {
                        self.error = "Enter a profile name.".into();
                        return None;
                    }
                    if let Some(step) = &mut editor.creation_step {
                        *step = (*step + 1).min(2);
                    }
                    self.access.selected = 0;
                    self.error.clear();
                }
                None
            }
            Control::AccessWizardBack => {
                if self
                    .access
                    .editor
                    .as_ref()
                    .is_some_and(|editor| editor.creation_step == Some(0))
                {
                    self.access.profile_draft = self.access.editor.take();
                    self.access.creating_profile = true;
                    self.access.tab = crate::access::Tab::Accounts;
                    self.access_command(crate::access::Command::List(self.access.tab));
                } else if let Some(editor) = &mut self.access.editor
                    && let Some(step) = &mut editor.creation_step
                {
                    *step = step.saturating_sub(1);
                }
                self.access.selected = 0;
                None
            }
            Control::AccessNew => {
                self.access.provision = Some(crate::access::ProvisionDraft {
                    identity: String::new(),
                    key: uuid::Uuid::new_v4(),
                    confirm: false,
                });
                self.access.result = None;
                self.access.selected = 0;
                self.error.clear();
                None
            }
            Control::AccessIdentity => {
                self.focus(control);
                None
            }
            Control::AccessProvisionNext => {
                if let Some(draft) = &mut self.access.provision {
                    if draft.identity.trim().is_empty() {
                        self.error = "Enter a Telegram ID, @username or name.".into();
                    } else {
                        draft.confirm = true;
                        self.error.clear();
                        self.access.selected = 0;
                    }
                }
                None
            }
            Control::AccessProvisionSubmit => {
                if let Some(draft) = &self.access.provision {
                    self.access_command(crate::access::Command::Provision {
                        identity: draft.identity.trim().into(),
                        key: draft.key,
                    });
                }
                None
            }
            Control::AccessProvisionCancel => {
                if let Some(draft) = &mut self.access.provision {
                    if draft.confirm {
                        draft.confirm = false;
                    } else {
                        self.access.provision = None;
                    }
                }
                self.access.selected = 0;
                self.error.clear();
                None
            }
            Control::AccessResultClose => {
                self.access.result = None;
                self.access.selected = 0;
                None
            }
            Control::AccessField(_) => {
                self.focus(control);
                None
            }
            Control::AccessSave => {
                if let Some(editor) = &self.access.editor {
                    match editor.save() {
                        Ok(command) => self.access_command(command),
                        Err(error) => self.error = error.to_string(),
                    }
                }
                None
            }
            Control::AccessDeviceItem(index) => {
                if let Some(devices) = &mut self.access.devices {
                    devices.card = devices.items.get(index).cloned();
                }
                self.access.selected = 0;
                None
            }
            Control::AccessDeviceNew | Control::AccessDeviceEdit => {
                if let Some(devices) = &mut self.access.devices {
                    devices.edit(control == Control::AccessDeviceNew);
                }
                self.error.clear();
                self.access.selected = 0;
                None
            }
            Control::AccessDeviceName => {
                self.focus(control);
                None
            }
            Control::AccessDeviceSave => {
                if let Some(devices) = &self.access.devices {
                    match devices.save() {
                        Ok(command) => self.access_command(command),
                        Err(error) => self.error = error.to_string(),
                    }
                }
                None
            }
            Control::AccessDeviceDelete => {
                if let Some(devices) = &mut self.access.devices {
                    devices.delete_key = Some(uuid::Uuid::new_v4());
                }
                self.access.selected = 0;
                None
            }
            Control::AccessDeviceRotate(awg) => {
                if let Some(devices) = &mut self.access.devices {
                    devices.rotate_key = Some(uuid::Uuid::new_v4());
                    devices.rotate_awg = awg;
                }
                self.access.selected = 0;
                None
            }
            Control::AccessTraffic => {
                self.access.traffic = Some(0);
                self.access.selected = 0;
                None
            }
            Control::AccessTrafficPage(next) => {
                if let Some(page) = &mut self.access.traffic {
                    // The drawing clamps the page to the last one.
                    *page = if next {
                        *page + 1
                    } else {
                        page.saturating_sub(1)
                    };
                }
                None
            }
            Control::AccessLimits => {
                use crate::access::text;
                let scope = match (&self.access.devices, &self.access.card) {
                    (Some(devices), _) => {
                        let Some(card) = &devices.card else {
                            return None;
                        };
                        crate::policy_scope::Scope::Device {
                            profile: text(&devices.profile["id"]),
                            id: text(&card["id"]),
                            title: text(&card["display_name"]),
                        }
                    }
                    (None, Some(card)) => crate::policy_scope::Scope::Profile {
                        id: text(&card["id"]),
                        title: text(&card["display_name"]),
                    },
                    (None, None) => return None,
                };
                self.open_scoped_policies(scope);
                None
            }
            Control::AccessTariff => {
                use crate::access::text;
                let card = self.access.card.as_ref()?;
                let (id, title) = (text(&card["id"]), text(&card["display_name"]));
                self.open_profile_tariff(id, title);
                None
            }
            Control::AccessRenewal => {
                let card = self.access.card.as_ref()?;
                let id = card["_summary"]["renewal"]["id"].as_str()?;
                let id = uuid::Uuid::parse_str(id).ok()?;
                self.open_renewal(id);
                None
            }
            Control::AccessDeviceConfirm => {
                if let Some(devices) = &self.access.devices
                    && let (Some(card), Some(key)) =
                        (&devices.card, devices.delete_key.or(devices.rotate_key))
                    && let Some(revision) = card["revision"].as_u64()
                {
                    self.access_command(crate::access::Command::DeviceMutate {
                        profile: crate::access::text(&devices.profile["id"]),
                        device: card["id"].as_str().map(str::to_owned),
                        revision,
                        name: String::new(),
                        delete: devices.delete_key.is_some(),
                        rotate: devices
                            .rotate_key
                            .map(|_| if devices.rotate_awg { "awg" } else { "xray" }.to_string()),
                        key,
                    });
                }
                None
            }
            Control::AccessDevicePage(next) => {
                if let Some(devices) = &mut self.access.devices {
                    let last = devices
                        .items
                        .len()
                        .div_ceil(self.access_capacity.get().max(1))
                        .saturating_sub(1);
                    devices.page = if next {
                        (devices.page + 1).min(last)
                    } else {
                        devices.page.saturating_sub(1)
                    };
                }
                self.access.selected = 0;
                None
            }
            Control::AccessDurationCustom => {
                if let Some(editor) = &mut self.access.editor {
                    editor.duration_input = Some(editor.duration_draft.clone());
                }
                self.access.selected = 0;
                None
            }
            Control::AccessDurationInput => {
                self.focus(control);
                None
            }
            Control::AccessDurationApply => {
                if let Some(editor) = &mut self.access.editor
                    && let Some(input) = &editor.duration_input
                {
                    match crate::access::expiry_from_duration(input, std::time::SystemTime::now()) {
                        Ok(date) => {
                            editor.duration_draft = input.clone();
                            editor.fields[1] = date;
                            editor.duration_input = None;
                            editor.expiry_view = false;
                            editor.key = uuid::Uuid::new_v4();
                            self.access.selected = 0;
                            self.error.clear();
                        }
                        Err(error) => self.error = error.to_string(),
                    }
                }
                None
            }
            Control::AccessExpiryMenu => {
                if let Some(editor) = &mut self.access.editor {
                    editor.expiry_view = true;
                    editor.expiry_page = 0;
                }
                self.access.selected = 0;
                None
            }
            Control::AccessExpiry(days) => {
                if let Some(editor) = &mut self.access.editor {
                    match days
                        .map(|d| crate::access::expiry_after(d, std::time::SystemTime::now()))
                        .transpose()
                    {
                        Ok(date) => {
                            editor.fields[1] = date.unwrap_or_default();
                            editor.key = uuid::Uuid::new_v4();
                            editor.expiry_view = false;
                            self.access.selected = 0;
                        }
                        Err(error) => self.error = error.to_string(),
                    }
                }
                None
            }
            Control::AccessExpiryPage(next) => {
                if let Some(editor) = &mut self.access.editor {
                    let last = 6usize
                        .div_ceil(self.access_capacity.get().max(1))
                        .saturating_sub(1);
                    editor.expiry_page = if next {
                        (editor.expiry_page + 1).min(last)
                    } else {
                        editor.expiry_page.saturating_sub(1)
                    };
                }
                self.access.selected = 0;
                None
            }
            Control::AccessExpiryBack => {
                if let Some(editor) = &mut self.access.editor {
                    if let Some(input) = editor.duration_input.take() {
                        editor.duration_draft = input;
                    } else {
                        editor.expiry_view = false;
                    }
                }
                self.access.selected = 0;
                self.error.clear();
                None
            }
            Control::AccessAccountAction(role) => {
                if let Some(card) = &self.access.card {
                    let card = card.get("_account").unwrap_or(card);
                    let body = if role {
                        serde_json::json!({"role": if card["role"] == "admin" { "member" } else { "admin" }})
                    } else {
                        serde_json::json!({"status": if card["status"] == "approved" { "disabled" } else { "approved" }})
                    };
                    self.access.account_action = Some((body, uuid::Uuid::new_v4()));
                    self.access.selected = 0;
                }
                None
            }
            Control::AccessLogin => {
                let login = self.access.card.as_ref().map(|card| {
                    card.get("_account").unwrap_or(card)["login"]
                        .as_str()
                        .unwrap_or("")
                        .to_owned()
                });
                self.access.login_input = login;
                self.error.clear();
                self.access.selected = 0;
                None
            }
            Control::AccessLoginInput => {
                self.focus(control);
                None
            }
            Control::AccessLoginSave => {
                if let (Some(card), Some(login)) = (&self.access.card, &self.access.login_input)
                    && let card = card.get("_account").unwrap_or(card)
                    && let (Some(id), Some(revision)) =
                        (card["id"].as_str(), card["revision"].as_u64())
                {
                    let command = crate::access::Command::AccountMutate {
                        id: id.into(),
                        revision,
                        body: serde_json::json!({ "login": login.trim() }),
                        key: uuid::Uuid::new_v4(),
                    };
                    self.access_command(command);
                }
                None
            }
            Control::AccessPassword => {
                let has = self.access.card.as_ref().is_some_and(|card| {
                    card.get("_account").unwrap_or(card)["has_password"] == true
                });
                self.access.password_input = Some(Default::default());
                self.access.password_field = usize::from(!has);
                self.error.clear();
                self.access.selected = 0;
                None
            }
            Control::AccessPasswordField(index) => {
                self.access.password_field = index;
                self.focus(control);
                None
            }
            Control::AccessPasswordSave => {
                if let Some([current, new, again]) = &self.access.password_input {
                    if new != again {
                        self.error = crate::access::password_error("password_mismatch");
                        return None;
                    }
                    let mut body = serde_json::json!({ "password": new });
                    if !current.is_empty() {
                        body["current_password"] = current.as_str().into();
                    }
                    let command = crate::access::Command::SetPassword {
                        body,
                        key: uuid::Uuid::new_v4(),
                    };
                    self.access_command(command);
                }
                None
            }
            Control::AccessInviteNew
                if crate::access::bot_username_missing(&self.access.telegram_bot) =>
            {
                // A new invite's link names the bot: its username comes first.
                self.activate(Control::AccessBotUsername);
                None
            }
            Control::AccessInviteNew => {
                self.access_command(crate::access::Command::InviteTemplates);
                None
            }
            Control::AccessMerge(_) | Control::AccessMergeReject => {
                if let Some(id) = self
                    .access
                    .card
                    .as_ref()
                    .and_then(|card| card["id"].as_str())
                {
                    let mut body = serde_json::json!({
                        "decision": if control == Control::AccessMergeReject { "reject" } else { "approve" }
                    });
                    if let Control::AccessMerge(Some(main)) = control {
                        body["keep_profile"] = if main { "main" } else { "other" }.into();
                    }
                    let command = crate::access::Command::MergeDecision {
                        id: id.to_owned(),
                        body,
                        key: uuid::Uuid::new_v4(),
                    };
                    self.access_command(command);
                }
                None
            }
            Control::AccessRegistration => {
                self.access.selected = 0;
                self.access_command(crate::access::Command::Registration);
                None
            }
            Control::AccessRegistrationOpen(open) => {
                self.access_command(crate::access::Command::RegistrationUpdate {
                    body: serde_json::json!({ "open": open }),
                    key: uuid::Uuid::new_v4(),
                });
                None
            }
            Control::AccessRegistrationCleanup(days) => {
                let days = (days > 0).then_some(days);
                self.access_command(crate::access::Command::RegistrationUpdate {
                    body: serde_json::json!({ "cleanup_days": days }),
                    key: uuid::Uuid::new_v4(),
                });
                None
            }
            Control::AccessWithoutAccess => {
                self.access.page = 0;
                self.access.search.clear();
                self.access_command(crate::access::Command::WithoutAccess);
                None
            }
            Control::AccessBotUsername => {
                self.access.bot_username_input = Some(
                    self.access.telegram_bot["username"]
                        .as_str()
                        .unwrap_or("")
                        .to_owned(),
                );
                self.error.clear();
                self.access.selected = 0;
                None
            }
            Control::AccessBotUsernameInput => {
                self.focus(control);
                None
            }
            Control::AccessBotUsernameSave => {
                if let Some(username) = &self.access.bot_username_input {
                    let command = crate::access::Command::SetBotUsername {
                        username: username.trim().trim_start_matches('@').to_owned(),
                        key: uuid::Uuid::new_v4(),
                    };
                    self.access_command(command);
                }
                None
            }
            Control::AccessInviteTemplate(index) => {
                let mut body = serde_json::json!({ "vpn": index > 0 });
                if index > 1
                    && let Some(id) = self
                        .access
                        .invite_templates
                        .as_ref()
                        .and_then(|tariffs| tariffs.get(index - 2))
                        .and_then(|tariff| tariff["id"].as_str())
                {
                    body["tariff_id"] = id.into();
                }
                self.access_command(crate::access::Command::InviteCreate {
                    body,
                    key: uuid::Uuid::new_v4(),
                });
                None
            }
            Control::AccessInviteRevoke => {
                if let Some(id) = self
                    .access
                    .card
                    .as_ref()
                    .and_then(|card| card["id"].as_str())
                {
                    let command = crate::access::Command::InviteRevoke {
                        id: id.to_owned(),
                        key: uuid::Uuid::new_v4(),
                    };
                    self.access_command(command);
                }
                None
            }
            Control::AccessInviteAccount => {
                if let Some(id) = self
                    .access
                    .card
                    .as_ref()
                    .and_then(|card| card.get("_account").unwrap_or(card)["id"].as_str())
                {
                    let command = crate::access::Command::InviteCreate {
                        body: serde_json::json!({ "account_id": id }),
                        key: uuid::Uuid::new_v4(),
                    };
                    self.access_command(command);
                }
                None
            }
            Control::AccessRemovePasskeys => {
                self.access.passkeys_confirm = true;
                self.access.selected = 0;
                None
            }
            Control::AccessUnlinkWorkstations => {
                self.access.workstations_confirm = true;
                self.access.selected = 0;
                None
            }
            Control::AccessEndSessions
            | Control::AccessRemovePasskeysConfirm
            | Control::AccessUnlinkWorkstationsConfirm => {
                if let Some(id) = self.access.card.as_ref().and_then(|card| {
                    card.get("_account").unwrap_or(card)["id"]
                        .as_str()
                        .map(str::to_owned)
                }) {
                    let key = uuid::Uuid::new_v4();
                    let command = match control {
                        Control::AccessEndSessions => {
                            crate::access::Command::EndSessions { id, key }
                        }
                        Control::AccessRemovePasskeysConfirm => {
                            crate::access::Command::RemovePasskeys { id, key }
                        }
                        _ => crate::access::Command::UnlinkWorkstations { id, key },
                    };
                    self.access_command(command);
                }
                None
            }
            Control::AccessRole(None) => {
                self.access.role_picker = true;
                self.access.selected = 0;
                None
            }
            Control::AccessRole(Some(role)) => {
                self.access.role_picker = false;
                let staff = self
                    .access
                    .card
                    .as_ref()
                    .map(|card| card.get("_account").unwrap_or(card)["role"] == "admin")
                    .unwrap_or(false);
                // Promoting to administrator and demoting are open to every
                // administrator; any other preset is an owner's choice.
                let body = match role {
                    "member" => serde_json::json!({ "role": "member" }),
                    "admin" if !staff => serde_json::json!({ "role": "admin" }),
                    _ => serde_json::json!({ "roles": [role] }),
                };
                self.access.account_action = Some((body, uuid::Uuid::new_v4()));
                self.access.selected = 0;
                None
            }
            Control::AccessAccountConfirm => {
                if let (Some(card), Some((body, key))) =
                    (&self.access.card, &self.access.account_action)
                    && let card = card.get("_account").unwrap_or(card)
                    && let (Some(id), Some(revision)) =
                        (card["id"].as_str(), card["revision"].as_u64())
                {
                    self.access.role_pending = body["role"].as_str().map(|role| role == "admin");
                    let command = crate::access::Command::AccountMutate {
                        id: id.into(),
                        revision,
                        body: body.clone(),
                        key: *key,
                    };
                    self.access.account_action = None;
                    self.access_command(command);
                }
                None
            }
            Control::AccessProfileAction(delete) => {
                self.access.profile_action = Some(delete);
                self.access.selected = 0;
                None
            }
            Control::AccessProfileConfirm => {
                if let (Some(card), Some(delete)) = (&self.access.card, self.access.profile_action)
                {
                    self.access_command(crate::access::Command::Mutate {
                        id: card["id"].as_str().map(str::to_owned),
                        revision: card["desired_revision"].as_u64(),
                        body: serde_json::json!({"frozen": card["frozen"] != true}),
                        delete,
                        key: uuid::Uuid::new_v4(),
                    });
                }
                None
            }
            Control::Access => {
                self.open_access();
                None
            }
            Control::AccessTab(tab) => {
                self.access.creating_profile = false;
                self.access.editor = None;
                self.access.devices = None;
                self.access.tab = tab;
                self.access.owner_filter = None;
                self.access.items.clear();
                self.access.page = 0;
                self.access.search.clear();
                self.access.notice.clear();
                self.access_command(crate::access::Command::List(tab));
                None
            }
            Control::AccessItem(index) => {
                if let Some(id) = self.access.items.get(index).and_then(|v| v["id"].as_str()) {
                    if self.access.creating_profile {
                        let owner = id.to_owned();
                        self.access.creating_profile = false;
                        self.access.tab = crate::access::Tab::Profiles;
                        self.access.card = None;
                        self.access.notice.clear();
                        self.access_command(crate::access::Command::EditContext {
                            id: None,
                            owner: Some(owner),
                        });
                        return None;
                    }
                    self.access.notice.clear();
                    self.access.traffic = None;
                    let tab = if self.access.items[index]["_account_only"] == true {
                        crate::access::Tab::Accounts
                    } else {
                        self.access.tab
                    };
                    self.access_command(crate::access::Command::Card(tab, id.into()));
                }
                None
            }
            Control::AccessSearch => {
                self.focus(control);
                None
            }
            Control::AccessRefresh => {
                if let Some(devices) = &self.access.devices {
                    self.access_command(crate::access::Command::Devices(crate::access::text(
                        &devices.profile["id"],
                    )));
                    return None;
                }
                let command = self
                    .access
                    .card
                    .as_ref()
                    .and_then(|v| v["id"].as_str())
                    .map(|id| crate::access::Command::Card(self.access.tab, id.into()))
                    .unwrap_or(crate::access::Command::List(self.access.tab));
                self.access_command(command);
                None
            }
            Control::AccessBack if self.access.result.is_some() => {
                self.access.result = None;
                None
            }
            Control::AccessBack if self.access.provision.is_some() => {
                self.activate_access(Control::AccessProvisionCancel)
            }
            Control::AccessBack => {
                if self.access.creating_profile {
                    self.access.creating_profile = false;
                    self.access.profile_draft = None;
                    self.access.tab = crate::access::Tab::Profiles;
                    self.access.notice.clear();
                    self.access_command(crate::access::Command::List(self.access.tab));
                    return None;
                }
                self.error.clear();
                self.access.selected = 0;
                if let Some(devices) = &mut self.access.devices {
                    if devices.editor.take().is_some()
                        || devices.delete_key.take().is_some()
                        || devices.rotate_key.take().is_some()
                    {
                        return None;
                    }
                    if devices.card.take().is_some() {
                        return None;
                    }
                    let profile = devices.profile.clone();
                    self.access.card = Some(profile);
                    self.access.devices = None;
                    self.access.notice.clear();
                    return None;
                }
                if std::mem::take(&mut self.access.role_picker)
                    || self.access.login_input.take().is_some()
                    || self.access.password_input.take().is_some()
                    || std::mem::take(&mut self.access.passkeys_confirm)
                    || std::mem::take(&mut self.access.workstations_confirm)
                    || self.access.invite_templates.take().is_some()
                    || self.access.bot_username_input.take().is_some()
                    || self.access.registration.take().is_some()
                    || self.access.editor.take().is_some()
                    || self.access.profile_action.take().is_some()
                    || self.access.account_action.take().is_some()
                    || self.access.traffic.take().is_some()
                {
                    return None;
                }
                if self.access.decision.take().is_none() {
                    self.access.card = None;
                    if self.access.tab == crate::access::Tab::Accounts {
                        self.access.tab = crate::access::Tab::Profiles;
                    }
                    self.access_command(crate::access::Command::List(self.access.tab));
                }
                None
            }
            Control::AccessPage(next) => {
                let total = self
                    .access
                    .filtered()
                    .len()
                    .div_ceil(self.access_capacity.get().max(1))
                    .max(1);
                self.access.page = if next {
                    (self.access.page + 1).min(total - 1)
                } else {
                    self.access.page.saturating_sub(1)
                };
                None
            }
            Control::AccessDecision(approve) => {
                self.access.decision = Some(approve);
                self.access.selected = 0;
                None
            }
            Control::AccessConfirm => {
                if let (Some(card), Some(approve)) = (&self.access.card, self.access.decision)
                    && let Some(id) = card["id"].as_str()
                {
                    self.access_command(crate::access::Command::Decision {
                        id: id.into(),
                        approve,
                        key: uuid::Uuid::new_v4(),
                    });
                }
                None
            }
            _ => None,
        }
    }
    pub(in crate::tui) fn access_command(&mut self, command: crate::access::Command) {
        self.nodes_requested = Some(crate::nodes::Command::Access(command));
    }
    pub(in crate::tui) fn open_access(&mut self) {
        crate::backend::ask_registration_again();
        if !self.controller_ready() {
            return;
        }
        let profile = self.form.saved.as_ref().map(|p| p.id);
        if self.access.profile != profile {
            self.access = crate::access::Browser {
                profile,
                ..Default::default()
            };
        }
        self.access.creating_profile = false;
        self.access.card = None;
        self.access.devices = None;
        self.access.decision = None;
        self.access.editor = None;
        self.access.profile_action = None;
        self.access.account_action = None;
        self.access.traffic = None;
        self.access.owner_filter = None;
        self.screen = Screen::Access;
        self.quick_focus = false;
        self.access_command(crate::access::Command::List(self.access.tab));
    }
    pub(in crate::tui) fn access_controls(&self) -> Vec<Control> {
        if self.access.result.is_some() {
            return vec![Control::AccessResultClose];
        }
        if let Some(draft) = &self.access.provision {
            return if draft.confirm {
                vec![
                    Control::AccessProvisionCancel,
                    Control::AccessProvisionSubmit,
                ]
            } else {
                vec![
                    Control::AccessIdentity,
                    Control::AccessProvisionCancel,
                    Control::AccessProvisionNext,
                ]
            };
        }
        self.hits
            .iter()
            .filter_map(|hit| {
                matches!(
                    hit.control,
                    Control::AccessTab(_)
                        | Control::AccessItem(_)
                        | Control::AccessSearch
                        | Control::AccessRefresh
                        | Control::AccessBack
                        | Control::AccessPage(_)
                        | Control::AccessDecision(_)
                        | Control::AccessConfirm
                        | Control::AccessEdit
                        | Control::AccessNew
                        | Control::AccessWizardNext
                        | Control::AccessWizardBack
                        | Control::AccessField(_)
                        | Control::AccessSave
                        | Control::AccessProfileAction(_)
                        | Control::AccessAccountAction(_)
                        | Control::AccessRole(_)
                        | Control::AccessLogin
                        | Control::AccessLoginInput
                        | Control::AccessLoginSave
                        | Control::AccessPassword
                        | Control::AccessPasswordField(_)
                        | Control::AccessPasswordSave
                        | Control::AccessEndSessions
                        | Control::AccessRemovePasskeys
                        | Control::AccessInviteNew
                        | Control::AccessInviteTemplate(_)
                        | Control::AccessInviteRevoke
                        | Control::AccessInviteAccount
                        | Control::AccessBotUsername
                        | Control::AccessRegistration
                        | Control::AccessMerge(_)
                        | Control::AccessMergeReject
                        | Control::AccessRegistrationOpen(_)
                        | Control::AccessRegistrationCleanup(_)
                        | Control::AccessWithoutAccess
                        | Control::AccessBotUsernameInput
                        | Control::AccessBotUsernameSave
                        | Control::AccessRemovePasskeysConfirm
                        | Control::AccessUnlinkWorkstations
                        | Control::AccessUnlinkWorkstationsConfirm
                        | Control::AccessDeviceItem(_)
                        | Control::AccessDeviceNew
                        | Control::AccessDeviceEdit
                        | Control::AccessDeviceName
                        | Control::AccessDeviceSave
                        | Control::AccessDeviceDelete
                        | Control::AccessDeviceRotate(_)
                        | Control::AccessDeviceConfirm
                        | Control::AccessLimits
                        | Control::AccessTariff
                        | Control::AccessRenewal
                        | Control::AccessTraffic
                        | Control::AccessTrafficPage(_)
                        | Control::AccessDevicePage(_)
                        | Control::AccessDurationCustom
                        | Control::AccessDurationInput
                        | Control::AccessDurationApply
                        | Control::AccessExpiryMenu
                        | Control::AccessExpiry(_)
                        | Control::AccessExpiryPage(_)
                        | Control::AccessExpiryBack
                        | Control::AccessAccountConfirm
                        | Control::AccessProfileConfirm
                        | Control::AccessGrants
                        | Control::AccessGrant(_, _)
                        | Control::AccessRule(_, _)
                        | Control::AccessGrantMode(_)
                        | Control::AccessGrantPage(_)
                        | Control::AccessGrantBack
                        | Control::AccessGrantAll(_)
                )
                .then_some(hit.control)
            })
            .collect()
    }
}
