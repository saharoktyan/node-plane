//! VPN protocol adapters. Protocol differences live only here.
pub(crate) mod awg;
pub(crate) mod inspection;
pub(crate) mod traffic;
pub(crate) mod xray;

use std::fs;
use std::path::Path;

/// A node.env setting with its default. The agent reads node.env; the
/// subcommands read the environment their shell entry point sourced it into.
pub(crate) type Setting<'a> = &'a (dyn Fn(&str, &str) -> String + Sync);

/// Settings of a subcommand started by a shell entry point.
pub(crate) fn process_setting(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

pub(crate) fn sha256_hex(data: &[u8]) -> String {
    ring::digest::digest(&ring::digest::SHA256, data)
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// Profile and peer names: what the controller issues and Xray emails.
pub(crate) fn valid_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-'))
}

/// Holds `<config>.lock` exclusively, the lock every writer of a protocol
/// config takes, until dropped.
pub(crate) fn lock_config(config: &Path) -> Result<fs::File, String> {
    let mut name = config.as_os_str().to_owned();
    name.push(".lock");
    let lock = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&name)
        .map_err(|err| format!("config lock unavailable: {err}"))?;
    lock.lock()
        .map_err(|err| format!("config lock unavailable: {err}"))?;
    Ok(lock)
}

/// A fresh scratch directory for a test.
#[cfg(test)]
pub(crate) fn test_dir(name: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("np-protocols-{}-{name}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    dir
}

#[cfg(test)]
mod tests {
    #[test]
    fn sha256_is_lowercase_hex() {
        // hashlib.sha256(b"server").hexdigest()
        assert_eq!(
            super::sha256_hex(b"server"),
            "b3eacd33433b31b5252351032c9b3e7a2e7aa7738d5decdf0dd6c62680853c06"
        );
    }
}
