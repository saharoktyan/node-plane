//! The systemd units of temporary access: the expiry timer and its scan,
//! and one supervisor per leased protocol container. They run this binary;
//! units of earlier releases ran `apply-profile-intent.py`, are still ours,
//! and are rewritten.
use std::fs;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use super::leases::{self, Host, RuntimeSettings};

pub(crate) const UNITS: &str = "/etc/systemd/system";
const AGENT: &str = "/usr/local/bin/node-plane-agent";
const FORMER: &str = "/usr/bin/python3 /opt/node-plane-runtime/apply-profile-intent.py";
const EXPIRY_SERVICE: &str = "node-plane-lease-expiry.service";
const EXPIRY_TIMER: &str = "node-plane-lease-expiry.timer";

pub(crate) fn supervisor(protocol: &str) -> String {
    format!("node-plane-leased-{protocol}.service")
}

/// The unit `name` as this release writes it, or as earlier ones did.
fn unit(name: &str, former: bool) -> Option<String> {
    let exec = |current: &str, old: &str| {
        if former {
            format!("{FORMER} {old}")
        } else {
            format!("{AGENT} {current}")
        }
    };
    if name == EXPIRY_SERVICE {
        return Some(format!(
            "[Unit]
Description=Node Plane temporary access expiry
After=docker.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=root
UMask=0077
ExecStart={}
Restart=on-failure
RestartSec=5s
TimeoutStartSec=60
KillMode=control-group
",
            exec("lease monitor", "monitor-leases")
        ));
    }
    if name == EXPIRY_TIMER {
        return Some(
            "[Unit]
Description=Node Plane temporary access expiry timer

[Timer]
OnBootSec=1s
OnUnitInactiveSec=5s
AccuracySec=1s
Unit=node-plane-lease-expiry.service

[Install]
WantedBy=timers.target
"
            .into(),
        );
    }
    let protocol = ["awg", "xray"]
        .into_iter()
        .find(|protocol| name == supervisor(protocol))?;
    Some(format!(
        "[Unit]
Description=Node Plane leased {protocol} runtime
Requires=docker.service
After=docker.service
PartOf=docker.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=root
UMask=0077
ExecStart={}
Restart=always
RestartSec=5s
TimeoutStopSec=45s
KillMode=control-group

[Install]
WantedBy=docker.service
",
        exec(
            &format!("lease supervise {protocol}"),
            &format!("start-leased-runtime {protocol}"),
        )
    ))
}

/// Whether the unit file at `path` is one of ours.
fn owned(path: &Path, name: &str) -> bool {
    !path.is_symlink()
        && fs::read_to_string(path).is_ok_and(|text| {
            [false, true]
                .iter()
                .any(|former| unit(name, *former).as_deref() == Some(text.as_str()))
        })
}

fn write_unit(dir: &Path, name: &str, text: &str) -> Result<(), String> {
    let failed = |err: std::io::Error| format!("{name}: {err}");
    let staged = dir.join(format!(".node-plane-lease-{}-{name}", std::process::id()));
    let result = (|| {
        let mut file = fs::File::create(&staged)?;
        file.set_permissions(fs::Permissions::from_mode(0o644))?;
        file.write_all(text.as_bytes())?;
        file.sync_all()?;
        fs::rename(&staged, dir.join(name))
    })();
    if result.is_err() {
        let _ = fs::remove_file(&staged);
    }
    result.map_err(failed)
}

/// Writes `names` that differ from this release; whether any changed.
fn write_units(dir: &Path, names: &[String], only_former: bool) -> Result<bool, String> {
    let mut changed = false;
    for name in names {
        let path = dir.join(name);
        let current = unit(name, false).ok_or("unknown lease unit")?;
        let text = fs::read_to_string(&path).ok();
        if text.as_deref() == Some(current.as_str()) {
            continue;
        }
        if only_former && (path.is_symlink() || text != unit(name, true)) {
            continue;
        }
        write_unit(dir, name, &current)?;
        changed = true;
    }
    if changed {
        fs::File::open(dir)
            .and_then(|dir| dir.sync_all())
            .map_err(|err| format!("{}: {err}", dir.display()))?;
    }
    Ok(changed)
}

fn all_units() -> Vec<String> {
    vec![
        EXPIRY_SERVICE.into(),
        EXPIRY_TIMER.into(),
        supervisor("awg"),
        supervisor("xray"),
    ]
}

/// Serializes unit changes; concurrent first issuances never see partial
/// files.
fn lock(dir: &Path) -> Result<fs::File, String> {
    let path = dir.join(".node-plane-lease-expiry.lock");
    let file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|err| format!("lease unit lock: {err}"))?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
        .map_err(|err| format!("lease unit lock: {err}"))?;
    file.lock()
        .map_err(|err| format!("lease unit lock: {err}"))?;
    Ok(file)
}

/// Rewrites units an earlier release wrote. The agent runs this at start,
/// so an update leaves no unit on the removed script.
pub(crate) fn migrate(host: &dyn Host, dir: &Path) -> Result<(), String> {
    if !all_units().iter().any(|name| dir.join(name).exists()) {
        return Ok(());
    }
    let _lock = lock(dir)?;
    if write_units(dir, &all_units(), true)? {
        host.check(&["systemctl", "daemon-reload"], Some(30))?;
    }
    Ok(())
}

/// Installs and starts expiry before the first leased identity; with a
/// protocol, also hands its container to the supervisor.
pub(crate) fn ensure(host: &dyn Host, dir: &Path, protocol: Option<&str>) -> Result<(), String> {
    let _lock = lock(dir)?;
    let mut names = vec![EXPIRY_SERVICE.to_string(), EXPIRY_TIMER.to_string()];
    names.extend(protocol.map(supervisor));
    let mut changed = write_units(dir, &names, false)?;
    changed |= write_units(dir, &all_units(), true)?;
    if changed {
        host.check(&["systemctl", "daemon-reload"], Some(30))?;
        // Newly written units may not be loaded yet; the reset is advisory.
        let _ = host.run(
            &["systemctl", "reset-failed", EXPIRY_SERVICE, EXPIRY_TIMER],
            Some(15),
        );
    }
    host.check(&["systemctl", "enable", "--now", EXPIRY_TIMER], Some(30))?;
    host.check(
        &["systemctl", "is-active", "--quiet", EXPIRY_TIMER],
        Some(15),
    )?;
    host.check(&["systemctl", "start", EXPIRY_SERVICE], Some(30))?;
    if let Some(protocol) = protocol {
        let settings = RuntimeSettings::read(protocol)?;
        hand_over(host, protocol, &settings)?;
        if !leases::enforcement_ready(host, protocol, &settings) {
            return Err("temporary expiry enforcement is unavailable".into());
        }
    }
    Ok(())
}

/// The supervisor takes the restart of the verified container from Docker.
/// No shared daemon setting, other container or permanent identity changes.
fn hand_over(host: &dyn Host, protocol: &str, settings: &RuntimeSettings) -> Result<(), String> {
    let record = leases::container(host, protocol, settings)?;
    host.check(
        &["docker", "update", "--restart", "no", leases::id(&record)],
        Some(30),
    )?;
    host.check(
        &["systemctl", "enable", "--now", &supervisor(protocol)],
        Some(30),
    )?;
    Ok(())
}

/// Stops the supervisors of `protocols`, when they are ours.
pub(crate) fn stop(host: &dyn Host, dir: &Path, protocols: &[&str]) -> Result<(), String> {
    for protocol in protocols {
        let name = supervisor(protocol);
        let path = dir.join(&name);
        if !path.exists() && !path.is_symlink() {
            continue;
        }
        if !owned(&path, &name) {
            return Err("leased runtime unit ownership is unconfirmed".into());
        }
        host.check(&["systemctl", "disable", "--now", &name], Some(45))?;
    }
    Ok(())
}

/// Keeps a replaced protocol container under its supervisor; whether the
/// protocol has one.
pub(crate) fn refresh(host: &dyn Host, dir: &Path, protocol: &str) -> Result<bool, String> {
    let name = supervisor(protocol);
    let path = dir.join(&name);
    if !path.exists() && !path.is_symlink() {
        return Ok(false);
    }
    if !owned(&path, &name) {
        return Err("leased runtime unit ownership is unconfirmed".into());
    }
    ensure(host, dir, None)?;
    hand_over(host, protocol, &RuntimeSettings::read(protocol)?)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::intents::leases::fake::FakeHost;

    #[test]
    fn units_run_the_agent_and_stop_only_when_ours() {
        let dir = crate::protocols::test_dir("lease-units");
        let host = FakeHost::new(dir.join("config.json"));
        write_units(&dir, &[supervisor("xray")], false).unwrap();
        let text = fs::read_to_string(dir.join(supervisor("xray"))).unwrap();
        assert!(text.contains("ExecStart=/usr/local/bin/node-plane-agent lease supervise xray"));
        assert!(text.contains("WantedBy=docker.service") && text.contains("Restart=always"));
        assert!(!text.contains("node-plane-agent.service"));
        stop(&host, &dir, &["xray", "awg"]).unwrap();
        assert_eq!(
            *host.calls.borrow(),
            [[
                "systemctl",
                "disable",
                "--now",
                "node-plane-leased-xray.service"
            ]
            .map(String::from)]
        );
        fs::write(dir.join(supervisor("xray")), "unrelated unit").unwrap();
        assert!(stop(&host, &dir, &["xray"]).is_err());
    }

    #[test]
    fn units_of_earlier_releases_are_ours_and_rewritten() {
        let dir = crate::protocols::test_dir("lease-migrate");
        let host = FakeHost::new(dir.join("config.json"));
        migrate(&host, &dir).unwrap();
        assert!(host.calls.borrow().is_empty(), "no units, nothing to do");
        let former = unit(&supervisor("awg"), true).unwrap();
        assert!(former.contains("apply-profile-intent.py start-leased-runtime awg"));
        fs::write(dir.join(supervisor("awg")), &former).unwrap();
        fs::write(dir.join(EXPIRY_SERVICE), "administrator's unit").unwrap();
        assert!(owned(&dir.join(supervisor("awg")), &supervisor("awg")));
        migrate(&host, &dir).unwrap();
        assert_eq!(
            fs::read_to_string(dir.join(supervisor("awg"))).unwrap(),
            unit(&supervisor("awg"), false).unwrap()
        );
        assert_eq!(
            fs::read_to_string(dir.join(EXPIRY_SERVICE)).unwrap(),
            "administrator's unit"
        );
        assert!(host.ran(&["systemctl", "daemon-reload"]));
    }

    #[test]
    fn the_first_lease_installs_expiry_and_hands_over_the_container() {
        let dir = crate::protocols::test_dir("lease-ensure");
        let host = FakeHost::new(dir.join("config.json"));
        *host.running.borrow_mut() = true;
        ensure(&host, &dir, None).unwrap();
        assert!(dir.join(EXPIRY_TIMER).is_file() && !dir.join(supervisor("xray")).exists());
        for call in [
            &["systemctl", "daemon-reload"][..],
            &["systemctl", "enable", "--now", EXPIRY_TIMER],
            &["systemctl", "start", EXPIRY_SERVICE],
        ] {
            assert!(host.ran(call), "{call:?}");
        }
        host.calls.borrow_mut().clear();
        ensure(&host, &dir, None).unwrap();
        assert!(
            !host.ran(&["systemctl", "daemon-reload"]),
            "unchanged units"
        );
        let settings = RuntimeSettings {
            config: dir.join("config.json"),
            container: "test".into(),
            server_key: String::new(),
        };
        hand_over(&host, "xray", &settings).unwrap();
        assert!(host.ran(&["docker", "update", "--restart", "no", &"a".repeat(64)]));
        assert!(host.ran(&[
            "systemctl",
            "enable",
            "--now",
            "node-plane-leased-xray.service"
        ]));
    }
}
