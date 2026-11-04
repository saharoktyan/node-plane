//! The decommission fence: `<journal>.disabled`, written once every profile
//! the journal knows is deleted. Afterwards no command runs on the node.
use std::fs;
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;

use rusqlite::params;
use serde_json::{Value, json};

use super::profile::{Intent, Runner};
use super::{Lock, Response, database, fences_schema, has_table, open, open_readonly, sidecar};

/// A decommission command is a lowercase hyphenated UUID.
fn command_id(value: &str) -> Result<&str, String> {
    let valid = value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| {
            if matches!(index, 8 | 13 | 18 | 23) {
                byte == b'-'
            } else {
                matches!(byte, b'0'..=b'9' | b'a'..=b'f')
            }
        });
    valid
        .then_some(value)
        .ok_or_else(|| "invalid decommission command identity".into())
}

fn record(command: &str) -> Value {
    json!({"kind": "decommission", "command_id": command})
}

/// The fence of `command`; the caller holds the journal lock.
pub(crate) fn verify(journal: &Path, command: &str) -> Result<Value, String> {
    command_id(command)?;
    let text = fs::read_to_string(sidecar(journal, ".disabled"))
        .map_err(|_| "decommission fence missing or invalid".to_string())?;
    let found: Value = serde_json::from_str(&text)
        .map_err(|_| "decommission fence missing or invalid".to_string())?;
    if found != record(command) {
        return Err("decommission command identity conflict".into());
    }
    Ok(found)
}

/// Deletes every profile the journal holds but has not deleted, under the
/// journal lock. A failure leaves its command running, never a guessed
/// success.
fn revoke_profiles(
    journal: &Path,
    command: &str,
    instance: &str,
    lock: &Lock,
    runner: &mut Runner<'_>,
) -> Result<(), String> {
    let connection = open(journal)?;
    if !has_table(&connection, "commands")? {
        return Err("invalid command journal; decommission refused".into());
    }
    let running = connection
        .prepare("SELECT 1 FROM commands WHERE status = 'running' LIMIT 1")
        .and_then(|mut statement| statement.exists([]))
        .map_err(database)?;
    if running {
        return Err("unfinished profile command blocks decommission".into());
    }
    if !has_table(&connection, "fences")? {
        return Ok(());
    }
    fences_schema(&connection)?;
    let targets: Vec<Fenced> = {
        let mut statement = connection
            .prepare(
                "SELECT f.protocol, f.profile, f.revision, f.action, c.status
            FROM fences f LEFT JOIN commands c ON c.id=f.command_id
            ORDER BY f.protocol, f.profile",
            )
            .map_err(database)?;
        statement
            .query_map([], |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                ))
            })
            .map_err(database)?
            .collect::<Result<_, _>>()
            .map_err(database)?
    };
    // Every candidate is checked before the first mutation.
    let mut intents = Vec::new();
    for (protocol, profile, revision, action, status) in targets {
        let succeeded = status.as_deref() == Some("succeeded");
        if action.as_deref() == Some("delete") && succeeded {
            continue;
        }
        if !succeeded {
            return Err("unconfirmed historical profile blocks decommission".into());
        }
        let intent = Intent {
            command_id: format!("{command}:{protocol}:{profile}"),
            protocol,
            runtime_name: profile,
            revision: revision + 1,
            action: "delete".into(),
            uuid: String::new(),
            short_id: String::new(),
            lease_seconds: None,
        };
        intent.validate()?;
        intents.push(intent);
    }
    for intent in intents {
        let transaction = connection.unchecked_transaction().map_err(database)?;
        transaction
            .execute(
                "INSERT INTO commands(id, fingerprint, status, response, instance_id)
                    VALUES (?, ?, 'running', NULL, ?)",
                params![intent.command_id, intent.fingerprint(), instance],
            )
            .map_err(database)?;
        transaction
            .execute(
                "UPDATE fences SET revision=?, command_id=?, action='delete'
                    WHERE protocol=? AND profile=?",
                params![
                    intent.revision,
                    intent.command_id,
                    intent.protocol,
                    intent.runtime_name
                ],
            )
            .map_err(database)?;
        transaction.commit().map_err(database)?;
        let response: Response = runner(&intent, lock.fd())?;
        connection
            .execute(
                "UPDATE commands SET status='succeeded', response=? WHERE id=?",
                params![response.to_json(), intent.command_id],
            )
            .map_err(database)?;
    }
    Ok(())
}

/// Fences the node for removal once every known profile is deleted; with a
/// runner it first deletes them. A journal from before deletion tracking
/// needs a fresh delete of each profile first: nothing is guessed from old
/// fences.
pub(crate) fn prepare(
    journal: &Path,
    command: &str,
    instance: &str,
    runner: Option<&mut Runner<'_>>,
) -> Result<Value, String> {
    command_id(command)?;
    let lock = Lock::exclusive(journal)?;
    let marker = sidecar(journal, ".disabled");
    if marker.exists() {
        return verify(journal, command);
    }
    if journal.exists() {
        if let Some(runner) = runner {
            revoke_profiles(journal, command, instance, &lock, runner)?;
        }
        let connection = open_readonly(journal)?;
        if !has_table(&connection, "commands")? {
            return Err("invalid command journal; decommission refused".into());
        }
        let exists = |sql: &str| {
            connection
                .prepare(sql)
                .and_then(|mut statement| statement.exists([]))
                .map_err(database)
        };
        if exists("SELECT 1 FROM commands WHERE status = 'running' LIMIT 1")? {
            return Err("unfinished profile command blocks decommission".into());
        }
        // Node operations share the journal before any profile exists: no
        // fences is no pending revocation.
        if has_table(&connection, "fences")? && exists("SELECT 1 FROM fences LIMIT 1")? {
            let tracked = connection
                .prepare("PRAGMA table_info(fences)")
                .and_then(|mut statement| {
                    statement
                        .query_map([], |row| row.get::<_, String>(1))?
                        .collect::<Result<Vec<_>, _>>()
                })
                .map_err(database)?
                .iter()
                .any(|column| column == "action");
            if !tracked {
                return Err("profile journal needs a fresh delete revision".into());
            }
            if exists(
                "SELECT 1 FROM fences f LEFT JOIN commands c ON c.id = f.command_id
                WHERE f.action IS NULL OR f.action != 'delete' OR c.status IS NULL OR c.status != 'succeeded'
                LIMIT 1",
            )? {
                return Err("managed profiles remain on the node".into());
            }
        }
    }
    let fence = record(command);
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&marker)
        .map_err(|err| format!("cannot create decommission fence: {err}"))?;
    file.write_all(format!("{fence}\n").as_bytes())
        .and_then(|()| file.sync_all())
        .and_then(|()| fs::File::open(journal.parent().unwrap_or(Path::new("/")))?.sync_all())
        .map_err(|err| format!("cannot persist decommission fence: {err}"))?;
    Ok(fence)
}

/// A fenced profile: protocol, name, revision, action and command status.
type Fenced = (String, String, i64, Option<String>, Option<String>);

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use rusqlite::Connection;

    use super::*;
    use crate::intents::profile::apply;
    use crate::intents::test_journal;

    const COMMAND: &str = "0b5e3c1a-2f4d-4e6a-8b7c-9d0e1f2a3b4c";
    const OTHER: &str = "1b5e3c1a-2f4d-4e6a-8b7c-9d0e1f2a3b4c";

    fn intent(revision: i64, command: &str, action: &str) -> Intent {
        Intent {
            command_id: command.into(),
            protocol: "awg".into(),
            runtime_name: "p_test".into(),
            revision,
            action: action.into(),
            uuid: String::new(),
            short_id: String::new(),
            lease_seconds: None,
        }
    }

    fn ok(_: &Intent, _: std::os::fd::RawFd) -> Result<Response, String> {
        Ok(Response::ok())
    }

    fn uncertain(_: &Intent, _: std::os::fd::RawFd) -> Result<Response, String> {
        Err("unknown outcome".into())
    }

    fn commands_table(journal: &Path, status: &str) {
        let connection = Connection::open(journal).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE commands (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, response TEXT, instance_id TEXT NOT NULL)",
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO commands VALUES ('node-op', 'hash', ?, '{}', 'agent')",
                [status],
            )
            .unwrap();
    }

    #[test]
    fn decommission_needs_a_confirmed_delete_and_then_fences_every_command() {
        let journal = test_journal("decommission");
        apply(&intent(1, "task-1", "ensure"), &journal, "agent", &mut ok).unwrap();
        assert!(prepare(&journal, COMMAND, "agent", None).is_err());
        apply(&intent(2, "task-2", "delete"), &journal, "agent", &mut ok).unwrap();
        let expected = record(COMMAND);
        assert_eq!(prepare(&journal, COMMAND, "agent", None).unwrap(), expected);
        assert_eq!(prepare(&journal, COMMAND, "agent", None).unwrap(), expected);
        assert_eq!(verify(&journal, COMMAND).unwrap(), expected);
        assert!(prepare(&journal, OTHER, "agent", None).is_err());
        assert!(prepare(&journal, "not-a-uuid", "agent", None).is_err());
        assert!(apply(&intent(3, "task-3", "ensure"), &journal, "agent", &mut ok).is_err());
    }

    #[test]
    fn an_interrupted_command_or_an_untracked_fence_refuses() {
        let journal = test_journal("decommission-untracked");
        assert!(
            apply(
                &intent(1, "task-1", "delete"),
                &journal,
                "agent",
                &mut uncertain
            )
            .is_err()
        );
        assert!(prepare(&journal, COMMAND, "agent", None).is_err());
        let connection = Connection::open(&journal).unwrap();
        connection
            .execute_batch("UPDATE commands SET status = 'succeeded' WHERE id = 'task-1'; UPDATE fences SET action = NULL;")
            .unwrap();
        assert!(prepare(&journal, COMMAND, "agent", None).is_err());
    }

    #[test]
    fn an_empty_node_or_one_with_node_operations_only_is_fenced() {
        let empty = test_journal("decommission-empty");
        assert_eq!(
            prepare(&empty, COMMAND, "agent", None).unwrap()["kind"],
            "decommission"
        );
        assert!(!empty.exists());
        let operations = test_journal("decommission-operations");
        commands_table(&operations, "succeeded");
        assert_eq!(
            prepare(&operations, COMMAND, "agent", None).unwrap()["kind"],
            "decommission"
        );
        let unfinished = test_journal("decommission-unfinished");
        commands_table(&unfinished, "running");
        let err = prepare(&unfinished, COMMAND, "agent", None).unwrap_err();
        assert!(err.contains("unfinished"), "{err}");
        assert!(!sidecar(&unfinished, ".disabled").exists());
    }

    #[test]
    fn historical_profiles_unknown_to_the_backend_are_revoked() {
        let journal = test_journal("decommission-history");
        apply(&intent(1, "task-1", "ensure"), &journal, "agent", &mut ok).unwrap();
        let xray = Intent {
            command_id: "second".into(),
            protocol: "xray".into(),
            runtime_name: "old_profile".into(),
            uuid: "11111111-1111-4111-8111-111111111111".into(),
            short_id: "0123456789abcdef".into(),
            ..intent(1, "second", "ensure")
        };
        apply(&xray, &journal, "agent", &mut ok).unwrap();
        let calls = Mutex::new(Vec::new());
        let mut record_calls = |intent: &Intent, _| {
            calls
                .lock()
                .unwrap()
                .push((intent.runtime_name.clone(), intent.action.clone()));
            Ok(Response::ok())
        };
        prepare(&journal, COMMAND, "agent", Some(&mut record_calls)).unwrap();
        prepare(&journal, COMMAND, "agent", Some(&mut record_calls)).unwrap();
        assert_eq!(
            *calls.lock().unwrap(),
            [
                ("p_test".to_string(), "delete".to_string()),
                ("old_profile".into(), "delete".into())
            ]
        );
        let remaining: i64 = Connection::open(&journal)
            .unwrap()
            .query_row(
                "SELECT COUNT(*) FROM fences WHERE action!='delete'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(remaining, 0);
        assert!(apply(&intent(3, "late", "ensure"), &journal, "agent", &mut ok).is_err());
    }

    #[test]
    fn a_failed_historical_revocation_stays_uncertain_and_does_not_fence() {
        let journal = test_journal("decommission-failed");
        apply(&intent(1, "task-1", "ensure"), &journal, "agent", &mut ok).unwrap();
        assert!(prepare(&journal, COMMAND, "agent", Some(&mut uncertain)).is_err());
        assert!(!sidecar(&journal, ".disabled").exists());
        let calls = Mutex::new(0);
        let mut counted = |intent: &Intent, fd| {
            *calls.lock().unwrap() += 1;
            ok(intent, fd)
        };
        let err = prepare(&journal, COMMAND, "agent", Some(&mut counted)).unwrap_err();
        assert!(err.contains("unfinished"), "{err}");
        assert_eq!(*calls.lock().unwrap(), 0);
    }

    #[test]
    fn an_old_journal_needs_a_fresh_delete_first() {
        let journal = test_journal("decommission-old");
        let connection = Connection::open(&journal).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE commands (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, response TEXT, instance_id TEXT NOT NULL);
                 CREATE TABLE fences (protocol TEXT, profile TEXT, revision INTEGER NOT NULL, command_id TEXT NOT NULL, PRIMARY KEY(protocol, profile));
                 INSERT INTO commands VALUES ('old', 'old', 'succeeded', '{}', 'old');
                 INSERT INTO fences VALUES ('awg', 'p_test', 1, 'old');",
            )
            .unwrap();
        drop(connection);
        assert!(prepare(&journal, COMMAND, "agent", None).is_err());
        apply(&intent(2, "task-2", "delete"), &journal, "agent", &mut ok).unwrap();
        assert_eq!(
            prepare(&journal, COMMAND, "agent", None).unwrap()["kind"],
            "decommission"
        );
    }
}
