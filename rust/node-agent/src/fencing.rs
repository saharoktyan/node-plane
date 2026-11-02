//! Command fences shared by mutating operations.
use std::fs;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::process::Command;

use tonic::Status;

use crate::state::AgentState;

impl AgentState {
    pub(crate) fn check_force_dispatch_fence(&self, command_id: &str) -> Result<(), Status> {
        if valid_force_identity(command_id)
            && Path::new("/etc/node-plane/force-recovery")
                .join(format!("command-{command_id}.fence"))
                .exists()
        {
            return Err(Status::failed_precondition("force_command_cancelled"));
        }
        Ok(())
    }

    pub(crate) fn guard_fenced_maintenance(&self) -> Result<Option<fs::File>, Status> {
        let helper = self.resolve_runtime_path("/opt/node-plane-runtime/apply-profile-intent.py");
        let journal = Path::new("/etc/node-plane/profile-intents.sqlite3");
        if !Path::new(&helper).exists() && !journal.exists() {
            return Ok(None);
        }
        if !Path::new(&helper).exists() {
            return Err(Status::failed_precondition(
                "profile fence helper missing; maintenance refused",
            ));
        }
        let lock = fs::OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .mode(0o600)
            .open("/etc/node-plane/profile-intents.sqlite3.lock")
            .map_err(|_| Status::failed_precondition("profile fence lock unavailable"))?;
        lock.lock()
            .map_err(|_| Status::failed_precondition("profile fence lock unavailable"))?;
        let output = Command::new(&helper)
            .arg("guard-maintenance")
            .output()
            .map_err(|_| Status::failed_precondition("profile fence check unavailable"))?;
        if !output.status.success() {
            return Err(Status::failed_precondition(
                "runtime is managed by the explicit backend; maintenance refused",
            ));
        }
        Ok(Some(lock))
    }
}

pub(crate) fn valid_force_identity(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(i, c)| {
            if matches!(i, 8 | 13 | 18 | 23) {
                c == b'-'
            } else {
                c.is_ascii_digit() || (b'a'..=b'f').contains(&c)
            }
        })
}
