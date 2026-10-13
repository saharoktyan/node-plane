//! Private journals live apart from user preferences and SSH credentials.
use crate::ssh;
use anyhow::{Result, ensure};
use fs2::FileExt;
use std::{
    fs,
    fs::OpenOptions,
    path::{Path, PathBuf},
};

pub fn directory(state: &Path, name: &str) -> Result<PathBuf> {
    ssh::secure_state_directory(state)?;
    let destination = state.join(name);
    ssh::secure_state_directory(&destination)?;
    let lock_path = state.join("state-layout.lock");
    let mut options = OpenOptions::new();
    options.create(true).truncate(false).read(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    if lock_path.exists() {
        ssh::secure_file(&lock_path)?;
    }
    let lock = options.open(&lock_path)?;
    ssh::secure_file(&lock_path)?;
    lock.lock_exclusive()?;
    for entry in fs::read_dir(state)? {
        let source = entry?.path();
        let Some(filename) = source.file_name().and_then(|s| s.to_str()) else {
            continue;
        };
        let matches = match name {
            "operations" => {
                filename.ends_with(".json")
                    && [
                        "update-",
                        "node-operation-",
                        "node-creation-",
                        "backup-command-",
                        "controller-removal-",
                    ]
                    .iter()
                    .any(|prefix| filename.starts_with(prefix))
            }
            "logs" => filename.starts_with("operation-") && filename.ends_with(".log"),
            _ => false,
        };
        if !matches {
            continue;
        }
        ssh::secure_file(&source)?;
        let target = destination.join(filename);
        if target.try_exists()? {
            ssh::secure_file(&target)?;
            ensure!(
                fs::read(&source)? == fs::read(&target)?,
                "Conflicting saved journal at {}. Both copies were preserved.",
                target.display()
            );
        } else {
            // The private directories and migration lock exclude competing
            // migrations. Rename publishes a complete file without creating
            // hard links rejected by the private-file security checks.
            fs::rename(&source, &target)?;
            #[cfg(unix)]
            fs::File::open(&destination)?.sync_all()?;
            continue;
        }
        fs::remove_file(&source)?;
    }
    #[cfg(unix)]
    fs::File::open(state)?.sync_all()?;
    FileExt::unlock(&lock)?;
    Ok(destination)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn migrates_journals_without_moving_preferences_and_preserves_conflicts() {
        let root = tempfile::tempdir().unwrap();
        let state = root.path().join("state");
        ssh::secure_state_directory(&state).unwrap();
        for name in [
            "update-one.json",
            "node-creation-two.json",
            "node-operation-three.json",
            "installations.json",
            "operation-four.log",
        ] {
            fs::write(state.join(name), name).unwrap();
        }
        let operations = directory(&state, "operations").unwrap();
        assert!(operations.join("update-one.json").exists());
        assert!(operations.join("node-creation-two.json").exists());
        assert!(state.join("installations.json").exists());
        assert!(!state.join("update-one.json").exists());
        assert!(
            directory(&state, "logs")
                .unwrap()
                .join("operation-four.log")
                .exists()
        );
        fs::write(state.join("update-one.json"), "different").unwrap();
        assert!(directory(&state, "operations").is_err());
        assert_eq!(
            fs::read_to_string(operations.join("update-one.json")).unwrap(),
            "update-one.json"
        );
        assert_eq!(
            fs::read_to_string(state.join("update-one.json")).unwrap(),
            "different"
        );
    }
}
