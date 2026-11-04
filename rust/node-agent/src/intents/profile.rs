//! Profile intents: one VPN identity of one protocol, ensured or deleted at
//! a revision. Applying records the command and advances the profile's fence
//! before the mutation runs; temporary identities also record their lease.
use std::os::fd::RawFd;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::Command;

use chrono::{DateTime, Duration, SecondsFormat, Utc};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};

use super::leases::System;
use super::{
    Lock, Response, commands_schema, database, disabled, fences_schema, fingerprint, has_table,
    leases, leases_schema, open, open_readonly,
};

/// Temporary access lasts 12 hours, a day or three days.
pub(crate) const LEASE_DURATIONS: [i64; 3] = [43200, 86400, 259200];

fn matches(value: &str, max: usize, allowed: impl Fn(u8) -> bool) -> bool {
    (1..=max).contains(&value.len()) && value.bytes().all(allowed)
}

pub(crate) fn command_id(value: &str) -> bool {
    matches(value, 128, |b| {
        b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b':' | b'-')
    })
}

/// `tmp_` and 32 lowercase hex digits: a temporary identity.
pub(crate) fn temporary_name(value: &str) -> bool {
    value.strip_prefix("tmp_").is_some_and(|hex| {
        hex.len() == 32 && hex.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
    })
}

/// What Python's `uuid.UUID` accepts from the controller: 32 hex digits,
/// optionally hyphenated, braced or `urn:uuid:` prefixed.
fn uuid(value: &str) -> bool {
    let hex = value.replace("urn:", "").replace("uuid:", "");
    let hex = hex.trim_matches(|c| c == '{' || c == '}').replace('-', "");
    hex.len() == 32 && hex.bytes().all(|b| b.is_ascii_hexdigit())
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Intent {
    pub(crate) command_id: String,
    pub(crate) protocol: String,
    pub(crate) runtime_name: String,
    pub(crate) revision: i64,
    pub(crate) action: String,
    pub(crate) uuid: String,
    pub(crate) short_id: String,
    pub(crate) lease_seconds: Option<i64>,
}

impl Intent {
    /// A checked intent from its JSON form.
    pub(crate) fn parse(value: &Value) -> Result<Self, String> {
        let invalid = || "invalid command identity".to_string();
        let map = value.as_object().ok_or_else(invalid)?;
        let keys = [
            "command_id",
            "protocol",
            "runtime_name",
            "revision",
            "action",
            "uuid",
            "short_id",
        ];
        let lease = map.contains_key("lease_seconds");
        if map.len() != keys.len() + usize::from(lease)
            || keys.iter().any(|key| !map.contains_key(*key))
        {
            return Err(invalid());
        }
        let text = |key: &str| map[key].as_str().map(str::to_string).ok_or_else(invalid);
        let integer = |key: &str, error: &str| {
            map[key]
                .as_i64()
                .filter(|_| map[key].is_i64())
                .ok_or_else(|| error.to_string())
        };
        let intent = Self {
            command_id: text("command_id")?,
            protocol: text("protocol")?,
            runtime_name: text("runtime_name")?,
            revision: integer("revision", "invalid revision")?,
            action: text("action")?,
            uuid: text("uuid")?,
            short_id: text("short_id")?,
            lease_seconds: if lease {
                Some(integer("lease_seconds", "invalid temporary lease")?)
            } else {
                None
            },
        };
        intent.validate()?;
        Ok(intent)
    }

    pub(crate) fn validate(&self) -> Result<(), String> {
        if !command_id(&self.command_id) {
            return Err("invalid command identity".into());
        }
        if !matches(&self.runtime_name, 64, |b| {
            b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-')
        }) {
            return Err("invalid runtime name".into());
        }
        if self.revision <= 0 {
            return Err("invalid revision".into());
        }
        if !matches!(self.protocol.as_str(), "awg" | "xray")
            || !matches!(self.action.as_str(), "ensure" | "delete")
        {
            return Err("invalid intent".into());
        }
        if self.protocol == "xray" && self.action == "ensure" {
            if !uuid(&self.uuid)
                || !(self.short_id.len() == 16
                    && self.short_id.bytes().all(|b| b.is_ascii_hexdigit()))
            {
                return Err("invalid xray identity".into());
            }
        } else if !self.uuid.is_empty() || !self.short_id.is_empty() {
            return Err("unexpected identity".into());
        }
        if let Some(seconds) = self.lease_seconds
            && (!LEASE_DURATIONS.contains(&seconds)
                || self.action != "ensure"
                || !temporary_name(&self.runtime_name))
        {
            return Err("invalid temporary lease".into());
        }
        Ok(())
    }

    pub(crate) fn to_value(&self) -> Value {
        let mut value = json!({
            "command_id": self.command_id, "protocol": self.protocol,
            "runtime_name": self.runtime_name, "revision": self.revision,
            "action": self.action, "uuid": self.uuid, "short_id": self.short_id,
        });
        if let Some(seconds) = self.lease_seconds {
            value["lease_seconds"] = json!(seconds);
        }
        value
    }

    pub(crate) fn fingerprint(&self) -> String {
        fingerprint(&self.to_value())
    }

    fn ensure(&self) -> bool {
        self.action == "ensure"
    }
}

/// `datetime.isoformat()` of an aware UTC time, as the journal stores it.
pub(crate) fn timestamp(time: DateTime<Utc>) -> String {
    time.to_rfc3339_opts(SecondsFormat::Micros, false)
}

pub(crate) fn parse_timestamp(text: &str) -> Result<DateTime<Utc>, String> {
    DateTime::parse_from_rfc3339(text)
        .map(|time| time.with_timezone(&Utc))
        .map_err(|_| "invalid lease deadline".to_string())
}

/// The profile's lease, when it has one; an ensure may only meet a live
/// lease of a temporary intent.
fn lease_current(
    connection: &Connection,
    intent: &Intent,
    now: DateTime<Utc>,
) -> Result<Option<(String, String)>, String> {
    let lease = connection
        .query_row(
            "SELECT expires_at,status FROM temporary_leases WHERE protocol=? AND profile=?",
            params![intent.protocol, intent.runtime_name],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()
        .map_err(database)?;
    if intent.ensure()
        && let Some((deadline, status)) = &lease
    {
        if status != "active" || parse_timestamp(deadline)? <= now {
            return Err("temporary lease is unavailable".into());
        }
        if intent.lease_seconds.is_none() {
            return Err("temporary identity cannot become permanent".into());
        }
    }
    Ok(lease)
}

/// Runs the mutation of an intent while the journal lock is held.
pub(crate) type Runner<'a> = dyn FnMut(&Intent, RawFd) -> Result<Response, String> + 'a;

fn status_of(
    connection: &Connection,
    id: &str,
) -> Result<Option<(String, String, Option<String>)>, String> {
    connection
        .query_row(
            "SELECT fingerprint, status, response FROM commands WHERE id = ?",
            [id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()
        .map_err(database)
}

/// Records and runs `intent`; a repeated command returns its first result.
pub(crate) fn apply(
    intent: &Intent,
    journal: &Path,
    instance: &str,
    runner: &mut Runner<'_>,
) -> Result<Response, String> {
    intent.validate()?;
    let fingerprint = intent.fingerprint();
    let lock = Lock::exclusive(journal)?;
    if disabled(journal) {
        return Err("agent removal is in progress".into());
    }
    let connection = open(journal)?;
    commands_schema(&connection)?;
    fences_schema(&connection)?;
    leases_schema(&connection)?;
    let target = params![intent.protocol, intent.runtime_name];
    let now = Utc::now();
    let mut lease = if intent.ensure() {
        None
    } else {
        lease_current(&connection, intent, now)?
    };
    let mut retry_delete = false;
    if let Some((prior, status, response)) = status_of(&connection, &intent.command_id)? {
        if prior != fingerprint {
            return Err("command identity conflict".into());
        }
        if status == "succeeded" {
            lease_current(&connection, intent, now)?;
            return Response::parse(response.as_deref().unwrap_or_default());
        }
        retry_delete = status == "running" && !intent.ensure() && lease.is_some();
        if !retry_delete {
            return Err("previous command did not complete successfully".into());
        }
    }
    // The node-wide lock serializes mutations. A crashed predecessor may have
    // orphaned an external operation: do not advance any fence.
    let mut allowed: Vec<String> = Vec::new();
    if retry_delete {
        allowed.push(intent.command_id.clone());
    }
    if lease.is_some() && !intent.ensure() {
        let mut statement = connection
            .prepare("SELECT command_id FROM temporary_leases WHERE protocol=? AND profile=?")
            .map_err(database)?;
        for id in statement
            .query_map(target, |row| row.get::<_, String>(0))
            .map_err(database)?
        {
            allowed.push(id.map_err(database)?);
        }
    }
    let mut running = connection
        .prepare("SELECT id FROM commands WHERE status = 'running'")
        .map_err(database)?;
    for id in running
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(database)?
    {
        if !allowed.contains(&id.map_err(database)?) {
            return Err("unfinished execution requires operator reconciliation".into());
        }
    }
    drop(running);
    lease = lease_current(&connection, intent, now)?;
    if lease.is_some() && intent.ensure() {
        return Err("temporary lease cannot be renewed".into());
    }
    let fence: Option<(i64, String)> = connection
        .query_row(
            "SELECT revision,command_id FROM fences WHERE protocol = ? AND profile = ?",
            target,
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(database)?;
    if intent.lease_seconds.is_some() && fence.is_some() {
        return Err("temporary lease requires an unused identity".into());
    }
    if let Some((revision, command)) = &fence
        && intent.revision <= *revision
        && !(retry_delete && *revision == intent.revision && *command == intent.command_id)
    {
        return Err("stale or conflicting revision".into());
    }
    let transaction = connection.unchecked_transaction().map_err(database)?;
    if !retry_delete {
        transaction
            .execute(
                "INSERT INTO commands(id, fingerprint, status, instance_id) VALUES (?, ?, 'running', ?)",
                params![intent.command_id, fingerprint, instance],
            )
            .map_err(database)?;
    }
    transaction
        .execute(
            "INSERT INTO fences(protocol, profile, revision, command_id, action) VALUES (?, ?, ?, ?, ?) ON CONFLICT(protocol, profile) DO UPDATE SET revision = excluded.revision, command_id = excluded.command_id, action = excluded.action",
            params![intent.protocol, intent.runtime_name, intent.revision, intent.command_id, intent.action],
        )
        .map_err(database)?;
    if let Some(seconds) = intent.lease_seconds {
        // A conservative deadline before any external change: a crash after
        // creating the peer still leaves revocation work.
        transaction
            .execute(
                "INSERT INTO temporary_leases VALUES (?,?,?,?,'provisioning')",
                params![
                    intent.protocol,
                    intent.runtime_name,
                    intent.command_id,
                    timestamp(Utc::now() + Duration::seconds(seconds))
                ],
            )
            .map_err(database)?;
    } else if !intent.ensure() && lease.is_some() {
        transaction
            .execute(
                "UPDATE temporary_leases SET status='revoking' WHERE protocol=? AND profile=?",
                target,
            )
            .map_err(database)?;
    }
    transaction.commit().map_err(database)?;

    // A failure keeps the command running: it cannot prove the external
    // operation stopped. The fence stays closed.
    let mut response = runner(intent, lock.fd())?;

    let transaction = connection.unchecked_transaction().map_err(database)?;
    if let Some(seconds) = intent.lease_seconds {
        let expires = timestamp(Utc::now() + Duration::seconds(seconds));
        let mut payload: Value = if response.payload_json.is_empty() {
            json!({})
        } else {
            serde_json::from_str(&response.payload_json)
                .map_err(|_| "invalid lease result".to_string())?
        };
        let map = payload.as_object_mut().ok_or("invalid lease result")?;
        map.insert("expires_at".into(), json!(expires));
        map.insert(
            "revocation_mode".into(),
            json!(if intent.protocol == "xray" {
                "new_connections_only"
            } else {
                "peer_removed"
            }),
        );
        response.payload_json = payload.to_string();
        transaction
            .execute(
                "UPDATE temporary_leases SET status='active',expires_at=? WHERE protocol=? AND profile=?",
                params![expires, intent.protocol, intent.runtime_name],
            )
            .map_err(database)?;
    } else if !intent.ensure() && lease.is_some() {
        transaction
            .execute(
                "UPDATE temporary_leases SET status='revoked' WHERE protocol=? AND profile=?",
                target,
            )
            .map_err(database)?;
        transaction
            .execute(
                "UPDATE commands SET status='superseded' WHERE status='running' AND id=(SELECT command_id FROM temporary_leases WHERE protocol=? AND profile=?)",
                target,
            )
            .map_err(database)?;
    }
    transaction
        .execute(
            "UPDATE commands SET status = 'succeeded', response = ? WHERE id = ?",
            params![response.to_json(), intent.command_id],
        )
        .map_err(database)?;
    transaction.commit().map_err(database)?;
    Ok(response)
}

/// The confirmed result of `intent`, read only.
pub(crate) fn lookup(intent: &Intent, journal: &Path) -> Result<Response, String> {
    intent.validate()?;
    let connection = open_readonly(journal)?;
    let unconfirmed = || "command success is not confirmed".to_string();
    let (prior, status, response) =
        status_of(&connection, &intent.command_id)?.ok_or_else(unconfirmed)?;
    if prior != intent.fingerprint() || status != "succeeded" {
        return Err(unconfirmed());
    }
    if has_table(&connection, "temporary_leases")? {
        lease_current(&connection, intent, Utc::now())?;
    }
    Response::parse(response.as_deref().unwrap_or_default())
}

/// What a repair saw of the profile: four booleans.
pub(crate) const OBSERVATION: [&str; 4] = [
    "disk_present",
    "live_present",
    "identity_matches",
    "config_available",
];

/// Retires an interrupted command after an agent restart, recording what
/// `inspector` sees live. The old mutation never runs again; the next
/// revision repairs the profile.
pub(crate) fn resolve_interrupted(
    intent: &Intent,
    journal: &Path,
    instance: &str,
    inspector: &mut dyn FnMut(&Intent) -> Result<Value, String>,
) -> Result<Value, String> {
    intent.validate()?;
    let _lock = Lock::exclusive(journal)?;
    if disabled(journal) {
        return Err("agent removal is in progress".into());
    }
    let connection = open(journal)?;
    let row: Option<(String, String, Option<String>, String)> = connection
        .query_row(
            "SELECT fingerprint, status, response, instance_id FROM commands WHERE id = ?",
            [&intent.command_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()
        .map_err(database)?;
    let Some((_, status, response, owner)) = row.filter(|row| row.0 == intent.fingerprint()) else {
        return Err("command identity mismatch".into());
    };
    if status == "superseded" {
        return serde_json::from_str(response.as_deref().unwrap_or_default())
            .map_err(|_| "invalid repair record".to_string());
    }
    if status != "running" {
        return Err("command is not interrupted".into());
    }
    if instance.is_empty() || owner == instance {
        return Err("agent service restart is required before repair".into());
    }
    let observation = inspector(intent)?;
    let valid = observation.as_object().is_some_and(|map| {
        map.len() == OBSERVATION.len()
            && OBSERVATION
                .iter()
                .all(|key| map.get(*key).is_some_and(Value::is_boolean))
    });
    if !valid {
        return Err("live inspection unavailable".into());
    }
    let record = json!({"observation": observation, "resolved_at": timestamp(Utc::now())});
    let changed = connection
        .execute(
            "UPDATE commands SET status = 'superseded', response = ? WHERE id = ? AND status = 'running'",
            params![record.to_string(), intent.command_id],
        )
        .map_err(database)?;
    if changed != 1 {
        return Err("command changed during repair".into());
    }
    Ok(record)
}

/// Starts a runtime entry point that inherits the journal lock.
pub(crate) fn locked_command(
    runtime: &Path,
    name: &str,
    lock: RawFd,
    instance: &str,
) -> Result<Command, String> {
    let mut command = crate::entry::command(runtime, name)?;
    command
        .env("NODE_PLANE_PROFILE_LOCK_FD", lock.to_string())
        .env("NODE_PLANE_AGENT_INSTANCE_ID", instance);
    // SAFETY: fcntl is async-signal-safe; it only clears close-on-exec of
    // the lock in the child, which then inherits it.
    unsafe {
        command.pre_exec(move || {
            if libc::fcntl(lock, libc::F_SETFD, 0) == -1 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    Ok(command)
}

/// Stdout of a locked runtime entry point, or its error.
pub(crate) fn run_locked(
    runtime: &Path,
    name: &str,
    args: &[String],
    lock: RawFd,
    instance: &str,
) -> Result<String, String> {
    let output = locked_command(runtime, name, lock, instance)?
        .args(args)
        .output()
        .map_err(|err| format!("failed to execute {name}: {err}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let stdout = String::from_utf8_lossy(&output.stdout);
        let detail = if stderr.trim().is_empty() {
            stdout.trim()
        } else {
            stderr.trim()
        };
        return Err(format!(
            "{name} failed: {}",
            if detail.is_empty() {
                "unknown error"
            } else {
                detail
            }
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

/// The mutation of a profile intent: the protocol's add or delete entry
/// point. A temporary identity of a stopped leased container is pruned from
/// its config instead.
pub(crate) fn run(
    intent: &Intent,
    lock: RawFd,
    runtime: &Path,
    instance: &str,
) -> Result<Response, String> {
    let protocol = intent.protocol.as_str();
    if intent.lease_seconds.is_some() {
        let ready = leases::RuntimeSettings::read(protocol)
            .is_ok_and(|settings| leases::enforcement_ready(&System, protocol, &settings));
        if !ready {
            return Err("temporary expiry enforcement is unavailable".into());
        }
    }
    if !intent.ensure() && temporary_name(&intent.runtime_name) {
        let settings = leases::RuntimeSettings::read(protocol)?;
        let record = leases::container(&System, protocol, &settings)?;
        if !leases::running(&record) {
            leases::prune_config(
                protocol,
                &settings.config,
                std::slice::from_ref(&intent.runtime_name),
                &settings.server_key,
            )?;
            return Ok(Response::ok());
        }
    }
    let script = match (protocol, intent.ensure()) {
        ("xray", true) => "xray-add-user-existing.sh".to_string(),
        (protocol, ensure) => format!("{protocol}-{}-user.sh", if ensure { "add" } else { "del" }),
    };
    let mut args = vec![intent.runtime_name.clone()];
    if protocol == "xray" && intent.ensure() {
        args.extend([intent.uuid.clone(), intent.short_id.clone()]);
    }
    // The AWG output keeps its format: the agent extracts the client config.
    Ok(Response {
        summary: run_locked(runtime, &script, &args, lock, instance)?,
        payload_json: String::new(),
    })
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Barrier, Mutex};

    use super::*;
    use crate::intents::test_journal;

    fn intent(revision: i64, command: &str) -> Intent {
        Intent {
            command_id: command.into(),
            protocol: "awg".into(),
            runtime_name: "p_test".into(),
            revision,
            action: "ensure".into(),
            uuid: String::new(),
            short_id: String::new(),
            lease_seconds: None,
        }
    }

    fn delete(revision: i64, command: &str) -> Intent {
        Intent {
            action: "delete".into(),
            ..intent(revision, command)
        }
    }

    /// A runner that records what it ran.
    fn recorder(
        calls: &Mutex<Vec<Intent>>,
    ) -> impl FnMut(&Intent, RawFd) -> Result<Response, String> + '_ {
        move |intent, _| {
            calls.lock().unwrap().push(intent.clone());
            Ok(Response {
                summary: "private result".into(),
                payload_json: String::new(),
            })
        }
    }

    fn uncertain(_: &Intent, _: RawFd) -> Result<Response, String> {
        Err("unknown remote outcome".into())
    }

    fn status(journal: &Path) -> Vec<String> {
        let connection = Connection::open(journal).unwrap();
        let mut statement = connection
            .prepare("SELECT status FROM commands ORDER BY id")
            .unwrap();
        statement
            .query_map([], |row| row.get(0))
            .unwrap()
            .collect::<Result<_, _>>()
            .unwrap()
    }

    #[test]
    fn intents_parse_strictly() {
        let value = intent(1, "task-1").to_value();
        assert_eq!(Intent::parse(&value).unwrap(), intent(1, "task-1"));
        let mut extra = value.clone();
        extra["other"] = json!(1);
        assert!(Intent::parse(&extra).is_err());
        let mut bad = value.clone();
        bad["revision"] = json!(true);
        assert!(Intent::parse(&bad).is_err());
        bad["revision"] = json!(0);
        assert!(Intent::parse(&bad).is_err());
        let mut xray = value.clone();
        xray["protocol"] = json!("xray");
        assert!(
            Intent::parse(&xray).is_err(),
            "an Xray ensure needs its identity"
        );
        xray["uuid"] = json!("11111111-1111-4111-8111-111111111111");
        xray["short_id"] = json!("0123456789abcdef");
        assert!(Intent::parse(&xray).is_ok());
        let mut lease = value;
        lease["lease_seconds"] = json!(43200);
        assert!(
            Intent::parse(&lease).is_err(),
            "temporary identities are tmp_ names"
        );
        lease["runtime_name"] = json!(format!("tmp_{}", "a".repeat(32)));
        assert!(Intent::parse(&lease).is_ok());
        lease["lease_seconds"] = json!(60);
        assert!(Intent::parse(&lease).is_err());
    }

    #[test]
    fn a_repeated_command_returns_its_first_result_without_running() {
        let journal = test_journal("repeat");
        let calls = Mutex::new(Vec::new());
        let first = apply(
            &intent(1, "task-1"),
            &journal,
            "agent",
            &mut recorder(&calls),
        )
        .unwrap();
        let second = apply(
            &intent(1, "task-1"),
            &journal,
            "agent",
            &mut recorder(&calls),
        )
        .unwrap();
        assert_eq!(first, second);
        assert_eq!(calls.lock().unwrap().len(), 1);
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(&journal).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(lookup(&intent(1, "task-1"), &journal).unwrap(), first);
        assert!(lookup(&delete(1, "task-1"), &journal).is_err());
        assert!(
            apply(
                &delete(1, "task-1"),
                &journal,
                "agent",
                &mut recorder(&calls)
            )
            .is_err(),
            "another payload cannot reuse the command"
        );
        assert_eq!(calls.lock().unwrap().len(), 1);
    }

    #[test]
    fn the_revision_fence_rejects_delayed_commands() {
        let journal = test_journal("fence");
        let calls = Mutex::new(Vec::new());
        apply(
            &intent(1, "task-1"),
            &journal,
            "agent",
            &mut recorder(&calls),
        )
        .unwrap();
        apply(
            &delete(2, "task-2"),
            &journal,
            "agent",
            &mut recorder(&calls),
        )
        .unwrap();
        for stale in [intent(1, "late-task"), intent(2, "another-task")] {
            assert!(apply(&stale, &journal, "agent", &mut recorder(&calls)).is_err());
        }
        // The old command returns its history; the user is not enabled again.
        apply(
            &intent(1, "task-1"),
            &journal,
            "agent",
            &mut recorder(&calls),
        )
        .unwrap();
        let actions: Vec<String> = calls
            .lock()
            .unwrap()
            .iter()
            .map(|i| i.action.clone())
            .collect();
        assert_eq!(actions, ["ensure", "delete"]);
    }

    #[test]
    fn an_uncertain_failure_blocks_the_whole_node() {
        let journal = test_journal("uncertain");
        let calls = Mutex::new(Vec::new());
        assert!(apply(&intent(1, "task-1"), &journal, "agent", &mut uncertain).is_err());
        let other = Intent {
            runtime_name: "p_other".into(),
            ..intent(1, "other-profile")
        };
        for next in [intent(1, "task-1"), delete(2, "new-task"), other] {
            assert!(apply(&next, &journal, "agent", &mut recorder(&calls)).is_err());
        }
        assert!(calls.lock().unwrap().is_empty());
        assert_eq!(status(&journal), ["running"]);
        assert!(lookup(&intent(1, "task-1"), &journal).is_err());
    }

    #[test]
    fn a_newer_revision_waits_for_the_running_mutation() {
        let journal = test_journal("serial");
        let calls = Arc::new(Mutex::new(Vec::new()));
        let started = Arc::new(Barrier::new(2));
        let first = {
            let (journal, calls, started) = (journal.clone(), calls.clone(), started.clone());
            std::thread::spawn(move || {
                apply(&intent(1, "task-1"), &journal, "agent", &mut |intent, _| {
                    started.wait();
                    std::thread::sleep(std::time::Duration::from_millis(200));
                    calls.lock().unwrap().push(intent.revision);
                    Ok(Response::ok())
                })
            })
        };
        started.wait();
        apply(&delete(2, "task-2"), &journal, "agent", &mut |intent, _| {
            calls.lock().unwrap().push(intent.revision);
            Ok(Response::ok())
        })
        .unwrap();
        first.join().unwrap().unwrap();
        assert_eq!(*calls.lock().unwrap(), [1, 2]);
    }

    #[test]
    fn repair_requires_an_agent_restart_and_live_inspection() {
        let journal = test_journal("repair");
        let observation = json!({"disk_present": true, "live_present": false,
            "identity_matches": false, "config_available": false});
        assert!(apply(&intent(1, "task-1"), &journal, "before", &mut uncertain).is_err());
        let mut seen = |_: &Intent| Ok(observation.clone());
        assert!(resolve_interrupted(&intent(1, "task-1"), &journal, "before", &mut seen).is_err());
        let mut partial = |_: &Intent| Ok(json!({"available": false}));
        assert!(
            resolve_interrupted(&intent(1, "task-1"), &journal, "after", &mut partial).is_err()
        );
        let resolved =
            resolve_interrupted(&intent(1, "task-1"), &journal, "after", &mut seen).unwrap();
        assert_eq!(resolved["observation"], observation);
        let mut unused = |_: &Intent| Err("not inspected again".to_string());
        assert_eq!(
            resolve_interrupted(&intent(1, "task-1"), &journal, "after", &mut unused).unwrap(),
            resolved
        );
        let calls = Mutex::new(Vec::new());
        assert!(
            apply(
                &intent(1, "task-1"),
                &journal,
                "after",
                &mut recorder(&calls)
            )
            .is_err()
        );
        apply(
            &delete(2, "replacement"),
            &journal,
            "after",
            &mut recorder(&calls),
        )
        .unwrap();
        assert_eq!(calls.lock().unwrap().len(), 1);
    }

    #[test]
    fn repair_cannot_change_the_payload_or_resolve_a_success() {
        let journal = test_journal("repair-success");
        let calls = Mutex::new(Vec::new());
        apply(
            &intent(1, "task-1"),
            &journal,
            "agent",
            &mut recorder(&calls),
        )
        .unwrap();
        let mut empty = |_: &Intent| Ok(json!({}));
        assert!(resolve_interrupted(&intent(1, "task-1"), &journal, "new", &mut empty).is_err());
        assert!(resolve_interrupted(&delete(1, "task-1"), &journal, "new", &mut empty).is_err());
    }

    #[test]
    fn a_temporary_lease_is_recorded_and_cannot_be_renewed() {
        let journal = test_journal("lease");
        let name = format!("tmp_{}", "a".repeat(32));
        let temporary = Intent {
            runtime_name: name.clone(),
            lease_seconds: Some(43200),
            ..intent(1, "lease-1")
        };
        let calls = Mutex::new(Vec::new());
        let response = apply(&temporary, &journal, "agent", &mut recorder(&calls)).unwrap();
        let payload: Value = serde_json::from_str(&response.payload_json).unwrap();
        assert_eq!(payload["revocation_mode"], "peer_removed");
        let expires = parse_timestamp(payload["expires_at"].as_str().unwrap()).unwrap();
        assert!(expires > Utc::now() + Duration::seconds(43000));
        let again = Intent {
            command_id: "lease-2".into(),
            revision: 2,
            ..temporary.clone()
        };
        assert!(apply(&again, &journal, "agent", &mut recorder(&calls)).is_err());
        let permanent = Intent {
            runtime_name: name.clone(),
            ..intent(2, "permanent")
        };
        assert!(apply(&permanent, &journal, "agent", &mut recorder(&calls)).is_err());
        let revoke = Intent {
            runtime_name: name,
            ..delete(2, "revoke")
        };
        apply(&revoke, &journal, "agent", &mut recorder(&calls)).unwrap();
        let connection = Connection::open(&journal).unwrap();
        let state: String = connection
            .query_row("SELECT status FROM temporary_leases", [], |row| row.get(0))
            .unwrap();
        assert_eq!(state, "revoked");
        assert_eq!(calls.lock().unwrap().len(), 2);
    }
}
