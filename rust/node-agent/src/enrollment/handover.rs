//! Who had the agent before this enrollment, and the handover of its
//! journal: the agent side of `archive_agent_journals.py` for token installs.
//!
//! The agent reports the previous node, controller and CA in its challenge;
//! the controller refuses to take an agent from another active node of its
//! own. On completion a journal of another controller is archived, with its
//! fences, and never replayed by the new one.
use std::fs;
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

use crate::intents::{Lock, sidecar};

/// What the agent knew before the installer ran.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub(crate) struct Previous {
    /// The node key the installer replaced.
    pub(crate) node_key: String,
    /// The controller installation that last enrolled the agent.
    pub(crate) controller: String,
    /// SHA-256 of the CA the agent trusted, hex.
    pub(crate) ca_sha256: String,
}

fn uuid(text: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.len() == 36
        && bytes.iter().enumerate().all(|(index, c)| match index {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_digit() || (b'a'..=b'f').contains(c),
        })
}

pub(crate) fn marker(state: &Path) -> PathBuf {
    state.join("controller-installation-id")
}

pub(crate) fn previous(previous_node: &Path, ca: &Path, state: &Path) -> Previous {
    let read = |path: &Path| {
        fs::read_to_string(path)
            .map(|text| text.trim().to_string())
            .unwrap_or_default()
    };
    let controller = read(&marker(state));
    Previous {
        node_key: read(previous_node),
        controller: if uuid(&controller) {
            controller
        } else {
            String::new()
        },
        ca_sha256: fs::read(ca)
            .map(|der| crate::protocols::sha256_hex(&der))
            .unwrap_or_default(),
    }
}

impl Previous {
    /// Whether `controller` with `ca_sha256` is the one that had the agent.
    /// Agents of releases before the marker are matched by their CA.
    pub(crate) fn same_controller(&self, controller: &str, ca_sha256: &str) -> bool {
        if self.controller.is_empty() {
            !self.ca_sha256.is_empty() && self.ca_sha256 == ca_sha256
        } else {
            self.controller == controller
        }
    }
}

const SUFFIXES: [&str; 6] = ["", "-wal", "-shm", "-journal", ".lock", ".disabled"];

/// Moves `from` to `to`, across file systems when it must.
fn relocate(from: &Path, to: &Path) -> std::io::Result<()> {
    match fs::rename(from, to) {
        Err(err) if err.raw_os_error() == Some(libc::EXDEV) => {
            fs::copy(from, to)?;
            fs::File::open(to)?.sync_all()?;
            fs::remove_file(from)
        }
        other => other,
    }
}

/// Archives the journal unless `controller` is the one that wrote it, then
/// records `controller`. Returns the archive.
pub(crate) fn hand_over(
    previous: &Previous,
    controller: &str,
    ca_sha256: &str,
    node_key: &str,
    journal: &Path,
    state: &Path,
    stamp: &str,
) -> Result<Option<PathBuf>, String> {
    if !uuid(controller) {
        return Err("invalid controller identity".into());
    }
    let present = |journal: &Path| {
        SUFFIXES
            .iter()
            .map(|suffix| sidecar(journal, suffix))
            .filter(|path| path.exists())
            .collect::<Vec<_>>()
    };
    let mut archive = None;
    if !present(journal).is_empty() && !previous.same_controller(controller, ca_sha256) {
        if previous.controller.is_empty() && previous.ca_sha256.is_empty() {
            return Err(
                "cannot identify the journal's owner; refusing to discard its fences".into(),
            );
        }
        // Lease commands of the old installation finish first.
        let _lock = Lock::exclusive(journal)?;
        let files = present(journal);
        if files
            .iter()
            .any(|path| !path.is_file() || path.is_symlink())
        {
            return Err("agent journal artifact is not a regular file".into());
        }
        let archives = state.join("journal-archives");
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&archives)
            .map_err(|err| err.to_string())?;
        let owner = if previous.controller.is_empty() {
            "legacy"
        } else {
            &previous.controller
        };
        let mut suffix = [0u8; 4];
        ring::rand::SecureRandom::fill(&ring::rand::SystemRandom::new(), &mut suffix)
            .map_err(|_| "no randomness")?;
        let suffix: String = suffix.iter().map(|byte| format!("{byte:02x}")).collect();
        let destination = archives.join(format!("previous-controller-{owner}-{stamp}-{suffix}"));
        fs::DirBuilder::new()
            .mode(0o700)
            .create(&destination)
            .map_err(|err| err.to_string())?;
        let manifest = serde_json::json!({
            "previous_controller": (!previous.controller.is_empty()).then_some(&previous.controller),
            "new_controller": controller,
            "previous_node": (!previous.node_key.is_empty()).then_some(&previous.node_key),
            "new_node": node_key,
            "archived_at": stamp,
            "files": files.iter().map(|path| path.display().to_string()).collect::<Vec<_>>(),
        });
        write_private(
            &destination.join("manifest.json"),
            format!("{manifest:#}\n").as_bytes(),
        )?;
        for path in &files {
            let target = destination.join(path.file_name().expect("journal files have names"));
            relocate(path, &target).map_err(|err| format!("{}: {err}", path.display()))?;
        }
        archive = Some(destination);
    }
    let marker = marker(state);
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(state)
        .map_err(|err| err.to_string())?;
    let temporary = marker.with_extension("tmp");
    let _ = fs::remove_file(&temporary);
    write_private(&temporary, format!("{controller}\n").as_bytes())?;
    fs::rename(&temporary, &marker).map_err(|err| err.to_string())?;
    Ok(archive)
}

fn write_private(path: &Path, data: &[u8]) -> Result<(), String> {
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .map_err(|err| format!("{}: {err}", path.display()))?;
    file.write_all(data)
        .and_then(|()| file.sync_all())
        .map_err(|err| err.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    const OLD: &str = "11111111-1111-4111-8111-111111111111";
    const NEW: &str = "22222222-2222-4222-8222-222222222222";

    fn journal(dir: &Path) -> PathBuf {
        let journal = dir.join("etc/profile-intents.sqlite3");
        fs::create_dir_all(journal.parent().unwrap()).unwrap();
        fs::write(&journal, "fences").unwrap();
        fs::write(sidecar(&journal, "-wal"), "wal").unwrap();
        journal
    }

    #[test]
    fn the_previous_owner_is_read_from_the_node() {
        let dir = crate::protocols::test_dir("handover-previous");
        let state = dir.join("state");
        fs::create_dir_all(&state).unwrap();
        assert_eq!(
            previous(&dir.join("none"), &dir.join("none"), &state),
            Previous::default()
        );
        fs::write(dir.join("previous-node"), "n0\n").unwrap();
        fs::write(dir.join("ca.crt"), "ca").unwrap();
        fs::write(marker(&state), format!("{OLD}\n")).unwrap();
        let found = previous(&dir.join("previous-node"), &dir.join("ca.crt"), &state);
        assert_eq!(found.node_key, "n0");
        assert_eq!(found.controller, OLD);
        assert_eq!(found.ca_sha256, crate::protocols::sha256_hex(b"ca"));
        fs::write(marker(&state), "not-a-uuid").unwrap();
        assert_eq!(
            previous(&dir.join("none"), &dir.join("none"), &state).controller,
            ""
        );
    }

    #[test]
    fn the_same_controller_keeps_its_journal() {
        let dir = crate::protocols::test_dir("handover-same");
        let journal = journal(&dir);
        let state = dir.join("state");
        let previous = Previous {
            controller: NEW.into(),
            ..Previous::default()
        };
        assert_eq!(
            hand_over(&previous, NEW, "ca", "n1", &journal, &state, "t").unwrap(),
            None
        );
        assert!(journal.exists());
        assert_eq!(
            fs::read_to_string(marker(&state)).unwrap(),
            format!("{NEW}\n")
        );
        // An agent from before the marker is recognised by its CA.
        let legacy = Previous {
            ca_sha256: "ca".into(),
            ..Previous::default()
        };
        assert_eq!(
            hand_over(&legacy, NEW, "ca", "n1", &journal, &state, "t").unwrap(),
            None
        );
    }

    #[test]
    fn another_controllers_journal_is_archived_with_its_fences() {
        let dir = crate::protocols::test_dir("handover-other");
        let journal = journal(&dir);
        let state = dir.join("state");
        let previous = Previous {
            node_key: "n0".into(),
            controller: OLD.into(),
            ca_sha256: "old".into(),
        };
        let archive = hand_over(
            &previous,
            NEW,
            "new",
            "n1",
            &journal,
            &state,
            "20261104T000000Z",
        )
        .unwrap()
        .unwrap();
        assert!(!journal.exists() && !sidecar(&journal, "-wal").exists());
        assert_eq!(
            fs::read_to_string(archive.join("profile-intents.sqlite3")).unwrap(),
            "fences"
        );
        let name = archive.file_name().unwrap().to_string_lossy().to_string();
        assert!(
            name.starts_with(&format!("previous-controller-{OLD}-20261104T000000Z-")),
            "{name}"
        );
        let manifest: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(archive.join("manifest.json")).unwrap())
                .unwrap();
        assert_eq!(manifest["previous_node"], "n0");
        assert_eq!(manifest["new_controller"], NEW);
        assert_eq!(
            fs::read_to_string(marker(&state)).unwrap(),
            format!("{NEW}\n")
        );
    }

    #[test]
    fn an_unattributable_journal_is_never_discarded() {
        let dir = crate::protocols::test_dir("handover-unknown");
        let journal = journal(&dir);
        let state = dir.join("state");
        let error = hand_over(
            &Previous::default(),
            NEW,
            "new",
            "n1",
            &journal,
            &state,
            "t",
        )
        .unwrap_err();
        assert!(error.contains("refusing"), "{error}");
        assert!(journal.exists());
        assert!(!marker(&state).exists());
        assert!(
            hand_over(
                &Previous::default(),
                "nope",
                "new",
                "n1",
                &journal,
                &state,
                "t"
            )
            .is_err()
        );
    }
}
