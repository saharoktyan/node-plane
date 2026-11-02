//! Durable agent-owned state for enforcement modules.
//!
//! Enforcement must survive agent and Xray restarts without the controller
//! (VPN plan, section 2, rule 4). Each module keeps one JSON document; writes
//! replace it atomically, so a crash leaves either the old or the new state.
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use serde::Serialize;
use serde::de::DeserializeOwned;

pub(crate) struct Journal {
    path: PathBuf,
}

impl Journal {
    pub(crate) fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    /// The stored state, or the default when nothing was written yet.
    /// A damaged document is an error: silently starting empty would drop
    /// active blocks.
    pub(crate) fn load<T: DeserializeOwned + Default>(&self) -> io::Result<T> {
        match fs::read(&self.path) {
            Ok(raw) => serde_json::from_slice(&raw)
                .map_err(|err| io::Error::new(io::ErrorKind::InvalidData, err)),
            Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(T::default()),
            Err(err) => Err(err),
        }
    }

    pub(crate) fn save<T: Serialize>(&self, value: &T) -> io::Result<()> {
        let raw = serde_json::to_vec(value).map_err(io::Error::other)?;
        let dir = self.path.parent().unwrap_or(Path::new("."));
        fs::create_dir_all(dir)?;
        let tmp = self.path.with_extension("tmp");
        {
            let mut file = fs::File::create(&tmp)?;
            file.write_all(&raw)?;
            file.sync_all()?;
        }
        fs::rename(&tmp, &self.path)?;
        // Make the rename itself durable.
        fs::File::open(dir)?.sync_all()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn dir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "np-journal-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = fs::remove_dir_all(&dir);
        dir
    }

    #[test]
    fn missing_state_is_default_and_saved_state_round_trips() {
        let dir = dir();
        let journal = Journal::new(dir.join("state.json"));
        assert!(journal.load::<BTreeMap<String, u64>>().unwrap().is_empty());
        let value = BTreeMap::from([("a".to_string(), 1u64)]);
        journal.save(&value).unwrap();
        assert_eq!(journal.load::<BTreeMap<String, u64>>().unwrap(), value);
        assert!(!dir.join("state.tmp").exists());
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn damaged_state_is_an_error_not_an_empty_start() {
        let dir = dir();
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("state.json"), b"{").unwrap();
        let journal = Journal::new(dir.join("state.json"));
        assert!(journal.load::<BTreeMap<String, u64>>().is_err());
        fs::remove_dir_all(dir).unwrap();
    }
}
