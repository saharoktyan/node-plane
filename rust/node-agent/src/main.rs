use std::env;
use std::fs;
use std::path::Path;
use std::time::Duration;

use chrono::Utc;
use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
struct AgentConfig {
    node_key: String,
    listen_addr: String,
    heartbeat_seconds: u64,
    runtime_root: String,
    state_dir: String,
    log_dir: String,
}

impl Default for AgentConfig {
    fn default() -> Self {
        Self {
            node_key: env::var("NODE_AGENT_NODE_KEY")
                .ok()
                .filter(|value| !value.trim().is_empty())
                .unwrap_or_else(|| "unknown-node".to_string()),
            listen_addr: env::var("NODE_AGENT_LISTEN_ADDR")
                .ok()
                .filter(|value| !value.trim().is_empty())
                .unwrap_or_else(|| "127.0.0.1:50061".to_string()),
            heartbeat_seconds: env::var("NODE_AGENT_HEARTBEAT_SECONDS")
                .ok()
                .and_then(|value| value.parse::<u64>().ok())
                .filter(|value| *value > 0)
                .unwrap_or(30),
            runtime_root: "/opt/node-plane-runtime".to_string(),
            state_dir: "/var/lib/node-plane-agent".to_string(),
            log_dir: "/var/log/node-plane-agent".to_string(),
        }
    }
}

impl AgentConfig {
    fn load() -> Self {
        let path = env::var("NODE_AGENT_CONFIG_PATH")
            .ok()
            .filter(|value| !value.trim().is_empty())
            .unwrap_or_else(|| "/etc/node-plane/agent.toml".to_string());
        if !Path::new(&path).is_file() {
            return Self::default();
        }
        let Ok(raw) = fs::read_to_string(&path) else {
            return Self::default();
        };
        toml::from_str(&raw).unwrap_or_else(|_| Self::default())
    }
}

fn print_startup(config: &AgentConfig) {
    println!("node-plane-agent starting");
    println!("node_key={}", config.node_key);
    println!("listen_addr={}", config.listen_addr);
    println!("runtime_root={}", config.runtime_root);
    println!("state_dir={}", config.state_dir);
    println!("log_dir={}", config.log_dir);
    println!("heartbeat_seconds={}", config.heartbeat_seconds);
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let config = AgentConfig::load();
    print_startup(&config);

    let mut ticker = tokio::time::interval(Duration::from_secs(config.heartbeat_seconds));

    loop {
        tokio::select! {
            _ = ticker.tick() => {
                println!(
                    "heartbeat node_key={} ts={}",
                    config.node_key,
                    Utc::now().to_rfc3339(),
                );
            }
            signal = tokio::signal::ctrl_c() => {
                signal?;
                println!("node-plane-agent stopping");
                break;
            }
        }
    }

    Ok(())
}
