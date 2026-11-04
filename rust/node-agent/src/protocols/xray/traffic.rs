//! VLESS user counters from Xray's StatsService.
use std::collections::BTreeMap;
use std::fs;

use serde_json::Value;

use super::Node;
use super::api::XrayApi;
use crate::protocols::traffic::{Counters, counter};

/// Statistics must be enabled for the user's level on both inbounds, and
/// the config must still hold this identity.
fn check_config(
    config: &Value,
    tags: &[String; 2],
    name: &str,
    identity: &str,
) -> Result<(), String> {
    let services = config["api"]["services"].as_array();
    if !config["stats"].is_object()
        || !services.is_some_and(|list| list.iter().any(|s| s == "StatsService"))
    {
        return Err("Xray statistics unavailable".into());
    }
    if tags[0] == tags[1] {
        return Err("invalid Xray inbounds".into());
    }
    let inbounds = config["inbounds"]
        .as_array()
        .ok_or("missing Xray inbound")?;
    for tag in tags {
        let [inbound] = inbounds
            .iter()
            .filter(|inbound| inbound["tag"] == tag.as_str())
            .collect::<Vec<_>>()[..]
        else {
            return Err("missing Xray inbound".into());
        };
        let clients = inbound["settings"]["clients"]
            .as_array()
            .ok_or("missing Xray inbound")?;
        let [user] = clients
            .iter()
            .filter(|client| client["email"] == name)
            .collect::<Vec<_>>()[..]
        else {
            return Err("Xray identity changed".into());
        };
        if user["id"] != identity {
            return Err("Xray identity changed".into());
        }
        let level = match &user["level"] {
            Value::Null => "0".to_string(),
            Value::String(level) => level.clone(),
            other => other.to_string(),
        };
        let policy = &config["policy"]["levels"][level.as_str()];
        if policy["statsUserUplink"] != true || policy["statsUserDownlink"] != true {
            return Err("Xray user statistics unavailable".into());
        }
    }
    Ok(())
}

/// Counters named exactly `user>>>NAME>>>traffic>>>uplink|downlink`; a
/// missing one is zero.
fn parse(name: &str, stats: &BTreeMap<String, i64>) -> Result<Counters, String> {
    let prefix = format!("user>>>{name}>>>traffic>>>");
    let mut counters = Counters::default();
    for (stat, value) in stats {
        let value = u64::try_from(*value).map_err(|_| "invalid traffic counter")?;
        match stat.strip_prefix(&prefix) {
            Some("uplink") => counters.uplink = counter(value)?,
            Some("downlink") => counters.downlink = counter(value)?,
            _ => return Err("unexpected Xray statistics identity".into()),
        }
    }
    Ok(counters)
}

pub(crate) async fn counters(node: &Node, name: &str, identity: &str) -> Result<Counters, String> {
    let config: Value =
        serde_json::from_str(&fs::read_to_string(&node.config).map_err(|err| err.to_string())?)
            .map_err(|err| err.to_string())?;
    check_config(&config, &node.tags, name, identity)?;
    let unavailable = |_| "Xray statistics unavailable".to_string();
    let api = XrayApi::connect(&node.api).await.map_err(unavailable)?;
    let stats = api
        .query_stats(&format!("user>>>{name}>>>"), false)
        .await
        .map_err(unavailable)?;
    parse(name, &stats)
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    const UUID: &str = "00000000-0000-4000-8000-000000000001";

    fn tags() -> [String; 2] {
        ["reality-tcp".into(), "reality-xhttp".into()]
    }

    fn config() -> Value {
        let inbound = |tag: &str| json!({"tag": tag, "settings": {"clients": [{"email": "alice", "id": UUID}]}});
        json!({
            "stats": {},
            "api": {"services": ["StatsService"]},
            "policy": {"levels": {"0": {"statsUserUplink": true, "statsUserDownlink": true}}},
            "inbounds": [inbound("reality-tcp"), inbound("reality-xhttp")],
        })
    }

    #[test]
    fn disabled_statistics_or_another_identity_are_unknown() {
        assert!(check_config(&config(), &tags(), "alice", UUID).is_ok());
        let mut disabled = config();
        disabled["policy"]["levels"]["0"]["statsUserUplink"] = false.into();
        assert!(check_config(&disabled, &tags(), "alice", UUID).is_err());
        let mut changed = config();
        changed["inbounds"][0]["settings"]["clients"][0]["id"] = "wrong".into();
        assert!(check_config(&changed, &tags(), "alice", UUID).is_err());
        let mut no_service = config();
        no_service["api"]["services"] = json!([]);
        assert!(check_config(&no_service, &tags(), "alice", UUID).is_err());
    }

    #[test]
    fn only_the_users_two_counters_count() {
        let stats = |items: &[(&str, i64)]| -> BTreeMap<String, i64> {
            items
                .iter()
                .map(|(name, value)| (name.to_string(), *value))
                .collect()
        };
        assert_eq!(
            parse(
                "alice",
                &stats(&[
                    ("user>>>alice>>>traffic>>>uplink", 3),
                    ("user>>>alice>>>traffic>>>downlink", 7)
                ])
            ),
            Ok(Counters {
                uplink: 3,
                downlink: 7
            })
        );
        assert_eq!(parse("alice", &stats(&[])), Ok(Counters::default()));
        assert!(parse("alice", &stats(&[("user>>>alice>>>traffic>>>uplinkx", 1)])).is_err());
        assert!(parse("alice", &stats(&[("user>>>alice-2>>>traffic>>>uplink", 1)])).is_err());
        assert!(parse("alice", &stats(&[("user>>>alice>>>traffic>>>uplink", -1)])).is_err());
    }
}
