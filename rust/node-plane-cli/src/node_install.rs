//! Runs the controller's one-time agent install command on a server over a
//! single SSH login (Server setup → Auto). The command is the same one Manual
//! shows; the controller then enrolls the agent by its token.
use std::sync::Arc;

use anyhow::{Context, Result, bail, ensure};
use serde_json::Value;
use zeroize::Zeroizing;

use crate::ssh::{Interaction, SshSession};

const INSTALLER: &str = "/tmp/node-plane-agent-installer.sh";

/// How the installer gets root on the server.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Privilege {
    Root,
    PasswordlessSudo,
    /// `sudo -S` reads the password from standard input.
    SudoPassword,
}

fn quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}

/// The shell command for `enrollment` (the controller's reply).
pub fn script(enrollment: &Value, privilege: Privilege) -> Result<String> {
    let url = enrollment["installer"]["url"]
        .as_str()
        .context("The controller did not return the installer")?;
    let sha256 = enrollment["installer"]["sha256"]
        .as_str()
        .filter(|value| value.len() == 64 && value.bytes().all(|c| c.is_ascii_hexdigit()))
        .context("The controller did not return the installer checksum")?;
    ensure!(
        url.starts_with("https://"),
        "The installer must be downloaded over HTTPS"
    );
    let arguments = enrollment["arguments"]
        .as_array()
        .context("The controller did not return the install arguments")?
        .iter()
        .map(|value| value.as_str().map(quote))
        .collect::<Option<Vec<_>>>()
        .context("Invalid install arguments")?;
    let run = match privilege {
        Privilege::Root => "sh",
        Privilege::PasswordlessSudo => "sudo -n sh",
        Privilege::SudoPassword => "sudo -S -p '' sh",
    };
    Ok(format!(
        "curl -fsSLo {INSTALLER} {} && echo '{sha256}  {INSTALLER}' | sha256sum -c - >/dev/null && {run} {INSTALLER} {}",
        quote(url),
        arguments.join(" ")
    ))
}

/// Installs the agent and returns the installer's last words.
pub async fn install(
    session: &mut SshSession,
    enrollment: &Value,
    interaction: Arc<dyn Interaction>,
) -> Result<String> {
    let quiet = |_: crate::ssh::OutputStream, _: &[u8]| {};
    let privilege = if session.run("test \"$(id -u)\" = 0", None, quiet).await? == 0 {
        Privilege::Root
    } else if session.run("sudo -n true", None, quiet).await? == 0 {
        Privilege::PasswordlessSudo
    } else {
        Privilege::SudoPassword
    };
    let password = if privilege == Privilege::SudoPassword {
        let user = session.ssh_user.clone();
        let prompt = interaction.clone();
        let password = tokio::task::spawn_blocking(move || {
            prompt.input(
                "sudo password",
                &format!(
                    "The agent installs as root. Enter the sudo password of {user}; it is used once and not saved."
                ),
                true,
            )
        })
        .await??;
        let mut input = Zeroizing::new(password.as_bytes().to_vec());
        input.push(b'\n');
        Some(input)
    } else {
        None
    };
    let command = script(enrollment, privilege)?;
    let mut output = Vec::new();
    let code = session
        .run(
            &command,
            password.as_ref().map(|value| value.as_slice()),
            |_, data| output.extend_from_slice(data),
        )
        .await
        .context("The install command did not finish; check the server")?;
    let text = String::from_utf8_lossy(&output);
    let tail = text
        .lines()
        .filter(|line| !line.trim().is_empty())
        .rev()
        .take(3)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect::<Vec<_>>()
        .join("\n");
    if code != 0 {
        bail!("The agent installation failed (exit {code}):\n{tail}");
    }
    Ok(tail)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn enrollment() -> Value {
        json!({
            "installer": {"url": "https://github.com/x/y/releases/download/v1/node-plane-agent-installer.sh",
                          "sha256": "b".repeat(64)},
            "arguments": ["--node-key", "n1", "--token", "1.abc", "--port", "50061"],
        })
    }

    #[test]
    fn the_installer_is_checked_before_it_runs_as_root() {
        let root = script(&enrollment(), Privilege::Root).unwrap();
        assert_eq!(
            root,
            format!(
                "curl -fsSLo /tmp/node-plane-agent-installer.sh 'https://github.com/x/y/releases/download/v1/node-plane-agent-installer.sh' && echo '{}  /tmp/node-plane-agent-installer.sh' | sha256sum -c - >/dev/null && sh /tmp/node-plane-agent-installer.sh '--node-key' 'n1' '--token' '1.abc' '--port' '50061'",
                "b".repeat(64)
            )
        );
        assert!(
            script(&enrollment(), Privilege::SudoPassword)
                .unwrap()
                .contains("&& sudo -S -p '' sh /tmp/")
        );
        assert!(
            script(&enrollment(), Privilege::PasswordlessSudo)
                .unwrap()
                .contains("&& sudo -n sh /tmp/")
        );
    }

    #[test]
    fn unexpected_replies_run_nothing() {
        let mut plain = enrollment();
        plain["installer"]["url"] = "http://example/installer.sh".into();
        assert!(script(&plain, Privilege::Root).is_err());
        let mut checksum = enrollment();
        checksum["installer"]["sha256"] = "x; rm -rf /".into();
        assert!(script(&checksum, Privilege::Root).is_err());
        let mut quoted = enrollment();
        quoted["arguments"] = json!(["it's"]);
        assert!(
            script(&quoted, Privilege::Root)
                .unwrap()
                .ends_with("'it'\"'\"'s'")
        );
    }
}
