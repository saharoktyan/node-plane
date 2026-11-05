//! A VLESS identity in Xray's config file and in the live HandlerService.
use std::fs;
use std::path::Path;

use serde_json::Value;

use super::Node;
use super::api::XrayApi;
use super::user_sync::{Users, VISION};
use crate::protocols::inspection::Inspection;
use crate::protocols::{Setting, lock_config};

pub(crate) async fn inspect(
    setting: Setting<'_>,
    name: &str,
    uuid: &str,
) -> Result<Inspection, String> {
    let node = Node::read(setting);
    let _lock = lock_config(&node.config)?;
    let api = XrayApi::connect(&node.api)
        .await
        .map_err(|err| err.to_string())?;
    compare(&api, &node.config, &node.tags, name, uuid).await
}

/// Both inbounds must hold the identity, the Vision one with its flow.
/// Presence on only one is drift, never a confirmed state.
async fn compare(
    users: &impl Users,
    config: &Path,
    tags: &[String; 2],
    name: &str,
    uuid: &str,
) -> Result<Inspection, String> {
    let config: Value =
        serde_json::from_str(&fs::read_to_string(config).map_err(|err| err.to_string())?)
            .map_err(|err| err.to_string())?;
    if tags[0] == tags[1] {
        return Err("missing protocol inbound".into());
    }
    let (mut disk, mut live, mut matches) = (Vec::new(), Vec::new(), true);
    for (index, tag) in tags.iter().enumerate() {
        let clients = config["inbounds"]
            .as_array()
            .and_then(|inbounds| {
                inbounds
                    .iter()
                    .find(|inbound| inbound["tag"] == tag.as_str())
            })
            .and_then(|inbound| inbound["settings"]["clients"].as_array())
            .ok_or("missing protocol inbound")?;
        let stored: Vec<&Value> = clients
            .iter()
            .filter(|client| super::identity::client_name(client) == Some(name))
            .collect();
        if stored.len() > 1 {
            return Err("duplicate profile".into());
        }
        let actual = users
            .users(tag)
            .await?
            .remove(&super::identity::email(name, tag));
        let flow = if index == 0 { VISION } else { "" };
        matches &= stored.first().is_some_and(|client| {
            client["id"] == uuid && client.get("flow").and_then(Value::as_str).unwrap_or("") == flow
        }) && actual == Some((uuid.to_string(), flow.to_string()));
        disk.push(!stored.is_empty());
        live.push(actual.is_some());
    }
    let complete = matches && disk.iter().all(|d| *d) && live.iter().all(|l| *l);
    Ok(Inspection {
        disk_present: disk.contains(&true),
        live_present: live.contains(&true),
        identity_matches: complete,
        config_available: complete,
    })
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::sync::Mutex;

    use serde_json::json;

    use super::*;
    use crate::protocols::xray::api::InboundUser;
    use crate::protocols::xray::user_sync::Live;

    const UUID: &str = "11111111-1111-4111-8111-111111111111";

    type Tags = BTreeMap<String, BTreeMap<String, Live>>;

    struct Fake(Mutex<Result<Tags, String>>);

    impl Users for Fake {
        async fn users(&self, tag: &str) -> Result<BTreeMap<String, Live>, String> {
            let live = self.0.lock().unwrap();
            Ok(live
                .as_ref()
                .map_err(Clone::clone)?
                .get(tag)
                .cloned()
                .unwrap_or_default())
        }
        async fn add(&self, _: &str, _: &InboundUser) -> Result<(), String> {
            unreachable!("inspection is read-only")
        }
        async fn remove(&self, _: &str, _: &str) -> Result<(), String> {
            unreachable!("inspection is read-only")
        }
    }

    fn tags() -> [String; 2] {
        ["tcp".into(), "xhttp".into()]
    }

    fn live(tcp_flow: &str, xhttp: bool) -> Tags {
        let user = |tag: &str, flow: &str| {
            BTreeMap::from([(
                format!("p_test.{tag}"),
                (UUID.to_string(), flow.to_string()),
            )])
        };
        BTreeMap::from([
            ("tcp".to_string(), user("tcp", tcp_flow)),
            (
                "xhttp".to_string(),
                if xhttp {
                    user("xhttp", "")
                } else {
                    BTreeMap::new()
                },
            ),
        ])
    }

    #[tokio::test]
    async fn both_inbounds_and_the_vision_flow_must_match_and_nothing_changes() {
        let path = crate::protocols::test_dir("xray-inspect-match").join("config.json");
        let config = json!({"inbounds": [
            {"tag": "tcp", "settings": {"clients": [{"name": "p_test", "email": "p_test.tcp", "id": UUID, "flow": VISION}]}},
            {"tag": "xhttp", "settings": {"clients": [{"name": "p_test", "email": "p_test.xhttp", "id": UUID}]}},
        ]});
        fs::write(&path, config.to_string()).unwrap();
        let fake = Fake(Mutex::new(Ok(live(VISION, true))));
        let found = compare(&fake, &path, &tags(), "p_test", UUID)
            .await
            .unwrap();
        assert!(found.identity_matches && found.config_available);
        *fake.0.lock().unwrap() = Ok(live(VISION, false));
        let found = compare(&fake, &path, &tags(), "p_test", UUID)
            .await
            .unwrap();
        assert!(found.disk_present && found.live_present && !found.identity_matches);
        *fake.0.lock().unwrap() = Ok(live("", true));
        assert!(
            !compare(&fake, &path, &tags(), "p_test", UUID)
                .await
                .unwrap()
                .identity_matches
        );
        // Deletion intents carry no UUID: presence only.
        *fake.0.lock().unwrap() = Ok(live(VISION, true));
        let found = compare(&fake, &path, &tags(), "p_test", "").await.unwrap();
        assert!(found.disk_present && found.live_present && !found.identity_matches);
        assert_eq!(fs::read_to_string(&path).unwrap(), config.to_string());
    }

    #[tokio::test]
    async fn an_api_error_or_a_damaged_config_is_unknown_not_absence() {
        let path = crate::protocols::test_dir("xray-inspect-unknown").join("config.json");
        let empty = |tag: &str| json!({"tag": tag, "settings": {"clients": []}});
        fs::write(
            &path,
            json!({"inbounds": [empty("tcp"), empty("xhttp")]}).to_string(),
        )
        .unwrap();
        let fake = Fake(Mutex::new(Err("unavailable".into())));
        assert!(
            compare(&fake, &path, &tags(), "p_test", UUID)
                .await
                .is_err()
        );
        *fake.0.lock().unwrap() = Ok(BTreeMap::new());
        let found = compare(&fake, &path, &tags(), "p_test", UUID)
            .await
            .unwrap();
        assert_eq!(found, Inspection::default());
        fs::write(&path, json!({"inbounds": [empty("tcp")]}).to_string()).unwrap();
        assert!(
            compare(&fake, &path, &tags(), "p_test", UUID)
                .await
                .is_err()
        );
        let duplicate = json!({"tag": "xhttp", "settings": {"clients": [{"email": "p_test.xhttp"}, {"name": "p_test"}]}});
        fs::write(
            &path,
            json!({"inbounds": [empty("tcp"), duplicate]}).to_string(),
        )
        .unwrap();
        assert!(
            compare(&fake, &path, &tags(), "p_test", UUID)
                .await
                .is_err()
        );
    }
}
