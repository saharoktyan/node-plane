//! systemd hardening of the agent service.
//!
//! Only options that do not get in the way of the agent's work: it must still
//! run Docker, apt, ufw, tc, nft and nsenter and write systemd units. A
//! compromised agent can leave this sandbox through the Docker socket,
//! systemd-run or its own unit; it limits mistakes, such as a write to the
//! wrong path, not an attacker. Checked against those tools in a Debian 12 VM.
use std::fs;
use std::path::Path;
use std::process::Command;

pub(crate) const UNIT: &str = "/etc/systemd/system/node-plane-agent.service";
pub(crate) const DROP_IN: &str = "/etc/systemd/system/node-plane-agent.service.d/hardening.conf";

pub(crate) const CONTENT: &str = "\
# Written by node-plane-agent; see docs/agent.md.
[Service]
ProtectHome=read-only
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectHostname=yes
LockPersonality=yes
RestrictRealtime=yes
SystemCallArchitectures=native
";

/// Write the drop-in when it differs; true when systemd has to reload.
pub(crate) fn write(path: &Path) -> Result<bool, String> {
    if fs::read_to_string(path).is_ok_and(|current| current == CONTENT) {
        return Ok(false);
    }
    let parent = path.parent().ok_or("drop-in path has no directory")?;
    fs::create_dir_all(parent).map_err(|err| format!("{}: {err}", parent.display()))?;
    let temporary = parent.join(".hardening.conf.tmp");
    fs::write(&temporary, CONTENT)
        .and_then(|()| fs::rename(&temporary, path))
        .map_err(|err| format!("{}: {err}", path.display()))?;
    Ok(true)
}

/// Keep the installed service hardened; applies from its next start.
pub(crate) fn ensure() -> Result<bool, String> {
    if !Path::new(UNIT).is_file() {
        return Ok(false);
    }
    let changed = write(Path::new(DROP_IN))?;
    if changed {
        let status = Command::new("systemctl")
            .arg("daemon-reload")
            .status()
            .map_err(|err| format!("systemctl daemon-reload: {err}"))?;
        if !status.success() {
            return Err("systemctl daemon-reload failed".into());
        }
    }
    Ok(changed)
}

pub(crate) fn run(args: &[String]) -> Option<i32> {
    if args.first().map(String::as_str) != Some("harden") {
        return None;
    }
    if args.len() != 1 {
        eprintln!("usage: harden");
        return Some(1);
    }
    Some(match ensure() {
        Ok(true) => {
            println!("agent service hardening written to {DROP_IN}");
            0
        }
        Ok(false) => {
            println!("agent service hardening is current");
            0
        }
        Err(err) => {
            eprintln!("{err}");
            1
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn drop_in_is_written_once_and_restored_when_changed() {
        let dir = std::env::temp_dir().join(format!("node-plane-hardening-{}", std::process::id()));
        let path = dir.join("node-plane-agent.service.d/hardening.conf");
        assert_eq!(write(&path), Ok(true));
        assert_eq!(fs::read_to_string(&path).unwrap(), CONTENT);
        assert_eq!(write(&path), Ok(false));
        fs::write(&path, "[Service]\nProtectHome=no\n").unwrap();
        assert_eq!(write(&path), Ok(true));
        assert_eq!(fs::read_to_string(&path).unwrap(), CONTENT);
        assert!(!path.with_file_name(".hardening.conf.tmp").exists());
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn hardening_keeps_what_the_agent_needs() {
        // Each of these would break Docker, apt, ufw, tc or nsenter.
        for absent in [
            "ProtectSystem",
            "PrivateTmp",
            "NoNewPrivileges",
            "CapabilityBoundingSet",
            "RestrictNamespaces",
            "ProtectKernelTunables",
            "DeviceAllow",
        ] {
            assert!(!CONTENT.contains(absent), "{absent}");
        }
    }
}
