//! `serve`, `link` and a fake controller API on loopback ports.
use std::{net::SocketAddr, time::Duration};

use axum::{
    Json, Router,
    body::{Body, Bytes},
    http::{HeaderMap, Request, StatusCode},
    response::IntoResponse,
    routing::{any, get},
};
use http_body_util::{BodyExt, Full};
use hyper_util::{client::legacy::Client, rt::TokioExecutor};
use node_plane_relay::{link, serve};
use serde_json::{Value, json};

const TOKEN: &str = "0123456789abcdef0123456789abcdef";

async fn listen(router: Router) -> SocketAddr {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    address
}

/// Echoes what reached it, the way the controller API would see it.
fn fake_api() -> Router {
    async fn echo(request: Request<Body>) -> impl IntoResponse {
        let (parts, body) = request.into_parts();
        let headers: Value = parts
            .headers
            .iter()
            .map(|(k, v)| (k.to_string(), json!(v.to_str().unwrap())))
            .collect::<serde_json::Map<_, _>>()
            .into();
        let via: Vec<_> = parts.headers.get_all("x-node-plane-via").iter().collect();
        let body = body.collect().await.unwrap().to_bytes();
        (
            [
                ("etag", "\"7\""),
                ("set-cookie", "a=b"),
                ("x-node-plane-secret", "1"),
            ],
            Json(json!({
                "method": parts.method.as_str(),
                "uri": parts.uri.to_string(),
                "headers": headers,
                "via_count": via.len(),
                "body": String::from_utf8_lossy(&body),
            })),
        )
    }
    Router::new()
        .route(
            "/api/v1/slow",
            get(|| async {
                tokio::time::sleep(Duration::from_secs(5)).await;
                "late"
            }),
        )
        .route("/api/v1/large", get(|| async { vec![b'x'; 64 << 10] }))
        .route("/api/v1/*rest", any(echo))
}

async fn relay(limits: serve::Limits) -> SocketAddr {
    listen(serve::router(serve::Config {
        token: TOKEN.into(),
        limits,
    }))
    .await
}

fn link_config(relay: SocketAddr, api: SocketAddr) -> link::Config {
    link::Config {
        relay: format!("http://{relay}"),
        token: TOKEN.into(),
        api: format!("http://{api}"),
        limits: link::Limits {
            response_body: 16 << 10,
            ..link::Limits::default()
        },
    }
}

async fn call(request: Request<Full<Bytes>>) -> (StatusCode, HeaderMap, Value) {
    let client = Client::builder(TokioExecutor::new()).build_http();
    let response = client.request(request).await.unwrap();
    let (parts, body) = response.into_parts();
    let body = body.collect().await.unwrap().to_bytes();
    let body = serde_json::from_slice(&body).unwrap_or(Value::Null);
    (parts.status, parts.headers, body)
}

async fn get_json(relay: SocketAddr, path: &str) -> (StatusCode, HeaderMap, Value) {
    call(
        Request::get(format!("http://{relay}{path}"))
            .body(Full::default())
            .unwrap(),
    )
    .await
}

async fn wait_for_link(relay: SocketAddr, state: &str) {
    for _ in 0..100 {
        if get_json(relay, "/health").await.2["link"] == state {
            return;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    panic!("the link never became {state}");
}

#[tokio::test]
async fn without_a_link_the_relay_answers_controller_unavailable() {
    let relay = relay(serve::Limits::default()).await;
    let (status, headers, body) = get_json(relay, "/health").await;
    assert_eq!(
        (status, body),
        (
            StatusCode::OK,
            json!({"status": "ok", "link": "disconnected"})
        )
    );
    assert!(headers.get("x-request-id").is_none());
    let (status, headers, body) = get_json(relay, "/api/v1/me").await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["error"]["code"], "controller_unavailable");
    assert_eq!(
        headers["x-request-id"].to_str().unwrap(),
        body["error"]["request_id"]
    );
    let (status, _, body) = get_json(relay, "/index.php").await;
    assert_eq!(
        (status, &body["error"]["code"]),
        (StatusCode::NOT_FOUND, &json!("not_found"))
    );
}

#[tokio::test]
async fn requests_reach_the_api_with_the_mark_and_only_allowed_headers() {
    let (relay, api) = (
        relay(serve::Limits::default()).await,
        listen(fake_api()).await,
    );
    tokio::spawn(link::run(link_config(relay, api)));
    wait_for_link(relay, "connected").await;

    let request = Request::post(format!("http://{relay}/api/v1/profiles/p1?x=1"))
        .header("authorization", "Bearer session")
        .header("idempotency-key", "k1")
        .header("x-node-plane-via", "browser")
        .header("x-node-plane-telegram-user-id", "42")
        .header("cookie", "c=1")
        .body(Full::new(Bytes::from_static(b"{\"a\":1}")))
        .unwrap();
    let (status, headers, body) = call(request).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["method"], "POST");
    assert_eq!(body["uri"], "/api/v1/profiles/p1?x=1");
    assert_eq!(body["body"], "{\"a\":1}");
    let sent = &body["headers"];
    assert_eq!(sent["authorization"], "Bearer session");
    assert_eq!(sent["idempotency-key"], "k1");
    assert_eq!(
        (&sent["x-node-plane-via"], &body["via_count"]),
        (&json!("relay"), &json!(1))
    );
    assert!(sent.get("x-node-plane-telegram-user-id").is_none());
    assert!(sent.get("cookie").is_none());
    assert_eq!(headers["etag"], "\"7\"");
    assert!(headers.get("set-cookie").is_none() && headers.get("x-node-plane-secret").is_none());
}

#[tokio::test]
async fn the_relay_enforces_its_limits() {
    let limits = serve::Limits {
        request_body: 1024,
        timeout: Duration::from_millis(300),
        ..serve::Limits::default()
    };
    let (relay, api) = (relay(limits).await, listen(fake_api()).await);
    tokio::spawn(link::run(link_config(relay, api)));
    wait_for_link(relay, "connected").await;

    let large = Request::post(format!("http://{relay}/api/v1/echo"))
        .body(Full::new(Bytes::from(vec![b'x'; 2048])))
        .unwrap();
    let (status, _, body) = call(large).await;
    assert_eq!(
        (status, &body["error"]["code"]),
        (StatusCode::PAYLOAD_TOO_LARGE, &json!("request_too_large"))
    );

    let (status, _, body) = get_json(relay, "/api/v1/slow").await;
    assert_eq!(
        (status, &body["error"]["code"]),
        (StatusCode::GATEWAY_TIMEOUT, &json!("controller_timeout"))
    );

    let (status, _, body) = get_json(relay, "/api/v1/large").await;
    assert_eq!(
        (status, &body["error"]["code"]),
        (StatusCode::BAD_GATEWAY, &json!("response_too_large"))
    );
}

#[tokio::test]
async fn a_link_with_the_wrong_token_is_refused() {
    let (relay, api) = (
        relay(serve::Limits::default()).await,
        listen(fake_api()).await,
    );
    let mut config = link_config(relay, api);
    config.token = "f".repeat(32);
    let mut established = false;
    let error = link::session(&config, &mut established).await.unwrap_err();
    assert!(!established && error.contains("does not match"), "{error}");
    assert_eq!(get_json(relay, "/health").await.2["link"], "disconnected");
}

#[tokio::test]
async fn a_newer_link_replaces_the_older_one() {
    let (relay, api) = (
        relay(serve::Limits::default()).await,
        listen(fake_api()).await,
    );
    let config = link_config(relay, api);
    let first = tokio::spawn({
        let config = config.clone();
        async move {
            let mut established = false;
            link::session(&config, &mut established).await
        }
    });
    wait_for_link(relay, "connected").await;
    tokio::spawn(link::run(config));
    // The relay closes the first link once the second one says hello.
    let ended = tokio::time::timeout(Duration::from_secs(5), first).await;
    assert!(ended.is_ok(), "the first link is still open");
    wait_for_link(relay, "connected").await;
    assert_eq!(get_json(relay, "/api/v1/x").await.0, StatusCode::OK);
}

#[tokio::test]
async fn an_unreachable_api_answers_bad_gateway() {
    let relay = relay(serve::Limits::default()).await;
    // Nothing listens on the API port yet.
    let spare = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let api = spare.local_addr().unwrap();
    drop(spare);
    tokio::spawn(link::run(link_config(relay, api)));
    wait_for_link(relay, "connected").await;
    let (status, _, body) = get_json(relay, "/api/v1/me").await;
    assert_eq!(
        (status, &body["error"]["code"]),
        (StatusCode::BAD_GATEWAY, &json!("controller_unavailable"))
    );
}

/// The binary itself: `serve` and `link` as processes, set up from the environment.
#[tokio::test]
async fn the_binary_serves_and_links() {
    let api = listen(fake_api()).await;
    let dir = std::env::temp_dir().join(format!("relay-test-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let token = dir.join("token");
    std::fs::write(&token, format!("{TOKEN}\n")).unwrap();
    let spare = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let relay = spare.local_addr().unwrap();
    drop(spare);
    let binary = env!("CARGO_BIN_EXE_node-plane-relay");
    let spawn = |args: &[(&str, String)], command: &str| {
        tokio::process::Command::new(binary)
            .arg(command)
            .envs(args.iter().cloned())
            .kill_on_drop(true)
            .spawn()
            .unwrap()
    };
    let _serve = spawn(
        &[
            ("NODE_PLANE_RELAY_LISTEN", relay.to_string()),
            (
                "NODE_PLANE_RELAY_LINK_TOKEN_FILE",
                token.display().to_string(),
            ),
        ],
        "serve",
    );
    let _link = spawn(
        &[
            ("NODE_PLANE_RELAY_URL", format!("http://{relay}")),
            ("NODE_PLANE_RELAY_TOKEN_FILE", token.display().to_string()),
            ("NODE_PLANE_API_URL", format!("http://{api}")),
        ],
        "link",
    );
    for _ in 0..200 {
        if tokio::net::TcpStream::connect(relay).await.is_ok() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    wait_for_link(relay, "connected").await;
    let (status, _, body) = get_json(relay, "/api/v1/me").await;
    assert_eq!(
        (status, &body["headers"]["x-node-plane-via"]),
        (StatusCode::OK, &json!("relay"))
    );

    std::fs::write(&token, "short").unwrap();
    let refused = tokio::process::Command::new(binary)
        .arg("serve")
        .env("NODE_PLANE_RELAY_LINK_TOKEN_FILE", &token)
        .output()
        .await
        .unwrap();
    assert_eq!(refused.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&refused.stderr).contains("at least 32 characters"));
    std::fs::remove_dir_all(&dir).unwrap();
}
