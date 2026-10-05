use std::env;
use std::fs;
use std::time::Duration;

use tonic::transport::{Certificate, Channel, ClientTlsConfig, Endpoint, Identity};

use crate::agent::v1::node_agent_service_client::NodeAgentServiceClient;
use crate::agent::v1::{
    AgentEmpty, ApplyBackendNodeSettingsRequest, DecommissionRequest, InstallDockerRequest,
    InstallDockerResponse, LocalHealth, PathExistsRequest, RefreshAwgConfigRequest,
    RefreshAwgConfigResponse, RunDiagnosticsRequest, RunDiagnosticsResponse,
    RuntimeCommandResponse, RuntimeFacts, RuntimeFileSpec, SyncRuntimeFilesRequest,
    SyncRuntimeFilesResponse,
};

pub struct AgentTransport {
    target: String,
}

fn required_tls_path(name: &str) -> std::io::Result<String> {
    env::var(name)
        .map(|value| value.trim().to_string())
        .ok()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::NotFound,
                format!("required mutual TLS setting {name} is missing"),
            )
        })
}

impl AgentTransport {
    pub async fn backend_node_action(
        &self,
        request: crate::agent::v1::BackendNodeActionRequest,
    ) -> Result<RuntimeCommandResponse, tonic::Status> {
        let mut client = self
            .client()
            .await
            .map_err(|_| tonic::Status::unavailable("agent unavailable"))?;
        Ok(client.backend_node_action(request).await?.into_inner())
    }
    pub async fn get_backend_xray_public(
        &self,
    ) -> Result<crate::agent::v1::BackendXrayPublic, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        Ok(client
            .get_backend_xray_public(AgentEmpty {})
            .await?
            .into_inner())
    }

    pub async fn apply_backend_node_settings(
        &self,
        request: ApplyBackendNodeSettingsRequest,
    ) -> Result<RuntimeCommandResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        Ok(client
            .apply_backend_node_settings(request)
            .await?
            .into_inner())
    }

    pub async fn recover_backend_node_settings(
        &self,
        request: ApplyBackendNodeSettingsRequest,
    ) -> Result<RuntimeCommandResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        Ok(client
            .recover_backend_node_settings(request)
            .await?
            .into_inner())
    }

    pub async fn resolve_backend_node_settings(
        &self,
        request: ApplyBackendNodeSettingsRequest,
    ) -> Result<RuntimeCommandResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        Ok(client
            .resolve_backend_node_settings(request)
            .await?
            .into_inner())
    }
    pub fn new(target: impl Into<String>) -> Self {
        Self {
            target: target.into(),
        }
    }

    async fn client(
        &self,
    ) -> Result<NodeAgentServiceClient<Channel>, Box<dyn std::error::Error + Send + Sync>> {
        let ca = fs::read(required_tls_path("NODE_AGENT_CA_CERT")?)?;
        let certificate = fs::read(required_tls_path("NODE_AGENT_CLIENT_CERT")?)?;
        let key = fs::read(required_tls_path("NODE_AGENT_CLIENT_KEY")?)?;
        let host = self
            .target
            .rsplit_once(':')
            .map(|(host, _)| host)
            .unwrap_or(self.target.as_str())
            .trim_start_matches('[')
            .trim_end_matches(']')
            .to_string();
        if host.is_empty() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "agent target must include a host",
            )
            .into());
        }
        let tls = ClientTlsConfig::new()
            .ca_certificate(Certificate::from_pem(ca))
            .identity(Identity::from_pem(certificate, key))
            .domain_name(host);
        let endpoint = Endpoint::from_shared(format!("https://{}", self.target))?
            .tls_config(tls)?
            .connect_timeout(Duration::from_secs(5));
        Ok(NodeAgentServiceClient::new(endpoint.connect().await?))
    }

    pub async fn get_runtime_facts(&self) -> Result<RuntimeFacts, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let mut request = tonic::Request::new(AgentEmpty {});
        request.set_timeout(Duration::from_secs(5));
        let response = client.get_runtime_facts(request).await?;
        Ok(response.into_inner())
    }

    pub async fn get_node_health(&self) -> Result<LocalHealth, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client.get_node_health(AgentEmpty {}).await?;
        Ok(response.into_inner())
    }

    pub async fn run_diagnostics(&self) -> Result<RunDiagnosticsResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client.run_diagnostics(RunDiagnosticsRequest {}).await?;
        Ok(response.into_inner())
    }

    pub async fn sync_runtime_files(
        &self,
        files: Vec<RuntimeFileSpec>,
    ) -> Result<SyncRuntimeFilesResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client
            .sync_runtime_files(SyncRuntimeFilesRequest { files })
            .await?;
        Ok(response.into_inner())
    }

    pub async fn install_docker(&self) -> Result<InstallDockerResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client.install_docker(InstallDockerRequest {}).await?;
        Ok(response.into_inner())
    }

    pub async fn prepare_decommission(&self, command_id: &str) -> Result<(), tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let mut request = tonic::Request::new(DecommissionRequest {
            command_id: command_id.to_string(),
            bot_public_key: String::new(),
        });
        request.set_timeout(Duration::from_secs(30));
        client.prepare_decommission(request).await?;
        Ok(())
    }

    pub async fn delete_runtime_for_decommission(
        &self,
        command_id: &str,
    ) -> Result<(), tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let mut request = tonic::Request::new(DecommissionRequest {
            command_id: command_id.to_string(),
            bot_public_key: String::new(),
        });
        request.set_timeout(Duration::from_secs(180));
        client.delete_runtime_for_decommission(request).await?;
        Ok(())
    }

    pub async fn uninstall_agent_for_decommission(
        &self,
        command_id: &str,
        bot_public_key: &str,
    ) -> Result<(), tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let mut request = tonic::Request::new(DecommissionRequest {
            command_id: command_id.to_string(),
            bot_public_key: bot_public_key.to_string(),
        });
        request.set_timeout(Duration::from_secs(30));
        client.uninstall_agent_for_decommission(request).await?;
        Ok(())
    }

    pub async fn refresh_awg_config(
        &self,
        wg_conf: &str,
        profile_name: &str,
    ) -> Result<RefreshAwgConfigResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let mut request = tonic::Request::new(RefreshAwgConfigRequest {
            wg_conf: wg_conf.to_string(),
            profile_name: profile_name.to_string(),
        });
        request.set_timeout(Duration::from_secs(30));
        Ok(client.refresh_awg_config(request).await?.into_inner())
    }

    pub async fn path_exists(&self, path: &str) -> Result<bool, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client
            .path_exists(PathExistsRequest {
                path: path.to_string(),
            })
            .await?;
        Ok(response.into_inner().exists)
    }

    pub async fn recover_profile_intent(
        &self,
        request: crate::agent::v1::ApplyProfileIntentRequest,
    ) -> Result<RuntimeCommandResponse, tonic::Status> {
        let mut client = self
            .client()
            .await
            .map_err(|_| tonic::Status::unavailable("agent unavailable"))?;
        Ok(client.recover_profile_intent(request).await?.into_inner())
    }

    pub async fn resolve_profile_intent(
        &self,
        request: crate::agent::v1::ApplyProfileIntentRequest,
    ) -> Result<crate::agent::v1::ProfileInspection, tonic::Status> {
        let mut client = self
            .client()
            .await
            .map_err(|_| tonic::Status::unavailable("agent unavailable"))?;
        Ok(client.resolve_profile_intent(request).await?.into_inner())
    }

    pub async fn apply_profile_intent(
        &self,
        request: crate::agent::v1::ApplyProfileIntentRequest,
    ) -> Result<RuntimeCommandResponse, tonic::Status> {
        let mut client = self
            .client()
            .await
            .map_err(|_| tonic::Status::unavailable("agent unavailable"))?;
        Ok(client.apply_profile_intent(request).await?.into_inner())
    }

    pub async fn inspect_profile(
        &self,
        protocol: &str,
        profile: &str,
        expected_uuid: &str,
    ) -> Result<crate::agent::v1::ProfileInspection, tonic::Status> {
        let mut client = self
            .client()
            .await
            .map_err(|_| tonic::Status::unavailable("agent unavailable"))?;
        let mut request = tonic::Request::new(crate::agent::v1::InspectProfileRequest {
            protocol_kind: protocol.to_string(),
            profile_name: profile.to_string(),
            expected_xray_uuid: expected_uuid.to_string(),
        });
        request.set_timeout(Duration::from_secs(20));
        Ok(client.inspect_profile(request).await?.into_inner())
    }
}
