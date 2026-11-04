//! Decommission fence: prepared before runtime removal, verified after.
use std::path::Path;

use tonic::Status;

use crate::intents::{Lock, decommission, profile};
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn prepare_decommission(&self, command_id: &str) -> Result<(), Status> {
        let runtime = self.resolve_runtime_path("/opt/node-plane-runtime");
        // Without a runtime only the fences are checked; no revocation is
        // attempted.
        let revocable = Path::new(&self.config.node_env_path).is_file();
        let mut runner = |intent: &profile::Intent, lock| {
            profile::run(intent, lock, Path::new(&runtime), &self.instance_id)
        };
        decommission::prepare(
            &self.journal(),
            command_id,
            &self.instance_id,
            revocable.then_some(&mut runner as &mut profile::Runner<'_>),
        )
        .map_err(|reason| {
            if reason == "invalid decommission command identity" {
                return Status::invalid_argument(reason);
            }
            Status::failed_precondition(format!(
                "profile revocations are not confirmed; decommission refused: {}",
                reason.chars().take(2048).collect::<String>()
            ))
        })?;
        Ok(())
    }

    pub(crate) fn verify_prepared_decommission(&self, command_id: &str) -> Result<(), Status> {
        decommission::verify(&self.journal(), command_id)
            .map(|_| ())
            .map_err(|_| Status::failed_precondition("decommission identity is not prepared"))
    }

    pub(crate) fn guard_prepared_decommission(&self, command_id: &str) -> Result<Lock, Status> {
        let lock = Lock::exclusive(&self.journal())
            .map_err(|_| Status::failed_precondition("profile fence lock unavailable"))?;
        // Runtime cleanup removes the runtime. The durable marker lives
        // beside the journal and remains readable until agent uninstall.
        self.verify_prepared_decommission(command_id)?;
        Ok(lock)
    }
}
