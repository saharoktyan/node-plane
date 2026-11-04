//! The Xray server config: creating it with new REALITY keys, and reading
//! the public parameters clients need. Replaces `init-xray.sh` and
//! `sync-xray.sh`, which only start `node-plane-agent xray-config init|public`
//! for the profile intent journal.
//!
//! Keys come from the `xray x25519` command of the release image, the same
//! code the server runs.
use std::fs;
use std::path::Path;
use std::process::Command;

use serde_json::{Value, json};

use super::user_sync::{VISION, write_config};
use crate::runtime::images::Kind;

const TCP_TAG: &str = "reality-tcp";
const XHTTP_TAG: &str = "reality-xhttp";

fn valid_key(value: &str) -> bool {
    value.len() == 43
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-'))
}

/// The REALITY password (public key) in `xray x25519` output: `Password`,
/// `Password (PublicKey)` or, in older releases, `Public key`.
fn password(output: &str) -> Option<String> {
    output
        .lines()
        .find_map(|line| {
            let (label, value) = line.trim().split_once(':')?;
            let label = label
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
                .to_lowercase();
            let value = value.trim();
            let known = matches!(
                label.as_str(),
                "password" | "password (publickey)" | "public key" | "publickey"
            );
            (known && !value.is_empty() && !value.contains(char::is_whitespace))
                .then(|| value.to_string())
        })
        .filter(|value| valid_key(value))
}

/// The private key in `xray x25519` output, on its label line or the next.
fn private_key(output: &str) -> Option<String> {
    let lines: Vec<&str> = output.lines().map(str::trim).collect();
    for (index, line) in lines.iter().enumerate() {
        let lower = line.to_lowercase();
        for prefix in ["private key", "privatekey"] {
            if !lower.starts_with(prefix) {
                continue;
            }
            let tail = line[prefix.len()..].trim_start_matches(':').trim();
            let value = if tail.is_empty() {
                lines[index + 1..]
                    .iter()
                    .find(|next| !next.is_empty())
                    .copied()?
            } else {
                tail
            };
            return valid_key(value).then(|| value.to_string());
        }
    }
    None
}

fn docker(args: &[&str]) -> Result<String, String> {
    let output = Command::new("docker")
        .args(args)
        .output()
        .map_err(|err| format!("docker failed: {err}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let stdout = String::from_utf8_lossy(&output.stdout);
        let detail = if stderr.trim().is_empty() {
            stdout
        } else {
            stderr
        };
        return Err(detail.trim().to_string());
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

fn image_or_release(image: &str) -> String {
    if image.trim().is_empty() {
        Kind::Xray.release().to_string()
    } else {
        image.to_string()
    }
}

/// What clients need of the server, with the host and flow they connect with.
fn metadata(
    host: &str,
    sni: &str,
    password: &str,
    short_id: &str,
    ports: (u64, u64),
    path: &str,
    flow: &str,
) -> Value {
    json!({
        "xray_host": host,
        "xray_sni": sni,
        "xray_pbk": password,
        "xray_sid": short_id,
        "xray_short_id": short_id,
        "xray_tcp_port": ports.0,
        "xray_xhttp_port": ports.1,
        "xray_xhttp_path_prefix": path,
        "xray_flow": flow,
        "xray_fp": "chrome",
    })
}

/// The new server config: the API, a Vision inbound and an XHTTP inbound
/// sharing one REALITY identity.
fn server_config(sni: &str, private: &str, short_id: &str, ports: (u16, u16), path: &str) -> Value {
    let reality = json!({
        "show": false,
        "dest": format!("{sni}:443"),
        "xver": 0,
        "serverNames": [sni],
        "privateKey": private,
        "shortIds": [short_id],
    });
    json!({
        "log": {"loglevel": "warning"},
        "stats": {},
        "policy": {"levels": {"0": {
            "statsUserUplink": true,
            "statsUserDownlink": true,
            "statsUserOnline": true,
        }}},
        "api": {"tag": "api", "services": ["StatsService", "HandlerService", "RoutingService"]},
        "inbounds": [
            {
                "tag": "api",
                "listen": "127.0.0.1",
                "port": 10085,
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
            },
            {
                "tag": TCP_TAG,
                "listen": "0.0.0.0",
                "port": ports.0,
                "protocol": "vless",
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": reality},
            },
            {
                "tag": XHTTP_TAG,
                "listen": "0.0.0.0",
                "port": ports.1,
                "protocol": "vless",
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {
                    "network": "xhttp",
                    "security": "reality",
                    "realitySettings": reality,
                    "xhttpSettings": {"path": path},
                },
            },
        ],
        "outbounds": [
            {"protocol": "freedom", "tag": "direct"},
            {"protocol": "freedom", "tag": "api"},
        ],
        "routing": {"rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]},
    })
}

fn short_id() -> Result<String, String> {
    use ring::rand::SecureRandom;
    let mut bytes = [0u8; 8];
    ring::rand::SystemRandom::new()
        .fill(&mut bytes)
        .map_err(|_| "random source unavailable".to_string())?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// `init CONFIG HOST [SNI TCP_PORT XHTTP_PORT PATH FLOW IMAGE]`: writes a
/// new config with new keys and prints its client parameters.
fn init(args: &[String]) -> Result<Value, String> {
    let arg = |index: usize, default: &str| -> String {
        args.get(index)
            .filter(|value| !value.is_empty())
            .cloned()
            .unwrap_or_else(|| default.to_string())
    };
    let config = arg(0, "/opt/node-plane-runtime/xray/config.json");
    let host = arg(1, "");
    if host.is_empty() {
        return Err("PUBLIC_HOST is required".into());
    }
    let sni = arg(2, "www.cloudflare.com");
    let port = |index, default| {
        arg(index, default)
            .parse::<u16>()
            .map_err(|_| "Xray ports must be numbers".to_string())
    };
    let ports = (port(3, "443")?, port(4, "8443")?);
    let (path, flow, image) = (
        arg(5, "/assets"),
        arg(6, VISION),
        image_or_release(&arg(7, "")),
    );
    let config = Path::new(&config);
    if let Some(parent) = config.parent() {
        fs::create_dir_all(parent).map_err(|err| err.to_string())?;
    }
    if config.is_dir() {
        let stamp = chrono::Local::now().format("%Y%m%d-%H%M%S");
        let mut aside = config.as_os_str().to_owned();
        aside.push(format!(".dirbak.{stamp}"));
        fs::rename(config, aside).map_err(|err| err.to_string())?;
    }
    let _ = docker(&["pull", &image]);
    let output = docker(&["run", "--rm", &image, "x25519"])?;
    let (Some(private), Some(password)) = (private_key(&output), password(&output)) else {
        return Err("Could not parse xray x25519 output".into());
    };
    let short_id = short_id()?;
    write_config(
        config,
        &server_config(&sni, &private, &short_id, ports, &path),
    )?;
    Ok(metadata(
        &host,
        &sni,
        &password,
        &short_id,
        (ports.0.into(), ports.1.into()),
        &path,
        &flow,
    ))
}

/// `public CONFIG HOST [FLOW IMAGE]`: the client parameters of an existing
/// config. The key is derived in the running container when there is one.
pub(crate) fn public(
    config: &str,
    host: &str,
    flow: &str,
    image: &str,
    container: &str,
) -> Result<Value, String> {
    if host.is_empty() {
        return Err("PUBLIC_HOST is required".into());
    }
    let raw = fs::read_to_string(config).map_err(|_| format!("Xray config not found: {config}"))?;
    let config: Value = serde_json::from_str(&raw).map_err(|err| err.to_string())?;
    let inbound = |tag: &str| {
        config["inbounds"]
            .as_array()
            .and_then(|inbounds| inbounds.iter().find(|inbound| inbound["tag"] == tag))
            .cloned()
    };
    let (Some(tcp), Some(xhttp)) = (inbound(TCP_TAG), inbound(XHTTP_TAG)) else {
        return Err("Could not find reality-tcp and reality-xhttp inbounds".into());
    };
    let reality = &tcp["streamSettings"]["realitySettings"];
    let private = reality["privateKey"].as_str().unwrap_or_default();
    if private.is_empty() {
        return Err("privateKey is missing in Xray config".into());
    }
    let output = docker(&["exec", container, "xray", "x25519", "-i", private])
        // Offline synchronization starts the image once instead.
        .or_else(|_| {
            docker(&[
                "run",
                "--rm",
                &image_or_release(image),
                "x25519",
                "-i",
                private,
            ])
        })?;
    let password = password(&output).ok_or("Could not derive Reality password from private key")?;
    let first = |key: &str| reality[key][0].as_str().unwrap_or_default().to_string();
    let port = |inbound: &Value, default: u64| {
        inbound["port"]
            .as_u64()
            .filter(|port| *port > 0)
            .unwrap_or(default)
    };
    let path = xhttp["streamSettings"]["xhttpSettings"]["path"]
        .as_str()
        .filter(|path| !path.is_empty())
        .unwrap_or("/assets");
    Ok(metadata(
        host,
        &first("serverNames"),
        &password,
        &first("shortIds"),
        (port(&tcp, 443), port(&xhttp, 8443)),
        path,
        flow,
    ))
}

fn execute(args: &[String]) -> Result<Value, String> {
    let (command, rest) = args
        .split_first()
        .ok_or("usage: xray-config init|public …")?;
    match command.as_str() {
        "init" => init(rest),
        "public" => {
            let arg = |index: usize, default: &str| -> String {
                rest.get(index)
                    .filter(|value| !value.is_empty())
                    .cloned()
                    .unwrap_or_else(|| default.to_string())
            };
            let container = std::env::var("XRAY_CONTAINER_NAME").unwrap_or_else(|_| "xray".into());
            public(
                &arg(0, "/opt/node-plane-runtime/xray/config.json"),
                &arg(1, ""),
                &arg(2, VISION),
                &arg(3, ""),
                &container,
            )
        }
        _ => Err("usage: xray-config init|public …".into()),
    }
}

/// Runs the subcommand when the agent was started as `xray-config …`.
pub(crate) fn run(args: &[String]) -> Option<i32> {
    let (first, rest) = args.split_first()?;
    if first != "xray-config" {
        return None;
    }
    Some(match execute(rest) {
        Ok(value) => {
            println!("{value}");
            0
        }
        Err(err) => {
            eprintln!("{err}");
            1
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const PRIVATE: &str = "aGVsbG8taGVsbG8taGVsbG8taGVsbG8taGVsbG8tYWI";
    const PUBLIC: &str = "cHVibGljLXB1YmxpYy1wdWJsaWMtcHVibGljLXB1Ymw";

    #[test]
    fn keys_are_read_from_current_and_older_x25519_output() {
        let current = format!("PrivateKey: {PRIVATE}\nPassword (PublicKey): {PUBLIC}\nHash32: x\n");
        assert_eq!(private_key(&current).as_deref(), Some(PRIVATE));
        assert_eq!(password(&current).as_deref(), Some(PUBLIC));
        let older = format!("Private key:\n\n{PRIVATE}\nPublic key: {PUBLIC}\n");
        assert_eq!(private_key(&older).as_deref(), Some(PRIVATE));
        assert_eq!(password(&older).as_deref(), Some(PUBLIC));
        assert_eq!(password(&format!("Password: {PUBLIC} extra")), None);
        assert_eq!(password("Password: short"), None);
        assert_eq!(private_key("PrivateKey: short"), None);
    }

    #[test]
    fn a_new_config_shares_one_reality_identity_between_both_inbounds() {
        let config = server_config(
            "www.example.com",
            PRIVATE,
            "0123456789abcdef",
            (443, 8443),
            "/assets",
        );
        let inbounds = config["inbounds"].as_array().unwrap();
        assert_eq!(inbounds[0]["tag"], "api");
        for inbound in &inbounds[1..] {
            let reality = &inbound["streamSettings"]["realitySettings"];
            assert_eq!(reality["privateKey"], PRIVATE);
            assert_eq!(reality["dest"], "www.example.com:443");
            assert_eq!(reality["shortIds"][0], "0123456789abcdef");
        }
        assert_eq!(
            inbounds[2]["streamSettings"]["xhttpSettings"]["path"],
            "/assets"
        );
        assert_eq!(short_id().unwrap().len(), 16);
    }

    #[test]
    fn init_requires_a_host_and_numeric_ports() {
        let args = |items: &[&str]| {
            items
                .iter()
                .map(|item| item.to_string())
                .collect::<Vec<_>>()
        };
        assert!(init(&args(&["/nonexistent/config.json"])).is_err());
        assert!(init(&args(&["/nonexistent/config.json", "host", "sni", "443x"])).is_err());
    }
}
