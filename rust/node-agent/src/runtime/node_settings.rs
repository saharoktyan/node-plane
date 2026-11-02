//! Node settings: legacy script path and the backend intent path.
use std::process::Command;

use serde_json::Value;
use tonic::Status;

use crate::agent;
use crate::agent::v1::{
    ApplyBackendNodeSettingsRequest, ApplyNodeSettingsRequest, RuntimeCommandResponse,
};
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn apply_node_settings(
        &self,
        request: ApplyNodeSettingsRequest,
    ) -> Result<RuntimeCommandResponse, Status> {
        self.run_runtime_command(
            "apply-node-settings.sh",
            &[
                self.resolve_runtime_path(&request.xray_config_path),
                request.xray_sni,
                request.xray_tcp_port.to_string(),
                request.xray_xhttp_port.to_string(),
                request.xray_xhttp_path_prefix,
                request.apply_xray.to_string(),
                request.apply_awg.to_string(),
            ],
        )
        .map(|summary| RuntimeCommandResponse {
            summary,
            payload_json: String::new(),
        })
    }

    pub(crate) fn apply_backend_node_settings(
        &self,
        req: ApplyBackendNodeSettingsRequest,
    ) -> Result<RuntimeCommandResponse, Status> {
        self.check_force_dispatch_fence(&req.command_id)?;
        if req.node_key != self.config.node_key || req.desired_revision == 0 {
            return Err(Status::failed_precondition(
                "node identity or revision mismatch",
            ));
        }
        let intent = backend_settings_intent(&req)?;
        let output = self
            .run_runtime_command(
                "apply-profile-intent.py",
                &["apply-node-settings".to_string(), intent.to_string()],
            )
            .map_err(|_| {
                Status::failed_precondition("node settings intent blocked; reconciliation required")
            })?;
        let value: Value = serde_json::from_str(&output)
            .map_err(|_| Status::internal("invalid node settings response"))?;
        let payload = value
            .get("payload_json")
            .and_then(Value::as_str)
            .ok_or_else(|| Status::internal("invalid node settings response"))?;
        Ok(RuntimeCommandResponse {
            summary: "node settings applied".to_string(),
            payload_json: payload.to_string(),
        })
    }

    pub(crate) fn recover_backend_node_settings(
        &self,
        req: ApplyBackendNodeSettingsRequest,
    ) -> Result<RuntimeCommandResponse, Status> {
        if req.node_key != self.config.node_key || req.desired_revision == 0 {
            return Err(Status::failed_precondition(
                "node identity or revision mismatch",
            ));
        }
        let intent = backend_settings_intent(&req)?;
        let output = self
            .run_runtime_command(
                "apply-profile-intent.py",
                &["lookup-node-settings".to_string(), intent.to_string()],
            )
            .map_err(|_| Status::failed_precondition("node settings success is not confirmed"))?;
        let value: Value = serde_json::from_str(&output)
            .map_err(|_| Status::internal("invalid node settings response"))?;
        let payload = value
            .get("payload_json")
            .and_then(Value::as_str)
            .ok_or_else(|| Status::internal("invalid node settings response"))?;
        Ok(RuntimeCommandResponse {
            summary: "node settings confirmed".to_string(),
            payload_json: payload.to_string(),
        })
    }

    pub(crate) fn resolve_backend_node_settings(
        &self,
        req: ApplyBackendNodeSettingsRequest,
    ) -> Result<RuntimeCommandResponse, Status> {
        if req.node_key != self.config.node_key || req.desired_revision == 0 {
            return Err(Status::failed_precondition(
                "node identity or revision mismatch",
            ));
        }
        let intent = backend_settings_intent(&req)?;
        // Recovery must use the current agent's journal implementation even
        // when an agent-only node has no deployed VPN runtime/helper at all.
        let helper = self.resolve_runtime_path("/opt/node-plane-runtime/apply-profile-intent.py");
        let code = format!(
            "import sys\n__file__ = sys.argv[1]\nsys.argv = sys.argv[1:]\n{}",
            include_str!("../../../../runtime_assets/apply-profile-intent.py")
        );
        let output = Command::new("python3")
            .args([
                "-c",
                &code,
                &helper,
                "resolve-node-settings",
                &intent.to_string(),
            ])
            .env("NODE_PLANE_AGENT_INSTANCE_ID", &self.instance_id)
            .output()
            .map_err(|_| {
                Status::failed_precondition("node settings recovery helper unavailable")
            })?;
        if !output.status.success() {
            return Err(Status::failed_precondition(
                "node settings journal retirement was not confirmed",
            ));
        }
        let value: Value = serde_json::from_slice(&output.stdout)
            .map_err(|_| Status::internal("invalid node settings repair response"))?;
        let observation = value
            .get("observation")
            .ok_or_else(|| Status::internal("invalid node settings repair observation"))?;
        for key in ["config_matches", "containers_running"] {
            if observation.get(key).and_then(Value::as_bool).is_none() {
                return Err(Status::internal("invalid node settings repair observation"));
            }
        }
        Ok(RuntimeCommandResponse {
            summary: "interrupted node settings command retired".to_string(),
            payload_json: observation.to_string(),
        })
    }
}

// New commands carry an explicit protocol plan. Legacy array requests retain
// their exact journal fingerprints for read-only recovery. Older agents reject
// the new object before invoking runtime mutations instead of inferring cleanup.
pub(crate) fn backend_settings_intent(
    req: &agent::v1::ApplyBackendNodeSettingsRequest,
) -> Result<Value, Status> {
    let parsed: Value = serde_json::from_str(&req.protocols_json)
        .map_err(|_| Status::invalid_argument("invalid protocols"))?;
    let (protocols, plan) = if parsed.is_array() {
        (parsed, None)
    } else if let Some(object) = parsed.as_object() {
        if object.len() != 2
            || !object.contains_key("protocols")
            || !object.contains_key("protocol_plan")
        {
            return Err(Status::invalid_argument("invalid protocol plan"));
        }
        (
            object["protocols"].clone(),
            Some(object["protocol_plan"].clone()),
        )
    } else {
        return Err(Status::invalid_argument("invalid protocol plan"));
    };
    let mut intent = serde_json::json!({
        "kind": "node_settings", "node_key": req.node_key,
        "command_id": req.command_id, "revision": req.desired_revision,
        "protocols": protocols,
        "settings": serde_json::from_str::<Value>(&req.settings_json)
            .map_err(|_| Status::invalid_argument("invalid settings"))?,
    });
    if let Some(plan) = plan {
        intent["protocol_plan"] = plan;
    }
    Ok(intent)
}

#[cfg(test)]
mod tests {
    #[test]
    fn settings_plan_is_explicit_and_legacy_recovery_keeps_its_shape() {
        use super::{agent, backend_settings_intent};
        let mut request = agent::v1::ApplyBackendNodeSettingsRequest {
            command_id: "command".into(),
            node_key: "de1".into(),
            desired_revision: 1,
            protocols_json: "[\"awg\"]".into(),
            settings_json: "{}".into(),
        };
        let legacy = backend_settings_intent(&request).unwrap();
        assert_eq!(legacy["protocols"], serde_json::json!(["awg"]));
        assert!(legacy.get("protocol_plan").is_none());
        request.protocols_json = serde_json::json!({"protocols": [],
            "protocol_plan": {"remove_protocols": ["awg"]}})
        .to_string();
        let planned = backend_settings_intent(&request).unwrap();
        assert_eq!(planned["protocols"], serde_json::json!([]));
        assert_eq!(
            planned["protocol_plan"]["remove_protocols"],
            serde_json::json!(["awg"])
        );
        request.protocols_json = "{\"protocols\":[]}".into();
        assert!(backend_settings_intent(&request).is_err());
    }
}
