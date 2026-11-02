//! The controller's SSH key in the agent host's authorized_keys.
use std::env;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use tonic::Status;

use crate::agent::v1::RemoveAuthorizedKeyResponse;
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn authorized_keys_path(&self) -> Result<PathBuf, Status> {
        agent_authorized_keys_path(env::var("HOME").ok().as_deref())
    }

    pub(crate) fn remove_authorized_key(
        &self,
        public_key: &str,
    ) -> Result<RemoveAuthorizedKeyResponse, Status> {
        let key = public_key.trim();
        if key.is_empty() {
            return Err(Status::invalid_argument("public_key is required"));
        }
        let path = self.authorized_keys_path()?;
        if !path.is_file() {
            return Ok(RemoveAuthorizedKeyResponse {
                summary: "authorized_keys not present".to_string(),
                removed: false,
            });
        }
        let raw = fs::read_to_string(&path)
            .map_err(|err| Status::internal(format!("failed to read authorized_keys: {err}")))?;
        let lines: Vec<&str> = raw.lines().collect();
        let kept: Vec<&str> = lines
            .iter()
            .copied()
            .filter(|line| line.trim() != key)
            .collect();
        if kept.len() == lines.len() {
            return Ok(RemoveAuthorizedKeyResponse {
                summary: "bot ssh key not present".to_string(),
                removed: false,
            });
        }
        let mut next = kept.join("\n");
        if !next.is_empty() {
            next.push('\n');
        }
        fs::write(&path, next)
            .map_err(|err| Status::internal(format!("failed to write authorized_keys: {err}")))?;
        let _ = fs::set_permissions(&path, fs::Permissions::from_mode(0o600));
        Ok(RemoveAuthorizedKeyResponse {
            summary: "bot ssh key removed".to_string(),
            removed: true,
        })
    }
}

pub(crate) fn agent_authorized_keys_path(home: Option<&str>) -> Result<PathBuf, Status> {
    // The supported systemd agent runs as root without User=, so HOME is
    // normally absent. Never require a login-shell environment for removal.
    let path = Path::new(home.filter(|value| !value.is_empty()).unwrap_or("/root"));
    if !path.is_absolute()
        || path
            .components()
            .any(|part| matches!(part, std::path::Component::ParentDir))
    {
        return Err(Status::failed_precondition("invalid agent home directory"));
    }
    Ok(path.join(".ssh").join("authorized_keys"))
}

#[cfg(test)]
mod tests {
    #[test]
    fn service_removal_does_not_require_a_login_home() {
        assert_eq!(
            super::agent_authorized_keys_path(None).unwrap(),
            std::path::PathBuf::from("/root/.ssh/authorized_keys")
        );
        assert_eq!(
            super::agent_authorized_keys_path(Some("")).unwrap(),
            std::path::PathBuf::from("/root/.ssh/authorized_keys")
        );
        assert!(super::agent_authorized_keys_path(Some("relative")).is_err());
        assert!(super::agent_authorized_keys_path(Some("/root/../etc")).is_err());
    }
}
