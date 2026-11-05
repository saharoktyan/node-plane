//! RelayService: the backend sets up the web cabinet's relay on a node.
//! The agent installs it and returns its key; the driver pins that key and
//! keeps the link. Keys cross only as public halves.
use tonic::{Request, Response, Status};

use crate::DriverContext;
use crate::agent::v1::{ApplyRelayRequest as AgentApplyRelay, RelayState};
use crate::agent_transport::AgentTransport;
use crate::driver::v1::relay_service_server::RelayService;
use crate::driver::v1::{ApplyRelayRequest, RelayNodeRequest, RelayStatus};
use crate::relays::{self, Entry};
use node_plane_relay::keys;

pub(crate) struct RelayApi {
    pub(crate) ctx: DriverContext,
}

impl RelayApi {
    /// The node's agent, once it has shown it is that node's.
    async fn agent(&self, node_key: &str) -> Result<AgentTransport, Status> {
        if node_key.is_empty() {
            return Err(Status::invalid_argument("node key is required"));
        }
        let target = self
            .ctx
            .agent_target(node_key)
            .ok_or_else(|| Status::failed_precondition("no node-agent target configured"))?;
        let transport = AgentTransport::new(target);
        if transport.get_runtime_facts().await?.node_key != node_key {
            return Err(Status::failed_precondition("agent node identity mismatch"));
        }
        Ok(transport)
    }

    fn status(&self, node_key: &str, agent: Option<&RelayState>) -> RelayStatus {
        let mut status = RelayStatus {
            agent_reachable: agent.is_some(),
            ..RelayStatus::default()
        };
        if let Some(state) = agent {
            status.installed = state.installed;
            status.active = state.active;
            status.relay_key = keys::hex(&state.relay_key);
        }
        if let Some((entry, link)) = self.ctx.relays.status(node_key) {
            status.linked = true;
            status.url = entry.url;
            status.link_connected = link.connected;
            status.link_error = link.error;
            status.link_since = link.since;
        }
        status
    }
}

#[tonic::async_trait]
impl RelayService for RelayApi {
    async fn apply_relay(
        &self,
        request: Request<ApplyRelayRequest>,
    ) -> Result<Response<RelayStatus>, Status> {
        let request = request.into_inner();
        let agent = self.agent(&request.node_key).await?;
        let state = agent
            .apply_relay(AgentApplyRelay {
                domain: request.domain.clone(),
                port: request.port,
                tls: request.tls.clone(),
                acme_contact: request.acme_contact,
                certificate_path: request.certificate_path,
                key_path: request.key_path,
                link_key: self.ctx.relays.link_key(),
            })
            .await?;
        if state.relay_key.len() != keys::KEY_LEN {
            return Err(Status::internal("the agent returned no relay key"));
        }
        // The agent checked the domain; the URL is built from what it took.
        self.ctx
            .relays
            .pin(Entry {
                node_key: request.node_key.clone(),
                url: relays::url(
                    &request.domain.trim().to_ascii_lowercase(),
                    request.port,
                    &request.tls,
                ),
                relay_key: keys::hex(&state.relay_key),
            })
            .map_err(Status::internal)?;
        Ok(Response::new(self.status(&request.node_key, Some(&state))))
    }

    async fn remove_relay(
        &self,
        request: Request<RelayNodeRequest>,
    ) -> Result<Response<RelayStatus>, Status> {
        let node_key = request.into_inner().node_key;
        // The link goes first: a relay that is being removed gets no requests.
        self.ctx
            .relays
            .forget(&node_key)
            .map_err(Status::internal)?;
        let state = self.agent(&node_key).await?.remove_relay().await?;
        Ok(Response::new(self.status(&node_key, Some(&state))))
    }

    async fn get_relay(
        &self,
        request: Request<RelayNodeRequest>,
    ) -> Result<Response<RelayStatus>, Status> {
        let node_key = request.into_inner().node_key;
        let state = match self.agent(&node_key).await {
            Ok(agent) => agent.get_relay_state().await.ok(),
            Err(_) => None,
        };
        Ok(Response::new(self.status(&node_key, state.as_ref())))
    }
}
