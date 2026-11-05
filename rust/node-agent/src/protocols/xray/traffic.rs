//! VLESS user counters from Xray's StatsService, per inbound.
use std::collections::BTreeMap;
use std::fs;

use serde_json::Value;

use super::api::XrayApi;
use super::identity::{client_name, email};
use super::{INBOUNDS, Node};
use crate::protocols::traffic::{ByInbound, Counters, counter};

/// Statistics must be enabled for the user's level on both inbounds, and
/// the config must still hold this identity under its email of each.
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
            .filter(|client| client_name(client) == Some(name))
            .collect::<Vec<_>>()[..]
        else {
            return Err("Xray identity changed".into());
        };
        if user["id"] != identity || user["email"] != email(name, tag).as_str() {
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

/// The client's counters, `user>>>NAME.TAG>>>traffic>>>uplink|downlink`
/// on each inbound, and their sum; a missing one is zero. `stats` holds
/// only counters of this client's emails.
fn parse(
    name: &str,
    tags: &[String; 2],
    stats: &BTreeMap<String, i64>,
) -> Result<(Counters, ByInbound), String> {
    let mut inbounds: ByInbound = INBOUNDS
        .iter()
        .map(|inbound| (inbound.to_string(), Counters::default()))
        .collect();
    for (stat, value) in stats {
        let value = u64::try_from(*value).map_err(|_| "invalid traffic counter")?;
        let (inbound, direction) = tags
            .iter()
            .zip(INBOUNDS)
            .find_map(|(tag, inbound)| {
                stat.strip_prefix(&format!("user>>>{}>>>traffic>>>", email(name, tag)))
                    .map(|direction| (inbound, direction))
            })
            .ok_or("unexpected Xray statistics identity")?;
        let counters = inbounds.get_mut(inbound).expect("every inbound is listed");
        match direction {
            "uplink" => counters.uplink = counter(value)?,
            "downlink" => counters.downlink = counter(value)?,
            _ => return Err("unexpected Xray statistics identity".into()),
        }
    }
    let total = Counters {
        uplink: counter(inbounds.values().map(|c| c.uplink).sum())?,
        downlink: counter(inbounds.values().map(|c| c.downlink).sum())?,
    };
    Ok((total, inbounds))
}

/// The counters of the client's emails among `stats`.
fn own(name: &str, stats: &BTreeMap<String, i64>) -> BTreeMap<String, i64> {
    let prefix = format!("user>>>{name}.");
    stats
        .iter()
        .filter(|(stat, _)| stat.starts_with(&prefix))
        .map(|(stat, value)| (stat.clone(), *value))
        .collect()
}

pub(crate) async fn counters(
    node: &Node,
    name: &str,
    identity: &str,
) -> Result<(Counters, ByInbound), String> {
    let config: Value =
        serde_json::from_str(&fs::read_to_string(&node.config).map_err(|err| err.to_string())?)
            .map_err(|err| err.to_string())?;
    check_config(&config, &node.tags, name, identity)?;
    let unavailable = |_| "Xray statistics unavailable".to_string();
    let api = XrayApi::connect(&node.api).await.map_err(unavailable)?;
    let stats = api
        .query_stats(&format!("user>>>{name}."), false)
        .await
        .map_err(unavailable)?;
    parse(name, &node.tags, &own(name, &stats))
}

/// Counters of several users, `(name, identity)`, from one statistics
/// query; a user whose counters are unknown gets none.
pub(crate) async fn batch(
    node: &Node,
    users: &[(String, String)],
) -> Vec<Option<(Counters, ByInbound)>> {
    let config = fs::read_to_string(&node.config)
        .ok()
        .and_then(|raw| serde_json::from_str::<Value>(&raw).ok());
    let Some(config) = config else {
        return vec![None; users.len()];
    };
    let stats = match XrayApi::connect(&node.api).await {
        Ok(api) => api.query_stats("user>>>", false).await.ok(),
        Err(_) => None,
    };
    let Some(stats) = stats else {
        return vec![None; users.len()];
    };
    split(&config, &node.tags, &stats, users)
}

/// Each user's own counters from the statistics of all users.
fn split(
    config: &Value,
    tags: &[String; 2],
    stats: &BTreeMap<String, i64>,
    users: &[(String, String)],
) -> Vec<Option<(Counters, ByInbound)>> {
    users
        .iter()
        .map(|(name, identity)| {
            check_config(config, tags, name, identity).ok()?;
            parse(name, tags, &own(name, stats)).ok()
        })
        .collect()
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
        let inbound = |tag: &str| {
            let client = json!({"name": "alice", "email": format!("alice.{tag}"), "id": UUID});
            json!({"tag": tag, "settings": {"clients": [client]}})
        };
        json!({
            "stats": {},
            "api": {"services": ["StatsService"]},
            "policy": {"levels": {"0": {"statsUserUplink": true, "statsUserDownlink": true}}},
            "inbounds": [inbound("reality-tcp"), inbound("reality-xhttp")],
        })
    }

    fn stats(items: &[(&str, i64)]) -> BTreeMap<String, i64> {
        items
            .iter()
            .map(|(name, value)| (name.to_string(), *value))
            .collect()
    }

    fn counters(uplink: u64, downlink: u64) -> Counters {
        Counters { uplink, downlink }
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
        let mut shared = config();
        shared["inbounds"][1]["settings"]["clients"][0]["email"] = "alice.reality-tcp".into();
        assert!(check_config(&shared, &tags(), "alice", UUID).is_err());
        let mut no_service = config();
        no_service["api"]["services"] = json!([]);
        assert!(check_config(&no_service, &tags(), "alice", UUID).is_err());
    }

    #[test]
    fn each_inbound_counts_separately_and_the_total_sums_them() {
        let (total, inbounds) = parse(
            "alice",
            &tags(),
            &stats(&[
                ("user>>>alice.reality-tcp>>>traffic>>>uplink", 3),
                ("user>>>alice.reality-tcp>>>traffic>>>downlink", 7),
                ("user>>>alice.reality-xhttp>>>traffic>>>downlink", 5),
            ]),
        )
        .unwrap();
        assert_eq!(total, counters(3, 12));
        assert_eq!(inbounds["vless-tcp"], counters(3, 7));
        assert_eq!(inbounds["vless-xhttp"], counters(0, 5));
        let (total, inbounds) = parse("alice", &tags(), &stats(&[])).unwrap();
        assert_eq!(total, Counters::default());
        assert_eq!(inbounds.len(), 2);
        for wrong in [
            ("user>>>alice.reality-tcp>>>traffic>>>uplinkx", 1),
            ("user>>>alice.other>>>traffic>>>uplink", 1),
            ("user>>>alice.reality-tcp>>>traffic>>>uplink", -1),
        ] {
            assert!(
                parse("alice", &tags(), &stats(&[wrong])).is_err(),
                "{wrong:?}"
            );
        }
    }

    #[test]
    fn a_batch_gives_each_user_only_their_own_counters() {
        let all = stats(&[
            ("user>>>alice.reality-tcp>>>traffic>>>uplink", 3),
            ("user>>>alice.reality-xhttp>>>traffic>>>downlink", 7),
            ("user>>>alice-2.reality-tcp>>>traffic>>>uplink", 100),
            ("user>>>bob.reality-tcp>>>traffic>>>uplink", 5),
        ]);
        let users = [
            ("alice".to_string(), UUID.to_string()),
            // Not in the config: unknown, the others still count.
            ("bob".to_string(), UUID.to_string()),
            ("alice".to_string(), "wrong".to_string()),
        ];
        let result = split(&config(), &tags(), &all, &users);
        assert_eq!(result[0].as_ref().unwrap().0, counters(3, 7));
        assert_eq!(result[1..], [None, None]);
    }
}
