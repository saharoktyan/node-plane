use std::fs;
use std::net::SocketAddr;
use std::time::{Duration, Instant};

mod api;
mod bot;
mod cabinet;
mod config;
mod enforcement;
mod enrollment;
mod entry;
mod fencing;
mod host;
mod intents;
mod journal;
mod lifecycle;
mod profiles;
mod protocols;
mod relay;
mod runtime;
mod shaping;
mod state;
mod update;

use chrono::Utc;
use tonic::transport::{Certificate, Identity, Server, ServerTlsConfig};

pub mod agent {
    pub mod v1 {
        tonic::include_proto!("nodeplane.agent.v1");
    }
}

use agent::v1::node_agent_service_server::NodeAgentServiceServer;
use api::NodeAgentApi;
use config::AgentConfig;
use host::metrics as host_metrics;
use state::AgentState;

fn print_startup(config: &AgentConfig) {
    println!("node-plane-agent starting");
    println!("node_key={}", config.node_key);
    println!("listen_addr={}", config.listen_addr);
    println!("runtime_root={}", config.runtime_root);
    println!("state_dir={}", config.state_dir);
    println!("log_dir={}", config.log_dir);
    println!("node_env_path={}", config.node_env_path);
    println!("xray_config_path={}", config.xray_config_path);
    println!("awg_config_path={}", config.awg_config_path);
    println!("heartbeat_seconds={}", config.heartbeat_seconds);
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if let Some(code) = host::hardening::run(&args) {
        std::process::exit(code);
    }
    if let Some(code) = relay::run(&args).await {
        std::process::exit(code);
    }
    if let Some(code) = update::run(&args) {
        std::process::exit(code);
    }
    if let Some(code) = protocols::awg::profile::run(&args) {
        std::process::exit(code);
    }
    if let Some(code) = entry::run(&args) {
        std::process::exit(code);
    }
    if let Some(code) = protocols::xray::api::cli::run(&args).await {
        std::process::exit(code);
    }
    if let Some(code) = protocols::xray::user_sync::run(&args).await {
        std::process::exit(code);
    }
    if let Some(code) = protocols::traffic::run(&args).await {
        std::process::exit(code);
    }
    if let Some(code) = protocols::inspection::run(&args).await {
        std::process::exit(code);
    }
    if let Some(code) = protocols::awg::peer_sync::run(&args) {
        std::process::exit(code);
    }
    if let Some(code) = protocols::awg::entropy::run(&args) {
        std::process::exit(code);
    }
    if let Some(code) = protocols::xray::setup::run(&args) {
        std::process::exit(code);
    }
    if let Some(code) = protocols::awg::setup::run(&args) {
        std::process::exit(code);
    }
    if let Some(code) = intents::operation::run_command(&args) {
        std::process::exit(code);
    }
    if let Some(code) = intents::run(&args) {
        std::process::exit(code);
    }
    if let Some(code) = protocols::deploy::run(&args) {
        std::process::exit(code);
    }
    if let Some(code) = protocols::settings::run(&args) {
        std::process::exit(code);
    }
    let config = AgentConfig::load();
    relay::follow_update(&relay::System, &relay::Paths::host());
    // Installed agents of earlier releases get the hardening on their next start.
    if let Err(err) = host::hardening::ensure() {
        eprintln!("agent service hardening not written: {err}");
    }
    if enrollment::pending(&config) {
        enrollment::serve(&config).await?;
    }
    // Lease units of earlier releases ran the removed script; the wrapper
    // files go once no unit runs them.
    match intents::scheduler::migrate(
        &intents::leases::System,
        std::path::Path::new(intents::scheduler::UNITS),
    ) {
        Ok(()) => entry::remove_delivered(std::path::Path::new(&config.runtime_root)),
        Err(err) => eprintln!("lease unit migration failed: {err}"),
    }
    let state = AgentState::new(config.clone());
    let api = NodeAgentApi {
        state: state.clone(),
    };
    let addr: SocketAddr = config.listen_addr.parse()?;
    print_startup(&config);

    tokio::spawn(enforcement::run(state.clone()));

    let sampler = state.cpu.clone();
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(Duration::from_secs(host_metrics::SAMPLE_SECONDS));
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            ticker.tick().await;
            let raw = fs::read_to_string("/proc/stat").ok();
            if let Ok(mut sampler) = sampler.lock() {
                sampler.observe(raw.as_deref(), Instant::now());
            }
        }
    });

    let mut ticker = tokio::time::interval(Duration::from_secs(config.heartbeat_seconds));

    tokio::spawn(async move {
        loop {
            ticker.tick().await;
            state.mark_heartbeat();
            println!(
                "heartbeat node_key={} ts={}",
                state.config.node_key,
                Utc::now().to_rfc3339(),
            );
        }
    });

    let server_identity = Identity::from_pem(
        fs::read(&config.tls_certificate_path)?,
        fs::read(&config.tls_key_path)?,
    );
    let client_ca = Certificate::from_pem(fs::read(&config.tls_client_ca_path)?);
    let tls = ServerTlsConfig::new()
        .identity(server_identity)
        .client_ca_root(client_ca);

    let listener = tokio::net::TcpListener::bind(addr).await?;
    // Serving with this binary is what confirms a self-update.
    update::confirm(&config);
    let incoming = tonic::transport::server::TcpIncoming::from_listener(listener, true, None)
        .map_err(|err| err as Box<dyn std::error::Error>)?;
    Server::builder()
        .tls_config(tls)?
        // The web cabinet's files come in one message (see src/cabinet).
        .add_service(NodeAgentServiceServer::new(api).max_decoding_message_size(48 << 20))
        .serve_with_incoming_shutdown(incoming, async {
            let _ = tokio::signal::ctrl_c().await;
            println!("node-plane-agent stopping");
        })
        .await?;

    Ok(())
}
