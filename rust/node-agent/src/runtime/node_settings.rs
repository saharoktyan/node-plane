//! Node settings: the legacy script path and the journaled intent path.
use serde_json::Value;
use tonic::Status;

use crate::agent;
use crate::agent::v1::{
    ApplyBackendNodeSettingsRequest, ApplyNodeSettingsRequest, RuntimeCommandResponse,
};
use crate::intents::leases::System;
use crate::intents::runtime::Runtime;
use crate::intents::{apply, settings, verify};
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

    /// The runtime the node commands manage.
    pub(crate) fn node_runtime(&self) -> Runtime {
        Runtime::system(
            self.resolve_runtime_path("/opt/node-plane-runtime").into(),
            self.config.node_env_path.clone().into(),
            &self.instance_id,
        )
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
        let runtime = self.node_runtime();
        let response = settings::apply(
            &intent,
            &self.journal(),
            &self.instance_id,
            &mut |settings, lock, progress| apply::run(&System, &runtime, settings, lock, progress),
        )
        .map_err(|err| {
            eprintln!("node settings {} failed: {err}", req.command_id);
            Status::failed_precondition("node settings intent blocked; reconciliation required")
        })?;
        Ok(RuntimeCommandResponse {
            summary: "node settings applied".to_string(),
            payload_json: response.payload_json,
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
        let response = settings::lookup(&intent, &self.journal())
            .map_err(|_| Status::failed_precondition("node settings success is not confirmed"))?;
        Ok(RuntimeCommandResponse {
            summary: "node settings confirmed".to_string(),
            payload_json: response.payload_json,
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
        // Works on an agent-only node without any VPN runtime.
        let runtime = self.node_runtime();
        let record = settings::resolve(
            &intent,
            &self.journal(),
            &self.instance_id,
            &mut |settings| verify::observe(&System, &runtime, settings),
        )
        .map_err(|_| {
            Status::failed_precondition("node settings journal retirement was not confirmed")
        })?;
        Ok(RuntimeCommandResponse {
            summary: "interrupted node settings command retired".to_string(),
            payload_json: record["observation"].to_string(),
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
