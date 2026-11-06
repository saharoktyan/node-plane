//! Backend node operations, serialized with profile and settings commands
//! through the journal: maintenance, inspection and temporary access over
//! the authenticated node RPC. The journal survives runtime cleanup; an
//! unknown outcome is recovered by reading the exact command, never by
//! replaying a destructive one. Replaces `backend-node-operation.py`.
use std::fs;
use std::net::{TcpListener, UdpSocket};
use std::os::fd::RawFd;
use std::path::Path;

use rusqlite::{OptionalExtension, params};
use serde_json::{Map, Value, json};

use super::leases::{self, Host};
use super::profile::{self, Intent, LEASE_DURATIONS, temporary_name};
use super::runtime::{Paths, Runtime, managed_ports, owner, remove_protocols};
use super::settings::{Progress, Settings};
use super::{
    Lock, Response, apply, commands_schema, database, disabled, has_table, open, open_readonly,
    scheduler, sha256_hex, sorted_dumps,
};
use crate::protocols::awg::client::all_fields;

pub(crate) const ACTIONS: [&str; 11] = [
    "bootstrap",
    "reinstall_keep",
    "reinstall_clean",
    "cleanup_runtime",
    "install_docker",
    "check_ports",
    "open_ports",
    "sync_runtime",
    "sync_env",
    "sync_xray",
    "regenerate_entropy",
];

/// Operations that change a runtime: refused for a runtime of another node.
const OWNED: [&str; 8] = [
    "cleanup_runtime",
    "bootstrap",
    "reinstall_clean",
    "reinstall_keep",
    "sync_env",
    "sync_xray",
    "sync_runtime",
    "regenerate_entropy",
];

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Error {
    /// node.env assigns the runtime to another node; nothing changed.
    OwnerConflict(String),
    Failed(String),
}

impl From<String> for Error {
    fn from(err: String) -> Self {
        Self::Failed(err)
    }
}

impl From<&str> for Error {
    fn from(err: &str) -> Self {
        Self::Failed(err.into())
    }
}

/// Runs a node operation while the journal lock is held.
pub(crate) type Runner<'a> =
    dyn FnMut(&str, &Value, RawFd, &Progress) -> Result<Value, String> + 'a;

fn retired(intent: &Value, action: &str) -> Value {
    json!({"node_key": intent["node_key"], "action": action,
           "revision": intent["revision"], "retired": true})
}

/// Records and runs a node operation; `resolve_<action>` retires it, and
/// `recover` only reads its result.
#[allow(clippy::too_many_arguments)]
pub(crate) fn command(
    journal: &Path,
    env: &Path,
    instance: &str,
    action: &str,
    command_id: &str,
    intent: &Value,
    recover: bool,
    runner: &mut Runner<'_>,
) -> Result<Value, Error> {
    let (action, resolving) = match action.strip_prefix("resolve_") {
        Some(action) => (action, true),
        None => (action, false),
    };
    if !ACTIONS.contains(&action) || !profile::command_id(command_id) {
        return Err("invalid node operation".into());
    }
    let map = intent.as_object().ok_or("invalid node operation intent")?;
    if matches!(
        action,
        "install_docker" | "sync_runtime" | "cleanup_runtime"
    ) {
        let keys = [
            "node_key",
            "command_id",
            "revision",
            "protocols",
            "settings",
        ];
        if map.len() != keys.len() || keys.iter().any(|key| !map.contains_key(*key)) {
            return Err("invalid node operation intent".into());
        }
    } else {
        Settings::check(&settings_intent(intent))?;
    }
    if intent["command_id"] != command_id {
        return Err("command identity mismatch".into());
    }
    if OWNED.contains(&action) && !resolving && !recover {
        // Before the journal or any file: a refusal leaves no command with
        // an unknown outcome.
        let owner = owner(env)?;
        if intent["node_key"] != owner.as_str() {
            let owner: String = owner
                .chars()
                .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
                .take(64)
                .collect();
            return Err(Error::OwnerConflict(if owner.is_empty() {
                "unknown".into()
            } else {
                owner
            }));
        }
    }
    let fingerprint =
        sha256_hex(sorted_dumps(&json!({"action": action, "intent": intent})).as_bytes());
    let lock = Lock::exclusive(journal)?;
    if disabled(journal) {
        return Err("agent removal is in progress".into());
    }
    let connection = open(journal)?;
    commands_schema(&connection)?;
    let prior: Option<(String, String, Option<String>)> = connection
        .query_row(
            "SELECT fingerprint, status, response FROM commands WHERE id = ?",
            [command_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()
        .map_err(database)?;
    if let Some((prior, status, response)) = prior {
        if prior != fingerprint {
            return Err("command identity conflict".into());
        }
        if resolving && matches!(status.as_str(), "running" | "retired") {
            // Every mutation subprocess inherits this lock: holding it proves
            // the old executor finished. Retiring the fingerprint fences a
            // delayed duplicate.
            connection
                .execute(
                    "UPDATE commands SET status = 'retired' WHERE id = ?",
                    [command_id],
                )
                .map_err(database)?;
            return Ok(retired(intent, action));
        }
        if status != "succeeded" {
            return Err("node operation outcome is uncertain".into());
        }
        return serde_json::from_str(response.as_deref().unwrap_or_default())
            .map_err(|_| "invalid node operation result".into());
    }
    if resolving {
        connection
            .execute(
                "INSERT INTO commands VALUES (?, ?, 'retired', NULL, ?)",
                params![command_id, fingerprint, instance],
            )
            .map_err(database)?;
        return Ok(retired(intent, action));
    }
    if recover {
        return Err("node operation result not found".into());
    }
    let running = connection
        .prepare("SELECT 1 FROM commands WHERE status = 'running' LIMIT 1")
        .and_then(|mut statement| statement.exists([]))
        .map_err(database)?;
    if running {
        return Err("unfinished operation requires reconciliation".into());
    }
    connection
        .execute(
            "INSERT INTO commands VALUES (?, ?, 'running', NULL, ?)",
            params![command_id, fingerprint, instance],
        )
        .map_err(database)?;
    let progress = Progress::new(journal, command_id);
    if action == "install_docker" {
        progress.step("docker_verify");
    }
    let result = json!({"node_key": intent["node_key"], "action": action,
        "revision": intent["revision"],
        "result": runner(action, intent, lock.fd(), &progress)?});
    connection
        .execute(
            "UPDATE commands SET status = 'succeeded', response = ? WHERE id = ?",
            params![result.to_string(), command_id],
        )
        .map_err(database)?;
    Ok(result)
}

/// The node operation intent as a settings intent.
fn settings_intent(intent: &Value) -> Value {
    let mut value = intent.clone();
    value["kind"] = json!("node_settings");
    value
}

/// The ports of the intent's protocols.
fn ports(settings: &Settings<'_>) -> Vec<(&'static str, i64)> {
    let mut ports = Vec::new();
    if settings.enabled("xray") {
        ports.push(("tcp", settings.port("xray_tcp_port", 0)));
        ports.push(("tcp", settings.port("xray_xhttp_port", 0)));
    }
    if settings.enabled("awg") {
        ports.push(("udp", settings.port("awg_port", 0)));
    }
    ports
}

fn free(protocol: &str, port: u16) -> bool {
    if protocol == "tcp" {
        TcpListener::bind(("0.0.0.0", port)).is_ok()
    } else {
        UdpSocket::bind(("0.0.0.0", port)).is_ok()
    }
}

fn docker_available(host: &dyn Host) -> bool {
    host.installed("docker") && host.succeeds(&["docker", "info"], Some(30))
}

/// The protocol list of an intent.
fn protocols(intent: &Value) -> Vec<&str> {
    intent["protocols"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect()
}

/// The mutation of a node operation.
pub(crate) fn run(
    host: &dyn Host,
    runtime: &Runtime,
    action: &str,
    intent: &Value,
    lock: RawFd,
    progress: &Progress,
) -> Result<Value, String> {
    match action {
        "cleanup_runtime" => {
            remove_protocols(host, runtime, &protocols(intent))?;
            return Ok(json!({"cleaned": true}));
        }
        "install_docker" => {
            if !docker_available(host) {
                return Err("Docker is unavailable".into());
            }
            return Ok(json!({"docker": true}));
        }
        "sync_runtime" => {
            if runtime
                .units
                .join("node-plane-lease-expiry.service")
                .is_file()
            {
                scheduler::ensure(host, &runtime.units, None)?;
            }
            return Ok(json!({"synced": true}));
        }
        "regenerate_entropy" => {
            profile::run_locked(
                &runtime.root,
                "regenerate-awg-entropy.sh",
                &[],
                lock,
                &runtime.instance,
            )?;
            return Ok(json!({"regenerated": true}));
        }
        _ => {}
    }
    let value = settings_intent(intent);
    let settings = Settings::check(&value)?;
    match action {
        "check_ports" => {
            let owned = managed_ports(host, &runtime.env);
            let statuses: Vec<Value> = ports(&settings)
                .into_iter()
                .map(|(protocol, port)| {
                    let port = u16::try_from(port).unwrap_or_default();
                    let status = if owned.contains(&(protocol, port)) {
                        "managed"
                    } else if free(protocol, port) {
                        "free"
                    } else {
                        "busy"
                    };
                    json!({"protocol": protocol, "port": port, "status": status})
                })
                .collect();
            return Ok(json!({"ports": statuses}));
        }
        "open_ports" => {
            if !host.installed("ufw") {
                return Ok(json!({"firewall": "unmanaged"}));
            }
            for (protocol, port) in ports(&settings) {
                host.check(&["ufw", "allow", &format!("{port}/{protocol}")], Some(120))?;
            }
            host.check(&["ufw", "reload"], Some(120))?;
            return Ok(json!({"firewall": "opened"}));
        }
        "reinstall_keep" => {
            let facts = inspect(host, runtime);
            if settings
                .protocols()
                .iter()
                .any(|protocol| facts[format!("{protocol}_config_valid")] != true)
            {
                return Err("reusable protocol config is missing".into());
            }
        }
        "reinstall_clean" => remove_protocols(host, runtime, &settings.protocols())?,
        _ => {}
    }
    // Bootstrap, reinstalls and repairs share the settings path; identities
    // stay unless a clean reinstall removed them.
    let response = apply::run(host, runtime, &settings, lock, progress)?;
    serde_json::from_str(&response.payload_json).map_err(|_| "invalid node settings result".into())
}

/// Host resources the controller shows; a missing reading stays unknown.
pub(crate) fn host_metrics(root: &Path) -> Value {
    let mut metrics = crate::host::metrics::resources();
    let mut stat: libc::statvfs = unsafe { std::mem::zeroed() };
    let path = std::ffi::CString::new(root.as_os_str().as_encoded_bytes()).unwrap_or_default();
    // SAFETY: a valid C string and a zeroed statvfs the call fills.
    if unsafe { libc::statvfs(path.as_ptr(), &mut stat) } == 0 && stat.f_blocks > 0 {
        let fragment = stat.f_frsize;
        metrics["disk_free_percent"] =
            json!((1000.0 * stat.f_bavail as f64 / stat.f_blocks as f64).round() / 10.0);
        metrics["disk_total_bytes"] = json!(stat.f_blocks * fragment);
        metrics["disk_free_bytes"] = json!(stat.f_bavail * fragment);
    }
    if let Some(uptime) = fs::read_to_string("/proc/uptime")
        .ok()
        .and_then(|raw| raw.split_whitespace().next()?.parse::<f64>().ok())
    {
        metrics["uptime_seconds"] = json!(uptime as u64);
    }
    metrics
}

/// What is installed and running, and the public AWG parameters.
pub(crate) fn inspect(host: &dyn Host, runtime: &Runtime) -> Value {
    let paths = Paths::read(&runtime.env).ok();
    let docker = docker_available(host);
    let xray_valid = paths.as_ref().is_some_and(|paths| {
        fs::read_to_string(&paths.xray)
            .ok()
            .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
            .is_some_and(|data| {
                ["reality-tcp", "reality-xhttp"].iter().all(|tag| {
                    data["inbounds"]
                        .as_array()
                        .and_then(|items| items.iter().rev().find(|item| item["tag"] == *tag))
                        .and_then(|item| {
                            item["streamSettings"]["realitySettings"]["privateKey"].as_str()
                        })
                        .is_some_and(|key| !key.is_empty())
                })
            })
    });
    let awg = paths
        .as_ref()
        .and_then(|paths| fs::read_to_string(&paths.awg).ok());
    let mut entropy = Vec::new();
    let mut parameters = Map::new();
    let mut awg_valid = false;
    if let Some(text) = &awg {
        awg_valid = text.lines().any(|line| {
            line.strip_prefix("PrivateKey")
                .map(|rest| rest.trim_start())
                .and_then(|rest| rest.strip_prefix('='))
                .is_some_and(|value| !value.trim().is_empty())
        }) && text.contains("HeaderProtectionKey");
        let entropic = |name: &str| {
            matches!(name, "RandomTrailers" | "DisableCookies")
                || (name.len() == 2
                    && matches!(
                        (name.as_bytes()[0], name.as_bytes()[1]),
                        (b'S' | b'H', b'1'..=b'4') | (b'I', b'1'..=b'5')
                    ))
        };
        entropy = text
            .lines()
            .filter(|line| {
                line.split_once('=').is_some_and(|(name, _)| {
                    entropic(name.trim_end()) && !name.starts_with(char::is_whitespace)
                })
            })
            .map(|line| json!(line))
            .collect();
        let mut section = String::new();
        for raw in text.lines() {
            let line = raw.trim();
            if line.starts_with('[') && line.ends_with(']') {
                section = line.to_lowercase();
            } else if section == "[interface]"
                && !line.starts_with('#')
                && let Some((key, value)) = line.split_once('=')
                && all_fields().any(|field| field == key.trim())
                && !value.trim().is_empty()
            {
                parameters.insert(key.trim().into(), json!(value.trim()));
            }
        }
    }
    let container = |protocol: &str| -> (bool, bool) {
        let Some(paths) = paths.as_ref().filter(|_| docker) else {
            return (false, false);
        };
        match host.run(
            &[
                "docker",
                "inspect",
                "-f",
                "{{.State.Running}}",
                paths.container(protocol),
            ],
            Some(30),
        ) {
            Ok(output) if output.code == Some(0) => (true, output.stdout.trim() == "true"),
            _ => (false, false),
        }
    };
    let (xray_installed, xray_running) = container("xray");
    let (awg_installed, awg_running) = container("awg");
    json!({"docker": docker, "xray_config_valid": xray_valid, "awg_config_valid": awg_valid,
        "xray_installed": xray_installed, "awg_installed": awg_installed,
        "xray_running": xray_running, "awg_running": awg_running, "entropy": entropy,
        "awg_parameters": parameters, "host_metrics": host_metrics(&runtime.root),
        "inspection_available": true})
}

/// What temporary access needs of the node besides the journal.
pub(crate) trait Temporary {
    /// Installs the expiry timer and the protocol's supervisor.
    fn schedule(&mut self, protocol: &str) -> Result<(), String>;
    /// The mutation of a profile intent.
    fn mutate(&mut self, intent: &Intent, lock: RawFd) -> Result<Response, String>;
    /// `inspect-profile.sh` of an identity that may be unused.
    fn inspect(&mut self, intent: &Intent) -> Result<Value, String>;
    fn enforcement_ready(&mut self, protocol: &str) -> bool;
}

/// The node itself.
pub(crate) struct Node<'a> {
    pub(crate) host: &'a dyn Host,
    pub(crate) runtime: &'a Runtime,
}

impl Temporary for Node<'_> {
    fn schedule(&mut self, protocol: &str) -> Result<(), String> {
        scheduler::ensure(self.host, &self.runtime.units, Some(protocol))
    }

    fn mutate(&mut self, intent: &Intent, lock: RawFd) -> Result<Response, String> {
        profile::run(intent, lock, &self.runtime.root, &self.runtime.instance)
    }

    fn inspect(&mut self, intent: &Intent) -> Result<Value, String> {
        let mut command = crate::entry::command(&self.runtime.root, "inspect-profile.sh")?;
        command.args([&intent.protocol, &intent.runtime_name]);
        if intent.protocol == "xray" {
            command.arg(&intent.uuid);
        } else {
            command.arg("--allow-unused");
        }
        let output = command
            .output()
            .map_err(|err| format!("inspect-profile.sh failed: {err}"))?;
        if !output.status.success() {
            return Err("live inspection unavailable".into());
        }
        serde_json::from_slice(&output.stdout).map_err(|_| "live inspection unavailable".into())
    }

    fn enforcement_ready(&mut self, protocol: &str) -> bool {
        leases::RuntimeSettings::read(protocol)
            .is_ok_and(|settings| leases::enforcement_ready(self.host, protocol, &settings))
    }
}

fn revocation_mode(protocol: &str) -> &'static str {
    if protocol == "xray" {
        "new_connections_only"
    } else {
        "peer_removed"
    }
}

/// The lease of a profile: its command, deadline and status.
fn lease(journal: &Path, intent: &Intent) -> Result<Option<(String, String, String)>, String> {
    let connection = open_readonly(journal)?;
    if !has_table(&connection, "temporary_leases")? {
        return Ok(None);
    }
    connection
        .query_row(
            "SELECT command_id, expires_at, status FROM temporary_leases WHERE protocol = ? AND profile = ?",
            params![intent.protocol, intent.runtime_name],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()
        .map_err(database)
}

/// Temporary access over the node RPC: grant, revoke, status and the
/// cancellation of an unused grant. An unknown action of an old runtime
/// fails instead of losing its lifetime.
pub(crate) fn temporary(
    journal: &Path,
    instance: &str,
    action: &str,
    command_id: &str,
    intent: &Value,
    recover: bool,
    node: &mut dyn Temporary,
) -> Result<Value, String> {
    // node_key selected this agent; the agent checked its identity.
    let mut value = intent.clone();
    if let Some(map) = value.as_object_mut() {
        map.remove("node_key");
    }
    let intent = Intent::parse(&value)?;
    if intent.command_id != command_id {
        return Err("temporary operation is not supported".into());
    }
    match action {
        "temporary_resolve" => {
            if !recover {
                return Err("temporary cancellation requires recovery".into());
            }
            return resolve_unused(journal, instance, &intent, node);
        }
        "temporary_status" => {
            if !recover {
                return Err("temporary status is read-only".into());
            }
            let _lock = Lock::shared(journal)?;
            let (lease_command, expires_at, status) = lease(journal, &intent)?
                .filter(|(lease_command, ..)| *lease_command == command_id)
                .ok_or("temporary identity not found")?;
            let _ = lease_command;
            return Ok(json!({"expires_at": expires_at, "status": status,
                "enforcement_ready": node.enforcement_ready(&intent.protocol)}));
        }
        "temporary_ensure" => {
            if intent.lease_seconds.is_none() || intent.action != "ensure" {
                return Err("temporary lease is required".into());
            }
        }
        "temporary_revoke" => {
            if intent.action != "delete" || intent.lease_seconds.is_some() {
                return Err("invalid temporary revocation".into());
            }
            // Never a permanent peer.
            let (_, _, status) = lease(journal, &intent)?.ok_or("temporary identity not found")?;
            if matches!(status.as_str(), "expired" | "revoked") {
                return Ok(json!({"revocation_mode": revocation_mode(&intent.protocol)}));
            }
        }
        _ => return Err("invalid temporary action".into()),
    }
    if disabled(journal) {
        return Err("agent removal is in progress".into());
    }
    let response = if recover {
        profile::lookup(&intent, journal)?
    } else {
        if action == "temporary_ensure" {
            node.schedule(&intent.protocol)?;
        }
        profile::apply(&intent, journal, instance, &mut |intent, lock| {
            node.mutate(intent, lock)
        })?
    };
    let mut result: Value = if response.payload_json.is_empty() {
        json!({})
    } else {
        serde_json::from_str(&response.payload_json)
            .map_err(|_| "invalid temporary result".to_string())?
    };
    if action == "temporary_ensure" {
        if !result["expires_at"]
            .as_str()
            .is_some_and(|at| !at.is_empty())
        {
            return Err("temporary expiry receipt missing".into());
        }
        if intent.protocol == "awg" {
            let summary = &response.summary;
            let start = summary
                .find("[Interface]")
                .ok_or("AWG client config missing")?;
            let config = summary[start..]
                .split("\n===========")
                .next()
                .unwrap_or_default()
                .trim();
            let key = summary
                .lines()
                .map(str::trim)
                .find(|line| line.starts_with("vpn://"))
                .ok_or("AWG client key missing")?;
            result["wg_conf"] = json!(config);
            result["vpn_key"] = json!(key);
        } else {
            // REALITY keeps the inbound's shortId and public parameters: only
            // the client UUID is new.
            result["xray_uuid"] = json!(intent.uuid);
        }
    } else {
        result["revocation_mode"] = json!(revocation_mode(&intent.protocol));
    }
    Ok(result)
}

/// Fences an unused grant before its cancellation is acknowledged. A
/// scheduler failure may leave no command row: the superseded receipt also
/// fences a delayed original request then. A lease or a peer on disk or
/// live needs reconciliation instead.
fn resolve_unused(
    journal: &Path,
    instance: &str,
    intent: &Intent,
    node: &mut dyn Temporary,
) -> Result<Value, String> {
    if intent.action != "ensure"
        || !intent
            .lease_seconds
            .is_some_and(|seconds| LEASE_DURATIONS.contains(&seconds))
        || !temporary_name(&intent.runtime_name)
    {
        return Err("invalid temporary cancellation".into());
    }
    let fingerprint = intent.fingerprint();
    let _lock = Lock::exclusive(journal)?;
    if disabled(journal) {
        return Err("agent removal is in progress".into());
    }
    let connection = open(journal)?;
    commands_schema(&connection)?;
    let prior: Option<(String, String, Option<String>, String)> = connection
        .query_row(
            "SELECT fingerprint, status, response, instance_id FROM commands WHERE id = ?",
            [&intent.command_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()
        .map_err(database)?;
    if let Some((prior, status, response, owner)) = prior {
        if prior != fingerprint {
            return Err("command identity mismatch".into());
        }
        if status == "superseded" {
            return serde_json::from_str(response.as_deref().unwrap_or_default())
                .map_err(|_| "invalid repair record".into());
        }
        if status != "running" || owner == instance {
            return Err("agent service restart or confirmed journal is required".into());
        }
    }
    if has_table(&connection, "temporary_leases")? {
        let leased = connection
            .prepare("SELECT 1 FROM temporary_leases WHERE protocol = ? AND profile = ?")
            .and_then(|mut statement| {
                statement.exists(params![intent.protocol, intent.runtime_name])
            })
            .map_err(database)?;
        if leased {
            return Err("existing temporary lease requires reconciliation".into());
        }
    }
    let observation = node.inspect(intent)?;
    let absent = observation.as_object().is_some_and(|map| {
        map.len() == profile::OBSERVATION.len()
            && profile::OBSERVATION
                .iter()
                .all(|key| map.get(*key) == Some(&Value::Bool(false)))
    });
    if !absent {
        return Err("temporary absence is unconfirmed".into());
    }
    let record = json!({"unused_confirmed": true, "observation": observation});
    connection
        .execute(
            "INSERT INTO commands VALUES (?, ?, 'superseded', ?, ?) ON CONFLICT(id) DO UPDATE SET status = excluded.status, response = excluded.response",
            params![intent.command_id, fingerprint, sorted_dumps(&record), instance],
        )
        .map_err(database)?;
    Ok(record)
}

/// `node-plane-agent node-operation ACTION COMMAND_ID INTENT [--recover]`:
/// one node operation, as the agent runs it for the controller. A refused
/// runtime of another node exits 3.
pub(crate) fn run_command(args: &[String]) -> Option<i32> {
    let (first, rest) = args.split_first()?;
    if first != "node-operation" {
        return None;
    }
    let (action, command_id, intent, recover) = match rest {
        [action, command_id, intent] => (action, command_id, intent, false),
        [action, command_id, intent, flag] if flag == "--recover" => {
            (action, command_id, intent, true)
        }
        _ => {
            eprintln!("usage: node-operation ACTION COMMAND_ID INTENT [--recover]");
            return Some(2);
        }
    };
    let instance = super::instance();
    let runtime = Runtime::system(
        crate::protocols::settings::runtime(),
        leases::node_env(),
        &instance,
    );
    let journal = super::journal_path();
    let intent: Value = match serde_json::from_str(intent) {
        Ok(intent) => intent,
        Err(_) => {
            eprintln!("invalid node operation intent");
            return Some(2);
        }
    };
    let result = if action == "inspect" {
        Ok(inspect(&leases::System, &runtime))
    } else if action.starts_with("temporary_") {
        let mut node = Node {
            host: &leases::System,
            runtime: &runtime,
        };
        temporary(
            &journal, &instance, action, command_id, &intent, recover, &mut node,
        )
        .map_err(Error::Failed)
    } else {
        command(
            &journal,
            &runtime.env,
            &instance,
            action,
            command_id,
            &intent,
            recover,
            &mut |action, intent, lock, progress| {
                run(&leases::System, &runtime, action, intent, lock, progress)
            },
        )
    };
    Some(match result {
        Ok(value) => {
            println!("{value}");
            0
        }
        Err(Error::OwnerConflict(owner)) => {
            eprintln!("runtime_owner_conflict: {owner}");
            3
        }
        Err(Error::Failed(err)) => {
            eprintln!("{err}");
            1
        }
    })
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use rusqlite::Connection;

    use super::*;
    use crate::intents::leases::fake::Scripted;
    use crate::intents::test_journal;
    use crate::protocols::test_dir;

    fn intent() -> Value {
        json!({"node_key": "n1", "command_id": "job1", "revision": 2, "protocols": ["awg"],
            "settings": {"public_host": "node.example", "awg_port": 51820}})
    }

    /// A node.env of `owner`, in a directory of its own.
    fn env(owner: &str) -> std::path::PathBuf {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let dir = test_dir(&format!(
            "operation-env-{owner}-{}",
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::write(dir.join("node.env"), format!("SERVER_KEY={owner}\n")).unwrap();
        dir.join("node.env")
    }

    struct Calls(RefCell<Vec<String>>);

    impl Calls {
        fn runner(
            &self,
            result: Result<Value, String>,
        ) -> impl FnMut(&str, &Value, RawFd, &Progress) -> Result<Value, String> + '_ {
            move |action, _, _, _| {
                self.0.borrow_mut().push(action.into());
                result.clone()
            }
        }
    }

    fn execute(
        journal: &Path,
        action: &str,
        recover: bool,
        instance: &str,
        runner: &mut Runner<'_>,
    ) -> Result<Value, Error> {
        command(
            journal,
            &env("n1"),
            instance,
            action,
            "job1",
            &intent(),
            recover,
            runner,
        )
    }

    #[test]
    fn a_runtime_of_another_node_is_refused_before_the_journal() {
        let journal = test_journal("operation-owner");
        let calls = Calls(RefCell::new(Vec::new()));
        let result = command(
            &journal,
            &env("ger2"),
            "agent",
            "bootstrap",
            "job1",
            &intent(),
            false,
            &mut calls.runner(Ok(json!({}))),
        );
        assert_eq!(result, Err(Error::OwnerConflict("ger2".into())));
        assert!(calls.0.borrow().is_empty());
        assert!(!journal.exists() && !super::super::sidecar(&journal, ".lock").exists());
    }

    #[test]
    fn retries_and_recovery_do_not_repeat_a_clean_reinstall() {
        let journal = test_journal("operation-retry");
        let calls = Calls(RefCell::new(Vec::new()));
        let mut runner = calls.runner(Ok(json!({"verified": true})));
        let result = execute(&journal, "reinstall_clean", false, "agent", &mut runner).unwrap();
        assert_eq!(result["result"], json!({"verified": true}));
        assert_eq!(
            execute(&journal, "reinstall_clean", false, "agent", &mut runner).unwrap(),
            result
        );
        assert_eq!(
            execute(&journal, "reinstall_clean", true, "agent", &mut runner).unwrap(),
            result
        );
        assert_eq!(calls.0.borrow().len(), 1);
        assert!(execute(&journal, "reinstall_keep", false, "agent", &mut runner).is_err());
    }

    #[test]
    fn a_finished_executor_is_retired_under_the_lock_without_a_restart() {
        let journal = test_journal("operation-retire");
        let calls = Calls(RefCell::new(Vec::new()));
        let mut failing = calls.runner(Err("timeout".into()));
        assert!(execute(&journal, "reinstall_clean", false, "old", &mut failing).is_err());
        assert_eq!(
            execute(
                &journal,
                "resolve_reinstall_clean",
                true,
                "old",
                &mut failing
            )
            .unwrap()["retired"],
            true
        );
        assert_eq!(
            execute(
                &journal,
                "resolve_reinstall_clean",
                true,
                "new",
                &mut failing
            )
            .unwrap()["retired"],
            true
        );
        assert!(execute(&journal, "reinstall_clean", false, "new", &mut failing).is_err());
        assert_eq!(calls.0.borrow().len(), 1);
    }

    #[test]
    fn retiring_a_command_that_never_started_fences_a_delayed_request() {
        let journal = test_journal("operation-fence");
        let calls = Calls(RefCell::new(Vec::new()));
        let mut runner = calls.runner(Ok(json!({})));
        assert_eq!(
            execute(
                &journal,
                "resolve_reinstall_clean",
                true,
                "agent",
                &mut runner
            )
            .unwrap()["retired"],
            true
        );
        assert!(execute(&journal, "reinstall_clean", false, "agent", &mut runner).is_err());
        assert!(calls.0.borrow().is_empty());
    }

    #[test]
    fn progress_survives_a_failure_without_a_replay() {
        let journal = test_journal("operation-progress");
        let identity = "0a1b2c3d-0000-4000-8000-00000000000b";
        let mut value = intent();
        value["command_id"] = json!(identity);
        let calls = RefCell::new(0);
        let mut interrupted = |_: &str, _: &Value, _: RawFd, progress: &Progress| {
            *calls.borrow_mut() += 1;
            progress.step("xray_init");
            Err::<Value, String>("timeout".into())
        };
        let run = |recover: bool, runner: &mut Runner<'_>| {
            command(
                &journal,
                &env("n1"),
                "agent",
                "bootstrap",
                identity,
                &value,
                recover,
                runner,
            )
        };
        assert!(run(false, &mut interrupted).is_err());
        let receipt = journal
            .with_file_name("operation-progress")
            .join(format!("{identity}.json"));
        let observation: Value =
            serde_json::from_str(&fs::read_to_string(receipt).unwrap()).unwrap();
        assert_eq!(
            observation,
            json!({"operation_id": identity, "stage": "xray_init"})
        );
        assert!(run(true, &mut interrupted).is_err());
        assert_eq!(*calls.borrow(), 1);
    }

    #[test]
    fn invalid_operations_are_refused() {
        let journal = test_journal("operation-invalid");
        let calls = Calls(RefCell::new(Vec::new()));
        let mut runner = calls.runner(Ok(json!({})));
        assert!(execute(&journal, "reconcile_access", false, "agent", &mut runner).is_err());
        assert!(
            command(
                &journal,
                &env("n1"),
                "agent",
                "bootstrap",
                "other",
                &intent(),
                false,
                &mut runner
            )
            .is_err()
        );
        let mut extra = intent();
        extra["unexpected"] = json!(1);
        assert!(
            command(
                &journal,
                &env("n1"),
                "agent",
                "cleanup_runtime",
                "job1",
                &extra,
                false,
                &mut runner
            )
            .is_err()
        );
        assert!(calls.0.borrow().is_empty());
        assert!(!journal.exists());
    }

    #[test]
    fn bootstrap_and_reinstalls_take_the_settings_path_and_clean_only_on_a_clean_reinstall() {
        // Cleanup is checked before the settings: a refused cleanup proves
        // it ran, and a missing runtime stops the settings path.
        let root = test_dir("operation-paths");
        let runtime = Runtime {
            root: root.clone(),
            env: root.join("node.env"),
            units: root.join("units"),
            ra_policy: root.join("ra"),
            instance: "agent".into(),
        };
        let host = Scripted::new(|_| (0, String::new()));
        for action in ["bootstrap", "reinstall_keep", "reinstall_clean"] {
            let err = run(&host, &runtime, action, &intent(), 0, &Progress::none()).unwrap_err();
            // node.env is what makes a bootstrapped runtime.
            fs::write(root.join("node.env"), "SERVER_KEY=n1\n").unwrap();
            let expected = match action {
                "reinstall_keep" => "reusable protocol config is missing",
                "reinstall_clean" => "runtime cleanup path is outside managed directories",
                _ => "node runtime is not bootstrapped",
            };
            assert_eq!(err, expected, "{action}");
        }
        assert_eq!(
            run(
                &host,
                &runtime,
                "open_ports",
                &intent(),
                0,
                &Progress::none()
            )
            .unwrap(),
            json!({"firewall": "opened"})
        );
        assert_eq!(
            host.ran(&["ufw"]),
            [vec!["ufw", "allow", "51820/udp"], vec!["ufw", "reload"]]
        );
        let mut statuses = intent();
        statuses["settings"]["awg_port"] = json!(
            UdpSocket::bind(("0.0.0.0", 0))
                .map(|s| {
                    let port = s.local_addr().unwrap().port();
                    std::mem::forget(s);
                    port
                })
                .unwrap()
        );
        let ports = run(
            &host,
            &runtime,
            "check_ports",
            &statuses,
            0,
            &Progress::none(),
        )
        .unwrap();
        assert_eq!(ports["ports"][0]["status"], "busy");
    }

    #[test]
    fn inspection_distinguishes_absent_stopped_and_running_containers() {
        let root = test_dir("operation-inspect");
        fs::write(
            root.join("node.env"),
            "XRAY_CONFIG=/missing/xray.json\nAWG_CONFIG=/missing/wg0.conf\n",
        )
        .unwrap();
        let runtime = Runtime {
            root: root.clone(),
            env: root.join("node.env"),
            units: root.join("units"),
            ra_policy: root.join("ra"),
            instance: "agent".into(),
        };
        for (code, output, installed, running) in [
            (1, "", false, false),
            (0, "false", true, false),
            (0, "true", true, true),
        ] {
            let host = Scripted::new(move |args| match args {
                ["docker", "info"] => (0, String::new()),
                ["docker", "inspect", ..] => (code, output.into()),
                _ => (1, String::new()),
            });
            let facts = inspect(&host, &runtime);
            for protocol in ["awg", "xray"] {
                assert_eq!(facts[format!("{protocol}_installed")], installed);
                assert_eq!(facts[format!("{protocol}_running")], running);
            }
            assert_eq!(facts["inspection_available"], true);
            assert!(facts["host_metrics"]["disk_total_bytes"].as_u64().is_some());
        }
    }

    /// A node whose profile mutation and inspection are recorded.
    struct FakeNode {
        mutations: Vec<Intent>,
        summary: String,
        observation: Value,
        schedule: Result<(), String>,
    }

    impl FakeNode {
        fn new() -> Self {
            Self {
                mutations: Vec::new(),
                summary: "OK".into(),
                observation: json!({"disk_present": false, "live_present": false,
                    "identity_matches": false, "config_available": false}),
                schedule: Ok(()),
            }
        }
    }

    impl Temporary for FakeNode {
        fn schedule(&mut self, _: &str) -> Result<(), String> {
            self.schedule.clone()
        }

        fn mutate(&mut self, intent: &Intent, _: RawFd) -> Result<Response, String> {
            self.mutations.push(intent.clone());
            Ok(Response {
                summary: self.summary.clone(),
                payload_json: String::new(),
            })
        }

        fn inspect(&mut self, _: &Intent) -> Result<Value, String> {
            Ok(self.observation.clone())
        }

        fn enforcement_ready(&mut self, _: &str) -> bool {
            true
        }
    }

    fn grant(protocol: &str, command: &str) -> Value {
        let xray = protocol == "xray";
        json!({"command_id": command, "protocol": protocol,
            "runtime_name": format!("tmp_{}", "a".repeat(32)), "revision": 1, "action": "ensure",
            "uuid": if xray { "6f1c0d7e-4f5a-4b8e-9c2d-3e4f5a6b7c8d" } else { "" },
            "short_id": if xray { "0123456789abcdef" } else { "" }, "lease_seconds": 86400})
    }

    fn revocation(grant: &Value, command: &str) -> Value {
        let mut value = grant.clone();
        let map = value.as_object_mut().unwrap();
        map.remove("lease_seconds");
        map.insert("command_id".into(), json!(command));
        map.insert("action".into(), json!("delete"));
        map.insert("revision".into(), json!(2));
        map.insert("uuid".into(), json!(""));
        map.insert("short_id".into(), json!(""));
        value
    }

    fn call(
        journal: &Path,
        instance: &str,
        action: &str,
        intent: &Value,
        recover: bool,
        node: &mut FakeNode,
    ) -> Result<Value, String> {
        temporary(
            journal,
            instance,
            action,
            intent["command_id"].as_str().unwrap(),
            intent,
            recover,
            node,
        )
    }

    #[test]
    fn an_awg_grant_returns_its_exact_receipt_and_recovers_without_a_mutation() {
        let journal = test_journal("temporary-awg");
        let mut node = FakeNode::new();
        node.summary =
            "[Interface]\nPrivateKey = secret\n\n=========== key\nvpn://encoded\n====".into();
        let value = grant("awg", "grant-1");
        let first = call(
            &journal,
            "agent",
            "temporary_ensure",
            &value,
            false,
            &mut node,
        )
        .unwrap();
        let recovered = call(
            &journal,
            "agent",
            "temporary_ensure",
            &value,
            true,
            &mut node,
        )
        .unwrap();
        assert_eq!(first, recovered);
        assert_eq!(first["vpn_key"], "vpn://encoded");
        assert_eq!(first["wg_conf"], "[Interface]\nPrivateKey = secret");
        assert_eq!(node.mutations.len(), 1);
        call(
            &journal,
            "agent",
            "temporary_revoke",
            &revocation(&value, "revoke-1"),
            false,
            &mut node,
        )
        .unwrap();
        assert!(
            call(
                &journal,
                "agent",
                "temporary_ensure",
                &value,
                true,
                &mut node
            )
            .is_err()
        );
    }

    #[test]
    fn a_vless_grant_and_its_early_revocation_report_the_connection_limit() {
        let journal = test_journal("temporary-xray");
        let mut node = FakeNode::new();
        let mut value = grant("xray", "grant-1");
        value["lease_seconds"] = json!(259200);
        let receipt = call(
            &journal,
            "agent",
            "temporary_ensure",
            &value,
            false,
            &mut node,
        )
        .unwrap();
        assert_eq!(receipt["xray_uuid"], value["uuid"]);
        assert!(receipt.get("wg_conf").is_none());
        assert_eq!(
            call(
                &journal,
                "agent",
                "temporary_ensure",
                &value,
                true,
                &mut node
            )
            .unwrap(),
            receipt
        );
        let revoked = call(
            &journal,
            "agent",
            "temporary_revoke",
            &revocation(&value, "revoke-1"),
            false,
            &mut node,
        )
        .unwrap();
        assert_eq!(revoked["revocation_mode"], "new_connections_only");
        let status: String = Connection::open(&journal)
            .unwrap()
            .query_row("SELECT status FROM temporary_leases", [], |row| row.get(0))
            .unwrap();
        assert_eq!(status, "revoked");
        // A repeated revocation of a revoked lease reports it again.
        let again = call(
            &journal,
            "agent",
            "temporary_revoke",
            &revocation(&value, "revoke-2"),
            false,
            &mut node,
        )
        .unwrap();
        assert_eq!(again["revocation_mode"], "new_connections_only");
        assert_eq!(node.mutations.len(), 2);
    }

    #[test]
    fn status_is_read_only_and_accepts_the_transport_node_key() {
        let journal = test_journal("temporary-status");
        let mut node = FakeNode::new();
        let value = grant("xray", "grant-1");
        call(
            &journal,
            "agent",
            "temporary_ensure",
            &value,
            false,
            &mut node,
        )
        .unwrap();
        let mut transport = value.clone();
        transport["node_key"] = json!("n1");
        let state = call(
            &journal,
            "agent",
            "temporary_status",
            &transport,
            true,
            &mut node,
        )
        .unwrap();
        assert_eq!(
            (
                state["status"].as_str(),
                state["enforcement_ready"].as_bool()
            ),
            (Some("active"), Some(true))
        );
        assert!(
            call(
                &journal,
                "agent",
                "temporary_status",
                &transport,
                false,
                &mut node
            )
            .is_err()
        );
        Connection::open(&journal)
            .unwrap()
            .execute("UPDATE temporary_leases SET status='expired'", [])
            .unwrap();
        assert_eq!(
            call(
                &journal,
                "agent",
                "temporary_status",
                &value,
                true,
                &mut node
            )
            .unwrap()["status"],
            "expired"
        );
        assert_eq!(node.mutations.len(), 1);
    }

    #[test]
    fn a_scheduler_failure_or_an_invalid_grant_provisions_nothing() {
        let journal = test_journal("temporary-scheduler");
        let mut node = FakeNode::new();
        node.schedule = Err("systemd unavailable".into());
        assert!(
            call(
                &journal,
                "agent",
                "temporary_ensure",
                &grant("awg", "grant-1"),
                false,
                &mut node
            )
            .is_err()
        );
        node.schedule = Ok(());
        let mut long = grant("awg", "grant-2");
        long["lease_seconds"] = json!(345600);
        assert!(
            call(
                &journal,
                "agent",
                "temporary_ensure",
                &long,
                false,
                &mut node
            )
            .is_err()
        );
        let conflicting = grant("awg", "conflicting");
        assert!(
            temporary(
                &journal,
                "agent",
                "temporary_ensure",
                "request-id",
                &conflicting,
                false,
                &mut node
            )
            .is_err()
        );
        assert!(node.mutations.is_empty());
        assert!(!journal.exists());
    }

    #[test]
    fn a_revocation_never_deletes_a_permanent_peer() {
        let journal = test_journal("temporary-permanent");
        let mut node = FakeNode::new();
        let mut permanent = grant("awg", "grant-1");
        permanent.as_object_mut().unwrap().remove("lease_seconds");
        permanent["runtime_name"] = json!("permanent");
        let intent = Intent::parse(&permanent).unwrap();
        profile::apply(&intent, &journal, "agent", &mut |_, _| Ok(Response::ok())).unwrap();
        assert!(
            call(
                &journal,
                "agent",
                "temporary_revoke",
                &revocation(&permanent, "revoke-1"),
                false,
                &mut node
            )
            .is_err()
        );
        assert!(node.mutations.is_empty());
    }

    #[test]
    fn an_unused_grant_is_fenced_against_a_delayed_original() {
        let journal = test_journal("temporary-unused");
        let mut node = FakeNode::new();
        let value = grant("awg", "grant-1");
        let result = call(
            &journal,
            "agent",
            "temporary_resolve",
            &value,
            true,
            &mut node,
        )
        .unwrap();
        assert_eq!(result["unused_confirmed"], true);
        assert_eq!(
            call(
                &journal,
                "agent",
                "temporary_resolve",
                &value,
                true,
                &mut node
            )
            .unwrap(),
            result
        );
        let intent = Intent::parse(&value).unwrap();
        assert!(
            profile::apply(&intent, &journal, "agent", &mut |_, _| Ok(Response::ok())).is_err()
        );
        let mut shorter = value.clone();
        shorter["lease_seconds"] = json!(43200);
        assert!(
            call(
                &journal,
                "agent",
                "temporary_resolve",
                &shorter,
                true,
                &mut node
            )
            .is_err()
        );
        assert!(node.mutations.is_empty());
    }

    #[test]
    fn an_unused_grant_needs_absence_and_a_restarted_agent() {
        let journal = test_journal("temporary-unused-restart");
        let mut node = FakeNode::new();
        let value = grant("awg", "grant-1");
        let intent = Intent::parse(&value).unwrap();
        assert!(
            profile::apply(&intent, &journal, "old", &mut |_, _| Err(
                "interrupted".into()
            ))
            .is_err()
        );
        assert!(
            call(
                &journal,
                "old",
                "temporary_resolve",
                &value,
                true,
                &mut node
            )
            .is_err()
        );
        // The grant recorded its lease before the mutation: never cancelled.
        assert!(
            call(
                &journal,
                "new",
                "temporary_resolve",
                &value,
                true,
                &mut node
            )
            .is_err()
        );
        let other = grant("awg", "grant-2");
        let mut other_name = other.clone();
        other_name["runtime_name"] = json!(format!("tmp_{}", "b".repeat(32)));
        let absent = node.observation.clone();
        for observation in [
            json!({"disk_present": false, "live_present": true, "identity_matches": false, "config_available": false}),
            json!({"disk_present": true, "live_present": false, "identity_matches": false, "config_available": false}),
            json!({}),
            json!({"disk_present": false, "live_present": 0, "identity_matches": false, "config_available": false}),
        ] {
            node.observation = observation;
            assert!(
                call(
                    &journal,
                    "new",
                    "temporary_resolve",
                    &other_name,
                    true,
                    &mut node
                )
                .is_err()
            );
        }
        node.observation = absent;
        assert!(
            call(
                &journal,
                "new",
                "temporary_resolve",
                &other_name,
                true,
                &mut node
            )
            .is_ok()
        );
    }
}
