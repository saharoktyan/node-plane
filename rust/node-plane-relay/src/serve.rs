//! The public side: `/api/v1/*` over the controller's link, and `/health`.
use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

use axum::{
    Json, Router,
    body::{Body, Bytes},
    extract::{Request, State},
    http::{HeaderName, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{any, get},
};
use node_plane_api::{codes, error_body};
use ring::signature::Ed25519KeyPair;
use tokio::sync::{Notify, Semaphore, mpsc, oneshot};
use tokio_stream::wrappers::ReceiverStream;
use tonic::{Status, Streaming, server::NamedService};
use uuid::Uuid;

use crate::{
    LINK_KEY, LINK_NONCE, headers, keys,
    proto::{
        self, LinkMessage, RelayMessage, link_message,
        link_server::{Link, LinkServer},
        relay_message,
    },
    tls,
};

#[derive(Clone, Copy, Debug)]
pub struct Limits {
    pub request_body: usize,
    pub concurrent: usize,
    pub timeout: Duration,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            request_body: 1 << 20,
            concurrent: 64,
            timeout: Duration::from_secs(30),
        }
    }
}

/// A Response carries up to 4 MiB of body plus headers.
const MAX_LINK_MESSAGE: usize = 5 << 20;

pub struct Config {
    /// The relay's own key.
    pub key: Ed25519KeyPair,
    /// `link.pub`: the controller's public key in hex, written by the node's
    /// agent and read at every Attach, so a new key needs no restart.
    /// `status.json`: whether the controller is attached, for the agent.
    pub state_dir: PathBuf,
    pub limits: Limits,
}

/// How long the controller has to answer the challenge.
const HELLO_TIMEOUT: Duration = Duration::from_secs(10);

/// The controller's link while it is connected.
struct Connection {
    generation: u64,
    tx: mpsc::Sender<Result<RelayMessage, Status>>,
    pending: Mutex<HashMap<u64, oneshot::Sender<proto::Response>>>,
    replaced: Notify,
}

struct Inner {
    key: Ed25519KeyPair,
    state_dir: PathBuf,
    limits: Limits,
    current: Mutex<Option<Arc<Connection>>>,
    generations: AtomicU64,
    ids: AtomicU64,
    permits: Semaphore,
}

#[derive(Clone)]
pub struct Hub(Arc<Inner>);

pub fn router(config: Config) -> Router {
    let hub = Hub(Arc::new(Inner {
        key: config.key,
        state_dir: config.state_dir,
        limits: config.limits,
        current: Mutex::new(None),
        generations: AtomicU64::new(0),
        ids: AtomicU64::new(0),
        permits: Semaphore::new(config.limits.concurrent),
    }));
    let link = LinkServer::new(hub.clone()).max_decoding_message_size(MAX_LINK_MESSAGE);
    let attach = format!("/{}/Attach", LinkServer::<Hub>::NAME);
    Router::new()
        .route("/health", get(health))
        .route("/api/v1/*rest", any(forward))
        .route_service(&attach, link)
        .fallback(not_found)
        .with_state(hub)
}

impl Hub {
    fn current(&self) -> Option<Arc<Connection>> {
        self.0.current.lock().unwrap().clone()
    }

    /// Records for the agent whether a controller is attached.
    fn status(&self, version: Option<&str>) {
        let since = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_secs());
        let status = serde_json::json!({
            "link": if version.is_some() { "connected" } else { "disconnected" },
            "since": since,
            "controller_link_version": version,
        });
        let path = self.0.state_dir.join("status.json");
        if let Err(error) = keys::write_private(&path, status.to_string().as_bytes()) {
            eprintln!("{error}");
        }
    }

    /// The controller's key, if one is pinned.
    fn pinned(&self) -> Option<Vec<u8>> {
        let text = std::fs::read_to_string(self.0.state_dir.join("link.pub")).ok()?;
        keys::unhex(&text).filter(|key| key.len() == keys::KEY_LEN)
    }

    /// Serves one link from a valid Hello until it ends or a newer link
    /// replaces it.
    async fn run(
        self,
        mut inbound: Streaming<LinkMessage>,
        tx: mpsc::Sender<Result<RelayMessage, Status>>,
        link_key: Vec<u8>,
        nonce: [u8; 32],
    ) {
        let relay_key = keys::public(&self.0.key);
        let version = match tokio::time::timeout(HELLO_TIMEOUT, inbound.message()).await {
            Ok(Ok(Some(LinkMessage {
                message: Some(link_message::Message::Hello(hello)),
            }))) if keys::verify(
                &link_key,
                keys::ATTACH_LINK,
                &[&nonce, &relay_key],
                &hello.signature,
            ) =>
            {
                hello.version
            }
            _ => {
                eprintln!("link refused: no valid hello");
                return;
            }
        };
        let connection = Arc::new(Connection {
            generation: self.0.generations.fetch_add(1, Ordering::Relaxed),
            tx,
            pending: Mutex::new(HashMap::new()),
            replaced: Notify::new(),
        });
        if let Some(old) = self.0.current.lock().unwrap().replace(connection.clone()) {
            old.replaced.notify_one();
        }
        eprintln!("link connected (controller link {version})");
        self.status(Some(&version));
        loop {
            let message = tokio::select! {
                message = inbound.message() => message,
                () = connection.replaced.notified() => break,
            };
            let Ok(Some(message)) = message else { break };
            if let Some(link_message::Message::Response(response)) = message.message {
                let waiter = connection.pending.lock().unwrap().remove(&response.id);
                if let Some(waiter) = waiter {
                    let _ = waiter.send(response);
                }
            }
        }
        {
            let mut current = self.0.current.lock().unwrap();
            if current
                .as_ref()
                .is_some_and(|c| c.generation == connection.generation)
            {
                *current = None;
                self.status(None);
            }
        }
        connection.pending.lock().unwrap().clear();
        eprintln!("link disconnected");
    }
}

#[tonic::async_trait]
impl Link for Hub {
    type AttachStream = ReceiverStream<Result<RelayMessage, Status>>;

    async fn attach(
        &self,
        request: tonic::Request<Streaming<LinkMessage>>,
    ) -> Result<tonic::Response<Self::AttachStream>, Status> {
        let hex = |name| {
            request
                .metadata()
                .get(name)
                .and_then(|v| v.to_str().ok())
                .and_then(keys::unhex)
        };
        let (Some(link_key), Some(link_nonce)) = (hex(LINK_KEY), hex(LINK_NONCE)) else {
            return Err(Status::unauthenticated(
                "the link key and nonce are required",
            ));
        };
        if link_nonce.len() != 32 || self.pinned().as_ref() != Some(&link_key) {
            return Err(Status::unauthenticated(
                "the link key is not the controller's",
            ));
        }
        let inbound = request.into_inner();
        let nonce = keys::nonce();
        let challenge = proto::Challenge {
            nonce: nonce.to_vec(),
            signature: keys::sign(&self.0.key, keys::ATTACH_RELAY, &[&link_nonce, &link_key]),
        };
        let (tx, rx) = mpsc::channel(self.0.limits.concurrent);
        let first = RelayMessage {
            message: Some(relay_message::Message::Challenge(challenge)),
        };
        // The channel is new and empty.
        tx.try_send(Ok(first))
            .map_err(|_| Status::internal("link closed"))?;
        tokio::spawn(self.clone().run(inbound, tx, link_key, nonce));
        Ok(tonic::Response::new(ReceiverStream::new(rx)))
    }
}

async fn health(State(hub): State<Hub>) -> Response {
    let link = if hub.current().is_some() {
        "connected"
    } else {
        "disconnected"
    };
    Json(serde_json::json!({"status": "ok", "link": link})).into_response()
}

async fn not_found() -> Response {
    failure(StatusCode::NOT_FOUND, codes::NOT_FOUND)
}

async fn forward(State(hub): State<Hub>, request: Request) -> Response {
    let Ok(_permit) = hub.0.permits.try_acquire() else {
        return failure(StatusCode::SERVICE_UNAVAILABLE, codes::RELAY_BUSY);
    };
    let Some(connection) = hub.current() else {
        return failure(
            StatusCode::SERVICE_UNAVAILABLE,
            codes::CONTROLLER_UNAVAILABLE,
        );
    };
    let limits = hub.0.limits;
    let (parts, body) = request.into_parts();
    let Ok(body) = axum::body::to_bytes(body, limits.request_body).await else {
        return failure(StatusCode::PAYLOAD_TOO_LARGE, codes::REQUEST_TOO_LARGE);
    };
    let id = hub.0.ids.fetch_add(1, Ordering::Relaxed);
    let (waiter, answer) = oneshot::channel();
    connection.pending.lock().unwrap().insert(id, waiter);
    let message = RelayMessage {
        message: Some(relay_message::Message::Request(proto::Request {
            id,
            method: parts.method.to_string(),
            path_and_query: parts
                .uri
                .path_and_query()
                .map_or("/", |p| p.as_str())
                .to_string(),
            headers: headers::pick(&parts.headers, headers::REQUEST),
            body: body.to_vec(),
        })),
    };
    if connection.tx.send(Ok(message)).await.is_err() {
        connection.pending.lock().unwrap().remove(&id);
        return failure(
            StatusCode::SERVICE_UNAVAILABLE,
            codes::CONTROLLER_UNAVAILABLE,
        );
    }
    match tokio::time::timeout(limits.timeout, answer).await {
        Ok(Ok(response)) => respond(response),
        Ok(Err(_)) => failure(
            StatusCode::SERVICE_UNAVAILABLE,
            codes::CONTROLLER_UNAVAILABLE,
        ),
        Err(_) => {
            connection.pending.lock().unwrap().remove(&id);
            failure(StatusCode::GATEWAY_TIMEOUT, codes::CONTROLLER_TIMEOUT)
        }
    }
}

fn respond(response: proto::Response) -> Response {
    let status = u16::try_from(response.status)
        .ok()
        .and_then(|s| StatusCode::from_u16(s).ok())
        .unwrap_or(StatusCode::BAD_GATEWAY);
    let mut out = Response::new(Body::from(Bytes::from(response.body)));
    *out.status_mut() = status;
    for header in response.headers {
        if !headers::allowed(&header.name, headers::RESPONSE) {
            continue;
        }
        if let (Ok(name), Ok(value)) = (
            HeaderName::try_from(header.name),
            HeaderValue::from_bytes(&header.value),
        ) {
            out.headers_mut().append(name, value);
        }
    }
    out
}

/// An error the relay answers itself, in the contract's error format.
fn failure(status: StatusCode, code: &str) -> Response {
    let request_id = Uuid::new_v4().to_string();
    let mut response = (status, Json(error_body(code, &request_id))).into_response();
    let headers = response.headers_mut();
    headers.insert("cache-control", HeaderValue::from_static("no-store"));
    if let Ok(value) = HeaderValue::from_str(&request_id) {
        headers.insert("x-request-id", value);
    }
    response
}

fn setting(name: &str) -> Result<String, String> {
    std::env::var(name).map_err(|_| format!("{name} is not set"))
}

/// `serve` as set up by the environment (see the binary's usage); runs
/// until the listener fails.
pub async fn from_env() -> Result<(), String> {
    use std::env;
    let listen =
        env::var("NODE_PLANE_RELAY_LISTEN").unwrap_or_else(|_| "127.0.0.1:8443".to_string());
    let state = PathBuf::from(
        env::var("NODE_PLANE_RELAY_STATE_DIR")
            .unwrap_or_else(|_| "/var/lib/node-plane-relay".to_string()),
    );
    let key = keys::load_or_create(&state.join("relay.key"))?;
    let tls = match env::var("NODE_PLANE_RELAY_TLS").as_deref() {
        Err(_) | Ok("proxy") => tls::Mode::Proxy,
        Ok("files") => tls::Mode::Files {
            certificate: setting("NODE_PLANE_RELAY_TLS_CERT")?.into(),
            key: setting("NODE_PLANE_RELAY_TLS_KEY")?.into(),
        },
        Ok("acme") => tls::Mode::Acme {
            domain: setting("NODE_PLANE_RELAY_DOMAIN")?,
            contact: env::var("NODE_PLANE_RELAY_ACME_CONTACT")
                .ok()
                .filter(|v| !v.is_empty()),
            cache: state.join("acme"),
            staging: env::var("NODE_PLANE_RELAY_ACME_STAGING").as_deref() == Ok("1"),
        },
        Ok(other) => return Err(format!("NODE_PLANE_RELAY_TLS: unknown mode {other}")),
    };
    let listener = tokio::net::TcpListener::bind(&listen)
        .await
        .map_err(|e| format!("{listen}: {e}"))?;
    eprintln!("relay listening on {listen} ({tls:?})");
    let router = router(Config {
        key,
        state_dir: state,
        limits: Limits::default(),
    });
    tls::serve(listener, router, tls).await
}
