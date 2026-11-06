//! Tariffs in Workstation: the catalog under Policies, one tariff, and the
//! tariff and renewal request of one profile opened from its card.
use super::super::*;
use crate::controller::{Command, TariffDraft};
use crate::tariffs::{
    access_label, expiry_hint, expiry_label, limit_lines, next_expiry, next_term, region_chosen,
    renewal_days, term_label, toggle_all, toggle_region,
};

impl App {
    pub(in crate::tui) fn open_profile_tariff(&mut self, id: String, title: String) {
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
        self.controller_navigation_revision = self.controller_navigation_revision.wrapping_add(1);
        self.controller.scope = None;
        self.controller.tariffs = None;
        self.controller.tariff = None;
        self.controller.tariff_draft = None;
        self.controller.tariff_assign = Some(serde_json::json!({"profile_id":id,"title":title}));
        self.controller.selected = 0;
        self.controller_viewport.set(0);
        self.controller_section = Some(ControllerSection::Policies);
        self.screen = Screen::Controller;
        self.quick_focus = false;
        self.controller_sidebar = false;
        self.error.clear();
        self.controller_command(Command::TariffChoices(id));
    }

    pub(in crate::tui) fn open_renewal(&mut self, id: uuid::Uuid) {
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
        self.controller_navigation_revision = self.controller_navigation_revision.wrapping_add(1);
        self.controller.scope = None;
        self.controller.tariff_assign = None;
        self.controller.renewal = Some(serde_json::json!({"id":id}));
        self.controller.selected = 0;
        self.controller_viewport.set(0);
        self.controller_section = Some(ControllerSection::Policies);
        self.screen = Screen::Controller;
        self.quick_focus = false;
        self.controller_sidebar = false;
        self.error.clear();
        self.controller_command(Command::Renewal(id));
    }

    /// Whether a tariff view is shown instead of the section it opened from.
    pub(in crate::tui) fn tariff_view_open(&self) -> bool {
        let state = &self.controller;
        state.scope.is_none()
            && state.defaults_input.is_none()
            && (state.renewal.is_some()
                || state.tariff_assign.is_some()
                || state.tariff.is_some()
                || state.tariffs.is_some())
    }

    pub(in crate::tui) fn open_tariff_id(&self) -> Option<(uuid::Uuid, u64)> {
        let tariff = self.controller.tariff.as_ref()?;
        Some((
            uuid::Uuid::parse_str(tariff["id"].as_str()?).ok()?,
            tariff["revision"].as_u64()?,
        ))
    }

    /// Handles a tariff control; false for any other control.
    pub(in crate::tui) fn activate_tariff(&mut self, control: Control) -> bool {
        let state = &self.controller;
        let command = match control {
            Control::ControllerTariffs => Some(Command::Tariffs),
            Control::ControllerTariff(index) => state
                .tariffs
                .as_ref()
                .and_then(|list| list["items"][index]["id"].as_str())
                .and_then(|id| uuid::Uuid::parse_str(id).ok())
                .map(Command::Tariff),
            Control::ControllerTariffNew => {
                self.controller.tariff = None;
                self.open_text(
                    crate::localized::Purpose::TariffNew,
                    &serde_json::Value::Null,
                );
                None
            }
            Control::ControllerTariffRename | Control::ControllerTariffDescribe => {
                let (purpose, field) = if control == Control::ControllerTariffRename {
                    (crate::localized::Purpose::TariffTitles, "titles")
                } else {
                    (
                        crate::localized::Purpose::TariffDescriptions,
                        "descriptions",
                    )
                };
                let value = state
                    .tariff
                    .as_ref()
                    .map(|t| t[field].clone())
                    .unwrap_or_default();
                self.open_text(purpose, &value);
                None
            }
            Control::ControllerTariffTerm => self.open_tariff_id().map(|(id, revision)| {
                let days = &self.controller.tariff.as_ref().unwrap()["duration_days"];
                Command::TariffSave(
                    id,
                    serde_json::json!({"duration_days": next_term(days)}),
                    revision,
                )
            }),
            Control::ControllerTariffNodes | Control::ControllerTariffRegion(_) => {
                self.open_tariff_id().and_then(|(id, revision)| {
                    let tariff = self.controller.tariff.as_ref()?;
                    let policy = &tariff["access_policy"];
                    let changed = match control {
                        Control::ControllerTariffRegion(index) => {
                            toggle_region(policy, tariff["regions"][index]["id"].as_str()?)
                        }
                        _ => toggle_all(policy),
                    };
                    Some(Command::TariffSave(
                        id,
                        serde_json::json!({"access_policy": changed}),
                        revision,
                    ))
                })
            }
            Control::ControllerTariffLimits => {
                if let Some(tariff) = &self.controller.tariff {
                    let scope = crate::policy_scope::Scope::Tariff {
                        id: tariff["id"].as_str().unwrap_or("").to_owned(),
                        title: tariff["title"].as_str().unwrap_or("").to_owned(),
                    };
                    self.controller.policies = None;
                    self.controller.selected = 0;
                    self.controller.scope = Some(scope.clone());
                    self.controller_command(Command::ScopedPolicies(scope));
                }
                None
            }
            Control::ControllerTariffDelete => self
                .open_tariff_id()
                .map(|(id, revision)| Command::TariffDelete(id, revision)),
            Control::ControllerTariffPick(choice) => {
                let tariffs = state.tariff_assign.as_ref().map(|v| &v["tariffs"]);
                let tariff = choice.and_then(|index| tariffs.map(|t| &t["items"][index]));
                if choice.is_some() && tariff.is_none_or(|t| !t.is_object()) {
                    return true;
                }
                let has_term = tariff.is_some_and(|t| t["duration_days"].is_u64());
                let expires = state
                    .tariff_assign
                    .as_ref()
                    .is_some_and(|v| v["profile"]["expires_at"].is_string());
                self.controller.tariff_draft = Some(TariffDraft {
                    choice,
                    expiry: match (has_term, expires) {
                        (false, _) => "keep",
                        (true, true) => "extend",
                        (true, false) => "start",
                    },
                    apply_access: false,
                    key: uuid::Uuid::new_v4(),
                });
                self.controller.selected = 0;
                None
            }
            Control::ControllerTariffExpiry | Control::ControllerTariffAccess => {
                if let Some(draft) = &mut self.controller.tariff_draft {
                    if control == Control::ControllerTariffExpiry {
                        draft.expiry = next_expiry(draft.expiry);
                    } else {
                        draft.apply_access = !draft.apply_access;
                    }
                    // A changed choice is a different command.
                    draft.key = uuid::Uuid::new_v4();
                }
                None
            }
            Control::ControllerTariffApply => {
                let (Some(open), Some(draft)) = (&state.tariff_assign, &state.tariff_draft) else {
                    return true;
                };
                let (Some(profile), Some(revision)) = (
                    open["profile_id"].as_str(),
                    open["profile"]["desired_revision"].as_u64(),
                ) else {
                    return true;
                };
                Some(Command::TariffAssign(
                    profile.to_owned(),
                    draft.body(&open["tariffs"]),
                    revision,
                    draft.key,
                ))
            }
            Control::ControllerRenewalApprove(_) | Control::ControllerRenewalReject => {
                let renewal = state.renewal.as_ref().filter(|r| r["status"] == "pending");
                let Some(id) = renewal
                    .and_then(|r| r["id"].as_str())
                    .and_then(|id| uuid::Uuid::parse_str(id).ok())
                else {
                    return true;
                };
                match control {
                    Control::ControllerRenewalApprove(days) => {
                        let request = renewal.unwrap();
                        let revision = request["profile_revision"].as_u64();
                        revision
                            .filter(|_| renewal_days(request).contains(&days))
                            .map(|revision| {
                                Command::RenewalApprove(id, days, revision, uuid::Uuid::new_v4())
                            })
                    }
                    _ => Some(Command::RenewalReject(id)),
                }
            }
            _ => return false,
        };
        if let Some(command) = command {
            self.controller_command(command);
        }
        true
    }

    /// Back inside tariff views; false when no tariff view is open.
    pub(in crate::tui) fn tariff_back(&mut self) -> bool {
        let state = &mut self.controller;
        if matches!(state.scope, Some(crate::policy_scope::Scope::Tariff { .. })) {
            // Tariff limits return to their tariff.
            state.scope = None;
            state.policies = None;
            state.selected = 0;
            if let Some((id, _)) = self.open_tariff_id() {
                self.controller_command(Command::Tariff(id));
            }
            return true;
        }
        if state.renewal.take().is_some() {
            self.controller_section = None;
            self.screen = Screen::Access;
            self.controller.selected = 0;
            return true;
        }
        if state.tariff_assign.is_some() {
            if state.tariff_draft.take().is_none() {
                state.tariff_assign = None;
                self.controller_section = None;
                self.screen = Screen::Access;
            }
            state.selected = 0;
            return true;
        }
        if state.tariff.take().is_some() {
            state.selected = 0;
            // The list may show an older name, term or profile count.
            if state.tariffs.is_some() {
                self.controller_command(Command::Tariffs);
            }
            return true;
        }
        if state.tariffs.take().is_some() {
            state.selected = 0;
            return true;
        }
        false
    }

    /// The text and labelled controls of the open tariff view.
    pub(in crate::tui) fn tariff_view(&self) -> (String, Vec<(String, Control)>) {
        let state = &self.controller;
        let back = ("Back".to_string(), Control::ControllerBack);
        if let Some(request) = &state.renewal {
            return renewal_view(request, back);
        }
        if let Some(open) = &state.tariff_assign {
            return self.profile_tariff_view(open, back);
        }
        if let Some(tariff) = &state.tariff {
            let mut text = format!(
                "Tariff: {}\nProfiles: {}",
                tariff["title"].as_str().unwrap_or(""),
                tariff["profiles"]
            );
            if let Some(description) = tariff["description"].as_str() {
                text.push_str(&format!("\n\n{description}"));
            }
            text.push_str(&format!("\n\n{}", limit_lines(tariff).join("\n")));
            text.push_str("\n\nLimits stay linked to the tariff's profiles; the term and nodes are copied when it is assigned.");
            let mut controls = vec![
                (
                    format!("Access term: {}", term_label(&tariff["duration_days"])),
                    Control::ControllerTariffTerm,
                ),
                (
                    format!(
                        "Nodes: {}",
                        access_label(&tariff["access_policy"], &tariff["regions"])
                    ),
                    Control::ControllerTariffNodes,
                ),
            ];
            for (index, region) in tariff["regions"]
                .as_array()
                .map(Vec::as_slice)
                .unwrap_or(&[])
                .iter()
                .enumerate()
                .take(30)
            {
                let chosen = region_chosen(
                    &tariff["access_policy"],
                    region["id"].as_str().unwrap_or(""),
                );
                controls.push((
                    format!(
                        "{} Region: {}",
                        if chosen { "[x]" } else { "[ ]" },
                        region["title"].as_str().unwrap_or("")
                    ),
                    Control::ControllerTariffRegion(index),
                ));
            }
            controls.extend([
                ("Limits".into(), Control::ControllerTariffLimits),
                ("Rename".into(), Control::ControllerTariffRename),
                ("Description".into(), Control::ControllerTariffDescribe),
                ("Delete tariff".into(), Control::ControllerTariffDelete),
                back,
            ]);
            return (text, controls);
        }
        let Some(list) = &state.tariffs else {
            return ("Please wait".into(), vec![back]);
        };
        let items = list["items"].as_array().map(Vec::as_slice).unwrap_or(&[]);
        let text = if items.is_empty() {
            "No tariffs yet.".to_string()
        } else {
            "A tariff is a set of limits with an access term and nodes. Its limits stay linked to its profiles; the term and nodes are copied when you assign it.".to_string()
        };
        let mut controls: Vec<(String, Control)> = items
            .iter()
            .enumerate()
            .map(|(index, item)| {
                (
                    format!(
                        "{} · {} · profiles: {}",
                        item["title"].as_str().unwrap_or(""),
                        term_label(&item["duration_days"]),
                        item["profiles"]
                    ),
                    Control::ControllerTariff(index),
                )
            })
            .collect();
        controls.push(("Create tariff".into(), Control::ControllerTariffNew));
        controls.push(back);
        (text, controls)
    }

    fn profile_tariff_view(
        &self,
        open: &serde_json::Value,
        back: (String, Control),
    ) -> (String, Vec<(String, Control)>) {
        let title = open["title"].as_str().unwrap_or("");
        if !open["tariffs"].is_object() {
            return ("Please wait".into(), vec![back]);
        }
        let current = open["summary"]["tariff"]["title"]
            .as_str()
            .unwrap_or("No tariff");
        let mut text = format!("Profile: {title}\nTariff: {current}");
        if open["applied"] == true {
            text.push_str("\n\nTariff applied.");
        }
        let items = open["tariffs"]["items"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        let Some(draft) = &self.controller.tariff_draft else {
            let mut controls: Vec<(String, Control)> = items
                .iter()
                .enumerate()
                .take(40)
                .map(|(index, item)| {
                    (
                        format!(
                            "{} · {}",
                            item["title"].as_str().unwrap_or(""),
                            term_label(&item["duration_days"])
                        ),
                        Control::ControllerTariffPick(Some(index)),
                    )
                })
                .collect();
            controls.push(("Without tariff".into(), Control::ControllerTariffPick(None)));
            controls.push(back);
            return (text, controls);
        };
        let mut controls = Vec::new();
        match draft.choice.map(|index| &items[index]) {
            None => text.push_str(
                "\n\nRemove the tariff? Its limits stop applying; the term and nodes stay.",
            ),
            Some(tariff) => {
                text.push_str(&format!(
                    "\n\nNew tariff: {}",
                    tariff["title"].as_str().unwrap_or("")
                ));
                if tariff["duration_days"].is_u64() {
                    text.push_str(&format!(
                        "\n{}",
                        expiry_hint(draft.expiry, &tariff["duration_days"])
                    ));
                    controls.push((
                        format!("Term: {}", expiry_label(draft.expiry)),
                        Control::ControllerTariffExpiry,
                    ));
                }
                if tariff["access_policy"].is_object() {
                    controls.push((
                        format!(
                            "Replace nodes: {}",
                            if draft.apply_access { "on" } else { "off" }
                        ),
                        Control::ControllerTariffAccess,
                    ));
                    if draft.apply_access {
                        text.push_str("\nThe profile's nodes are replaced by the tariff's; configs on removed nodes stop working.");
                    }
                }
                text.push_str(
                    "\nThe tariff's limits apply while it is assigned and follow its changes.",
                );
            }
        }
        controls.push(("Apply".into(), Control::ControllerTariffApply));
        controls.push(back);
        (text, controls)
    }
}

/// A renewal request and its decision, as the bot shows it.
fn renewal_view(
    request: &serde_json::Value,
    back: (String, Control),
) -> (String, Vec<(String, Control)>) {
    if !request["status"].is_string() {
        return ("Please wait".into(), vec![back]);
    }
    let day = |value: &serde_json::Value| {
        value
            .as_str()
            .map_or("—".to_string(), |v| v.chars().take(10).collect())
    };
    let mut text = format!(
        "Renewal request · {}\nAccess until: {}\nTariff: {}\nRequested: {}",
        request["display_name"].as_str().unwrap_or(""),
        day(&request["profile_expires_at"]),
        request["tariff"]["title"].as_str().unwrap_or("No tariff"),
        request["created_at"].as_str().map_or(String::new(), |at| at
            .chars()
            .take(16)
            .collect::<String>()
            .replace('T', " "))
    );
    let mut controls = Vec::new();
    match request["status"].as_str() {
        Some("pending") => {
            for days in renewal_days(request) {
                controls.push((
                    format!("Extend by {days} days"),
                    Control::ControllerRenewalApprove(days),
                ));
            }
            controls.push(("Decline".into(), Control::ControllerRenewalReject));
        }
        Some("approved") => text.push_str(&format!(
            "\n\nAccess extended until {}.",
            day(&request["expires_at"])
        )),
        _ => text.push_str("\n\nThe request was declined."),
    }
    controls.push(back);
    (text, controls)
}
