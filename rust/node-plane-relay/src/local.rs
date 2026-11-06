//! The web cabinet served by the controller itself, without a relay.
//!
//! The controller's worker writes a small settings file: the address to
//! listen on and the cabinet's directory. This server reads it every few
//! seconds and listens, moves or stops as it says. It hands out the
//! cabinet's files like the relay does, and passes `/api/v1/*` to the API on
//! this host under the relay's rules (`link::forward`): only the allowed
//! headers, the relay's mark, the client's address, the same limits. The
//! API then treats these requests as it treats the relay's, since both come
//! from the network.
use std::{
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};

use axum::{
    Router,
    body::Body,
    extract::{ConnectInfo, Request, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::any,
};
use hyper_util::{client::legacy::Client, rt::TokioExecutor};
use node_plane_api::codes;
use tokio::{sync::Semaphore, task::JoinHandle};

use crate::{cabinet, headers, link, proto};

/// How often the settings are read again.
const POLL: Duration = Duration::from_secs(3);

/// What the worker asks for.
#[derive(Clone, Debug, PartialEq)]
pub struct Settings {
    pub listen: SocketAddr,
    pub root: PathBuf,
}

/// The settings in `path`, or None when there are none (or they are not
/// readable as such: then the server stays quiet, and says why).
pub fn read(path: &Path) -> Option<Settings> {
    let text = std::fs::read_to_string(path).ok()?;
    let value: serde_json::Value = match serde_json::from_str(&text) {
        Ok(value) => value,
        Err(error) => {
            eprintln!("{}: {error}", path.display());
            return None;
        }
    };
    let listen = value["listen"].as_str()?.parse().ok();
    let root = value["root"].as_str().map(PathBuf::from);
    match (listen, root) {
        (Some(listen), Some(root)) if root.is_absolute() => Some(Settings { listen, root }),
        _ => {
            eprintln!(
                "{}: needs listen (host:port) and an absolute root",
                path.display()
            );
            None
        }
    }
}

struct Inner {
    api: String,
    root: PathBuf,
    http: link::Http,
    permits: Semaphore,
    limits: link::Limits,
    request_body: usize,
}

/// The server's routes: the API through the relay's rules, the cabinet for
/// everything else.
pub fn router(api: String, root: PathBuf) -> Router {
    let limits = link::Limits::default();
    let state = Arc::new(Inner {
        api,
        root,
        http: Client::builder(TokioExecutor::new()).build_http(),
        permits: Semaphore::new(limits.concurrent),
        limits,
        request_body: 1 << 20,
    });
    Router::new()
        .route("/api/v1/*rest", any(forward))
        .fallback(files)
        .with_state(state)
}

async fn files(State(state): State<Arc<Inner>>, request: Request) -> Response {
    cabinet::respond(&state.root, request.method(), request.uri().path()).await
}

fn failure(status: StatusCode, code: &str) -> Response {
    let body = node_plane_api::error_body(code, &uuid::Uuid::new_v4().to_string());
    (status, axum::Json(body)).into_response()
}

async fn forward(
    State(state): State<Arc<Inner>>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    request: Request,
) -> Response {
    let Ok(_permit) = state.permits.try_acquire() else {
        return failure(StatusCode::SERVICE_UNAVAILABLE, codes::RELAY_BUSY);
    };
    let (parts, body) = request.into_parts();
    let Ok(body) = axum::body::to_bytes(body, state.request_body).await else {
        return failure(StatusCode::PAYLOAD_TOO_LARGE, codes::REQUEST_TOO_LARGE);
    };
    let request = proto::Request {
        id: 0,
        method: parts.method.to_string(),
        path_and_query: parts
            .uri
            .path_and_query()
            .map_or("/", |p| p.as_str())
            .to_string(),
        headers: headers::pick(&parts.headers, headers::REQUEST),
        body: body.to_vec(),
        client: peer.ip().to_string(),
    };
    let answer = link::forward(&state.http, &state.api, request, state.limits).await;
    let mut response = Response::new(Body::from(answer.body));
    *response.status_mut() =
        StatusCode::from_u16(answer.status as u16).unwrap_or(StatusCode::BAD_GATEWAY);
    for header in answer.headers {
        if let (Ok(name), Ok(value)) = (
            http::HeaderName::try_from(header.name),
            http::HeaderValue::from_bytes(&header.value),
        ) {
            response.headers_mut().append(name, value);
        }
    }
    response
}

async fn listen(settings: Settings, api: String) -> Result<(), String> {
    let listener = tokio::net::TcpListener::bind(settings.listen)
        .await
        .map_err(|e| format!("{}: {e}", settings.listen))?;
    eprintln!("cabinet listening on {}", settings.listen);
    axum::serve(
        listener,
        router(api, settings.root).into_make_service_with_connect_info::<SocketAddr>(),
    )
    .await
    .map_err(|e| e.to_string())
}

/// Follows the settings file for good: listens while it asks to, moves when
/// the address changes, stops when it goes away.
pub async fn run(settings_path: PathBuf, api: String) {
    let mut current: Option<(Settings, JoinHandle<()>)> = None;
    loop {
        let wanted = read(&settings_path);
        let same = match (&current, &wanted) {
            (Some((running, task)), Some(wanted)) => running == wanted && !task.is_finished(),
            (None, None) => true,
            _ => false,
        };
        if !same {
            if let Some((_, task)) = current.take() {
                task.abort();
                let _ = task.await;
            }
            if let Some(settings) = wanted {
                let api = api.clone();
                let task_settings = settings.clone();
                let task = tokio::spawn(async move {
                    if let Err(error) = listen(task_settings, api).await {
                        eprintln!("{error}");
                    }
                });
                current = Some((settings, task));
            } else {
                eprintln!("cabinet not served: no settings");
            }
        }
        tokio::time::sleep(POLL).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn settings_are_read_or_nothing() {
        let dir = std::env::temp_dir().join(format!("cabinet-local-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("settings.json");
        assert_eq!(read(&path), None);
        std::fs::write(
            &path,
            r#"{"listen": "0.0.0.0:8090", "root": "/var/lib/node-plane-cabinet/current"}"#,
        )
        .unwrap();
        assert_eq!(
            read(&path),
            Some(Settings {
                listen: "0.0.0.0:8090".parse().unwrap(),
                root: "/var/lib/node-plane-cabinet/current".into(),
            })
        );
        std::fs::write(&path, r#"{"listen": "cab:80", "root": "/x"}"#).unwrap();
        assert_eq!(read(&path), None);
        std::fs::write(&path, r#"{"listen": "0.0.0.0:8090", "root": "relative"}"#).unwrap();
        assert_eq!(read(&path), None);
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
