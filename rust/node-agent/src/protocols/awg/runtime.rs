//! AmneziaWG runtime: init, deploy and entropy through the runtime scripts.
use tonic::Status;

use crate::agent::v1::RuntimeCommandResponse;
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn init_awg(&self) -> Result<RuntimeCommandResponse, Status> {
        let summary = self.run_runtime_command("init-awg.sh", &[])?;
        Ok(RuntimeCommandResponse {
            summary,
            payload_json: String::new(),
        })
    }

    pub(crate) fn deploy_awg(&self) -> Result<RuntimeCommandResponse, Status> {
        let summary = self.run_runtime_command("deploy-awg.sh", &[])?;
        Ok(RuntimeCommandResponse {
            summary,
            payload_json: String::new(),
        })
    }

    pub(crate) fn regenerate_awg_entropy(&self) -> Result<RuntimeCommandResponse, Status> {
        let summary = self.run_runtime_command("regenerate-awg-entropy.sh", &[])?;
        Ok(RuntimeCommandResponse {
            summary,
            payload_json: String::new(),
        })
    }
}
