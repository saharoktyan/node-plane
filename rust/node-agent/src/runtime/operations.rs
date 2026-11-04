//! Backend node operations and their progress.
use std::fs;
use std::path::Path;

use serde_json::Value;
use tonic::Status;

use crate::agent::v1::{BackendNodeActionRequest, RuntimeCommandResponse};
use crate::host::metrics as host_metrics;
use crate::intents::leases::System;
use crate::intents::operation::{self, Node};
use crate::protocols::traffic;
use crate::protocols::xray::revocation;
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
                "inspect" | "host_metrics" | "traffic" | "traffic_batch" | "operation_progress"
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
                &self.journal().with_file_name("operation-progress"),
                &req.command_id,
            );
            return Ok(RuntimeCommandResponse {
                summary: "node operation observation".into(),
                payload_json: value.to_string(),
            });
        }
        if req.action == "traffic" || req.action == "traffic_batch" {
            if req.recover {
                return Err(Status::failed_precondition(
                    "node operation needs attention",
                ));
            }
            let setting = |key: &str, default: &str| self.node_env_value(key, default);
            let journal = self.journal();
            let attention = |_| Status::failed_precondition("node operation needs attention");
            let payload = if req.action == "traffic" {
                serde_json::to_string(
                    &traffic::observe(&setting, &journal, &req.intent_json)
                        .await
                        .map_err(attention)?,
                )
            } else {
                serde_json::to_string(
                    &traffic::observe_batch(&setting, &journal, &req.intent_json)
                        .await
                        .map_err(attention)?,
                )
            };
            return Ok(RuntimeCommandResponse {
                summary: "backend node operation completed".into(),
                payload_json: payload
                    .map_err(|_| Status::internal("invalid node operation response"))?,
            });
        }
        let inspect = req.action == "inspect";
        let sync_runtime = req.action == "sync_runtime";
        let (action, recover, intent_json) =
            (req.action.clone(), req.recover, req.intent_json.clone());
        let state = self.clone();
        let output = tokio::task::spawn_blocking(move || state.node_operation(&req))
            .await
            .map_err(|_| Status::internal("node operation worker unavailable"))??;
        let mut value = output;
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
        revocation::refine(&action, recover, &intent_json, &mut value).await;
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

impl AgentState {
    /// A journaled node operation, a read of the node, or temporary access.
    fn node_operation(&self, req: &BackendNodeActionRequest) -> Result<Value, Status> {
        let runtime = self.node_runtime();
        let journal = self.journal();
        let attention = |err: String| {
            eprintln!(
                "node operation {} {} failed: {err}",
                req.action, req.command_id
            );
            Status::failed_precondition("node operation needs attention")
        };
        if req.action == "inspect" {
            return Ok(operation::inspect(&System, &runtime));
        }
        let intent: Value = serde_json::from_str(&req.intent_json)
            .map_err(|_| Status::invalid_argument("invalid node operation intent"))?;
        if req.action.starts_with("temporary_") {
            let mut node = Node {
                host: &System,
                runtime: &runtime,
            };
            return operation::temporary(
                &journal,
                &self.instance_id,
                &req.action,
                &req.command_id,
                &intent,
                req.recover,
                &mut node,
            )
            .map_err(attention);
        }
        operation::command(
            &journal,
            &runtime.env,
            &self.instance_id,
            &req.action,
            &req.command_id,
            &intent,
            req.recover,
            &mut |action, intent, lock, progress| {
                operation::run(&System, &runtime, action, intent, lock, progress)
            },
        )
        .map_err(|err| match err {
            // Refused before any change: a certain outcome, unlike others.
            operation::Error::OwnerConflict(owner) => {
                Status::failed_precondition(format!("runtime_owner_conflict: {owner}"))
            }
            operation::Error::Failed(err) => attention(err),
        })
    }
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
