//! `serve`, `link` and a fake controller API on loopback ports.
use std::{
    net::SocketAddr,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use axum::{
    Json, Router,
    body::{Body, Bytes},
    http::{HeaderMap, Request, StatusCode},
    response::IntoResponse,
    routing::{any, get},
};
use http_body_util::{BodyExt, Full};
use hyper_util::{client::legacy::Client, rt::TokioExecutor};
use node_plane_relay::{keys, link, serve, tls};
use ring::signature::Ed25519KeyPair;
use serde_json::{Value, json};

/// A relay's state directory with the controller's key pinned, and the
/// controller's key.
struct Pair {
    dir: PathBuf,
    relay_key: Vec<u8>,
    link_key: Arc<Ed25519KeyPair>,
}

impl Pair {
    fn new() -> Self {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let dir = std::env::temp_dir().join(format!(
            "relay-test-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let relay = keys::load_or_create(&dir.join("relay.key")).unwrap();
        let link = keys::load_or_create(&dir.join("link.key")).unwrap();
        std::fs::write(dir.join("link.pub"), keys::hex(&keys::public(&link))).unwrap();
        Self {
            relay_key: keys::public(&relay),
            link_key: Arc::new(link),
            dir,
        }
    }
}

impl Drop for Pair {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

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

async fn relay(pair: &Pair, limits: serve::Limits) -> SocketAddr {
    listen(serve::router(serve::Config {
        key: keys::load_or_create(&pair.dir.join("relay.key")).unwrap(),
        state_dir: pair.dir.clone(),
        limits,
    }))
    .await
}

fn link_config(pair: &Pair, relay: SocketAddr, api: SocketAddr) -> link::Config {
    link::Config {
        relay: format!("http://{relay}"),
        relay_key: pair.relay_key.clone(),
        key: pair.link_key.clone(),
        api: format!("http://{api}"),
        ca: None,
        limits: link::Limits {
            response_body: 16 << 10,
            ..link::Limits::default()
        },
        status: Default::default(),
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
    let pair = Pair::new();
    let relay = relay(&pair, serve::Limits::default()).await;
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
    let pair = Pair::new();
    let (relay, api) = (
        relay(&pair, serve::Limits::default()).await,
        listen(fake_api()).await,
    );
    let config = link_config(&pair, relay, api);
    let status = config.status.clone();
    tokio::spawn(link::run(config));
    wait_for_link(relay, "connected").await;

    // The controller's side knows it too.
    for _ in 0..100 {
        if status.lock().unwrap().connected {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert!(status.lock().unwrap().connected);
    assert!(status.lock().unwrap().since > 0);

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
    let recorded: Value =
        serde_json::from_slice(&std::fs::read(pair.dir.join("status.json")).unwrap()).unwrap();
    assert_eq!(recorded["link"], "connected");
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
    let pair = Pair::new();
    let limits = serve::Limits {
        request_body: 1024,
        timeout: Duration::from_millis(300),
        ..serve::Limits::default()
    };
    let (relay, api) = (relay(&pair, limits).await, listen(fake_api()).await);
    tokio::spawn(link::run(link_config(&pair, relay, api)));
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

async fn refused(config: &link::Config) -> String {
    let mut established = false;
    let error = link::session(config, &mut established).await.unwrap_err();
    assert!(!established, "{error}");
    error
}

#[tokio::test]
async fn only_the_pinned_keys_attach() {
    let pair = Pair::new();
    let (relay, api) = (
        relay(&pair, serve::Limits::default()).await,
        listen(fake_api()).await,
    );
    let stranger = Pair::new();

    // Another controller's key.
    let mut config = link_config(&pair, relay, api);
    config.key = stranger.link_key.clone();
    let error = refused(&config).await;
    assert!(error.contains("not the controller's"), "{error}");

    // A server that is not the pinned relay.
    let mut config = link_config(&pair, relay, api);
    config.relay_key = stranger.relay_key.clone();
    let error = refused(&config).await;
    assert!(error.contains("did not prove its key"), "{error}");

    // No controller pinned on the relay.
    std::fs::remove_file(pair.dir.join("link.pub")).unwrap();
    let error = refused(&link_config(&pair, relay, api)).await;
    assert!(error.contains("not the controller's"), "{error}");
    assert_eq!(get_json(relay, "/health").await.2["link"], "disconnected");
}

#[tokio::test]
async fn a_newer_link_replaces_the_older_one() {
    let pair = Pair::new();
    let (relay, api) = (
        relay(&pair, serve::Limits::default()).await,
        listen(fake_api()).await,
    );
    let config = link_config(&pair, relay, api);
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
    let pair = Pair::new();
    let relay = relay(&pair, serve::Limits::default()).await;
    // Nothing listens on the API port yet.
    let spare = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let api = spare.local_addr().unwrap();
    drop(spare);
    tokio::spawn(link::run(link_config(&pair, relay, api)));
    wait_for_link(relay, "connected").await;
    let (status, _, body) = get_json(relay, "/api/v1/me").await;
    assert_eq!(
        (status, &body["error"]["code"]),
        (StatusCode::BAD_GATEWAY, &json!("controller_unavailable"))
    );
}

/// The binary itself: `serve` and `link` as processes, set up from the
/// environment, with keys made by `key`.
#[tokio::test]
async fn the_binary_serves_and_links() {
    let api = listen(fake_api()).await;
    let pair = Pair::new();
    let binary = env!("CARGO_BIN_EXE_node-plane-relay");
    let key = |path: PathBuf| {
        let output = std::process::Command::new(binary)
            .arg("key")
            .arg(path)
            .output()
            .unwrap();
        assert!(output.status.success());
        String::from_utf8(output.stdout).unwrap().trim().to_string()
    };
    // The relay's key is new; the controller's is the pinned one.
    let relay_key = key(pair.dir.join("state/relay.key"));
    std::fs::copy(pair.dir.join("link.pub"), pair.dir.join("state/link.pub")).unwrap();
    assert_eq!(
        key(pair.dir.join("link.key")),
        keys::hex(&keys::public(&pair.link_key))
    );

    let spare = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let relay = spare.local_addr().unwrap();
    drop(spare);
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
                "NODE_PLANE_RELAY_STATE_DIR",
                pair.dir.join("state").display().to_string(),
            ),
        ],
        "serve",
    );
    let _link = spawn(
        &[
            ("NODE_PLANE_RELAY_URL", format!("http://{relay}")),
            ("NODE_PLANE_RELAY_KEY", relay_key),
            (
                "NODE_PLANE_RELAY_LINK_KEY_FILE",
                pair.dir.join("link.key").display().to_string(),
            ),
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

    let refused = tokio::process::Command::new(binary)
        .arg("link")
        .env("NODE_PLANE_RELAY_URL", "http://127.0.0.1:9")
        .env("NODE_PLANE_RELAY_KEY", "00")
        .output()
        .await
        .unwrap();
    assert_eq!(refused.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&refused.stderr).contains("not a public key"));
}

/// Naming the pinned key is not enough: the Hello must be signed with it.
#[tokio::test]
async fn a_forged_hello_does_not_attach() {
    use node_plane_relay::proto::{Hello, LinkMessage, link_client::LinkClient, link_message};
    let pair = Pair::new();
    let relay = relay(&pair, serve::Limits::default()).await;
    let mut client = LinkClient::connect(format!("http://{relay}"))
        .await
        .unwrap();
    let (tx, rx) = tokio::sync::mpsc::channel(4);
    let mut request = tonic::Request::new(tokio_stream::wrappers::ReceiverStream::new(rx));
    let metadata = request.metadata_mut();
    metadata.insert(
        node_plane_relay::LINK_KEY,
        keys::hex(&keys::public(&pair.link_key)).parse().unwrap(),
    );
    metadata.insert(
        node_plane_relay::LINK_NONCE,
        keys::hex(&[7; 32]).parse().unwrap(),
    );
    let mut inbound = client.attach(request).await.unwrap().into_inner();
    assert!(inbound.message().await.unwrap().is_some(), "no challenge");
    let forged = Pair::new();
    let hello = LinkMessage {
        message: Some(link_message::Message::Hello(Hello {
            version: "x".into(),
            signature: keys::sign(&forged.link_key, keys::ATTACH_LINK, &[&[0; 32]]),
        })),
    };
    tx.send(hello).await.unwrap();
    // The relay drops the stream instead of serving it.
    let ended = tokio::time::timeout(Duration::from_secs(5), inbound.message()).await;
    assert!(matches!(ended, Ok(Ok(None)) | Ok(Err(_))), "{ended:?}");
    assert_eq!(get_json(relay, "/health").await.2["link"], "disconnected");
}

/// `files` mode: the cabinet and the link share one TLS port.
#[tokio::test]
async fn the_relay_serves_tls_from_files() {
    use rcgen::{BasicConstraints, CertificateParams, IsCa, KeyPair};
    use tokio_rustls::{
        TlsConnector,
        rustls::{
            ClientConfig, RootCertStore, crypto::ring::default_provider, pki_types::ServerName,
        },
    };

    let pair = Pair::new();
    let mut authority = CertificateParams::new(Vec::<String>::new()).unwrap();
    authority.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    let authority_key = KeyPair::generate().unwrap();
    let authority = authority.self_signed(&authority_key).unwrap();
    let key = KeyPair::generate().unwrap();
    let certificate = CertificateParams::new(vec!["localhost".to_string()])
        .unwrap()
        .signed_by(&key, &authority, &authority_key)
        .unwrap();
    std::fs::write(pair.dir.join("tls.crt"), certificate.pem()).unwrap();
    std::fs::write(pair.dir.join("tls.key"), key.serialize_pem()).unwrap();

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let router = serve::router(serve::Config {
        key: keys::load_or_create(&pair.dir.join("relay.key")).unwrap(),
        state_dir: pair.dir.clone(),
        limits: serve::Limits::default(),
    });
    let mode = tls::Mode::Files {
        certificate: pair.dir.join("tls.crt"),
        key: pair.dir.join("tls.key"),
    };
    tokio::spawn(async move { tls::serve(listener, router, mode).await.unwrap() });

    let api = listen(fake_api()).await;
    let mut config = link_config(&pair, "127.0.0.1:1".parse().unwrap(), api);
    config.relay = format!("https://localhost:{port}");
    config.ca = Some(authority.pem().into_bytes());
    tokio::spawn(link::run(config));

    // A browser: HTTP/1.1 over TLS, trusting the same authority.
    let mut roots = RootCertStore::empty();
    roots.add(authority.der().clone()).unwrap();
    let client = ClientConfig::builder_with_provider(Arc::new(default_provider()))
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_root_certificates(roots)
        .with_no_client_auth();
    let connector = TlsConnector::from(Arc::new(client));
    let get = |path: &'static str| {
        let connector = connector.clone();
        async move {
            let tcp = tokio::net::TcpStream::connect(("127.0.0.1", port))
                .await
                .unwrap();
            let name = ServerName::try_from("localhost").unwrap();
            let tls = connector.connect(name, tcp).await.unwrap();
            let (mut sender, connection) =
                hyper::client::conn::http1::handshake(hyper_util::rt::TokioIo::new(tls))
                    .await
                    .unwrap();
            tokio::spawn(connection);
            let request = Request::get(path)
                .header("host", "localhost")
                .body(Full::<Bytes>::default())
                .unwrap();
            let response = sender.send_request(request).await.unwrap();
            let status = response.status();
            let body = response.into_body().collect().await.unwrap().to_bytes();
            (status, serde_json::from_slice::<Value>(&body).unwrap())
        }
    };
    for _ in 0..100 {
        if get("/health").await.1["link"] == "connected" {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let (status, body) = get("/api/v1/me").await;
    assert_eq!(
        (status, &body["headers"]["x-node-plane-via"]),
        (StatusCode::OK, &json!("relay"))
    );
}
