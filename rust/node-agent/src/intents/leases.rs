//! The runtime of temporary access: the protocol container the lease
//! supervisor owns, and expired identities kept out of its config.
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use serde_json::Value;

use crate::protocols::awg::peer_sync::atomic_write;
use crate::protocols::awg::peers::split_peer;
use crate::protocols::lock_config;
use crate::protocols::xray::user_sync::write_config;
use crate::runtime::files::env_value;

const NODE_ENV: &str = "/etc/node-plane/node.env";

/// Where node.env puts the protocol's config and container.
pub(crate) struct RuntimeSettings {
    pub(crate) config: PathBuf,
    pub(crate) container: String,
    pub(crate) server_key: String,
}

impl RuntimeSettings {
    pub(crate) fn read(protocol: &str) -> Result<Self, String> {
        Self::from_env(protocol, &fs::read_to_string(NODE_ENV).unwrap_or_default())
    }

    fn from_env(protocol: &str, raw: &str) -> Result<Self, String> {
        let value = |key: &str, default: &str| {
            env_value(raw, key)
                .filter(|value| !value.is_empty())
                .unwrap_or_else(|| default.to_string())
        };
        let (config, container) = match protocol {
            "awg" => (
                value(
                    "AWG_CONFIG",
                    "/opt/node-plane-runtime/amnezia-awg/data/wg0.conf",
                ),
                value("AWG_CONTAINER_NAME", "amnezia-awg"),
            ),
            "xray" => (
                value("XRAY_CONFIG", "/opt/node-plane-runtime/xray/config.json"),
                value("XRAY_CONTAINER_NAME", "xray"),
            ),
            _ => return Err("invalid leased protocol".into()),
        };
        let server_key = value("SERVER_KEY", "");
        let name = |text: &str, max: usize, dot: bool| {
            text.len() <= max
                && text.bytes().enumerate().all(|(index, b)| {
                    b.is_ascii_alphanumeric()
                        || (index > 0 && (matches!(b, b'_' | b'-') || (dot && b == b'.')))
                })
        };
        if !Path::new(&config).is_absolute()
            || container.is_empty()
            || !name(&container, 128, true)
            || !name(&server_key, 64, false)
        {
            return Err("invalid leased runtime environment".into());
        }
        Ok(Self {
            config: config.into(),
            container,
            server_key,
        })
    }

    fn destination(protocol: &str) -> &'static str {
        if protocol == "xray" {
            "/etc/xray"
        } else {
            "/opt/amnezia/awg"
        }
    }
}

fn succeeds(program: &str, args: &[&str]) -> bool {
    Command::new(program)
        .args(args)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|status| status.success())
}

/// The protocol's container, when its name, ID and config mount are ours.
pub(crate) fn container(protocol: &str, settings: &RuntimeSettings) -> Result<Value, String> {
    let output = Command::new("docker")
        .args(["container", "inspect", &settings.container])
        .stderr(Stdio::null())
        .output()
        .map_err(|_| "leased runtime is unavailable".to_string())?;
    if !output.status.success() {
        return Err("leased runtime is unavailable".into());
    }
    let records: Value =
        serde_json::from_slice(&output.stdout).map_err(|_| "leased runtime is unavailable")?;
    let [record] = records.as_array().map(Vec::as_slice).unwrap_or_default() else {
        return Err("leased runtime is unavailable".into());
    };
    let source = settings
        .config
        .parent()
        .unwrap_or(Path::new("/"))
        .to_string_lossy();
    let id = record["Id"].as_str().unwrap_or_default();
    let owned = record["Name"].as_str() == Some(&format!("/{}", settings.container))
        && id.len() == 64
        && id.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
        && record["Mounts"].as_array().is_some_and(|mounts| {
            mounts.iter().any(|mount| {
                mount["Type"] == "bind"
                    && mount["Source"].as_str() == Some(&source)
                    && mount["Destination"] == RuntimeSettings::destination(protocol)
            })
        })
        && record["State"]["Running"].is_boolean();
    if !owned {
        return Err("leased container ownership is unconfirmed".into());
    }
    Ok(record.clone())
}

pub(crate) fn running(record: &Value) -> Result<bool, String> {
    record["State"]["Running"]
        .as_bool()
        .ok_or_else(|| "leased container ownership is unconfirmed".into())
}

/// Whether expiry is enforced now: the timer and its scan healthy, and the
/// supervisor running the protocol container, which Docker must not restart
/// on its own.
pub(crate) fn enforcement_ready(protocol: &str) -> bool {
    let units = [
        ("is-enabled", "node-plane-lease-expiry.timer".to_string()),
        ("is-active", "node-plane-lease-expiry.timer".to_string()),
        ("is-active", "node-plane-lease-expiry.service".to_string()),
        (
            "is-enabled",
            format!("node-plane-leased-{protocol}.service"),
        ),
        ("is-active", format!("node-plane-leased-{protocol}.service")),
    ];
    if !units
        .iter()
        .all(|(action, unit)| succeeds("systemctl", &[action, "--quiet", unit]))
    {
        return false;
    }
    // `is-failed` answers 1 for a unit that has not failed.
    let failed = Command::new("systemctl")
        .args(["is-failed", "--quiet", "node-plane-lease-expiry.service"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    if !failed.is_ok_and(|status| status.code() == Some(1)) {
        return false;
    }
    let Ok(settings) = RuntimeSettings::read(protocol) else {
        return false;
    };
    container(protocol, &settings).is_ok_and(|record| {
        record["State"]["Running"] == true && record["HostConfig"]["RestartPolicy"]["Name"] == "no"
    })
}

/// Removes journaled temporary identities from a stopped container's config.
pub(crate) fn prune_config(
    protocol: &str,
    config: &Path,
    names: &[String],
    server_key: &str,
) -> Result<(), String> {
    if names.is_empty() {
        return Ok(());
    }
    if names
        .iter()
        .any(|name| !super::profile::temporary_name(name))
    {
        return Err("invalid temporary identity".into());
    }
    if config.is_symlink() || config.ancestors().skip(1).any(Path::is_symlink) {
        return Err("unsafe leased configuration path".into());
    }
    let _lock = lock_config(config)?;
    let original =
        fs::read_to_string(config).map_err(|err| format!("{}: {err}", config.display()))?;
    match protocol {
        "xray" => {
            let mut value: Value = serde_json::from_str(&original)
                .map_err(|err| format!("{}: {err}", config.display()))?;
            let mut changed = false;
            for inbound in value["inbounds"]
                .as_array_mut()
                .ok_or("Xray inbounds are missing")?
            {
                if inbound["protocol"] != "vless" {
                    continue;
                }
                let clients = inbound["settings"]["clients"]
                    .as_array_mut()
                    .ok_or("Xray clients are missing")?;
                let before = clients.len();
                clients.retain(|client| {
                    !names.iter().any(|name| {
                        client["email"].as_str() == Some(name)
                            || client["name"].as_str() == Some(name)
                    })
                });
                changed |= clients.len() != before;
            }
            if changed {
                write_config(config, &value)?;
            }
        }
        "awg" => {
            let mut updated = original.clone();
            for name in names {
                let peer = if server_key.is_empty() {
                    name.clone()
                } else {
                    format!("{server_key}-{name}")
                };
                updated = split_peer(&updated, &peer).0;
            }
            if updated != original {
                atomic_write(config, &updated)?;
            }
        }
        _ => return Err("invalid leased protocol".into()),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runtime_settings_follow_node_env_and_are_checked() {
        let settings =
            RuntimeSettings::from_env("awg", "SERVER_KEY=n1\nAWG_CONTAINER_NAME=awg\n").unwrap();
        assert_eq!(settings.container, "awg");
        assert_eq!(settings.server_key, "n1");
        assert_eq!(
            settings.config,
            Path::new("/opt/node-plane-runtime/amnezia-awg/data/wg0.conf")
        );
        assert!(RuntimeSettings::from_env("xray", "XRAY_CONFIG=relative.json\n").is_err());
        assert!(RuntimeSettings::from_env("xray", "SERVER_KEY=-bad\n").is_err());
        assert!(RuntimeSettings::from_env("other", "").is_err());
    }

    #[test]
    fn only_temporary_identities_leave_the_config() {
        let dir = crate::protocols::test_dir("lease-prune");
        let name = format!("tmp_{}", "b".repeat(32));
        let awg = dir.join("wg0.conf");
        let text = format!(
            "[Interface]\nPrivateKey = s\n\n# n1-{name}\n[Peer]\nPublicKey = t\n\n# n1-keep\n[Peer]\nPublicKey = k\n"
        );
        fs::write(&awg, &text).unwrap();
        prune_config("awg", &awg, std::slice::from_ref(&name), "n1").unwrap();
        let pruned = fs::read_to_string(&awg).unwrap();
        assert!(!pruned.contains(&name) && pruned.contains("# n1-keep"));
        assert!(prune_config("awg", &awg, &["keep".to_string()], "n1").is_err());

        let xray = dir.join("config.json");
        let config = serde_json::json!({"inbounds": [{"protocol": "vless", "settings": {"clients": [
            {"email": name, "id": "a"}, {"email": "keep", "id": "b"}]}}]});
        fs::write(&xray, config.to_string()).unwrap();
        prune_config("xray", &xray, std::slice::from_ref(&name), "").unwrap();
        let pruned: Value = serde_json::from_str(&fs::read_to_string(&xray).unwrap()).unwrap();
        assert_eq!(
            pruned["inbounds"][0]["settings"]["clients"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
    }
}
