//! `node-plane-agent node-settings apply CONFIG SNI TCP XHTTP PATH XRAY AWG`:
//! applies node settings to the protocol configs and redeploys the
//! containers, restoring the previous config when a deployment fails.
//! Replaces `apply-node-settings.sh`, which only starts this subcommand for
//! the profile intent journal.
//!
//! Deployments run through `deploy-xray.sh` and `deploy-awg.sh`, entry
//! points of `container-deploy` (`protocols/deploy`); AWG parameter
//! generation stays in `awg_profile.py`.
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::Value;

use super::awg::settings::{PRESET_FILE, adopt_config, current_preset, listen_port};
use super::xray::settings::{Settings, apply};
use super::xray::user_sync::write_config;
use super::{Setting, process_setting};

const RUNTIME: &str = "/opt/node-plane-runtime";

/// The runtime directory; tests point it elsewhere.
pub(crate) fn runtime() -> PathBuf {
    std::env::var("NODE_PLANE_RUNTIME_ROOT")
        .unwrap_or_else(|_| RUNTIME.into())
        .into()
}

/// `cp -p`: contents, mode and times.
pub(crate) fn copy_preserving(from: &Path, to: &Path) -> Result<(), String> {
    let status = Command::new("cp")
        .arg("-p")
        .arg(from)
        .arg(to)
        .status()
        .map_err(|err| format!("cp failed: {err}"))?;
    status
        .success()
        .then_some(())
        .ok_or_else(|| format!("could not copy {}", from.display()))
}

/// A copy of `config` next to it, restored by `restore`.
pub(crate) struct Backup {
    config: PathBuf,
    copy: PathBuf,
}

impl Backup {
    pub(crate) fn take(config: &Path, prefix: &str) -> Result<Self, String> {
        let dir = config.parent().unwrap_or(Path::new("."));
        let copy = dir.join(format!("{prefix}{}", std::process::id()));
        copy_preserving(config, &copy)?;
        Ok(Self {
            config: config.to_path_buf(),
            copy,
        })
    }

    pub(crate) fn restore(&self) {
        let _ = copy_preserving(&self.copy, &self.config);
    }
}

impl Drop for Backup {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.copy);
    }
}

fn quiet(program: &str, args: &[&str]) -> bool {
    Command::new(program)
        .args(args)
        .output()
        .is_ok_and(|output| output.status.success())
}

/// Runs a deploy script; its report joins this command's output, or goes
/// to stderr for a rollback deployment.
fn deploy(script: &str, to_stderr: bool) -> bool {
    let Ok(mut command) = crate::entry::command(&runtime(), script) else {
        return false;
    };
    if to_stderr {
        command.stdout(std::io::stderr());
    }
    command.status().is_ok_and(|status| status.success())
}

/// Python truthiness of a JSON document.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(number) => number.as_f64() != Some(0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(list) => !list.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

fn xray(setting: Setting<'_>, config: &Path, args: &[String]) -> Result<(), String> {
    let [sni, tcp, xhttp, path] = args else {
        return Err("Xray SNI or ports are invalid".into());
    };
    let port = |value: &str| {
        (!value.is_empty() && value.bytes().all(|c| c.is_ascii_digit()))
            .then(|| value.parse::<u16>().ok())
            .flatten()
    };
    let (Some(tcp_port), Some(xhttp_port)) = (port(tcp), port(xhttp)) else {
        return Err("Xray SNI or ports are invalid".into());
    };
    if sni.is_empty() {
        return Err("Xray SNI or ports are invalid".into());
    }
    if !config.is_file() {
        return Err(format!("Xray config missing: {}", config.display()));
    }
    let settings = Settings {
        sni: sni.clone(),
        tcp_port,
        xhttp_port,
        path: if path.is_empty() {
            "/assets".into()
        } else {
            path.clone()
        },
        ipv6: setting("IPV6_OUTBOUND", "on") == "on",
        block_bittorrent: setting("XRAY_BITTORRENT", "allow") == "block",
    };
    let backup = Backup::take(config, ".xray-config-backup-")?;
    apply_xray_file(config, &settings)?;
    if deploy("deploy-xray.sh", false) {
        return Ok(());
    }
    backup.restore();
    let _ = super::deploy::prepare_leased("xray");
    quiet(
        "docker",
        &[
            "restart",
            &setting("XRAY_CONTAINER_NAME", "node-plane-xray"),
        ],
    );
    Err("Xray deployment failed; previous config restored".into())
}

pub(crate) fn apply_xray_file(config: &Path, settings: &Settings) -> Result<(), String> {
    let raw = fs::read_to_string(config).map_err(|err| err.to_string())?;
    let mut value: Value = serde_json::from_str(&raw).map_err(|err| err.to_string())?;
    apply(&mut value, settings)?;
    write_config(config, &value)
}

fn awg(setting: Setting<'_>) -> Result<(), String> {
    let config = PathBuf::from(setting(
        "AWG_CONFIG",
        "/opt/node-plane-runtime/amnezia-awg/data/wg0.conf",
    ));
    adopt_config(&config)?;
    let backup = Backup::take(&config, ".awg-config-backup-")?;
    let text = fs::read_to_string(&config).map_err(|err| err.to_string())?;
    let current = current_preset(&config, &text);
    let preset = setting("AWG_I1_PRESET", "quic");
    let parameters = setting("AWG_PARAMETERS_FILE", "");
    let manual = !parameters.is_empty() && {
        let raw = fs::read_to_string(&parameters).map_err(|err| err.to_string())?;
        truthy(&serde_json::from_str(&raw).map_err(|err| err.to_string())?)
    };
    if manual {
        if crate::protocols::awg::profile::apply_file(&config, Path::new(&parameters)).is_err() {
            backup.restore();
            return Err("Manual AWG parameters failed validation; previous config restored".into());
        }
    } else if current != preset
        && crate::protocols::awg::profile::regenerate_file(&config, &preset).is_err()
    {
        backup.restore();
        return Err("AWG entropy update failed; previous config restored".into());
    }
    let text = fs::read_to_string(&config).map_err(|err| err.to_string())?;
    let updated = match listen_port(&text, &setting("AWG_SERVER_PORT", "51820")) {
        Ok(updated) => updated,
        Err(err) => {
            backup.restore();
            return Err(err);
        }
    };
    fs::write(&config, updated).map_err(|err| err.to_string())?;
    fs::set_permissions(&config, fs::Permissions::from_mode(0o600))
        .map_err(|err| err.to_string())?;
    if !deploy("deploy-awg.sh", false) {
        backup.restore();
        deploy("deploy-awg.sh", true);
        return Err("AWG deployment failed; previous config restored".into());
    }
    drop(backup);
    let recorded = if manual { "manual".to_string() } else { preset };
    fs::write(config.with_file_name(PRESET_FILE), format!("{recorded}\n"))
        .map_err(|err| err.to_string())
}

fn execute(setting: Setting<'_>, args: &[String]) -> Result<(), String> {
    match args {
        [
            command,
            config,
            sni,
            tcp,
            xhttp,
            path,
            apply_xray,
            apply_awg,
        ] if command == "apply" => {
            if apply_xray == "true" {
                xray(
                    setting,
                    Path::new(config),
                    &[sni.clone(), tcp.clone(), xhttp.clone(), path.clone()],
                )?;
            }
            if apply_awg == "true" {
                awg(setting)?;
            }
            Ok(())
        }
        // The Xray config edit alone, for routing policy tests.
        [command, config, sni, tcp, xhttp, path] if command == "xray-config" => {
            let port = |value: &str| {
                value
                    .parse::<u16>()
                    .map_err(|_| "Xray SNI or ports are invalid".to_string())
            };
            let settings = Settings {
                sni: sni.clone(),
                tcp_port: port(tcp)?,
                xhttp_port: port(xhttp)?,
                path: path.clone(),
                ipv6: setting("IPV6_OUTBOUND", "on") == "on",
                block_bittorrent: setting("XRAY_BITTORRENT", "allow") == "block",
            };
            apply_xray_file(Path::new(config), &settings)
        }
        _ => Err("usage: node-settings apply CONFIG SNI TCP_PORT XHTTP_PORT PATH XRAY AWG".into()),
    }
}

/// Runs the subcommand when the agent was started as `node-settings …`.
pub(crate) fn run(args: &[String]) -> Option<i32> {
    let (first, rest) = args.split_first()?;
    if first != "node-settings" {
        return None;
    }
    Some(match execute(&process_setting, rest) {
        Ok(()) => {
            println!("Node settings applied");
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
    use serde_json::json;

    use super::*;

    #[test]
    fn manual_parameters_count_only_when_present() {
        for value in [
            json!(null),
            json!(false),
            json!(0),
            json!(""),
            json!([]),
            json!({}),
        ] {
            assert!(!truthy(&value), "{value}");
        }
        assert!(truthy(&json!({"Jc": "4"})));
    }

    fn runtime_with(deploy: &str) -> std::path::PathBuf {
        let dir = crate::protocols::test_dir(&format!("settings-{deploy}"));
        let script = dir.join("deploy-xray.sh");
        fs::write(
            &script,
            format!("#!/bin/sh\necho deployed\nexit {deploy}\n"),
        )
        .unwrap();
        fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
        dir
    }

    #[test]
    fn a_failed_xray_deployment_restores_the_previous_config() {
        let config_of = |dir: &Path| {
            let config = dir.join("config.json");
            let inbound =
                |tag: &str| json!({"tag": tag, "streamSettings": {"realitySettings": {}}});
            let value = json!({"inbounds": [inbound("reality-tcp"), inbound("reality-xhttp")],
                               "outbounds": [{"tag": "direct"}]});
            fs::write(&config, value.to_string()).unwrap();
            config
        };
        let args = ["www.example.com", "443", "8443", "/x"].map(String::from);
        let setting = |key: &str, default: &str| match key {
            // Not a leased runtime and no container to restart in tests.
            "XRAY_CONTAINER_NAME" => "np-test-absent".into(),
            _ => default.to_string(),
        };
        let failing = runtime_with("1");
        let config = config_of(&failing);
        let before = fs::read_to_string(&config).unwrap();
        let err = with_runtime(&failing, || xray(&setting, &config, &args)).unwrap_err();
        assert!(err.contains("previous config restored"), "{err}");
        assert_eq!(fs::read_to_string(&config).unwrap(), before);
        let working = runtime_with("0");
        let config = config_of(&working);
        with_runtime(&working, || xray(&setting, &config, &args)).unwrap();
        let applied: Value = serde_json::from_str(&fs::read_to_string(&config).unwrap()).unwrap();
        assert_eq!(
            applied["inbounds"][1]["streamSettings"]["xhttpSettings"]["path"],
            "/x"
        );
        assert_eq!(
            fs::read_dir(&working).unwrap().count(),
            2,
            "no backup is left"
        );
        let invalid = ["", "443", "8443", "/x"].map(String::from);
        assert!(xray(&setting, &config, &invalid).is_err());
    }

    /// Serializes tests that point the runtime directory elsewhere.
    fn with_runtime<T>(dir: &Path, run: impl FnOnce() -> T) -> T {
        static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
        let _guard = LOCK.lock().unwrap_or_else(|err| err.into_inner());
        // SAFETY: tests that read this variable hold LOCK.
        unsafe { std::env::set_var("NODE_PLANE_RUNTIME_ROOT", dir) };
        let result = run();
        unsafe { std::env::remove_var("NODE_PLANE_RUNTIME_ROOT") };
        result
    }
}
