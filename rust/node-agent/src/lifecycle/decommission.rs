//! Decommission fence: prepared before runtime removal, verified after.
use std::fs;
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::process::Command;

use serde_json::Value;
use tonic::Status;

use crate::state::AgentState;

impl AgentState {
    pub(crate) fn prepare_decommission(&self, command_id: &str) -> Result<(), Status> {
        let helper = self.resolve_runtime_path("/opt/node-plane-runtime/apply-profile-intent.py");
        if !Path::new(&helper).exists()
            && !Path::new("/etc/node-plane/profile-intents.sqlite3").exists()
        {
            let valid_id = command_id.len() == 36
                && command_id.bytes().enumerate().all(|(index, byte)| {
                    if matches!(index, 8 | 13 | 18 | 23) {
                        byte == b'-'
                    } else {
                        byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()
                    }
                });
            if !valid_id {
                return Err(Status::invalid_argument(
                    "invalid decommission command identity",
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
            let marker_path = "/etc/node-plane/profile-intents.sqlite3.disabled";
            if Path::new(marker_path).exists() {
                self.verify_prepared_decommission(command_id)?;
                return Ok(());
            }
            let mut marker = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(marker_path)
                .map_err(|_| Status::failed_precondition("cannot create decommission fence"))?;
            marker
                .write_all(
                    serde_json::json!({"kind": "decommission", "command_id": command_id})
                        .to_string()
                        .as_bytes(),
                )
                .and_then(|_| marker.write_all(b"\n"))
                .and_then(|_| marker.sync_all())
                .and_then(|_| fs::File::open("/etc/node-plane")?.sync_all())
                .map_err(|_| Status::failed_precondition("cannot persist decommission fence"))?;
            return Ok(());
        }
        // A draining node cannot bootstrap again to replace its old helper.
        // Use this agent's journal implementation, retaining the deployed path
        // solely to locate the protocol deletion scripts invoked by run().
        let code = format!(
            "import sys\n__file__ = sys.argv[1]\nsys.argv = sys.argv[1:]\n{}",
            include_str!("../../../../runtime_assets/apply-profile-intent.py")
        );
        // An agent-only recovery may have created a control-only journal.
        // Without runtime helpers, inspect fences only; never attempt revocation.
        let action = if Path::new(&helper).exists() {
            "prepare-decommission"
        } else {
            "prepare-decommission-readonly"
        };
        let output = Command::new("python3")
            .args(["-c", &code, &helper, action, command_id])
            .env("NODE_PLANE_AGENT_INSTANCE_ID", &self.instance_id)
            .output()
            .map_err(|_| Status::failed_precondition("decommission helper unavailable"))?;
        if !output.status.success() {
            // This helper only validates journal/fence state. Preserve its
            // reason in server diagnostics instead of claiming transport loss.
            let reason = String::from_utf8_lossy(&output.stderr);
            return Err(Status::failed_precondition(format!(
                "profile revocations are not confirmed; decommission refused: {}",
                reason.chars().take(2048).collect::<String>().trim()
            )));
        }
        Ok(())
    }

    pub(crate) fn verify_prepared_decommission(&self, command_id: &str) -> Result<(), Status> {
        let marker = fs::read("/etc/node-plane/profile-intents.sqlite3.disabled")
            .map_err(|_| Status::failed_precondition("decommission identity is not prepared"))?;
        let record: Value = serde_json::from_slice(&marker)
            .map_err(|_| Status::failed_precondition("decommission identity is not prepared"))?;
        if record != serde_json::json!({"kind": "decommission", "command_id": command_id}) {
            return Err(Status::failed_precondition(
                "decommission identity is not prepared",
            ));
        }
        Ok(())
    }

    pub(crate) fn guard_prepared_decommission(&self, command_id: &str) -> Result<fs::File, Status> {
        let lock = fs::OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .mode(0o600)
            .open("/etc/node-plane/profile-intents.sqlite3.lock")
            .map_err(|_| Status::failed_precondition("profile fence lock unavailable"))?;
        lock.lock()
            .map_err(|_| Status::failed_precondition("profile fence lock unavailable"))?;
        // Runtime cleanup removes the helper itself. The durable marker lives
        // outside runtime_root and remains readable until agent uninstall.
        self.verify_prepared_decommission(command_id)?;
        Ok(lock)
    }
}
