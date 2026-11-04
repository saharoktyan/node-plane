//! `node-plane-agent container-deploy xray|awg`: replaces the protocol's
//! container with one running the current config, and brings the previous
//! container back when the new one does not start. Replaces the body of
//! `deploy-xray.sh` and `deploy-awg.sh`, which only start this subcommand
//! for the profile intent journal, and `deploy-container-guard.sh`.
pub(crate) mod guard;

use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::Duration;

use ring::rand::{SecureRandom, SystemRandom};

use super::{Setting, process_setting};

fn succeeds(program: &str, args: &[&str]) -> bool {
    Command::new(program)
        .args(args)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|status| status.success())
}

/// The Docker CLI, through sudo when only that reaches the daemon.
pub(crate) struct Docker {
    sudo: bool,
}

impl Docker {
    /// Starts the daemon when it is down.
    pub(crate) fn connect() -> Result<Self, String> {
        if succeeds("docker", &["info"]) {
            return Ok(Self { sudo: false });
        }
        let sudo = std::env::var_os("PATH")
            .is_some_and(|path| std::env::split_paths(&path).any(|dir| dir.join("sudo").is_file()));
        if sudo {
            let _ = succeeds("sudo", &["systemctl", "enable", "--now", "docker"])
                || succeeds("sudo", &["service", "docker", "start"]);
            if succeeds("sudo", &["docker", "info"]) {
                return Ok(Self { sudo: true });
            }
        }
        let _ = succeeds("systemctl", &["enable", "--now", "docker"])
            || succeeds("service", &["docker", "start"]);
        if succeeds("docker", &["info"]) {
            return Ok(Self { sudo: false });
        }
        for (program, args) in [
            ("id", &[][..]),
            ("groups", &[]),
            ("ls", &["-l", "/var/run/docker.sock"]),
        ] {
            let _ = Command::new(program)
                .args(args)
                .stdout(std::io::stderr())
                .status();
        }
        Err("Docker daemon is unavailable for this session.".into())
    }

    pub(crate) fn command(&self, args: &[&str]) -> Command {
        let mut command = if self.sudo {
            let mut command = Command::new("sudo");
            command.arg("docker");
            command
        } else {
            Command::new("docker")
        };
        command.args(args);
        command
    }

    /// Stdout of a command whose errors reach our stderr.
    pub(crate) fn output(&self, args: &[&str]) -> Result<String, String> {
        let output = self
            .command(args)
            .stderr(Stdio::inherit())
            .output()
            .map_err(|err| format!("docker failed: {err}"))?;
        if !output.status.success() {
            return Err(format!("docker {} failed", args.first().unwrap_or(&"")));
        }
        Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
    }

    /// A command whose stdout is discarded.
    pub(crate) fn status(&self, args: &[&str]) -> Result<(), String> {
        let status = self
            .command(args)
            .stdout(Stdio::null())
            .status()
            .map_err(|err| format!("docker failed: {err}"))?;
        status
            .success()
            .then_some(())
            .ok_or_else(|| format!("docker {} failed", args.first().unwrap_or(&"")))
    }

    /// A command whose output is discarded.
    pub(crate) fn quiet(&self, args: &[&str]) -> bool {
        self.command(args)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_ok_and(|status| status.success())
    }

    /// Diagnostics: a command whose output goes to our stderr.
    pub(crate) fn report(&self, args: &[&str]) {
        let _ = self.command(args).stdout(std::io::stderr()).status();
    }
}

/// Random hex digits.
pub(crate) fn random_hex(bytes: usize) -> Result<String, String> {
    let mut buffer = vec![0u8; bytes];
    SystemRandom::new()
        .fill(&mut buffer)
        .map_err(|_| "random source unavailable".to_string())?;
    Ok(buffer.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// A random UUID, version 4.
pub(crate) fn uuid4() -> Result<String, String> {
    let mut bytes = [0u8; 16];
    SystemRandom::new()
        .fill(&mut bytes)
        .map_err(|_| "random source unavailable".to_string())?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex: String = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    Ok(format!(
        "{}-{}-{}-{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..]
    ))
}

/// Whether the lease supervisor runs the protocol's container.
pub(crate) fn leased(service: &str) -> bool {
    Path::new(&format!(
        "/etc/systemd/system/node-plane-leased-{service}.service"
    ))
    .exists()
}

/// Lets the lease supervisor drop expired users before the container starts.
pub(crate) fn prepare_leased(service: &str) -> Result<(), String> {
    if !leased(service) {
        return Ok(());
    }
    crate::intents::supervisor::prepare(service, &crate::intents::journal_path())
}

/// Hands the new container to the lease supervisor.
pub(crate) fn leased_deploy(service: &str) -> Result<(), String> {
    crate::intents::scheduler::refresh(
        &crate::intents::leases::System,
        Path::new(crate::intents::scheduler::UNITS),
        service,
    )
    .map(|_| ())
}

/// Waits for a new container to fail on its config, if it does.
pub(crate) fn settle(setting: Setting<'_>, seconds: u64) {
    let seconds = setting("NODE_PLANE_DEPLOY_SETTLE_SECONDS", "")
        .parse()
        .unwrap_or(seconds);
    std::thread::sleep(Duration::from_secs(seconds));
}

/// `chmod 0755`, best effort: the containers read their config through it.
pub(crate) fn open_directory(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o755));
}

/// Runs the subcommand when the agent was started as `container-deploy …`.
pub(crate) fn run(args: &[String]) -> Option<i32> {
    let (first, rest) = args.split_first()?;
    if first != "container-deploy" {
        return None;
    }
    let result = match rest {
        [service] if service == "xray" => super::xray::deploy::deploy(&process_setting),
        [service] if service == "awg" => super::awg::deploy::deploy(&process_setting),
        _ => Err("usage: container-deploy xray|awg".into()),
    };
    Some(match result {
        Ok(text) => {
            println!("{text}");
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
    #[test]
    fn deployment_identities_are_version_4_uuids() {
        let id = super::uuid4().unwrap();
        let parts: Vec<&str> = id.split('-').collect();
        assert_eq!(
            parts.iter().map(|part| part.len()).collect::<Vec<_>>(),
            [8, 4, 4, 4, 12]
        );
        assert!(parts[2].starts_with('4'));
        assert!(matches!(&parts[3][..1], "8" | "9" | "a" | "b"));
        assert_ne!(id, super::uuid4().unwrap());
    }
}
