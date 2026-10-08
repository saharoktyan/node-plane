//! Private, nonsecret workstation connection preferences.
use crate::ssh;
use anyhow::{Context, Result, ensure};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::Path,
};
use tempfile::NamedTempFile;
use uuid::Uuid;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Installation {
    pub id: Uuid,
    pub name: String,
    pub host: String,
    pub port: u16,
    pub user: String,
    pub branch: String,
    pub admin_ids: String,
    pub account: String,
}
impl Installation {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            !self.name.trim().is_empty()
                && self.name.len() <= 120
                && !self.name.chars().any(char::is_control),
            "Enter a short installation name."
        );
        ensure!(
            self.host.len() <= 253
                && self
                    .host
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || ".:-".contains(c)),
            "Enter a hostname or IP address without a scheme, username or path."
        );
        ensure!(self.port > 0, "SSH port must be between 1 and 65535.");
        ensure!(
            self.user.len() <= 64
                && self
                    .user
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || "_-".contains(c)),
            "Enter a valid SSH username."
        );
        ensure!(
            matches!(self.branch.as_str(), "" | "main" | "dev"),
            "Update channel must be main or dev, or blank to use the installed channel."
        );
        ensure!(
            self.admin_ids.is_empty()
                || self
                    .admin_ids
                    .split(',')
                    .all(|id| id.trim().parse::<u64>().is_ok_and(|id| id > 0)),
            "Administrator IDs must be positive numbers separated by commas."
        );
        ensure!(
            self.account.is_empty()
                || Uuid::parse_str(&self.account).is_ok()
                || self
                    .account
                    .parse::<u64>()
                    .is_ok_and(|id| id > 0 && id < 2_u64.pow(63)),
            "Administrator account must be a UUID or Telegram ID."
        );
        Ok(())
    }
    pub fn matches(&self, host: &str, port: u16, user: &str) -> bool {
        self.host.eq_ignore_ascii_case(host) && self.port == port && self.user == user
    }
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Connections {
    pub version: u32,
    pub revision: u64,
    pub selected: Option<Uuid>,
    pub installations: Vec<Installation>,
}
impl Default for Connections {
    fn default() -> Self {
        Self {
            version: 1,
            revision: 0,
            selected: None,
            installations: Vec::new(),
        }
    }
}
impl Connections {
    pub fn load(state: &Path) -> Result<Self> {
        let path = state.join("installations.json");
        if !path.try_exists()? {
            return Ok(Self::default());
        }
        ssh::secure_file(&path)?;
        ensure!(
            path.metadata()?.len() <= 1024 * 1024,
            "Installation preferences are oversized."
        );
        let value: Self = serde_json::from_slice(&fs::read(path)?)
            .context("Saved installation preferences are invalid; they were not replaced")?;
        ensure!(
            value.version == 1 && value.installations.len() <= 1000,
            "Saved installation preferences are unsupported."
        );
        let mut ids = std::collections::HashSet::new();
        for profile in &value.installations {
            profile.validate()?;
            ensure!(
                ids.insert(profile.id),
                "Saved installation IDs are duplicated."
            );
        }
        ensure!(
            value.selected.is_none_or(|id| ids.contains(&id)),
            "Selected installation is missing."
        );
        Ok(value)
    }
    pub fn active(&self) -> Option<&Installation> {
        self.installations
            .iter()
            .find(|p| Some(p.id) == self.selected)
    }
    pub fn save(&mut self, state: &Path) -> Result<()> {
        ssh::secure_state_directory(state)?;
        let lock_path = state.join("installations.lock");
        if lock_path.exists() {
            ssh::secure_file(&lock_path)?;
        }
        let mut options = OpenOptions::new();
        options.create(true).truncate(false).read(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let lock = options.open(&lock_path)?;
        ssh::secure_file(&lock_path)?;
        lock.try_lock_exclusive()
            .context("Installation preferences are being edited in another window. Try again.")?;
        let current = Self::load(state)?;
        ensure!(
            current.revision == self.revision,
            "Installation preferences changed in another window. Restart the assistant to reload them."
        );
        ensure!(
            self.installations.len() <= 1000,
            "Too many saved installations."
        );
        for profile in &self.installations {
            profile.validate()?;
        }
        let next_revision = self
            .revision
            .checked_add(1)
            .context("Installation preference revision is exhausted")?;
        let mut next = self.clone();
        next.revision = next_revision;
        let mut file = NamedTempFile::new_in(state)?;
        ssh::secure_file(file.path())?;
        file.write_all(&serde_json::to_vec_pretty(&next)?)?;
        file.as_file().sync_all()?;
        file.persist(state.join("installations.json"))
            .map_err(|e| e.error)?;
        #[cfg(unix)]
        fs::File::open(state)?.sync_all()?;
        self.revision = next_revision;
        Ok(())
    }
    pub fn import_update_history(&mut self, state: &Path) -> Result<bool> {
        if !self.installations.is_empty() || !state.try_exists()? {
            return Ok(false);
        }
        let mut records = Vec::new();
        for entry in fs::read_dir(state)? {
            let path = entry?.path();
            let name = path
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or_default();
            if name.starts_with("update-") && name.ends_with(".json") {
                // A damaged history record must not prevent editing new connections.
                if let Ok(record) = crate::operation_store::UpdateRecord::load(&path) {
                    records.push(record);
                }
            }
        }
        records.sort_by(|a, b| (&a.host, a.port, &a.user).cmp(&(&b.host, b.port, &b.user)));
        for record in records {
            if self
                .installations
                .iter()
                .any(|p| p.matches(&record.host, record.port, &record.user))
            {
                continue;
            }
            let profile = Installation {
                id: Uuid::new_v4(),
                name: record.host.clone(),
                host: record.host,
                port: record.port,
                user: record.user,
                branch: String::new(),
                admin_ids: String::new(),
                account: record.account_id,
            };
            if profile.validate().is_ok() {
                self.installations.push(profile);
            }
        }
        if let Some(profile) = self.installations.first() {
            self.selected = Some(profile.id);
            self.save(state)?;
            return Ok(true);
        }
        Ok(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn installation(host: &str) -> Installation {
        Installation {
            id: Uuid::new_v4(),
            name: host.into(),
            host: host.into(),
            port: 22,
            user: "root".into(),
            branch: String::new(),
            admin_ids: "42".into(),
            account: String::new(),
        }
    }
    #[test]
    fn preferences_roundtrip_and_conflicting_windows_do_not_overwrite_each_other() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Connections::default();
        let one = installation("one.example");
        store.selected = Some(one.id);
        store.installations.push(one);
        store.save(dir.path()).unwrap();
        let mut stale = Connections::load(dir.path()).unwrap();
        store.installations.push(installation("two.example"));
        store.selected = Some(store.installations[1].id);
        store.save(dir.path()).unwrap();
        assert!(stale.save(dir.path()).is_err());
        let loaded = Connections::load(dir.path()).unwrap();
        assert_eq!(loaded.active().unwrap().host, "two.example");
        let raw = fs::read_to_string(dir.path().join("installations.json")).unwrap();
        assert!(!raw.contains("token") && !raw.contains("password"));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(dir.path().join("installations.json"))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }
    }
    #[test]
    fn corrupt_preferences_are_not_silently_replaced() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("installations.json"), "invalid").unwrap();
        assert!(Connections::load(dir.path()).is_err());
        assert!(Connections::default().save(dir.path()).is_err());
        assert_eq!(
            fs::read_to_string(dir.path().join("installations.json")).unwrap(),
            "invalid"
        );
    }
    #[test]
    fn update_history_imports_connections_once_without_changing_operation_records() {
        use crate::operation_store::{Intent, UpdateRecord};
        let dir = tempfile::tempdir().unwrap();
        let record = UpdateRecord {
            version: 1,
            id: Uuid::new_v4(),
            host: "vps.example".into(),
            port: 2222,
            user: "admin".into(),
            account_id: Uuid::new_v4().to_string(),
            intent: Intent {
                kind: "stack".into(),
                target_ref: "origin/dev".into(),
                branch: "dev".into(),
            },
            dispatch_started: true,
            job_id: Some(Uuid::new_v4()),
            complete: false,
        };
        record.save(dir.path()).unwrap();
        let before = fs::read(record.path(dir.path())).unwrap();
        let mut store = Connections::default();
        assert!(store.import_update_history(dir.path()).unwrap());
        assert_eq!(store.installations.len(), 1);
        assert_eq!(store.active().unwrap().port, 2222);
        assert_eq!(store.active().unwrap().account, record.account_id);
        assert!(!store.import_update_history(dir.path()).unwrap());
        assert_eq!(fs::read(record.path(dir.path())).unwrap(), before);
    }
}
