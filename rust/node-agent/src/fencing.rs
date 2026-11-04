//! Command fences shared by mutating operations.
use std::path::Path;

use tonic::Status;

use crate::intents::Lock;
use crate::intents::legacy::{self, Target};
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

    /// Holds the journal lock for a maintenance change, once no backend
    /// fence owns the runtime.
    pub(crate) fn guard_fenced_maintenance(&self) -> Result<Option<Lock>, Status> {
        let journal = self.journal();
        let lock = Lock::exclusive(&journal)
            .map_err(|_| Status::failed_precondition("profile fence lock unavailable"))?;
        legacy::guard(&journal, Target::Node).map_err(|_| {
            Status::failed_precondition(
                "runtime is managed by the explicit backend; maintenance refused",
            )
        })?;
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
