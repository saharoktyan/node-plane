//! Explicit forced recovery of an interrupted command.
use std::io::Write;
use std::process::{Command, Stdio};

use serde_json::Value;
use tonic::Status;

use crate::agent::v1::{ForceRecoveryRequest, RuntimeCommandResponse};
use crate::fencing::valid_force_identity;
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn force_recovery(
        &self,
        req: ForceRecoveryRequest,
    ) -> Result<RuntimeCommandResponse, Status> {
        if req.node_key != self.config.node_key {
            return Err(Status::failed_precondition("agent node identity mismatch"));
        }
        if !matches!(req.phase.as_str(), "prepare" | "retire")
            || !matches!(req.operation_kind.as_str(), "settings" | "profile" | "node")
            || !valid_force_identity(&req.ticket_id)
            || !valid_force_identity(&req.command_id)
            || req.intent_json.len() > 65536
        {
            return Err(Status::invalid_argument("invalid force recovery request"));
        }
        let intent: Value = serde_json::from_str(&req.intent_json)
            .map_err(|_| Status::invalid_argument("invalid force recovery request"))?;
        let spec = serde_json::json!({
            "node_key": req.node_key, "ticket_id": req.ticket_id,
            "command_id": req.command_id, "kind": req.operation_kind, "intent": intent,
        });
        let mut child = Command::new("python3")
            .args([
                "-c",
                include_str!("../../../../runtime_assets/force-recovery.py"),
                &req.phase,
            ])
            .env("NODE_PLANE_AGENT_INSTANCE_ID", &self.instance_id)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|_| Status::failed_precondition("force_helper_unavailable"))?;
        child
            .stdin
            .take()
            .ok_or_else(|| Status::failed_precondition("force_helper_unavailable"))?
            .write_all(spec.to_string().as_bytes())
            .map_err(|_| Status::failed_precondition("force_helper_unavailable"))?;
        let output = child
            .wait_with_output()
            .map_err(|_| Status::failed_precondition("force_helper_unavailable"))?;
        if !output.status.success() {
            let reason = String::from_utf8_lossy(&output.stderr);
            let reason = reason.trim();
            let safe = reason.starts_with("force_")
                && reason.len() <= 100
                && reason.bytes().all(|c| c.is_ascii_lowercase() || c == b'_');
            return Err(Status::failed_precondition(if safe {
                reason
            } else {
                "force_recovery_failed"
            }));
        }
        let value: Value = serde_json::from_slice(&output.stdout)
            .map_err(|_| Status::internal("invalid force recovery receipt"))?;
        if req.phase == "prepare" && value["restart_required"] == true {
            // The normal service stop kills its entire cgroup, including child
            // mutation scripts. The journal flock independently verifies this
            // at retirement; a restarted listener alone is not sufficient.
            let property = |name: &str| -> Option<String> {
                let output = Command::new("timeout")
                    .args([
                        "5s",
                        "systemctl",
                        "show",
                        "node-plane-agent.service",
                        "--value",
                        "-p",
                        name,
                    ])
                    .output()
                    .ok()?;
                output
                    .status
                    .success()
                    .then(|| String::from_utf8_lossy(&output.stdout).trim().to_string())
            };
            if property("MainPID") != Some(std::process::id().to_string())
                || property("KillMode").as_deref() != Some("control-group")
            {
                return Err(Status::failed_precondition(
                    "force_managed_restart_unavailable",
                ));
            }
            let unit = format!("node-plane-force-recovery-{}", req.ticket_id);
            let restart = Command::new("timeout")
                .args([
                    "5s",
                    "systemd-run",
                    "--quiet",
                    "--collect",
                    "--unit",
                    &unit,
                    "--on-active=1s",
                    "/bin/systemctl",
                    "restart",
                    "node-plane-agent.service",
                ])
                .output()
                .map_err(|_| Status::failed_precondition("force_restart_unavailable"))?;
            if !restart.status.success() {
                let scheduled = Command::new("timeout")
                    .args([
                        "5s",
                        "systemctl",
                        "is-active",
                        "--quiet",
                        &format!("{unit}.timer"),
                    ])
                    .status()
                    .is_ok_and(|status| status.success());
                if !scheduled {
                    return Err(Status::failed_precondition("force_restart_unavailable"));
                }
            }
        }
        Ok(RuntimeCommandResponse {
            summary: "explicit force recovery receipt".into(),
            payload_json: value.to_string(),
        })
    }
}
