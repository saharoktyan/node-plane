//! The mutation of a node settings intent on a bootstrapped node: removal
//! of unselected protocols, the port choice, node.env, missing configs, the
//! firewall and the deployment, each verified before acknowledgment. No
//! implicit install and no key reset. Replaces `run_node_settings` of
//! `apply-profile-intent.py`.
use std::fs;
use std::io::Write;
use std::os::fd::RawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;

use serde_json::{Value, json};

use super::leases::Host;
use super::profile::run_locked;
use super::runtime::{Paths, Runtime, owned_containers, remove_protocols, sourced};
use super::settings::{Progress, Settings, digest};
use super::{Response, sorted_dumps, verify};
use crate::protocols::awg::ports;
use crate::protocols::settings::copy_preserving;
use crate::protocols::xray::user_sync::VISION;
use crate::runtime::images::Kind;

/// Python's `shlex.quote`.
fn quote(value: &str) -> String {
    if value.is_empty() {
        return "''".into();
    }
    let safe = value
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&b));
    if safe {
        value.into()
    } else {
        format!("'{}'", value.replace('\'', "'\"'\"'"))
    }
}

/// Writes a complete, durable file in place of `path`.
fn replace(path: &Path, text: &str) -> Result<(), String> {
    let dir = path.parent().unwrap_or(Path::new("/"));
    let staged = dir.join(format!(
        ".{}.{}",
        path.file_name().unwrap_or_default().to_string_lossy(),
        crate::protocols::deploy::random_hex(8)?
    ));
    let result = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&staged)
        .and_then(|mut file| {
            file.write_all(text.as_bytes())?;
            file.sync_all()
        })
        .and_then(|()| fs::rename(&staged, path))
        .map_err(|err| format!("{}: {err}", path.display()));
    if result.is_err() {
        let _ = fs::remove_file(&staged);
    }
    result
}

/// node.env with the controller-owned settings replaced and every other
/// line kept.
fn rewrite_env(raw: &str, replacements: &[(&str, String)]) -> String {
    let mut lines: Vec<String> = raw
        .lines()
        .filter(|line| {
            let line = line.trim_start();
            !replacements.iter().any(|(name, _)| {
                line.strip_prefix(name)
                    .is_some_and(|rest| rest.starts_with('='))
            })
        })
        .map(str::to_string)
        .collect();
    lines.extend(
        replacements
            .iter()
            .map(|(name, value)| format!("{name}={}", quote(value))),
    );
    lines.join("\n") + "\n"
}

/// Applies a checked settings intent while its journal lock is held.
pub(crate) fn run(
    host: &dyn Host,
    runtime: &Runtime,
    requested: &Settings<'_>,
    lock: RawFd,
    progress: &Progress,
) -> Result<Response, String> {
    progress.step("protocol_settings");
    if requested.empty() {
        return Err("no VPN settings or authorized protocol removal requested".into());
    }
    let script = |name: &str| runtime.root.join(name);
    if !runtime.env.is_file() || !script("apply-node-settings.sh").is_file() {
        return Err("node runtime is not bootstrapped".into());
    }
    let [owner]: [String; 1] = sourced(&runtime.env, &[("SERVER_KEY", "")])?
        .try_into()
        .map_err(|_| "invalid node environment")?;
    if owner != requested.node_key() {
        return Err("protocol runtime ownership is not confirmed for this node".into());
    }
    let paths = Paths::read(&runtime.env)?;
    // The cleanup shares this durable command and its lock: no implicit
    // replay.
    let removed = requested.removed();
    if !removed.is_empty() && host.installed("docker") {
        progress.step("protocol_cleanup");
        remove_protocols(host, runtime, &removed)?;
        if !owned_containers(host, &paths, &removed)?.is_empty() {
            return Err("removed protocol containers are still present".into());
        }
    }
    let mut effective = requested.value().clone();
    let mut selected = None;
    if requested.enabled("awg") && requested.text("awg_port_mode", "") == "auto" {
        progress.step("awg_port");
        let port = ports::select(
            host,
            requested.text("awg_i1_preset", "quic"),
            requested.port("awg_port", 0),
            &paths.awg,
            &paths.awg_container,
        )?;
        effective["settings"]["awg_port"] = json!(port);
        selected = Some(port);
    }
    let settings = Settings::derived(&effective);
    let interface = settings.text("awg_interface", "wg0");
    let mut replacements: Vec<(&str, String)> = vec![
        ("SERVER_KEY", settings.node_key().into()),
        (
            "AWG_SERVER_IP",
            settings
                .text("awg_public_host", settings.text("public_host", "localhost"))
                .into(),
        ),
        (
            "AWG_SERVER_PORT",
            settings.port("awg_port", 51820).to_string(),
        ),
        ("AWG_IFACE", interface.into()),
        (
            "AWG_I1_PRESET",
            settings.text("awg_i1_preset", "quic").into(),
        ),
        (
            "XRAY_FP",
            settings.text("xray_fingerprint", "chrome").into(),
        ),
        ("IPV6_OUTBOUND", settings.text("ipv6_outbound", "on").into()),
        (
            "XRAY_BITTORRENT",
            settings.text("xray_bittorrent", "allow").into(),
        ),
    ];
    let parameters = runtime.env.with_file_name("awg-parameters.json");
    replace(
        &parameters,
        &sorted_dumps(
            settings
                .settings()
                .get("awg_parameters")
                .unwrap_or(&json!({})),
        ),
    )?;
    replacements.push(("AWG_PARAMETERS_FILE", parameters.display().to_string()));
    // Keeps the server and peer identities when the interface is renamed.
    let awg_path = paths.awg.with_file_name(format!("{interface}.conf"));
    if awg_path != paths.awg && paths.awg.exists() {
        if let Some(dir) = awg_path.parent() {
            fs::create_dir_all(dir).map_err(|err| format!("{}: {err}", dir.display()))?;
        }
        copy_preserving(&paths.awg, &awg_path)?;
    }
    replacements.push(("AWG_CONFIG", awg_path.display().to_string()));
    let raw = fs::read_to_string(&runtime.env)
        .map_err(|err| format!("{}: {err}", runtime.env.display()))?;
    replace(&runtime.env, &rewrite_env(&raw, &replacements))?;
    let dir = runtime.env.parent().unwrap_or(Path::new("/"));
    fs::File::open(dir)
        .and_then(|dir| dir.sync_all())
        .map_err(|err| format!("{}: {err}", dir.display()))?;
    let locked =
        |name: &str, args: &[String]| run_locked(&script(name), args, lock, &runtime.instance);
    // A prepared agent has the entry points and node.env but no configs:
    // only missing ones are created, under this same command. Existing keys
    // are never regenerated on a retry.
    if settings.enabled("xray") {
        progress.step("xray_init");
        if !paths.xray.exists() {
            locked(
                "init-xray.sh",
                &[
                    paths.xray.display().to_string(),
                    settings.text("public_host", "").into(),
                    settings.text("xray_sni", "").into(),
                    settings.port("xray_tcp_port", 0).to_string(),
                    settings.port("xray_xhttp_port", 0).to_string(),
                    settings.text("xray_xhttp_path", "").into(),
                    VISION.into(),
                    Kind::Xray.release().into(),
                ],
            )?;
        }
        let raw = fs::read_to_string(&paths.xray)
            .map_err(|err| format!("{}: {err}", paths.xray.display()))?;
        serde_json::from_str::<Value>(&raw).map_err(|err| format!("Xray config: {err}"))?;
    }
    if settings.enabled("awg") {
        progress.step("awg_init");
        if !awg_path.exists() {
            locked("init-awg.sh", &[])?;
        }
        fs::read_to_string(&awg_path).map_err(|err| format!("{}: {err}", awg_path.display()))?;
    }
    let mut rules = Vec::new();
    if settings.enabled("xray") {
        rules.push(format!("{}/tcp", settings.port("xray_tcp_port", 0)));
        rules.push(format!("{}/tcp", settings.port("xray_xhttp_port", 0)));
    }
    if settings.enabled("awg") {
        rules.push(format!("{}/udp", settings.port("awg_port", 0)));
    }
    if host.installed("ufw") {
        progress.step("firewall");
        for rule in &rules {
            host.check(&["ufw", "allow", rule], Some(120))?;
        }
        host.check(&["ufw", "reload"], Some(120))?;
    }
    progress.step("protocol_deploy");
    let enabled = |protocol: &str| settings.enabled(protocol).to_string();
    locked(
        "apply-node-settings.sh",
        &[
            paths.xray.display().to_string(),
            settings.text("xray_sni", "").into(),
            settings.port("xray_tcp_port", 443).to_string(),
            settings.port("xray_xhttp_port", 8443).to_string(),
            settings.text("xray_xhttp_path", "/assets").into(),
            enabled("xray"),
            enabled("awg"),
        ],
    )?;
    // The entry point reports success only after its edits and deployment;
    // the result is read back independently.
    progress.step("protocol_verify");
    verify::environment(&settings, &runtime.env)?;
    verify::config(&settings, &paths.xray, &awg_path, &runtime.tool())?;
    for protocol in settings.protocols() {
        let state = host.check(
            &[
                "docker",
                "inspect",
                "-f",
                "{{.State.Running}}",
                paths.container(protocol),
            ],
            Some(30),
        )?;
        if state.trim() != "true" {
            return Err("managed protocol container is not running".into());
        }
    }
    let mut payload = json!({"node_key": settings.node_key(), "revision": settings.revision(),
        "settings_sha256": digest(&effective)});
    if let Some(port) = selected {
        payload["awg_port"] = json!(port);
    }
    Ok(Response {
        summary: "node settings applied and verified".into(),
        payload_json: sorted_dumps(&payload),
    })
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::PermissionsExt;
    use std::path::PathBuf;

    use super::*;
    use crate::intents::leases::fake::Scripted;
    use crate::intents::settings::tests::{intent, tool};
    use crate::intents::verify::tests::xray_config;
    use crate::protocols::test_dir;

    fn script(path: &Path, text: &str) {
        fs::write(path, text).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    }

    fn runtime(root: &Path) -> Runtime {
        script(&root.join("apply-node-settings.sh"), "#!/bin/sh\nexit 0\n");
        let _ = fs::copy(tool(), root.join("awg_profile.py"));
        Runtime {
            root: root.into(),
            env: root.join("node.env"),
            units: root.join("units"),
            ra_policy: root.join("ra"),
            instance: "agent".into(),
        }
    }

    fn env(root: &Path, xray: &Path, awg: &Path) {
        fs::write(root.join("node.env"), format!(
            "SERVER_KEY=n1\nXRAY_CONFIG={}\nAWG_CONFIG={}\nXRAY_CONTAINER_NAME=xray\nAWG_CONTAINER_NAME=awg\n",
            xray.display(), awg.display())).unwrap();
    }

    /// Docker reports running containers; nothing is published.
    fn host() -> Scripted {
        Scripted::new(|args| match args {
            ["docker", "inspect", ..] => (0, "true\n".into()),
            ["docker", "ps", ..] => (0, String::new()),
            _ => (0, String::new()),
        })
    }

    fn firewall(host: &Scripted) -> Vec<Vec<String>> {
        host.ran(&["ufw"])
    }

    fn rules(rules: &[&str]) -> Vec<Vec<String>> {
        let mut calls: Vec<Vec<String>> = rules
            .iter()
            .map(|rule| vec!["ufw".into(), "allow".into(), rule.to_string()])
            .collect();
        calls.push(vec!["ufw".into(), "reload".into()]);
        calls
    }

    #[test]
    fn a_clean_runtime_gets_its_configs_once_and_the_ports_of_enabled_protocols() {
        let root = test_dir("apply-clean");
        let (xray, awg): (PathBuf, PathBuf) = (root.join("xray.json"), root.join("wg0.conf"));
        env(&root, &xray, &awg);
        let runtime = runtime(&root);
        let launched = root.join("launched");
        script(
            &root.join("init-xray.sh"),
            &format!(
                "#!/bin/sh\necho init-xray >> '{}'\ncat > \"$1\" <<'EOF'\n{}\nEOF\n",
                launched.display(),
                xray_config(443, 8443)
            ),
        );
        script(
            &root.join("init-awg.sh"),
            &format!(
                "#!/bin/sh\necho init-awg >> '{}'\nprintf '[Interface]\\nListenPort = 51820\\n' > '{}'\n",
                launched.display(),
                awg.display()
            ),
        );
        let value = intent(1, "task-1");
        let settings = Settings::check(&value, &tool()).unwrap();
        let host = host();
        let first = run(&host, &runtime, &settings, 0, &Progress::none()).unwrap();
        let second = run(&host, &runtime, &settings, 0, &Progress::none()).unwrap();
        assert_eq!(first, second);
        assert_eq!(
            fs::read_to_string(&launched).unwrap(),
            "init-xray\ninit-awg\n"
        );
        let expected = rules(&["443/tcp", "8443/tcp", "51820/udp"]);
        assert_eq!(firewall(&host), [expected.clone(), expected].concat());
        let payload: Value = serde_json::from_str(&first.payload_json).unwrap();
        assert_eq!(payload["settings_sha256"], json!(digest(&value)));
        let env = fs::read_to_string(root.join("node.env")).unwrap();
        assert!(
            env.contains("AWG_SERVER_IP=node.example\n") && env.contains("XRAY_BITTORRENT=allow\n")
        );
        assert_eq!(env.matches("SERVER_KEY=").count(), 1);
    }

    #[test]
    fn only_the_ports_of_enabled_protocols_are_opened() {
        let root = test_dir("apply-ports");
        let (xray, awg) = (root.join("xray.json"), root.join("wg0.conf"));
        let runtime = runtime(&root);
        for (protocols, expected) in [
            (json!(["awg"]), vec!["51999/udp"]),
            (json!(["xray"]), vec!["4443/tcp", "9443/tcp"]),
            (
                json!(["awg", "xray"]),
                vec!["4443/tcp", "9443/tcp", "51999/udp"],
            ),
        ] {
            env(&root, &xray, &awg);
            fs::write(&awg, "[Interface]\nListenPort = 51999\n").unwrap();
            fs::write(&xray, xray_config(4443, 9443).to_string()).unwrap();
            let mut value = intent(1, "task-1");
            value["protocols"] = protocols;
            value["settings"]["awg_port"] = json!(51999);
            value["settings"]["xray_tcp_port"] = json!(4443);
            value["settings"]["xray_xhttp_port"] = json!(9443);
            let host = host();
            run(
                &host,
                &runtime,
                &Settings::check(&value, &tool()).unwrap(),
                0,
                &Progress::none(),
            )
            .unwrap();
            assert_eq!(firewall(&host), rules(&expected));
        }
    }

    #[test]
    fn a_selected_port_reaches_node_env_the_config_and_the_result() {
        let root = test_dir("apply-selected");
        let awg = root.join("wg0.conf");
        env(&root, &root.join("xray.json"), &awg);
        let runtime = runtime(&root);
        script(
            &root.join("init-awg.sh"),
            &format!(
                "#!/bin/bash\nsource '{}'\nprintf '[Interface]\\nListenPort = %s\\nI1 = <b 0xc000000001><rc 8><r 900>\\n' \"$AWG_SERVER_PORT\" > '{}'\n",
                root.join("node.env").display(),
                awg.display()
            ),
        );
        let mut value = intent(1, "task-1");
        value["protocols"] = json!(["awg"]);
        for (key, setting) in [
            ("awg_port", json!(443)),
            ("awg_port_mode", json!("auto")),
            ("awg_i1_preset", json!("quic")),
            ("ipv6_outbound", json!("on")),
        ] {
            value["settings"][key] = setting;
        }
        // Another container publishes 443/udp.
        let host = Scripted::new(|args| match args {
            ["docker", "inspect", ..] => (0, "true\n".into()),
            ["docker", "ps", ..] => (0, "other\n".into()),
            ["docker", "container", "inspect", ..] => (
                0,
                json!([{"Name": "/other", "Mounts": [],
                "HostConfig": {"PortBindings": {"443/udp": [{"HostPort": "443"}]}}}])
                .to_string(),
            ),
            _ => (0, String::new()),
        });
        let result = run(
            &host,
            &runtime,
            &Settings::check(&value, &tool()).unwrap(),
            0,
            &Progress::none(),
        )
        .unwrap();
        let payload: Value = serde_json::from_str(&result.payload_json).unwrap();
        let port = payload["awg_port"].as_u64().unwrap();
        assert_ne!(port, 443);
        assert_eq!(firewall(&host), rules(&[&format!("{port}/udp")]));
        assert!(
            fs::read_to_string(root.join("node.env"))
                .unwrap()
                .contains(&format!("AWG_SERVER_PORT={port}\n"))
        );
        assert!(
            fs::read_to_string(&awg)
                .unwrap()
                .contains(&format!("ListenPort = {port}"))
        );
        let mut effective = value.clone();
        effective["settings"]["awg_port"] = json!(port);
        assert_eq!(payload["settings_sha256"], json!(digest(&effective)));
        assert_eq!(value["settings"]["awg_port"], 443);
    }

    #[test]
    fn a_runtime_of_another_node_is_never_touched() {
        let root = test_dir("apply-foreign");
        let config = root.join("xray.json");
        fs::write(&config, "foreign configuration").unwrap();
        fs::write(
            root.join("node.env"),
            format!("SERVER_KEY=other-node\nXRAY_CONFIG={}\n", config.display()),
        )
        .unwrap();
        let runtime = runtime(&root);
        let original = fs::read_to_string(root.join("node.env")).unwrap();
        let mut value = intent(1, "task-1");
        value["protocols"] = json!([]);
        value["protocol_plan"] = json!({"remove_protocols": ["xray"]});
        let host = host();
        let err = run(
            &host,
            &runtime,
            &Settings::check(&value, &tool()).unwrap(),
            0,
            &Progress::none(),
        )
        .unwrap_err();
        assert!(err.contains("ownership is not confirmed"));
        assert!(host.calls.borrow().is_empty());
        assert_eq!(
            fs::read_to_string(&config).unwrap(),
            "foreign configuration"
        );
        assert_eq!(fs::read_to_string(root.join("node.env")).unwrap(), original);
    }

    #[test]
    fn an_agent_only_removal_cleans_both_protocols_and_opens_no_ports() {
        let root = test_dir("apply-removal");
        fs::create_dir_all(root.join("xray")).unwrap();
        fs::create_dir_all(root.join("amnezia-awg/data")).unwrap();
        let (xray, awg) = (
            root.join("xray/config.json"),
            root.join("amnezia-awg/data/wg0.conf"),
        );
        fs::write(&xray, "{}").unwrap();
        fs::write(&awg, "[Interface]").unwrap();
        env(&root, &xray, &awg);
        let runtime = runtime(&root);
        let mut value = intent(1, "task-1");
        value["protocols"] = json!([]);
        value["settings"] = json!({});
        value["protocol_plan"] = json!({"remove_protocols": ["awg", "xray"]});
        let host = host();
        let result = run(
            &host,
            &runtime,
            &Settings::check(&value, &tool()).unwrap(),
            0,
            &Progress::none(),
        )
        .unwrap();
        assert_eq!(firewall(&host), rules(&[]));
        assert!(!xray.exists() && !root.join("amnezia-awg").exists());
        let payload: Value = serde_json::from_str(&result.payload_json).unwrap();
        assert_eq!(payload["settings_sha256"], json!(digest(&value)));
        assert!(root.join("node.env").exists() && root.join("apply-node-settings.sh").exists());
    }

    #[test]
    fn quoting_and_rewriting_keep_other_lines() {
        assert_eq!(quote(""), "''");
        assert_eq!(quote("/opt/x.conf"), "/opt/x.conf");
        assert_eq!(quote("a b'c"), "'a b'\"'\"'c'");
        let raw = "# comment\nSERVER_KEY=old\n  AWG_IFACE=wg0\nAWG_IFACE_EXTRA=keep\nOTHER=1";
        let rewritten = rewrite_env(
            raw,
            &[("SERVER_KEY", "n1".into()), ("AWG_IFACE", "vpn 0".into())],
        );
        assert_eq!(
            rewritten,
            "# comment\nAWG_IFACE_EXTRA=keep\nOTHER=1\nSERVER_KEY=n1\nAWG_IFACE='vpn 0'\n"
        );
    }
}
