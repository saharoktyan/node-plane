use std::{env, path::PathBuf, sync::Arc};

use node_plane_relay::{keys, link, serve};

const USAGE: &str = "usage: node-plane-relay serve | link | key PATH | --version

serve     NODE_PLANE_RELAY_LISTEN (default 127.0.0.1:8443),
          NODE_PLANE_RELAY_STATE_DIR (default /var/lib/node-plane-relay):
          relay.key (created when missing), link.pub (the controller's key)
link      NODE_PLANE_RELAY_URL, NODE_PLANE_RELAY_KEY (the relay's key, hex),
          NODE_PLANE_RELAY_LINK_KEY_FILE (created when missing),
          NODE_PLANE_API_URL (default http://127.0.0.1:8080)
key PATH  prints the public half of the key at PATH, creating it when missing";

fn setting(name: &str) -> Result<String, String> {
    env::var(name).map_err(|_| format!("{name} is not set"))
}

#[tokio::main]
async fn main() {
    let command = env::args().nth(1).unwrap_or_default();
    let result = match command.as_str() {
        "serve" => run_serve().await,
        "link" => run_link().await,
        "key" => match env::args().nth(2) {
            Some(path) => keys::load_or_create(path.as_ref()).map(|key| {
                println!("{}", keys::hex(&keys::public(&key)));
            }),
            None => Err(USAGE.to_string()),
        },
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
    let state = PathBuf::from(
        env::var("NODE_PLANE_RELAY_STATE_DIR")
            .unwrap_or_else(|_| "/var/lib/node-plane-relay".to_string()),
    );
    let key = keys::load_or_create(&state.join("relay.key"))?;
    let listener = tokio::net::TcpListener::bind(&listen)
        .await
        .map_err(|e| format!("{listen}: {e}"))?;
    eprintln!("relay listening on {listen}");
    let router = serve::router(serve::Config {
        key,
        link_key_path: state.join("link.pub"),
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
        relay_key: keys::unhex(&setting("NODE_PLANE_RELAY_KEY")?)
            .filter(|key| key.len() == keys::KEY_LEN)
            .ok_or("NODE_PLANE_RELAY_KEY is not a public key in hex")?,
        key: Arc::new(keys::load_or_create(
            setting("NODE_PLANE_RELAY_LINK_KEY_FILE")?.as_ref(),
        )?),
        api: env::var("NODE_PLANE_API_URL").unwrap_or_else(|_| "http://127.0.0.1:8080".to_string()),
        limits: link::Limits::default(),
    };
    tokio::select! {
        () = link::run(config) => Ok(()),
        _ = tokio::signal::ctrl_c() => Ok(()),
    }
}
