//! Signed release statements and version order.
//!
//! The release workflow signs a short statement that names the agent version
//! and the SHA-256 of its archive. The agent installs only an archive that a
//! statement signed by the release key describes, so whoever asks for an
//! update, the controller included, can only pick a published release.
use std::cmp::Ordering;

use ring::signature::{ED25519, UnparsedPublicKey};

pub(crate) const TARGET: &str = "linux-amd64";
pub(crate) const ARCHIVE: &str = "node-plane-agent-linux-amd64.tar.gz";
pub(crate) const BINARY: &str = "node-plane-agent-linux-amd64";
pub(crate) const STATEMENT: &str = "node-plane-agent-linux-amd64.release";
pub(crate) const SIGNATURE: &str = "node-plane-agent-linux-amd64.release.sig";
const HEADER: &str = "node-plane-agent-release 1";

/// Raw Ed25519 public key of the releases, as hex; `build.rs` reads it from
/// `release-key.hex`. Without it the agent refuses self-updates.
const RELEASE_KEY: &str = match option_env!("NODE_PLANE_RELEASE_KEY") {
    Some(key) => key,
    None => "",
};

pub(crate) fn release_key() -> Option<Vec<u8>> {
    decode_hex(RELEASE_KEY.trim()).filter(|key| key.len() == 32)
}

#[derive(Debug, PartialEq)]
pub(crate) struct Release {
    pub(crate) version: String,
    pub(crate) sha256: String,
}

/// Check the signature first, then the exact statement layout.
pub(crate) fn verify(statement: &[u8], signature: &[u8], key: &[u8]) -> Result<Release, String> {
    UnparsedPublicKey::new(&ED25519, key)
        .verify(statement, signature)
        .map_err(|_| "agent_update_signature_invalid".to_string())?;
    parse(statement).ok_or_else(|| "agent_update_statement_invalid".to_string())
}

fn parse(statement: &[u8]) -> Option<Release> {
    let text = std::str::from_utf8(statement).ok()?;
    let mut lines = text.strip_suffix('\n')?.split('\n');
    if lines.next()? != HEADER {
        return None;
    }
    let mut field = |name: &str| {
        lines
            .next()?
            .strip_prefix(name)?
            .strip_prefix(' ')
            .map(str::to_string)
    };
    let version = field("version")?;
    let target = field("target")?;
    let archive = field("archive")?;
    let sha256 = field("sha256")?;
    if lines.next().is_some()
        || parse_version(&version).is_none()
        || target != TARGET
        || archive != ARCHIVE
        || sha256.len() != 64
        || !sha256
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return None;
    }
    Some(Release { version, sha256 })
}

/// The statement the release workflow signs.
#[cfg(test)]
pub(crate) fn statement(version: &str, sha256: &str) -> String {
    format!("{HEADER}\nversion {version}\ntarget {TARGET}\narchive {ARCHIVE}\nsha256 {sha256}\n")
}

type Version<'a> = ([u64; 3], Option<Vec<&'a str>>);

fn parse_version(text: &str) -> Option<Version<'_>> {
    let (core, pre) = match text.split_once('-') {
        Some((core, pre)) => (core, Some(pre)),
        None => (text, None),
    };
    let mut parts = core.split('.');
    let mut numbers = [0u64; 3];
    for number in &mut numbers {
        let part = parts.next()?;
        if part.is_empty() || !part.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        *number = part.parse().ok()?;
    }
    if parts.next().is_some() {
        return None;
    }
    let pre = match pre {
        Some(pre) => {
            let ids: Vec<&str> = pre.split('.').collect();
            if ids
                .iter()
                .any(|id| id.is_empty() || !id.bytes().all(|b| b.is_ascii_alphanumeric()))
            {
                return None;
            }
            Some(ids)
        }
        None => None,
    };
    Some((numbers, pre))
}

pub(crate) fn valid_version(text: &str) -> bool {
    parse_version(text).is_some()
}

/// Semantic version order; a pre-release sorts before its release.
pub(crate) fn compare(left: &str, right: &str) -> Option<Ordering> {
    let (left_core, left_pre) = parse_version(left)?;
    let (right_core, right_pre) = parse_version(right)?;
    Some(
        left_core
            .cmp(&right_core)
            .then_with(|| match (left_pre, right_pre) {
                (None, None) => Ordering::Equal,
                (None, Some(_)) => Ordering::Greater,
                (Some(_), None) => Ordering::Less,
                (Some(left), Some(right)) => {
                    for (a, b) in left.iter().zip(&right) {
                        let order = match (a.parse::<u64>(), b.parse::<u64>()) {
                            (Ok(a), Ok(b)) => a.cmp(&b),
                            (Ok(_), Err(_)) => Ordering::Less,
                            (Err(_), Ok(_)) => Ordering::Greater,
                            (Err(_), Err(_)) => a.cmp(b),
                        };
                        if order != Ordering::Equal {
                            return order;
                        }
                    }
                    left.len().cmp(&right.len())
                }
            }),
    )
}

fn decode_hex(text: &str) -> Option<Vec<u8>> {
    if !text.len().is_multiple_of(2) {
        return None;
    }
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(text.get(i..i + 2)?, 16).ok())
        .collect()
}
