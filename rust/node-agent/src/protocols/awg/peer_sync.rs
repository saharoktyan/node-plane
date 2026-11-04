//! `node-plane-agent awg-peer revoke NAME`: removes one AWG peer from
//! wg0.conf and the live interface. Replaces the revoke path of
//! `awg-del-user.sh` and `awg-peer-state.py`; the profile intent journal
//! still runs `awg-del-user.sh`, which only starts this subcommand.
//!
//! The peer is archived as `<clients>/<name>.revoked.json` and removed from
//! the file before the live interface, so a restarted container does not
//! bring it back and a retry finishes a partial revocation. Re-enabling the
//! same identity restores it from the archive (`awg-add-user.sh`).
use std::fs;
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::path::Path;
use std::process::Command;

use super::Node;
use super::peers::{Archive, fields, fingerprint, split_peer};
use crate::protocols::{Setting, lock_config, process_setting, valid_name};

/// Writes `text` to `path` through a private temporary file in the same
/// directory, durable once this returns.
pub(crate) fn atomic_write(path: &Path, text: &str) -> Result<(), String> {
    let dir = path.parent().unwrap_or(Path::new("."));
    let failed = |err: std::io::Error| format!("{} write failed: {err}", path.display());
    let candidate = dir.join(format!(".peer-{}", std::process::id()));
    let result = (|| {
        let mut file = fs::File::create(&candidate)?;
        file.set_permissions(fs::Permissions::from_mode(0o600))?;
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

/// Removes the peer's key from the live interface.
pub(crate) trait Interface {
    fn remove(&self, key: &str) -> Result<(), String>;
}

impl Interface for Node {
    fn remove(&self, key: &str) -> Result<(), String> {
        let output = Command::new("docker")
            .args([
                "exec",
                "-i",
                &self.container,
                "wg",
                "set",
                &self.interface,
                "peer",
                key,
                "remove",
            ])
            .output()
            .map_err(|err| format!("docker failed: {err}"))?;
        if !output.status.success() {
            return Err(format!(
                "wg set failed: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
        Ok(())
    }
}

/// Revokes `name`; the caller holds the config lock. Nothing to do when
/// neither the config nor an archive knows the peer.
pub(crate) fn revoke(
    config: &Path,
    clients: &Path,
    name: &str,
    interface: &impl Interface,
) -> Result<(), String> {
    let text = fs::read_to_string(config).map_err(|err| err.to_string())?;
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(clients)
        .map_err(|err| err.to_string())?;
    let archive = clients.join(format!("{name}.revoked.json"));
    let (current, peer) = split_peer(&text, name);
    let data = if !peer.is_empty() {
        let record = serde_json::json!({"server": fingerprint(&text)?, "peer": peer});
        atomic_write(&archive, &record.to_string())?;
        // Persist the revocation before changing the running interface.
        atomic_write(config, &current)?;
        fields(&peer)
    } else if archive.exists() {
        let record: Archive =
            serde_json::from_str(&fs::read_to_string(&archive).map_err(|err| err.to_string())?)
                .map_err(|err| err.to_string())?;
        fields(&record.peer)
    } else {
        return Ok(());
    };
    let key = data
        .get("PublicKey")
        .ok_or("revoked peer has no public key")?;
    interface.remove(key)
}

fn running(container: &str) -> Result<bool, String> {
    let output = Command::new("docker")
        .args(["ps", "--format", "{{.Names}}"])
        .output()
        .map_err(|err| format!("docker failed: {err}"))?;
    if !output.status.success() {
        return Err("Docker is not available".into());
    }
    Ok(String::from_utf8_lossy(&output.stdout)
        .lines()
        .any(|line| line == container))
}

fn execute(setting: Setting<'_>, args: &[String]) -> Result<(), String> {
    let [command, name] = args else {
        return Err("usage: awg-peer revoke NAME".into());
    };
    if command != "revoke" {
        return Err("usage: awg-peer revoke NAME".into());
    }
    let node = Node::read(setting);
    let peer = node.peer_name(name);
    if !valid_name(name)
        || !peer
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b'-'))
    {
        return Err("Invalid AWG profile name".into());
    }
    if !running(&node.container)? {
        return Err(format!("Container {} not found", node.container));
    }
    let _lock = lock_config(&node.config)?;
    revoke(&node.config, &node.clients, &peer, &node)
}

/// Runs the subcommand when the agent was started as `awg-peer …`.
pub(crate) fn run(args: &[String]) -> Option<i32> {
    let (first, rest) = args.split_first()?;
    if first != "awg-peer" {
        return None;
    }
    Some(match execute(&process_setting, rest) {
        Ok(()) => {
            println!("OK");
            0
        }
        Err(err) => {
            eprintln!("AWG peer change failed: {err}");
            1
        }
    })
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use super::*;
    use crate::protocols::test_dir;

    #[derive(Default)]
    struct Fake {
        removed: RefCell<Vec<String>>,
        fail: bool,
    }

    impl Interface for Fake {
        fn remove(&self, key: &str) -> Result<(), String> {
            if self.fail {
                return Err("container stopped".into());
            }
            self.removed.borrow_mut().push(key.to_string());
            Ok(())
        }
    }

    const CONFIG: &str = "[Interface]\nPrivateKey = server\n\n# n1-alice\n[Peer]\nPublicKey = alice-key\nPresharedKey = secret\nAllowedIPs = 10.8.1.2/32\n\n# n1-bob\n[Peer]\nPublicKey = bob-key\n";

    #[test]
    fn revocation_archives_the_peer_before_the_live_removal_and_retries() {
        let dir = test_dir("awg-revoke");
        let (config, clients) = (dir.join("wg0.conf"), dir.join("clients"));
        fs::write(&config, CONFIG).unwrap();
        let failing = Fake {
            fail: true,
            ..Fake::default()
        };
        assert!(revoke(&config, &clients, "n1-alice", &failing).is_err());
        let text = fs::read_to_string(&config).unwrap();
        assert!(!text.contains("alice-key") && text.contains("bob-key"));
        let archive: Archive = serde_json::from_str(
            &fs::read_to_string(clients.join("n1-alice.revoked.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(archive.server, fingerprint(CONFIG).unwrap());
        assert!(
            archive
                .peer
                .starts_with("# n1-alice\n[Peer]\nPublicKey = alice-key")
        );
        // The retry finds the peer only in the archive and finishes.
        let fake = Fake::default();
        revoke(&config, &clients, "n1-alice", &fake).unwrap();
        assert_eq!(*fake.removed.borrow(), ["alice-key"]);
        let unknown = Fake::default();
        revoke(&config, &clients, "n1-carol", &unknown).unwrap();
        assert!(unknown.removed.borrow().is_empty());
        let mode = fs::metadata(&clients).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o700);
    }

    #[test]
    fn the_python_restore_reads_the_archive() {
        let dir = test_dir("awg-revoke-format");
        let (config, clients) = (dir.join("wg0.conf"), dir.join("clients"));
        fs::write(&config, CONFIG).unwrap();
        revoke(&config, &clients, "n1-alice", &Fake::default()).unwrap();
        let record: serde_json::Value = serde_json::from_str(
            &fs::read_to_string(clients.join("n1-alice.revoked.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(
            record.as_object().unwrap().keys().collect::<Vec<_>>(),
            ["peer", "server"]
        );
    }
}
