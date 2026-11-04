//! VPN profile intents, protocol-independent: apply, recover, resolve, inspect.
use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::Value;
use tonic::Status;

use crate::agent;
use crate::agent::v1::{ApplyProfileIntentRequest, RuntimeCommandResponse};
use crate::intents::Response;
use crate::intents::profile::{self, Intent};
use crate::state::AgentState;

fn intent(req: &ApplyProfileIntentRequest) -> Intent {
    Intent {
        command_id: req.command_id.clone(),
        protocol: req.protocol_kind.clone(),
        runtime_name: req.profile_name.clone(),
        revision: i64::try_from(req.desired_revision).unwrap_or(0),
        action: req.action.clone(),
        uuid: req.uuid.clone(),
        short_id: req.short_id.clone(),
        lease_seconds: None,
    }
}

/// What `inspect-profile.sh` sees of the profile on disk and live.
fn inspect(runtime: &Path, intent: &Intent) -> Result<Value, String> {
    let mut command = Command::new(runtime.join("inspect-profile.sh"));
    command.args([&intent.protocol, &intent.runtime_name]);
    if intent.protocol == "xray" {
        // A deletion carries no identity: the repair compares presence only.
        command.arg(&intent.uuid);
    }
    let output = command
        .output()
        .map_err(|err| format!("inspect-profile.sh failed: {err}"))?;
    if !output.status.success() {
        return Err("live inspection unavailable".into());
    }
    serde_json::from_slice(&output.stdout).map_err(|_| "live inspection unavailable".into())
}

impl AgentState {
    fn runtime(&self) -> PathBuf {
        self.resolve_runtime_path("/opt/node-plane-runtime").into()
    }

    /// The reply to a confirmed profile intent; an AWG issuance carries the
    /// client config.
    fn profile_response(intent: &Intent, response: &Response) -> RuntimeCommandResponse {
        let payload = if intent.protocol == "awg" && intent.action == "ensure" {
            AgentState::extract_awg_payload_json(&response.summary)
        } else {
            String::new()
        };
        RuntimeCommandResponse {
            summary: "profile intent applied".to_string(),
            payload_json: payload,
        }
    }

    pub(crate) fn resolve_profile_intent(
        &self,
        req: ApplyProfileIntentRequest,
    ) -> Result<agent::v1::ProfileInspection, Status> {
        let intent = intent(&req);
        let runtime = self.runtime();
        let record = profile::resolve_interrupted(
            &intent,
            &self.journal(),
            &self.instance_id,
            &mut |intent| inspect(&runtime, intent),
        )
        .map_err(|_| {
            Status::failed_precondition(
                "profile repair requires a restarted agent and live inspection",
            )
        })?;
        let observation = &record["observation"];
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
        let intent = intent(&req);
        let response = profile::lookup(&intent, &self.journal()).map_err(|_| {
            Status::failed_precondition("profile intent blocked; reconciliation required")
        })?;
        Ok(Self::profile_response(&intent, &response))
    }

    pub(crate) fn apply_profile_intent(
        &self,
        req: ApplyProfileIntentRequest,
    ) -> Result<RuntimeCommandResponse, Status> {
        self.check_force_dispatch_fence(&req.command_id)?;
        let intent = intent(&req);
        let runtime = self.runtime();
        let response = profile::apply(
            &intent,
            &self.journal(),
            &self.instance_id,
            &mut |intent, lock| profile::run(intent, lock, &runtime, &self.instance_id),
        )
        .map_err(|err| {
            eprintln!("profile intent {} failed: {err}", intent.command_id);
            Status::failed_precondition("profile intent blocked; reconciliation required")
        })?;
        Ok(Self::profile_response(&intent, &response))
    }
}
