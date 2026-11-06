//! Runtime entry points: the names journals, RPCs and earlier releases run.
//!
//! Each starts a subcommand of this binary from a script built into it:
//! node.env is sourced first and the probe settings keep an agent without the subcommand from
//! starting its server. Nothing executable is delivered to the node; the
//! wrapper files earlier releases left in the runtime are removed.
//! `node-plane-agent entry NAME [ARG...]` runs one by hand.
use std::fs;
use std::path::Path;
use std::process::Command;

/// Sources node.env and exports it, then the subcommand.
const EXPORTED: &str = "set -euo pipefail\nset -a\nsource \"${NODE_PLANE_NODE_ENV:-/etc/node-plane/node.env}\"\nset +a\n";
const AGENT: &str = "NODE_AGENT_CONFIG_PATH=/nonexistent/node-plane-agent-probe.toml NODE_AGENT_LISTEN_ADDR=probe \\\nexec \"${NODE_PLANE_AGENT_BIN:-/usr/local/bin/node-plane-agent}\"";

/// Entry point names and their scripts, after the shared prelude.
const ENTRIES: [(&str, Prelude, &str); 13] = [
    ("init-xray.sh", Prelude::None, " xray-config init \"$@\""),
    ("sync-xray.sh", Prelude::None, " xray-config public \"$@\""),
    (
        "deploy-xray.sh",
        Prelude::Exported,
        " container-deploy xray",
    ),
    ("init-awg.sh", Prelude::Exported, " awg-config init"),
    ("deploy-awg.sh", Prelude::Exported, " container-deploy awg"),
    (
        "regenerate-awg-entropy.sh",
        Prelude::Exported,
        " awg-config regenerate",
    ),
    (
        "apply-node-settings.sh",
        Prelude::Exported,
        " node-settings apply \"${1:-}\" \"${2:-}\" \"${3:-}\" \"${4:-}\" \"${5:-/assets}\" \"${6:-false}\" \"${7:-false}\"",
    ),
    (
        "inspect-profile.sh",
        Prelude::Exported,
        " inspect-profile \"$@\"",
    ),
    ("show-awg-entropy.sh", Prelude::Exported, " awg-entropy"),
    (
        "xray-add-user-existing.sh",
        Prelude::Xray("[[ $# -lt 2 || $# -gt 3 ]]", "<name> <uuid> [short_id]"),
        " xray-user add \"${XRAY_CONFIG:-/opt/node-plane-runtime/xray/config.json}\" \"${XRAY_CONTAINER_NAME:-node-plane-xray}\" \"${XRAY_INBOUND_TCP_TAG:-reality-tcp}\" \"${XRAY_INBOUND_XHTTP_TAG:-reality-xhttp}\" \"$1\" \"$2\"",
    ),
    (
        "xray-del-user.sh",
        Prelude::Xray("[[ $# -ne 1 ]]", "<name>"),
        " xray-user delete \"${XRAY_CONFIG:-/opt/node-plane-runtime/xray/config.json}\" \"${XRAY_CONTAINER_NAME:-node-plane-xray}\" \"${XRAY_INBOUND_TCP_TAG:-reality-tcp}\" \"${XRAY_INBOUND_XHTTP_TAG:-reality-xhttp}\" \"$1\"",
    ),
    (
        "awg-add-user.sh",
        Prelude::Peer(true),
        " awg-peer add \"$1\"",
    ),
    (
        "awg-del-user.sh",
        Prelude::Peer(false),
        " awg-peer revoke \"$1\"",
    ),
];

/// Files earlier releases delivered next to these: a wrapper, the AWG image
/// the node built and the AWG profile tool (`protocols/awg/profile`).
const FORMER: [&str; 3] = [
    "apply-profile-intent.py",
    "amnezia-awg/Dockerfile",
    "awg_profile.py",
];

#[derive(Clone, Copy)]
enum Prelude {
    None,
    Exported,
    /// node.env sourced without export, and an argument check.
    Xray(&'static str, &'static str),
    /// node.env exported and one profile name; `true` for a new key.
    Peer(bool),
}

fn script(name: &str) -> Option<String> {
    let (_, prelude, command) = ENTRIES.iter().find(|(entry, ..)| *entry == name)?;
    let usage = |check: &str, usage: &str| {
        format!("if {check}; then\n  echo \"Usage: $0 {usage}\" >&2\n  exit 1\nfi\n")
    };
    let prelude = match prelude {
        Prelude::None => "set -euo pipefail\n".to_string(),
        Prelude::Exported => EXPORTED.to_string(),
        Prelude::Xray(check, text) => format!(
            "set -euo pipefail\nsource \"${{NODE_PLANE_NODE_ENV:-/etc/node-plane/node.env}}\"\n{}",
            usage(check, text)
        ),
        Prelude::Peer(new_key) => format!(
            "{}{EXPORTED}{}",
            if *new_key { "umask 077\n" } else { "" },
            usage("[[ $# -ne 1 || -z \"$1\" ]]", "<name>")
        ),
    };
    Some(format!("{prelude}{AGENT}{command}\n"))
}

/// The command of entry point `name`; add its arguments. `runtime` is the
/// runtime root the caller resolved; tests put fake entry points there.
pub(crate) fn command(runtime: &Path, name: &str) -> Result<Command, String> {
    #[cfg(test)]
    {
        let fake = runtime.join(name);
        if fake.is_file() {
            return Ok(Command::new(fake));
        }
    }
    #[cfg(not(test))]
    let _ = runtime;
    let script = script(name).ok_or_else(|| format!("unknown runtime entry point: {name}"))?;
    let mut command = Command::new("bash");
    command.arg("-c").arg(script).arg(name);
    // Entry points start this exact binary, unless the agent itself was
    // given another one.
    if std::env::var_os("NODE_PLANE_AGENT_BIN").is_none() {
        command.env(
            "NODE_PLANE_AGENT_BIN",
            std::env::current_exe().unwrap_or_default(),
        );
    }
    Ok(command)
}

/// Runs the subcommand when the agent was started as `entry`.
pub(crate) fn run(args: &[String]) -> Option<i32> {
    if args.first().map(String::as_str) != Some("entry") {
        return None;
    }
    let Some(name) = args.get(1) else {
        eprintln!("usage: node-plane-agent entry NAME [ARG...]");
        return Some(2);
    };
    let status = command(Path::new("/opt/node-plane-runtime"), name).and_then(|mut command| {
        command
            .args(&args[2..])
            .status()
            .map_err(|err| format!("{name}: {err}"))
    });
    Some(match status {
        Ok(status) => status.code().unwrap_or(1),
        Err(err) => {
            eprintln!("{err}");
            2
        }
    })
}

/// Removes the wrapper files earlier releases delivered to `runtime`.
pub(crate) fn remove_delivered(runtime: &Path) {
    for name in ENTRIES.iter().map(|(name, ..)| *name).chain(FORMER) {
        let path = runtime.join(name);
        if path.is_file() {
            let _ = fs::remove_file(&path);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn entry_points_start_the_wrappers_subcommands() {
        assert_eq!(
            script("deploy-awg.sh").unwrap(),
            "set -euo pipefail\nset -a\nsource \"${NODE_PLANE_NODE_ENV:-/etc/node-plane/node.env}\"\nset +a\n\
             NODE_AGENT_CONFIG_PATH=/nonexistent/node-plane-agent-probe.toml NODE_AGENT_LISTEN_ADDR=probe \\\n\
             exec \"${NODE_PLANE_AGENT_BIN:-/usr/local/bin/node-plane-agent}\" container-deploy awg\n"
        );
        let add = script("awg-add-user.sh").unwrap();
        assert!(add.starts_with("umask 077\nset -euo pipefail\nset -a\n"));
        assert!(add.ends_with(" awg-peer add \"$1\"\n"));
        assert!(add.contains("echo \"Usage: $0 <name>\" >&2"));
        let xray = script("xray-del-user.sh").unwrap();
        assert!(xray.contains(
            "source \"${NODE_PLANE_NODE_ENV:-/etc/node-plane/node.env}\"\nif [[ $# -ne 1 ]]; then"
        ));
        assert!(!xray.contains("set -a"));
        assert!(script("helper.sh").is_none());
    }

    #[test]
    fn every_script_is_valid_shell() {
        for (name, ..) in ENTRIES {
            let status = Command::new("bash")
                .args(["-n", "-c", &script(name).unwrap()])
                .status()
                .unwrap();
            assert!(status.success(), "{name}");
        }
    }

    #[test]
    fn a_usage_error_names_the_entry_point() {
        let runtime = Path::new("/nonexistent");
        let output = command(runtime, "xray-del-user.sh")
            .unwrap()
            .env("NODE_PLANE_NODE_ENV", "/nonexistent/node.env")
            .output()
            .unwrap();
        // node.env is missing here; the source fails before the check.
        assert!(!output.status.success());
        assert!(command(runtime, "helper.sh").is_err());
    }

    #[test]
    fn delivered_wrappers_are_removed_and_other_files_kept() {
        let dir = std::env::temp_dir().join(format!("np-entry-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        for name in [
            "deploy-xray.sh",
            "apply-profile-intent.py",
            "awg_profile.py",
            "VERSION",
        ] {
            fs::write(dir.join(name), "x").unwrap();
        }
        remove_delivered(&dir);
        assert!(!dir.join("deploy-xray.sh").exists());
        assert!(!dir.join("apply-profile-intent.py").exists());
        assert!(!dir.join("awg_profile.py").exists());
        assert!(dir.join("VERSION").exists());
        fs::remove_dir_all(&dir).unwrap();
    }
}
