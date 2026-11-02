//! AmneziaWG peers.
use tonic::Status;

use crate::agent::v1::{AddAwgUserRequest, DeleteProfileRequest, RuntimeCommandResponse};
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn add_awg_user(
        &self,
        request: AddAwgUserRequest,
    ) -> Result<RuntimeCommandResponse, Status> {
        if request.profile_name.trim().is_empty() {
            return Err(Status::invalid_argument("profile_name is required"));
        }
        let summary = self.run_runtime_command("awg-add-user.sh", &[request.profile_name])?;
        Ok(RuntimeCommandResponse {
            payload_json: Self::extract_awg_payload_json(summary.as_str()),
            summary: "AWG profile configured".to_string(),
        })
    }

    pub(crate) fn delete_awg_user(
        &self,
        request: DeleteProfileRequest,
    ) -> Result<RuntimeCommandResponse, Status> {
        if request.profile_name.trim().is_empty() {
            return Err(Status::invalid_argument("profile_name is required"));
        }
        let summary = self.run_runtime_command("awg-del-user.sh", &[request.profile_name])?;
        Ok(RuntimeCommandResponse {
            summary,
            payload_json: String::new(),
        })
    }
}
