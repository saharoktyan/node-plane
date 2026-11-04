//! The profile intent journal, `profile-intents.sqlite3` next to node.env.
//!
//! Every backend command that changes the node is recorded before it runs
//! and closed when its outcome is confirmed. An unfinished or failed command
//! fails closed: its external outcome is unknown, so nothing newer runs until
//! an operator resolves it. Revision fences stop delayed and duplicate
//! commands. One flock on `<journal>.lock` serializes every mutation of the
//! node, and the mutation subprocesses inherit it.
//!
//! `apply-profile-intent.py` shares the journal for the commands not moved
//! here yet; both keep its schema.
pub(crate) mod expiry;
pub(crate) mod leases;
pub(crate) mod legacy;
pub(crate) mod profile;
pub(crate) mod scheduler;
pub(crate) mod supervisor;

use std::fs;
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};
use serde_json::Value;

use crate::protocols::sha256_hex;

const JOURNAL: &str = "/etc/node-plane/profile-intents.sqlite3";

/// The journal of a subcommand; tests point it elsewhere.
pub(crate) fn journal_path() -> PathBuf {
    std::env::var_os("NODE_PLANE_PROFILE_JOURNAL")
        .map(PathBuf::from)
        .unwrap_or_else(|| JOURNAL.into())
}

/// The agent instance a subcommand records; the expiry service has none.
fn instance() -> String {
    std::env::var("NODE_PLANE_AGENT_INSTANCE_ID").unwrap_or_else(|_| "standalone".into())
}

fn leased_protocol(value: &str) -> Result<&str, String> {
    matches!(value, "awg" | "xray")
        .then_some(value)
        .ok_or_else(|| "invalid leased protocol".into())
}

/// `node-plane-agent lease monitor|expire|supervise P|prepare P|deploy P`:
/// the expiry service, one scan, the supervisor of a leased container, and
/// the steps of a deployment of one. Replaces those commands of
/// `apply-profile-intent.py`.
pub(crate) fn run(args: &[String]) -> Option<i32> {
    let (first, rest) = args.split_first()?;
    if first != "lease" {
        return None;
    }
    let journal = journal_path();
    let instance = instance();
    let runtime = crate::protocols::settings::runtime();
    let mut runner =
        |intent: &profile::Intent, lock: RawFd| profile::run(intent, lock, &runtime, &instance);
    let host = leases::System;
    let units = Path::new(scheduler::UNITS);
    let rest: Vec<&str> = rest.iter().map(String::as_str).collect();
    let result: Result<Option<String>, String> = match rest.as_slice() {
        ["monitor"] => expiry::monitor(&journal, &instance, &mut runner).map(|()| None),
        ["expire"] => {
            expiry::expire(&journal, &instance, chrono::Utc::now(), &mut runner).map(|scan| {
                let report = serde_json::json!({"expired": scan.expired, "pending": scan.pending});
                println!("{report}");
                if scan.pending > 0 {
                    std::process::exit(1);
                }
                None
            })
        }
        ["supervise", protocol] => leased_protocol(protocol).and_then(|protocol| {
            let settings = leases::RuntimeSettings::read(protocol)?;
            supervisor::supervise(&host, protocol, &journal, &settings).map(|()| None)
        }),
        ["prepare", protocol] => leased_protocol(protocol)
            .and_then(|protocol| supervisor::prepare(protocol, &journal))
            .map(|()| Some(r#"{"prepared": true}"#.into())),
        ["deploy", protocol] => leased_protocol(protocol)
            .and_then(|protocol| scheduler::refresh(&host, units, protocol))
            .map(|protected| Some(serde_json::json!({"protected": protected}).to_string())),
        _ => Err(
            "usage: lease monitor|expire|supervise PROTOCOL|prepare PROTOCOL|deploy PROTOCOL"
                .into(),
        ),
    };
    Some(match result {
        Ok(output) => {
            if let Some(output) = output {
                println!("{output}");
            }
            0
        }
        Err(err) => {
            eprintln!("{err}");
            1
        }
    })
}

impl crate::state::AgentState {
    /// The journal beside node.env.
    pub(crate) fn journal(&self) -> PathBuf {
        Path::new(&self.config.node_env_path).with_file_name("profile-intents.sqlite3")
    }
}

/// `<journal><suffix>`: its lock, removal marker and SQLite side files.
pub(crate) fn sidecar(journal: &Path, suffix: &str) -> PathBuf {
    let mut name = journal.as_os_str().to_owned();
    name.push(suffix);
    name.into()
}

/// Agent removal fenced the journal: no command runs any more.
pub(crate) fn disabled(journal: &Path) -> bool {
    sidecar(journal, ".disabled").exists()
}

/// The journal lock, held until dropped.
pub(crate) struct Lock(fs::File);

impl Lock {
    fn open(journal: &Path) -> Result<fs::File, String> {
        let path = sidecar(journal, ".lock");
        if let Some(dir) = journal.parent() {
            fs::create_dir_all(dir).map_err(|err| format!("{}: {err}", dir.display()))?;
        }
        let file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .map_err(|err| format!("journal lock unavailable: {err}"))?;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
            .map_err(|err| format!("journal lock unavailable: {err}"))?;
        Ok(file)
    }

    /// Waits for every other mutation of the node.
    pub(crate) fn exclusive(journal: &Path) -> Result<Self, String> {
        let file = Self::open(journal)?;
        file.lock()
            .map_err(|err| format!("journal lock unavailable: {err}"))?;
        Ok(Self(file))
    }

    /// For subprocesses that inherit it as `NODE_PLANE_PROFILE_LOCK_FD`.
    pub(crate) fn fd(&self) -> RawFd {
        self.0.as_raw_fd()
    }
}

fn database(err: rusqlite::Error) -> String {
    format!("command journal: {err}")
}

/// The journal for writing, private and durable on every commit.
pub(crate) fn open(journal: &Path) -> Result<Connection, String> {
    let connection = Connection::open(journal).map_err(database)?;
    fs::set_permissions(journal, fs::Permissions::from_mode(0o600))
        .map_err(|err| format!("{}: {err}", journal.display()))?;
    connection
        .pragma_update(None, "synchronous", "FULL")
        .map_err(database)?;
    Ok(connection)
}

/// The journal for reading; a busy journal fails after 2 s.
pub(crate) fn open_readonly(journal: &Path) -> Result<Connection, String> {
    let connection = Connection::open_with_flags(
        journal,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(database)?;
    connection
        .busy_timeout(Duration::from_secs(2))
        .map_err(database)?;
    Ok(connection)
}

pub(crate) fn has_table(connection: &Connection, name: &str) -> Result<bool, String> {
    connection
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            [name],
            |_| Ok(()),
        )
        .map(|_| true)
        .or_else(|err| match err {
            rusqlite::Error::QueryReturnedNoRows => Ok(false),
            err => Err(database(err)),
        })
}

pub(crate) fn commands_schema(connection: &Connection) -> Result<(), String> {
    connection
        .execute_batch(
            "CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, response TEXT, instance_id TEXT NOT NULL)",
        )
        .map_err(database)
}

/// Profile fences; journals from before deletion tracking gain `action`.
pub(crate) fn fences_schema(connection: &Connection) -> Result<(), String> {
    connection
        .execute_batch(
            "CREATE TABLE IF NOT EXISTS fences (protocol TEXT, profile TEXT, revision INTEGER NOT NULL, command_id TEXT NOT NULL, action TEXT, PRIMARY KEY(protocol, profile))",
        )
        .map_err(database)?;
    let mut columns = connection
        .prepare("PRAGMA table_info(fences)")
        .map_err(database)?;
    let names = columns
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(database)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(database)?;
    if !names.iter().any(|name| name == "action") {
        connection
            .execute_batch("ALTER TABLE fences ADD COLUMN action TEXT")
            .map_err(database)?;
    }
    Ok(())
}

pub(crate) fn leases_schema(connection: &Connection) -> Result<(), String> {
    connection
        .execute_batch(
            "CREATE TABLE IF NOT EXISTS temporary_leases (
        protocol TEXT NOT NULL, profile TEXT NOT NULL, command_id TEXT NOT NULL,
        expires_at TEXT NOT NULL, status TEXT NOT NULL
            CHECK(status IN ('provisioning','active','revoking','revoked','expired')),
        PRIMARY KEY(protocol, profile))",
        )
        .map_err(database)
}

/// Python's `json.dumps(value, sort_keys=True, separators=(',', ':'))`, the
/// form command fingerprints hash.
pub(crate) fn canonical(value: &Value) -> String {
    fn string(out: &mut String, text: &str) {
        out.push('"');
        for unit in text.encode_utf16() {
            match unit {
                0x22 => out.push_str("\\\""),
                0x5c => out.push_str("\\\\"),
                0x0a => out.push_str("\\n"),
                0x0d => out.push_str("\\r"),
                0x09 => out.push_str("\\t"),
                0x08 => out.push_str("\\b"),
                0x0c => out.push_str("\\f"),
                0x20..=0x7e => out.push(char::from(unit as u8)),
                _ => out.push_str(&format!("\\u{unit:04x}")),
            }
        }
        out.push('"');
    }
    fn write(out: &mut String, value: &Value) {
        match value {
            Value::Object(map) => {
                out.push('{');
                let mut keys: Vec<&String> = map.keys().collect();
                keys.sort();
                for (index, key) in keys.into_iter().enumerate() {
                    if index > 0 {
                        out.push(',');
                    }
                    string(out, key);
                    out.push(':');
                    write(out, &map[key]);
                }
                out.push('}');
            }
            Value::Array(items) => {
                out.push('[');
                for (index, item) in items.iter().enumerate() {
                    if index > 0 {
                        out.push(',');
                    }
                    write(out, item);
                }
                out.push(']');
            }
            Value::String(text) => string(out, text),
            other => out.push_str(&other.to_string()),
        }
    }
    let mut out = String::new();
    write(&mut out, value);
    out
}

pub(crate) fn fingerprint(value: &Value) -> String {
    sha256_hex(canonical(value).as_bytes())
}

/// What a mutation reports: `{summary, payload_json}`, both strings.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Response {
    pub(crate) summary: String,
    pub(crate) payload_json: String,
}

impl Response {
    pub(crate) fn ok() -> Self {
        Self {
            summary: "OK".into(),
            payload_json: String::new(),
        }
    }

    pub(crate) fn to_json(&self) -> String {
        serde_json::json!({"summary": self.summary, "payload_json": self.payload_json}).to_string()
    }

    /// A stored response, which must have exactly both string fields.
    pub(crate) fn parse(text: &str) -> Result<Self, String> {
        let value: Value =
            serde_json::from_str(text).map_err(|_| "invalid runtime result".to_string())?;
        let map = value.as_object().ok_or("invalid runtime result")?;
        match (map.len(), &map.get("summary"), &map.get("payload_json")) {
            (2, Some(Value::String(summary)), Some(Value::String(payload_json))) => Ok(Self {
                summary: summary.clone(),
                payload_json: payload_json.clone(),
            }),
            _ => Err("invalid runtime result".into()),
        }
    }
}

/// A test journal in a fresh directory.
#[cfg(test)]
pub(crate) fn test_journal(name: &str) -> PathBuf {
    crate::protocols::test_dir(&format!("journal-{name}")).join("journal.sqlite3")
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn fingerprints_match_the_python_journal() {
        let intent = json!({"command_id": "task-1", "protocol": "xray", "runtime_name": "p_test",
            "revision": 3, "action": "ensure", "uuid": "11111111-1111-4111-8111-111111111111",
            "short_id": "0123456789abcdef", "lease_seconds": 43200});
        assert_eq!(
            fingerprint(&intent),
            "843e119b24d4c3102521c0b9dca265f89d6dbe0734f5009dfa043a622bfacccf"
        );
        assert_eq!(
            canonical(&json!({"b": [1, true, null], "a": "é\n\u{1}\"\\/"})),
            r#"{"a":"\u00e9\n\u0001\"\\/","b":[1,true,null]}"#
        );
    }

    #[test]
    fn responses_have_exactly_two_strings() {
        let response = Response::parse(&Response::ok().to_json()).unwrap();
        assert_eq!(response, Response::ok());
        assert!(Response::parse(r#"{"summary": "OK"}"#).is_err());
        assert!(Response::parse(r#"{"summary": "OK", "payload_json": 1}"#).is_err());
    }
}
