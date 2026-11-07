//! gRPC surface of the agent. Handlers only unpack requests and call modules.
use tonic::{Request, Response, Status};

use crate::agent;
use crate::agent::v1::node_agent_service_server::NodeAgentService;
use crate::agent::v1::{
    AddAwgUserRequest, AddXrayUserRequest, AgentEmpty, ApplyBackendNodeSettingsRequest,
    ApplyNodeSettingsRequest, CheckPortsRequest, CheckPortsResponse, DeleteProfileRequest,
    DeleteRuntimeRequest, DeleteRuntimeResponse, InitXrayRequest, InitXrayResponse,
    InstallDockerRequest, InstallDockerResponse, ListRemoteProfilesRequest,
    ListRemoteProfilesResponse, LocalHealth, OpenPortsRequest, OpenPortsResponse,
    PathExistsRequest, PathExistsResponse, RefreshAwgConfigRequest, RefreshAwgConfigResponse,
    RemoveAuthorizedKeyRequest, RemoveAuthorizedKeyResponse, RunDiagnosticsRequest,
    RunDiagnosticsResponse, RuntimeCommandResponse, RuntimeFacts, SyncNodeEnvRequest,
    SyncNodeEnvResponse, SyncRuntimeFilesRequest, SyncRuntimeFilesResponse, SyncXrayRequest,
    SyncXrayResponse,
};
use crate::state::AgentState;

#[derive(Clone)]
pub(crate) struct NodeAgentApi {
    pub(crate) state: AgentState,
}

#[tonic::async_trait]
impl NodeAgentService for NodeAgentApi {
    async fn force_recovery(
        &self,
        request: Request<agent::v1::ForceRecoveryRequest>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(
            self.state.force_recovery(request.into_inner())?,
        ))
    }

    async fn backend_node_action(
        &self,
        request: Request<agent::v1::BackendNodeActionRequest>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(
            self.state.backend_node_action(request.into_inner()).await?,
        ))
    }

    async fn get_runtime_facts(
        &self,
        _request: Request<AgentEmpty>,
    ) -> Result<Response<RuntimeFacts>, Status> {
        Ok(Response::new(self.state.runtime_facts()))
    }

    async fn get_node_health(
        &self,
        _request: Request<AgentEmpty>,
    ) -> Result<Response<LocalHealth>, Status> {
        Ok(Response::new(self.state.health()))
    }

    async fn resolve_profile_intent(
        &self,
        request: Request<agent::v1::ApplyProfileIntentRequest>,
    ) -> Result<Response<agent::v1::ProfileInspection>, Status> {
        Ok(Response::new(
            self.state.resolve_profile_intent(request.into_inner())?,
        ))
    }

    async fn recover_profile_intent(
        &self,
        request: Request<agent::v1::ApplyProfileIntentRequest>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(
            self.state.recover_profile_intent(request.into_inner())?,
        ))
    }

    async fn apply_profile_intent(
        &self,
        request: Request<agent::v1::ApplyProfileIntentRequest>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(
            self.state.apply_profile_intent(request.into_inner())?,
        ))
    }

    async fn inspect_profile(
        &self,
        request: Request<agent::v1::InspectProfileRequest>,
    ) -> Result<Response<agent::v1::ProfileInspection>, Status> {
        Ok(Response::new(
            self.state.inspect_profile(request.into_inner()).await?,
        ))
    }

    async fn list_remote_profiles(
        &self,
        request: Request<ListRemoteProfilesRequest>,
    ) -> Result<Response<ListRemoteProfilesResponse>, Status> {
        let protocol_kind = request.into_inner().protocol_kind.trim().to_lowercase();
        let mut items = Vec::new();
        if protocol_kind.is_empty() || protocol_kind == "awg" {
            items.extend(self.state.awg_profiles());
        }
        if protocol_kind.is_empty() || protocol_kind == "xray" {
            items.extend(self.state.xray_profiles());
        }
        Ok(Response::new(ListRemoteProfilesResponse { items }))
    }

    async fn run_diagnostics(
        &self,
        _request: Request<RunDiagnosticsRequest>,
    ) -> Result<Response<RunDiagnosticsResponse>, Status> {
        Ok(Response::new(self.state.diagnostics()))
    }

    async fn check_ports(
        &self,
        request: Request<CheckPortsRequest>,
    ) -> Result<Response<CheckPortsResponse>, Status> {
        Ok(Response::new(self.state.check_ports(request.into_inner())))
    }

    async fn sync_node_env(
        &self,
        request: Request<SyncNodeEnvRequest>,
    ) -> Result<Response<SyncNodeEnvResponse>, Status> {
        Ok(Response::new(
            self.state
                .sync_node_env(request.into_inner().content.as_str())?,
        ))
    }

    async fn open_ports(
        &self,
        request: Request<OpenPortsRequest>,
    ) -> Result<Response<OpenPortsResponse>, Status> {
        Ok(Response::new(self.state.open_ports(request.into_inner())?))
    }

    async fn sync_runtime_files(
        &self,
        request: Request<SyncRuntimeFilesRequest>,
    ) -> Result<Response<SyncRuntimeFilesResponse>, Status> {
        Ok(Response::new(
            self.state.sync_runtime_files(request.into_inner())?,
        ))
    }

    async fn sync_xray(
        &self,
        request: Request<SyncXrayRequest>,
    ) -> Result<Response<SyncXrayResponse>, Status> {
        Ok(Response::new(self.state.sync_xray(request.into_inner())?))
    }

    async fn apply_node_settings(
        &self,
        request: Request<ApplyNodeSettingsRequest>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(
            self.state.apply_node_settings(request.into_inner())?,
        ))
    }

    async fn apply_backend_node_settings(
        &self,
        request: Request<ApplyBackendNodeSettingsRequest>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(
            self.state
                .apply_backend_node_settings(request.into_inner())?,
        ))
    }

    async fn recover_backend_node_settings(
        &self,
        request: Request<ApplyBackendNodeSettingsRequest>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(
            self.state
                .recover_backend_node_settings(request.into_inner())?,
        ))
    }

    async fn resolve_backend_node_settings(
        &self,
        request: Request<ApplyBackendNodeSettingsRequest>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(
            self.state
                .resolve_backend_node_settings(request.into_inner())?,
        ))
    }

    async fn get_backend_xray_public(
        &self,
        _request: Request<AgentEmpty>,
    ) -> Result<Response<agent::v1::BackendXrayPublic>, Status> {
        Ok(Response::new(self.state.get_backend_xray_public()?))
    }

    async fn refresh_awg_config(
        &self,
        request: Request<RefreshAwgConfigRequest>,
    ) -> Result<Response<RefreshAwgConfigResponse>, Status> {
        Ok(Response::new(
            self.state.refresh_awg_config(request.into_inner())?,
        ))
    }

    async fn apply_admission_policy(
        &self,
        request: Request<agent::v1::ApplyAdmissionPolicyRequest>,
    ) -> Result<Response<agent::v1::ApplyAdmissionPolicyResponse>, Status> {
        Ok(Response::new(
            self.state.apply_admission_policy(request.into_inner())?,
        ))
    }

    async fn observe_sessions(
        &self,
        request: Request<agent::v1::ObserveSessionsRequest>,
    ) -> Result<Response<agent::v1::ObserveSessionsResponse>, Status> {
        Ok(Response::new(
            self.state.observe_sessions(request.into_inner())?,
        ))
    }

    async fn apply_rate_policy(
        &self,
        request: Request<agent::v1::ApplyRatePolicyRequest>,
    ) -> Result<Response<agent::v1::ApplyRatePolicyResponse>, Status> {
        Ok(Response::new(
            self.state.apply_rate_policy(request.into_inner())?,
        ))
    }

    async fn install_docker(
        &self,
        _request: Request<InstallDockerRequest>,
    ) -> Result<Response<InstallDockerResponse>, Status> {
        let state = self.state.clone();
        let result = tokio::task::spawn_blocking(move || state.install_docker())
            .await
            .map_err(|_| Status::internal("Docker installation worker unavailable"))??;
        Ok(Response::new(result))
    }

    async fn update_agent(
        &self,
        request: Request<agent::v1::UpdateAgentRequest>,
    ) -> Result<Response<agent::v1::UpdateAgentResponse>, Status> {
        let state = self.state.clone();
        let request = request.into_inner();
        let (response, outcome) = tokio::task::spawn_blocking(move || state.update_agent(request))
            .await
            .map_err(|_| Status::internal("agent update worker unavailable"))??;
        if outcome == crate::update::Outcome::Updating {
            // Answer first; the service then restarts into the new binary.
            tokio::spawn(async {
                tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                crate::update::restart();
            });
        }
        Ok(Response::new(response))
    }

    async fn uninstall_agent(
        &self,
        _request: Request<AgentEmpty>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(self.state.uninstall_agent()?))
    }

    async fn prepare_decommission(
        &self,
        request: Request<agent::v1::DecommissionRequest>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        self.state
            .prepare_decommission(&request.into_inner().command_id)?;
        Ok(Response::new(RuntimeCommandResponse {
            summary: "decommission prepared; profile mutations fenced".to_string(),
            payload_json: String::new(),
        }))
    }

    async fn delete_runtime_for_decommission(
        &self,
        request: Request<agent::v1::DecommissionRequest>,
    ) -> Result<Response<DeleteRuntimeResponse>, Status> {
        let _guard = self
            .state
            .guard_prepared_decommission(&request.into_inner().command_id)?;
        Ok(Response::new(self.state.delete_runtime_unchecked(false)?))
    }

    async fn uninstall_agent_for_decommission(
        &self,
        request: Request<agent::v1::DecommissionRequest>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        let req = request.into_inner();
        Ok(Response::new(self.state.uninstall_agent_for_decommission(
            &req.command_id,
            &req.bot_public_key,
        )?))
    }

    async fn delete_runtime(
        &self,
        request: Request<DeleteRuntimeRequest>,
    ) -> Result<Response<DeleteRuntimeResponse>, Status> {
        Ok(Response::new(
            self.state
                .delete_runtime(request.into_inner().preserve_config)?,
        ))
    }

    async fn init_xray(
        &self,
        request: Request<InitXrayRequest>,
    ) -> Result<Response<InitXrayResponse>, Status> {
        Ok(Response::new(self.state.init_xray(request.into_inner())?))
    }

    async fn deploy_xray(
        &self,
        _request: Request<AgentEmpty>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(self.state.deploy_xray()?))
    }

    async fn init_awg(
        &self,
        _request: Request<AgentEmpty>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(self.state.init_awg()?))
    }

    async fn deploy_awg(
        &self,
        _request: Request<AgentEmpty>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(self.state.deploy_awg()?))
    }

    async fn get_awg_entropy(
        &self,
        _request: Request<AgentEmpty>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(self.state.get_awg_entropy()?))
    }

    async fn regenerate_awg_entropy(
        &self,
        _request: Request<AgentEmpty>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(self.state.regenerate_awg_entropy()?))
    }

    async fn path_exists(
        &self,
        request: Request<PathExistsRequest>,
    ) -> Result<Response<PathExistsResponse>, Status> {
        Ok(Response::new(
            self.state.path_exists(&request.into_inner().path),
        ))
    }

    async fn remove_authorized_key(
        &self,
        request: Request<RemoveAuthorizedKeyRequest>,
    ) -> Result<Response<RemoveAuthorizedKeyResponse>, Status> {
        Ok(Response::new(
            self.state
                .remove_authorized_key(&request.into_inner().public_key)?,
        ))
    }

    async fn add_xray_user(
        &self,
        request: Request<AddXrayUserRequest>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(
            self.state.add_xray_user(request.into_inner())?,
        ))
    }

    async fn delete_xray_user(
        &self,
        request: Request<DeleteProfileRequest>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(
            self.state.delete_xray_user(request.into_inner())?,
        ))
    }

    async fn add_awg_user(
        &self,
        request: Request<AddAwgUserRequest>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(
            self.state.add_awg_user(request.into_inner())?,
        ))
    }

    async fn delete_awg_user(
        &self,
        request: Request<DeleteProfileRequest>,
    ) -> Result<Response<RuntimeCommandResponse>, Status> {
        Ok(Response::new(
            self.state.delete_awg_user(request.into_inner())?,
        ))
    }

    async fn apply_relay(
        &self,
        request: Request<agent::v1::ApplyRelayRequest>,
    ) -> Result<Response<agent::v1::RelayState>, Status> {
        let settings =
            crate::relay::Settings::parse(request.into_inner()).map_err(crate::relay::status)?;
        relay_blocking(move |host, paths| crate::relay::apply(host, paths, &settings)).await
    }

    async fn get_relay_state(
        &self,
        _request: Request<AgentEmpty>,
    ) -> Result<Response<agent::v1::RelayState>, Status> {
        relay_blocking(|host, paths| Ok(crate::relay::state(host, paths))).await
    }

    async fn remove_relay(
        &self,
        _request: Request<AgentEmpty>,
    ) -> Result<Response<agent::v1::RelayState>, Status> {
        relay_blocking(|host, paths| crate::relay::remove(host, paths)).await
    }

    async fn apply_cabinet(
        &self,
        request: Request<agent::v1::ApplyCabinetRequest>,
    ) -> Result<Response<agent::v1::CabinetState>, Status> {
        let request = request.into_inner();
        crate::cabinet::check(&request).map_err(crate::cabinet::status)?;
        cabinet_blocking(move |host, paths| crate::cabinet::apply(host, paths, &request)).await
    }

    async fn remove_cabinet(
        &self,
        _request: Request<AgentEmpty>,
    ) -> Result<Response<agent::v1::CabinetState>, Status> {
        cabinet_blocking(|host, paths| crate::cabinet::remove(host, paths)).await
    }

    async fn bot_key(
        &self,
        _request: Request<AgentEmpty>,
    ) -> Result<Response<agent::v1::BotState>, Status> {
        bot_blocking(|host, paths| crate::bot::key(host, paths)).await
    }

    async fn apply_bot(
        &self,
        request: Request<agent::v1::ApplyBotRequest>,
    ) -> Result<Response<agent::v1::BotState>, Status> {
        let request = request.into_inner();
        crate::bot::check(&request).map_err(crate::bot::status)?;
        bot_blocking(move |host, paths| crate::bot::apply(host, paths, &request)).await
    }

    async fn remove_bot(
        &self,
        _request: Request<AgentEmpty>,
    ) -> Result<Response<agent::v1::BotState>, Status> {
        bot_blocking(|host, paths| crate::bot::remove(host, paths)).await
    }
}

/// The bot's environment is built with uv and waits on systemd; keep it off the async workers.
async fn bot_blocking(
    work: impl FnOnce(&crate::bot::System, &crate::bot::Paths) -> Result<agent::v1::BotState, String>
    + Send
    + 'static,
) -> Result<Response<agent::v1::BotState>, Status> {
    tokio::task::spawn_blocking(move || work(&crate::bot::System, &crate::bot::Paths::host()))
        .await
        .map_err(|_| Status::internal("bot worker unavailable"))?
        .map(Response::new)
        .map_err(crate::bot::status)
}

/// The cabinet's files and the relay's restart stay off the async workers.
async fn cabinet_blocking(
    work: impl FnOnce(
        &crate::relay::System,
        &crate::cabinet::Paths,
    ) -> Result<agent::v1::CabinetState, String>
    + Send
    + 'static,
) -> Result<Response<agent::v1::CabinetState>, Status> {
    tokio::task::spawn_blocking(move || work(&crate::relay::System, &crate::cabinet::Paths::host()))
        .await
        .map_err(|_| Status::internal("cabinet worker unavailable"))?
        .map(Response::new)
        .map_err(crate::cabinet::status)
}

/// The relay's host work waits on systemd; keep it off the async workers.
async fn relay_blocking(
    work: impl FnOnce(
        &crate::relay::System,
        &crate::relay::Paths,
    ) -> Result<agent::v1::RelayState, String>
    + Send
    + 'static,
) -> Result<Response<agent::v1::RelayState>, Status> {
    tokio::task::spawn_blocking(move || work(&crate::relay::System, &crate::relay::Paths::host()))
        .await
        .map_err(|_| Status::internal("relay worker unavailable"))?
        .map(Response::new)
        .map_err(crate::relay::status)
}

#[cfg(test)]
mod tests {
    use crate::config::AgentConfig;
    use crate::state::AgentState;

    #[tokio::test]
    async fn host_metric_rpc_is_bound_to_node_and_does_not_need_runtime_files() {
        use super::{NodeAgentApi, NodeAgentService, agent};
        use serde_json::Value;
        use std::time::{Duration, Instant};
        use tonic::Request;
        let state = AgentState::new(AgentConfig {
            node_key: "cpu-node".into(),
            ..AgentConfig::default()
        });
        let now = Instant::now();
        let start = now - Duration::from_secs(60);
        for second in (0..=60).step_by(5) {
            state.cpu.lock().unwrap().observe(
                Some(&format!("cpu {} 0 0 {}", second * 9, second)),
                start + Duration::from_secs(second),
            );
        }
        let api = NodeAgentApi { state };
        let request = |node_key: &str| {
            Request::new(agent::v1::BackendNodeActionRequest {
                node_key: node_key.into(),
                action: "host_metrics".into(),
                ..Default::default()
            })
        };
        let value: Value = serde_json::from_str(
            &api.backend_node_action(request("cpu-node"))
                .await
                .unwrap()
                .into_inner()
                .payload_json,
        )
        .unwrap();
        assert_eq!(value["node_key"], "cpu-node");
        assert_eq!(value["host_metrics"]["cpu_used_percent"], 90.0);
        assert!(
            api.backend_node_action(request("other-node"))
                .await
                .is_err()
        );
    }
}
