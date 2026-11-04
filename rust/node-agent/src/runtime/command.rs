//! Runtime entry points (`entry`) the RPCs run, under the profile intent
//! journal's lock and fences (`intents::legacy`).
use std::path::Path;

use tonic::Status;

use crate::intents::legacy;
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn run_runtime_command(
        &self,
        script_name: &str,
        args: &[String],
    ) -> Result<String, Status> {
        if legacy::scope(script_name).is_some() {
            let runtime = self.resolve_runtime_path("/opt/node-plane-runtime");
            return legacy::run(
                &self.journal(),
                Path::new(&runtime),
                script_name,
                args,
                &self.instance_id,
            )
            .map_err(|err| {
                let detail = err
                    .strip_prefix(&format!("{script_name} failed: "))
                    .unwrap_or(&err)
                    .to_string();
                Status::failed_precondition(format!("{script_name} failed: {detail}"))
            });
        }
        Err(Status::invalid_argument(format!(
            "unknown runtime entry point: {script_name}"
        )))
    }
}
