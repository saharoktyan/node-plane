//! The runtime of temporary access: the protocol container the lease
//! supervisor owns, and expired identities kept out of its config.
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use serde_json::Value;

use crate::protocols::awg::peer_sync::atomic_write;
use crate::protocols::awg::peers::split_peer;
use crate::protocols::lock_config;
use crate::protocols::xray::user_sync::write_config;
use crate::runtime::files::env_value;

/// node.env; the lease commands of tests point elsewhere.
pub(crate) fn node_env() -> PathBuf {
    std::env::var_os("NODE_PLANE_NODE_ENV")
        .map(PathBuf::from)
        .unwrap_or_else(|| "/etc/node-plane/node.env".into())
}

/// What a host command returned.
pub(crate) struct Output {
    pub(crate) code: Option<i32>,
    pub(crate) stdout: String,
}

/// The commands lease handling runs on the host: Docker and systemd.
/// Tests answer them instead.
pub(crate) trait Host {
    /// Runs `args`, killed after `timeout` seconds when given.
    fn run(&self, args: &[&str], timeout: Option<u64>) -> Result<Output, String>;

    /// Stdout of a command that must succeed.
    fn check(&self, args: &[&str], timeout: Option<u64>) -> Result<String, String> {
        let output = self.run(args, timeout)?;
        if output.code != Some(0) {
            return Err(format!("{} failed", args.join(" ")));
        }
        Ok(output.stdout)
    }

    fn succeeds(&self, args: &[&str], timeout: Option<u64>) -> bool {
        self.run(args, timeout)
            .is_ok_and(|output| output.code == Some(0))
    }

    /// Whether `program` is on PATH, like `shutil.which`.
    fn installed(&self, program: &str) -> bool {
        use std::os::unix::fs::PermissionsExt;
        std::env::var_os("PATH").is_some_and(|path| {
            std::env::split_paths(&path).any(|dir| {
                fs::metadata(dir.join(program))
                    .is_ok_and(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
            })
        })
    }
}

/// The real host.
pub(crate) struct System;

impl Host for System {
    fn run(&self, args: &[&str], timeout: Option<u64>) -> Result<Output, String> {
        let mut command = match timeout {
            Some(seconds) => {
                let mut command = Command::new("timeout");
                command.arg(format!("{seconds}s")).args(args);
                command
            }
            None => {
                let mut command = Command::new(args[0]);
                command.args(&args[1..]);
                command
            }
        };
        let output = command
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()
            .map_err(|err| format!("{}: {err}", args[0]))?;
        Ok(Output {
            code: output.status.code(),
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        })
    }
}

/// Where node.env puts the protocol's config and container.
pub(crate) struct RuntimeSettings {
    pub(crate) config: PathBuf,
    pub(crate) container: String,
    pub(crate) server_key: String,
}

impl RuntimeSettings {
    pub(crate) fn read(protocol: &str) -> Result<Self, String> {
        Self::from_env(
            protocol,
            &fs::read_to_string(node_env()).unwrap_or_default(),
        )
    }

    pub(crate) fn from_env(protocol: &str, raw: &str) -> Result<Self, String> {
        let value = |key: &str, default: &str| {
            env_value(raw, key)
                .filter(|value| !value.is_empty())
                .unwrap_or_else(|| default.to_string())
        };
        let (config, container) = match protocol {
            "awg" => (
                value(
                    "AWG_CONFIG",
                    "/opt/node-plane-runtime/amnezia-awg/data/wg0.conf",
                ),
                value("AWG_CONTAINER_NAME", "amnezia-awg"),
            ),
            "xray" => (
                value("XRAY_CONFIG", "/opt/node-plane-runtime/xray/config.json"),
                value("XRAY_CONTAINER_NAME", "xray"),
            ),
            _ => return Err("invalid leased protocol".into()),
        };
        let server_key = value("SERVER_KEY", "");
        let name = |text: &str, max: usize, dot: bool| {
            text.len() <= max
                && text.bytes().enumerate().all(|(index, b)| {
                    b.is_ascii_alphanumeric()
                        || (index > 0 && (matches!(b, b'_' | b'-') || (dot && b == b'.')))
                })
        };
        if !Path::new(&config).is_absolute()
            || container.is_empty()
            || !name(&container, 128, true)
            || !name(&server_key, 64, false)
        {
            return Err("invalid leased runtime environment".into());
        }
        Ok(Self {
            config: config.into(),
            container,
            server_key,
        })
    }

    fn destination(protocol: &str) -> &'static str {
        if protocol == "xray" {
            "/etc/xray"
        } else {
            "/opt/amnezia/awg"
        }
    }
}

/// The protocol's container, when its name, ID and config mount are ours.
pub(crate) fn container(
    host: &dyn Host,
    protocol: &str,
    settings: &RuntimeSettings,
) -> Result<Value, String> {
    let unavailable = || "leased runtime is unavailable".to_string();
    let output = host
        .check(
            &["docker", "container", "inspect", &settings.container],
            Some(15),
        )
        .map_err(|_| unavailable())?;
    let records: Value = serde_json::from_str(&output).map_err(|_| unavailable())?;
    let [record] = records.as_array().map(Vec::as_slice).unwrap_or_default() else {
        return Err(unavailable());
    };
    let source = settings
        .config
        .parent()
        .unwrap_or(Path::new("/"))
        .to_string_lossy();
    let id = record["Id"].as_str().unwrap_or_default();
    let owned = record["Name"].as_str() == Some(&format!("/{}", settings.container))
        && id.len() == 64
        && id.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
        && record["Mounts"].as_array().is_some_and(|mounts| {
            mounts.iter().any(|mount| {
                mount["Type"] == "bind"
                    && mount["Source"].as_str() == Some(&source)
                    && mount["Destination"] == RuntimeSettings::destination(protocol)
            })
        })
        && record["State"]["Running"].is_boolean();
    if !owned {
        return Err("leased container ownership is unconfirmed".into());
    }
    Ok(record.clone())
}

/// The ID of a checked container record.
pub(crate) fn id(record: &Value) -> &str {
    record["Id"].as_str().unwrap_or_default()
}

pub(crate) fn running(record: &Value) -> bool {
    record["State"]["Running"] == true
}

/// Whether expiry is enforced now: the timer and its scan healthy, and the
/// supervisor running the protocol container, which Docker must not restart
/// on its own.
pub(crate) fn enforcement_ready(
    host: &dyn Host,
    protocol: &str,
    settings: &RuntimeSettings,
) -> bool {
    let units = [
        ("is-enabled", "node-plane-lease-expiry.timer".to_string()),
        ("is-active", "node-plane-lease-expiry.timer".to_string()),
        ("is-active", "node-plane-lease-expiry.service".to_string()),
        (
            "is-enabled",
            format!("node-plane-leased-{protocol}.service"),
        ),
        ("is-active", format!("node-plane-leased-{protocol}.service")),
    ];
    if !units
        .iter()
        .all(|(action, unit)| host.succeeds(&["systemctl", action, "--quiet", unit], Some(15)))
    {
        return false;
    }
    // `is-failed` answers 1 for a unit that has not failed.
    let failed = host.run(
        &[
            "systemctl",
            "is-failed",
            "--quiet",
            "node-plane-lease-expiry.service",
        ],
        Some(15),
    );
    if !failed.is_ok_and(|output| output.code == Some(1)) {
        return false;
    }
    container(host, protocol, settings).is_ok_and(|record| {
        running(&record) && record["HostConfig"]["RestartPolicy"]["Name"] == "no"
    })
}

/// Removes journaled temporary identities from a stopped container's config.
pub(crate) fn prune_config(
    protocol: &str,
    config: &Path,
    names: &[String],
    server_key: &str,
) -> Result<(), String> {
    if names.is_empty() {
        return Ok(());
    }
    if names
        .iter()
        .any(|name| !super::profile::temporary_name(name))
    {
        return Err("invalid temporary identity".into());
    }
    if config.is_symlink() || config.ancestors().skip(1).any(Path::is_symlink) {
        return Err("unsafe leased configuration path".into());
    }
    let _lock = lock_config(config)?;
    let original =
        fs::read_to_string(config).map_err(|err| format!("{}: {err}", config.display()))?;
    match protocol {
        "xray" => {
            let mut value: Value = serde_json::from_str(&original)
                .map_err(|err| format!("{}: {err}", config.display()))?;
            let mut changed = false;
            for inbound in value["inbounds"]
                .as_array_mut()
                .ok_or("Xray inbounds are missing")?
            {
                if inbound["protocol"] != "vless" {
                    continue;
                }
                let clients = inbound["settings"]["clients"]
                    .as_array_mut()
                    .ok_or("Xray clients are missing")?;
                let before = clients.len();
                clients.retain(|client| {
                    !names.iter().any(|name| {
                        client["email"].as_str() == Some(name)
                            || client["name"].as_str() == Some(name)
                    })
                });
                changed |= clients.len() != before;
            }
            if changed {
                write_config(config, &value)?;
            }
        }
        "awg" => {
            let mut updated = original.clone();
            for name in names {
                let peer = if server_key.is_empty() {
                    name.clone()
                } else {
                    format!("{server_key}-{name}")
                };
                updated = split_peer(&updated, &peer).0;
            }
            if updated != original {
                atomic_write(config, &updated)?;
            }
        }
        _ => return Err("invalid leased protocol".into()),
    }
    Ok(())
}

/// A host that answers like the lease tests' Docker and systemd, and
/// records what ran.
#[cfg(test)]
pub(crate) mod fake {
    use std::cell::RefCell;
    use std::path::PathBuf;

    use serde_json::json;

    use super::{Host, Output};

    pub(crate) struct FakeHost {
        pub(crate) config: PathBuf,
        pub(crate) running: RefCell<bool>,
        pub(crate) policy: RefCell<String>,
        pub(crate) failed: bool,
        pub(crate) bad_mount: bool,
        pub(crate) calls: RefCell<Vec<Vec<String>>>,
        /// The config when the container started.
        pub(crate) started: RefCell<Option<String>>,
        /// Called on `docker wait`, while the supervisor waits.
        pub(crate) on_wait: Box<dyn Fn()>,
        /// Commands that fail.
        pub(crate) failing: Vec<Vec<String>>,
    }

    impl FakeHost {
        pub(crate) fn new(config: PathBuf) -> Self {
            Self {
                config,
                running: RefCell::new(false),
                policy: RefCell::new("no".into()),
                failed: false,
                bad_mount: false,
                calls: RefCell::new(Vec::new()),
                started: RefCell::new(None),
                on_wait: Box::new(|| {}),
                failing: Vec::new(),
            }
        }

        pub(crate) fn ran(&self, args: &[&str]) -> bool {
            self.calls.borrow().iter().any(|call| call == args)
        }
    }

    /// The exit code and stdout of a command.
    type Answer = Box<dyn Fn(&[&str]) -> (i32, String)>;

    /// A host whose commands an answer function handles; it records them.
    pub(crate) struct Scripted {
        pub(crate) answer: Answer,
        pub(crate) programs: Vec<&'static str>,
        pub(crate) calls: RefCell<Vec<Vec<String>>>,
    }

    impl Scripted {
        pub(crate) fn new(answer: impl Fn(&[&str]) -> (i32, String) + 'static) -> Self {
            Self {
                answer: Box::new(answer),
                programs: vec!["docker", "ufw"],
                calls: RefCell::new(Vec::new()),
            }
        }

        /// The recorded commands that start with `prefix`.
        pub(crate) fn ran(&self, prefix: &[&str]) -> Vec<Vec<String>> {
            self.calls
                .borrow()
                .iter()
                .filter(|call| {
                    call.iter()
                        .map(String::as_str)
                        .take(prefix.len())
                        .eq(prefix.iter().copied())
                })
                .cloned()
                .collect()
        }
    }

    impl Host for Scripted {
        fn run(&self, args: &[&str], _: Option<u64>) -> Result<Output, String> {
            self.calls
                .borrow_mut()
                .push(args.iter().map(|arg| arg.to_string()).collect());
            let (code, stdout) = (self.answer)(args);
            Ok(Output {
                code: Some(code),
                stdout,
            })
        }

        fn installed(&self, program: &str) -> bool {
            self.programs.contains(&program)
        }
    }

    impl Host for FakeHost {
        fn run(&self, args: &[&str], _: Option<u64>) -> Result<Output, String> {
            let call: Vec<String> = args.iter().map(|arg| arg.to_string()).collect();
            self.calls.borrow_mut().push(call.clone());
            let mut stdout = String::new();
            let mut code = 0;
            if self.failing.contains(&call) {
                code = 3;
            } else if args.starts_with(&["docker", "container", "inspect"]) {
                let awg = self.config.extension().is_some_and(|ext| ext == "conf");
                let source = if self.bad_mount {
                    "/unrelated".to_string()
                } else {
                    self.config.parent().unwrap().to_string_lossy().into_owned()
                };
                stdout = json!([{"Id": "a".repeat(64), "Name": "/test",
                    "State": {"Running": *self.running.borrow()},
                    "HostConfig": {"RestartPolicy": {"Name": *self.policy.borrow()}},
                    "Mounts": [{"Type": "bind", "Source": source,
                        "Destination": if awg { "/opt/amnezia/awg" } else { "/etc/xray" }}]}])
                .to_string();
            } else if args.starts_with(&["docker", "update"]) {
                *self.policy.borrow_mut() = "no".into();
            } else if args.starts_with(&["docker", "start"]) {
                *self.started.borrow_mut() = std::fs::read_to_string(&self.config).ok();
                *self.running.borrow_mut() = true;
            } else if args.starts_with(&["docker", "wait"]) {
                (self.on_wait)();
            } else if args.starts_with(&["systemctl", "is-failed"]) {
                code = if self.failed { 0 } else { 1 };
            }
            Ok(Output {
                code: Some(code),
                stdout,
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::fake::FakeHost;
    use super::*;

    fn settings(config: &Path) -> RuntimeSettings {
        RuntimeSettings {
            config: config.to_path_buf(),
            container: "test".into(),
            server_key: "lv1".into(),
        }
    }

    #[test]
    fn runtime_settings_follow_node_env_and_are_checked() {
        let settings =
            RuntimeSettings::from_env("awg", "SERVER_KEY=n1\nAWG_CONTAINER_NAME=awg\n").unwrap();
        assert_eq!(settings.container, "awg");
        assert_eq!(settings.server_key, "n1");
        assert_eq!(
            settings.config,
            Path::new("/opt/node-plane-runtime/amnezia-awg/data/wg0.conf")
        );
        assert!(RuntimeSettings::from_env("xray", "XRAY_CONFIG=relative.json\n").is_err());
        assert!(RuntimeSettings::from_env("xray", "SERVER_KEY=-bad\n").is_err());
        assert!(RuntimeSettings::from_env("other", "").is_err());
    }

    #[test]
    fn enforcement_needs_the_timer_the_supervisor_and_an_owned_running_container() {
        let config = crate::protocols::test_dir("lease-ready").join("config.json");
        let ready = |change: &dyn Fn(&mut FakeHost)| {
            let mut host = FakeHost::new(config.clone());
            *host.running.borrow_mut() = true;
            change(&mut host);
            enforcement_ready(&host, "xray", &settings(&config))
        };
        assert!(ready(&|_| {}));
        assert!(!ready(&|host| *host.running.borrow_mut() = false));
        assert!(!ready(
            &|host| *host.policy.borrow_mut() = "unless-stopped".into()
        ));
        assert!(!ready(&|host| host.failed = true));
        assert!(!ready(&|host| host.bad_mount = true));
        // A restarting or stopped expiry worker is not ready.
        assert!(!ready(&|host| host.failing = vec![
            [
                "systemctl",
                "is-active",
                "--quiet",
                "node-plane-lease-expiry.service"
            ]
            .map(String::from)
            .to_vec()
        ]));
    }

    #[test]
    fn only_temporary_identities_leave_the_config() {
        let dir = crate::protocols::test_dir("lease-prune");
        let name = format!("tmp_{}", "b".repeat(32));
        let awg = dir.join("wg0.conf");
        let text = format!(
            "[Interface]\nPrivateKey = s\n\n# n1-{name}\n[Peer]\nPublicKey = t\n\n# n1-keep\n[Peer]\nPublicKey = k\n"
        );
        fs::write(&awg, &text).unwrap();
        prune_config("awg", &awg, std::slice::from_ref(&name), "n1").unwrap();
        let pruned = fs::read_to_string(&awg).unwrap();
        assert!(!pruned.contains(&name) && pruned.contains("# n1-keep"));
        assert!(prune_config("awg", &awg, &["keep".to_string()], "n1").is_err());

        let xray = dir.join("config.json");
        let config = serde_json::json!({"inbounds": [{"protocol": "vless", "settings": {"clients": [
            {"email": name, "id": "a"}, {"email": "keep", "id": "b"}]}}]});
        fs::write(&xray, config.to_string()).unwrap();
        prune_config("xray", &xray, std::slice::from_ref(&name), "").unwrap();
        let pruned: Value = serde_json::from_str(&fs::read_to_string(&xray).unwrap()).unwrap();
        assert_eq!(
            pruned["inbounds"][0]["settings"]["clients"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
    }
}
