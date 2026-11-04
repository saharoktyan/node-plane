//! AmneziaWG runtime: init, deploy, entropy and client config refresh.
use std::io::Write;
use std::process::{Command, Stdio};

use tonic::Status;

use crate::agent::v1::{RefreshAwgConfigRequest, RefreshAwgConfigResponse, RuntimeCommandResponse};
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn refresh_awg_config(
        &self,
        request: RefreshAwgConfigRequest,
    ) -> Result<RefreshAwgConfigResponse, Status> {
        let script = self.resolve_runtime_path("/opt/node-plane-runtime/refresh-awg-config.py");
        let mut child = Command::new("bash")
            .arg("-c")
            .arg("set -a; source \"$1\"; exec python3 \"$2\" \"$3\"")
            .arg("refresh-awg")
            .arg(&self.config.node_env_path)
            .arg(script)
            .arg(&request.profile_name)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|err| Status::internal(format!("failed to start AWG refresh: {err}")))?;
        child
            .stdin
            .take()
            .ok_or_else(|| Status::internal("AWG refresh stdin missing"))?
            .write_all(request.wg_conf.as_bytes())
            .map_err(|err| Status::internal(format!("failed to pass AWG config: {err}")))?;
        let output = child
            .wait_with_output()
            .map_err(|err| Status::internal(format!("AWG refresh failed: {err}")))?;
        if !output.status.success() {
            return Err(Status::failed_precondition(format!(
                "AWG refresh failed: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            )));
        }
        let value: serde_json::Value = serde_json::from_slice(&output.stdout)
            .map_err(|err| Status::internal(format!("AWG refresh returned invalid JSON: {err}")))?;
        Ok(RefreshAwgConfigResponse {
            wg_conf: value["wg_conf"].as_str().unwrap_or_default().to_string(),
            vpn_key: value["vpn_key"].as_str().unwrap_or_default().to_string(),
        })
    }

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
