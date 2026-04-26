use tonic::transport::Channel;

use crate::agent::v1::node_agent_service_client::NodeAgentServiceClient;
use crate::agent::v1::{
    AgentEmpty, CheckPortsRequest, CheckPortsResponse, ListRemoteProfilesRequest, LocalHealth,
    OpenPortsRequest, OpenPortsResponse, PortCheckSpec, RemoteProfileRecord,
    RunDiagnosticsRequest, RunDiagnosticsResponse, RuntimeFacts, RuntimeFileSpec,
    SyncNodeEnvRequest, SyncNodeEnvResponse, SyncRuntimeFilesRequest,
    SyncRuntimeFilesResponse,
};

pub struct AgentTransport {
    target: String,
}

impl AgentTransport {
    pub fn new(target: impl Into<String>) -> Self {
        Self {
            target: target.into(),
        }
    }

    async fn client(&self) -> Result<NodeAgentServiceClient<Channel>, tonic::transport::Error> {
        NodeAgentServiceClient::connect(format!("http://{}", self.target)).await
    }

    pub async fn get_runtime_facts(&self) -> Result<RuntimeFacts, tonic::Status> {
        let mut client = self.client().await.map_err(|err| {
            tonic::Status::unavailable(format!("failed to connect to node agent: {err}"))
        })?;
        let response = client.get_runtime_facts(AgentEmpty {}).await?;
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

    pub async fn sync_node_env(
        &self,
        content: &str,
    ) -> Result<SyncNodeEnvResponse, tonic::Status> {
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
}
