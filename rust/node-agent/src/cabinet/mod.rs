//! The web cabinet's files, served by this node's relay.
//!
//! The controller sends the cabinet's build with its release version. Each
//! version gets its own directory under the cabinet's root, written whole
//! before `current` points to it, so the relay never serves half of one;
//! the older versions go after the switch. The relay learns the directory
//! from an environment file of its unit, read only when the relay starts:
//! the first files restart it, a new version needs nothing more. The files
//! are public, as the cabinet is; only their paths and types are checked.
use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Component, Path, PathBuf};

use tonic::Status;

use crate::agent::v1::{ApplyCabinetRequest, CabinetState};
use crate::relay::{self, Host};

/// The most a build may hold; today's is under 2 MiB.
const MAX_BYTES: usize = 32 << 20;
const MAX_FILES: usize = 2000;
const SUFFIXES: &[&str] = &[
    "html",
    "js",
    "css",
    "json",
    "svg",
    "png",
    "ico",
    "woff",
    "woff2",
    "txt",
    "webmanifest",
];

pub(crate) struct Paths {
    /// One directory per version and `current`, the one served.
    pub root: PathBuf,
    /// The relay's, with its unit's extra environment for the cabinet.
    pub relay: relay::Paths,
}

impl Paths {
    pub(crate) fn host() -> Self {
        Self {
            root: PathBuf::from("/var/lib/node-plane-cabinet"),
            relay: relay::Paths::host(),
        }
    }
}

fn valid_version(version: &str) -> bool {
    !version.is_empty()
        && version.len() <= 64
        && !version.starts_with('.')
        && version
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'-' | b'+'))
}

fn valid_path(path: &str) -> bool {
    let suffix = Path::new(path).extension().and_then(|s| s.to_str());
    path.len() <= 512
        && !path.contains('\0')
        && suffix.is_some_and(|s| SUFFIXES.contains(&s))
        && Path::new(path)
            .components()
            .all(|part| matches!(part, Component::Normal(_)))
}

/// The request's files, checked: plain relative paths of the build's types,
/// a page among them, within the size limits.
pub(crate) fn check(request: &ApplyCabinetRequest) -> Result<(), String> {
    if !valid_version(&request.version) {
        return Err("cabinet_version_invalid".into());
    }
    let total: usize = request.files.iter().map(|f| f.content.len()).sum();
    if request.files.len() > MAX_FILES || total > MAX_BYTES {
        return Err("cabinet_files_too_large".into());
    }
    if request.files.iter().any(|f| !valid_path(&f.path)) {
        return Err("cabinet_files_invalid".into());
    }
    if !request.files.iter().any(|f| f.path == "index.html") {
        return Err("cabinet_files_invalid".into());
    }
    Ok(())
}

fn error(path: &Path) -> impl Fn(std::io::Error) -> String + '_ {
    move |e| format!("{}: {e}", path.display())
}

fn served(paths: &Paths) -> String {
    fs::read_link(paths.root.join("current"))
        .ok()
        .and_then(|target| target.file_name().map(|n| n.to_string_lossy().into_owned()))
        .unwrap_or_default()
}

pub(crate) fn state(paths: &Paths) -> CabinetState {
    CabinetState {
        version: served(paths),
    }
}

/// Writes the version's files, points `current` at them and has the relay
/// serve them.
pub(crate) fn apply(
    host: &dyn Host,
    paths: &Paths,
    request: &ApplyCabinetRequest,
) -> Result<CabinetState, String> {
    check(request)?;
    if !paths.relay.unit.exists() {
        return Err("cabinet_needs_relay".into());
    }
    fs::create_dir_all(&paths.root).map_err(error(&paths.root))?;
    fs::set_permissions(&paths.root, fs::Permissions::from_mode(0o755))
        .map_err(error(&paths.root))?;
    let target = paths.root.join(&request.version);
    if served(paths) != request.version || !target.join("index.html").is_file() {
        let staging = paths.root.join(format!(".{}.tmp", request.version));
        let _ = fs::remove_dir_all(&staging);
        for file in &request.files {
            let path = staging.join(&file.path);
            if let Some(dir) = path.parent() {
                fs::create_dir_all(dir).map_err(error(dir))?;
            }
            fs::write(&path, &file.content).map_err(error(&path))?;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).map_err(error(&path))?;
        }
        // Directories the relay's user may enter, whatever the umask.
        for entry in walk(&staging) {
            fs::set_permissions(&entry, fs::Permissions::from_mode(0o755))
                .map_err(error(&entry))?;
        }
        let _ = fs::remove_dir_all(&target);
        fs::rename(&staging, &target).map_err(error(&target))?;
        let link = paths.root.join(".current.tmp");
        let _ = fs::remove_file(&link);
        symlink(&request.version, &link).map_err(error(&link))?;
        fs::rename(&link, paths.root.join("current")).map_err(error(&paths.root))?;
    }
    // Only the version served stays.
    if let Ok(entries) = fs::read_dir(&paths.root) {
        for entry in entries.flatten() {
            let name = entry.file_name();
            if name != *request.version && name != "current" {
                let _ = fs::remove_dir_all(entry.path());
            }
        }
    }
    let env = format!(
        "# Written by node-plane-agent: the web cabinet beside the relay.\n\
         NODE_PLANE_RELAY_CABINET_DIR={}\n",
        paths.root.join("current").display()
    );
    let changed = relay::write(&paths.relay.cabinet_env, env.as_bytes(), 0o644)?
        | relay::refresh_unit(host, &paths.relay)?;
    if changed && !host.systemctl(&["try-restart", relay::SERVICE])? {
        return Err("relay_start_failed".into());
    }
    Ok(state(paths))
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

/// The relay stops serving the cabinet, and its files go.
pub(crate) fn remove(host: &dyn Host, paths: &Paths) -> Result<CabinetState, String> {
    let env = &paths.relay.cabinet_env;
    let had = env.exists();
    match fs::remove_file(env) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(format!("{}: {e}", env.display())),
    }
    if had && paths.relay.unit.exists() {
        host.systemctl(&["try-restart", relay::SERVICE])?;
    }
    match fs::remove_dir_all(&paths.root) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(format!("{}: {e}", paths.root.display())),
    }
    Ok(state(paths))
}

pub(crate) fn status(error: String) -> Status {
    if (error.starts_with("cabinet_") || error.starts_with("relay_")) && !error.contains(' ') {
        Status::invalid_argument(error)
    } else {
        Status::internal(error)
    }
}

#[cfg(test)]
mod tests;
