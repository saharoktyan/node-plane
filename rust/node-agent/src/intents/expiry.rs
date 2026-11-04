//! Local expiry of temporary access, independent of the controller and of
//! the agent's RPC service: the timer's scan revokes every overdue lease.
use std::path::Path;
use std::time::Duration;

use chrono::{DateTime, Utc};
use rusqlite::{OptionalExtension, params};
use serde_json::json;

use super::profile::{Intent, Runner, parse_timestamp};
use super::{Lock, database, disabled, has_table, open, sha256_hex};

/// Overdue leases revoked by a scan, and those whose revocation failed.
#[derive(Debug, Default, PartialEq, Eq)]
pub(crate) struct Scan {
    pub(crate) expired: u32,
    pub(crate) pending: u32,
}

/// Revokes every overdue lease once. Only idempotent deletes are retried; an
/// interrupted ensure never runs again. The lock also waits for orphaned
/// mutations to stop.
pub(crate) fn expire(
    journal: &Path,
    instance: &str,
    now: DateTime<Utc>,
    runner: &mut Runner<'_>,
) -> Result<Scan, String> {
    if !journal.exists() {
        return Ok(Scan::default());
    }
    let lock = Lock::exclusive(journal)?;
    // Decommission writes this fence only after every known peer is removed;
    // cleanup may already have deleted the protocol runtime.
    if disabled(journal) {
        return Ok(Scan::default());
    }
    let connection = open(journal)?;
    if !has_table(&connection, "temporary_leases")? {
        return Ok(Scan::default());
    }
    let rows: Vec<(String, String, String, String, String)> = {
        let mut statement = connection
            .prepare(
                "SELECT protocol,profile,command_id,expires_at,status
                FROM temporary_leases WHERE status IN ('provisioning','active','revoking')
                ORDER BY expires_at,protocol,profile",
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
    let mut scan = Scan::default();
    for (protocol, profile, command_id, deadline, status) in rows {
        if status != "revoking" && parse_timestamp(&deadline)? > now {
            continue;
        }
        // The identity stays reserved, also after a failed delete.
        connection
            .execute(
                "UPDATE temporary_leases SET status='revoking' WHERE protocol=? AND profile=?",
                params![protocol, profile],
            )
            .map_err(database)?;
        let (revision, fence_command, fence_action): (i64, String, Option<String>) = connection
            .query_row(
                "SELECT revision,command_id,action FROM fences WHERE protocol=? AND profile=?",
                params![protocol, profile],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()
            .map_err(database)?
            .ok_or("temporary lease has no fence")?;
        let intent = Intent {
            command_id: format!("expiry:{}", sha256_hex(command_id.as_bytes())),
            protocol: protocol.clone(),
            runtime_name: profile.clone(),
            revision: revision + 1,
            action: "delete".into(),
            uuid: String::new(),
            short_id: String::new(),
            lease_seconds: None,
        };
        intent.validate()?;
        let Ok(response) = runner(&intent, lock.fd()) else {
            scan.pending += 1;
            continue;
        };
        let transaction = connection.unchecked_transaction().map_err(database)?;
        transaction
            .execute(
                "INSERT INTO commands VALUES (?,?,'succeeded',?,?) ON CONFLICT(id) DO NOTHING",
                params![
                    intent.command_id,
                    intent.fingerprint(),
                    response.to_json(),
                    instance
                ],
            )
            .map_err(database)?;
        transaction
            .execute(
                "UPDATE fences SET revision=?,command_id=?,action='delete' WHERE protocol=? AND profile=?",
                params![intent.revision, intent.command_id, protocol, profile],
            )
            .map_err(database)?;
        transaction
            .execute(
                "UPDATE commands SET status='superseded' WHERE id=? AND status='running'",
                [&command_id],
            )
            .map_err(database)?;
        if fence_action.as_deref() == Some("delete") {
            transaction
                .execute(
                    "UPDATE commands SET status='superseded' WHERE id=? AND status='running'",
                    [&fence_command],
                )
                .map_err(database)?;
        }
        transaction
            .execute(
                "UPDATE temporary_leases SET status='expired' WHERE protocol=? AND profile=?",
                params![protocol, profile],
            )
            .map_err(database)?;
        transaction.commit().map_err(database)?;
        scan.expired += 1;
    }
    Ok(scan)
}

/// Scans every second for the life of the expiry service. A failed
/// revocation ends it with an error, so systemd retries and the service
/// never reports an unhealthy scan as healthy.
pub(crate) fn monitor(
    journal: &Path,
    instance: &str,
    runner: &mut Runner<'_>,
) -> Result<(), String> {
    loop {
        let scan = expire(journal, instance, Utc::now(), runner)?;
        if scan.expired > 0 {
            println!(
                "{}",
                json!({"expired": scan.expired, "pending": scan.pending})
            );
        }
        if scan.pending > 0 {
            return Err("temporary lease revocation is pending".into());
        }
        std::thread::sleep(Duration::from_secs(1));
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use rusqlite::Connection;

    use super::*;
    use crate::intents::profile::{apply, lookup, timestamp};
    use crate::intents::{Response, sidecar, test_journal};

    fn lease(protocol: &str, seed: u8) -> Intent {
        let xray = protocol == "xray";
        Intent {
            command_id: format!("grant-{seed}"),
            protocol: protocol.into(),
            runtime_name: format!("tmp_{}", format!("{seed:02x}").repeat(16)),
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
            lease_seconds: Some(86400),
        }
    }

    fn revocation(lease: &Intent, command: &str, revision: i64) -> Intent {
        Intent {
            command_id: command.into(),
            revision,
            action: "delete".into(),
            uuid: String::new(),
            short_id: String::new(),
            lease_seconds: None,
            ..lease.clone()
        }
    }

    /// Records the runs of every mutation.
    struct Calls(Mutex<Vec<Intent>>);

    impl Calls {
        fn new() -> Self {
            Self(Mutex::new(Vec::new()))
        }

        fn runner(
            &self,
        ) -> impl FnMut(&Intent, std::os::fd::RawFd) -> Result<Response, String> + '_ {
            |intent, _| {
                self.0.lock().unwrap().push(intent.clone());
                Ok(Response::ok())
            }
        }

        fn actions(&self) -> Vec<String> {
            self.0
                .lock()
                .unwrap()
                .iter()
                .map(|i| i.action.clone())
                .collect()
        }
    }

    fn failing(_: &Intent, _: std::os::fd::RawFd) -> Result<Response, String> {
        Err("lost result".into())
    }

    fn overdue(journal: &Path, intent: &Intent) {
        Connection::open(journal)
            .unwrap()
            .execute(
                "UPDATE temporary_leases SET expires_at=? WHERE protocol=? AND profile=?",
                params![
                    timestamp(Utc::now() - chrono::Duration::seconds(1)),
                    intent.protocol,
                    intent.runtime_name
                ],
            )
            .unwrap();
    }

    fn status(journal: &Path, intent: &Intent) -> String {
        Connection::open(journal)
            .unwrap()
            .query_row(
                "SELECT status FROM temporary_leases WHERE protocol=? AND profile=?",
                params![intent.protocol, intent.runtime_name],
                |row| row.get(0),
            )
            .unwrap()
    }

    fn command_status(journal: &Path, id: &str) -> String {
        Connection::open(journal)
            .unwrap()
            .query_row("SELECT status FROM commands WHERE id=?", [id], |row| {
                row.get(0)
            })
            .unwrap()
    }

    fn scan(journal: &Path, runner: &mut Runner<'_>) -> Scan {
        expire(journal, "agent", Utc::now(), runner).unwrap()
    }

    fn found(expired: u32, pending: u32) -> Scan {
        Scan { expired, pending }
    }

    #[test]
    fn leases_last_their_duration_and_a_repeat_does_not_extend_them() {
        let journal = test_journal("expiry-lifetime");
        let calls = Calls::new();
        let mut seed = 0;
        for protocol in ["awg", "xray"] {
            for seconds in [43200, 86400, 259200] {
                seed += 1;
                let intent = Intent {
                    lease_seconds: Some(seconds),
                    ..lease(protocol, seed)
                };
                let before = Utc::now();
                let receipt = apply(&intent, &journal, "agent", &mut calls.runner()).unwrap();
                let payload: serde_json::Value =
                    serde_json::from_str(&receipt.payload_json).unwrap();
                let deadline = parse_timestamp(payload["expires_at"].as_str().unwrap()).unwrap();
                assert!(deadline >= before + chrono::Duration::seconds(seconds));
                assert!(deadline <= Utc::now() + chrono::Duration::seconds(seconds));
                let mode = if protocol == "xray" {
                    "new_connections_only"
                } else {
                    "peer_removed"
                };
                assert_eq!(payload["revocation_mode"], mode);
                assert_eq!(
                    apply(&intent, &journal, "agent", &mut calls.runner()).unwrap(),
                    receipt
                );
                assert_eq!(lookup(&intent, &journal).unwrap(), receipt);
                let longer = Intent {
                    lease_seconds: Some(259200),
                    ..intent.clone()
                };
                if seconds != 259200 {
                    assert!(apply(&longer, &journal, "agent", &mut calls.runner()).is_err());
                }
            }
        }
        assert_eq!(calls.actions().len(), 6);
        assert_eq!(scan(&journal, &mut calls.runner()), Scan::default());
    }

    #[test]
    fn expiry_touches_only_the_overdue_lease() {
        let journal = test_journal("expiry-isolated");
        let calls = Calls::new();
        let (first, second) = (lease("awg", 1), lease("awg", 2));
        let permanent = Intent {
            runtime_name: "permanent".into(),
            lease_seconds: None,
            ..lease("awg", 3)
        };
        for intent in [&first, &second, &permanent] {
            apply(intent, &journal, "agent", &mut calls.runner()).unwrap();
        }
        overdue(&journal, &first);
        assert_eq!(scan(&journal, &mut calls.runner()), found(1, 0));
        let last = calls.0.lock().unwrap().last().cloned().unwrap();
        assert_eq!(
            (last.runtime_name, last.action),
            (first.runtime_name.clone(), "delete".into())
        );
        assert_eq!(status(&journal, &second), "active");
        lookup(&second, &journal).unwrap();
        lookup(&permanent, &journal).unwrap();
        assert_eq!(scan(&journal, &mut calls.runner()), Scan::default());
    }

    #[test]
    fn expired_access_cannot_be_read_replayed_renewed_or_made_permanent() {
        let journal = test_journal("expiry-final");
        let calls = Calls::new();
        let intent = lease("xray", 1);
        apply(&intent, &journal, "agent", &mut calls.runner()).unwrap();
        overdue(&journal, &intent);
        assert!(lookup(&intent, &journal).is_err());
        assert!(apply(&intent, &journal, "agent", &mut calls.runner()).is_err());
        scan(&journal, &mut calls.runner());
        for leased in [true, false] {
            let replacement = Intent {
                command_id: "replacement".into(),
                revision: 3,
                lease_seconds: leased.then_some(86400),
                ..intent.clone()
            };
            assert!(apply(&replacement, &journal, "agent", &mut calls.runner()).is_err());
        }
        assert_eq!(calls.actions(), ["ensure", "delete"]);
    }

    #[test]
    fn an_interrupted_grant_is_revoked_without_running_again() {
        let journal = test_journal("expiry-interrupted");
        let calls = Calls::new();
        let intent = lease("awg", 1);
        let mut interrupted = |intent: &Intent, fd| {
            calls.runner()(intent, fd)?;
            Err::<Response, _>("private diagnostics".to_string())
        };
        assert!(apply(&intent, &journal, "agent", &mut interrupted).is_err());
        assert_eq!(status(&journal, &intent), "provisioning");
        overdue(&journal, &intent);
        assert_eq!(scan(&journal, &mut calls.runner()), found(1, 0));
        assert_eq!(command_status(&journal, &intent.command_id), "superseded");
        // Expiry frees the journal for other profiles.
        apply(&lease("awg", 2), &journal, "agent", &mut calls.runner()).unwrap();
    }

    #[test]
    fn a_failed_revocation_stays_pending_and_is_retried() {
        let journal = test_journal("expiry-retry");
        let calls = Calls::new();
        let (first, second) = (lease("awg", 1), lease("xray", 2));
        for intent in [&first, &second] {
            apply(intent, &journal, "agent", &mut calls.runner()).unwrap();
            overdue(&journal, intent);
        }
        let mut awg_down = |intent: &Intent, fd| {
            if intent.protocol == "awg" {
                return Err("offline runtime".to_string());
            }
            calls.runner()(intent, fd)
        };
        assert_eq!(scan(&journal, &mut awg_down), found(1, 1));
        assert_eq!(status(&journal, &first), "revoking");
        assert_eq!(scan(&journal, &mut calls.runner()), found(1, 0));
        assert_eq!(status(&journal, &first), "expired");
    }

    #[test]
    fn the_timer_settles_a_failed_manual_revocation() {
        let journal = test_journal("expiry-manual");
        let calls = Calls::new();
        let intent = lease("awg", 1);
        apply(&intent, &journal, "agent", &mut calls.runner()).unwrap();
        let deletion = revocation(&intent, "manual", 2);
        assert!(apply(&deletion, &journal, "agent", &mut failing).is_err());
        assert_eq!(scan(&journal, &mut calls.runner()), found(1, 0));
        let running: i64 = Connection::open(&journal)
            .unwrap()
            .query_row(
                "SELECT COUNT(*) FROM commands WHERE status='running'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(running, 0);
        let permanent = Intent {
            runtime_name: "permanent".into(),
            lease_seconds: None,
            ..lease("awg", 2)
        };
        apply(&permanent, &journal, "agent", &mut calls.runner()).unwrap();
    }

    #[test]
    fn an_early_revocation_ends_expiry_and_the_identity() {
        let journal = test_journal("expiry-early");
        let calls = Calls::new();
        let intent = lease("awg", 1);
        apply(&intent, &journal, "agent", &mut calls.runner()).unwrap();
        apply(
            &revocation(&intent, "revoke", 2),
            &journal,
            "agent",
            &mut calls.runner(),
        )
        .unwrap();
        assert_eq!(status(&journal, &intent), "revoked");
        let later = Utc::now() + chrono::Duration::days(2);
        assert_eq!(
            expire(&journal, "agent", later, &mut calls.runner()).unwrap(),
            Scan::default()
        );
        let again = Intent {
            command_id: "again".into(),
            revision: 3,
            ..intent
        };
        assert!(apply(&again, &journal, "agent", &mut calls.runner()).is_err());
    }

    #[test]
    fn concurrent_scans_revoke_once() {
        let journal = test_journal("expiry-concurrent");
        let calls = Calls::new();
        let intent = lease("awg", 1);
        apply(&intent, &journal, "agent", &mut calls.runner()).unwrap();
        overdue(&journal, &intent);
        let expired: u32 = std::thread::scope(|scope| {
            let scans: Vec<_> = (0..2)
                .map(|_| scope.spawn(|| scan(&journal, &mut calls.runner()).expired))
                .collect();
            scans.into_iter().map(|scan| scan.join().unwrap()).sum()
        });
        assert_eq!(expired, 1);
        assert_eq!(calls.actions(), ["ensure", "delete"]);
    }

    #[test]
    fn removal_or_an_old_journal_scans_nothing_and_creates_nothing() {
        let journal = test_journal("expiry-removed");
        let calls = Calls::new();
        assert_eq!(scan(&journal, &mut calls.runner()), Scan::default());
        assert!(!journal.exists());
        Connection::open(&journal)
            .unwrap()
            .execute_batch("CREATE TABLE old_fixture (id TEXT)")
            .unwrap();
        scan(&journal, &mut calls.runner());
        let tables: Vec<String> = {
            let connection = Connection::open(&journal).unwrap();
            let mut statement = connection
                .prepare("SELECT name FROM sqlite_master WHERE type='table'")
                .unwrap();
            statement
                .query_map([], |row| row.get(0))
                .unwrap()
                .collect::<Result<_, _>>()
                .unwrap()
        };
        assert_eq!(tables, ["old_fixture"]);

        let removed = test_journal("expiry-removed-marker");
        let intent = lease("awg", 1);
        apply(&intent, &removed, "agent", &mut calls.runner()).unwrap();
        overdue(&removed, &intent);
        std::fs::write(sidecar(&removed, ".disabled"), "").unwrap();
        assert_eq!(scan(&removed, &mut calls.runner()), Scan::default());
        assert_eq!(calls.actions(), ["ensure"]);
    }

    #[test]
    fn the_monitor_fails_for_a_supervised_retry_when_revocation_is_pending() {
        let journal = test_journal("expiry-monitor");
        let calls = Calls::new();
        let intent = lease("awg", 1);
        apply(&intent, &journal, "agent", &mut calls.runner()).unwrap();
        overdue(&journal, &intent);
        let err = monitor(&journal, "agent", &mut failing).unwrap_err();
        assert!(err.contains("pending"), "{err}");
    }
}
