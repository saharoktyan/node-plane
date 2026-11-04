//! The AWG server config: creating it, and regenerating its obfuscation
//! parameters. Replaces `init-awg.sh` and `regenerate-awg-entropy.sh`, which
//! only start `node-plane-agent awg-config init|regenerate` for the profile
//! intent journal.
//!
//! Keys and parameters come from `profile`.
use std::fs;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use super::{entropy, profile};
use crate::protocols::deploy::prepare_leased;
use crate::protocols::{Setting, process_setting};

fn config_path(setting: Setting<'_>) -> PathBuf {
    setting(
        "AWG_CONFIG",
        "/opt/node-plane-runtime/amnezia-awg/data/wg0.conf",
    )
    .into()
}

/// The interface address, with the tunnel's IPv6 network unless it has one.
fn server_address(value: &str) -> String {
    if value.contains(':') {
        value.to_string()
    } else {
        format!("{value}, fd42:6e70:6177:2::1/64")
    }
}

/// Writes a new config with a new server key and parameters; an existing,
/// non-empty config is kept with its identities.
fn init(setting: Setting<'_>) -> Result<String, String> {
    let config = config_path(setting);
    let dir = config.parent().unwrap_or(Path::new("."));
    fs::create_dir_all(dir).map_err(|err| err.to_string())?;
    if fs::metadata(&config).is_ok_and(|meta| meta.len() > 0) {
        return Ok(format!("AWG config already exists: {}", config.display()));
    }
    let parameters = profile::init_lines(&setting("AWG_I1_PRESET", "quic"))?;
    let text = format!(
        "[Interface]\nPrivateKey = {}\nAddress = {}\nListenPort = {}\n{parameters}",
        profile::new_private_key(),
        server_address(&setting("AWG_SERVER_ADDRESS", "10.8.1.0/24")),
        setting("AWG_SERVER_PORT", "51820"),
    );
    let staged = dir.join(format!(".awg-init-{}", std::process::id()));
    let result = (|| {
        let mut file = fs::File::create(&staged).map_err(|err| err.to_string())?;
        file.set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|err| err.to_string())?;
        file.write_all(text.as_bytes())
            .map_err(|err| err.to_string())?;
        profile::validate_file(&staged)?;
        fs::rename(&staged, &config).map_err(|err| err.to_string())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&staged);
    }
    result?;
    Ok(format!("AWG 3.1 config initialized: {}", config.display()))
}

fn docker(args: &[&str]) -> Result<String, String> {
    let output = Command::new("docker")
        .args(args)
        .output()
        .map_err(|err| format!("docker failed: {err}"))?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

/// New parameters for the preset, applied by restarting the container;
/// the previous config comes back when that fails. Client configs must be
/// reissued afterwards.
fn regenerate(setting: Setting<'_>) -> Result<String, String> {
    let config = config_path(setting);
    if !config.is_file() {
        return Err(format!("AWG config not found: {}", config.display()));
    }
    let container = setting("AWG_CONTAINER_NAME", "node-plane-awg");
    let backup = config.with_file_name(format!(".awg-regenerate-backup-{}", std::process::id()));
    fs::copy(&config, &backup).map_err(|err| err.to_string())?;
    let attempt = || -> Result<String, String> {
        let report = profile::regenerate_file(&config, &setting("AWG_I1_PRESET", "quic"))?
            .map(|text| format!("{text}\n"))
            .unwrap_or_default();
        prepare_leased("awg")?;
        docker(&["restart", &container])?;
        std::thread::sleep(Duration::from_secs(2));
        let state = docker(&["inspect", "-f", "{{.State.Status}}", &container])?;
        if state != "running" {
            return Err(format!("AWG container is {state} after entropy update"));
        }
        Ok(report)
    };
    let result = attempt();
    if let Err(err) = &result {
        let restored = fs::copy(&backup, &config).is_ok() && prepare_leased("awg").is_ok();
        let _ = fs::remove_file(&backup);
        if restored {
            let _ = docker(&["restart", &container]);
        }
        return Err(format!(
            "{err}\nAWG entropy update failed; previous config restored"
        ));
    }
    let _ = fs::remove_file(&backup);
    let report = result?;
    let parameters = entropy::read(setting)?;
    Ok(format!(
        "{report}{parameters}\nWARNING: client AWG configs must be reissued after entropy regeneration."
    ))
}

/// Runs the subcommand when the agent was started as `awg-config …`.
pub(crate) fn run(args: &[String]) -> Option<i32> {
    let (first, rest) = args.split_first()?;
    if first != "awg-config" {
        return None;
    }
    let result = match rest {
        [command] if command == "init" => init(&process_setting),
        [command] if command == "regenerate" => regenerate(&process_setting),
        _ => Err("usage: awg-config init|regenerate".into()),
    };
    Some(match result {
        Ok(text) => {
            println!("{text}");
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
    #[test]
    fn the_server_gets_the_tunnel_ipv6_network_unless_configured() {
        assert_eq!(
            super::server_address("10.8.1.0/24"),
            "10.8.1.0/24, fd42:6e70:6177:2::1/64"
        );
        assert_eq!(
            super::server_address("10.8.1.0/24, fd00::1/64"),
            "10.8.1.0/24, fd00::1/64"
        );
    }
}
