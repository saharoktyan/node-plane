use std::env;

use node_plane_relay::{link, read_token, serve};

const USAGE: &str = "usage: node-plane-relay serve | link | --version

serve  NODE_PLANE_RELAY_LISTEN (default 127.0.0.1:8443),
       NODE_PLANE_RELAY_LINK_TOKEN_FILE
link   NODE_PLANE_RELAY_URL, NODE_PLANE_RELAY_TOKEN_FILE,
       NODE_PLANE_API_URL (default http://127.0.0.1:8080)";

fn setting(name: &str) -> Result<String, String> {
    env::var(name).map_err(|_| format!("{name} is not set"))
}

#[tokio::main]
async fn main() {
    let command = env::args().nth(1).unwrap_or_default();
    let result = match command.as_str() {
        "serve" => run_serve().await,
        "link" => run_link().await,
        "--version" => {
            println!(
                "node-plane-relay {} ({})",
                node_plane_relay::VERSION,
                env!("NODE_PLANE_BINARY_COMMIT")
            );
            Ok(())
        }
        _ => Err(USAGE.to_string()),
    };
    if let Err(error) = result {
        eprintln!("{error}");
        std::process::exit(2);
    }
}

async fn run_serve() -> Result<(), String> {
    let listen =
        env::var("NODE_PLANE_RELAY_LISTEN").unwrap_or_else(|_| "127.0.0.1:8443".to_string());
    let token = read_token(&setting("NODE_PLANE_RELAY_LINK_TOKEN_FILE")?)?;
    let listener = tokio::net::TcpListener::bind(&listen)
        .await
        .map_err(|e| format!("{listen}: {e}"))?;
    eprintln!("relay listening on {listen}");
    let router = serve::router(serve::Config {
        token,
        limits: serve::Limits::default(),
    });
    // The link stream never ends by itself, so stop without draining it.
    tokio::select! {
        result = axum::serve(listener, router) => result.map_err(|e| e.to_string()),
        _ = tokio::signal::ctrl_c() => Ok(()),
    }
}

async fn run_link() -> Result<(), String> {
    let config = link::Config {
        relay: setting("NODE_PLANE_RELAY_URL")?,
        token: read_token(&setting("NODE_PLANE_RELAY_TOKEN_FILE")?)?,
        api: env::var("NODE_PLANE_API_URL").unwrap_or_else(|_| "http://127.0.0.1:8080".to_string()),
        limits: link::Limits::default(),
    };
    tokio::select! {
        () = link::run(config) => Ok(()),
        _ = tokio::signal::ctrl_c() => Ok(()),
    }
}
