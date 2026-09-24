use std::env;
use std::fs;
use std::time::Duration;

use tonic::transport::{Certificate, Channel, ClientTlsConfig, Endpoint, Identity};

use crate::agent::v1::node_agent_service_client::NodeAgentServiceClient;
use crate::agent::v1::{
    AddAwgUserRequest, AddXrayUserRequest, AgentEmpty, CheckPortsRequest, CheckPortsResponse,
    DeleteProfileRequest, DeleteRuntimeRequest, DeleteRuntimeResponse, InitXrayRequest,
    InitXrayResponse, InstallDockerRequest, InstallDockerResponse, ListRemoteProfilesRequest,
    LocalHealth, OpenPortsRequest, OpenPortsResponse, PathExistsRequest, PortCheckSpec,
    RemoteProfileRecord, RemoveAuthorizedKeyRequest, RemoveAuthorizedKeyResponse,
    RunDiagnosticsRequest, RunDiagnosticsResponse, RuntimeCommandResponse, RuntimeFacts,
    RuntimeFileSpec, SyncNodeEnvRequest, SyncNodeEnvResponse, SyncRuntimeFilesRequest,
    SyncRuntimeFilesResponse, SyncXrayRequest, SyncXrayResponse,
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

    pub async fn list_remote_profiles(
        &self,
        protocol_kind: &str,
    ) -> Result<Vec<RemoteProfileRecord>, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client
            .list_remote_profiles(ListRemoteProfilesRequest {
                protocol_kind: protocol_kind.to_string(),
            })
            .await?;
        Ok(response.into_inner().items)
    }

    pub async fn run_diagnostics(&self) -> Result<RunDiagnosticsResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client.run_diagnostics(RunDiagnosticsRequest {}).await?;
        Ok(response.into_inner())
    }

    pub async fn check_ports(
        &self,
        items: Vec<PortCheckSpec>,
    ) -> Result<CheckPortsResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client.check_ports(CheckPortsRequest { items }).await?;
        Ok(response.into_inner())
    }

    pub async fn sync_node_env(&self, content: &str) -> Result<SyncNodeEnvResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client
            .sync_node_env(SyncNodeEnvRequest {
                content: content.to_string(),
            })
            .await?;
        Ok(response.into_inner())
    }

    pub async fn open_ports(
        &self,
        items: Vec<PortCheckSpec>,
    ) -> Result<OpenPortsResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client.open_ports(OpenPortsRequest { items }).await?;
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

    pub async fn sync_xray(
        &self,
        config_path: &str,
        public_host: &str,
        flow: &str,
        image: &str,
    ) -> Result<SyncXrayResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client
            .sync_xray(SyncXrayRequest {
                config_path: config_path.to_string(),
                public_host: public_host.to_string(),
                flow: flow.to_string(),
                image: image.to_string(),
            })
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

    pub async fn delete_runtime(
        &self,
        preserve_config: bool,
    ) -> Result<DeleteRuntimeResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let mut request = tonic::Request::new(DeleteRuntimeRequest { preserve_config });
        request.set_timeout(Duration::from_secs(180));
        let response = client.delete_runtime(request).await?;
        Ok(response.into_inner())
    }

    pub async fn init_xray(
        &self,
        config_path: &str,
        public_host: &str,
        sni_host: &str,
        tcp_port: u32,
        xhttp_port: u32,
        xhttp_path_prefix: &str,
        flow: &str,
        image: &str,
    ) -> Result<InitXrayResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client
            .init_xray(InitXrayRequest {
                config_path: config_path.to_string(),
                public_host: public_host.to_string(),
                sni_host: sni_host.to_string(),
                tcp_port,
                xhttp_port,
                xhttp_path_prefix: xhttp_path_prefix.to_string(),
                flow: flow.to_string(),
                image: image.to_string(),
            })
            .await?;
        Ok(response.into_inner())
    }

    pub async fn deploy_xray(&self) -> Result<RuntimeCommandResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client.deploy_xray(AgentEmpty {}).await?;
        Ok(response.into_inner())
    }

    pub async fn init_awg(&self) -> Result<RuntimeCommandResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client.init_awg(AgentEmpty {}).await?;
        Ok(response.into_inner())
    }

    pub async fn deploy_awg(&self) -> Result<RuntimeCommandResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client.deploy_awg(AgentEmpty {}).await?;
        Ok(response.into_inner())
    }

    pub async fn get_awg_entropy(&self) -> Result<RuntimeCommandResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let mut request = tonic::Request::new(AgentEmpty {});
        request.set_timeout(Duration::from_secs(10));
        let response = client.get_awg_entropy(request).await?;
        Ok(response.into_inner())
    }

    pub async fn regenerate_awg_entropy(&self) -> Result<RuntimeCommandResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let mut request = tonic::Request::new(AgentEmpty {});
        request.set_timeout(Duration::from_secs(180));
        let response = client.regenerate_awg_entropy(request).await?;
        Ok(response.into_inner())
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

    pub async fn remove_authorized_key(
        &self,
        public_key: &str,
    ) -> Result<RemoveAuthorizedKeyResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client
            .remove_authorized_key(RemoveAuthorizedKeyRequest {
                public_key: public_key.to_string(),
            })
            .await?;
        Ok(response.into_inner())
    }

    pub async fn add_xray_user(
        &self,
        profile_name: &str,
        uuid: &str,
        short_id: &str,
    ) -> Result<RuntimeCommandResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client
            .add_xray_user(AddXrayUserRequest {
                profile_name: profile_name.to_string(),
                uuid: uuid.to_string(),
                short_id: short_id.to_string(),
            })
            .await?;
        Ok(response.into_inner())
    }

    pub async fn delete_xray_user(
        &self,
        profile_name: &str,
    ) -> Result<RuntimeCommandResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client
            .delete_xray_user(DeleteProfileRequest {
                profile_name: profile_name.to_string(),
            })
            .await?;
        Ok(response.into_inner())
    }

    pub async fn add_awg_user(
        &self,
        profile_name: &str,
    ) -> Result<RuntimeCommandResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client
            .add_awg_user(AddAwgUserRequest {
                profile_name: profile_name.to_string(),
            })
            .await?;
        Ok(response.into_inner())
    }

    pub async fn delete_awg_user(
        &self,
        profile_name: &str,
    ) -> Result<RuntimeCommandResponse, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client
            .delete_awg_user(DeleteProfileRequest {
                profile_name: profile_name.to_string(),
            })
            .await?;
        Ok(response.into_inner())
    }
}
