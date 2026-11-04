//! Forced recovery: an operator cancels one exact interrupted command. It
//! never runs VPN changes or claims the old mutation succeeded. Prepare
//! publishes a ticket and a dispatch fence that stops a late original
//! request; retire, after an agent restart and once no orphan holds the
//! journal lock, closes the command with a receipt. Replaces
//! `force-recovery.py`.
//!
//! Errors are `force_*` codes: they never carry the request.
use std::fs;
use std::io::{Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

use rusqlite::{OptionalExtension, params};
use serde_json::{Map, Value, json};

use super::{
    canonical, commands_schema, database, disabled, fences_schema, has_table, open_readonly,
    sha256_hex, sidecar, sorted_dumps,
};

/// The node operations a ticket may retire.
pub(crate) const NODE_ACTIONS: [&str; 12] = [
    "bootstrap",
    "reinstall_keep",
    "reinstall_clean",
    "cleanup_runtime",
    "install_docker",
    "sync_runtime",
    "check_ports",
    "open_ports",
    "sync_env",
    "sync_xray",
    "regenerate_entropy",
    "reconcile_access",
];

fn code(code: &str) -> String {
    code.to_string()
}

/// A lowercase hyphenated UUID.
fn uuid(value: &Value) -> Result<&str, String> {
    let text = value
        .as_str()
        .ok_or_else(|| code("force_invalid_request"))?;
    crate::fencing::valid_force_identity(text)
        .then_some(text)
        .ok_or_else(|| code("force_invalid_identity"))
}

fn node_key(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .enumerate()
            .all(|(index, b)| b.is_ascii_alphanumeric() || (index > 0 && matches!(b, b'_' | b'-')))
}

/// A checked ticket request.
pub(crate) struct Spec {
    pub(crate) ticket_id: String,
    pub(crate) node_key: String,
    pub(crate) command_id: String,
    pub(crate) kind: String,
    body: Value,
    fingerprint: String,
    binding: String,
}

fn protocols(list: &Value) -> bool {
    list.as_array().is_some_and(|items| {
        items.len() <= 2
            && items
                .iter()
                .all(|item| matches!(item.as_str(), Some("awg" | "xray")))
            && items
                .iter()
                .enumerate()
                .all(|(index, item)| !items[..index].contains(item))
    })
}

impl Spec {
    pub(crate) fn check(spec: &Value) -> Result<Self, String> {
        let invalid = || code("force_invalid_request");
        let map = spec.as_object().ok_or_else(invalid)?;
        let keys = ["ticket_id", "node_key", "command_id", "kind", "intent"];
        if map.len() != keys.len() || keys.iter().any(|key| !map.contains_key(*key)) {
            return Err(invalid());
        }
        let ticket_id = uuid(&map["ticket_id"])?.to_string();
        let command_id = uuid(&map["command_id"])?.to_string();
        let node = map["node_key"].as_str().ok_or_else(invalid)?;
        if !node_key(node) {
            return Err(code("force_invalid_identity"));
        }
        let kind = map["kind"].as_str().ok_or_else(invalid)?;
        let intent = map["intent"].as_object().ok_or_else(invalid)?;
        if !matches!(kind, "settings" | "profile" | "node") {
            return Err(invalid());
        }
        let body = if kind == "node" {
            intent.get("intent")
        } else {
            Some(&map["intent"])
        };
        let body = body
            .and_then(Value::as_object)
            .ok_or_else(|| code("force_invalid_identity"))?;
        if body.get("command_id").and_then(Value::as_str) != Some(&command_id) {
            return Err(code("force_invalid_identity"));
        }
        if !body
            .get("revision")
            .is_some_and(|revision| revision.as_i64().is_some_and(|r| r >= 1))
        {
            return Err(code("force_invalid_revision"));
        }
        if kind != "profile" && body.get("node_key").and_then(Value::as_str) != Some(node) {
            return Err(code("force_invalid_identity"));
        }
        let only = |map: &Map<String, Value>, keys: &[&str]| {
            map.len() == keys.len() && keys.iter().all(|key| map.contains_key(*key))
        };
        match kind {
            "profile" => {
                let runtime_name = body.get("runtime_name").and_then(Value::as_str);
                let valid = only(
                    body,
                    &["command_id", "protocol", "runtime_name", "revision", "action", "uuid", "short_id"],
                ) && matches!(body["protocol"].as_str(), Some("awg" | "xray"))
                    && matches!(body["action"].as_str(), Some("ensure" | "delete"))
                    // The profile name pattern is the command id's.
                    && runtime_name.is_some_and(super::profile::command_id);
                if !valid {
                    return Err(invalid());
                }
            }
            "settings" => {
                let required = [
                    "kind",
                    "node_key",
                    "command_id",
                    "revision",
                    "protocols",
                    "settings",
                ];
                let valid = required.iter().all(|key| body.contains_key(*key))
                    && body
                        .keys()
                        .all(|key| required.contains(&key.as_str()) || key == "protocol_plan")
                    && body["kind"] == "node_settings";
                if !valid {
                    return Err(invalid());
                }
            }
            _ => {
                let valid = only(intent, &["action", "intent"])
                    && intent["action"]
                        .as_str()
                        .is_some_and(|action| NODE_ACTIONS.contains(&action));
                if !valid {
                    return Err(invalid());
                }
            }
        }
        if kind != "profile" {
            if !body.get("protocols").is_some_and(protocols)
                || !body.get("settings").is_some_and(Value::is_object)
            {
                return Err(invalid());
            }
            if let Some(plan) = body.get("protocol_plan") {
                let valid = plan.as_object().is_some_and(|plan| {
                    only(plan, &["remove_protocols"]) && protocols(&plan["remove_protocols"])
                });
                if !valid {
                    return Err(invalid());
                }
            }
        }
        // A node operation's fingerprint is its journal's: Python's default
        // separators.
        let fingerprint = sha256_hex(
            if kind == "node" {
                sorted_dumps(&map["intent"])
            } else {
                canonical(&map["intent"])
            }
            .as_bytes(),
        );
        Ok(Self {
            ticket_id,
            node_key: node.into(),
            command_id,
            kind: kind.into(),
            body: Value::Object(body.clone()),
            fingerprint,
            binding: sha256_hex(canonical(spec).as_bytes()),
        })
    }

    fn receipt(&self) -> Value {
        json!({"ticket_id": self.ticket_id, "node_key": self.node_key,
               "command_id": self.command_id, "kind": self.kind,
               "forced": true, "outcome_unknown": true})
    }
}

/// The ticket directory, private to this user.
fn private_directory(path: &Path) -> Result<PathBuf, String> {
    let untrusted = || code("force_ticket_store_untrusted");
    if path.ancestors().any(Path::is_symlink) {
        return Err(untrusted());
    }
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path)
        .map_err(|_| untrusted())?;
    let info = fs::symlink_metadata(path).map_err(|_| untrusted())?;
    // SAFETY: geteuid has no preconditions.
    let euid = unsafe { libc::geteuid() };
    if !info.is_dir() || info.uid() != euid || info.mode() & 0o077 != 0 {
        return Err(untrusted());
    }
    Ok(path.to_path_buf())
}

fn read_record(path: &Path) -> Result<Value, String> {
    let mut text = String::new();
    fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .and_then(|mut file| file.read_to_string(&mut text))
        .map_err(|_| code("force_recovery_failed"))?;
    serde_json::from_str(&text).map_err(|_| code("force_recovery_failed"))
}

/// Publishes a complete, durable record once; a crash never leaves an empty
/// one. An existing record must carry the same binding.
fn publish(dir: &Path, path: &Path, value: Value) -> Result<Value, String> {
    let failed = |_| code("force_recovery_failed");
    let staged = dir.join(format!(
        ".staged-{}",
        crate::protocols::deploy::random_hex(8)?
    ));
    let result = (|| {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&staged)
            .map_err(failed)?;
        file.write_all(sorted_dumps(&value).as_bytes())
            .and_then(|()| file.sync_all())
            .map_err(failed)?;
        let value = match fs::hard_link(&staged, path) {
            Ok(()) => value,
            Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => {
                let existing = read_record(path)?;
                if existing["binding"] != value["binding"] {
                    return Err(code("force_ticket_conflict"));
                }
                existing
            }
            Err(err) => return Err(failed(err)),
        };
        fs::File::open(dir)
            .and_then(|dir| dir.sync_all())
            .map_err(failed)?;
        Ok(value)
    })();
    let _ = fs::remove_file(&staged);
    result
}

/// Publishes the ticket and the dispatch fence of the command.
pub(crate) fn prepare(
    spec: &Spec,
    instance: &str,
    dir: &Path,
    journal: &Path,
) -> Result<Value, String> {
    if instance.is_empty() {
        return Err(code("force_instance_missing"));
    }
    if journal.exists() || journal.is_symlink() {
        if journal.is_symlink() {
            return Err(code("force_journal_untrusted"));
        }
        let connection = open_readonly(journal).map_err(|_| code("force_recovery_failed"))?;
        let row: Option<(String, String)> =
            if has_table(&connection, "commands").map_err(|_| code("force_recovery_failed"))? {
                connection
                    .query_row(
                        "SELECT fingerprint,status FROM commands WHERE id=?",
                        [&spec.command_id],
                        |row| Ok((row.get(0)?, row.get(1)?)),
                    )
                    .optional()
                    .map_err(|_| code("force_recovery_failed"))?
            } else {
                None
            };
        if let Some((fingerprint, status)) = row {
            if fingerprint != spec.fingerprint {
                return Err(code("force_command_conflict"));
            }
            if !matches!(status.as_str(), "running" | "superseded" | "retired") {
                return Err(code("force_command_already_terminal"));
            }
        }
    }
    let dir = private_directory(dir)?;
    let ticket = publish(
        &dir,
        &dir.join(format!("{}.json", spec.ticket_id)),
        json!({"binding": spec.binding, "old_instance": instance}),
    )?;
    publish(
        &dir,
        &dir.join(format!("command-{}.fence", spec.command_id)),
        json!({"binding": spec.binding, "command_id": spec.command_id, "node_key": spec.node_key}),
    )?;
    Ok(
        json!({"ticket_id": spec.ticket_id, "node_key": spec.node_key,
              "restart_required": ticket["old_instance"] == instance}),
    )
}

/// Closes the command with a forced receipt and advances its fence, once a
/// restarted agent holds the journal lock nobody else holds.
pub(crate) fn retire(
    spec: &Spec,
    instance: &str,
    dir: &Path,
    journal: &Path,
) -> Result<Value, String> {
    let dir = private_directory(dir)?;
    let ticket = read_record(&dir.join(format!("{}.json", spec.ticket_id)))?;
    let fence = read_record(&dir.join(format!("command-{}.fence", spec.command_id)))?;
    if ticket["binding"] != spec.binding.as_str() || fence["binding"] != spec.binding.as_str() {
        return Err(code("force_ticket_conflict"));
    }
    if instance.is_empty() || ticket["old_instance"] == instance {
        return Err(code("force_restart_required"));
    }
    // Every mutation subprocess inherits this lock: a new agent alone does
    // not prove an old child stopped.
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(sidecar(journal, ".lock"))
        .map_err(|_| code("force_recovery_failed"))?;
    if lock.try_lock().is_err() {
        return Err(code("force_execution_still_running"));
    }
    if disabled(journal) {
        return Err(code("force_agent_removal_pending"));
    }
    if journal.is_symlink() {
        return Err(code("force_journal_untrusted"));
    }
    let failed = |_: String| code("force_recovery_failed");
    let connection = super::open(journal).map_err(failed)?;
    commands_schema(&connection).map_err(failed)?;
    let row: Option<(String, String, Option<String>)> = connection
        .query_row(
            "SELECT fingerprint,status,response FROM commands WHERE id=?",
            [&spec.command_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()
        .map_err(|err| failed(database(err)))?;
    let terminal = if spec.kind == "node" {
        "retired"
    } else {
        "superseded"
    };
    let receipt = spec.receipt();
    if let Some((fingerprint, status, response)) = &row {
        if *fingerprint != spec.fingerprint {
            return Err(code("force_command_conflict"));
        }
        if status == terminal {
            let old: Value = serde_json::from_str(response.as_deref().unwrap_or("{}"))
                .map_err(|_| code("force_already_retired"))?;
            if old != receipt {
                return Err(code("force_already_retired"));
            }
            return Ok(receipt);
        }
        if status != "running" {
            return Err(code("force_command_already_terminal"));
        }
    }
    let stored = sorted_dumps(&receipt);
    let transaction = connection
        .unchecked_transaction()
        .map_err(|err| failed(database(err)))?;
    let changed = if row.is_some() {
        transaction.execute(
            "UPDATE commands SET status=?,response=? WHERE id=? AND status=?",
            params![terminal, stored, spec.command_id, "running"],
        )
    } else {
        transaction.execute(
            "INSERT INTO commands VALUES (?,?,?,?,?)",
            params![
                spec.command_id,
                spec.fingerprint,
                terminal,
                stored,
                instance
            ],
        )
    };
    changed.map_err(|err| failed(database(err)))?;
    let body = &spec.body;
    match spec.kind.as_str() {
        "settings" => transaction
            .execute_batch("CREATE TABLE IF NOT EXISTS node_settings_fence (node_key TEXT PRIMARY KEY, revision INTEGER NOT NULL, command_id TEXT NOT NULL)")
            .and_then(|()| {
                transaction.execute(
                    "INSERT INTO node_settings_fence VALUES (?,?,?) ON CONFLICT(node_key) DO UPDATE SET revision=excluded.revision,command_id=excluded.command_id WHERE excluded.revision>node_settings_fence.revision",
                    params![spec.node_key, body["revision"].as_i64(), spec.command_id],
                )
            })
            .map(|_| ()),
        "profile" => {
            fences_schema(&transaction).map_err(failed)?;
            transaction
                .execute(
                    "INSERT INTO fences VALUES (?,?,?,?,?) ON CONFLICT(protocol,profile) DO UPDATE SET revision=excluded.revision,command_id=excluded.command_id,action=excluded.action WHERE excluded.revision>fences.revision",
                    params![
                        body["protocol"].as_str(),
                        body["runtime_name"].as_str(),
                        body["revision"].as_i64(),
                        spec.command_id,
                        body["action"].as_str()
                    ],
                )
                .map(|_| ())
        }
        _ => Ok(()),
    }
    .map_err(|err| failed(database(err)))?;
    transaction.commit().map_err(|err| failed(database(err)))?;
    drop(lock);
    Ok(receipt)
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::PermissionsExt;

    use rusqlite::Connection;

    use super::*;
    use crate::intents::profile::{Intent, apply};
    use crate::intents::{Lock, Response, test_journal};

    const NODE: &str = "node-1";

    fn id(seed: u8) -> String {
        format!("{seed:08x}-0000-4000-8000-{seed:012x}")
    }

    fn spec(kind: &str, revision: i64, command: &str) -> Value {
        let intent = match kind {
            "settings" => json!({"kind": "node_settings", "command_id": command, "node_key": NODE,
                "revision": revision, "protocols": ["awg"],
                "settings": {"public_host": "127.0.0.1", "awg_port": 51820}}),
            "profile" => {
                json!({"command_id": command, "protocol": "awg", "runtime_name": "test-user",
                "revision": revision, "action": "ensure", "uuid": "", "short_id": ""})
            }
            _ => {
                json!({"action": "sync_runtime", "intent": {"command_id": command, "node_key": NODE,
                "revision": revision, "protocols": ["awg"], "settings": {"awg_port": 51820}}})
            }
        };
        json!({"ticket_id": command.replacen("-0000-", "-1111-", 1), "node_key": NODE, "command_id": command,
               "kind": kind, "intent": intent})
    }

    struct Store {
        dir: PathBuf,
        journal: PathBuf,
    }

    fn store(name: &str) -> Store {
        let journal = test_journal(&format!("force-{name}"));
        Store {
            dir: journal.with_file_name("tickets"),
            journal,
        }
    }

    impl Store {
        fn prepare(&self, spec: &Value, instance: &str) -> Result<Value, String> {
            prepare(&Spec::check(spec)?, instance, &self.dir, &self.journal)
        }

        fn retire(&self, spec: &Value, instance: &str) -> Result<Value, String> {
            retire(&Spec::check(spec)?, instance, &self.dir, &self.journal)
        }

        fn query<T: rusqlite::types::FromSql>(&self, sql: &str) -> T {
            Connection::open(&self.journal)
                .unwrap()
                .query_row(sql, [], |row| row.get(0))
                .unwrap()
        }
    }

    #[test]
    fn every_node_operation_can_be_retired() {
        for (seed, action) in NODE_ACTIONS.iter().enumerate() {
            let store = store(&format!("action-{action}"));
            let mut node = spec("node", 2, &id(seed as u8));
            node["intent"]["action"] = json!(action);
            store.prepare(&node, "before").unwrap();
            assert_eq!(
                store.retire(&node, "after").unwrap()["outcome_unknown"],
                true
            );
            let status: String = store.query(&format!(
                "SELECT status FROM commands WHERE id='{}'",
                id(seed as u8)
            ));
            assert_eq!(status, "retired");
        }
    }

    #[test]
    fn retirement_needs_a_restarted_agent_and_a_free_journal_lock() {
        let store = store("restart");
        let settings = spec("settings", 2, &id(1));
        assert_eq!(
            store.prepare(&settings, "").unwrap_err(),
            "force_instance_missing"
        );
        let ticket = store.prepare(&settings, "inst-1").unwrap();
        assert_eq!(ticket["restart_required"], true);
        assert_eq!(
            store.retire(&settings, "inst-1").unwrap_err(),
            "force_restart_required"
        );
        let orphan = Lock::exclusive(&store.journal).unwrap();
        assert_eq!(
            store.retire(&settings, "inst-2").unwrap_err(),
            "force_execution_still_running"
        );
        drop(orphan);
        let receipt = store.retire(&settings, "inst-2").unwrap();
        assert_eq!(
            (receipt["forced"].clone(), receipt["node_key"].clone()),
            (json!(true), json!(NODE))
        );
    }

    #[test]
    fn requests_are_checked_before_anything_is_written() {
        let settings = spec("settings", 2, &id(1));
        let reject = |change: &dyn Fn(&mut Value), expected: &str| {
            let mut bad = settings.clone();
            change(&mut bad);
            assert_eq!(Spec::check(&bad).err().as_deref(), Some(expected), "{bad}");
        };
        reject(
            &|spec| {
                spec.as_object_mut().unwrap().remove("kind");
            },
            "force_invalid_request",
        );
        reject(
            &|spec| spec["ticket_id"] = json!("0".repeat(32)),
            "force_invalid_identity",
        );
        reject(
            &|spec| spec["ticket_id"] = json!("not-a-uuid"),
            "force_invalid_identity",
        );
        reject(
            &|spec| spec["node_key"] = json!("!invalid_node!"),
            "force_invalid_identity",
        );
        reject(
            &|spec| spec["command_id"] = json!(id(9)),
            "force_invalid_identity",
        );
        reject(
            &|spec| spec["intent"]["revision"] = json!(0),
            "force_invalid_revision",
        );
        reject(
            &|spec| spec["intent"]["revision"] = json!("two"),
            "force_invalid_revision",
        );
        let mut profile = spec("profile", 2, &id(1));
        profile["intent"]["action"] = json!("restart");
        assert_eq!(
            Spec::check(&profile).err().as_deref(),
            Some("force_invalid_request")
        );
        assert!(Spec::check(&json!({"sensitive_token": "secret"})).is_err());
    }

    #[test]
    fn retirement_keeps_higher_revisions_of_the_fences() {
        let store = store("fences");
        for (seed, revision) in [(1, 5), (2, 3), (3, 8)] {
            let settings = spec("settings", revision, &id(seed));
            store.prepare(&settings, "old").unwrap();
            store.retire(&settings, "new").unwrap();
        }
        let revision: i64 =
            store.query("SELECT revision FROM node_settings_fence WHERE node_key='node-1'");
        assert_eq!(revision, 8);
        for (seed, revision) in [(4, 5), (5, 3)] {
            let profile = spec("profile", revision, &id(seed));
            store.prepare(&profile, "old").unwrap();
            store.retire(&profile, "new").unwrap();
        }
        let command: String =
            store.query("SELECT command_id FROM fences WHERE profile='test-user'");
        assert_eq!(command, id(4));
    }

    #[test]
    fn a_retired_command_stays_fenced_and_replays_its_receipt() {
        let store = store("tombstone");
        let profile = spec("profile", 2, &id(1));
        store.prepare(&profile, "inst-1").unwrap();
        let fence = read_record(&store.dir.join(format!("command-{}.fence", id(1)))).unwrap();
        assert_eq!(
            (fence["command_id"].clone(), fence["node_key"].clone()),
            (json!(id(1)), json!(NODE))
        );
        let receipt = store.retire(&profile, "inst-2").unwrap();
        let status: String = store.query("SELECT status FROM commands");
        assert_eq!(status, "superseded");
        let original = Intent::parse(&profile["intent"]).unwrap();
        let err = apply(&original, &store.journal, "inst-3", &mut |_: &Intent, _| {
            Ok(Response::ok())
        })
        .unwrap_err();
        assert_eq!(err, "previous command did not complete successfully");
        store.prepare(&profile, "inst-2").unwrap();
        assert_eq!(store.retire(&profile, "inst-3").unwrap(), receipt);
        let mut changed = profile.clone();
        changed["intent"]["revision"] = json!(99);
        assert_eq!(
            store.prepare(&changed, "inst-4").unwrap_err(),
            "force_command_conflict"
        );
    }

    #[test]
    fn a_completed_success_is_never_overwritten() {
        let store = store("success");
        let settings = spec("settings", 2, &id(1));
        let checked = Spec::check(&settings).unwrap();
        let connection = Connection::open(&store.journal).unwrap();
        commands_schema(&connection).unwrap();
        connection
            .execute(
                "INSERT INTO commands VALUES (?, ?, 'succeeded', '{}', 'inst-0')",
                params![id(1), checked.fingerprint],
            )
            .unwrap();
        assert_eq!(
            store.prepare(&settings, "inst-1").unwrap_err(),
            "force_command_already_terminal"
        );
        let dir = private_directory(&store.dir).unwrap();
        publish(
            &dir,
            &dir.join(format!("{}.json", checked.ticket_id)),
            json!({"binding": checked.binding, "old_instance": "inst-1"}),
        )
        .unwrap();
        publish(
            &dir,
            &dir.join(format!("command-{}.fence", id(1))),
            json!({"binding": checked.binding, "command_id": id(1), "node_key": NODE}),
        )
        .unwrap();
        assert_eq!(
            store.retire(&settings, "inst-2").unwrap_err(),
            "force_command_already_terminal"
        );
        let status: String = store.query("SELECT status FROM commands");
        assert_eq!(status, "succeeded");
    }

    #[test]
    fn without_a_journal_retirement_creates_the_tombstone() {
        let store = store("absent");
        let profile = spec("profile", 2, &id(1));
        store.prepare(&profile, "inst-1").unwrap();
        assert!(!store.journal.exists());
        assert_eq!(
            store.retire(&profile, "inst-2").unwrap()["command_id"],
            id(1)
        );
        assert!(store.journal.exists());
    }

    #[test]
    fn untrusted_stores_and_removal_refuse() {
        let store = store("untrusted");
        let settings = spec("settings", 2, &id(1));
        let base = store.journal.parent().unwrap();
        fs::create_dir_all(base.join("real")).unwrap();
        std::os::unix::fs::symlink(base.join("real"), base.join("linked")).unwrap();
        let checked = Spec::check(&settings).unwrap();
        assert_eq!(
            prepare(&checked, "inst-1", &base.join("linked"), &store.journal).unwrap_err(),
            "force_ticket_store_untrusted"
        );
        fs::create_dir_all(base.join("open")).unwrap();
        fs::set_permissions(base.join("open"), fs::Permissions::from_mode(0o755)).unwrap();
        assert_eq!(
            prepare(&checked, "inst-1", &base.join("open"), &store.journal).unwrap_err(),
            "force_ticket_store_untrusted"
        );
        fs::write(base.join("real.sqlite3"), "").unwrap();
        std::os::unix::fs::symlink(base.join("real.sqlite3"), base.join("link.sqlite3")).unwrap();
        assert_eq!(
            prepare(&checked, "inst-1", &store.dir, &base.join("link.sqlite3")).unwrap_err(),
            "force_journal_untrusted"
        );
        store.prepare(&settings, "inst-1").unwrap();
        fs::write(sidecar(&store.journal, ".disabled"), "").unwrap();
        assert_eq!(
            store.retire(&settings, "inst-2").unwrap_err(),
            "force_agent_removal_pending"
        );
    }

    #[test]
    fn a_ticket_is_published_once_and_conflicts_are_refused() {
        let store = store("publish");
        let settings = spec("settings", 2, &id(1));
        let first = store.prepare(&settings, "inst-1").unwrap();
        assert_eq!(store.prepare(&settings, "inst-1").unwrap(), first);
        let ticket = read_record(&store.dir.join(format!(
            "{}.json",
            Spec::check(&settings).unwrap().ticket_id
        )))
        .unwrap();
        assert_eq!(ticket["old_instance"], "inst-1");
        let mut conflicting = settings.clone();
        conflicting["node_key"] = json!("node-2");
        conflicting["intent"]["node_key"] = json!("node-2");
        assert_eq!(
            store.prepare(&conflicting, "inst-1").unwrap_err(),
            "force_ticket_conflict"
        );
    }
}
