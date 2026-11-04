//! Cumulative traffic counters of one consenting profile (`traffic` node
//! operation). Replaces `traffic-snapshot.py`.
//!
//! Counters are never reset. Only the requested identity's counters and an
//! opaque epoch leave the agent; the epoch changes when the protocol
//! container restarts, and a restart during the read makes it unknown.
use std::fs;
use std::path::Path;
use std::process::Command;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::{Setting, awg, valid_name, xray};
use crate::protocols::xray::api::cli::valid_uuid;

/// Protobuf's int64 upper bound: Xray counters are signed.
const MAX_COUNTER: u64 = i64::MAX as u64;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Query {
    node_key: String,
    runtime_name: String,
    protocol: String,
    identity: String,
}

#[derive(Debug, PartialEq, Eq, Serialize)]
pub(crate) struct Snapshot {
    node_key: String,
    runtime_name: String,
    protocol: String,
    identity: String,
    epoch: String,
    uplink_bytes: u64,
    downlink_bytes: u64,
}

/// Bytes the client sent and received.
#[derive(Debug, Default, PartialEq, Eq)]
pub(crate) struct Counters {
    pub(crate) uplink: u64,
    pub(crate) downlink: u64,
}

pub(crate) fn counter(value: u64) -> Result<u64, String> {
    (value <= MAX_COUNTER)
        .then_some(value)
        .ok_or_else(|| "invalid traffic counter".into())
}

fn valid_awg_key(value: &str) -> bool {
    // 32 bytes in padded base64.
    value.len() == 44
        && value.ends_with('=')
        && value[..43]
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'+' | b'/'))
}

impl Query {
    fn validate(&self) -> Result<(), String> {
        if !valid_name(&self.runtime_name) || !valid_name(&self.node_key) {
            return Err("invalid traffic query".into());
        }
        match self.protocol.as_str() {
            "xray" if valid_uuid(&self.identity) => Ok(()),
            "xray" => Err("invalid Xray identity".into()),
            "awg" if valid_awg_key(&self.identity) => Ok(()),
            "awg" => Err("invalid AWG identity".into()),
            _ => Err("unsupported traffic protocol".into()),
        }
    }
}

fn valid_container(name: &str) -> bool {
    let bytes = name.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 128
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'.' | b'-'))
}

/// Identifies one run of the container on this boot.
fn container_epoch(container: &str) -> Result<String, String> {
    let output = Command::new("docker")
        .args(["inspect", container])
        .output()
        .map_err(|err| format!("docker failed: {err}"))?;
    if !output.status.success() {
        return Err("protocol container unavailable".into());
    }
    let records: Vec<Value> =
        serde_json::from_slice(&output.stdout).map_err(|err| err.to_string())?;
    let [record] = records.as_slice() else {
        return Err("protocol container unavailable".into());
    };
    if record["State"]["Running"] != true {
        return Err("protocol container unavailable".into());
    }
    let boot =
        fs::read_to_string("/proc/sys/kernel/random/boot_id").map_err(|err| err.to_string())?;
    Ok(epoch(boot.trim(), record))
}

/// Byte-compatible with the epochs of `traffic-snapshot.py`
/// (`json.dumps([boot_id, Id, StartedAt, Pid])`), so collection continues
/// across the agent update without a false counter reset.
fn epoch(boot: &str, record: &Value) -> String {
    let text = |value: &Value| serde_json::to_string(value).unwrap_or_default();
    super::sha256_hex(
        format!(
            "[{}, {}, {}, {}]",
            text(&Value::from(boot)),
            text(&record["Id"]),
            text(&record["State"]["StartedAt"]),
            text(&record["State"]["Pid"]),
        )
        .as_bytes(),
    )
}

pub(crate) async fn read(setting: Setting<'_>, query: Query) -> Result<Snapshot, String> {
    query.validate()?;
    let xray = xray::Node::read(setting);
    let awg = awg::Node::read(setting);
    let container = if query.protocol == "xray" {
        &xray.container
    } else {
        &awg.container
    };
    let epoch = checked_epoch(container)?;
    let counters = if query.protocol == "xray" {
        xray::traffic::counters(&xray, &query.runtime_name, &query.identity).await?
    } else {
        let (node, name, identity) = (
            awg.clone(),
            query.runtime_name.clone(),
            query.identity.clone(),
        );
        tokio::task::spawn_blocking(move || awg::traffic::counters(&node, &name, &identity))
            .await
            .map_err(|err| err.to_string())??
    };
    if checked_epoch(container)? != epoch {
        return Err("protocol restarted during traffic observation".into());
    }
    Ok(Snapshot {
        node_key: query.node_key,
        runtime_name: query.runtime_name,
        protocol: query.protocol,
        identity: query.identity,
        epoch,
        uplink_bytes: counters.uplink,
        downlink_bytes: counters.downlink,
    })
}

fn checked_epoch(container: &str) -> Result<String, String> {
    if !valid_container(container) {
        return Err("invalid protocol container".into());
    }
    container_epoch(container)
}

/// Runs a read under the journal's shared lock, as profile mutations hold
/// it exclusively. Never waits behind a bootstrap and never creates the
/// journal.
pub(crate) async fn observe(
    setting: Setting<'_>,
    journal: &Path,
    intent_json: &str,
) -> Result<Snapshot, String> {
    let query: Query = serde_json::from_str(intent_json).map_err(|_| "invalid traffic query")?;
    let mut lock = journal.as_os_str().to_owned();
    lock.push(".lock");
    let lock = fs::File::open(&lock).map_err(|err| format!("journal lock unavailable: {err}"))?;
    lock.try_lock_shared()
        .map_err(|_| "node is busy with a profile change")?;
    let mut disabled = journal.as_os_str().to_owned();
    disabled.push(".disabled");
    if Path::new(&disabled).exists() {
        return Err("agent removal is in progress".into());
    }
    read(setting, query).await
}

#[cfg(test)]
mod tests {
    use super::*;

    const UUID: &str = "00000000-0000-4000-8000-000000000001";

    fn query(changes: &[(&str, &str)]) -> Result<Query, String> {
        let mut value = serde_json::json!({
            "node_key": "n1", "runtime_name": "alice", "protocol": "xray", "identity": UUID,
        });
        for (key, change) in changes {
            value[key] = (*change).into();
        }
        serde_json::from_value::<Query>(value)
            .map_err(|err| err.to_string())
            .and_then(|query| query.validate().map(|()| query))
    }

    #[test]
    fn queries_name_exactly_one_identity() {
        assert!(query(&[]).is_ok());
        let key = "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=";
        assert!(query(&[("protocol", "awg"), ("identity", key)]).is_ok());
        for changes in [
            &[("runtime_name", "../alice")][..],
            &[("protocol", "unknown")],
            &[("identity", "not-uuid")],
            &[("identity", "00000000-0000-4000-8000-00000000000A")],
            &[("node_key", "../wrong")],
            &[("extra", "arbitrary arguments")],
            &[("protocol", "awg"), ("identity", "a2tr")],
            &[
                ("protocol", "awg"),
                ("identity", "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2t=="),
            ],
        ] {
            assert!(query(changes).is_err(), "{changes:?}");
        }
    }

    #[test]
    fn epochs_match_the_python_snapshot() {
        let record = serde_json::json!({"Id": "abc", "State": {"StartedAt": "2026-11-05T10:00:00.1Z", "Pid": 42}});
        // hashlib.sha256(json.dumps(["boot", "abc", "2026-11-05T10:00:00.1Z", 42]).encode()).hexdigest()
        assert_eq!(
            epoch("boot", &record),
            "8960b5d29094169394f59cf5c3e8eff5e891b1bcb79ef66745c01fd4b4762674"
        );
    }

    #[test]
    fn counters_exclude_values_beyond_int64() {
        assert_eq!(counter(MAX_COUNTER), Ok(MAX_COUNTER));
        assert!(counter(MAX_COUNTER + 1).is_err());
    }

    #[tokio::test]
    async fn reads_wait_for_no_mutation_and_never_create_the_journal() {
        let dir = crate::protocols::test_dir("traffic-lock");
        let journal = dir.join("journal.sqlite3");
        let setting = |_: &str, default: &str| default.to_string();
        let intent = serde_json::json!({"node_key": "n1", "runtime_name": "alice", "protocol": "xray", "identity": UUID}).to_string();
        assert!(
            observe(&setting, &journal, &intent).await.is_err(),
            "no lock file"
        );
        let lock = fs::File::create(dir.join("journal.sqlite3.lock")).unwrap();
        lock.lock().unwrap();
        let busy = observe(&setting, &journal, &intent).await.unwrap_err();
        assert!(busy.contains("busy"), "{busy}");
        lock.unlock().unwrap();
        fs::write(dir.join("journal.sqlite3.disabled"), "").unwrap();
        let removing = observe(&setting, &journal, &intent).await.unwrap_err();
        assert!(removing.contains("removal"), "{removing}");
        assert!(!journal.exists());
    }
}
