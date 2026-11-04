//! Download, check and swap the agent binary, with a rollback guard.
//!
//! The current binary is kept as `<binary>.previous`. Before the swap a
//! transient systemd timer is armed that runs that previous binary as
//! `update-guard` a few minutes later: unless the new agent has started and
//! confirmed the update by then, the guard puts the previous binary back and
//! restarts the service.
use std::cmp::Ordering;
use std::fs;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use super::release::{self, ARCHIVE, BINARY, SIGNATURE, STATEMENT};

const STATEMENT_LIMIT: u64 = 4096;
const SIGNATURE_LIMIT: u64 = 1024;
const ARCHIVE_LIMIT: u64 = 64 * 1024 * 1024;
pub(crate) const GUARD_DELAY: &str = "180s";
const PENDING: &str = "pending";
const ROLLED_BACK: &str = "rolled-back";

pub(crate) type Fetch = fn(&str, &Path, u64) -> Result<(), String>;
pub(crate) type Schedule = fn(&Path, &Path, &Path) -> Result<(), String>;

pub(crate) struct Host {
    pub(crate) binary: PathBuf,
    pub(crate) stage: PathBuf,
    pub(crate) key: Option<Vec<u8>>,
    pub(crate) own: String,
    pub(crate) fetch: Fetch,
    pub(crate) schedule: Schedule,
}

#[derive(Debug, PartialEq)]
pub(crate) enum Outcome {
    Current,
    Updating,
}

pub(crate) fn previous(binary: &Path) -> PathBuf {
    suffixed(binary, ".previous")
}

fn suffixed(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(suffix);
    PathBuf::from(name)
}

fn valid_base(base: &str) -> bool {
    (base.starts_with("https://") || base.starts_with("http://"))
        && base.len() <= 512
        && !base.ends_with('/')
        && base
            .bytes()
            .all(|b| b.is_ascii_graphic() && !b"\"'\\`$".contains(&b))
}

/// Fetch, check and install `version` from `base`.
pub(crate) fn update(host: &Host, base: &str, version: &str) -> Result<Outcome, String> {
    if !valid_base(base) || !release::valid_version(version) {
        return Err("agent_update_invalid".into());
    }
    match release::compare(version, &host.own) {
        Some(Ordering::Equal) => return Ok(Outcome::Current),
        Some(Ordering::Less) => return Err("agent_update_downgrade".into()),
        Some(Ordering::Greater) => {}
        None => return Err("agent_update_version_unknown".into()),
    }
    let key = host.key.as_deref().ok_or("agent_update_key_missing")?;
    fs::create_dir_all(&host.stage).map_err(|err| format!("agent_update_stage: {err}"))?;
    fs::set_permissions(&host.stage, fs::Permissions::from_mode(0o700))
        .map_err(|err| format!("agent_update_stage: {err}"))?;
    if host.stage.join(PENDING).exists() {
        return Err("agent_update_in_progress".into());
    }
    if read_line(&host.stage.join(ROLLED_BACK)).as_deref() == Some(version) {
        return Err("agent_update_rolled_back".into());
    }
    let new = prepare(host, key, base, version)?;
    activate(host, &new, version)?;
    Ok(Outcome::Updating)
}

fn prepare(host: &Host, key: &[u8], base: &str, version: &str) -> Result<PathBuf, String> {
    let work = host.stage.join("download");
    let _ = fs::remove_dir_all(&work);
    fs::create_dir(&work).map_err(|err| format!("agent_update_stage: {err}"))?;
    let get = |name: &str, limit: u64| -> Result<Vec<u8>, String> {
        let path = work.join(name);
        (host.fetch)(&format!("{base}/{name}"), &path, limit)?;
        let data = fs::read(&path).map_err(|_| "agent_update_download_failed".to_string())?;
        if data.len() as u64 > limit {
            return Err("agent_update_download_failed".into());
        }
        Ok(data)
    };
    let statement = get(STATEMENT, STATEMENT_LIMIT)?;
    let signature = get(SIGNATURE, SIGNATURE_LIMIT)?;
    let release = release::verify(&statement, &signature, key)?;
    if release.version != version {
        return Err("agent_update_version_mismatch".into());
    }
    let archive = get(ARCHIVE, ARCHIVE_LIMIT)?;
    if crate::protocols::sha256_hex(&archive) != release.sha256 {
        return Err("agent_update_checksum_mismatch".into());
    }
    let unpacked = work.join("unpacked");
    fs::create_dir(&unpacked).map_err(|err| format!("agent_update_stage: {err}"))?;
    let status = Command::new("tar")
        .arg("-xzf")
        .arg(work.join(ARCHIVE))
        .arg("-C")
        .arg(&unpacked)
        .arg("--")
        .arg(BINARY)
        .status()
        .map_err(|_| "agent_update_unpack_failed".to_string())?;
    let new = unpacked.join(BINARY);
    let meta = fs::symlink_metadata(&new).map_err(|_| "agent_update_unpack_failed".to_string())?;
    if !status.success() || !meta.file_type().is_file() {
        return Err("agent_update_unpack_failed".into());
    }
    fs::set_permissions(&new, fs::Permissions::from_mode(0o755))
        .map_err(|_| "agent_update_unpack_failed".to_string())?;
    // The new binary must run on this host and report the signed version.
    let output = Command::new(&new)
        .arg("version")
        .output()
        .map_err(|_| "agent_update_self_check_failed".to_string())?;
    if !output.status.success() || String::from_utf8_lossy(&output.stdout).trim() != version {
        return Err("agent_update_self_check_failed".into());
    }
    Ok(new)
}

fn copy_atomic(from: &Path, to: &Path) -> Result<(), String> {
    let temporary = suffixed(to, ".tmp");
    let data = fs::read(from).map_err(|err| format!("agent_update_install_failed: {err}"))?;
    let result = (|| {
        let mut file = fs::File::create(&temporary)?;
        file.write_all(&data)?;
        file.set_permissions(fs::Permissions::from_mode(0o755))?;
        file.sync_all()?;
        fs::rename(&temporary, to)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result.map_err(|err| format!("agent_update_install_failed: {err}"))
}

fn activate(host: &Host, new: &Path, version: &str) -> Result<(), String> {
    let previous = previous(&host.binary);
    copy_atomic(&host.binary, &previous)?;
    let pending = host.stage.join(PENDING);
    fs::write(&pending, format!("{version}\n"))
        .map_err(|err| format!("agent_update_stage: {err}"))?;
    if let Err(err) = (host.schedule)(&previous, &host.binary, &host.stage) {
        let _ = fs::remove_file(&pending);
        return Err(err);
    }
    if let Err(err) = copy_atomic(new, &host.binary) {
        // The guard finds nothing pending and leaves the binary alone.
        let _ = fs::remove_file(&pending);
        return Err(err);
    }
    let _ = fs::remove_dir_all(host.stage.join("download"));
    Ok(())
}

fn read_line(path: &Path) -> Option<String> {
    fs::read_to_string(path)
        .ok()
        .map(|text| text.trim().to_string())
        .filter(|text| !text.is_empty())
}

/// The started agent confirms an update to its own version.
pub(crate) fn confirm(stage: &Path, own: &str) -> bool {
    let pending = stage.join(PENDING);
    if read_line(&pending).as_deref() == Some(own) {
        let _ = fs::remove_file(stage.join(ROLLED_BACK));
        return fs::remove_file(pending).is_ok();
    }
    false
}

/// Put the previous binary back when the update was never confirmed.
pub(crate) fn guard(binary: &Path, stage: &Path) -> Result<Option<String>, String> {
    let pending = stage.join(PENDING);
    let Some(version) = read_line(&pending) else {
        return Ok(None);
    };
    copy_atomic(&previous(binary), binary)?;
    fs::write(stage.join(ROLLED_BACK), format!("{version}\n"))
        .map_err(|err| format!("agent_update_stage: {err}"))?;
    fs::remove_file(&pending).map_err(|err| format!("agent_update_stage: {err}"))?;
    Ok(Some(version))
}

/// The version the guard put back, if the last update was rolled back.
pub(crate) fn rolled_back(stage: &Path) -> Option<String> {
    read_line(&stage.join(ROLLED_BACK))
}

pub(crate) fn clear_rolled_back(stage: &Path) {
    let _ = fs::remove_file(stage.join(ROLLED_BACK));
}

pub(crate) fn fetch(url: &str, dest: &Path, limit: u64) -> Result<(), String> {
    let has = |tool: &str| {
        Command::new("sh")
            .args(["-c", &format!("command -v {tool} >/dev/null 2>&1")])
            .status()
            .is_ok_and(|status| status.success())
    };
    let mut command = if has("curl") {
        let mut command = Command::new("curl");
        command
            .args(["--fail", "--silent", "--show-error", "--location"])
            .args(["--proto", "=https,http", "--proto-redir", "=https,http"])
            .args(["--max-time", "300", "--max-filesize", &limit.to_string()])
            .arg("--output")
            .arg(dest)
            .arg(url);
        command
    } else if has("wget") {
        let mut command = Command::new("wget");
        command.args(["-q", "-T", "300", "-O"]).arg(dest).arg(url);
        command
    } else {
        return Err("agent_update_downloader_missing".into());
    };
    let output = command
        .output()
        .map_err(|_| "agent_update_download_failed".to_string())?;
    if !output.status.success() {
        return Err("agent_update_download_failed".into());
    }
    Ok(())
}

pub(crate) fn schedule(previous: &Path, binary: &Path, stage: &Path) -> Result<(), String> {
    let unit = format!(
        "node-plane-agent-update-guard-{}",
        chrono::Utc::now().timestamp()
    );
    let output = Command::new("systemd-run")
        .args(["--quiet", "--collect", "--unit", &unit])
        .arg(format!("--on-active={GUARD_DELAY}"))
        .arg(previous)
        .arg("update-guard")
        .arg(binary)
        .arg(stage)
        .output()
        .map_err(|_| "agent_update_guard_failed".to_string())?;
    if !output.status.success() {
        return Err("agent_update_guard_failed".into());
    }
    Ok(())
}
