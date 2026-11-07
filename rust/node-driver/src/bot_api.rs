//! BotService: the backend has a node run the Telegram bot. The package lies
//! on this host, built from the controller's release; the driver reads it and
//! the node's agent installs it.
use std::path::PathBuf;

use tonic::{Request, Response, Status};

use crate::DriverContext;
use crate::agent::v1::{ApplyBotRequest as AgentApplyBot, BotState};
use crate::cabinet_api::read_tree;
use crate::driver::v1::bot_service_server::BotService;
use crate::driver::v1::{ApplyBotRequest, BotNodeRequest, BotStatus};
use crate::relay_api::agent;

/// The code, its pinned packages and uv; the agent sets its own limits too.
const MAX_BYTES: u64 = 40 << 20;

pub(crate) struct BotApi {
    pub(crate) ctx: DriverContext,
}

fn refused(error: String) -> Status {
    if error.starts_with("bot_") && !error.contains(' ') {
        Status::invalid_argument(error)
    } else {
        Status::internal(error)
    }
}

fn status(state: BotState) -> Response<BotStatus> {
    Response::new(BotStatus {
        version: state.version,
        public_key: state.public_key,
        active: state.active,
    })
}

#[tonic::async_trait]
impl BotService for BotApi {
    async fn bot_key(
        &self,
        request: Request<BotNodeRequest>,
    ) -> Result<Response<BotStatus>, Status> {
        let node_key = request.into_inner().node_key;
        Ok(status(agent(&self.ctx, &node_key).await?.bot_key().await?))
    }

    async fn apply_bot(
        &self,
        request: Request<ApplyBotRequest>,
    ) -> Result<Response<BotStatus>, Status> {
        let request = request.into_inner();
        let root = PathBuf::from(&request.root);
        let files = tokio::task::spawn_blocking(move || {
            read_tree(&root, "app/telegram_client/main.py", "bot", MAX_BYTES)
        })
        .await
        .map_err(|_| Status::internal("bot reader unavailable"))?
        .map_err(refused)?;
        let state = agent(&self.ctx, &request.node_key)
            .await?
            .apply_bot(AgentApplyBot {
                version: request.version,
                files,
                bot_token: request.bot_token,
                api_url: request.api_url,
                relay_ca: request.relay_ca,
                key_id: request.key_id,
            })
            .await?;
        Ok(status(state))
    }

    async fn remove_bot(
        &self,
        request: Request<BotNodeRequest>,
    ) -> Result<Response<BotStatus>, Status> {
        let node_key = request.into_inner().node_key;
        Ok(status(
            agent(&self.ctx, &node_key).await?.remove_bot().await?,
        ))
    }
}
