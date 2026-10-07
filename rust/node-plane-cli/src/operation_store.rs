//! Nonsecret durable intent, written before remote dispatch. Never auto-replay.
use crate::{config::Request, ssh};
use anyhow::{Context, Result, ensure};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use std::{
    fs::{File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};
use tempfile::NamedTempFile;
use uuid::Uuid;

#[derive(Clone, Deserialize, Serialize, PartialEq, Debug)]
pub struct Intent {
    pub kind: String,
    pub target_ref: String,
    pub branch: String,
}
#[derive(Deserialize, Serialize)]
pub struct UpdateRecord {
    pub version: u32,
    pub id: Uuid,
    pub host: String,
    pub port: u16,
    pub user: String,
    pub account_id: String,
    pub intent: Intent,
    pub dispatch_started: bool,
    pub job_id: Option<Uuid>,
    pub complete: bool,
}
impl UpdateRecord {
    pub fn matches(&self, r: &Request) -> bool {
        self.host.eq_ignore_ascii_case(&r.host) && self.port == r.port && self.user == r.user
    }
    pub fn path(&self, state: &Path) -> PathBuf {
        state.join(format!("update-{}.json", self.id))
    }
    pub fn save(&self, state: &Path) -> Result<()> {
        ssh::secure_state_directory(state)?;
        let path = self.path(state);
        if path.exists() {
            ssh::secure_file(&path)?;
        }
        let mut temporary = NamedTempFile::new_in(state)?;
        ssh::secure_file(temporary.path())?;
        temporary.write_all(&serde_json::to_vec(self)?)?;
        temporary.as_file().sync_all()?;
        temporary.persist(&path).map_err(|error| error.error)?;
        #[cfg(unix)]
        File::open(state)?.sync_all()?;
        Ok(())
    }
    pub fn load(path: &Path) -> Result<Self> {
        ssh::secure_file(path)?;
        ensure!(
            path.metadata()?.len() <= 65536,
            "Saved update record is oversized."
        );
        let value: Self = serde_json::from_slice(&std::fs::read(path)?)
            .context("Saved update record is invalid; it was not replaced")?;
        ensure!(
            value.version == 1
                && Uuid::parse_str(&value.account_id).is_ok()
                && value.intent.kind == "stack"
                && matches!(value.intent.branch.as_str(), "main" | "dev"),
            "Saved update record is unsupported."
        );
        ensure!(
            path.file_name().and_then(|p| p.to_str()) == Some(&format!("update-{}.json", value.id)),
            "Saved update identity does not match its filename."
        );
        Ok(value)
    }
}
pub fn unfinished(request: &Request) -> Result<Option<UpdateRecord>> {
    if let Some(id) = request.workflow.operation {
        let record = UpdateRecord::load(&request.state_dir.join(format!("update-{id}.json")))?;
        ensure!(
            record.matches(request),
            "Saved update belongs to another controller SSH address/user."
        );
        return Ok(Some(record));
    }
    let mut found = None;
    for item in std::fs::read_dir(&request.state_dir)? {
        let path = item?.path();
        let name = path
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or_default();
        if name.starts_with("update-") && name.ends_with(".json") {
            let value = UpdateRecord::load(&path)?;
            if !value.complete && value.matches(request) {
                ensure!(
                    found.is_none(),
                    "Several unfinished updates exist. Select the exact --operation UUID."
                );
                found = Some(value);
            }
        }
    }
    Ok(found)
}
pub struct OperationLock(File);
impl OperationLock {
    pub fn acquire(state: &Path) -> Result<Self> {
        ssh::secure_state_directory(state)?;
        let path = state.join("workstation-operation.lock");
        if path.exists() {
            ssh::secure_file(&path)?;
        }
        let mut options = OpenOptions::new();
        options.create(true).truncate(false).read(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let file = options.open(&path)?;
        ssh::secure_file(&path)?;
        file.try_lock_exclusive().context(
            "Another assistant operation is running. Wait before opening this controller again",
        )?;
        Ok(Self(file))
    }
}
impl Drop for OperationLock {
    fn drop(&mut self) {
        let _ = FileExt::unlock(&self.0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn dispatch_identity_survives_restart_without_secrets() {
        let state = tempfile::tempdir().unwrap();
        let id = Uuid::new_v4();
        let mut record = UpdateRecord {
            version: 1,
            id,
            host: "vps.example".into(),
            port: 22,
            user: "root".into(),
            account_id: Uuid::new_v4().to_string(),
            intent: Intent {
                kind: "stack".into(),
                target_ref: "v0.4.3-alpha.48".into(),
                branch: "dev".into(),
            },
            dispatch_started: false,
            job_id: None,
            complete: false,
        };
        record.save(state.path()).unwrap();
        record.dispatch_started = true;
        record.save(state.path()).unwrap();
        let loaded = UpdateRecord::load(&record.path(state.path())).unwrap();
        assert_eq!(loaded.id, id);
        assert!(loaded.dispatch_started);
        assert_eq!(loaded.intent, record.intent);
        let text = std::fs::read_to_string(record.path(state.path())).unwrap();
        assert!(!text.contains("token"));
        assert!(!text.contains("password"));
    }
    #[test]
    fn local_lock_excludes_another_process_attempt() {
        let state = tempfile::tempdir().unwrap();
        let one = OperationLock::acquire(state.path()).unwrap();
        assert!(OperationLock::acquire(state.path()).is_err());
        drop(one);
        assert!(OperationLock::acquire(state.path()).is_ok());
    }
}
