use super::super::*;

impl App {
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
            Control::AccessNew => {
                if self.access.tab == crate::access::Tab::Accounts
                    || self.access.owner_filter.is_some()
                {
                    let owner = self.access.owner_filter.clone().or_else(|| {
                        self.access
                            .card
                            .as_ref()
                            .and_then(|v| v["id"].as_str())
                            .map(str::to_owned)
                    });
                    self.access_command(crate::access::Command::EditContext { id: None, owner });
                } else {
                    self.access.notice = "Select an account, then choose New profile.".into();
                    self.access.tab = crate::access::Tab::Accounts;
                    self.access_command(crate::access::Command::List(self.access.tab));
                }
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
            Control::AccessDevices => {
                if let Some(id) = self.access.card.as_ref().and_then(|v| v["id"].as_str()) {
                    self.access.notice.clear();
                    self.access_command(crate::access::Command::Devices(id.into()));
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
            Control::AccessDeviceConfirm => {
                if let Some(devices) = &self.access.devices
                    && let (Some(card), Some(key)) = (&devices.card, devices.delete_key)
                    && let Some(revision) = card["revision"].as_u64()
                {
                    self.access_command(crate::access::Command::DeviceMutate {
                        profile: crate::access::text(&devices.profile["id"]),
                        device: card["id"].as_str().map(str::to_owned),
                        revision,
                        name: String::new(),
                        delete: true,
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
                    editor.expiry_view = false;
                }
                self.access.selected = 0;
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
            Control::AccessAccountConfirm => {
                if let (Some(card), Some((body, key))) =
                    (&self.access.card, &self.access.account_action)
                    && let card = card.get("_account").unwrap_or(card)
                    && let (Some(id), Some(revision)) =
                        (card["id"].as_str(), card["revision"].as_u64())
                {
                    self.access_command(crate::access::Command::AccountMutate {
                        id: id.into(),
                        revision,
                        body: body.clone(),
                        key: *key,
                    });
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
                    self.access.notice.clear();
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
            Control::AccessBack => {
                self.error.clear();
                self.access.selected = 0;
                if let Some(devices) = &mut self.access.devices {
                    if devices.editor.take().is_some() || devices.delete_key.take().is_some() {
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
                if self.access.editor.take().is_some()
                    || self.access.profile_action.take().is_some()
                    || self.access.account_action.take().is_some()
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
        self.access.card = None;
        self.access.devices = None;
        self.access.decision = None;
        self.access.editor = None;
        self.access.profile_action = None;
        self.access.account_action = None;
        self.access.owner_filter = None;
        self.screen = Screen::Access;
        self.quick_focus = false;
        self.access_command(crate::access::Command::List(self.access.tab));
    }
    pub(in crate::tui) fn access_controls(&self) -> Vec<Control> {
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
                        | Control::AccessField(_)
                        | Control::AccessSave
                        | Control::AccessProfileAction(_)
                        | Control::AccessAccountAction(_)
                        | Control::AccessDevices
                        | Control::AccessDeviceItem(_)
                        | Control::AccessDeviceNew
                        | Control::AccessDeviceEdit
                        | Control::AccessDeviceName
                        | Control::AccessDeviceSave
                        | Control::AccessDeviceDelete
                        | Control::AccessDeviceConfirm
                        | Control::AccessDevicePage(_)
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
