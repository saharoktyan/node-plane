use anyhow::{Context, Result, ensure};
use hyper::Method;
use serde_json::{Value, json};
use uuid::Uuid;

pub fn recovery_inventory_path(offset: usize, node_key: Option<&str>) -> String {
    match node_key {
        None => format!("/api/v1/system/recovery?group_by_node=true&offset={offset}"),
        Some(key) => {
            let key: String = key
                .bytes()
                .map(|b| {
                    if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) {
                        char::from(b).to_string()
                    } else {
                        format!("%{b:02X}")
                    }
                })
                .collect();
            format!("/api/v1/system/recovery?offset={offset}&node_key={key}")
        }
    }
}

#[derive(Clone)]
pub enum Command {
    Probe,
    Presence,
    Overview,
    Onboarding,
    OnboardingDefaults,
    OnboardingComplete,
    OnboardingDismiss,
    Updates,
    Defaults,
    Policies,
    Alerts,
    WorkstationAudit(usize),
    WorkstationAuditErrors(usize),
    RecoveryHistory(usize),
    RecoveryInventory(usize),
    RecoveryNode(String, usize),
    RecoveryAction(String, String, String, usize, Option<String>),
    AnnouncementOverview,
    /// The texts by language (`localized`) and how many each would reach.
    /// The texts and the title (null for none).
    AnnouncementPreview(Value, Value),
    AnnouncementCreate(Value, Value, Uuid, u64),
    /// The languages the controller writes texts in.
    Languages,
    AnnouncementStatus(Uuid),
    AlertPreferences(Value),
    AlertDismiss(Uuid),
    BotTitle,
    BotTitleSave(String),
    TelegramSettings,
    TelegramToken(zeroize::Zeroizing<String>),
    /// Turn the bot on with this token, or off with none.
    TelegramSwitch(Option<zeroize::Zeroizing<String>>),
    TelegramAdmin(bool, u64, Uuid),
    SshKey,
    PolicySave(&'static str, Value),
    VpnPolicySave(Value, Uuid),
    /// Policies of one profile, device or node, with what they inherit.
    ScopedPolicies(crate::policy_scope::Scope),
    ScopedPolicySave(crate::policy_scope::Scope, Value, Uuid),
    /// Starts a new quota period for the scope's profile now.
    QuotaReset(crate::policy_scope::Scope, Uuid),
    /// Moves the scope profile's quota start day (YYYY-MM-DD) at a revision.
    QuotaAnchor(crate::policy_scope::Scope, String, u64),
    DefaultsSave(Value, u64),
    /// The tariff catalog, with the regions a tariff may grant.
    Tariffs,
    Tariff(Uuid),
    /// A new tariff named in several languages.
    TariffCreate(Value),
    /// A partial tariff document at the tariff's revision.
    TariffSave(Uuid, Value, u64),
    TariffDelete(Uuid, u64),
    /// Tariffs offered to one profile, with the profile and its summary.
    TariffChoices(String),
    /// Assigns, relinks or removes a profile's tariff at a profile revision.
    TariffAssign(String, Value, u64, Uuid),
    /// A renewal request with its profile's term, revision and tariff.
    Renewal(Uuid),
    /// Extends by days at the profile revision seen in the request.
    RenewalApprove(Uuid, u64, u64, Uuid),
    RenewalReject(Uuid),
    /// The installation's services: kinds, states and instances.
    Services,
    /// The web cabinet's relay, with its link as the driver sees it.
    Relay,
    /// The nodes a new relay can run on.
    RelayNodes,
    /// Sets the relay up or changes it (`PUT /api/v1/relay`).
    RelaySave(Value),
    RelayRemove,
    /// The web cabinet: where it is served from, and whether this release has it.
    Cabinet,
    /// Serves the cabinet or changes where (`PUT /api/v1/web-cabinet`).
    CabinetSave(Value),
    CabinetRemove,
    Backups,
    BackupCatalog(usize),
    BackupDetail(Uuid),
    BackupDelete(Uuid, String),
    BackupExport(Uuid, std::path::PathBuf),
    BackupImport(std::path::PathBuf),
    BackupPreferences(Value),
    BackupQueue(Value, Uuid),
    BackupJob(Uuid),
    UpdateVersions(usize),
    UpdateResult(Uuid),
    UpdateDismiss(Uuid),
    UpdateCheck,
    UpdatePreferences(Value),
    Plan {
        action: String,
        cleanup_nodes: bool,
    },
    Queue {
        plan: Uuid,
        phrase: String,
        key: Uuid,
    },
    Job(Uuid),
    Retry(Uuid, Uuid),
    Shutdown(Uuid, Uuid),
    Abort(Uuid),
    Verify(Value),
}
impl Command {
    pub fn read_only(&self) -> bool {
        matches!(self, Self::Probe | Self::Presence | Self::TelegramSettings)
            || self.request().is_ok_and(|r| r.0 == Method::GET)
    }
    pub fn request(&self) -> Result<(Method, String, Option<Value>, Option<Uuid>)> {
        let root = "/api/v1/system/cleanup";
        Ok(match self {
            Self::Onboarding => (Method::GET, "/api/v1/system/onboarding".into(), None, None),
            Self::OnboardingDismiss => (
                Method::POST,
                "/api/v1/system/onboarding/dismiss/workstation".into(),
                Some(json!({})),
                None,
            ),
            Self::OnboardingComplete => (
                Method::POST,
                "/api/v1/system/onboarding/complete/workstation".into(),
                Some(serde_json::json!({})),
                None,
            ),
            Self::OnboardingDefaults => (
                Method::POST,
                "/api/v1/system/onboarding/defaults".into(),
                Some(json!({})),
                None,
            ),
            Self::Probe
            | Self::Presence
            | Self::TelegramSettings
            | Self::TelegramToken(_)
            | Self::TelegramSwitch(_)
            | Self::TelegramAdmin(..) => {
                anyhow::bail!("Installation detection uses the SSH connection")
            }
            Self::UpdateResult(id) => (
                Method::GET,
                format!("/api/v1/system/updates/jobs/{id}"),
                None,
                None,
            ),
            Self::UpdateDismiss(id) => (
                Method::POST,
                format!("/api/v1/system/updates/jobs/{id}/dismiss"),
                Some(json!({})),
                None,
            ),
            Self::UpdateVersions(offset) => (
                Method::GET,
                format!("/api/v1/system/updates/versions?offset={offset}"),
                None,
                None,
            ),
            Self::Backups => (Method::GET, "/api/v1/system/backups".into(), None, None),
            Self::Services => (Method::GET, "/api/v1/services".into(), None, None),
            Self::Relay => (Method::GET, "/api/v1/relay".into(), None, None),
            Self::RelayNodes => (
                Method::GET,
                "/api/v1/nodes?stage=node&limit=100".into(),
                None,
                None,
            ),
            Self::RelaySave(body) => (
                Method::PUT,
                "/api/v1/relay".into(),
                Some(body.clone()),
                None,
            ),
            Self::RelayRemove => (Method::DELETE, "/api/v1/relay".into(), None, None),
            Self::Cabinet => (Method::GET, "/api/v1/web-cabinet".into(), None, None),
            Self::CabinetSave(body) => (
                Method::PUT,
                "/api/v1/web-cabinet".into(),
                Some(body.clone()),
                None,
            ),
            Self::CabinetRemove => (Method::DELETE, "/api/v1/web-cabinet".into(), None, None),
            Self::Tariffs | Self::TariffChoices(_) => {
                (Method::GET, "/api/v1/tariffs".into(), None, None)
            }
            Self::Tariff(id) => (Method::GET, format!("/api/v1/tariffs/{id}"), None, None),
            Self::TariffCreate(titles) => (
                Method::POST,
                "/api/v1/tariffs".into(),
                Some(json!({"titles":titles})),
                None,
            ),
            Self::TariffSave(id, body, _) => (
                Method::PATCH,
                format!("/api/v1/tariffs/{id}"),
                Some(body.clone()),
                None,
            ),
            Self::TariffDelete(id, _) => {
                (Method::DELETE, format!("/api/v1/tariffs/{id}"), None, None)
            }
            Self::Renewal(id) => (
                Method::GET,
                format!("/api/v1/renewal-requests/{id}"),
                None,
                None,
            ),
            Self::RenewalApprove(id, days, _, key) => (
                Method::POST,
                format!("/api/v1/renewal-requests/{id}/approve"),
                Some(json!({"days":days})),
                Some(*key),
            ),
            Self::RenewalReject(id) => (
                Method::POST,
                format!("/api/v1/renewal-requests/{id}/reject"),
                None,
                None,
            ),
            Self::TariffAssign(profile, body, _, key) => (
                Method::PUT,
                format!("/api/v1/profiles/{profile}/tariff"),
                Some(body.clone()),
                Some(*key),
            ),
            Self::BackupCatalog(offset) => (
                Method::GET,
                format!("/api/v1/system/backups/catalog?offset={offset}"),
                None,
                None,
            ),
            Self::BackupDelete(id, checksum) => (
                Method::POST,
                format!("/api/v1/system/backups/catalog/{id}/delete"),
                Some(json!({"checksum":checksum})),
                None,
            ),
            Self::BackupDetail(id) => (
                Method::GET,
                format!("/api/v1/system/backups/catalog/{id}"),
                None,
                None,
            ),
            Self::BackupExport(id, _) => (
                Method::GET,
                format!("/api/v1/system/backups/catalog/{id}/export"),
                None,
                None,
            ),
            Self::BackupImport(_) => (
                Method::POST,
                "/api/v1/system/backups/import".into(),
                None,
                None,
            ),
            Self::BackupPreferences(value) => (
                Method::PATCH,
                "/api/v1/system/backups/preferences".into(),
                Some(value.clone()),
                None,
            ),
            Self::BackupQueue(value, id) => (
                Method::POST,
                "/api/v1/system/backups/jobs".into(),
                Some(value.clone()),
                Some(*id),
            ),
            Self::BackupJob(id) => (
                Method::GET,
                format!("/api/v1/system/backups/jobs/{id}"),
                None,
                None,
            ),
            Self::BotTitle => (Method::GET, "/api/v1/system/bot-title".into(), None, None),
            Self::BotTitleSave(title) => (
                Method::PATCH,
                "/api/v1/system/bot-title".into(),
                Some(json!({"title":title})),
                None,
            ),
            Self::SshKey => (Method::GET, "/api/v1/system/ssh-key".into(), None, None),
            Self::WorkstationAuditErrors(offset) => (
                Method::GET,
                format!("/api/v1/system/workstation-audit?offset={offset}&errors_only=true"),
                None,
                None,
            ),
            Self::WorkstationAudit(offset) => (
                Method::GET,
                format!("/api/v1/system/workstation-audit?offset={offset}"),
                None,
                None,
            ),
            Self::RecoveryInventory(offset) => (
                Method::GET,
                recovery_inventory_path(*offset, None),
                None,
                None,
            ),
            Self::RecoveryNode(key, offset) => (
                Method::GET,
                recovery_inventory_path(*offset, Some(key)),
                None,
                None,
            ),
            Self::RecoveryAction(kind, id, action, _, _) => (
                Method::POST,
                crate::workstation::recovery_path(kind, id, action)?,
                if action == "force" {
                    Some(json!({
                        "confirm_operation_id": id,
                        "acknowledge_unknown_outcome": true,
                    }))
                } else {
                    None
                },
                None,
            ),
            Self::RecoveryHistory(offset) => (
                Method::GET,
                format!("/api/v1/system/recovery/history?offset={offset}"),
                None,
                None,
            ),
            Self::AnnouncementOverview => (Method::GET, "/api/v1/announcements".into(), None, None),
            Self::Languages => (Method::GET, "/api/v1/system/languages".into(), None, None),
            Self::AnnouncementPreview(texts, titles) => (
                Method::POST,
                "/api/v1/announcements/preview".into(),
                Some(announcement_body(texts, titles)),
                None,
            ),
            Self::AnnouncementCreate(texts, titles, key, _) => (
                Method::POST,
                "/api/v1/announcements".into(),
                Some(announcement_body(texts, titles)),
                Some(*key),
            ),
            Self::AnnouncementStatus(id) => (
                Method::GET,
                format!("/api/v1/announcements/{id}"),
                None,
                None,
            ),
            Self::Alerts => (Method::GET, "/api/v1/system/alerts".into(), None, None),
            Self::AlertPreferences(value) => (
                Method::PATCH,
                "/api/v1/system/alerts/preferences".into(),
                Some(value.clone()),
                None,
            ),
            Self::AlertDismiss(id) => (
                Method::POST,
                format!("/api/v1/system/alerts/{id}/dismiss"),
                Some(json!({})),
                None,
            ),
            Self::Policies => (Method::GET, "/api/v1/system/traffic".into(), None, None),
            Self::PolicySave(kind, value) => {
                ensure!(
                    matches!(*kind, "traffic" | "access-requests"),
                    "Invalid policy"
                );
                (
                    Method::PATCH,
                    format!(
                        "/api/v1/system/{}",
                        if *kind == "traffic" {
                            "traffic/preferences"
                        } else {
                            "access-requests"
                        }
                    ),
                    Some(value.clone()),
                    None,
                )
            }
            Self::VpnPolicySave(value, key) => (
                Method::PATCH,
                "/api/v1/vpn-policies/installation".into(),
                Some(value.clone()),
                Some(*key),
            ),
            Self::ScopedPolicies(scope) => (Method::GET, scope.path(), None, None),
            Self::ScopedPolicySave(scope @ crate::policy_scope::Scope::Tariff { .. }, value, _) => {
                (
                    Method::PATCH,
                    scope.path(),
                    Some(crate::policy_scope::split_revision(value).0),
                    None,
                )
            }
            Self::ScopedPolicySave(scope, value, key) => {
                (Method::PATCH, scope.path(), Some(value.clone()), Some(*key))
            }
            Self::QuotaReset(scope, key) => (
                Method::POST,
                format!(
                    "/api/v1/profiles/{}/quota/reset",
                    scope.profile().context("A quota needs a profile")?
                ),
                Some(json!({})),
                Some(*key),
            ),
            Self::QuotaAnchor(scope, day, _) => (
                Method::PATCH,
                format!(
                    "/api/v1/profiles/{}/quota",
                    scope.profile().context("A quota needs a profile")?
                ),
                Some(json!({"anchor_at":day})),
                None,
            ),
            Self::Defaults => (
                Method::GET,
                "/api/v1/system/installation-defaults".into(),
                None,
                None,
            ),
            Self::DefaultsSave(value, _) => (
                Method::PUT,
                "/api/v1/system/installation-defaults".into(),
                Some(value.clone()),
                None,
            ),
            Self::Updates => (Method::GET, "/api/v1/system/updates".into(), None, None),
            Self::UpdateCheck => (
                Method::POST,
                "/api/v1/system/updates/check".into(),
                Some(json!({})),
                None,
            ),
            Self::UpdatePreferences(value) => (
                Method::PATCH,
                "/api/v1/system/updates/preferences".into(),
                Some(value.clone()),
                None,
            ),
            Self::Overview => (Method::GET, root.into(), None, None),
            Self::Plan {
                action,
                cleanup_nodes,
            } => {
                ensure!(
                    matches!(action.as_str(), "reset" | "remove"),
                    "Invalid cleanup action"
                );
                (
                    Method::POST,
                    format!("{root}/plans"),
                    Some(json!({"action":action,"cleanup_nodes":cleanup_nodes})),
                    Some(Uuid::new_v4()),
                )
            }
            Self::Queue { plan, phrase, key } => (
                Method::POST,
                format!("{root}/jobs"),
                Some(json!({"plan_id":plan,"confirmation_phrase":phrase})),
                Some(*key),
            ),
            Self::Job(id) => (Method::GET, format!("{root}/jobs/{id}"), None, None),
            Self::Retry(id, key) => (
                Method::POST,
                format!("{root}/jobs/{id}/retry"),
                Some(json!({})),
                Some(*key),
            ),
            Self::Shutdown(id, key) => (
                Method::POST,
                format!("{root}/jobs/{id}/shutdown-ack"),
                None,
                Some(*key),
            ),
            Self::Abort(id) => (Method::POST, format!("{root}/jobs/{id}/abort"), None, None),
            Self::Verify(_) => anyhow::bail!("Host verification does not use the backend"),
        })
    }
}
/// The tariff chosen for a profile and what assigning it does.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TariffDraft {
    /// Index into the offered tariffs; None removes the tariff.
    pub choice: Option<usize>,
    pub expiry: &'static str,
    pub apply_access: bool,
    pub key: Uuid,
}

impl TariffDraft {
    /// The assignment body for the offered tariffs.
    pub fn body(&self, tariffs: &Value) -> Value {
        let tariff = self.choice.map(|index| &tariffs["items"][index]);
        json!({"tariff_id": tariff.map(|t| t["id"].clone()),
               "expiry": if tariff.is_some() { self.expiry } else { "keep" },
               "apply_access": tariff.is_some() && self.apply_access})
    }
}

/// The If-Match revision of a tariff command.
pub fn tariff_revision(command: &Command) -> Option<u64> {
    match command {
        Command::TariffSave(_, _, revision)
        | Command::TariffDelete(_, revision)
        | Command::TariffAssign(_, _, revision, _)
        | Command::RenewalApprove(_, _, revision, _) => Some(*revision),
        _ => None,
    }
}

#[derive(Default)]
pub struct Browser {
    pub plan_pending: bool,
    pub profile: Option<Uuid>,
    pub overview: Option<Value>,
    pub onboarding: Option<Value>,
    pub updates: Option<Value>,
    pub branch_confirmation: bool,
    pub defaults: Option<Value>,
    pub policies: Option<Value>,
    /// Set while Policies shows one profile, device or node.
    pub scope: Option<crate::policy_scope::Scope>,
    pub alerts: Option<Value>,
    pub workstation_audit: Option<Value>,
    pub recovery_history: Option<Value>,
    pub recovery_inventory: Option<Value>,
    pub recovery_group_offset: usize,
    pub history_detail: Option<(&'static str, usize)>,
    pub announcement_overview: Option<Value>,
    /// The announcement's texts as last previewed.
    pub announcement_texts: Option<Value>,
    /// Its title, or null without one; None until the title step is done.
    pub announcement_titles: Option<Value>,
    pub announcement_preview: Option<Value>,
    pub announcement_job: Option<Value>,
    pub announcement_key: Option<Uuid>,
    pub alerts_page: usize,
    pub alerts_unread: u64,
    pub bot_title: Option<Value>,
    pub telegram_result: Option<String>,
    pub telegram_role_pending: Option<bool>,
    pub telegram_settings: Option<Value>,
    pub ssh_key: Option<Value>,
    /// The relay page: `{"relay": …}` as the controller reports it.
    pub relay: Option<Value>,
    /// Settings › Services: `{"items": […]}` from the catalog.
    pub services: Option<Value>,
    /// Nodes a new relay can run on, while one is being set up.
    pub relay_nodes: Option<Value>,
    pub relay_draft: Option<crate::relay::Draft>,
    /// The node card the relay page was opened from; None for Settings.
    pub relay_origin: Option<(String, String)>,
    /// Removal waits for a second press.
    pub relay_confirm_remove: bool,
    /// The web cabinet page: `{"cabinet": …, "available": …, "relay_node_key": …}`.
    pub cabinet: Option<Value>,
    /// The controller's own server being set up or changed.
    pub cabinet_draft: Option<crate::cabinet::Draft>,
    pub cabinet_confirm_remove: bool,
    pub notice: String,
    pub defaults_advanced: bool,
    pub text_confirmation: bool,
    pub defaults_input: Option<(&'static str, String)>,
    /// A text in several languages being written.
    pub ltext: Option<crate::localized::Editor>,
    pub defaults_baseline: Option<Value>,
    pub backups: Option<Value>,
    pub backup_create_key: Option<Uuid>,
    pub backup_catalog: Option<Value>,
    pub backup_detail: Option<Value>,
    pub backup_job: Option<Value>,
    /// The tariff catalog and the open tariff, each with `regions`.
    pub tariffs: Option<Value>,
    pub tariff: Option<Value>,
    /// A profile's tariff being chosen: `tariffs`, `profile`, `summary`.
    pub tariff_assign: Option<Value>,
    pub tariff_draft: Option<TariffDraft>,
    /// The renewal request being decided, opened from a profile card.
    pub renewal: Option<Value>,
    pub versions: Option<Value>,
    pub components: Option<Value>,
    pub update_result: Option<Value>,
    pub components_page: usize,
    pub plan: Option<Value>,
    pub job: Option<Value>,
    pub phrase: String,
    pub key: Option<Uuid>,
    pub cleanup_nodes: bool,
    pub selected: usize,
    pub verifying: bool,
    pub removed: bool,
    pub verification_started: Option<std::time::Instant>,
}
impl Browser {
    pub fn removal_active(&self) -> bool {
        !self.removed
            && (self.verifying
                || self.job.as_ref().is_some_and(|job| {
                    job["action"] == "remove"
                        && matches!(
                            job["status"].as_str(),
                            Some("queued" | "running" | "blocked" | "awaiting_shutdown")
                        )
                }))
    }

    pub fn removal_ready(&self) -> bool {
        !self.removed
            && !self.verifying
            && self.job.as_ref().is_some_and(|job| {
                job["status"] == "awaiting_shutdown"
                    && job["cancel_requested"] != true
                    && job["shutdown_ack"] != true
            })
    }

    pub fn apply(&mut self, command: Command, value: Value) {
        let selected = self.selected;
        let keep_focus = matches!(
            &command,
            Command::AlertPreferences(_)
                | Command::AlertDismiss(_)
                | Command::PolicySave(_, _)
                | Command::VpnPolicySave(_, _)
                | Command::ScopedPolicySave(..)
                | Command::QuotaReset(..)
                | Command::QuotaAnchor(..)
                | Command::BackupPreferences(_)
                | Command::UpdateCheck
                | Command::UpdatePreferences(_)
                | Command::AnnouncementStatus(_)
        );
        match command {
            Command::TelegramAdmin(add, _, _) => {
                self.telegram_settings = Some(value);
                self.defaults_input = None;
                self.telegram_role_pending = None;
                self.telegram_result = Some(
                    if add {
                        "Administrator rights granted."
                    } else {
                        "Administrator rights revoked."
                    }
                    .into(),
                );
            }
            Command::TelegramSettings | Command::TelegramToken(_) | Command::TelegramSwitch(_) => {
                self.telegram_settings = Some(value);
                self.defaults_input = None;
            }
            Command::BotTitle | Command::BotTitleSave(_) => {
                self.bot_title = Some(value);
                self.defaults_input = None;
                self.selected = 0;
            }
            Command::Relay | Command::RelaySave(_) | Command::RelayRemove => {
                // A late answer after the relay page was left.
                if self.relay.is_none() {
                    return;
                }
                if !matches!(command, Command::Relay) {
                    self.relay_draft = None;
                    self.relay_nodes = None;
                    self.relay_confirm_remove = false;
                    self.selected = 0;
                }
                self.relay = Some(value);
                self.defaults_input = None;
            }
            Command::Cabinet | Command::CabinetSave(_) | Command::CabinetRemove => {
                // A late answer after the page was left.
                if self.cabinet.is_none() {
                    return;
                }
                if !matches!(command, Command::Cabinet) {
                    self.cabinet_draft = None;
                    self.cabinet_confirm_remove = false;
                    self.selected = 0;
                }
                self.cabinet = Some(value);
                self.defaults_input = None;
            }
            Command::Services => self.services = Some(value),
            Command::RelayNodes => {
                if self.relay_draft.is_some() {
                    self.relay_nodes = Some(value);
                }
            }
            Command::SshKey => {
                self.ssh_key = Some(value);
                self.notice.clear();
                self.selected = 0;
            }
            Command::WorkstationAudit(_) | Command::WorkstationAuditErrors(_) => {
                self.recovery_inventory = None;
                self.workstation_audit = Some(value);
                self.recovery_history = None;
                self.history_detail = None;
                self.selected = 0;
            }
            Command::RecoveryInventory(_)
            | Command::RecoveryNode(..)
            | Command::RecoveryAction(..) => {
                let mut value = value;
                if value.get("recovery_result").is_none()
                    && let Some(result) = self
                        .recovery_inventory
                        .as_ref()
                        .filter(|p| {
                            p["node_key"] == value["node_key"] && p["grouped"] == value["grouped"]
                        })
                        .and_then(|p| p.get("recovery_result"))
                {
                    value["recovery_result"] = result.clone();
                }
                if value["grouped"] == true {
                    self.recovery_group_offset = value["offset"].as_u64().unwrap_or(0) as usize;
                }
                let first_page = self.recovery_inventory.as_ref().is_none_or(|old| {
                    old["node_key"] != value["node_key"] || old["grouped"] != value["grouped"]
                });
                self.recovery_inventory = Some(value);
                self.workstation_audit = None;
                self.recovery_history = None;
                if first_page || matches!(command, Command::RecoveryAction(..)) {
                    self.selected = 0;
                }
            }
            Command::RecoveryHistory(_) => {
                self.recovery_inventory = None;
                self.recovery_history = Some(value);
                self.workstation_audit = None;
                self.history_detail = None;
                self.selected = 0;
            }
            Command::AnnouncementOverview => {
                self.announcement_job = value.get("last_job").filter(|v| v.is_object()).cloned();
                self.announcement_overview = Some(value);
                self.announcement_preview = None;
                self.announcement_texts = None;
                self.announcement_key = None;
                self.selected = 0;
            }
            Command::Languages => {
                if let (Some(editor), Some(languages)) =
                    (&mut self.ltext, value["languages"].as_array())
                {
                    let languages: Vec<String> = languages
                        .iter()
                        .filter_map(|name| name.as_str().map(str::to_owned))
                        .collect();
                    if !languages.is_empty() {
                        editor.languages = languages;
                    }
                }
            }
            Command::AnnouncementPreview(..) => {
                self.ltext = None;
                self.announcement_preview = Some(value);
                self.announcement_job = None;
                self.selected = 0;
            }
            Command::AnnouncementCreate(..) | Command::AnnouncementStatus(_) => {
                self.announcement_job = Some(value.clone());
                if let Some(overview) = &mut self.announcement_overview {
                    overview["last_job"] = value;
                }
                self.announcement_preview = None;
                self.selected = 0;
            }
            Command::Alerts | Command::AlertPreferences(_) | Command::AlertDismiss(_) => {
                self.alerts_unread = value["unacknowledged_count"].as_u64().unwrap_or(0);
                let count = value["active"].as_array().map_or(0, Vec::len);
                self.alerts_page = self.alerts_page.min(count.saturating_sub(1) / 5);
                self.alerts = Some(value);
                self.selected = 0;
            }
            Command::ScopedPolicies(scope)
            | Command::ScopedPolicySave(scope, ..)
            | Command::QuotaReset(scope, _)
            | Command::QuotaAnchor(scope, ..) => {
                // A late answer for a scope that is no longer shown.
                if self.scope.as_ref() != Some(&scope) {
                    return;
                }
                self.policies = Some(value);
                self.defaults_input = None;
                if !keep_focus {
                    self.selected = 0;
                }
            }
            Command::Policies | Command::PolicySave(_, _) | Command::VpnPolicySave(_, _) => {
                if self.scope.is_some() {
                    return;
                }
                self.policies = Some(value);
                self.defaults_input = None;
                self.ltext = None;
                self.selected = 0;
            }
            Command::Defaults | Command::DefaultsSave(_, _) => {
                self.defaults_baseline = Some(value.clone());
                self.defaults = Some(value);
                self.selected = 0;
            }
            Command::Onboarding
            | Command::OnboardingDefaults
            | Command::OnboardingComplete
            | Command::OnboardingDismiss => {
                self.onboarding = Some(value);
            }
            Command::Backups | Command::BackupPreferences(_) => {
                self.backup_job = value.get("last_job").filter(|v| v.is_object()).cloned();
                self.backups = Some(value);
                self.selected = 0;
            }
            Command::BackupCatalog(_) => {
                self.backup_catalog = Some(value);
                self.selected = 0;
            }
            Command::BackupDelete(_, _) => {
                self.backup_detail = None;
                self.backup_catalog = None;
                self.backups = Some(value);
                self.selected = 0;
            }
            Command::BackupDetail(_) | Command::BackupImport(_) | Command::BackupExport(_, _) => {
                self.backup_detail = Some(value);
                self.defaults_input = None;
                self.selected = 0;
            }
            Command::BackupQueue(_, _) => {
                self.defaults_input = None;
                self.backup_create_key = None;
                self.backup_job = Some(value);
                self.backup_detail = None;
                self.backup_catalog = None;
                self.selected = 0;
            }
            Command::BackupJob(_) => {
                self.backup_job = Some(value);
            }
            Command::Tariffs | Command::TariffDelete(..) => {
                self.tariffs = Some(value);
                self.tariff = None;
                self.defaults_input = None;
                self.selected = 0;
            }
            Command::Tariff(_) | Command::TariffCreate(_) | Command::TariffSave(..) => {
                // A late answer after the tariff screens were left.
                if self.tariffs.is_none() && self.tariff.is_none() {
                    return;
                }
                self.tariff = Some(value);
                self.defaults_input = None;
                self.ltext = None;
                if !matches!(&command, Command::TariffSave(..)) {
                    self.selected = 0;
                }
            }
            Command::Renewal(_) | Command::RenewalApprove(..) | Command::RenewalReject(_) => {
                // A late answer after the request was closed.
                if self.renewal.is_none() {
                    return;
                }
                self.renewal = Some(value);
                self.selected = 0;
            }
            Command::TariffChoices(ref profile) | Command::TariffAssign(ref profile, ..) => {
                // A late answer for a profile that is no longer shown.
                if self
                    .tariff_assign
                    .as_ref()
                    .is_none_or(|open| open["profile_id"] != profile.as_str())
                {
                    return;
                }
                if matches!(&command, Command::TariffAssign(..)) {
                    self.tariff_draft = None;
                }
                self.tariff_assign = Some(value);
                self.selected = 0;
            }
            Command::UpdateResult(_) => {
                self.update_result = Some(value);
            }
            Command::UpdateDismiss(id) => {
                self.update_result = None;
                self.selected = 0;
                if let Some(updates) = &mut self.updates {
                    let list = updates["dismissed_job_ids"]
                        .as_array()
                        .cloned()
                        .unwrap_or_default();
                    let mut list = list;
                    list.push(json!(id));
                    updates["dismissed_job_ids"] = json!(list);
                }
            }
            Command::UpdateVersions(_) => {
                self.versions = Some(value);
                self.selected = 0;
            }
            Command::Updates | Command::UpdateCheck | Command::UpdatePreferences(_) => {
                self.updates = Some(value);
                self.branch_confirmation = false;
                self.selected = 0;
            }
            Command::Probe | Command::Presence => {}
            Command::Overview => {
                self.job = value.get("latest_job").filter(|v| !v.is_null()).cloned();
                self.overview = Some(value);
            }
            Command::Plan { .. } => {
                self.plan_pending = false;
                self.plan = Some(value);
                self.phrase.clear();
                self.key = Some(Uuid::new_v4());
                self.selected = 0;
            }
            Command::Verify(_) => {
                self.removed = value["removed"] == true;
                self.verifying = !self.removed;
            }
            Command::Shutdown(..) => {
                self.job = Some(value);
                self.verifying = true;
                self.verification_started = Some(std::time::Instant::now());
            }
            Command::Job(_) | Command::Abort(_) => {
                if value["action"] == "remove" && value["shutdown_ack"] == true {
                    self.verifying = true;
                    self.verification_started
                        .get_or_insert_with(std::time::Instant::now);
                }
                self.job = Some(value);
                self.plan = None;
                self.phrase.clear();
            }
            _ => {
                self.job = Some(value);
                self.plan = None;
                self.phrase.clear();
            }
        }
        if keep_focus {
            self.selected = selected;
        }
    }
    pub fn verification_timed_out(&self) -> bool {
        self.verification_started
            .is_some_and(|start| start.elapsed() >= std::time::Duration::from_secs(180))
    }
    pub fn save_verification(
        &self,
        state: &std::path::Path,
        installation: &crate::connections::Installation,
    ) -> Result<()> {
        use std::io::Write;
        let overview = self
            .overview
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("Missing removal manifest"))?;
        verification_script(&overview["deployment"])?;
        let data = json!({"version":1,"host":installation.host,"port":installation.port,"user":installation.user,"overview":overview,"job":self.job});
        let state = crate::state_layout::directory(state, "operations")?;
        let mut temporary = tempfile::NamedTempFile::new_in(&state)?;
        temporary.write_all(&serde_json::to_vec(&data)?)?;
        temporary.as_file().sync_all()?;
        temporary.persist(state.join(format!("controller-removal-{}.json", installation.id)))?;
        Ok(())
    }
    pub fn restore_verification(
        &mut self,
        state: &std::path::Path,
        installation: &crate::connections::Installation,
    ) -> Result<()> {
        if !state.try_exists()? {
            return Ok(());
        }
        let path = crate::state_layout::directory(state, "operations")?
            .join(format!("controller-removal-{}.json", installation.id));
        if !path.try_exists()? {
            return Ok(());
        }
        let data: Value = serde_json::from_slice(&std::fs::read(path)?)?;
        ensure!(
            data["version"] == 1
                && data["host"] == installation.host
                && data["port"] == installation.port
                && data["user"] == installation.user,
            "Saved removal belongs to a different SSH connection"
        );
        verification_script(&data["overview"]["deployment"])?;
        ensure!(data["job"]["action"] == "remove", "Invalid removal record");
        self.profile = Some(installation.id);
        self.overview = Some(data["overview"].clone());
        self.job = Some(data["job"].clone());
        self.verifying = true;
        self.verification_started = Some(std::time::Instant::now());
        Ok(())
    }
    pub fn forget_verification(state: &std::path::Path, id: Uuid) -> Result<()> {
        if !state.try_exists()? {
            return Ok(());
        }
        let path = crate::state_layout::directory(state, "operations")?
            .join(format!("controller-removal-{id}.json"));
        match std::fs::remove_file(path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e.into()),
        }
    }
    pub fn poll(&self) -> Option<Command> {
        if self.removed {
            return None;
        }
        // Cleanup must keep progressing even if an older subsection is still loaded.
        if self.verifying && !self.removed {
            if self.verification_timed_out() {
                return None;
            }
            return self
                .overview
                .as_ref()?
                .get("deployment")
                .cloned()
                .map(Command::Verify);
        }
        if self.removal_active() {
            return self.job.as_ref()?["id"]
                .as_str()
                .and_then(|id| Uuid::parse_str(id).ok())
                .map(Command::Job);
        }
        if let Some(page) = &self.recovery_inventory {
            let offset = page["offset"].as_u64().unwrap_or(0) as usize;
            return Some(if let Some(key) = page["node_key"].as_str() {
                Command::RecoveryNode(key.into(), offset)
            } else {
                Command::RecoveryInventory(offset)
            });
        }
        if let Some(job) = &self.backup_job {
            return if matches!(
                job["status"].as_str(),
                Some("awaiting_executor" | "running")
            ) {
                job["id"]
                    .as_str()
                    .and_then(|id| Uuid::parse_str(id).ok())
                    .map(Command::BackupJob)
            } else {
                None
            };
        }
        if let Some(job) = &self.announcement_job
            && job["status"] == "running"
        {
            return job["id"]
                .as_str()
                .and_then(|id| Uuid::parse_str(id).ok())
                .map(Command::AnnouncementStatus);
        }
        if let Some(result) = &self.update_result {
            return if matches!(
                result["status"].as_str(),
                Some("awaiting_executor" | "running")
            ) {
                result["id"]
                    .as_str()
                    .and_then(|id| Uuid::parse_str(id).ok())
                    .map(Command::UpdateResult)
            } else {
                None
            };
        }
        let job = self.job.as_ref()?;
        if matches!(job["status"].as_str(), Some("queued" | "running")) {
            Some(Command::Job(Uuid::parse_str(job["id"].as_str()?).ok()?))
        } else {
            None
        }
    }
}

pub fn verification_script(deployment: &Value) -> Result<String> {
    ensure!(
        deployment["base_dir"].as_str().is_some()
            && deployment["shared_dir"].as_str().is_some()
            && deployment["units"].is_array(),
        "Missing installation verification manifest"
    );
    // Data is a JSON literal inside Python; no path is interpolated into shell code.
    let data = serde_json::to_string(&serde_json::to_string(deployment)?)?;
    Ok(format!(
        r#"import json, pathlib, subprocess, sys
config = json.loads({data})
paths = [config['base_dir'], config['shared_dir'], '/usr/local/bin/node-plane-driver']
removed = not any(pathlib.Path(p).exists() or pathlib.Path(p).is_symlink() for p in paths)
for unit in config['units']:
    path = pathlib.Path('/etc/systemd/system') / unit
    removed = removed and not (path.exists() or path.is_symlink())
    r = subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True, timeout=10)
    removed = removed and r.returncode in (3, 4) and r.stdout.strip() in ('inactive', 'unknown', 'failed')
if config.get('postgres_container'):
    r = subprocess.run(['docker', 'ps', '-a', '--format', '{{{{.Names}}}}'], capture_output=True, text=True, timeout=15)
    removed = removed and r.returncode == 0 and config['postgres_container'] not in r.stdout.splitlines()
print(json.dumps({{'removed': removed}}))
"#
    ))
}

pub const PROBE_SCRIPT: &str = r#"import json, pathlib, urllib.request
base = pathlib.Path('/opt/node-plane')
marker = base / '.node-plane-installation.json'
manifest = False
if marker.is_file() and not marker.is_symlink():
    record = json.loads(marker.read_text())
    manifest = record.get('format') == 'node-plane-systemd-v1' and record.get('base_dir') == str(base) and (base / 'current/app/backend/http_api.py').is_file()
ready = False
try:
    with urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=3) as response:
        ready = response.status == 200 and json.load(response).get('status') == 'alive'
except (OSError, ValueError):
    pass
print(json.dumps({'installed': manifest or ready, 'backend_ready':ready}))
"#;

/// An announcement as the API takes it: the title only when there is one.
fn announcement_body(texts: &Value, titles: &Value) -> Value {
    if titles.is_null() {
        json!({"texts": texts})
    } else {
        json!({"texts": texts, "titles": titles})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cleanup_requests_and_polling_follow_the_removal_lifecycle() {
        let id = Uuid::new_v4();
        let (_, path, body, _) = Command::Shutdown(id, Uuid::new_v4()).request().unwrap();
        assert!(path.ends_with("/shutdown-ack"));
        assert!(
            body.is_none(),
            "Workstation does not supply a Telegram notification"
        );
        assert!(Command::Abort(id).request().unwrap().1.ends_with("/abort"));
        let mut browser = Browser {
            job: Some(json!({"id":id,"action":"remove","status":"running"})),
            backups: Some(json!({})),
            backup_job: Some(json!({"status":"succeeded"})),
            ..Default::default()
        };
        assert!(matches!(browser.poll(), Some(Command::Job(value)) if value == id));
        browser.apply(
            Command::Job(id),
            json!({"id":id,"action":"remove","status":"awaiting_shutdown"}),
        );
        assert!(browser.removal_ready());
        browser.apply(
            Command::Abort(id),
            json!({"id":id,"action":"remove","status":"running","cancel_requested":true}),
        );
        assert!(!browser.removal_ready());
        assert!(browser.poll().is_some());
        browser.apply(
            Command::Job(id),
            json!({"id":id,"action":"remove","status":"aborted"}),
        );
        assert!(!browser.removal_active());
        assert!(browser.poll().is_none());
    }
    #[test]
    fn update_preferences_use_controller_api_and_preserve_cleanup_state() {
        let command = Command::UpdatePreferences(
            json!({"auto_check_enabled":true,"auto_check_interval_minutes":360}),
        );
        let (method, path, body, key) = command.request().unwrap();
        assert_eq!(method, Method::PATCH);
        assert_eq!(path, "/api/v1/system/updates/preferences");
        assert_eq!(body.unwrap()["auto_check_interval_minutes"], 360);
        assert!(key.is_none());
        let mut browser = Browser {
            job: Some(json!({"id":"cleanup","status":"succeeded"})),
            ..Default::default()
        };
        browser.apply(command, json!({"auto_check_enabled":true}));
        assert_eq!(browser.job.unwrap()["id"], "cleanup");
        assert_eq!(browser.updates.unwrap()["auto_check_enabled"], true);
    }

    #[test]
    fn update_result_polling_and_dismiss_keep_cleanup_job() {
        let id = Uuid::new_v4();
        let mut browser = Browser {
            job: Some(json!({"id":"cleanup","status":"succeeded"})),
            updates: Some(json!({"dismissed_job_ids":[]})),
            ..Default::default()
        };
        browser.apply(
            Command::UpdateResult(id),
            json!({"id":id,"status":"running"}),
        );
        assert!(matches!(browser.poll(), Some(Command::UpdateResult(value)) if value == id));
        browser.apply(
            Command::UpdateResult(id),
            json!({"id":id,"status":"succeeded"}),
        );
        assert!(browser.poll().is_none());
        browser.apply(Command::UpdateDismiss(id), json!({"dismissed_job_id":id}));
        assert!(browser.update_result.is_none());
        assert_eq!(
            browser.updates.unwrap()["dismissed_job_ids"][0],
            id.to_string()
        );
        assert_eq!(browser.job.unwrap()["id"], "cleanup");
    }

    #[test]
    fn backup_delete_preserves_selected_copy_and_refreshes_overview() {
        let id = Uuid::new_v4();
        let (method, path, body, _) = Command::BackupDelete(id, "checksum".into())
            .request()
            .unwrap();
        assert_eq!(method, Method::POST);
        assert_eq!(path, format!("/api/v1/system/backups/catalog/{id}/delete"));
        assert_eq!(body.unwrap()["checksum"], "checksum");
        let mut browser = Browser {
            backup_detail: Some(json!({"id":id})),
            backup_catalog: Some(json!({})),
            ..Default::default()
        };
        browser.apply(
            Command::BackupDelete(id, "checksum".into()),
            json!({"count":0}),
        );
        assert!(browser.backup_detail.is_none());
        assert!(browser.backup_catalog.is_none());
        assert_eq!(browser.backups.unwrap()["count"], 0);
    }

    #[test]
    fn backup_restore_keeps_checksum_and_command_identity() {
        let id = Uuid::new_v4();
        let backup = Uuid::new_v4();
        let body = json!({"action":"restore","backup_id":backup,"checksum":"abc"});
        let (method, path, value, key) = Command::BackupQueue(body.clone(), id).request().unwrap();
        assert_eq!(method, Method::POST);
        assert_eq!(path, "/api/v1/system/backups/jobs");
        assert_eq!(value.unwrap(), body);
        assert_eq!(key, Some(id));
        let mut browser = Browser::default();
        browser.apply(
            Command::BackupQueue(body, id),
            json!({"id":backup,"status":"running"}),
        );
        assert!(matches!(browser.poll(),Some(Command::BackupJob(value)) if value == backup));
        browser.apply(
            Command::BackupJob(backup),
            json!({"id":backup,"status":"succeeded"}),
        );
        assert!(browser.poll().is_none());
    }

    #[test]
    fn installation_defaults_draft_retains_revision_until_saved() {
        let mut browser = Browser::default();
        let original =
            json!({"revision":7,"protocols":["awg"],"settings":{"awg_i1_preset":"quic"}});
        browser.apply(Command::Defaults, original.clone());
        browser.defaults.as_mut().unwrap()["settings"]["awg_i1_preset"] = json!("dns");
        assert_eq!(browser.defaults_baseline, Some(original));
        let (method, path, _, _) = Command::DefaultsSave(json!({}), 7).request().unwrap();
        assert_eq!(method, Method::PUT);
        assert_eq!(path, "/api/v1/system/installation-defaults");
        browser.apply(Command::DefaultsSave(json!({}), 7), json!({"revision":8}));
        assert_eq!(browser.defaults, browser.defaults_baseline);
    }

    #[test]
    fn dismissed_alert_updates_unread_count_without_touching_cleanup() {
        let mut browser = Browser {
            alerts_page: 3,
            job: Some(json!({"status":"succeeded"})),
            ..Default::default()
        };
        browser.apply(
            Command::Alerts,
            json!({"active":[{"event_id":"test","dismissed":false}],"unacknowledged_count":1}),
        );
        assert_eq!(browser.alerts_page, 0);
        assert_eq!(browser.alerts_unread, 1);
        browser.apply(
            Command::AlertDismiss(Uuid::new_v4()),
            json!({"active":[{"event_id":"test","dismissed":true}],"unacknowledged_count":0}),
        );
        assert_eq!(browser.alerts_unread, 0);
        assert!(browser.job.is_some());
    }

    #[test]
    fn queue_keeps_confirmation_and_idempotency_key() {
        let key = Uuid::new_v4();
        let plan = Uuid::new_v4();
        let (_, path, body, sent) = Command::Queue {
            plan,
            phrase: "RESET NODE PLANE".into(),
            key,
        }
        .request()
        .unwrap();
        assert!(path.ends_with("/jobs"));
        assert_eq!(sent, Some(key));
        assert_eq!(body.unwrap()["plan_id"], plan.to_string());
    }
    #[test]
    fn removal_requires_independent_verification() {
        let mut browser = Browser::default();
        browser.apply(
            Command::Shutdown(Uuid::new_v4(), Uuid::new_v4()),
            json!({"status":"running"}),
        );
        assert!(browser.verifying);
        assert!(!browser.removed);
        browser.apply(Command::Verify(json!({})), json!({"removed":false}));
        assert!(!browser.removed);
        browser.apply(Command::Verify(json!({})), json!({"removed":true}));
        assert!(browser.removed);
    }
    #[cfg(unix)]
    #[test]
    fn host_verification_checks_paths_services_and_database() {
        use std::io::Write;
        use std::process::{Command as Process, Stdio};
        let dir = tempfile::tempdir().unwrap();
        let base = dir.path().join("missing-controller");
        let shared = dir.path().join("missing-shared");
        let driver = dir.path().join("missing-driver");
        let manifest = json!({"base_dir":base,"shared_dir":shared,"units":["node-plane-backend.service"],"postgres_container":"node-plane-postgres"});
        let script = verification_script(&manifest)
            .unwrap()
            .replace("/usr/local/bin/node-plane-driver", driver.to_str().unwrap());
        for (active, database, failure, expected) in [
            (false, false, false, true),
            (true, false, false, false),
            (false, true, false, false),
            (false, false, true, false),
        ] {
            let prelude = format!(
                "import subprocess, types\nsubprocess.run = lambda args, **kwargs: types.SimpleNamespace(returncode=({docker_code} if args[0]=='docker' else {unit_code}), stdout=({docker_output:?} if args[0]=='docker' else {unit_output:?}))\n",
                docker_code = if failure { 1 } else { 0 },
                unit_code = if active { 0 } else { 3 },
                docker_output = if database { "node-plane-postgres" } else { "" },
                unit_output = if active { "active" } else { "inactive" }
            );
            let mut child = Process::new("python3")
                .arg("-")
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .spawn()
                .unwrap();
            child
                .stdin
                .take()
                .unwrap()
                .write_all(format!("{prelude}{script}").as_bytes())
                .unwrap();
            let output = child.wait_with_output().unwrap();
            assert!(output.status.success());
            let value: Value = serde_json::from_slice(&output.stdout).unwrap();
            assert_eq!(value["removed"], expected);
        }
        std::fs::create_dir(&base).unwrap();
        let mut child = Process::new("python3")
            .arg("-")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let no_units =
            verification_script(&json!({"base_dir":base,"shared_dir":shared,"units":[]}))
                .unwrap()
                .replace("/usr/local/bin/node-plane-driver", driver.to_str().unwrap());
        child
            .stdin
            .take()
            .unwrap()
            .write_all(no_units.as_bytes())
            .unwrap();
        let output = child.wait_with_output().unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&output.stdout).unwrap()["removed"],
            false
        );
    }
    #[test]
    fn verification_resumes_only_for_the_same_saved_connection() {
        let state = tempfile::tempdir().unwrap();
        let installation = crate::connections::Installation {
            id: Uuid::new_v4(),
            name: "Test".into(),
            host: "controller.example".into(),
            port: 22,
            user: "root".into(),
            branch: "dev".into(),
            admin_ids: "42".into(),
            account: String::new(),
            installed: true,
        };
        let browser = Browser {
            overview: Some(
                json!({"deployment":{"base_dir":"/opt/node-plane","shared_dir":"/opt/node-plane/shared","units":[]}}),
            ),
            job: Some(json!({"action":"remove","shutdown_ack":true})),
            ..Default::default()
        };
        browser
            .save_verification(state.path(), &installation)
            .unwrap();
        let mut resumed = Browser::default();
        resumed
            .restore_verification(state.path(), &installation)
            .unwrap();
        assert!(matches!(resumed.poll(), Some(Command::Verify(_))));
        let mut changed = installation.clone();
        changed.host = "other.example".into();
        assert!(
            Browser::default()
                .restore_verification(state.path(), &changed)
                .is_err()
        );
        Browser::forget_verification(state.path(), installation.id).unwrap();
        assert!(
            !state
                .path()
                .join("operations")
                .join(format!("controller-removal-{}.json", installation.id))
                .exists()
        );
    }
    #[test]
    fn recovery_action_force_contract_requires_confirmation_body_for_allowed_kinds_only() {
        let id = Uuid::new_v4().to_string();
        for kind in ["settings", "profile", "node", "bootstrap"] {
            let cmd = Command::RecoveryAction(kind.into(), id.clone(), "force".into(), 0, None);
            let (method, path, body, _) = cmd.request().unwrap();
            assert_eq!(method, Method::POST);
            assert_eq!(path, format!("/api/v1/system/recovery/{kind}/{id}/force"));
            assert_eq!(
                body,
                Some(json!({
                    "confirm_operation_id": id,
                    "acknowledge_unknown_outcome": true,
                }))
            );
        }
        for kind in ["settings", "profile", "node", "bootstrap"] {
            let cmd = Command::RecoveryAction(kind.into(), id.clone(), "resolve".into(), 0, None);
            let (method, _, body, _) = cmd.request().unwrap();
            assert_eq!(method, Method::POST);
            assert_eq!(body, None);
        }
        let bad = Command::RecoveryAction("agent".into(), id.clone(), "force".into(), 0, None);
        assert!(bad.request().is_err());
    }
}
