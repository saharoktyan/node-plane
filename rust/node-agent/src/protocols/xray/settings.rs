//! Node settings in the Xray server config: SNI, ports, XHTTP path, IPv6
//! egress and the routing policy for client traffic.
use serde_json::{Value, json};

const CLIENT_INBOUNDS: [&str; 2] = ["reality-tcp", "reality-xhttp"];
const IPV6_DISABLED: &str = "node-plane-ipv6-disabled";
/// The blackhole the routing policy and the agent's block rules use.
const BLOCKED: &str = "node-plane-blocked";
const POLICY_RULES: [&str; 2] = ["node-plane-private", "node-plane-bittorrent"];
/// Host-local, private and reserved networks clients never reach.
const PRIVATE_NETWORKS: [&str; 16] = [
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "::/128",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
];

pub(crate) struct Settings {
    pub(crate) sni: String,
    pub(crate) tcp_port: u16,
    pub(crate) xhttp_port: u16,
    pub(crate) path: String,
    /// `IPV6_OUTBOUND=on`.
    pub(crate) ipv6: bool,
    /// `XRAY_BITTORRENT=block`.
    pub(crate) block_bittorrent: bool,
}

fn array<'a>(value: &'a mut Value, key: &str) -> Result<&'a mut Vec<Value>, String> {
    if value.get(key).is_none() {
        value[key] = json!([]);
    }
    value[key]
        .as_array_mut()
        .ok_or_else(|| format!("Xray config {key} is not a list"))
}

/// Applies the settings. The API rule stays first, then the policy rules,
/// then any others.
pub(crate) fn apply(config: &mut Value, settings: &Settings) -> Result<(), String> {
    if !config.is_object() {
        return Err("Xray config is not an object".into());
    }
    let inbounds = array(config, "inbounds")?;
    for (tag, port) in CLIENT_INBOUNDS
        .iter()
        .zip([settings.tcp_port, settings.xhttp_port])
    {
        let inbound = inbounds
            .iter_mut()
            .rev()
            .find(|inbound| inbound["tag"] == *tag)
            .ok_or("Xray Reality inbounds are missing")?;
        inbound["port"] = port.into();
        let reality = inbound["streamSettings"]
            .get_mut("realitySettings")
            .filter(|reality| reality.is_object())
            .ok_or("Xray Reality settings are missing")?;
        reality["serverNames"] = json!([settings.sni]);
        reality["dest"] = format!("{}:443", settings.sni).into();
        if *tag == "reality-xhttp" {
            let stream = &mut inbound["streamSettings"];
            if !stream["xhttpSettings"].is_object() {
                stream["xhttpSettings"] = json!({});
            }
            stream["xhttpSettings"]["path"] = settings.path.clone().into();
        }
        if settings.block_bittorrent {
            // routeOnly keeps the requested destination; sniffing only feeds routing.
            inbound["sniffing"] = json!({"enabled": true, "destOverride": ["http", "tls", "quic"], "routeOnly": true});
        } else if let Some(inbound) = inbound.as_object_mut() {
            inbound.remove("sniffing");
        }
    }

    let outbounds = array(config, "outbounds")?;
    let direct = outbounds
        .iter_mut()
        .find(|outbound| outbound["tag"] == "direct")
        .ok_or("Xray direct outbound is missing")?;
    if !direct["settings"].is_object() {
        direct["settings"] = json!({});
    }
    direct["settings"]["domainStrategy"] = if settings.ipv6 { "AsIs" } else { "ForceIPv4" }.into();
    outbounds.retain(|outbound| outbound["tag"] != IPV6_DISABLED);
    if !settings.ipv6 {
        outbounds.push(json!({"protocol": "blackhole", "tag": IPV6_DISABLED}));
    }
    if !outbounds.iter().any(|outbound| outbound["tag"] == BLOCKED) {
        outbounds.push(json!({"protocol": "blackhole", "tag": BLOCKED}));
    }

    if !config["routing"].is_object() {
        config["routing"] = json!({});
    }
    let rules = array(&mut config["routing"], "rules")?;
    rules.retain(|rule| {
        rule["outboundTag"] != IPV6_DISABLED
            && !POLICY_RULES.iter().any(|tag| rule["ruleTag"] == *tag)
    });
    if !settings.ipv6 {
        rules.insert(
            0,
            json!({"type": "field", "ip": ["::/0"], "outboundTag": IPV6_DISABLED}),
        );
    }
    let (api, others): (Vec<Value>, Vec<Value>) = rules
        .drain(..)
        .partition(|rule| rule["inboundTag"] == json!(["api"]));
    let mut policy = vec![json!({
        "type": "field",
        "ruleTag": "node-plane-private",
        "inboundTag": CLIENT_INBOUNDS,
        "ip": PRIVATE_NETWORKS,
        "outboundTag": BLOCKED,
    })];
    if settings.block_bittorrent {
        policy.push(json!({
            "type": "field",
            "ruleTag": "node-plane-bittorrent",
            "inboundTag": CLIENT_INBOUNDS,
            "protocol": ["bittorrent"],
            "outboundTag": BLOCKED,
        }));
    }
    rules.extend(api.into_iter().chain(policy).chain(others));

    // The agent limits concurrent VLESS sources through these services
    // and online statistics (VPN plan, section 5).
    if !config["api"].is_object() {
        config["api"] = json!({"tag": "api"});
    }
    let services = array(&mut config["api"], "services")?;
    for service in ["StatsService", "HandlerService", "RoutingService"] {
        if !services.iter().any(|known| known == service) {
            services.push(service.into());
        }
    }
    if !config["policy"].is_object() {
        config["policy"] = json!({});
    }
    if !config["policy"]["levels"].is_object() {
        config["policy"]["levels"] = json!({});
    }
    if !config["policy"]["levels"]["0"].is_object() {
        config["policy"]["levels"]["0"] = json!({});
    }
    config["policy"]["levels"]["0"]["statsUserOnline"] = true.into();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> Value {
        let inbound = |tag: &str, network: &str| json!({"tag": tag, "port": 1, "streamSettings": {"network": network, "realitySettings": {"serverNames": ["old"]}}});
        json!({
            "inbounds": [{"tag": "api"}, inbound("reality-tcp", "tcp"), inbound("reality-xhttp", "xhttp")],
            "outbounds": [{"protocol": "freedom", "tag": "direct"}, {"protocol": "freedom", "tag": "api"}],
            "routing": {"rules": [
                {"type": "field", "outboundTag": "custom", "domain": ["example.org"]},
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
            ]},
            "api": {"tag": "api", "services": ["HandlerService"]},
        })
    }

    fn settings(ipv6: bool, block_bittorrent: bool) -> Settings {
        Settings {
            sni: "www.example.com".into(),
            tcp_port: 443,
            xhttp_port: 8443,
            path: "/assets".into(),
            ipv6,
            block_bittorrent,
        }
    }

    fn tags(rules: &Value) -> Vec<String> {
        rules
            .as_array()
            .unwrap()
            .iter()
            .map(|rule| {
                rule["ruleTag"]
                    .as_str()
                    .or_else(|| rule["outboundTag"].as_str())
                    .unwrap()
                    .to_string()
            })
            .collect()
    }

    #[test]
    fn the_api_rule_stays_first_then_the_policy_and_settings_follow() {
        let mut value = config();
        apply(&mut value, &settings(true, false)).unwrap();
        assert_eq!(
            tags(&value["routing"]["rules"]),
            ["api", "node-plane-private", "custom"]
        );
        let tcp = &value["inbounds"][1];
        assert_eq!(tcp["port"], 443);
        assert_eq!(
            tcp["streamSettings"]["realitySettings"]["dest"],
            "www.example.com:443"
        );
        assert_eq!(
            value["inbounds"][2]["streamSettings"]["xhttpSettings"]["path"],
            "/assets"
        );
        assert_eq!(value["outbounds"][0]["settings"]["domainStrategy"], "AsIs");
        assert_eq!(value["outbounds"][2]["tag"], BLOCKED);
        assert_eq!(
            value["api"]["services"],
            json!(["HandlerService", "StatsService", "RoutingService"])
        );
        assert_eq!(value["policy"]["levels"]["0"]["statsUserOnline"], true);
        assert_eq!(
            value["routing"]["rules"][1]["ip"].as_array().unwrap().len(),
            16
        );
        // Applying again changes nothing.
        let once = value.clone();
        apply(&mut value, &settings(true, false)).unwrap();
        assert_eq!(value, once);
    }

    #[test]
    fn ipv6_and_bittorrent_switch_on_and_off() {
        let mut value = config();
        apply(&mut value, &settings(false, true)).unwrap();
        assert_eq!(
            tags(&value["routing"]["rules"]),
            [
                "api",
                "node-plane-private",
                "node-plane-bittorrent",
                IPV6_DISABLED,
                "custom"
            ]
        );
        assert_eq!(
            value["outbounds"][0]["settings"]["domainStrategy"],
            "ForceIPv4"
        );
        assert!(value["inbounds"][1]["sniffing"]["routeOnly"] == true);
        apply(&mut value, &settings(true, false)).unwrap();
        assert_eq!(
            tags(&value["routing"]["rules"]),
            ["api", "node-plane-private", "custom"]
        );
        assert!(value["inbounds"][1].get("sniffing").is_none());
        assert!(
            !value["outbounds"]
                .as_array()
                .unwrap()
                .iter()
                .any(|o| o["tag"] == IPV6_DISABLED)
        );
    }

    #[test]
    fn a_config_without_reality_inbounds_or_direct_outbound_is_refused() {
        let mut missing = config();
        missing["inbounds"].as_array_mut().unwrap().pop();
        assert!(apply(&mut missing, &settings(true, false)).is_err());
        let mut no_direct = config();
        no_direct["outbounds"] = json!([]);
        assert!(apply(&mut no_direct, &settings(true, false)).is_err());
    }
}
