//! Xray runtime: init, deploy, settings sync and public metadata.
use std::process::Command;

use serde_json::Value;
use tonic::Status;

use crate::agent;
use crate::agent::v1::{
    InitXrayRequest, InitXrayResponse, RuntimeCommandResponse, SyncXrayRequest, SyncXrayResponse,
};
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn sync_xray(&self, request: SyncXrayRequest) -> Result<SyncXrayResponse, Status> {
        let config_path = self.resolve_runtime_path(&request.config_path);
        let generated_json = self.run_runtime_command(
            "sync-xray.sh",
            &[
                config_path,
                request.public_host,
                request.flow,
                request.image,
            ],
        )?;
        Ok(SyncXrayResponse {
            summary: "xray settings synced".to_string(),
            generated_json,
        })
    }

    pub(crate) fn init_xray(&self, request: InitXrayRequest) -> Result<InitXrayResponse, Status> {
        let config_path = self.resolve_runtime_path(&request.config_path);
        let output = self.run_runtime_command(
            "init-xray.sh",
            &[
                config_path,
                request.public_host,
                request.sni_host,
                request.tcp_port.to_string(),
                request.xhttp_port.to_string(),
                request.xhttp_path_prefix,
                request.flow,
                request.image,
            ],
        )?;
        Ok(InitXrayResponse {
            summary: "xray initialized".to_string(),
            generated_json: output,
        })
    }

    pub(crate) fn deploy_xray(&self) -> Result<RuntimeCommandResponse, Status> {
        let summary = self.run_runtime_command("deploy-xray.sh", &[])?;
        Ok(RuntimeCommandResponse {
            summary,
            payload_json: String::new(),
        })
    }

    pub(crate) fn get_backend_xray_public(&self) -> Result<agent::v1::BackendXrayPublic, Status> {
        let script = self.resolve_runtime_path("/opt/node-plane-runtime/sync-xray.sh");
        let config = self.node_env_value("XRAY_CONFIG", "/opt/node-plane-runtime/xray/config.json");
        let image = self
            .node_env_value("XRAY_DOCKER_IMAGE", "ghcr.io/saharoktyan/xray-core:v26.10.31-np.2@sha256:43c919899d9a7daf94f988552d65e98fb8abfcc7b804fb39f94a3812755c08c0");
        let output = Command::new(script)
            .args([
                config,
                "localhost".to_string(),
                "xtls-rprx-vision".to_string(),
                image,
            ])
            .output()
            .map_err(|_| Status::failed_precondition("Xray public metadata is unavailable"))?;
        if !output.status.success() {
            return Err(Status::failed_precondition(
                "Xray public metadata is unavailable",
            ));
        }
        let mut value: Value = serde_json::from_slice(&output.stdout)
            .map_err(|_| Status::internal("invalid Xray public metadata"))?;
        value["xray_fp"] = serde_json::json!(self.node_env_value("XRAY_FP", "chrome"));
        // The helper returns public identity only. Never return the private
        // server key even if a future helper accidentally emits it.
        let allowed = [
            "xray_sni",
            "xray_pbk",
            "xray_sid",
            "xray_short_id",
            "xray_tcp_port",
            "xray_xhttp_port",
            "xray_xhttp_path_prefix",
            "xray_flow",
            "xray_fp",
        ];
        let mut public = serde_json::Map::new();
        for key in allowed {
            let field = value
                .get(key)
                .ok_or_else(|| Status::internal("incomplete Xray public metadata"))?;
            public.insert(key.to_string(), field.clone());
        }
        Ok(agent::v1::BackendXrayPublic {
            metadata_json: Value::Object(public).to_string(),
        })
    }
}
