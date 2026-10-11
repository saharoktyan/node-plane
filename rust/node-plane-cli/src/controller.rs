use anyhow::{Result, ensure};
use hyper::Method;
use serde_json::{Value, json};
use uuid::Uuid;

#[derive(Clone)]
pub enum Command {
    Probe,
    Overview,
    Updates,
    Defaults,
    Policies,
    Alerts,
    WorkstationAudit(usize),
    RecoveryHistory(usize),
    AnnouncementOverview,
    AnnouncementPreview(String),
    AnnouncementCreate(String, Uuid, u64),
    AnnouncementStatus(Uuid),
    AlertPreferences(Value),
    AlertDismiss(Uuid),
    BotTitle,
    BotTitleSave(String),
    SshKey,
    PolicySave(&'static str, Value),
    DefaultsSave(Value, u64),
    Backups,
    BackupCatalog(usize),
    BackupDetail(Uuid),
    BackupDelete(Uuid, String),
    BackupPreferences(Value),
    BackupQueue(Value, Uuid),
    BackupJob(Uuid),
    UpdateVersions(usize),
    UpdateComponents,
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
    Verify(Value),
}
impl Command {
    pub fn request(&self) -> Result<(Method, String, Option<Value>, Option<Uuid>)> {
        let root = "/api/v1/system/cleanup";
        Ok(match self {
            Self::Probe => anyhow::bail!("Installation detection uses the SSH connection"),
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
            Self::UpdateComponents => (
                Method::GET,
                "/api/v1/system/updates/rollout".into(),
                None,
                None,
            ),
            Self::UpdateVersions(offset) => (
                Method::GET,
                format!("/api/v1/system/updates/versions?offset={offset}"),
                None,
                None,
            ),
            Self::Backups => (Method::GET, "/api/v1/system/backups".into(), None, None),
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
            Self::WorkstationAudit(offset) => (
                Method::GET,
                format!("/api/v1/system/workstation-audit?offset={offset}"),
                None,
                None,
            ),
            Self::RecoveryHistory(offset) => (
                Method::GET,
                format!("/api/v1/system/recovery/history?offset={offset}"),
                None,
                None,
            ),
            Self::AnnouncementOverview => (Method::GET, "/api/v1/announcements".into(), None, None),
            Self::AnnouncementPreview(text) => (
                Method::POST,
                "/api/v1/announcements/preview".into(),
                Some(json!({"text":text})),
                None,
            ),
            Self::AnnouncementCreate(text, key, _) => (
                Method::POST,
                "/api/v1/announcements".into(),
                Some(json!({"text":text})),
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
                Some(json!({})),
                Some(*key),
            ),
            Self::Verify(_) => anyhow::bail!("Host verification does not use the backend"),
        })
    }
}
#[derive(Default)]
pub struct Browser {
    pub plan_pending: bool,
    pub profile: Option<Uuid>,
    pub overview: Option<Value>,
    pub updates: Option<Value>,
    pub branch_confirmation: bool,
    pub defaults: Option<Value>,
    pub policies: Option<Value>,
    pub alerts: Option<Value>,
    pub workstation_audit: Option<Value>,
    pub recovery_history: Option<Value>,
    pub history_detail: Option<(&'static str, usize)>,
    pub announcement_overview: Option<Value>,
    pub announcement_draft: Option<String>,
    pub announcement_preview: Option<Value>,
    pub announcement_job: Option<Value>,
    pub announcement_key: Option<Uuid>,
    pub alerts_page: usize,
    pub alerts_unread: u64,
    pub bot_title: Option<Value>,
    pub ssh_key: Option<Value>,
    pub notice: String,
    pub defaults_advanced: bool,
    pub defaults_input: Option<(&'static str, String)>,
    pub defaults_baseline: Option<Value>,
    pub backups: Option<Value>,
    pub backup_catalog: Option<Value>,
    pub backup_detail: Option<Value>,
    pub backup_job: Option<Value>,
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
    pub fn apply(&mut self, command: Command, value: Value) {
        let selected = self.selected;
        let keep_focus = matches!(
            &command,
            Command::AlertPreferences(_)
                | Command::AlertDismiss(_)
                | Command::PolicySave(_, _)
                | Command::BackupPreferences(_)
                | Command::UpdateCheck
                | Command::UpdatePreferences(_)
                | Command::AnnouncementStatus(_)
        );
        match command {
            Command::BotTitle | Command::BotTitleSave(_) => {
                self.bot_title = Some(value);
                self.defaults_input = None;
                self.selected = 0;
            }
            Command::SshKey => {
                self.ssh_key = Some(value);
                self.notice.clear();
                self.selected = 0;
            }
            Command::WorkstationAudit(_) => {
                self.workstation_audit = Some(value);
                self.recovery_history = None;
                self.history_detail = None;
                self.selected = 0;
            }
            Command::RecoveryHistory(_) => {
                self.recovery_history = Some(value);
                self.workstation_audit = None;
                self.history_detail = None;
                self.selected = 0;
            }
            Command::AnnouncementOverview => {
                self.announcement_job = value.get("last_job").filter(|v| v.is_object()).cloned();
                self.announcement_overview = Some(value);
                self.announcement_preview = None;
                self.announcement_draft = None;
                self.announcement_key = None;
                self.selected = 0;
            }
            Command::AnnouncementPreview(_) => {
                self.announcement_preview = Some(value);
                self.announcement_job = None;
                self.selected = 0;
            }
            Command::AnnouncementCreate(_, _, _) | Command::AnnouncementStatus(_) => {
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
            Command::Policies | Command::PolicySave(_, _) => {
                self.policies = Some(value);
                self.defaults_input = None;
                self.selected = 0;
            }
            Command::Defaults | Command::DefaultsSave(_, _) => {
                self.defaults_baseline = Some(value.clone());
                self.defaults = Some(value);
                self.selected = 0;
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
            Command::BackupDetail(_) => {
                self.backup_detail = Some(value);
                self.selected = 0;
            }
            Command::BackupQueue(_, _) => {
                self.backup_job = Some(value);
                self.backup_detail = None;
                self.backup_catalog = None;
                self.selected = 0;
            }
            Command::BackupJob(_) => {
                self.backup_job = Some(value);
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
            Command::UpdateComponents => {
                self.components = Some(value);
                self.components_page = 0;
                self.selected = 0;
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
            Command::Probe => {}
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
        std::fs::create_dir_all(state)?;
        let mut temporary = tempfile::NamedTempFile::new_in(state)?;
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
        let path = state.join(format!("controller-removal-{}.json", installation.id));
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
        let path = state.join(format!("controller-removal-{id}.json"));
        match std::fs::remove_file(path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e.into()),
        }
    }
    pub fn poll(&self) -> Option<Command> {
        if let Some(job) = &self.backup_job
            && self.backups.is_some()
        {
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

#[cfg(test)]
mod tests {
    use super::*;
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
        let mut browser = Browser {
            backups: Some(json!({})),
            ..Default::default()
        };
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
                .join(format!("controller-removal-{}.json", installation.id))
                .exists()
        );
    }
}
