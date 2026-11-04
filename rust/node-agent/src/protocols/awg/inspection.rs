//! An AWG peer in wg0.conf, its revocation archive and the live interface.
use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

use super::Node;
use super::peers::{Archive, fields, fingerprint, peer_blocks, split_peer};
use crate::protocols::inspection::Inspection;
use crate::protocols::lock_config;

pub(crate) fn inspect(
    node: &Node,
    profile: &str,
    allow_unused: bool,
) -> Result<Inspection, String> {
    let _lock = lock_config(&node.config)?;
    let text = fs::read_to_string(&node.config).map_err(|err| err.to_string())?;
    compare(
        &text,
        &node.clients,
        &node.peer_name(profile),
        allow_unused,
        || node.wg_show("dump"),
    )
}

fn allowed_ips(value: &str) -> BTreeSet<&str> {
    value.split(',').map(str::trim).collect()
}

/// Compares the peer with `wg show dump`. A peer without a recorded key is
/// reported absent only with `allow_unused` and only when every live peer
/// belongs to another disk peer and nothing of it is left in `clients`.
fn compare(
    text: &str,
    clients: &Path,
    name: &str,
    allow_unused: bool,
    dump: impl FnOnce() -> Result<String, String>,
) -> Result<Inspection, String> {
    let comment = format!("# {name}");
    if text.lines().filter(|line| line.trim() == comment).count() > 1 {
        return Err("duplicate peer".into());
    }
    let (_, block) = split_peer(text, name);
    let mut data = fields(&block);
    if data.is_empty() {
        let archive = clients.join(format!("{name}.revoked.json"));
        if archive.exists() {
            let record: Archive =
                serde_json::from_str(&fs::read_to_string(&archive).map_err(|err| err.to_string())?)
                    .map_err(|err| err.to_string())?;
            if record.server != fingerprint(text)? {
                return Err("archived identity belongs to another server".into());
            }
            data = fields(&record.peer);
        }
    }
    let key = data.get("PublicKey").filter(|key| !key.is_empty());
    if key.is_none() && !allow_unused {
        // Ordinary inspection needs a recorded identity, never the name alone.
        return Err("peer identity unavailable".into());
    }
    let output = dump()?;
    let mut lines = output.lines();
    if lines.next().is_none_or(|line| line.split('\t').count() < 4) {
        return Err("invalid live interface response".into());
    }
    let rows: Vec<Vec<&str>> = lines.map(|line| line.split('\t').collect()).collect();
    let Some(key) = key else {
        // First issuance can fail before a key exists. Any orphan, malformed
        // peer or client residue leaves absence unconfirmed.
        let keys: Vec<Option<String>> = peer_blocks(text)
            .map(|part| fields(part).remove("PublicKey"))
            .collect();
        let disk: BTreeSet<&str> = keys.iter().flatten().map(String::as_str).collect();
        let live: BTreeSet<&str> = rows.iter().map(|row| row[0]).collect();
        let residue = fs::read_dir(clients).map(|entries| {
            entries.flatten().any(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with(&format!("{name}."))
            })
        });
        if !block.is_empty()
            || !clients.is_dir()
            || residue.unwrap_or(true)
            || keys
                .iter()
                .any(|key| key.as_deref().is_none_or(str::is_empty))
            || disk.len() != keys.len()
            || rows.iter().any(|row| row.len() < 8)
            || live.len() != rows.len()
            || live != disk
        {
            return Err("unused peer absence is unconfirmed".into());
        }
        return Ok(Inspection::default());
    };
    let found: Vec<&Vec<&str>> = rows.iter().filter(|row| row[0] == key).collect();
    if found.len() > 1 || rows.iter().any(|row| row.len() < 8) {
        return Err("invalid live peer response".into());
    }
    let actual = found.first();
    let matches = !block.is_empty()
        && actual.is_some_and(|row| {
            data.get("PresharedKey").map(String::as_str) == Some(row[1])
                && allowed_ips(row[3])
                    == allowed_ips(data.get("AllowedIPs").map_or("", String::as_str))
        });
    Ok(Inspection {
        disk_present: !block.is_empty(),
        live_present: actual.is_some(),
        identity_matches: matches,
        config_available: matches && clients.join(format!("{name}.txt")).is_file(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocols::test_dir;

    const PEER: &str = "# node-p_test\n[Peer]\nPublicKey = public\nPresharedKey = secret\nAllowedIPs = 10.0.0.2/32, fd42:6e70:6177:2::2/128\n";
    const INTERFACE: &str = "server\tpublic\t51820\t0\n";

    fn ok(text: &str) -> impl FnOnce() -> Result<String, String> {
        let text = text.to_string();
        move || Ok(text)
    }

    #[test]
    fn a_live_peer_matches_by_key_preshared_key_and_allowed_ips() {
        let clients = test_dir("awg-inspect-live");
        let text = format!("[Interface]\nPrivateKey = server\n\n{PEER}");
        fs::write(clients.join("node-p_test.txt"), "private client config").unwrap();
        let dump = format!(
            "{INTERFACE}public\tsecret\t(none)\t10.0.0.2/32,fd42:6e70:6177:2::2/128\t0\t0\t0\t25\n"
        );
        let found = compare(&text, &clients, "node-p_test", false, ok(&dump)).unwrap();
        assert!(found.identity_matches && found.config_available);
        let wrong = dump.replace("public\tsecret", "public\twrong");
        let found = compare(&text, &clients, "node-p_test", false, ok(&wrong)).unwrap();
        assert!(found.disk_present && found.live_present && !found.identity_matches);
    }

    #[test]
    fn an_unknown_identity_is_not_absence() {
        let clients = test_dir("awg-inspect-unknown");
        let text = "[Interface]\nPrivateKey = server\n";
        assert!(compare(text, &clients, "missing", false, ok(INTERFACE)).is_err());
        let duplicate = format!("{text}\n{PEER}\n{PEER}");
        assert!(compare(&duplicate, &clients, "node-p_test", false, ok(INTERFACE)).is_err());
    }

    #[test]
    fn a_revoked_archive_proves_live_removal() {
        let clients = test_dir("awg-inspect-archive");
        let text = "[Interface]\nPrivateKey = server\n";
        let archive = serde_json::json!({"server": fingerprint(text).unwrap(), "peer": PEER});
        fs::write(
            clients.join("node-p_test.revoked.json"),
            archive.to_string(),
        )
        .unwrap();
        let found = compare(text, &clients, "node-p_test", false, ok(INTERFACE)).unwrap();
        assert_eq!(found, Inspection::default());
        let other = serde_json::json!({"server": "other", "peer": PEER});
        fs::write(clients.join("node-p_test.revoked.json"), other.to_string()).unwrap();
        assert!(compare(text, &clients, "node-p_test", false, ok(INTERFACE)).is_err());
    }

    #[test]
    fn an_unused_peer_is_absent_only_when_every_live_key_is_accounted_for() {
        let clients = test_dir("awg-inspect-unused");
        let text = "[Interface]\nPrivateKey = server\n\n# sibling\n[Peer]\nPublicKey = sibling-key\nAllowedIPs = 10.0.0.2/32\n";
        let dump = format!("{INTERFACE}sibling-key\tsecret\t(none)\t10.0.0.2/32\t0\t0\t0\t25\n");
        assert_eq!(
            compare(text, &clients, "missing", true, ok(&dump)).unwrap(),
            Inspection::default()
        );
        let orphan = format!("{dump}orphan\tsecret\t(none)\t10.0.0.3/32\t0\t0\t0\t25\n");
        assert!(compare(text, &clients, "missing", true, ok(&orphan)).is_err());
        fs::write(clients.join("missing.txt"), "private residue").unwrap();
        assert!(compare(text, &clients, "missing", true, ok(&dump)).is_err());
        let failed = || Err("docker exec failed".to_string());
        assert!(compare(text, &test_dir("awg-inspect-rpc"), "missing", true, failed).is_err());
    }
}
