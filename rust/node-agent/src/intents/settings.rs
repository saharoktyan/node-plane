//! Node settings intents: one journaled, node-wide change of the protocol
//! settings, its read-only recovery and its repair. Replaces those commands
//! of `apply-profile-intent.py`.
use std::fs;
use std::io::Write;
use std::os::fd::RawFd;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

use chrono::Utc;
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Map, Value, json};

use super::profile::{command_id, timestamp};
use super::{
    Lock, Response, canonical, commands_schema, database, disabled, fences_schema, fingerprint,
    open, open_readonly, sha256_hex, sorted_dumps,
};
use crate::protocols::awg::client::all_fields;
use crate::protocols::awg::ports;

const FIELDS: [&str; 6] = [
    "kind",
    "node_key",
    "command_id",
    "revision",
    "protocols",
    "settings",
];

const SETTINGS: [&str; 15] = [
    "public_host",
    "xray_host",
    "xray_sni",
    "xray_tcp_port",
    "xray_xhttp_port",
    "xray_xhttp_path",
    "awg_public_host",
    "awg_port",
    "xray_fingerprint",
    "awg_interface",
    "awg_i1_preset",
    "awg_port_mode",
    "awg_parameters",
    "ipv6_outbound",
    "xray_bittorrent",
];

/// What a repair reports: whether the configs match and the containers run.
pub(crate) const REPAIR: [&str; 2] = ["config_matches", "containers_running"];

/// `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`
pub(crate) fn node_key(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .enumerate()
            .all(|(index, b)| b.is_ascii_alphanumeric() || (index > 0 && matches!(b, b'_' | b'-')))
}

fn host(value: &str) -> bool {
    let bytes = value.as_bytes();
    !value.contains("..")
        && bytes.first().is_some_and(u8::is_ascii_alphanumeric)
        && bytes.last().is_some_and(u8::is_ascii_alphanumeric)
        && bytes
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-'))
}

/// A list of distinct protocols.
fn protocol_list(value: &Value) -> Option<Vec<&str>> {
    let items = value.as_array()?;
    let list: Vec<&str> = items
        .iter()
        .map(|item| item.as_str().filter(|p| matches!(*p, "awg" | "xray")))
        .collect::<Option<_>>()?;
    (list.len() <= 2 && (list.len() < 2 || list[0] != list[1])).then_some(list)
}

/// A checked settings intent.
pub(crate) struct Settings<'a>(&'a Value);

impl<'a> Settings<'a> {
    /// Checks `intent`; manual AWG parameters through `awg_profile.py
    /// validate` of `tool`.
    pub(crate) fn check(intent: &'a Value) -> Result<Self, String> {
        let map = intent.as_object().ok_or("invalid node settings intent")?;
        let keys = FIELDS.len() + usize::from(map.contains_key("protocol_plan"));
        if map.len() != keys || FIELDS.iter().any(|key| !map.contains_key(*key)) {
            return Err("invalid node settings intent".into());
        }
        if map["kind"] != "node_settings" || !map["node_key"].as_str().is_some_and(node_key) {
            return Err("invalid node key".into());
        }
        if !map["command_id"].as_str().is_some_and(command_id) {
            return Err("invalid command identity".into());
        }
        if !map["revision"]
            .as_i64()
            .is_some_and(|revision| revision > 0)
        {
            return Err("invalid revision".into());
        }
        let protocols = protocol_list(&map["protocols"]).ok_or("invalid protocols")?;
        if let Some(plan) = map.get("protocol_plan") {
            let plan = plan
                .as_object()
                .filter(|plan| plan.len() == 1 && plan.contains_key("remove_protocols"))
                .ok_or("invalid protocol plan")?;
            let removed =
                protocol_list(&plan["remove_protocols"]).ok_or("invalid protocol removal")?;
            if removed.iter().any(|p| protocols.contains(p)) {
                return Err("invalid protocol removal".into());
            }
        }
        let settings = map["settings"]
            .as_object()
            .filter(|settings| settings.keys().all(|key| SETTINGS.contains(&key.as_str())))
            .ok_or("invalid settings")?;
        let mut required = Vec::new();
        if !protocols.is_empty() {
            required.push("public_host");
        }
        if protocols.contains(&"xray") {
            required.extend([
                "xray_sni",
                "xray_tcp_port",
                "xray_xhttp_port",
                "xray_xhttp_path",
            ]);
        }
        if protocols.contains(&"awg") {
            required.push("awg_port");
        }
        if required.iter().any(|key| !settings.contains_key(*key)) {
            return Err("node settings are incomplete".into());
        }
        for (field, value) in settings {
            check_setting(field, value)?;
        }
        if protocols.contains(&"xray")
            && (settings["xray_tcp_port"] == settings["xray_xhttp_port"]
                || !settings["xray_xhttp_path"]
                    .as_str()
                    .is_some_and(|path| path.starts_with('/')))
        {
            return Err("invalid Xray ports or path".into());
        }
        Ok(Self(intent))
    }

    /// A value derived from a checked intent, such as its effective
    /// settings once a port is chosen.
    pub(crate) fn derived(value: &'a Value) -> Self {
        Self(value)
    }

    pub(crate) fn value(&self) -> &'a Value {
        self.0
    }

    pub(crate) fn command_id(&self) -> &'a str {
        self.0["command_id"].as_str().unwrap_or_default()
    }

    pub(crate) fn node_key(&self) -> &'a str {
        self.0["node_key"].as_str().unwrap_or_default()
    }

    pub(crate) fn revision(&self) -> i64 {
        self.0["revision"].as_i64().unwrap_or_default()
    }

    pub(crate) fn protocols(&self) -> Vec<&'a str> {
        protocol_list(&self.0["protocols"]).unwrap_or_default()
    }

    pub(crate) fn enabled(&self, protocol: &str) -> bool {
        self.protocols().contains(&protocol)
    }

    pub(crate) fn removed(&self) -> Vec<&'a str> {
        protocol_list(&self.0["protocol_plan"]["remove_protocols"]).unwrap_or_default()
    }

    /// Neither settings nor a removal: no runtime is a target.
    pub(crate) fn empty(&self) -> bool {
        self.protocols().is_empty() && self.removed().is_empty()
    }

    pub(crate) fn settings(&self) -> &'a Map<String, Value> {
        self.0["settings"].as_object().expect("checked settings")
    }

    /// A text setting, or its default.
    pub(crate) fn text(&self, key: &str, default: &'a str) -> &'a str {
        self.settings()
            .get(key)
            .and_then(Value::as_str)
            .unwrap_or(default)
    }

    pub(crate) fn port(&self, key: &str, default: i64) -> i64 {
        self.settings()
            .get(key)
            .and_then(Value::as_i64)
            .unwrap_or(default)
    }
}

fn check_setting(field: &str, value: &Value) -> Result<(), String> {
    if field == "awg_parameters" {
        let parameters = value.as_object().ok_or("unknown AWG parameters")?;
        if parameters.is_empty() {
            return Ok(());
        }
        let known: Vec<&str> = all_fields().collect();
        if parameters.keys().any(|key| !known.contains(&key.as_str())) {
            return Err("unknown AWG parameters".into());
        }
        if parameters.values().any(|value| {
            !value.as_str().is_some_and(|text| {
                !text.is_empty()
                    && text.len() <= 4096
                    && text.bytes().all(|b| (32..=126).contains(&b))
            })
        }) {
            return Err("invalid AWG parameter value".into());
        }
        return validate_parameters(parameters);
    }
    if field.ends_with("_port") {
        if !value
            .as_i64()
            .is_some_and(|port| (1..=65535).contains(&port))
        {
            return Err("invalid port".into());
        }
        return Ok(());
    }
    let text = value
        .as_str()
        .filter(|text| {
            !text.is_empty() && text.len() <= 255 && text.bytes().all(|b| (33..=126).contains(&b))
        })
        .ok_or("invalid text setting")?;
    let valid = match field {
        "public_host" | "xray_host" | "xray_sni" | "awg_public_host" => host(text),
        "xray_xhttp_path" => {
            text.starts_with('/')
                && text[1..]
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"/_~.%+-".contains(&b))
        }
        "awg_interface" => {
            text.len() <= 15
                && text.as_bytes()[0].is_ascii_alphabetic()
                && text
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
        }
        "awg_i1_preset" => matches!(text, "quic" | "dns" | "chaos"),
        "ipv6_outbound" => matches!(text, "on" | "off"),
        "xray_bittorrent" => matches!(text, "allow" | "block"),
        "awg_port_mode" => matches!(text, "auto" | "manual"),
        "xray_fingerprint" => matches!(
            text,
            "chrome" | "firefox" | "safari" | "ios" | "android" | "edge" | "random" | "randomized"
        ),
        _ => true,
    };
    if !valid {
        return Err(format!("invalid {}", field.replace('_', " ")));
    }
    Ok(())
}

/// The profile check of an interface holding `parameters`.
fn validate_parameters(parameters: &Map<String, Value>) -> Result<(), String> {
    use crate::protocols::awg::profile;
    let mut text = String::from("[Interface]\n");
    for (key, value) in parameters {
        text.push_str(&format!("{key} = {}\n", value.as_str().unwrap_or_default()));
    }
    profile::validate(&profile::interface_values(&text), true)
        .map_err(|_| "invalid AWG parameters".to_string())
}

/// The digest a verified result reports: the protocols and settings.
pub(crate) fn digest(intent: &Value) -> String {
    let mut protocols: Vec<Value> = intent["protocols"].as_array().cloned().unwrap_or_default();
    protocols.sort_by(|a, b| a.as_str().cmp(&b.as_str()));
    let value = json!({"protocols": protocols, "settings": intent["settings"]});
    sha256_hex(canonical(&value).as_bytes())
}

fn schema(connection: &Connection) -> Result<(), String> {
    commands_schema(connection)?;
    fences_schema(connection)?;
    connection
        .execute_batch(
            "CREATE TABLE IF NOT EXISTS node_settings_fence (node_key TEXT PRIMARY KEY, revision INTEGER NOT NULL, command_id TEXT NOT NULL)",
        )
        .map_err(database)
}

/// Where a node command reports its stage, an optional observation that
/// never carries config data. Only UUID commands report one.
pub(crate) struct Progress(Option<(PathBuf, String)>);

impl Progress {
    pub(crate) fn new(journal: &Path, command_id: &str) -> Self {
        Self(crate::fencing::valid_force_identity(command_id).then(|| {
            (
                journal
                    .with_file_name("operation-progress")
                    .join(format!("{command_id}.json")),
                command_id.to_string(),
            )
        }))
    }

    #[cfg(test)]
    pub(crate) fn none() -> Self {
        Self(None)
    }

    pub(crate) fn step(&self, stage: &str) {
        let Some((target, identity)) = &self.0 else {
            return;
        };
        let Some(dir) = target.parent() else {
            return;
        };
        if fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(dir)
            .is_err()
        {
            return;
        }
        let Ok(suffix) = crate::protocols::deploy::random_hex(8) else {
            return;
        };
        let staged = dir.join(format!(".{identity}.{suffix}"));
        let record = json!({"operation_id": identity, "stage": stage}).to_string();
        let written = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&staged)
            .and_then(|mut file| file.write_all(record.as_bytes()));
        if written.is_err() || fs::rename(&staged, target).is_err() {
            let _ = fs::remove_file(&staged);
        }
    }
}

/// Runs a settings change while the journal lock is held.
pub(crate) type Runner<'a> =
    dyn FnMut(&Settings<'_>, RawFd, &Progress) -> Result<Response, String> + 'a;

/// Records and runs `intent`; a repeated command returns its first result.
/// Its result must report exactly the requested settings.
pub(crate) fn apply(
    intent: &Value,
    journal: &Path,
    instance: &str,
    runner: &mut Runner<'_>,
) -> Result<Response, String> {
    let settings = Settings::check(intent)?;
    if settings.empty() {
        // Refused before the journal: no command of unknown outcome remains.
        return Err("no VPN settings or authorized protocol removal requested".into());
    }
    let fingerprint = fingerprint(intent);
    let lock = Lock::exclusive(journal)?;
    if disabled(journal) {
        return Err("agent removal is in progress".into());
    }
    let connection = open(journal)?;
    schema(&connection)?;
    let id = settings.command_id();
    let prior: Option<(String, String, Option<String>)> = connection
        .query_row(
            "SELECT fingerprint, status, response FROM commands WHERE id = ?",
            [id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()
        .map_err(database)?;
    if let Some((prior, status, response)) = prior {
        if prior != fingerprint {
            return Err("command identity conflict".into());
        }
        if status == "succeeded" {
            return Response::parse(response.as_deref().unwrap_or_default());
        }
        return Err("previous command did not complete successfully".into());
    }
    let running = connection
        .prepare("SELECT 1 FROM commands WHERE status = 'running' LIMIT 1")
        .and_then(|mut statement| statement.exists([]))
        .map_err(database)?;
    if running {
        return Err("unfinished execution requires operator reconciliation".into());
    }
    let fence: Option<i64> = connection
        .query_row(
            "SELECT revision FROM node_settings_fence WHERE node_key = ?",
            [settings.node_key()],
            |row| row.get(0),
        )
        .optional()
        .map_err(database)?;
    if fence.is_some_and(|fence| settings.revision() <= fence) {
        return Err("stale or conflicting revision".into());
    }
    let transaction = connection.unchecked_transaction().map_err(database)?;
    transaction
        .execute(
            "INSERT INTO commands(id, fingerprint, status, instance_id) VALUES (?, ?, 'running', ?)",
            params![id, fingerprint, instance],
        )
        .map_err(database)?;
    transaction
        .execute(
            "INSERT INTO node_settings_fence(node_key, revision, command_id) VALUES (?, ?, ?) ON CONFLICT(node_key) DO UPDATE SET revision = excluded.revision, command_id = excluded.command_id",
            params![settings.node_key(), settings.revision(), id],
        )
        .map_err(database)?;
    transaction.commit().map_err(database)?;
    let response = runner(&settings, lock.fd(), &Progress::new(journal, id))?;
    verify_result(&settings, &response)?;
    connection
        .execute(
            "UPDATE commands SET status = 'succeeded', response = ? WHERE id = ?",
            params![response.to_json(), id],
        )
        .map_err(database)?;
    Ok(response)
}

/// The result must report the requested settings; an automatically chosen
/// AWG port must be a candidate of the preset.
fn verify_result(settings: &Settings<'_>, response: &Response) -> Result<(), String> {
    let payload: Value = serde_json::from_str(&response.payload_json)
        .map_err(|_| "invalid node settings result".to_string())?;
    let mut effective = settings.value().clone();
    let mut expected = json!({"node_key": settings.node_key(), "revision": settings.revision()});
    if let Some(port) = payload.get("awg_port") {
        let selected = port.as_i64().filter(|_| port.is_i64());
        let valid = settings.enabled("awg")
            && settings.text("awg_port_mode", "") == "auto"
            && selected.is_some_and(|selected| {
                ports::candidates(
                    settings.text("awg_i1_preset", "quic"),
                    settings.port("awg_port", 0),
                )
                .is_ok_and(|candidates| {
                    u16::try_from(selected).is_ok_and(|selected| candidates.contains(&selected))
                })
            });
        if !valid {
            return Err("invalid selected AWG port".into());
        }
        effective["settings"]["awg_port"] = port.clone();
        expected["awg_port"] = port.clone();
    }
    expected["settings_sha256"] = json!(digest(&effective));
    if payload != expected {
        return Err("node settings verification mismatch".into());
    }
    Ok(())
}

/// The stored result of a confirmed command, without running anything.
pub(crate) fn lookup(intent: &Value, journal: &Path) -> Result<Response, String> {
    let settings = Settings::check(intent)?;
    let unconfirmed = || "node settings success is not confirmed".to_string();
    let connection = open_readonly(journal).map_err(|_| unconfirmed())?;
    let row: Option<(String, String, Option<String>)> = connection
        .query_row(
            "SELECT fingerprint, status, response FROM commands WHERE id = ?",
            [settings.command_id()],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()
        .map_err(|_| unconfirmed())?;
    match row {
        Some((prior, status, Some(response)))
            if prior == fingerprint(intent) && status == "succeeded" =>
        {
            Response::parse(&response)
        }
        _ => Err(unconfirmed()),
    }
}

/// Fences the exact interrupted command after an agent restart; never runs
/// a replacement. An empty command has no runtime to inspect: its exact
/// tombstone is recorded even without a journal, so a delayed apply cannot
/// run after the controller clears it.
pub(crate) fn resolve(
    intent: &Value,
    journal: &Path,
    instance: &str,
    inspector: &mut dyn FnMut(&Settings<'_>) -> Result<Value, String>,
) -> Result<Value, String> {
    let settings = Settings::check(intent)?;
    let empty = settings.empty();
    if !empty && !journal.is_file() {
        return Err("command journal is missing; outcome is unconfirmed".into());
    }
    let _lock = Lock::exclusive(journal)?;
    if disabled(journal) {
        return Err("agent removal is in progress".into());
    }
    let connection = open(journal)?;
    schema(&connection)?;
    let fingerprint = fingerprint(intent);
    let row: Option<(String, String, Option<String>, String)> = connection
        .query_row(
            "SELECT fingerprint, status, response, instance_id FROM commands WHERE id = ?",
            [settings.command_id()],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()
        .map_err(database)?;
    match &row {
        Some((prior, ..)) if *prior != fingerprint => {
            return Err("command identity mismatch".into());
        }
        Some((_, status, response, _)) if status == "superseded" => {
            return serde_json::from_str(response.as_deref().unwrap_or_default())
                .map_err(|_| "invalid repair record".into());
        }
        Some((_, status, _, _)) if status != "running" => {
            return Err("command is not interrupted".into());
        }
        Some((.., owner)) if instance.is_empty() || owner == instance => {
            return Err("agent service restart is required before repair".into());
        }
        None if !empty => return Err("command identity mismatch".into()),
        _ => {}
    }
    let observation = if empty {
        json!({"config_matches": false, "containers_running": false})
    } else {
        inspector(&settings)?
    };
    let valid = observation.as_object().is_some_and(|map| {
        map.len() == REPAIR.len()
            && REPAIR
                .iter()
                .all(|key| map.get(*key).is_some_and(Value::is_boolean))
    });
    if !valid {
        return Err("live inspection unavailable".into());
    }
    let record = json!({"observation": observation, "resolved_at": timestamp(Utc::now())});
    let stored = sorted_dumps(&record);
    let transaction = connection.unchecked_transaction().map_err(database)?;
    if row.is_none() {
        transaction
            .execute(
                "INSERT INTO commands(id, fingerprint, status, response, instance_id) VALUES (?, ?, 'superseded', ?, ?)",
                params![settings.command_id(), fingerprint, stored, instance],
            )
            .map_err(database)?;
        transaction
            .execute(
                "INSERT INTO node_settings_fence(node_key, revision, command_id) VALUES (?, ?, ?) ON CONFLICT(node_key) DO UPDATE SET revision = excluded.revision, command_id = excluded.command_id WHERE excluded.revision > node_settings_fence.revision",
                params![settings.node_key(), settings.revision(), settings.command_id()],
            )
            .map_err(database)?;
    } else {
        let changed = transaction
            .execute(
                "UPDATE commands SET status = 'superseded', response = ? WHERE id = ? AND status = 'running'",
                params![stored, settings.command_id()],
            )
            .map_err(database)?;
        if changed != 1 {
            return Err("command changed during repair".into());
        }
    }
    transaction.commit().map_err(database)?;
    Ok(record)
}

#[cfg(test)]
pub(crate) mod tests {
    use std::cell::RefCell;

    use super::*;
    use crate::intents::profile::{self, Intent};
    use crate::intents::test_journal;

    pub(crate) fn intent(revision: i64, command: &str) -> Value {
        json!({"kind": "node_settings", "node_key": "n1", "command_id": command,
            "revision": revision, "protocols": ["awg", "xray"],
            "settings": {"public_host": "node.example", "xray_sni": "www.cloudflare.com",
                "xray_tcp_port": 443, "xray_xhttp_port": 8443,
                "xray_xhttp_path": "/assets", "awg_port": 51820}})
    }

    /// A runner that reports exactly the requested settings, and counts.
    fn runner(
        calls: &RefCell<Vec<Value>>,
    ) -> impl FnMut(&Settings<'_>, RawFd, &Progress) -> Result<Response, String> + '_ {
        move |settings, _, _| {
            calls.borrow_mut().push(settings.value().clone());
            let payload = json!({"node_key": settings.node_key(), "revision": settings.revision(),
                "settings_sha256": digest(settings.value())});
            Ok(Response {
                summary: "applied".into(),
                payload_json: payload.to_string(),
            })
        }
    }

    fn status(journal: &Path, sql: &str) -> Option<String> {
        Connection::open(journal)
            .unwrap()
            .query_row(sql, [], |row| row.get(0))
            .ok()
    }

    #[test]
    fn a_repeated_command_returns_its_result_and_revisions_advance() {
        let journal = test_journal("settings-idempotent");
        let calls = RefCell::new(Vec::new());
        let first = apply(&intent(1, "task-1"), &journal, "agent", &mut runner(&calls)).unwrap();
        assert_eq!(
            apply(&intent(1, "task-1"), &journal, "agent", &mut runner(&calls)).unwrap(),
            first
        );
        assert_eq!(lookup(&intent(1, "task-1"), &journal).unwrap(), first);
        assert_eq!(calls.borrow().len(), 1);
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(&journal).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert!(apply(&intent(1, "other"), &journal, "agent", &mut runner(&calls)).is_err());
        assert!(apply(&intent(2, "task-1"), &journal, "agent", &mut runner(&calls)).is_err());
        let second = apply(&intent(2, "task-2"), &journal, "agent", &mut runner(&calls)).unwrap();
        assert_eq!(
            (calls.borrow().len(), second.summary.as_str()),
            (2, "applied")
        );
    }

    #[test]
    fn an_unverified_result_blocks_later_commands_of_any_kind() {
        let journal = test_journal("settings-unverified");
        let mut claimed = |_: &Settings<'_>, _: RawFd, _: &Progress| {
            Ok(Response {
                summary: "claimed".into(),
                payload_json: "{}".into(),
            })
        };
        assert!(apply(&intent(1, "task-1"), &journal, "agent", &mut claimed).is_err());
        assert!(lookup(&intent(1, "task-1"), &journal).is_err());
        let calls = RefCell::new(Vec::new());
        assert!(apply(&intent(2, "new"), &journal, "agent", &mut runner(&calls)).is_err());
        let profile = Intent {
            command_id: "profile-task".into(),
            protocol: "awg".into(),
            runtime_name: "alice".into(),
            revision: 1,
            action: "ensure".into(),
            uuid: String::new(),
            short_id: String::new(),
            lease_seconds: None,
        };
        assert!(
            profile::apply(&profile, &journal, "agent", &mut |_, _| Ok(Response::ok())).is_err()
        );
        assert!(calls.borrow().is_empty());
    }

    #[test]
    fn a_selected_fallback_port_is_verified_and_recovered_without_replay() {
        let journal = test_journal("settings-port");
        let mut value = intent(1, "task-1");
        value["settings"]["awg_port"] = json!(443);
        value["settings"]["awg_i1_preset"] = json!("quic");
        value["settings"]["awg_port_mode"] = json!("auto");
        let calls = RefCell::new(0);
        let mut selected = |settings: &Settings<'_>, _: RawFd, _: &Progress| {
            *calls.borrow_mut() += 1;
            let mut effective = settings.value().clone();
            effective["settings"]["awg_port"] = json!(8443);
            let payload = json!({"node_key": "n1", "revision": 1, "awg_port": 8443,
                "settings_sha256": digest(&effective)});
            Ok(Response {
                summary: "verified".into(),
                payload_json: payload.to_string(),
            })
        };
        let first = apply(&value, &journal, "agent", &mut selected).unwrap();
        assert_eq!(lookup(&value, &journal).unwrap(), first);
        assert_eq!(
            apply(&value, &journal, "agent", &mut selected).unwrap(),
            first
        );
        assert_eq!(*calls.borrow(), 1);
        // A port outside the preset's candidates is refused.
        let journal = test_journal("settings-port-foreign");
        let mut foreign = |settings: &Settings<'_>, _: RawFd, _: &Progress| {
            let mut effective = settings.value().clone();
            effective["settings"]["awg_port"] = json!(9999);
            let payload = json!({"node_key": "n1", "revision": 1, "awg_port": 9999,
                "settings_sha256": digest(&effective)});
            Ok(Response {
                summary: "verified".into(),
                payload_json: payload.to_string(),
            })
        };
        assert_eq!(
            apply(&value, &journal, "agent", &mut foreign).unwrap_err(),
            "invalid selected AWG port"
        );
    }

    #[test]
    fn progress_is_published_for_uuid_commands_only() {
        let journal = test_journal("settings-progress");
        let identity = "0a1b2c3d-0000-4000-8000-00000000000a";
        let value = intent(1, identity);
        let calls = RefCell::new(Vec::new());
        let mut inner = runner(&calls);
        let mut observed = |settings: &Settings<'_>, fd: RawFd, progress: &Progress| {
            progress.step("protocol_deploy");
            let receipt = journal
                .with_file_name("operation-progress")
                .join(format!("{identity}.json"));
            let record: Value =
                serde_json::from_str(&fs::read_to_string(receipt).unwrap()).unwrap();
            assert_eq!(
                record,
                json!({"operation_id": identity, "stage": "protocol_deploy"})
            );
            inner(settings, fd, progress)
        };
        apply(&value, &journal, "agent", &mut observed).unwrap();
        Progress::new(&journal, "node-task-1").step("ignored");
        assert_eq!(
            fs::read_dir(journal.with_file_name("operation-progress"))
                .unwrap()
                .count(),
            1
        );
    }

    #[test]
    fn invalid_settings_are_refused_before_the_journal() {
        let journal = test_journal("settings-invalid");
        let calls = RefCell::new(Vec::new());
        let reject = |change: &dyn Fn(&mut Value)| {
            let mut bad = intent(1, "task-1");
            change(&mut bad);
            assert!(Settings::check(&bad).is_err(), "{bad}");
        };
        reject(&|v| {
            v["settings"].as_object_mut().unwrap().remove("xray_sni");
        });
        reject(&|v| v["settings"]["xray_sni"] = json!("example.com\";print(1)"));
        reject(&|v| v["settings"]["xray_sni"] = json!("a..b"));
        reject(&|v| v["settings"]["xray_xhttp_port"] = json!(443));
        reject(&|v| v["settings"]["awg_port"] = json!(true));
        reject(&|v| v["settings"]["awg_port"] = json!(65536));
        reject(&|v| v["settings"]["xray_xhttp_path"] = json!("assets"));
        reject(&|v| v["settings"]["awg_interface"] = json!("0wg"));
        reject(&|v| v["settings"]["awg_i1_preset"] = json!("other"));
        reject(&|v| v["settings"]["unknown"] = json!("x"));
        reject(&|v| v["revision"] = json!(0));
        reject(&|v| v["node_key"] = json!("-n1"));
        reject(&|v| v["protocols"] = json!(["awg", "awg"]));
        reject(&|v| v["protocol_plan"] = json!({"remove_protocols": ["awg"]}));
        reject(&|v| v["protocol_plan"] = json!({"remove_protocols": [], "other": 1}));
        reject(&|v| v["settings"]["awg_parameters"] = json!({"Unknown": "1"}));
        reject(&|v| v["settings"]["awg_parameters"] = json!({"Jc": "x\n"}));
        reject(&|v| v["settings"]["awg_parameters"] = json!({"Jc": "4"}));
        let mut bad = intent(1, "task-1");
        bad["settings"]["xray_sni"] = json!("bad host");
        assert!(apply(&bad, &journal, "agent", &mut runner(&calls)).is_err());
        let empty = json!({"kind": "node_settings", "node_key": "n1", "command_id": "task-1",
            "revision": 1, "protocols": [], "settings": {}});
        assert!(
            apply(&empty, &journal, "agent", &mut runner(&calls))
                .unwrap_err()
                .contains("no VPN settings")
        );
        assert!(!journal.exists());
        assert!(calls.borrow().is_empty());
    }

    #[test]
    fn an_empty_removal_is_durable() {
        let journal = test_journal("settings-removal");
        let mut value = intent(1, "task-1");
        value["protocols"] = json!([]);
        value["settings"] = json!({});
        value["protocol_plan"] = json!({"remove_protocols": ["awg", "xray"]});
        let calls = RefCell::new(Vec::new());
        let first = apply(&value, &journal, "agent", &mut runner(&calls)).unwrap();
        assert_eq!(lookup(&value, &journal).unwrap(), first);
        assert_eq!(
            apply(&value, &journal, "agent", &mut runner(&calls)).unwrap(),
            first
        );
        assert_eq!(calls.borrow().len(), 1);
    }

    fn empty_intent() -> Value {
        json!({"kind": "node_settings", "node_key": "n1", "command_id": "node-task-1",
            "revision": 1, "protocols": [], "settings": {"ipv6_outbound": "on", "awg_parameters": {}}})
    }

    #[test]
    fn an_absent_empty_command_is_fenced_without_inspection() {
        let journal = test_journal("settings-empty-tombstone");
        let mut inspector = |_: &Settings<'_>| -> Result<Value, String> { panic!("no inspection") };
        let result = resolve(&empty_intent(), &journal, "agent", &mut inspector).unwrap();
        assert_eq!(
            result["observation"],
            json!({"config_matches": false, "containers_running": false})
        );
        assert_eq!(
            resolve(&empty_intent(), &journal, "agent", &mut inspector).unwrap(),
            result
        );
        let mut changed = empty_intent();
        changed["settings"] = json!({"ipv6_outbound": "off"});
        assert_eq!(
            resolve(&changed, &journal, "agent", &mut inspector).unwrap_err(),
            "command identity mismatch"
        );
        assert_eq!(
            status(&journal, "SELECT status FROM commands").as_deref(),
            Some("superseded")
        );
        let revision: i64 = Connection::open(&journal)
            .unwrap()
            .query_row("SELECT revision FROM node_settings_fence", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(revision, 1);
        let calls = RefCell::new(Vec::new());
        apply(&intent(2, "fresh"), &journal, "agent", &mut runner(&calls)).unwrap();
        assert_eq!(calls.borrow().len(), 1);
    }

    #[test]
    fn an_absent_nonempty_command_cannot_be_retired() {
        let journal = test_journal("settings-absent");
        let mut removal = intent(1, "task-1");
        removal["protocols"] = json!([]);
        removal["settings"] = json!({});
        removal["protocol_plan"] = json!({"remove_protocols": ["awg"]});
        for value in [intent(1, "task-1"), removal] {
            let err =
                resolve(&value, &journal, "agent", &mut |_| panic!("no inspection")).unwrap_err();
            assert!(err.contains("journal is missing"));
            assert!(!journal.exists());
        }
    }

    #[test]
    fn an_empty_tombstone_keeps_a_newer_fence() {
        let journal = test_journal("settings-newer");
        let calls = RefCell::new(Vec::new());
        apply(&intent(5, "newer"), &journal, "agent", &mut runner(&calls)).unwrap();
        resolve(&empty_intent(), &journal, "agent", &mut |_| {
            panic!("no inspection")
        })
        .unwrap();
        let fence: (i64, String) = Connection::open(&journal)
            .unwrap()
            .query_row(
                "SELECT revision, command_id FROM node_settings_fence",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(fence, (5, "newer".into()));
        assert_eq!(
            status(&journal, "SELECT status FROM commands WHERE id='newer'").as_deref(),
            Some("succeeded")
        );
    }

    #[test]
    fn an_interrupted_change_needs_a_restart_then_a_fresh_revision() {
        let journal = test_journal("settings-interrupted");
        let value = intent(1, "task-1");
        let mut interrupted =
            |_: &Settings<'_>, _: RawFd, _: &Progress| Err("interrupted".to_string());
        assert!(apply(&value, &journal, "old-agent", &mut interrupted).is_err());
        let mut observe =
            |_: &Settings<'_>| Ok(json!({"config_matches": false, "containers_running": true}));
        assert!(
            resolve(&value, &journal, "old-agent", &mut observe)
                .unwrap_err()
                .contains("restart")
        );
        let observations = RefCell::new(0);
        let mut counted = |_: &Settings<'_>| {
            *observations.borrow_mut() += 1;
            Ok(json!({"config_matches": false, "containers_running": true}))
        };
        let result = resolve(&value, &journal, "new-agent", &mut counted).unwrap();
        assert_eq!(
            result["observation"],
            json!({"config_matches": false, "containers_running": true})
        );
        assert_eq!(
            resolve(&value, &journal, "new-agent", &mut counted).unwrap(),
            result
        );
        assert_eq!(*observations.borrow(), 1);
        assert!(lookup(&value, &journal).is_err());
        let calls = RefCell::new(Vec::new());
        apply(
            &intent(2, "fresh"),
            &journal,
            "new-agent",
            &mut runner(&calls),
        )
        .unwrap();
        // An observation of another shape is refused.
        let journal = test_journal("settings-interrupted-shape");
        assert!(apply(&value, &journal, "old", &mut interrupted).is_err());
        let mut partial = |_: &Settings<'_>| Ok(json!({"config_matches": false}));
        assert_eq!(
            resolve(&value, &journal, "new", &mut partial).unwrap_err(),
            "live inspection unavailable"
        );
    }

    #[test]
    fn manual_awg_parameters_are_validated_by_the_profile() {
        let mut parameters = Map::new();
        for (key, value) in crate::protocols::awg::profile::new_profile("quic")
            .unwrap()
            .iter()
        {
            parameters.insert(key.into(), json!(value));
        }
        let mut value = intent(1, "task-1");
        value["settings"]["awg_parameters"] = Value::Object(parameters.clone());
        Settings::check(&value).unwrap();
        parameters.insert("Jmin".into(), json!("1000"));
        parameters.insert("Jmax".into(), json!("10"));
        value["settings"]["awg_parameters"] = Value::Object(parameters);
        assert!(Settings::check(&value).is_err());
    }
}
