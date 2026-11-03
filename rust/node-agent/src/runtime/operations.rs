//! Backend node operations and their progress.
use std::fs;
use std::path::Path;

use serde_json::Value;
use tonic::Status;

use crate::agent::v1::{BackendNodeActionRequest, RuntimeCommandResponse};
use crate::host::metrics as host_metrics;
use crate::state::AgentState;

impl AgentState {
    pub(crate) async fn backend_node_action(
        &self,
        req: BackendNodeActionRequest,
    ) -> Result<RuntimeCommandResponse, Status> {
        if !req.recover
            && !req.action.starts_with("resolve_")
            && !matches!(
                req.action.as_str(),
                "inspect" | "host_metrics" | "traffic" | "operation_progress"
            )
        {
            self.check_force_dispatch_fence(&req.command_id)?;
        }
        if req.node_key != self.config.node_key {
            return Err(Status::failed_precondition("agent node identity mismatch"));
        }
        if req.action == "host_metrics" {
            let mut metrics = host_metrics::resources();
            if let (Some(resources), Some(cpu)) =
                (metrics.as_object_mut(), self.cpu_metrics().as_object())
            {
                resources.extend(cpu.iter().map(|(key, value)| (key.clone(), value.clone())));
            }
            return Ok(RuntimeCommandResponse {
                summary: "host resource observation".into(),
                payload_json:
                    serde_json::json!({"node_key": self.config.node_key, "host_metrics": metrics})
                        .to_string(),
            });
        }
        if req.action == "operation_progress" {
            if !req.recover {
                return Err(Status::invalid_argument("progress is read-only"));
            }
            let value = read_operation_progress(
                Path::new("/etc/node-plane/operation-progress"),
                &req.command_id,
            );
            return Ok(RuntimeCommandResponse {
                summary: "node operation observation".into(),
                payload_json: value.to_string(),
            });
        }
        let inspect = req.action == "inspect";
        let sync_runtime = req.action == "sync_runtime";
        let state = self.clone();
        let output = tokio::task::spawn_blocking(move || {
            state.run_runtime_command(
                "backend-node-operation.py",
                &[
                    req.action,
                    req.command_id,
                    req.intent_json,
                    req.recover.to_string(),
                ],
            )
        })
        .await
        .map_err(|_| Status::internal("node operation worker unavailable"))?
        .map_err(|status| {
            owner_conflict(status.message())
                .unwrap_or_else(|| Status::failed_precondition("node operation needs attention"))
        })?;
        let mut value: Value = serde_json::from_str(&output)
            .map_err(|_| Status::internal("invalid node operation response"))?;
        if inspect
            && let (Some(metrics), Some(cpu)) = (
                value.get_mut("host_metrics").and_then(Value::as_object_mut),
                self.cpu_metrics().as_object(),
            )
        {
            metrics.extend(cpu.iter().map(|(key, value)| (key.clone(), value.clone())));
        }
        if inspect {
            // Unknown (null) when Docker cannot be inspected.
            value["images"] = self
                .container_images()
                .ok()
                .and_then(|images| serde_json::to_value(images).ok())
                .unwrap_or(Value::Null);
        }
        if sync_runtime {
            let state = self.clone();
            value["images"] = tokio::task::spawn_blocking(move || state.refresh_container_images())
                .await
                .map_err(|_| Status::internal("node operation worker unavailable"))??;
        }
        Ok(RuntimeCommandResponse {
            summary: "backend node operation completed".into(),
            payload_json: value.to_string(),
        })
    }
}

/// The script refused a VPN runtime of another node before changing
/// anything; this outcome is certain, unlike other failures.
fn owner_conflict(message: &str) -> Option<Status> {
    let (_, owner) = message.split_once("failed: runtime_owner_conflict: ")?;
    let owner: String = owner
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
        .take(64)
        .collect();
    Some(Status::failed_precondition(format!(
        "runtime_owner_conflict: {}",
        if owner.is_empty() { "unknown" } else { &owner }
    )))
}

pub(crate) fn read_operation_progress(directory: &Path, command_id: &str) -> Value {
    use std::io::Read;
    if command_id.len() != 36
        || !command_id.bytes().enumerate().all(|(index, byte)| {
            if matches!(index, 8 | 13 | 18 | 23) {
                byte == b'-'
            } else {
                byte.is_ascii_hexdigit()
            }
        })
    {
        return Value::Null;
    }
    let path = directory.join(format!("{command_id}.json"));
    let Ok(file) = fs::File::open(path) else {
        return Value::Null;
    };
    let mut raw = String::new();
    if file.take(2049).read_to_string(&mut raw).is_err() || raw.len() > 2048 {
        return Value::Null;
    }
    let Ok(value) = serde_json::from_str::<Value>(&raw) else {
        return Value::Null;
    };
    if value["operation_id"].as_str() != Some(command_id) {
        return Value::Null;
    }
    let Some(stage) = value["stage"].as_str() else {
        return Value::Null;
    };
    serde_json::json!({"operation_id": command_id, "stage": stage})
}

#[cfg(test)]
mod tests {
    #[test]
    fn only_an_owner_refusal_keeps_its_reason() {
        let status = super::owner_conflict(
            "backend-node-operation.py failed: runtime_owner_conflict: ger2\n",
        )
        .unwrap();
        assert_eq!(status.message(), "runtime_owner_conflict: ger2");
        assert!(super::owner_conflict("backend-node-operation.py failed: boom").is_none());
    }

    #[test]
    fn progress_lookup_is_bounded_read_only_and_checks_command_identity() {
        let directory = std::env::temp_dir().join(format!(
            "node-progress-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&directory).unwrap();
        let identity = "a65d2b73-f47a-42af-90aa-b6d4dccbf904".to_string();
        let path = directory.join(format!("{identity}.json"));
        let raw = serde_json::json!({"operation_id": identity, "stage":"awg_init"}).to_string();
        std::fs::write(&path, &raw).unwrap();
        assert_eq!(
            super::read_operation_progress(&directory, &identity)["stage"],
            "awg_init"
        );
        assert_eq!(std::fs::read_to_string(&path).unwrap(), raw);
        assert!(super::read_operation_progress(&directory, "../escape").is_null());
        std::fs::write(
            &path,
            serde_json::json!({"operation_id":"other", "stage":"awg_init"}).to_string(),
        )
        .unwrap();
        assert!(super::read_operation_progress(&directory, &identity).is_null());
        std::fs::write(&path, "x".repeat(4096)).unwrap();
        assert!(super::read_operation_progress(&directory, &identity).is_null());
        std::fs::remove_dir_all(directory).unwrap();
    }
}
