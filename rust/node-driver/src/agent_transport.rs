use tonic::transport::Channel;

use crate::agent::v1::node_agent_service_client::NodeAgentServiceClient;
use crate::agent::v1::{
    AgentEmpty, ListRemoteProfilesRequest, LocalHealth, RemoteProfileRecord, RunDiagnosticsRequest,
    RunDiagnosticsResponse, RuntimeFacts,
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
}
