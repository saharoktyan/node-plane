//! The web cabinet's relay as a service of this node.
//!
//! The relay runs from the agent's own binary (`node-plane-agent
//! relay-serve`), so it updates with the agent, but as the unprivileged
//! `node-plane-relay` user in a sandboxed unit: it faces the internet. Its
//! key stays in its state directory; the controller pins the public half,
//! and the relay pins the controller's link key that comes with every apply.
use std::fs;
use std::os::unix::fs::{PermissionsExt, chown};
use std::path::{Path, PathBuf};
use std::process::Command;

use node_plane_relay::keys;
use tonic::Status;

use crate::agent::v1::{ApplyRelayRequest, RelayState};

pub(crate) const USER: &str = "node-plane-relay";
pub(crate) const SERVICE: &str = "node-plane-relay.service";
const VERSION: &str = env!("NODE_PLANE_BINARY_VERSION");

/// Where the relay lives on the host; tests use a directory of their own.
pub(crate) struct Paths {
    pub unit: PathBuf,
    pub env: PathBuf,
    pub state: PathBuf,
    /// The agent version that last started the relay.
    pub version: PathBuf,
    /// Where the web cabinet's files are, when the node serves them
    /// (`crate::cabinet`); optional for the unit.
    pub cabinet_env: PathBuf,
}

impl Paths {
    pub(crate) fn host() -> Self {
        Self {
            unit: PathBuf::from("/etc/systemd/system").join(SERVICE),
            env: PathBuf::from("/etc/node-plane/relay.env"),
            state: PathBuf::from("/var/lib/node-plane-relay"),
            version: PathBuf::from("/etc/node-plane/relay.version"),
            cabinet_env: PathBuf::from("/etc/node-plane/relay-cabinet.env"),
        }
    }
}

/// What the relay needs from the host.
pub(crate) trait Host {
    /// The relay user's uid and gid, created when missing.
    fn user(&self) -> Result<(u32, u32), String>;
    fn systemctl(&self, args: &[&str]) -> Result<bool, String>;
    /// Allow (or with `false` delete) the TCP port in an active ufw.
    fn firewall(&self, port: u16, allow: bool);
}

pub(crate) struct System;

impl Host for System {
    fn user(&self) -> Result<(u32, u32), String> {
        let id = |flag: &str| -> Option<u32> {
            let output = Command::new("id").args([flag, USER]).output().ok()?;
            output.status.success().then_some(())?;
            String::from_utf8_lossy(&output.stdout).trim().parse().ok()
        };
        if id("-u").is_none() {
            let status = Command::new("useradd")
                .args(["--system", "--no-create-home", "--home-dir", "/nonexistent"])
                .args(["--shell", "/usr/sbin/nologin", USER])
                .status()
                .map_err(|e| format!("useradd: {e}"))?;
            if !status.success() {
                return Err(format!("could not create the {USER} user"));
            }
        }
        match (id("-u"), id("-g")) {
            (Some(uid), Some(gid)) => Ok((uid, gid)),
            _ => Err(format!("the {USER} user has no id")),
        }
    }

    fn systemctl(&self, args: &[&str]) -> Result<bool, String> {
        Command::new("systemctl")
            .args(args)
            .output()
            .map(|output| output.status.success())
            .map_err(|e| format!("systemctl: {e}"))
    }

    fn firewall(&self, port: u16, allow: bool) {
        let active = Command::new("ufw")
            .arg("status")
            .env("LC_ALL", "C")
            .output()
            .is_ok_and(|o| String::from_utf8_lossy(&o.stdout).starts_with("Status: active"));
        if active {
            let rule = format!("{port}/tcp");
            let mut command = Command::new("ufw");
            if allow {
                command.args(["allow", &rule, "comment", "Node Plane relay"]);
            } else {
                command.args(["delete", "allow", &rule]);
            }
            let _ = command.output();
        }
    }
}

/// Settings as the controller sent them, checked.
#[derive(Debug, PartialEq)]
pub(crate) struct Settings {
    domain: String,
    port: u16,
    tls: Tls,
    link_key: Vec<u8>,
}

#[derive(Debug, PartialEq)]
enum Tls {
    Acme { contact: String },
    Files { certificate: String, key: String },
    Proxy,
}

fn valid_domain(domain: &str) -> bool {
    domain.len() <= 253
        && domain.contains('.')
        && domain.split('.').all(|label| {
            !label.is_empty()
                && label.len() <= 63
                && !label.starts_with('-')
                && !label.ends_with('-')
                && label
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || c == b'-')
        })
}

/// A value that can sit in a systemd environment file as it is.
fn plain(value: &str) -> bool {
    value
        .bytes()
        .all(|c| c.is_ascii_graphic() && !matches!(c, b'"' | b'\'' | b'\\' | b'$' | b'`'))
}

impl Settings {
    pub(crate) fn parse(request: ApplyRelayRequest) -> Result<Self, String> {
        let domain = request.domain.trim().to_ascii_lowercase();
        if !valid_domain(&domain) {
            return Err("relay_domain_invalid".into());
        }
        let port = u16::try_from(request.port)
            .ok()
            .filter(|port| *port != 0)
            .ok_or("relay_port_invalid")?;
        let tls = match request.tls.as_str() {
            "acme" if port != 443 => return Err("relay_acme_needs_port_443".into()),
            "acme"
                if !request.acme_contact.is_empty()
                    && !(request.acme_contact.contains('@') && plain(&request.acme_contact)) =>
            {
                return Err("relay_acme_contact_invalid".into());
            }
            "acme" => Tls::Acme {
                contact: request.acme_contact,
            },
            "files" => {
                let absolute = |path: &str| path.starts_with('/') && plain(path);
                if !absolute(&request.certificate_path) || !absolute(&request.key_path) {
                    return Err("relay_certificate_path_invalid".into());
                }
                Tls::Files {
                    certificate: request.certificate_path,
                    key: request.key_path,
                }
            }
            "proxy" => Tls::Proxy,
            _ => return Err("relay_tls_invalid".into()),
        };
        if request.link_key.len() != keys::KEY_LEN {
            return Err("relay_link_key_invalid".into());
        }
        Ok(Self {
            domain,
            port,
            tls,
            link_key: request.link_key,
        })
    }

    fn env(&self, state: &Path) -> String {
        // Behind a proxy only the proxy on this host may reach the relay.
        let host = if self.tls == Tls::Proxy {
            "127.0.0.1"
        } else {
            "0.0.0.0"
        };
        let mut env = format!(
            "# Written by node-plane-agent from the controller's relay settings.\n\
             NODE_PLANE_RELAY_LISTEN={host}:{}\n\
             NODE_PLANE_RELAY_STATE_DIR={}\n\
             NODE_PLANE_RELAY_DOMAIN={}\n",
            self.port,
            state.display(),
            self.domain
        );
        match &self.tls {
            Tls::Acme { contact } => {
                env += "NODE_PLANE_RELAY_TLS=acme\n";
                env += &format!("NODE_PLANE_RELAY_ACME_CONTACT={contact}\n");
            }
            Tls::Files { certificate, key } => {
                env += "NODE_PLANE_RELAY_TLS=files\n";
                env += &format!("NODE_PLANE_RELAY_TLS_CERT={certificate}\n");
                env += &format!("NODE_PLANE_RELAY_TLS_KEY={key}\n");
            }
            Tls::Proxy => env += "NODE_PLANE_RELAY_TLS=proxy\n",
        }
        env
    }

    /// Whether the relay faces the internet on its port.
    fn public(&self) -> bool {
        self.tls != Tls::Proxy
    }
}

/// The relay's unit. It may bind low ports and reach the network, and
/// little else: no other files, no privileges, no other address families.
fn unit(paths: &Path, env: &Path, cabinet_env: &Path) -> String {
    format!(
        "# Written by node-plane-agent; see docs/agent.md.
[Unit]
Description=Node Plane relay (web cabinet)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={USER}
Group={USER}
EnvironmentFile={env}
EnvironmentFile=-{cabinet_env}
ExecStart=/usr/local/bin/node-plane-agent relay-serve
Restart=always
RestartSec=3
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
UMask=0077
ProtectSystem=strict
ReadWritePaths={state}
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectHostname=yes
ProtectClock=yes
ProtectProc=invisible
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service

[Install]
WantedBy=multi-user.target
",
        env = env.display(),
        cabinet_env = cabinet_env.display(),
        state = paths.display(),
    )
}

/// Writes `content` when it differs; true when it changed.
pub(crate) fn write(path: &Path, content: &[u8], mode: u32) -> Result<bool, String> {
    if fs::read(path).is_ok_and(|current| current == content) {
        return Ok(false);
    }
    let error = |e: std::io::Error| format!("{}: {e}", path.display());
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).map_err(error)?;
    }
    let temporary = path.with_extension("tmp");
    fs::write(&temporary, content).map_err(error)?;
    fs::set_permissions(&temporary, fs::Permissions::from_mode(mode)).map_err(error)?;
    fs::rename(&temporary, path).map_err(error)?;
    Ok(true)
}

fn listen_port(env: &Path) -> Option<u16> {
    let text = fs::read_to_string(env).ok()?;
    let listen = text
        .lines()
        .find_map(|line| line.strip_prefix("NODE_PLANE_RELAY_LISTEN="))?;
    if listen.starts_with("127.") {
        return None;
    }
    listen.rsplit_once(':')?.1.parse().ok()
}

pub(crate) fn apply(
    host: &dyn Host,
    paths: &Paths,
    settings: &Settings,
) -> Result<RelayState, String> {
    let (uid, gid) = host.user()?;
    let own = |path: &Path| {
        chown(path, Some(uid), Some(gid)).map_err(|e| format!("{}: {e}", path.display()))
    };
    fs::create_dir_all(&paths.state).map_err(|e| format!("{}: {e}", paths.state.display()))?;
    fs::set_permissions(&paths.state, fs::Permissions::from_mode(0o700))
        .map_err(|e| format!("{}: {e}", paths.state.display()))?;
    own(&paths.state)?;
    let key_path = paths.state.join("relay.key");
    keys::load_or_create(&key_path)?;
    own(&key_path)?;
    let pinned = paths.state.join("link.pub");
    write(&pinned, keys::hex(&settings.link_key).as_bytes(), 0o600)?;
    own(&pinned)?;

    let previous_port = listen_port(&paths.env);
    let changed = write(&paths.env, settings.env(&paths.state).as_bytes(), 0o600)?
        | write(
            &paths.unit,
            unit(&paths.state, &paths.env, &paths.cabinet_env).as_bytes(),
            0o644,
        )?;
    if let Some(port) =
        previous_port.filter(|port| Some(*port) != settings.public().then_some(settings.port))
    {
        host.firewall(port, false);
    }
    if settings.public() {
        host.firewall(settings.port, true);
    }
    host.systemctl(&["daemon-reload"])?;
    host.systemctl(&["enable", SERVICE])?;
    // A new controller key needs no restart: the relay reads it at Attach.
    let action = if changed { "restart" } else { "start" };
    if !host.systemctl(&[action, SERVICE])? {
        return Err("relay_start_failed".into());
    }
    let _ = write(&paths.version, VERSION.as_bytes(), 0o644);
    Ok(state(host, paths))
}

/// Brings an installed relay's unit up to this agent's; true when it
/// changed, and systemd has read it again.
pub(crate) fn refresh_unit(host: &dyn Host, paths: &Paths) -> Result<bool, String> {
    let changed = write(
        &paths.unit,
        unit(&paths.state, &paths.env, &paths.cabinet_env).as_bytes(),
        0o644,
    )?;
    if changed {
        host.systemctl(&["daemon-reload"])?;
    }
    Ok(changed)
}

pub(crate) fn remove(host: &dyn Host, paths: &Paths) -> Result<RelayState, String> {
    if let Some(port) = listen_port(&paths.env) {
        host.firewall(port, false);
    }
    if paths.unit.exists() {
        host.systemctl(&["disable", "--now", SERVICE])?;
    }
    for path in [&paths.unit, &paths.env, &paths.version] {
        match fs::remove_file(path) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(format!("{}: {e}", path.display())),
        }
    }
    host.systemctl(&["daemon-reload"])?;
    // Keys and certificates go with the service; a new relay gets new ones.
    match fs::remove_dir_all(&paths.state) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(format!("{}: {e}", paths.state.display())),
    }
    Ok(state(host, paths))
}

pub(crate) fn state(host: &dyn Host, paths: &Paths) -> RelayState {
    let installed = paths.unit.exists();
    let relay_key = fs::read(paths.state.join("relay.key"))
        .ok()
        .and_then(|bytes| ring::signature::Ed25519KeyPair::from_pkcs8(&bytes).ok())
        .map(|key| keys::public(&key))
        .unwrap_or_default();
    let status: serde_json::Value = fs::read(paths.state.join("status.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default();
    RelayState {
        installed,
        active: installed
            && host
                .systemctl(&["is-active", "--quiet", SERVICE])
                .unwrap_or(false),
        relay_key,
        link: status["link"]
            .as_str()
            .unwrap_or("disconnected")
            .to_string(),
        link_since: status["since"].as_u64().unwrap_or(0),
    }
}

/// After an agent update the relay still runs the old binary; restart it
/// into the new one.
pub(crate) fn follow_update(host: &dyn Host, paths: &Paths) {
    if !paths.unit.exists() || fs::read_to_string(&paths.version).is_ok_and(|v| v == VERSION) {
        return;
    }
    if host.systemctl(&["try-restart", SERVICE]).unwrap_or(false) {
        let _ = write(&paths.version, VERSION.as_bytes(), 0o644);
    }
}

pub(crate) fn status(error: String) -> Status {
    if error.starts_with("relay_") && !error.contains(' ') {
        Status::invalid_argument(error)
    } else {
        Status::internal(error)
    }
}

/// `node-plane-agent relay-serve`: the relay process of the unit above.
pub(crate) async fn run(args: &[String]) -> Option<i32> {
    if args.first().map(String::as_str) != Some("relay-serve") {
        return None;
    }
    let result = tokio::select! {
        result = node_plane_relay::serve::from_env() => result,
        _ = tokio::signal::ctrl_c() => Ok(()),
    };
    Some(match result {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("{error}");
            1
        }
    })
}

#[cfg(test)]
mod tests;
