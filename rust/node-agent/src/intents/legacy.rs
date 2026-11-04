//! Mutations the backend did not journal: old profile commands and runtime
//! maintenance. They take the journal lock like every command, and are
//! refused once the backend owns their target.
use std::path::Path;

use super::profile::run_locked;
use super::{Lock, disabled, open_readonly};

/// What a legacy mutation would touch: a profile, or the whole node.
#[derive(Clone, Copy)]
pub(crate) enum Target<'a> {
    Node,
    Profile { protocol: &'a str, profile: &'a str },
}

fn profile_name(profile: &str) -> bool {
    !profile.is_empty() && profile.len() <= 128 && !profile.contains('\0')
}

/// Refuses a target the backend owns. No journal means a node without the
/// backend; a journal that cannot be read may hold a fence, so it refuses.
pub(crate) fn guard(journal: &Path, target: Target<'_>) -> Result<(), String> {
    if disabled(journal) {
        return Err("agent removal is in progress".into());
    }
    if !journal.exists() {
        return Ok(());
    }
    let connection = open_readonly(journal)?;
    let exists = |sql: &str, params: &[&dyn rusqlite::ToSql]| -> Result<bool, String> {
        let mut statement = connection.prepare(sql).map_err(super::database)?;
        statement.exists(params).map_err(super::database)
    };
    let owned = match target {
        Target::Node => {
            exists("SELECT 1 FROM fences LIMIT 1", &[])?
                || (super::has_table(&connection, "node_settings_fence")?
                    && exists("SELECT 1 FROM node_settings_fence LIMIT 1", &[])?)
        }
        Target::Profile { protocol, profile } => {
            if !matches!(protocol, "awg" | "xray") || !profile_name(profile) {
                return Err("invalid legacy target".into());
            }
            exists(
                "SELECT 1 FROM fences WHERE protocol = ? AND profile = ?",
                &[&protocol, &profile],
            )?
        }
    };
    if owned {
        return Err("target is managed by the explicit backend".into());
    }
    Ok(())
}

/// Profile entry points and the protocol each changes.
const PROFILE_SCRIPTS: [(&str, &str); 4] = [
    ("xray-add-user-existing.sh", "xray"),
    ("xray-del-user.sh", "xray"),
    ("awg-add-user.sh", "awg"),
    ("awg-del-user.sh", "awg"),
];

/// Entry points that change the whole node.
pub(crate) const MAINTENANCE_SCRIPTS: [&str; 7] = [
    "init-xray.sh",
    "deploy-xray.sh",
    "init-awg.sh",
    "deploy-awg.sh",
    "apply-node-settings.sh",
    "regenerate-awg-entropy.sh",
    "sync-xray.sh",
];

/// The journal scope of an entry point: its protocol, `all`, or none.
pub(crate) fn scope(script: &str) -> Option<&'static str> {
    PROFILE_SCRIPTS
        .iter()
        .find(|(name, _)| *name == script)
        .map(|(_, protocol)| *protocol)
        .or_else(|| MAINTENANCE_SCRIPTS.contains(&script).then_some("all"))
}

/// Runs a legacy entry point of `runtime` under the journal lock, after its
/// fence check; its stdout.
pub(crate) fn run(
    journal: &Path,
    runtime: &Path,
    script: &str,
    args: &[String],
    instance: &str,
) -> Result<String, String> {
    let target = match scope(script) {
        Some("all") => Target::Node,
        Some(protocol) => {
            let profile = args.first().map(String::as_str).unwrap_or_default();
            if !profile_name(profile) {
                return Err("invalid profile command".into());
            }
            Target::Profile { protocol, profile }
        }
        None => return Err("invalid legacy command".into()),
    };
    let lock = Lock::exclusive(journal)?;
    guard(journal, target)?;
    run_locked(&runtime.join(script), args, lock.fd(), instance)
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::sync::Mutex;

    use super::*;
    use crate::intents::profile::{Intent, apply};
    use crate::intents::{Response, test_journal};

    fn intent(protocol: &str) -> Intent {
        let xray = protocol == "xray";
        Intent {
            command_id: "task-1".into(),
            protocol: protocol.into(),
            runtime_name: "p_test".into(),
            revision: 1,
            action: "ensure".into(),
            uuid: if xray {
                "11111111-1111-4111-8111-111111111111".into()
            } else {
                String::new()
            },
            short_id: if xray {
                "0123456789abcdef".into()
            } else {
                String::new()
            },
            lease_seconds: None,
        }
    }

    fn script(dir: &Path, name: &str, body: &str) {
        let path = dir.join(name);
        fs::write(&path, format!("#!/bin/sh\n{body}")).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
    }

    fn ok(_: &Intent, _: std::os::fd::RawFd) -> Result<Response, String> {
        Ok(Response::ok())
    }

    #[test]
    fn legacy_profile_commands_obey_the_fence_and_other_profiles_continue() {
        let journal = test_journal("legacy-profile");
        let runtime = journal.parent().unwrap().join("runtime");
        fs::create_dir_all(&runtime).unwrap();
        let log = runtime.join("invocations");
        script(
            &runtime,
            "xray-del-user.sh",
            &format!("printf '%s\\n' \"$1\" >> '{}'\necho OK\n", log.display()),
        );
        let args = |name: &str| vec![name.to_string()];
        assert_eq!(
            run(
                &journal,
                &runtime,
                "xray-del-user.sh",
                &args("old.name"),
                "agent"
            )
            .unwrap(),
            "OK"
        );
        apply(&intent("xray"), &journal, "agent", &mut ok).unwrap();
        assert!(
            run(
                &journal,
                &runtime,
                "xray-del-user.sh",
                &args("p_test"),
                "agent"
            )
            .is_err()
        );
        assert_eq!(
            run(
                &journal,
                &runtime,
                "xray-del-user.sh",
                &args("other.name"),
                "agent"
            )
            .unwrap(),
            "OK"
        );
        assert_eq!(fs::read_to_string(&log).unwrap(), "old.name\nother.name\n");
        assert!(run(&journal, &runtime, "unknown.sh", &args("x"), "agent").is_err());
    }

    #[test]
    fn maintenance_and_the_removal_marker_fail_closed() {
        let journal = test_journal("legacy-maintenance");
        guard(&journal, Target::Node).unwrap();
        apply(&intent("awg"), &journal, "agent", &mut ok).unwrap();
        assert!(guard(&journal, Target::Node).is_err());
        assert!(
            guard(
                &journal,
                Target::Profile {
                    protocol: "awg",
                    profile: "p_test"
                }
            )
            .is_err()
        );
        let runtime = journal.parent().unwrap();
        assert!(run(&journal, runtime, "deploy-awg.sh", &[], "agent").is_err());
        fs::write(crate::intents::sidecar(&journal, ".disabled"), "removal").unwrap();
        let later = Intent {
            command_id: "later".into(),
            revision: 2,
            ..intent("awg")
        };
        assert!(apply(&later, &journal, "agent", &mut ok).is_err());
        assert!(
            guard(
                &journal,
                Target::Profile {
                    protocol: "xray",
                    profile: "other"
                }
            )
            .is_err()
        );
    }

    #[test]
    fn a_node_settings_fence_refuses_maintenance() {
        let journal = test_journal("legacy-settings");
        let connection = rusqlite::Connection::open(&journal).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE fences (protocol TEXT, profile TEXT, revision INTEGER NOT NULL, command_id TEXT NOT NULL, action TEXT, PRIMARY KEY(protocol, profile));
                 CREATE TABLE node_settings_fence (node_key TEXT PRIMARY KEY, revision INTEGER NOT NULL, command_id TEXT NOT NULL);",
            )
            .unwrap();
        guard(&journal, Target::Node).unwrap();
        connection
            .execute(
                "INSERT INTO node_settings_fence VALUES ('n1', 1, 'task')",
                [],
            )
            .unwrap();
        assert!(guard(&journal, Target::Node).is_err());
    }

    #[test]
    fn an_unreadable_journal_refuses_legacy_mutations() {
        let journal = test_journal("legacy-corrupt");
        fs::write(&journal, "corrupt").unwrap();
        assert!(
            guard(
                &journal,
                Target::Profile {
                    protocol: "awg",
                    profile: "p_test"
                }
            )
            .is_err()
        );
    }

    #[test]
    fn the_running_legacy_command_finishes_before_a_new_fence() {
        let journal = test_journal("legacy-order");
        let runtime = journal.parent().unwrap().to_path_buf();
        let (entered, release) = (runtime.join("entered"), runtime.join("release"));
        script(
            &runtime,
            "awg-del-user.sh",
            &format!(
                "touch '{}'\nwhile [ ! -f '{}' ]; do sleep 0.01; done\necho OK\n",
                entered.display(),
                release.display()
            ),
        );
        let old = {
            let (journal, runtime) = (journal.clone(), runtime.clone());
            std::thread::spawn(move || {
                run(
                    &journal,
                    &runtime,
                    "awg-del-user.sh",
                    &["p_test".to_string()],
                    "agent",
                )
            })
        };
        while !entered.exists() {
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        let order = Mutex::new(Vec::new());
        let newer = std::thread::scope(|scope| {
            let handle = scope.spawn(|| {
                apply(&intent("awg"), &journal, "agent", &mut |_, _| {
                    order.lock().unwrap().push(release.exists());
                    Ok(Response::ok())
                })
            });
            std::thread::sleep(std::time::Duration::from_millis(100));
            fs::write(&release, "").unwrap();
            handle.join().unwrap()
        });
        assert_eq!(old.join().unwrap().unwrap(), "OK");
        newer.unwrap();
        assert_eq!(
            *order.lock().unwrap(),
            [true],
            "the new command ran after the old one"
        );
        assert!(
            run(
                &journal,
                &runtime,
                "awg-del-user.sh",
                &["p_test".to_string()],
                "agent"
            )
            .is_err()
        );
    }
}
