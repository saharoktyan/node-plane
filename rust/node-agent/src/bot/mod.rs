//! The Telegram bot as a service of this node.
//!
//! The bot runs here when Telegram is out of the controller's reach, or the
//! controller should stay private: it talks to Telegram from this node and
//! to the controller through the relay, signing every request with a key
//! of its own. The key is made here and never leaves; the controller pins
//! its public half (`BotKey`) before the first `ApplyBot`.
//!
//! The controller sends the bot's code from its release, the requirements
//! with their hashes and uv. Each version is installed whole, its virtual
//! environment built by uv from PyPI, before `current` points to it; the
//! unit then runs it as the unprivileged `node-plane-bot` user.
use std::fs;
use std::os::unix::fs::{PermissionsExt, chown, symlink};
use std::path::{Component, Path, PathBuf};
use std::process::Command;

use ring::rand::{SecureRandom, SystemRandom};
use ring::signature::{Ed25519KeyPair, KeyPair};
use tonic::Status;

use crate::agent::v1::{ApplyBotRequest, BotState};
use crate::relay::write;

pub(crate) const USER: &str = "node-plane-bot";
pub(crate) const SERVICE: &str = "node-plane-bot.service";
/// The code is a few MiB; uv, compressed, under 20.
const MAX_BYTES: usize = 40 << 20;
const MAX_FILES: usize = 2000;
/// uv inflated; today's is under 50 MiB.
const MAX_UV: usize = 128 << 20;

pub(crate) struct Paths {
    /// Versions, `current`, the key, the bot's state and uv's caches.
    pub root: PathBuf,
    pub env: PathBuf,
    pub unit: PathBuf,
}

impl Paths {
    pub(crate) fn host() -> Self {
        Self {
            root: PathBuf::from("/var/lib/node-plane-bot"),
            env: PathBuf::from("/etc/node-plane/bot.env"),
            unit: PathBuf::from("/etc/systemd/system").join(SERVICE),
        }
    }

    fn key(&self) -> PathBuf {
        self.root.join("bot.key")
    }

    fn state(&self) -> PathBuf {
        self.root.join("state")
    }
}

/// What the bot needs from the host.
pub(crate) trait Host {
    /// The bot user's uid and gid, created when missing.
    fn user(&self) -> Result<(u32, u32), String>;
    fn systemctl(&self, args: &[&str]) -> Result<bool, String>;
    /// Runs uv with its arguments and environment; the error is its output.
    fn uv(&self, uv: &Path, args: &[&str], env: &[(&str, PathBuf)]) -> Result<(), String>;
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
        crate::relay::System.systemctl(args)
    }

    fn uv(&self, uv: &Path, args: &[&str], env: &[(&str, PathBuf)]) -> Result<(), String> {
        let output = Command::new(uv)
            .args(args)
            .env("UV_NO_CONFIG", "1")
            .envs(env.iter().map(|(k, v)| (*k, v.as_os_str())))
            .output()
            .map_err(|e| format!("uv: {e}"))?;
        if output.status.success() {
            Ok(())
        } else {
            let text = String::from_utf8_lossy(&output.stderr);
            let tail: String = text.lines().rev().take(5).collect::<Vec<_>>().join(" | ");
            Err(format!("bot_install_failed: {tail}"))
        }
    }
}

use crate::relay::Host as _;

/// Starts with a digit, so a version never names `tools`, `state` or another
/// directory beside the versions.
fn valid_version(version: &str) -> bool {
    version.len() <= 64
        && version.starts_with(|c: char| c.is_ascii_digit())
        && version
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'-' | b'+'))
}

fn valid_path(path: &str) -> bool {
    let plain = path.len() <= 512
        && !path.contains('\0')
        && Path::new(path)
            .components()
            .all(|part| matches!(part, Component::Normal(_)));
    let known = matches!(
        path,
        "VERSION" | "RELEASE_CHANNEL" | "BUILD_COMMIT" | "requirements.txt" | "uv.zlib"
    ) || path.starts_with("app/")
        && Path::new(path)
            .extension()
            .is_some_and(|s| matches!(s.to_str(), Some("py" | "json" | "txt" | "md")));
    plain && known
}

/// A value that can sit in a systemd environment file as it is.
fn plain(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|c| c.is_ascii_graphic() && !matches!(c, b'"' | b'\'' | b'\\' | b'$' | b'`'))
}

fn valid_token(token: &str) -> bool {
    token.split_once(':').is_some_and(|(id, secret)| {
        !id.is_empty()
            && id.len() <= 20
            && id.bytes().all(|c| c.is_ascii_digit())
            && secret.len() >= 20
            && secret.len() <= 128
            && secret
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-'))
    })
}

/// The request, checked: the code's own kinds of files, a usable token and
/// an HTTPS address to reach the controller at.
pub(crate) fn check(request: &ApplyBotRequest) -> Result<(), String> {
    if !valid_version(&request.version) {
        return Err("bot_version_invalid".into());
    }
    let total: usize = request.files.iter().map(|f| f.content.len()).sum();
    if request.files.len() > MAX_FILES || total > MAX_BYTES {
        return Err("bot_files_too_large".into());
    }
    let has = |name: &str| request.files.iter().any(|f| f.path == name);
    if request.files.iter().any(|f| !valid_path(&f.path))
        || !has("app/telegram_client/main.py")
        || !has("requirements.txt")
        || !has("uv.zlib")
    {
        return Err("bot_files_invalid".into());
    }
    if !valid_token(&request.bot_token) {
        return Err("bot_token_invalid".into());
    }
    if request.key_id.len() != 32
        || !request
            .key_id
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    {
        return Err("bot_key_id_invalid".into());
    }
    if !request.api_url.starts_with("https://") || !plain(&request.api_url) {
        return Err("bot_api_url_invalid".into());
    }
    if !request.relay_ca.is_empty() && !request.relay_ca.starts_with("-----BEGIN CERTIFICATE-----")
    {
        return Err("bot_relay_ca_invalid".into());
    }
    Ok(())
}

fn error(path: &Path) -> impl Fn(std::io::Error) -> String + '_ {
    move |e| format!("{}: {e}", path.display())
}

fn installed(paths: &Paths) -> String {
    fs::read_link(paths.root.join("current"))
        .ok()
        .and_then(|target| target.file_name().map(|n| n.to_string_lossy().into_owned()))
        .unwrap_or_default()
}

fn public_key(paths: &Paths) -> Vec<u8> {
    fs::read(paths.key())
        .ok()
        .filter(|seed| seed.len() == 32)
        .and_then(|seed| Ed25519KeyPair::from_seed_unchecked(&seed).ok())
        .map(|key| key.public_key().as_ref().to_vec())
        .unwrap_or_default()
}

pub(crate) fn state(host: &dyn Host, paths: &Paths) -> BotState {
    BotState {
        version: installed(paths),
        public_key: public_key(paths),
        active: paths.unit.exists()
            && host
                .systemctl(&["is-active", "--quiet", SERVICE])
                .unwrap_or(false),
    }
}

/// The bot's key, made once; only the bot's user reads it.
pub(crate) fn key(host: &dyn Host, paths: &Paths) -> Result<BotState, String> {
    let (uid, gid) = host.user()?;
    fs::create_dir_all(&paths.root).map_err(error(&paths.root))?;
    fs::set_permissions(&paths.root, fs::Permissions::from_mode(0o755))
        .map_err(error(&paths.root))?;
    let path = paths.key();
    if public_key(paths).is_empty() {
        let mut seed = [0u8; 32];
        SystemRandom::new()
            .fill(&mut seed)
            .map_err(|_| "no randomness for the bot key".to_string())?;
        let temporary = path.with_extension("tmp");
        fs::write(&temporary, seed).map_err(error(&temporary))?;
        seed.fill(0);
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o400))
            .map_err(error(&temporary))?;
        chown(&temporary, Some(uid), Some(gid)).map_err(error(&temporary))?;
        fs::rename(&temporary, &path).map_err(error(&path))?;
    }
    Ok(state(host, paths))
}

/// Writes the version's code, builds its environment with uv and points
/// `current` at it.
fn install(host: &dyn Host, paths: &Paths, request: &ApplyBotRequest) -> Result<(), String> {
    let target = paths.root.join(&request.version);
    if installed(paths) == request.version && target.join(".venv/bin/python").exists() {
        return Ok(());
    }
    let staging = paths.root.join(format!(".{}.tmp", request.version));
    let _ = fs::remove_dir_all(&staging);
    let mut uv = None;
    for file in &request.files {
        if file.path == "uv.zlib" {
            uv = Some(&file.content);
            continue;
        }
        let path = staging.join(&file.path);
        if let Some(dir) = path.parent() {
            fs::create_dir_all(dir).map_err(error(dir))?;
        }
        fs::write(&path, &file.content).map_err(error(&path))?;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).map_err(error(&path))?;
    }
    let tools = paths.root.join("tools");
    fs::create_dir_all(&tools).map_err(error(&tools))?;
    let uv_path = tools.join("uv");
    let binary = miniz_oxide::inflate::decompress_to_vec_zlib_with_limit(
        uv.ok_or("bot_files_invalid")?,
        MAX_UV,
    )
    .map_err(|_| "bot_files_invalid".to_string())?;
    write(&uv_path, &binary, 0o755)?;
    let env = [
        ("UV_CACHE_DIR", paths.root.join("cache")),
        ("UV_PYTHON_INSTALL_DIR", paths.root.join("python")),
    ];
    // The system's Python when it fits the code, else one uv brings.
    let venv = staging.join(".venv");
    host.uv(
        &uv_path,
        &[
            "venv",
            "--quiet",
            "--python",
            ">=3.11,<3.13",
            &venv.to_string_lossy(),
        ],
        &env,
    )?;
    host.uv(
        &uv_path,
        &[
            "pip",
            "install",
            "--quiet",
            "--require-hashes",
            "--python",
            &venv.join("bin/python").to_string_lossy(),
            "-r",
            &staging.join("requirements.txt").to_string_lossy(),
        ],
        &env,
    )?;
    // Readable by the bot's user, whatever the umask; writable by no one but root.
    for entry in walk(&staging) {
        fs::set_permissions(&entry, fs::Permissions::from_mode(0o755)).map_err(error(&entry))?;
    }
    let _ = fs::remove_dir_all(&target);
    fs::rename(&staging, &target).map_err(error(&target))?;
    let link = paths.root.join(".current.tmp");
    let _ = fs::remove_file(&link);
    symlink(&request.version, &link).map_err(error(&link))?;
    fs::rename(&link, paths.root.join("current")).map_err(error(&paths.root))?;
    Ok(())
}

/// The directories under `root`, itself included.
fn walk(root: &Path) -> Vec<PathBuf> {
    let mut out = vec![root.to_path_buf()];
    let mut index = 0;
    while index < out.len() {
        if let Ok(entries) = fs::read_dir(&out[index]) {
            for entry in entries.flatten() {
                if entry.file_type().is_ok_and(|t| t.is_dir()) {
                    out.push(entry.path());
                }
            }
        }
        index += 1;
    }
    out
}

fn env(paths: &Paths, request: &ApplyBotRequest) -> String {
    let current = paths.root.join("current");
    let mut env = format!(
        "# Written by node-plane-agent: the Telegram bot run by this node.\n\
         BOT_TOKEN={}\n\
         NODE_PLANE_BACKEND_URL={}\n\
         NODE_PLANE_BOT_KEY_FILE={}\n\
         NODE_PLANE_BOT_KEY_ID={}\n\
         NODE_PLANE_APP_DIR={}\n\
         NODE_PLANE_SHARED_DIR={}\n\
         PYTHONPATH={}\n",
        request.bot_token,
        request.api_url.trim_end_matches('/'),
        paths.key().display(),
        request.key_id,
        current.display(),
        paths.state().display(),
        current.join("app").display(),
    );
    if !request.relay_ca.is_empty() {
        env += &format!(
            "NODE_PLANE_RELAY_CA_FILE={}\n",
            paths.root.join("relay-ca.pem").display()
        );
    }
    env
}

/// The bot's unit: the network and its own state, little else.
fn unit(paths: &Paths) -> String {
    let current = paths.root.join("current");
    format!(
        "# Written by node-plane-agent; see docs/agent.md.
[Unit]
Description=Node Plane Telegram bot (through the relay)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={USER}
Group={USER}
EnvironmentFile={env}
WorkingDirectory={current}
ExecStart={current}/.venv/bin/python -m telegram_client.main
Restart=always
RestartSec=3
RestartPreventExitStatus=78
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
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service

[Install]
WantedBy=multi-user.target
",
        env = paths.env.display(),
        current = current.display(),
        state = paths.state().display(),
    )
}

/// Installs the version, writes the bot's settings and runs it.
pub(crate) fn apply(
    host: &dyn Host,
    paths: &Paths,
    request: &ApplyBotRequest,
) -> Result<BotState, String> {
    check(request)?;
    if public_key(paths).is_empty() {
        // The controller pins the key first, so the bot is never unknown to it.
        return Err("bot_key_missing".into());
    }
    let (uid, gid) = host.user()?;
    install(host, paths, request)?;
    let state_dir = paths.state();
    fs::create_dir_all(&state_dir).map_err(error(&state_dir))?;
    fs::set_permissions(&state_dir, fs::Permissions::from_mode(0o700))
        .map_err(error(&state_dir))?;
    chown(&state_dir, Some(uid), Some(gid)).map_err(error(&state_dir))?;
    let ca = paths.root.join("relay-ca.pem");
    if request.relay_ca.is_empty() {
        let _ = fs::remove_file(&ca);
    } else {
        write(&ca, request.relay_ca.as_bytes(), 0o644)?;
    }
    let changed = write(&paths.env, env(paths, request).as_bytes(), 0o600)?
        | write(&paths.unit, unit(paths).as_bytes(), 0o644)?;
    host.systemctl(&["daemon-reload"])?;
    host.systemctl(&["enable", SERVICE])?;
    // A new version changes `current` under the running bot: restart it too.
    let action = if changed || !state(host, paths).active {
        "restart"
    } else {
        "try-restart"
    };
    if !host.systemctl(&[action, SERVICE])? {
        return Err("bot_start_failed".into());
    }
    if let Ok(entries) = fs::read_dir(&paths.root) {
        for entry in entries.flatten() {
            let name = entry.file_name();
            let name = name.to_string_lossy();
            let version_dir = entry.file_type().is_ok_and(|t| t.is_dir())
                && valid_version(&name)
                && name != request.version.as_str();
            if version_dir || name.starts_with('.') && name.ends_with(".tmp") {
                let _ = fs::remove_dir_all(entry.path());
            }
        }
    }
    Ok(state(host, paths))
}

/// Stops the bot; its code, key and state go.
pub(crate) fn remove(host: &dyn Host, paths: &Paths) -> Result<BotState, String> {
    if paths.unit.exists() {
        host.systemctl(&["disable", "--now", SERVICE])?;
    }
    for path in [&paths.unit, &paths.env] {
        match fs::remove_file(path) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(format!("{}: {e}", path.display())),
        }
    }
    host.systemctl(&["daemon-reload"])?;
    match fs::remove_dir_all(&paths.root) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(format!("{}: {e}", paths.root.display())),
    }
    Ok(state(host, paths))
}

pub(crate) fn status(error: String) -> Status {
    if error.starts_with("bot_") && !error.contains(' ') {
        Status::invalid_argument(error)
    } else if let Some(code) = error.strip_prefix("bot_install_failed: ") {
        // uv's last lines say why, such as PyPI out of reach.
        Status::failed_precondition(format!("bot_install_failed: {code}"))
    } else {
        Status::internal(error)
    }
}

#[cfg(test)]
mod tests;
