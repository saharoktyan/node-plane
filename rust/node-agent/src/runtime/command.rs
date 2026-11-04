//! Runtime helper scripts; mutating ones run under the profile intent
//! journal's lock and fences (`intents::legacy`).
use std::path::Path;
use std::process::Command;

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
        let script_path =
            self.resolve_runtime_path(&format!("/opt/node-plane-runtime/{script_name}"));
        let output = Command::new(&script_path)
            .env("NODE_PLANE_AGENT_INSTANCE_ID", &self.instance_id)
            // Helpers reach the Xray API through this exact binary.
            .env(
                "NODE_PLANE_AGENT_BIN",
                std::env::current_exe().unwrap_or_default(),
            )
            .args(args)
            .output()
            .map_err(|err| Status::internal(format!("failed to execute {script_name}: {err}")))?;
        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        if output.status.success() {
            return Ok(stdout);
        }
        let detail = if stderr.is_empty() { stdout } else { stderr };
        Err(Status::failed_precondition(format!(
            "{script_name} failed: {}",
            if detail.is_empty() {
                "unknown error".to_string()
            } else {
                detail
            }
        )))
    }
}
