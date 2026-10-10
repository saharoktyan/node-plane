//! Administrator access screens share the controller's account and request APIs.
use serde_json::{Value, json};
use uuid::Uuid;

#[derive(Clone, Copy, Default, PartialEq, Eq, Debug)]
pub enum Tab {
    Accounts,
    #[default]
    Profiles,
    Requests,
}
impl Tab {
    pub const ALL: [Self; 3] = [Self::Accounts, Self::Profiles, Self::Requests];
    pub fn label(self) -> &'static str {
        match self {
            Self::Accounts => "Accounts",
            Self::Profiles => "Profiles",
            Self::Requests => "Requests",
        }
    }
    pub fn root(self) -> &'static str {
        match self {
            Self::Accounts => "/api/v1/accounts",
            Self::Profiles => "/api/v1/profiles",
            Self::Requests => "/api/v1/access-requests",
        }
    }
}
#[derive(Clone)]
pub enum Command {
    EditContext {
        id: Option<String>,
        owner: Option<String>,
    },
    Mutate {
        id: Option<String>,
        revision: Option<u64>,
        body: Value,
        delete: bool,
        key: Uuid,
    },
    AccountMutate {
        id: String,
        revision: u64,
        body: Value,
        key: Uuid,
    },
    ProfileProgress(String),
    Devices(String),
    DeviceMutate {
        profile: String,
        device: Option<String>,
        revision: u64,
        name: String,
        delete: bool,
        key: Uuid,
    },
    List(Tab),
    Card(Tab, String),
    Decision {
        id: String,
        approve: bool,
        key: Uuid,
    },
}
impl Command {
    pub fn request(&self) -> (hyper::Method, String, Option<Value>, Option<Uuid>) {
        match self {
            Self::EditContext { .. } => (
                hyper::Method::GET,
                "/api/v1/nodes?limit=100".into(),
                None,
                None,
            ),
            Self::Mutate {
                id,
                body,
                delete,
                key,
                ..
            } => (
                if *delete {
                    hyper::Method::DELETE
                } else if id.is_some() {
                    hyper::Method::PATCH
                } else {
                    hyper::Method::POST
                },
                id.as_ref()
                    .map(|id| format!("/api/v1/profiles/{id}"))
                    .unwrap_or("/api/v1/profiles".into()),
                (!delete).then_some(body.clone()),
                Some(*key),
            ),
            Self::AccountMutate { id, body, key, .. } => (
                hyper::Method::PATCH,
                format!("/api/v1/accounts/{id}"),
                Some(body.clone()),
                Some(*key),
            ),
            Self::ProfileProgress(profile) => (
                hyper::Method::GET,
                format!("/api/v1/profiles/{profile}"),
                None,
                None,
            ),
            Self::Devices(profile) => (
                hyper::Method::GET,
                format!("/api/v1/profiles/{profile}/devices"),
                None,
                None,
            ),
            Self::DeviceMutate {
                profile,
                device,
                name,
                delete,
                key,
                ..
            } => (
                if *delete {
                    hyper::Method::DELETE
                } else if device.is_some() {
                    hyper::Method::PATCH
                } else {
                    hyper::Method::POST
                },
                format!(
                    "/api/v1/profiles/{profile}/devices{}",
                    device
                        .as_ref()
                        .map(|id| format!("/{id}"))
                        .unwrap_or_default()
                ),
                (!delete).then(|| json!({"display_name":name.trim()})),
                Some(*key),
            ),
            Self::List(tab) => (
                hyper::Method::GET,
                format!("{}?limit=100", tab.root()),
                None,
                None,
            ),
            Self::Card(tab, id) => (
                hyper::Method::GET,
                format!("{}/{id}", tab.root()),
                None,
                None,
            ),
            Self::Decision { id, approve, key } => (
                hyper::Method::POST,
                format!("/api/v1/access-requests/{id}/decision"),
                Some(json!({"decision": if *approve { "approve" } else { "reject" }})),
                Some(*key),
            ),
        }
    }
}
pub struct Editor {
    pub source: Option<Value>,
    pub owner: Option<String>,
    pub fields: [String; 2],
    pub key: Uuid,
    pub nodes: Vec<Value>,
    pub regions: Vec<Value>,
    pub policy: Value,
    pub grants_view: bool,
    pub grants_page: usize,
    pub future_view: bool,
    pub expiry_view: bool,
    pub expiry_page: usize,
}
impl Editor {
    pub fn new(source: Option<Value>, owner: Option<String>) -> Self {
        let fields = source
            .as_ref()
            .map(|v| {
                [
                    v["display_name"].as_str().unwrap_or("").into(),
                    v["expires_at"].as_str().unwrap_or("").into(),
                ]
            })
            .unwrap_or_default();
        Self {
            source,
            owner,
            fields,
            key: Uuid::new_v4(),
            nodes: Vec::new(),
            regions: Vec::new(),
            policy: json!({"explicit_grants": [], "rules": [], "exclusions": []}),
            grants_view: false,
            grants_page: 0,
            future_view: false,
            expiry_view: false,
            expiry_page: 0,
        }
    }
    pub fn toggle_grant(&mut self, node: &str, protocol: &str) {
        let granted = self.granted(node, protocol);
        for key in ["explicit_grants", "exclusions"] {
            self.policy[key]
                .as_array_mut()
                .unwrap()
                .retain(|g| !(g["node_key"] == node && g["protocol"] == protocol));
        }
        let grant = json!({"node_key": node, "protocol": protocol});
        self.policy[if granted {
            "exclusions"
        } else {
            "explicit_grants"
        }]
        .as_array_mut()
        .unwrap()
        .push(grant);
        self.key = Uuid::new_v4();
    }
    pub fn granted(&self, node: &str, protocol: &str) -> bool {
        let has = |key: &str| {
            self.policy[key]
                .as_array()
                .unwrap()
                .iter()
                .any(|g| g["node_key"] == node && g["protocol"] == protocol)
        };
        if has("exclusions") {
            return false;
        }
        has("explicit_grants")
            || self.policy["rules"].as_array().unwrap().iter().any(|r| {
                r["protocols"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|p| p == protocol)
                    && (r["scope"] == "all"
                        || self
                            .regions
                            .iter()
                            .find(|v| v["id"] == r["region_id"])
                            .is_some_and(|region| {
                                self.nodes
                                    .iter()
                                    .find(|v| v["key"] == node)
                                    .is_some_and(|n| {
                                        if !n["region_id"].is_null() {
                                            n["region_id"] == region["id"]
                                        } else {
                                            text(&n["region"])
                                                .split_whitespace()
                                                .collect::<Vec<_>>()
                                                .join(" ")
                                                .to_lowercase()
                                                == text(&region["title"])
                                                    .split_whitespace()
                                                    .collect::<Vec<_>>()
                                                    .join(" ")
                                                    .to_lowercase()
                                        }
                                    })
                            }))
            })
    }
    pub fn rule(&self, region: Option<&str>, protocol: &str) -> bool {
        self.policy["rules"].as_array().unwrap().iter().any(|r| {
            r["region_id"].as_str() == region
                && r["protocols"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|p| p == protocol)
        })
    }
    pub fn toggle_rule(&mut self, region: Option<&str>, protocol: &str) {
        let rules = self.policy["rules"].as_array_mut().unwrap();
        if let Some(index) = rules.iter().position(|r| r["region_id"].as_str() == region) {
            let protocols = rules[index]["protocols"].as_array_mut().unwrap();
            if let Some(index) = protocols.iter().position(|p| p == protocol) {
                protocols.remove(index);
            } else {
                protocols.push(json!(protocol));
            }
            if protocols.is_empty() {
                rules.remove(index);
            }
        } else {
            rules.push(
                json!({"scope": if region.is_some() { "region" } else { "all" },
            "region_id": region, "protocols": [protocol]}),
            );
        }
        self.key = Uuid::new_v4();
    }
    pub fn save(&self) -> anyhow::Result<Command> {
        anyhow::ensure!(!self.fields[0].trim().is_empty(), "Enter a profile name");
        let mut body = json!({"display_name": self.fields[0].trim(),
            "expires_at": if self.fields[1].trim().is_empty() { Value::Null } else { json!(self.fields[1].trim()) }});
        body["access_policy"] = self.policy.clone();
        if self.source.is_none() {
            body["owner_account_id"] = self
                .owner
                .as_ref()
                .map(|id| json!(id))
                .unwrap_or(Value::Null);
        }
        Ok(Command::Mutate {
            id: self
                .source
                .as_ref()
                .and_then(|v| v["id"].as_str())
                .map(str::to_owned),
            revision: self
                .source
                .as_ref()
                .and_then(|v| v["desired_revision"].as_u64()),
            body,
            delete: false,
            key: self.key,
        })
    }
}
pub struct DeviceEditor {
    pub source: Option<Value>,
    pub name: String,
    pub key: Uuid,
}
pub struct Devices {
    pub profile: Value,
    pub items: Vec<Value>,
    pub operation: Value,
    pub card: Option<Value>,
    pub editor: Option<DeviceEditor>,
    pub delete_key: Option<Uuid>,
    pub page: usize,
}
impl Devices {
    pub fn edit(&mut self, new: bool) {
        let source = if new { None } else { self.card.clone() };
        self.editor = Some(DeviceEditor {
            name: source
                .as_ref()
                .and_then(|v| v["display_name"].as_str())
                .unwrap_or("")
                .into(),
            source,
            key: Uuid::new_v4(),
        });
    }
    pub fn save(&self) -> anyhow::Result<Command> {
        let editor = self
            .editor
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("No device draft"))?;
        anyhow::ensure!(
            !editor.name.trim().is_empty() && editor.name.chars().count() <= 64,
            "Enter a device name (up to 64 characters)"
        );
        let revision = editor
            .source
            .as_ref()
            .map_or(&self.profile["desired_revision"], |v| &v["revision"])
            .as_u64()
            .ok_or_else(|| anyhow::anyhow!("Refresh the device list"))?;
        Ok(Command::DeviceMutate {
            profile: text(&self.profile["id"]),
            device: editor
                .source
                .as_ref()
                .and_then(|v| v["id"].as_str())
                .map(str::to_owned),
            revision,
            name: editor.name.clone(),
            delete: false,
            key: editor.key,
        })
    }
}

// UTC Gregorian date from Unix seconds; presets do not require an OS date command.
pub fn expiry_after(days: u32, now: std::time::SystemTime) -> anyhow::Result<String> {
    let seconds = now
        .duration_since(std::time::UNIX_EPOCH)?
        .as_secs()
        .checked_add(u64::from(days) * 86400)
        .ok_or_else(|| anyhow::anyhow!("Date overflow"))?;
    let day = i64::try_from(seconds / 86400)?;
    let z = day + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let date = doy - (153 * mp + 2) / 5 + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    let year = year + i64::from(month <= 2);
    anyhow::ensure!((1..=9999).contains(&year), "Date is out of range");
    let time = seconds % 86400;
    Ok(format!(
        "{year:04}-{month:02}-{date:02}T{:02}:{:02}:{:02}Z",
        time / 3600,
        time / 60 % 60,
        time % 60
    ))
}

#[derive(Default)]
pub struct Browser {
    pub profile: Option<Uuid>,
    pub tab: Tab,
    pub items: Vec<Value>,
    pub card: Option<Value>,
    pub selected: usize,
    pub page: usize,
    pub search: String,
    pub decision: Option<bool>,
    pub notice: String,
    pub owner_filter: Option<String>,
    pub editor: Option<Editor>,
    pub devices: Option<Devices>,
    pub account_action: Option<(Value, Uuid)>,
    pub profile_action: Option<bool>, // true: delete, false: freeze/unfreeze
}
impl Browser {
    pub fn filtered(&self) -> Vec<usize> {
        let search = self.search.to_lowercase();
        self.items
            .iter()
            .enumerate()
            .filter(|(_, item)| {
                self.owner_filter
                    .as_ref()
                    .is_none_or(|owner| item["owner_account_id"] == owner.as_str())
                    && (search.is_empty()
                        || [
                            "display_name",
                            "id",
                            "telegram_user_id",
                            "username",
                            "first_name",
                            "last_name",
                        ]
                        .iter()
                        .any(|key| item[key].to_string().to_lowercase().contains(&search)))
            })
            .map(|(index, _)| index)
            .collect()
    }
    pub fn poll(&self) -> Option<Command> {
        let card = self.card.as_ref()?;
        (self.tab == Tab::Profiles
            && card["deleting"] == true
            && self.devices.is_none()
            && self.editor.is_none()
            && self.profile_action.is_none()
            && !matches!(
                card["operation"]["status"].as_str(),
                Some("succeeded" | "no_targets" | "superseded")
            ))
        .then(|| Command::ProfileProgress(text(&card["id"])))
    }
    pub fn apply(&mut self, command: Command, value: Value) {
        if let Command::ProfileProgress(id) = &command {
            if self.tab == Tab::Profiles
                && self.card.as_ref().is_some_and(|v| v["id"] == id.as_str())
            {
                self.card = Some(value);
            }
            return;
        }
        self.selected = 0;
        match command {
            Command::ProfileProgress(_) => unreachable!(),
            Command::EditContext { owner, .. } => {
                let source = (!value["profile"].is_null()).then(|| value["profile"].clone());
                let mut editor = Editor::new(source, owner);
                editor.nodes = value["nodes"].as_array().cloned().unwrap_or_default();
                editor.regions = value["regions"].as_array().cloned().unwrap_or_default();
                if !value["policy"].is_null() {
                    for key in ["explicit_grants", "rules", "exclusions"] {
                        editor.policy[key] = value["policy"][key].clone();
                    }
                }
                self.editor = Some(editor);
            }
            Command::Mutate { delete, .. } => {
                self.tab = Tab::Profiles;
                self.card = Some(value["profile"].clone());
                self.editor = None;
                self.profile_action = None;
                self.notice = if delete {
                    "Profile removal scheduled."
                } else {
                    "Profile saved."
                }
                .into();
            }
            Command::Devices(_) | Command::DeviceMutate { .. } => {
                self.devices = Some(Devices {
                    profile: value["profile"].clone(),
                    items: value["devices"]["items"]
                        .as_array()
                        .cloned()
                        .unwrap_or_default(),
                    operation: value["operation"].clone(),
                    card: None,
                    editor: None,
                    delete_key: None,
                    page: 0,
                });
                if !value["result"].is_null() {
                    self.notice = if value["result"]["runtime_status"] == "metadata_only" {
                        "Device saved.".into()
                    } else {
                        format!(
                            "Device change: {}",
                            text(&value["result"]["runtime_status"])
                        )
                    };
                }
            }
            Command::AccountMutate { .. } => {
                self.tab = Tab::Accounts;
                self.card = Some(value);
                self.account_action = None;
                self.notice = "Account saved.".into();
            }
            Command::List(tab) => {
                self.tab = tab;
                self.items = value["items"].as_array().cloned().unwrap_or_default();
                self.card = None;
            }
            Command::Card(tab, _) => {
                self.tab = tab;
                self.card = Some(value);
            }
            Command::Decision { approve, .. } => {
                self.card = Some(value);
                self.decision = None;
                self.notice = if approve {
                    "Access approved."
                } else {
                    "Request rejected."
                }
                .into();
            }
        }
    }
}
pub fn text(value: &Value) -> String {
    value.as_str().map(str::to_owned).unwrap_or_else(|| {
        if value.is_null() {
            "—".into()
        } else {
            value.to_string()
        }
    })
}
pub fn label(tab: Tab, item: &Value) -> String {
    match tab {
        Tab::Profiles => format!(
            "{} · {}",
            text(&item["display_name"]),
            if item["deleting"] == true {
                if matches!(
                    item["operation"]["status"].as_str(),
                    Some("succeeded" | "no_targets")
                ) {
                    "Deleted"
                } else {
                    "Deleting"
                }
            } else if item["frozen"] == true {
                "Frozen"
            } else {
                "Active"
            }
        ),
        _ => format!(
            "{}{} · {}",
            text(&item["telegram_user_id"]),
            item["username"]
                .as_str()
                .map(|name| format!(" · @{name}"))
                .unwrap_or_default(),
            text(
                &item[if tab == Tab::Accounts {
                    "role"
                } else {
                    "status"
                }]
            )
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn device_commands_use_profile_revision_for_creation_and_device_revision_for_edits() {
        let mut devices = Devices {
            profile: json!({"id":"profile", "desired_revision":11}),
            items: vec![],
            operation: Value::Null,
            card: Some(json!({"id":"device", "display_name":"Phone", "revision":3})),
            editor: None,
            delete_key: None,
            page: 0,
        };
        devices.edit(true);
        devices.editor.as_mut().unwrap().name = "Laptop".into();
        let Command::DeviceMutate {
            revision, device, ..
        } = devices.save().unwrap()
        else {
            panic!()
        };
        assert_eq!(revision, 11);
        assert!(device.is_none());
        devices.edit(false);
        devices.editor.as_mut().unwrap().name = "My phone".into();
        let command = devices.save().unwrap();
        let Command::DeviceMutate {
            revision,
            device,
            key,
            ..
        } = &command
        else {
            panic!()
        };
        assert_eq!(*revision, 3);
        assert_eq!(device.as_deref(), Some("device"));
        assert_eq!(*key, devices.editor.as_ref().unwrap().key);
        let (method, path, body, _) = command.request();
        assert_eq!(method, hyper::Method::PATCH);
        assert_eq!(path, "/api/v1/profiles/profile/devices/device");
        assert_eq!(body.unwrap()["display_name"], "My phone");
        devices.editor.as_mut().unwrap().name = " ".into();
        assert!(devices.save().is_err());
    }
    #[test]
    fn expiry_presets_keep_utc_time_and_handle_leap_year_and_year_boundary() {
        use std::time::{Duration, UNIX_EPOCH};
        assert_eq!(
            expiry_after(1, UNIX_EPOCH + Duration::from_secs(1709168523)).unwrap(),
            "2024-03-01T01:02:03Z"
        );
        assert_eq!(
            expiry_after(1, UNIX_EPOCH + Duration::from_secs(1735606923)).unwrap(),
            "2025-01-01T01:02:03Z"
        );
        assert_eq!(expiry_after(7, UNIX_EPOCH).unwrap(), "1970-01-08T00:00:00Z");
    }
    #[test]
    fn deletion_observation_preserves_focus_stops_on_success_and_ignores_old_card() {
        let mut browser = Browser {
            tab: Tab::Profiles,
            selected: 4,
            card: Some(json!({"id":"p1", "deleting":true})),
            ..Default::default()
        };
        assert!(matches!(browser.poll(), Some(Command::ProfileProgress(_))));
        browser.apply(
            Command::ProfileProgress("p1".into()),
            json!({"id":"p1", "deleting":true, "operation":{"status":"blocked"}}),
        );
        assert_eq!(browser.selected, 4);
        assert!(browser.poll().is_some());
        browser.apply(
            Command::ProfileProgress("p1".into()),
            json!({"id":"p1", "deleting":true, "operation":{"status":"succeeded"}}),
        );
        assert!(browser.poll().is_none());
        browser.card = Some(json!({"id":"p2"}));
        browser.apply(Command::ProfileProgress("p1".into()), json!({"id":"p1"}));
        assert_eq!(browser.card.unwrap()["id"], "p2");
    }

    #[test]
    fn account_update_applies_returned_revision_and_clears_confirmation() {
        let key = Uuid::new_v4();
        let command = Command::AccountMutate {
            id: "account".into(),
            revision: 7,
            body: json!({"role":"admin"}),
            key,
        };
        let (method, path, body, request_key) = command.request();
        assert_eq!(method, hyper::Method::PATCH);
        assert_eq!(path, "/api/v1/accounts/account");
        assert_eq!(body.unwrap()["role"], "admin");
        assert_eq!(request_key, Some(key));
        let mut browser = Browser {
            account_action: Some((json!({"role":"admin"}), key)),
            ..Browser::default()
        };
        browser.apply(
            command,
            json!({"id":"account", "role":"admin", "revision":8}),
        );
        assert!(browser.account_action.is_none());
        assert_eq!(browser.card.unwrap()["revision"], 8);
    }

    #[test]
    fn inherited_access_can_be_excluded_and_reenabled_without_removing_rules() {
        let mut editor = Editor::new(None, None);
        editor.nodes = vec![json!({"key":"lv1","region":"Europe","protocols":["awg","xray"]})];
        editor.regions = vec![json!({"id":"eu","title":"Europe"})];
        editor.toggle_rule(Some("eu"), "awg");
        assert!(editor.granted("lv1", "awg"));
        editor.toggle_grant("lv1", "awg");
        assert!(!editor.granted("lv1", "awg"));
        assert!(editor.rule(Some("eu"), "awg"));
        editor.toggle_grant("lv1", "awg");
        assert!(editor.granted("lv1", "awg"));
        assert!(editor.policy["exclusions"].as_array().unwrap().is_empty());
    }
    #[test]
    fn regional_access_uses_identity_instead_of_displayed_region_name() {
        let mut editor = Editor::new(None, None);
        editor.nodes = vec![json!({"key":"lv1", "region":"New name", "region_id":"eu"})];
        editor.regions = vec![json!({"id":"eu", "title":"Original name"})];
        editor.toggle_rule(Some("eu"), "awg");
        assert!(editor.granted("lv1", "awg"));
    }
    #[test]
    fn edits_keep_identity_revision_and_policy_and_clear_unlimited_expiry() {
        let mut editor = Editor::new(
            Some(json!({"id":"profile","desired_revision":9,
            "display_name":"Old","expires_at":"2026-11-01T00:00:00Z"})),
            None,
        );
        editor.fields = ["New".into(), "".into()];
        let Command::Mutate {
            id,
            revision,
            body,
            key,
            ..
        } = editor.save().unwrap()
        else {
            panic!()
        };
        assert_eq!(id.as_deref(), Some("profile"));
        assert_eq!(revision, Some(9));
        assert_eq!(key, editor.key);
        assert_eq!(body["display_name"], "New");
        assert!(body["expires_at"].is_null());
        assert!(body.get("access_policy").is_some());
    }
}
