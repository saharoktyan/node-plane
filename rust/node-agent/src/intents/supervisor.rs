//! The leased protocol container never starts with overdue identities: the
//! supervisor starts it after pruning them, and deployments prune before
//! their new container starts.
use std::os::fd::RawFd;
use std::os::unix::fs::MetadataExt;
use std::path::Path;

use chrono::Utc;
use rusqlite::params;

use super::leases::{self, Host, RuntimeSettings};
use super::profile::parse_timestamp;
use super::{Lock, database, disabled, has_table, sidecar};

/// The journal lock while the config is pruned: inherited from a locked
/// entry point, or taken here.
enum RestartLock {
    Inherited,
    Own(#[allow(dead_code)] Lock),
}

impl RestartLock {
    fn take(journal: &Path) -> Result<Self, String> {
        let Some(inherited) = std::env::var_os("NODE_PLANE_PROFILE_LOCK_FD") else {
            return Lock::exclusive(journal).map(Self::Own);
        };
        let invalid = || "invalid inherited runtime lock".to_string();
        let fd: RawFd = inherited
            .to_str()
            .and_then(|fd| fd.parse().ok())
            .ok_or_else(invalid)?;
        let expected = std::fs::metadata(sidecar(journal, ".lock")).map_err(|_| invalid())?;
        // SAFETY: fstat only reads the descriptor's metadata into `actual`.
        let mut actual: libc::stat = unsafe { std::mem::zeroed() };
        if unsafe { libc::fstat(fd, &mut actual) } != 0
            || actual.st_dev != expected.dev()
            || actual.st_ino != expected.ino()
        {
            return Err(invalid());
        }
        // The inherited description shares its parent's flock, which stays
        // held; any other holder makes this fail instead of waiting.
        // SAFETY: flock on a descriptor checked above.
        if unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err(invalid());
        }
        Ok(Self::Inherited)
    }
}

/// Marks overdue and unconfirmed leases for revocation and prunes them from
/// the stopped container's config; the caller holds the journal lock.
fn prune_locked(protocol: &str, journal: &Path, settings: &RuntimeSettings) -> Result<(), String> {
    if disabled(journal) {
        return Err("node removal is in progress".into());
    }
    if !journal.exists() {
        return Ok(());
    }
    let connection = rusqlite::Connection::open(journal).map_err(database)?;
    if !has_table(&connection, "temporary_leases")? {
        return Ok(());
    }
    let now = Utc::now();
    let mut names = Vec::new();
    {
        let mut statement = connection
            .prepare("SELECT profile,expires_at,status FROM temporary_leases WHERE protocol=?")
            .map_err(database)?;
        let rows = statement
            .query_map([protocol], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            })
            .map_err(database)?;
        for row in rows {
            let (name, deadline, status) = row.map_err(database)?;
            if status != "active" || parse_timestamp(&deadline)? <= now {
                names.push(name);
            }
        }
    }
    // Pending revocations stay recorded if pruning or the start fails; a
    // removal is never reported before its runtime outcome is confirmed.
    let transaction = connection.unchecked_transaction().map_err(database)?;
    for name in &names {
        transaction
            .execute(
                "UPDATE temporary_leases SET status='revoking' WHERE protocol=? AND profile=? AND status IN ('active','provisioning')",
                params![protocol, name],
            )
            .map_err(database)?;
    }
    transaction.commit().map_err(database)?;
    leases::prune_config(protocol, &settings.config, &names, &settings.server_key)
}

/// Before a deployment starts the protocol's new container.
pub(crate) fn prepare(protocol: &str, journal: &Path) -> Result<(), String> {
    let _lock = RestartLock::take(journal)?;
    let settings = RuntimeSettings::read(protocol)?;
    prune_locked(protocol, journal, &settings)
}

/// Supervises the protocol container: starts it when stopped, after
/// pruning, then waits for it to exit.
pub(crate) fn supervise(
    host: &dyn Host,
    protocol: &str,
    journal: &Path,
    settings: &RuntimeSettings,
) -> Result<(), String> {
    let record = {
        let _lock = Lock::exclusive(journal)?;
        if disabled(journal) {
            return Err("node removal is in progress".into());
        }
        let record = leases::container(host, protocol, settings)?;
        host.check(
            &["docker", "update", "--restart", "no", leases::id(&record)],
            Some(30),
        )?;
        if !leases::running(&record) {
            prune_locked(protocol, journal, settings)?;
            host.check(&["docker", "start", leases::id(&record)], Some(30))?;
            if !leases::running(&leases::container(host, protocol, settings)?) {
                return Err("leased runtime did not start".into());
            }
        }
        record
    };
    // Grants, revocations and expiry share the lock: it is not held while
    // waiting. A restart after a crash runs the guard again.
    host.check(&["docker", "wait", leases::id(&record)], None)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::sync::Mutex;

    use rusqlite::Connection;
    use serde_json::{Value, json};

    use super::*;
    use crate::intents::leases::fake::FakeHost;
    use crate::intents::profile::{Intent, apply, timestamp};
    use crate::intents::{Response, test_journal};

    fn lease(journal: &Path, protocol: &str, overdue: bool) -> Intent {
        let name = format!("tmp_{}", crate::protocols::deploy::random_hex(16).unwrap());
        let xray = protocol == "xray";
        let intent = Intent {
            command_id: crate::protocols::deploy::uuid4().unwrap(),
            protocol: protocol.into(),
            runtime_name: name,
            revision: 1,
            action: "ensure".into(),
            uuid: if xray {
                crate::protocols::deploy::uuid4().unwrap()
            } else {
                String::new()
            },
            short_id: if xray {
                "0123456789abcdef".into()
            } else {
                String::new()
            },
            lease_seconds: Some(86400),
        };
        apply(&intent, journal, "agent", &mut |_, _| Ok(Response::ok())).unwrap();
        if overdue {
            Connection::open(journal)
                .unwrap()
                .execute(
                    "UPDATE temporary_leases SET expires_at=? WHERE command_id=?",
                    params![
                        timestamp(Utc::now() - chrono::Duration::seconds(1)),
                        intent.command_id
                    ],
                )
                .unwrap();
        }
        intent
    }

    fn settings(config: &Path) -> RuntimeSettings {
        RuntimeSettings {
            config: config.to_path_buf(),
            container: "test".into(),
            server_key: "lv1".into(),
        }
    }

    fn statuses(journal: &Path) -> std::collections::HashMap<String, String> {
        let connection = Connection::open(journal).unwrap();
        let mut statement = connection
            .prepare("SELECT profile,status FROM temporary_leases")
            .unwrap();
        statement
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
            .unwrap()
            .collect::<Result<_, _>>()
            .unwrap()
    }

    #[test]
    fn a_start_prunes_overdue_and_unconfirmed_vless_users_and_keeps_others() {
        let journal = test_journal("supervise-xray");
        let (due, active, uncertain) = (
            lease(&journal, "xray", true),
            lease(&journal, "xray", false),
            lease(&journal, "xray", false),
        );
        Connection::open(&journal)
            .unwrap()
            .execute(
                "UPDATE temporary_leases SET status='provisioning' WHERE command_id=?",
                [&uncertain.command_id],
            )
            .unwrap();
        let config = journal.with_file_name("config.json");
        let mut clients: Vec<Value> = [&due, &active, &uncertain]
            .iter()
            .map(|lease| json!({"id": lease.uuid, "email": lease.runtime_name}))
            .collect();
        clients.push(json!({"id": "permanent-secret", "email": "regular"}));
        fs::write(
            &config,
            json!({"inbounds": [{"protocol": "vless", "settings": {"clients": clients}}]})
                .to_string(),
        )
        .unwrap();
        let journal_lock = journal.clone();
        let mut host = FakeHost::new(config.clone());
        // Supervision must not hold the mutation and expiry lock.
        host.on_wait = Box::new(move || {
            let other = fs::OpenOptions::new()
                .append(true)
                .open(sidecar(&journal_lock, ".lock"))
                .unwrap();
            other.try_lock().unwrap();
        });
        supervise(&host, "xray", &journal, &settings(&config)).unwrap();
        let started: Value =
            serde_json::from_str(host.started.borrow().as_deref().unwrap()).unwrap();
        let names: Vec<&str> = started["inbounds"][0]["settings"]["clients"]
            .as_array()
            .unwrap()
            .iter()
            .map(|client| client["email"].as_str().unwrap())
            .collect();
        assert_eq!(names, [active.runtime_name.as_str(), "regular"]);
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(&config).unwrap().permissions().mode() & 0o777,
            0o600
        );
        let statuses = statuses(&journal);
        assert_eq!(statuses[&due.runtime_name], "revoking");
        assert_eq!(statuses[&active.runtime_name], "active");
        assert_eq!(statuses[&uncertain.runtime_name], "revoking");
        assert_eq!(
            host.calls.borrow().last().unwrap(),
            &["docker", "wait", &"a".repeat(64)].map(String::from)
        );
    }

    #[test]
    fn an_awg_restart_removes_only_the_expired_peer() {
        let journal = test_journal("supervise-awg");
        let (due, active) = (lease(&journal, "awg", true), lease(&journal, "awg", false));
        let config = journal.with_file_name("wg0.conf");
        let peer = |name: &str, key: &str| {
            format!("# {name}\n[Peer]\nPublicKey = {key}\nAllowedIPs = 10.8.1.2/32\n\n")
        };
        fs::write(
            &config,
            format!(
                "[Interface]\nPrivateKey = server-secret\n\n{}{}{}",
                peer(&format!("lv1-{}", due.runtime_name), "expired"),
                peer(&format!("lv1-{}", active.runtime_name), "active"),
                peer("permanent", "regular")
            ),
        )
        .unwrap();
        let host = FakeHost::new(config.clone());
        supervise(&host, "awg", &journal, &settings(&config)).unwrap();
        let started = host.started.borrow().clone().unwrap();
        assert!(!started.contains("PublicKey = expired"));
        for kept in [
            "PublicKey = active",
            "PublicKey = regular",
            "PrivateKey = server-secret",
        ] {
            assert!(started.contains(kept), "{kept}");
        }
    }

    #[test]
    fn an_unknown_config_or_owner_or_removal_never_starts_the_container() {
        let journal = test_journal("supervise-refuse");
        lease(&journal, "xray", true);
        let config = journal.with_file_name("config.json");
        fs::write(&config, "invalid json").unwrap();
        let mut host = FakeHost::new(config.clone());
        assert!(supervise(&host, "xray", &journal, &settings(&config)).is_err());
        assert!(
            !host
                .calls
                .borrow()
                .iter()
                .any(|call| call.starts_with(&["docker".to_string(), "start".to_string()]))
        );
        host.bad_mount = true;
        let err = supervise(&host, "xray", &journal, &settings(&config)).unwrap_err();
        assert!(err.contains("ownership"), "{err}");
        fs::write(sidecar(&journal, ".disabled"), "").unwrap();
        host.calls.borrow_mut().clear();
        let err = supervise(&host, "xray", &journal, &settings(&config)).unwrap_err();
        assert!(err.contains("removal"), "{err}");
        assert!(host.calls.borrow().is_empty());
    }

    #[test]
    fn a_running_shared_container_is_not_restarted() {
        let journal = test_journal("supervise-running");
        let config = journal.with_file_name("config.json");
        let host = FakeHost::new(config.clone());
        *host.running.borrow_mut() = true;
        supervise(&host, "xray", &journal, &settings(&config)).unwrap();
        assert!(!host.calls.borrow().iter().any(|call| {
            call[..2] == ["docker", "stop"]
                || call.starts_with(&["docker".to_string(), "start".to_string()])
        }));
    }

    // The process environment is shared by tests: one test sets it.
    static ENVIRONMENT: Mutex<()> = Mutex::new(());

    #[test]
    fn an_inherited_lock_stays_held_and_another_file_is_refused() {
        let _environment = ENVIRONMENT.lock().unwrap();
        let journal = test_journal("restart-lock");
        let parent = Lock::exclusive(&journal).unwrap();
        // SAFETY: tests that read this variable hold ENVIRONMENT.
        unsafe { std::env::set_var("NODE_PLANE_PROFILE_LOCK_FD", parent.fd().to_string()) };
        assert!(matches!(
            RestartLock::take(&journal),
            Ok(RestartLock::Inherited)
        ));
        let other = fs::OpenOptions::new()
            .append(true)
            .open(sidecar(&journal, ".lock"))
            .unwrap();
        assert!(other.try_lock().is_err(), "the parent still holds it");
        let unrelated = fs::File::create(journal.with_file_name("other.lock")).unwrap();
        use std::os::fd::AsRawFd;
        unsafe {
            std::env::set_var(
                "NODE_PLANE_PROFILE_LOCK_FD",
                unrelated.as_raw_fd().to_string(),
            )
        };
        assert!(RestartLock::take(&journal).is_err());
        unsafe { std::env::remove_var("NODE_PLANE_PROFILE_LOCK_FD") };
    }
}
