//! Xray identities.
use tonic::Status;

use crate::agent::v1::{AddXrayUserRequest, DeleteProfileRequest, RuntimeCommandResponse};
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn add_xray_user(
        &self,
        request: AddXrayUserRequest,
    ) -> Result<RuntimeCommandResponse, Status> {
        if request.profile_name.trim().is_empty() || request.uuid.trim().is_empty() {
            return Err(Status::invalid_argument(
                "profile_name and uuid are required",
            ));
        }
        let mut args = vec![request.profile_name, request.uuid];
        if !request.short_id.trim().is_empty() {
            args.push(request.short_id);
        }
        let summary = self.run_runtime_command("xray-add-user-existing.sh", &args)?;
        Ok(RuntimeCommandResponse {
            summary,
            payload_json: String::new(),
        })
    }

    pub(crate) fn delete_xray_user(
        &self,
        request: DeleteProfileRequest,
    ) -> Result<RuntimeCommandResponse, Status> {
        if request.profile_name.trim().is_empty() {
            return Err(Status::invalid_argument("profile_name is required"));
        }
        let summary = self.run_runtime_command("xray-del-user.sh", &[request.profile_name])?;
        Ok(RuntimeCommandResponse {
            summary,
            payload_json: String::new(),
        })
    }
}
