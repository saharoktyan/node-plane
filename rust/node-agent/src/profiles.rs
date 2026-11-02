//! VPN profile intents, protocol-independent: apply, recover, resolve, inspect.
use serde_json::Value;
use tonic::Status;

use crate::agent;
use crate::agent::v1::{ApplyProfileIntentRequest, InspectProfileRequest, RuntimeCommandResponse};
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn resolve_profile_intent(
        &self,
        req: ApplyProfileIntentRequest,
    ) -> Result<agent::v1::ProfileInspection, Status> {
        let intent = serde_json::json!({
            "command_id": req.command_id, "protocol": req.protocol_kind,
            "runtime_name": req.profile_name, "revision": req.desired_revision,
            "action": req.action, "uuid": req.uuid, "short_id": req.short_id,
        });
        let output = self
            .run_runtime_command(
                "apply-profile-intent.py",
                &["resolve".to_string(), intent.to_string()],
            )
            .map_err(|_| {
                Status::failed_precondition(
                    "profile repair requires a restarted agent and live inspection",
                )
            })?;
        let value: Value = serde_json::from_str(&output)
            .map_err(|_| Status::internal("invalid repair response"))?;
        let observation = &value["observation"];
        let boolean = |key: &str| {
            observation
                .get(key)
                .and_then(Value::as_bool)
                .ok_or_else(|| Status::internal("invalid repair observation"))
        };
        Ok(agent::v1::ProfileInspection {
            disk_present: boolean("disk_present")?,
            live_present: boolean("live_present")?,
            identity_matches: boolean("identity_matches")?,
            config_available: boolean("config_available")?,
        })
    }

    pub(crate) fn recover_profile_intent(
        &self,
        req: ApplyProfileIntentRequest,
    ) -> Result<RuntimeCommandResponse, Status> {
        let intent = serde_json::json!({
            "command_id": req.command_id, "protocol": req.protocol_kind,
            "runtime_name": req.profile_name, "revision": req.desired_revision,
            "action": req.action, "uuid": req.uuid, "short_id": req.short_id,
        });
        let output = self
            .run_runtime_command(
                "apply-profile-intent.py",
                &["lookup".to_string(), intent.to_string()],
            )
            .map_err(|_| {
                Status::failed_precondition("profile intent blocked; reconciliation required")
            })?;
        let value: Value = serde_json::from_str(&output)
            .map_err(|_| Status::internal("invalid intent response"))?;
        let summary = value["summary"]
            .as_str()
            .ok_or_else(|| Status::internal("invalid intent response"))?;
        let payload = if intent["protocol"] == "awg" && intent["action"] == "ensure" {
            AgentState::extract_awg_payload_json(summary)
        } else {
            String::new()
        };
        Ok(RuntimeCommandResponse {
            summary: "profile intent applied".to_string(),
            payload_json: payload,
        })
    }

    pub(crate) fn apply_profile_intent(
        &self,
        req: ApplyProfileIntentRequest,
    ) -> Result<RuntimeCommandResponse, Status> {
        self.check_force_dispatch_fence(&req.command_id)?;
        let intent = serde_json::json!({
            "command_id": req.command_id, "protocol": req.protocol_kind,
            "runtime_name": req.profile_name, "revision": req.desired_revision,
            "action": req.action, "uuid": req.uuid, "short_id": req.short_id,
        });
        let output = self
            .run_runtime_command("apply-profile-intent.py", &[intent.to_string()])
            .map_err(|_| {
                Status::failed_precondition("profile intent blocked; reconciliation required")
            })?;
        let value: Value = serde_json::from_str(&output)
            .map_err(|_| Status::internal("invalid intent response"))?;
        let summary = value["summary"]
            .as_str()
            .ok_or_else(|| Status::internal("invalid intent response"))?;
        let payload = if intent["protocol"] == "awg" && intent["action"] == "ensure" {
            AgentState::extract_awg_payload_json(summary)
        } else {
            String::new()
        };
        Ok(RuntimeCommandResponse {
            summary: "profile intent applied".to_string(),
            payload_json: payload,
        })
    }

    pub(crate) fn inspect_profile(
        &self,
        req: InspectProfileRequest,
    ) -> Result<agent::v1::ProfileInspection, Status> {
        let valid_name = !req.profile_name.is_empty()
            && req.profile_name.len() <= 64
            && req
                .profile_name
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c));
        if !valid_name || !matches!(req.protocol_kind.as_str(), "awg" | "xray") {
            return Err(Status::invalid_argument("invalid inspection target"));
        }
        let mut args = vec![req.protocol_kind.clone(), req.profile_name];
        if req.protocol_kind == "xray" {
            if !req.expected_xray_uuid.is_empty()
                && !(req.expected_xray_uuid.len() == 36
                    && req.expected_xray_uuid.bytes().enumerate().all(|(i, c)| {
                        if matches!(i, 8 | 13 | 18 | 23) {
                            c == b'-'
                        } else {
                            c.is_ascii_hexdigit()
                        }
                    }))
            {
                return Err(Status::invalid_argument("xray identity is required"));
            }
            args.push(req.expected_xray_uuid);
        }
        let output = self
            .run_runtime_command("inspect-profile.sh", &args)
            .map_err(|_| Status::failed_precondition("live profile inspection unavailable"))?;
        let value: Value = serde_json::from_str(&output)
            .map_err(|_| Status::internal("invalid inspection response"))?;
        let boolean = |key: &str| {
            value
                .get(key)
                .and_then(Value::as_bool)
                .ok_or_else(|| Status::internal("invalid inspection response"))
        };
        Ok(agent::v1::ProfileInspection {
            disk_present: boolean("disk_present")?,
            live_present: boolean("live_present")?,
            identity_matches: boolean("identity_matches")?,
            config_available: boolean("config_available")?,
        })
    }
}
