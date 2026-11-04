//! Independent checks of applied node settings: the protocol configs and
//! node.env must hold exactly what the command asked for, before it is
//! acknowledged and when a repair inspects an interrupted one.
use std::fs;
use std::path::Path;
use std::process::Command;

use serde_json::{Map, Value, json};

use super::leases::Host;
use super::runtime::{Paths, Runtime, owned_containers, sourced};
use super::settings::Settings;
use crate::protocols::awg::client::all_fields;

pub(crate) const CLIENT_INBOUNDS: [&str; 2] = ["reality-tcp", "reality-xhttp"];
const BLOCKED: &str = "node-plane-blocked";
const PRIVATE_RULE: &str = "node-plane-private";
const BITTORRENT_RULE: &str = "node-plane-bittorrent";

fn invalid(what: &str) -> String {
    format!("{what} verification failed")
}

/// Client inbounds never reach private destinations; BitTorrent follows the
/// policy.
pub(crate) fn routing_policy(data: &Value, block_bittorrent: bool) -> Result<(), String> {
    let rules: &[Value] = data["routing"]["rules"]
        .as_array()
        .map_or(&[], Vec::as_slice);
    let tags: Vec<Option<&str>> = rules.iter().map(|rule| rule["ruleTag"].as_str()).collect();
    let count = |tag: &str| tags.iter().filter(|t| **t == Some(tag)).count();
    if count(PRIVATE_RULE) != 1 || count(BITTORRENT_RULE) != usize::from(block_bittorrent) {
        return Err(invalid("Xray routing policy"));
    }
    let private = tags
        .iter()
        .position(|tag| *tag == Some(PRIVATE_RULE))
        .unwrap_or_default();
    // The local API route must precede blocks of loopback destinations.
    if rules
        .iter()
        .position(|rule| rule["inboundTag"] == json!(["api"]))
        .is_some_and(|api| api > private)
    {
        return Err(invalid("Xray API route"));
    }
    if rules[private]["inboundTag"] != json!(CLIENT_INBOUNDS)
        || rules[private]["outboundTag"] != BLOCKED
    {
        return Err(invalid("Xray private destination block"));
    }
    let outbounds: &[Value] = data["outbounds"].as_array().map_or(&[], Vec::as_slice);
    if !outbounds
        .iter()
        .any(|item| item["tag"] == BLOCKED && item["protocol"] == "blackhole")
    {
        return Err(invalid("Xray block outbound"));
    }
    if block_bittorrent {
        for tag in CLIENT_INBOUNDS {
            let sniffing = &inbound(data, tag)?["sniffing"];
            if sniffing["enabled"] != true || sniffing["routeOnly"] != true {
                return Err(invalid("Xray sniffing"));
            }
        }
    }
    Ok(())
}

fn inbound<'a>(data: &'a Value, tag: &str) -> Result<&'a Value, String> {
    data["inbounds"]
        .as_array()
        .and_then(|items| items.iter().rev().find(|item| item["tag"] == tag))
        .ok_or_else(|| invalid("Xray settings"))
}

fn xray(settings: &Settings<'_>, path: &Path) -> Result<(), String> {
    let raw = fs::read_to_string(path).map_err(|err| format!("{}: {err}", path.display()))?;
    let data: Value = serde_json::from_str(&raw).map_err(|_| invalid("Xray settings"))?;
    let sni = settings.text("xray_sni", "");
    for (tag, port) in [
        ("reality-tcp", settings.port("xray_tcp_port", 0)),
        ("reality-xhttp", settings.port("xray_xhttp_port", 0)),
    ] {
        let inbound = inbound(&data, tag)?;
        let reality = &inbound["streamSettings"]["realitySettings"];
        if inbound["port"] != port
            || reality["serverNames"] != json!([sni])
            || reality["dest"] != format!("{sni}:443")
        {
            return Err(invalid("Xray settings"));
        }
    }
    if inbound(&data, "reality-xhttp")?["streamSettings"]["xhttpSettings"]["path"]
        != settings.text("xray_xhttp_path", "")
    {
        return Err(invalid("Xray path"));
    }
    if settings.settings().contains_key("ipv6_outbound") {
        let enabled = settings.text("ipv6_outbound", "") == "on";
        let direct = data["outbounds"]
            .as_array()
            .and_then(|items| items.iter().find(|item| item["tag"] == "direct"))
            .ok_or_else(|| invalid("Xray IPv6 outbound"))?;
        if direct["settings"]["domainStrategy"] != if enabled { "AsIs" } else { "ForceIPv4" } {
            return Err(invalid("Xray IPv6 outbound"));
        }
        let blocks: Vec<&Value> = data["routing"]["rules"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|rule| rule["outboundTag"] == "node-plane-ipv6-disabled")
            .collect();
        let valid = if enabled {
            blocks.is_empty()
        } else {
            blocks.len() == 1 && blocks[0]["ip"] == json!(["::/0"])
        };
        if !valid {
            return Err(invalid("Xray IPv6 block"));
        }
    }
    routing_policy(&data, settings.text("xray_bittorrent", "allow") == "block")
}

/// Python's `awg_profile.interface_values`: the `[Interface]` settings.
fn interface_values(config: &str) -> Map<String, Value> {
    let mut section = String::new();
    let mut values = Map::new();
    for raw in config.lines() {
        let line = raw.trim();
        if line.starts_with('[') && line.ends_with(']') {
            section = line.to_lowercase();
        } else if section == "[interface]"
            && !line.starts_with('#')
            && let Some((key, value)) = line.split_once('=')
        {
            values.insert(key.trim().into(), json!(value.trim()));
        }
    }
    values
}

/// The I1 preset an interface section's I1 expression comes from.
fn preset(config: &str) -> Option<&'static str> {
    let interface = config.split("[Peer]").next().unwrap_or_default();
    let i1 = interface.lines().find_map(|line| {
        let rest = line.strip_prefix("I1")?;
        let value = rest.trim_start_matches([' ', '\t']).strip_prefix('=')?;
        let value = value.trim_start_matches([' ', '\t']);
        (!value.is_empty()).then(|| value.trim())
    })?;
    Some(if i1.starts_with("<b 0xc000000001>") {
        "quic"
    } else if i1.starts_with("<rc 2><b 0x01000001000000000000>") {
        "dns"
    } else {
        "chaos"
    })
}

fn awg(settings: &Settings<'_>, path: &Path, tool: &Path) -> Result<(), String> {
    let config = fs::read_to_string(path).map_err(|err| format!("{}: {err}", path.display()))?;
    if super::runtime::listen_port(&config).map(i64::from) != Some(settings.port("awg_port", 0)) {
        return Err(invalid("AWG port"));
    }
    if path.file_name().and_then(|name| name.to_str())
        != Some(&format!("{}.conf", settings.text("awg_interface", "wg0")))
    {
        return Err(invalid("AWG interface config"));
    }
    let parameters = settings
        .settings()
        .get("awg_parameters")
        .and_then(Value::as_object)
        .filter(|parameters| !parameters.is_empty());
    if let Some(parameters) = parameters {
        let valid = Command::new("python3")
            .arg(tool)
            .arg("validate")
            .arg(path)
            .output()
            .is_ok_and(|output| output.status.success());
        let actual = interface_values(&config);
        let text = |map: &Map<String, Value>, key: &str| {
            map.get(key)
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string()
        };
        if !valid || all_fields().any(|key| text(&actual, key) != text(parameters, key)) {
            return Err(invalid("manual AWG parameters"));
        }
    } else if settings.settings().contains_key("awg_i1_preset")
        && preset(&config) != Some(settings.text("awg_i1_preset", ""))
    {
        return Err(invalid("AWG preset"));
    }
    Ok(())
}

/// The protocol configs hold the settings; a removed protocol has none.
pub(crate) fn config(
    settings: &Settings<'_>,
    xray_path: &Path,
    awg_path: &Path,
    tool: &Path,
) -> Result<(), String> {
    for (protocol, path) in [("xray", xray_path), ("awg", awg_path)] {
        if settings.removed().contains(&protocol) && !path.as_os_str().is_empty() && path.exists() {
            return Err("removed protocol config is still present".into());
        }
    }
    if settings.enabled("xray") {
        xray(settings, xray_path)?;
    }
    if settings.enabled("awg") {
        awg(settings, awg_path, tool)?;
    }
    Ok(())
}

/// node.env holds the endpoint and client options, which live outside the
/// protocol configs.
pub(crate) fn environment(settings: &Settings<'_>, env: &Path) -> Result<(), String> {
    let mut expected: Vec<(&str, String)> = vec![("SERVER_KEY", settings.node_key().into())];
    if settings.settings().contains_key("ipv6_outbound") {
        expected.push(("IPV6_OUTBOUND", settings.text("ipv6_outbound", "").into()));
    }
    if settings.enabled("awg") {
        let host = settings.text("awg_public_host", settings.text("public_host", ""));
        expected.extend([
            ("AWG_SERVER_IP", host.to_string()),
            ("AWG_SERVER_PORT", settings.port("awg_port", 0).to_string()),
            ("AWG_IFACE", settings.text("awg_interface", "wg0").into()),
            (
                "AWG_I1_PRESET",
                settings.text("awg_i1_preset", "quic").into(),
            ),
        ]);
    }
    if settings.enabled("xray") {
        expected.extend([
            (
                "XRAY_FP",
                settings.text("xray_fingerprint", "chrome").into(),
            ),
            (
                "XRAY_BITTORRENT",
                settings.text("xray_bittorrent", "allow").into(),
            ),
        ]);
    }
    let mut names: Vec<(&str, &str)> = expected.iter().map(|(name, _)| (*name, "")).collect();
    if settings.enabled("awg") {
        names.push(("AWG_PARAMETERS_FILE", ""));
    }
    let mut values = sourced(env, &names)?;
    if settings.enabled("awg") {
        let file = values.pop().unwrap_or_default();
        let parameters: Value = if file.is_empty() {
            json!({})
        } else {
            fs::read_to_string(&file)
                .ok()
                .and_then(|raw| serde_json::from_str(&raw).ok())
                .ok_or_else(|| invalid("manual AWG environment"))?
        };
        let requested = settings
            .settings()
            .get("awg_parameters")
            .cloned()
            .unwrap_or_else(|| json!({}));
        if parameters != requested {
            return Err(invalid("manual AWG environment"));
        }
    }
    if values
        .iter()
        .zip(&expected)
        .any(|(value, (_, expected))| value != expected)
    {
        return Err("node environment differs from requested settings".into());
    }
    Ok(())
}

/// What a repair of an interrupted settings command sees: whether node.env
/// and the configs hold its settings, and whether its containers run with
/// the removed ones gone.
pub(crate) fn observe(
    host: &dyn Host,
    runtime: &Runtime,
    settings: &Settings<'_>,
) -> Result<Value, String> {
    if settings.empty() {
        // No authorized runtime target: inspect nothing of another
        // installation.
        return Ok(json!({"config_matches": false, "containers_running": false}));
    }
    let paths = Paths::read(&runtime.env)?;
    let mut removed_absent = true;
    let matches = (|| {
        environment(settings, &runtime.env)?;
        config(settings, &paths.xray, &paths.awg, &runtime.tool())?;
        let removed = settings.removed();
        if !removed.is_empty() && host.installed("docker") {
            removed_absent = owned_containers(host, &paths, &removed)?.is_empty();
            if !removed_absent {
                return Err("removed protocol containers are still present".to_string());
            }
        }
        Ok::<(), String>(())
    })()
    .is_ok();
    let running = removed_absent
        && settings.protocols().iter().all(|protocol| {
            host.run(
                &[
                    "docker",
                    "inspect",
                    "-f",
                    "{{.State.Running}}",
                    paths.container(protocol),
                ],
                Some(30),
            )
            .is_ok_and(|output| output.stdout.trim() == "true")
        });
    Ok(json!({"config_matches": matches, "containers_running": running}))
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::intents::leases::fake::Scripted;
    use crate::intents::settings::tests::{intent, tool};
    use crate::protocols::test_dir;

    /// A config after apply-node-settings added the routing policy.
    pub(crate) fn routed(mut data: Value) -> Value {
        let mut outbounds = data["outbounds"].as_array().cloned().unwrap_or_default();
        outbounds.push(json!({"protocol": "blackhole", "tag": "node-plane-blocked"}));
        data["outbounds"] = Value::Array(outbounds);
        data["routing"] = json!({"rules": [
            {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
            {"type": "field", "ruleTag": "node-plane-private", "ip": ["127.0.0.0/8"],
             "inboundTag": ["reality-tcp", "reality-xhttp"], "outboundTag": "node-plane-blocked"}]});
        data
    }

    pub(crate) fn xray_config(tcp: u16, xhttp: u16) -> Value {
        let reality =
            json!({"serverNames": ["www.cloudflare.com"], "dest": "www.cloudflare.com:443"});
        routed(
            json!({"outbounds": [{"protocol": "freedom", "tag": "direct"}], "inbounds": [
            {"tag": "reality-tcp", "port": tcp, "streamSettings": {"realitySettings": reality}},
            {"tag": "reality-xhttp", "port": xhttp, "streamSettings": {"realitySettings": reality,
                "xhttpSettings": {"path": "/assets"}}}]}),
        )
    }

    #[test]
    fn the_effective_configs_are_checked() {
        let dir = test_dir("verify-config");
        let value = intent(1, "task-1");
        let settings = Settings::check(&value, &tool()).unwrap();
        let (xray_path, awg_path) = (dir.join("xray.json"), dir.join("wg0.conf"));
        let data = xray_config(443, 8443);
        let write = |data: &Value| fs::write(&xray_path, data.to_string()).unwrap();
        write(&data);
        fs::write(&awg_path, "[Interface]\nListenPort = 51820\n").unwrap();
        config(&settings, &xray_path, &awg_path, &tool()).unwrap();
        let mut broken = data.clone();
        broken["routing"]["rules"] = json!([]);
        let mut reversed = data.clone();
        reversed["routing"]["rules"]
            .as_array_mut()
            .unwrap()
            .reverse();
        let mut unblocked = data.clone();
        unblocked["outbounds"].as_array_mut().unwrap().truncate(1);
        let mut moved = data.clone();
        moved["inbounds"][0]["port"] = json!(444);
        for broken in [broken, reversed, unblocked, moved] {
            write(&broken);
            assert!(config(&settings, &xray_path, &awg_path, &tool()).is_err());
        }
        write(&data);
        let mut blocked = value.clone();
        blocked["settings"]["xray_bittorrent"] = json!("block");
        assert!(
            config(
                &Settings::check(&blocked, &tool()).unwrap(),
                &xray_path,
                &awg_path,
                &tool()
            )
            .is_err()
        );
        fs::write(&awg_path, "[Interface]\nListenPort = 51821\n").unwrap();
        assert!(config(&settings, &xray_path, &awg_path, &tool()).is_err());
    }

    #[test]
    fn what_the_agent_writes_is_what_the_verifier_accepts() {
        use crate::protocols::xray::settings::{Settings as Xray, apply};
        let dir = test_dir("verify-agreement");
        let reality =
            json!({"privateKey": "key", "serverNames": ["old.example"], "dest": "old.example:443"});
        for (ipv6, block) in [(true, false), (false, true), (true, true), (false, false)] {
            let mut data = json!({"inbounds": [
                {"tag": "api", "protocol": "dokodemo-door", "port": 10085},
                {"tag": "reality-tcp", "port": 1, "streamSettings": {"realitySettings": reality}},
                {"tag": "reality-xhttp", "port": 2, "streamSettings": {"realitySettings": reality,
                    "xhttpSettings": {"path": "/old"}}}],
                "outbounds": [{"protocol": "freedom", "tag": "direct"}],
                "routing": {"rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]}});
            apply(
                &mut data,
                &Xray {
                    sni: "www.example.com".into(),
                    tcp_port: 443,
                    xhttp_port: 8443,
                    path: "/assets".into(),
                    ipv6,
                    block_bittorrent: block,
                },
            )
            .unwrap();
            routing_policy(&data, block).unwrap();
            assert!(routing_policy(&data, !block).is_err());
            let mut value = intent(1, "task-1");
            value["protocols"] = json!(["xray"]);
            value["settings"]["xray_sni"] = json!("www.example.com");
            value["settings"]["ipv6_outbound"] = json!(if ipv6 { "on" } else { "off" });
            value["settings"]["xray_bittorrent"] = json!(if block { "block" } else { "allow" });
            let path = dir.join("config.json");
            fs::write(&path, data.to_string()).unwrap();
            config(
                &Settings::check(&value, &tool()).unwrap(),
                &path,
                Path::new(""),
                &tool(),
            )
            .unwrap();
        }
    }

    #[test]
    fn an_old_preset_or_interface_is_refused() {
        let dir = test_dir("verify-preset");
        let mut value = intent(1, "task-1");
        value["protocols"] = json!(["awg"]);
        value["settings"]["awg_i1_preset"] = json!("dns");
        value["settings"]["awg_interface"] = json!("vpn0");
        let settings = Settings::check(&value, &tool()).unwrap();
        let path = dir.join("vpn0.conf");
        fs::write(
            &path,
            "[Interface]\nListenPort = 51820\nI1 = <b 0xc000000001><rc 8><r 900>\n",
        )
        .unwrap();
        assert!(config(&settings, Path::new(""), &path, &tool()).is_err());
        fs::write(
            &path,
            "[Interface]\nListenPort = 51820\nI1 = <rc 2><b 0x01000001000000000000><r 32>\n",
        )
        .unwrap();
        config(&settings, Path::new(""), &path, &tool()).unwrap();
        let old = dir.join("wg0.conf");
        fs::copy(&path, &old).unwrap();
        assert!(config(&settings, Path::new(""), &old, &tool()).is_err());
    }

    #[test]
    fn node_env_must_hold_the_endpoint_port_interface_preset_and_fingerprint() {
        let dir = test_dir("verify-env");
        let mut value = intent(1, "task-1");
        for (key, setting) in [
            ("awg_public_host", "awg.example"),
            ("awg_interface", "vpn0"),
            ("awg_i1_preset", "dns"),
            ("xray_fingerprint", "firefox"),
        ] {
            value["settings"][key] = json!(setting);
        }
        let settings = Settings::check(&value, &tool()).unwrap();
        let env = dir.join("node.env");
        let values = [
            ("SERVER_KEY", "n1"),
            ("AWG_SERVER_IP", "awg.example"),
            ("AWG_SERVER_PORT", "51820"),
            ("AWG_IFACE", "vpn0"),
            ("AWG_I1_PRESET", "dns"),
            ("XRAY_FP", "firefox"),
            ("XRAY_BITTORRENT", "allow"),
        ];
        let write = |changed: Option<&str>| {
            let text: String = values
                .iter()
                .map(|(key, value)| {
                    format!(
                        "{key}={}\n",
                        if Some(*key) == changed { "old" } else { value }
                    )
                })
                .collect();
            fs::write(&env, text).unwrap();
        };
        write(None);
        environment(&settings, &env).unwrap();
        for (key, _) in values {
            write(Some(key));
            assert!(environment(&settings, &env).is_err(), "{key}");
        }
    }

    #[test]
    fn a_repair_does_not_accept_configs_with_an_old_endpoint() {
        let dir = test_dir("verify-observe");
        fs::write(dir.join("node.env"), format!(
            "SERVER_KEY=n1\nAWG_SERVER_IP=old.example\nAWG_SERVER_PORT=51820\nAWG_IFACE=wg0\nAWG_I1_PRESET=quic\nXRAY_FP=chrome\nXRAY_CONFIG={0}/xray.json\nAWG_CONFIG={0}/wg0.conf\n",
            dir.display())).unwrap();
        let runtime = Runtime {
            root: dir.clone(),
            env: dir.join("node.env"),
            units: dir.join("units"),
            ra_policy: dir.join("ra"),
            instance: "agent".into(),
        };
        let host = Scripted::new(|args| match args {
            ["docker", "inspect", ..] => (0, "true\n".into()),
            _ => (1, String::new()),
        });
        let value = intent(1, "task-1");
        let observed =
            observe(&host, &runtime, &Settings::check(&value, &tool()).unwrap()).unwrap();
        assert_eq!(
            observed,
            json!({"config_matches": false, "containers_running": true})
        );
    }

    #[test]
    fn a_repair_of_a_removal_needs_the_containers_gone() {
        let dir = test_dir("verify-removal");
        fs::write(
            dir.join("node.env"),
            format!(
                "SERVER_KEY=n1\nXRAY_CONFIG={0}/xray/config.json\nAWG_CONFIG={0}/awg/wg0.conf\n",
                dir.display()
            ),
        )
        .unwrap();
        let runtime = Runtime {
            root: dir.clone(),
            env: dir.join("node.env"),
            units: dir.join("units"),
            ra_policy: dir.join("ra"),
            instance: "agent".into(),
        };
        let mut value = intent(1, "task-1");
        value["protocols"] = json!([]);
        value["settings"] = json!({});
        value["protocol_plan"] = json!({"remove_protocols": ["awg", "xray"]});
        let settings = Settings::check(&value, &tool()).unwrap();
        for leftover in [false, true] {
            let source = dir.join("xray").display().to_string();
            let host = Scripted::new(move |args| match args {
                ["docker", "ps", ..] => (0, if leftover { "a".into() } else { String::new() }),
                ["docker", "container", "inspect", ..] => (
                    0,
                    json!([{"Name": "/node-plane-xray", "Id": "a".repeat(64),
                    "Mounts": [{"Type": "bind", "Source": source, "Destination": "/etc/xray"}]}])
                    .to_string(),
                ),
                _ => (1, String::new()),
            });
            let observed = observe(&host, &runtime, &settings).unwrap();
            assert_eq!(
                observed,
                json!({"config_matches": !leftover, "containers_running": !leftover})
            );
        }
    }
}
