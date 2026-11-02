//! Runtime helper scripts, guarded by the profile intent journal.
use std::path::Path;
use std::process::Command;

use tonic::Status;

use crate::state::AgentState;

impl AgentState {
    pub(crate) fn run_runtime_command(
        &self,
        script_name: &str,
        args: &[String],
    ) -> Result<String, Status> {
        let script_path =
            self.resolve_runtime_path(&format!("/opt/node-plane-runtime/{script_name}"));
        let helper = self.resolve_runtime_path("/opt/node-plane-runtime/apply-profile-intent.py");
        let journal = Path::new("/etc/node-plane/profile-intents.sqlite3");
        let scope = match script_name {
            "xray-add-user-existing.sh" | "xray-del-user.sh" => Some("xray"),
            "awg-add-user.sh" | "awg-del-user.sh" => Some("awg"),
            "init-xray.sh"
            | "deploy-xray.sh"
            | "init-awg.sh"
            | "deploy-awg.sh"
            | "apply-node-settings.sh"
            | "regenerate-awg-entropy.sh"
            | "sync-xray.sh" => Some("all"),
            _ => None,
        };
        if scope.is_some() && !Path::new(&helper).exists() && journal.exists() {
            return Err(Status::failed_precondition(
                "profile fence helper missing; legacy mutation refused",
            ));
        }
        let mut command = if scope.is_some() && Path::new(&helper).exists() {
            let mut command = Command::new(&helper);
            let kind = scope.expect("guarded command has scope");
            let profile = if kind == "all" {
                ""
            } else {
                args.first().map(String::as_str).unwrap_or("")
            };
            command.args(["run-legacy", kind, profile, script_name]);
            command
        } else {
            Command::new(&script_path)
        };
        let output = command
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
