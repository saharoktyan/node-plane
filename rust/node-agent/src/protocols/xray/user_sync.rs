//! `node-plane-agent xray-user add|delete …`: one VLESS identity in Xray's
//! config file and in the live HandlerService, kept in sync.
//!
//! Replaces `xray-user-api.py`. The profile intent journal still calls
//! `xray-add-user-existing.sh` and `xray-del-user.sh`, which only start this
//! subcommand. The config file is written before the live change: a retry
//! finishes a partial update, and a restarted Xray loads the same users.
use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::{Value, json};

use super::api::cli::{valid_email, valid_uuid};
use super::api::{DEFAULT_ADDRESS, InboundUser, XrayApi};

const VISION: &str = "xtls-rprx-vision";
const DEPLOY: &str = "/opt/node-plane-runtime/deploy-xray.sh";

/// What Xray holds for one user: UUID and flow.
pub(crate) type Live = (String, String);

/// Live user operations; Xray's HandlerService on nodes, a fake in tests.
pub(crate) trait Users {
    async fn users(&self, tag: &str) -> Result<BTreeMap<String, Live>, String>;
    async fn add(&self, tag: &str, user: &InboundUser) -> Result<(), String>;
    /// Also closes the user's established connections (Node Plane image).
    async fn remove(&self, tag: &str, email: &str) -> Result<(), String>;
}

impl Users for XrayApi {
    async fn users(&self, tag: &str) -> Result<BTreeMap<String, Live>, String> {
        Ok(self
            .inbound_users(tag)
            .await
            .map_err(|err| err.to_string())?
            .into_iter()
            .map(|user| (user.email, (user.id, user.flow)))
            .collect())
    }

    async fn add(&self, tag: &str, user: &InboundUser) -> Result<(), String> {
        self.add_vless_user(tag, user)
            .await
            .map_err(|err| err.to_string())
    }

    async fn remove(&self, tag: &str, email: &str) -> Result<(), String> {
        self.remove_user(tag, email, true)
            .await
            .map_err(|err| err.to_string())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Action {
    Add { uuid: String },
    Delete,
}

/// The Xray config and its TCP (Vision) and XHTTP inbounds.
pub(crate) struct Target {
    pub(crate) config: PathBuf,
    pub(crate) tcp_tag: String,
    pub(crate) xhttp_tag: String,
}

fn client_name(client: &Value) -> Option<&str> {
    client
        .get("email")
        .or_else(|| client.get("name"))
        .and_then(Value::as_str)
}

fn clients<'a>(config: &'a mut Value, tag: &str) -> Result<&'a mut Vec<Value>, String> {
    config["inbounds"]
        .as_array_mut()
        .and_then(|inbounds| {
            inbounds
                .iter_mut()
                .find(|inbound| inbound["tag"].as_str() == Some(tag))
        })
        .and_then(|inbound| inbound["settings"]["clients"].as_array_mut())
        .ok_or_else(|| "Xray TCP/XHTTP inbound is missing".to_string())
}

pub(crate) fn write_config(path: &Path, config: &Value) -> Result<(), String> {
    let dir = path.parent().unwrap_or(Path::new("."));
    let failed = |err: std::io::Error| format!("Xray config write failed: {err}");
    let candidate = dir.join(format!(".xray-config-{}.json", std::process::id()));
    let result = (|| {
        let mut file = fs::File::create(&candidate)?;
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(fs::Permissions::from_mode(0o600))?;
        let text = serde_json::to_string_pretty(config).map_err(std::io::Error::other)?;
        file.write_all(text.as_bytes())?;
        file.sync_all()?;
        fs::rename(&candidate, path)?;
        fs::File::open(dir)?.sync_all()
    })();
    if result.is_err() {
        let _ = fs::remove_file(&candidate);
    }
    result.map_err(failed)
}

/// Changes `config` for the action and returns whether it changed.
fn edit_config(
    config: &mut Value,
    target: &Target,
    name: &str,
    action: &Action,
) -> Result<bool, String> {
    let mut changed = false;
    for tag in [&target.tcp_tag, &target.xhttp_tag] {
        let clients = clients(config, tag)?;
        let old = clients
            .iter()
            .find(|c| client_name(c) == Some(name))
            .cloned();
        clients.retain(|c| client_name(c) != Some(name));
        match action {
            Action::Add { uuid } => {
                let mut client = json!({"id": uuid, "name": name, "email": name});
                if tag == &target.tcp_tag {
                    client["flow"] = json!(VISION);
                }
                changed |= old.as_ref() != Some(&client);
                clients.push(client);
            }
            Action::Delete => changed |= old.is_some(),
        }
    }
    Ok(changed)
}

/// Applies the action to the config file and the live users. The caller
/// holds the config lock.
pub(crate) async fn sync(
    users: &impl Users,
    target: &Target,
    config: &mut Value,
    name: &str,
    action: &Action,
) -> Result<(), String> {
    let tags = [target.tcp_tag.as_str(), target.xhttp_tag.as_str()];
    let mut live = BTreeMap::new();
    for tag in tags {
        live.insert(tag, users.users(tag).await?.remove(name));
    }
    // Persist first: before enabling access, so a retry can finish it; before
    // revoking, so a restarted Xray does not restore access from the file.
    if edit_config(config, target, name, action)? {
        write_config(&target.config, config)?;
    }
    for tag in tags {
        match action {
            Action::Add { uuid } => {
                let flow = if tag == target.tcp_tag { VISION } else { "" };
                let desired = (uuid.clone(), flow.to_string());
                if live[tag].as_ref() == Some(&desired) {
                    continue;
                }
                if live[tag].is_some() {
                    users.remove(tag, name).await?;
                }
                let user = InboundUser {
                    email: name.to_string(),
                    id: uuid.clone(),
                    flow: flow.to_string(),
                };
                users.add(tag, &user).await?;
                if users.users(tag).await?.get(name) != Some(&desired) {
                    return Err(format!("Xray user mismatch after add on {tag}"));
                }
            }
            Action::Delete => {
                if live[tag].is_some() {
                    users.remove(tag, name).await?;
                }
                if users.users(tag).await?.contains_key(name) {
                    return Err(format!("Xray user still present on {tag}"));
                }
            }
        }
    }
    Ok(())
}

/// Whether Xray must be redeployed for the API, and whether HandlerService
/// must be added first. A mounted config file (instead of its directory)
/// would not see the atomic rename.
fn api_change(config: &Value, file_mount: bool) -> Option<bool> {
    let has_handler = config["api"]["services"]
        .as_array()
        .is_some_and(|list| list.iter().any(|s| s == "HandlerService"));
    (!has_handler || file_mount).then_some(!has_handler)
}

fn ensure_api(target: &Target, config: &mut Value, container: &str) -> Result<(), String> {
    let output = Command::new("docker")
        .args([
            "inspect",
            "-f",
            "{{range .Mounts}}{{println .Destination}}{{end}}",
            container,
        ])
        .output()
        .map_err(|err| format!("docker failed: {err}"))?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }
    let file_mount = String::from_utf8_lossy(&output.stdout)
        .lines()
        .any(|line| line == "/etc/xray/config.json");
    let Some(add_handler) = api_change(config, file_mount) else {
        return Ok(());
    };
    if add_handler {
        let services = &mut config["api"]["services"];
        if !services.is_array() {
            *services = json!([]);
        }
        services
            .as_array_mut()
            .expect("services is an array")
            .push(json!("HandlerService"));
        write_config(&target.config, config)?;
    }
    let status = Command::new(DEPLOY)
        .status()
        .map_err(|err| format!("{DEPLOY} failed: {err}"))?;
    if !status.success() {
        return Err(format!("{DEPLOY} failed"));
    }
    Ok(())
}

async fn execute(args: &[String]) -> Result<(), String> {
    let usage =
        || "usage: xray-user add|delete CONFIG CONTAINER TCP_TAG XHTTP_TAG NAME [UUID]".to_string();
    let [
        command,
        config,
        container,
        tcp_tag,
        xhttp_tag,
        name,
        rest @ ..,
    ] = args
    else {
        return Err(usage());
    };
    let action = match (command.as_str(), rest) {
        ("add", [uuid]) => {
            let uuid = uuid.to_ascii_lowercase();
            if !valid_uuid(&uuid) {
                return Err("invalid uuid".into());
            }
            Action::Add { uuid }
        }
        ("delete", []) => Action::Delete,
        _ => return Err(usage()),
    };
    if !valid_email(name) {
        return Err("invalid profile name".into());
    }
    let target = Target {
        config: PathBuf::from(config),
        tcp_tag: tcp_tag.clone(),
        xhttp_tag: xhttp_tag.clone(),
    };
    let lock = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(target.config.with_file_name(format!(
            "{}.lock",
            target.config.file_name().and_then(|n| n.to_str()).unwrap_or("config.json")
        )))
        .map_err(|err| format!("Xray config lock unavailable: {err}"))?;
    lock.lock()
        .map_err(|err| format!("Xray config lock unavailable: {err}"))?;
    let raw = fs::read_to_string(&target.config).map_err(|err| err.to_string())?;
    let mut config: Value = serde_json::from_str(&raw).map_err(|err| err.to_string())?;
    clients(&mut config, tcp_tag)?;
    clients(&mut config, xhttp_tag)?;
    ensure_api(&target, &mut config, container)?;
    // Tests point helpers at a published API port; nodes use the default.
    let address = std::env::var("NODE_PLANE_XRAY_API").unwrap_or_else(|_| DEFAULT_ADDRESS.into());
    let api = XrayApi::connect(&address)
        .await
        .map_err(|err| err.to_string())?;
    sync(&api, &target, &mut config, name, &action).await
}

/// Runs the subcommand when the agent was started as `xray-user …`.
pub(crate) async fn run(args: &[String]) -> Option<i32> {
    let (first, rest) = args.split_first()?;
    if first != "xray-user" {
        return None;
    }
    Some(match execute(rest).await {
        Ok(()) => {
            println!("OK");
            0
        }
        Err(err) => {
            eprintln!("xray user sync failed: {err}");
            1
        }
    })
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use super::*;

    const UUID: &str = "11111111-1111-4111-8111-111111111111";
    const OTHER: &str = "22222222-2222-4222-8222-222222222222";

    /// Live users per tag; `fail_add` makes the add on that tag fail once.
    #[derive(Default)]
    struct Fake {
        live: RefCell<BTreeMap<String, BTreeMap<String, Live>>>,
        fail_add: RefCell<Option<String>>,
        fail_remove: RefCell<Option<String>>,
        removed: RefCell<Vec<String>>,
    }

    impl Users for Fake {
        async fn users(&self, tag: &str) -> Result<BTreeMap<String, Live>, String> {
            Ok(self.live.borrow().get(tag).cloned().unwrap_or_default())
        }
        async fn add(&self, tag: &str, user: &InboundUser) -> Result<(), String> {
            if self.fail_add.borrow().as_deref() == Some(tag) {
                self.fail_add.borrow_mut().take();
                return Err("refused".into());
            }
            self.live
                .borrow_mut()
                .entry(tag.into())
                .or_default()
                .insert(user.email.clone(), (user.id.clone(), user.flow.clone()));
            Ok(())
        }
        async fn remove(&self, tag: &str, email: &str) -> Result<(), String> {
            if self.fail_remove.borrow().as_deref() == Some(tag) {
                self.fail_remove.borrow_mut().take();
                return Err("refused".into());
            }
            self.removed.borrow_mut().push(tag.into());
            if let Some(users) = self.live.borrow_mut().get_mut(tag) {
                users.remove(email);
            }
            Ok(())
        }
    }

    fn fixture(name: &str) -> (Target, Value) {
        let dir = std::env::temp_dir().join(format!("np-xray-user-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let config = json!({"inbounds": [
            {"tag": "reality-tcp", "settings": {"clients": [{"id": OTHER, "email": "bob"}]}},
            {"tag": "reality-xhttp", "settings": {"clients": []}},
        ]});
        let target = Target {
            config: dir.join("config.json"),
            tcp_tag: "reality-tcp".into(),
            xhttp_tag: "reality-xhttp".into(),
        };
        write_config(&target.config, &config).unwrap();
        (target, config)
    }

    fn saved(target: &Target) -> Value {
        serde_json::from_str(&fs::read_to_string(&target.config).unwrap()).unwrap()
    }

    fn add(uuid: &str) -> Action {
        Action::Add { uuid: uuid.into() }
    }

    #[tokio::test]
    async fn add_writes_both_inbounds_with_vision_on_tcp_only_and_rotates_live() {
        let (target, mut config) = fixture("add_rotates");
        let users = Fake::default();
        sync(&users, &target, &mut config, "alice", &add(UUID))
            .await
            .unwrap();
        let file = saved(&target);
        assert_eq!(
            file["inbounds"][0]["settings"]["clients"][1],
            json!({"id": UUID, "name": "alice", "email": "alice", "flow": VISION})
        );
        assert_eq!(
            file["inbounds"][1]["settings"]["clients"][0],
            json!({"id": UUID, "name": "alice", "email": "alice"})
        );
        assert_eq!(
            users.live.borrow()["reality-tcp"]["alice"],
            (UUID.to_string(), VISION.to_string())
        );
        // A second add changes nothing; a new UUID replaces the live user.
        sync(&users, &target, &mut config, "alice", &add(UUID))
            .await
            .unwrap();
        assert!(users.removed.borrow().is_empty());
        sync(&users, &target, &mut config, "alice", &add(OTHER))
            .await
            .unwrap();
        assert_eq!(*users.removed.borrow(), ["reality-tcp", "reality-xhttp"]);
        assert_eq!(users.live.borrow()["reality-xhttp"]["alice"].0, OTHER);
    }

    #[tokio::test]
    async fn a_partial_add_is_on_disk_and_a_retry_finishes_it() {
        let (target, mut config) = fixture("partial");
        let users = Fake::default();
        *users.fail_add.borrow_mut() = Some("reality-xhttp".into());
        assert!(
            sync(&users, &target, &mut config, "alice", &add(UUID))
                .await
                .is_err()
        );
        assert_eq!(
            saved(&target)["inbounds"][1]["settings"]["clients"][0]["id"],
            UUID
        );
        assert!(
            !users
                .live
                .borrow()
                .get("reality-xhttp")
                .is_some_and(|u| u.contains_key("alice"))
        );
        sync(&users, &target, &mut config, "alice", &add(UUID))
            .await
            .unwrap();
        assert!(users.live.borrow()["reality-xhttp"].contains_key("alice"));
    }

    #[tokio::test]
    async fn delete_revokes_on_disk_before_live_and_keeps_others() {
        let (target, mut config) = fixture("delete");
        let users = Fake::default();
        sync(&users, &target, &mut config, "alice", &add(UUID))
            .await
            .unwrap();
        sync(&users, &target, &mut config, "alice", &Action::Delete)
            .await
            .unwrap();
        let file = saved(&target);
        assert_eq!(
            file["inbounds"][0]["settings"]["clients"],
            json!([{"id": OTHER, "email": "bob"}])
        );
        assert_eq!(file["inbounds"][1]["settings"]["clients"], json!([]));
        assert!(
            users
                .live
                .borrow()
                .values()
                .all(|tag| !tag.contains_key("alice"))
        );
        // Deleting an absent user succeeds without live calls.
        users.removed.borrow_mut().clear();
        sync(&users, &target, &mut config, "alice", &Action::Delete)
            .await
            .unwrap();
        assert!(users.removed.borrow().is_empty());
    }

    #[tokio::test]
    async fn a_stale_live_uuid_is_replaced_even_when_the_file_is_current() {
        let (target, mut config) = fixture("stale");
        let users = Fake::default();
        sync(&users, &target, &mut config, "alice", &add(UUID))
            .await
            .unwrap();
        users
            .live
            .borrow_mut()
            .get_mut("reality-tcp")
            .unwrap()
            .insert("alice".into(), (OTHER.into(), VISION.into()));
        sync(&users, &target, &mut config, "alice", &add(UUID))
            .await
            .unwrap();
        assert_eq!(*users.removed.borrow(), ["reality-tcp"]);
        assert_eq!(users.live.borrow()["reality-tcp"]["alice"].0, UUID);
    }

    #[tokio::test]
    async fn a_partial_delete_is_revoked_on_disk_and_a_retry_finishes_it() {
        let (target, mut config) = fixture("partial-delete");
        let users = Fake::default();
        sync(&users, &target, &mut config, "alice", &add(UUID))
            .await
            .unwrap();
        *users.fail_remove.borrow_mut() = Some("reality-xhttp".into());
        assert!(
            sync(&users, &target, &mut config, "alice", &Action::Delete)
                .await
                .is_err()
        );
        assert_eq!(
            saved(&target)["inbounds"][1]["settings"]["clients"],
            json!([])
        );
        assert!(users.live.borrow()["reality-xhttp"].contains_key("alice"));
        sync(&users, &target, &mut config, "alice", &Action::Delete)
            .await
            .unwrap();
        assert!(!users.live.borrow()["reality-xhttp"].contains_key("alice"));
    }

    #[test]
    fn a_missing_handler_or_a_mounted_file_needs_a_redeploy() {
        let with = json!({"api": {"services": ["StatsService", "HandlerService"]}});
        assert_eq!(api_change(&with, false), None);
        assert_eq!(api_change(&with, true), Some(false));
        assert_eq!(
            api_change(&json!({"api": {"services": ["StatsService"]}}), false),
            Some(true)
        );
        assert_eq!(api_change(&json!({}), false), Some(true));
    }

    #[tokio::test]
    async fn invalid_arguments_are_refused_before_any_change() {
        let args = |values: &[&str]| values.iter().map(|v| v.to_string()).collect::<Vec<_>>();
        for case in [
            vec!["xray-user", "add", "/x", "xray", "t", "x", "alice"],
            vec![
                "xray-user",
                "add",
                "/x",
                "xray",
                "t",
                "x",
                "alice",
                "not-a-uuid",
            ],
            vec!["xray-user", "delete", "/x", "xray", "t", "x", "-alice"],
            vec!["xray-user", "delete", "/x", "xray", "t", "x", "alice", UUID],
        ] {
            assert_eq!(run(&args(&case)).await, Some(1), "{case:?}");
        }
        assert!(run(&args(&["xray-api"])).await.is_none());
    }
}
