//! Peers in an AmneziaWG server config (`wg0.conf`).
//!
//! A peer is the block that starts at its `# <name>` comment line and ends
//! at the next blank line. Revoked peers are archived as
//! `<clients>/<name>.revoked.json`, bound to the server by the fingerprint
//! of its private key.
use std::collections::BTreeMap;

use serde::Deserialize;

/// `key = value` lines of a block, with keys and values trimmed.
pub(crate) fn fields(text: &str) -> BTreeMap<String, String> {
    text.lines()
        .filter_map(|line| line.trim().split_once('='))
        .map(|(key, value)| (key.trim().to_string(), value.trim().to_string()))
        .collect()
}

/// The config without the named peer, and that peer's block (empty when
/// absent).
pub(crate) fn split_peer(text: &str, name: &str) -> (String, String) {
    let lines: Vec<&str> = text.split_inclusive('\n').collect();
    let comment = format!("# {name}");
    let Some(begin) = lines.iter().position(|line| line.trim() == comment) else {
        return (text.to_string(), String::new());
    };
    let mut end = begin + 1;
    while end < lines.len() && !lines[end].trim().is_empty() {
        end += 1;
    }
    let rest = [&lines[..begin], &lines[end..]].concat().concat();
    (rest, lines[begin..end].concat())
}

/// SHA-256 of the server private key, as hex.
pub(crate) fn fingerprint(text: &str) -> Result<String, String> {
    let interface = text.split("[Peer]").next().unwrap_or_default();
    let private = fields(interface).remove("PrivateKey").unwrap_or_default();
    if private.is_empty() {
        return Err("Server private key is missing".into());
    }
    Ok(crate::protocols::sha256_hex(private.as_bytes()))
}

/// The `[Peer]` blocks of a config, in order.
pub(crate) fn peer_blocks(text: &str) -> impl Iterator<Item = &str> {
    text.split("[Peer]").skip(1)
}

/// A revoked peer: the server it belonged to and its original block.
#[derive(Debug, Deserialize)]
pub(crate) struct Archive {
    pub(crate) server: String,
    pub(crate) peer: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    const CONFIG: &str = "[Interface]\nPrivateKey = server\n\n# n1-alice\n[Peer]\nPublicKey = public\nAllowedIPs = 10.0.0.2/32\n\n# n1-bob\n[Peer]\nPublicKey = other\n";

    #[test]
    fn a_peer_is_its_comment_line_up_to_the_next_blank_line() {
        let (rest, block) = split_peer(CONFIG, "n1-alice");
        assert_eq!(
            block,
            "# n1-alice\n[Peer]\nPublicKey = public\nAllowedIPs = 10.0.0.2/32\n"
        );
        assert_eq!(
            rest,
            "[Interface]\nPrivateKey = server\n\n\n# n1-bob\n[Peer]\nPublicKey = other\n"
        );
        assert_eq!(
            split_peer(CONFIG, "n1-carol"),
            (CONFIG.into(), String::new())
        );
        assert_eq!(fields(&block)["AllowedIPs"], "10.0.0.2/32");
        assert_eq!(peer_blocks(CONFIG).count(), 2);
    }

    #[test]
    fn the_fingerprint_hashes_the_server_private_key() {
        // hashlib.sha256(b"server").hexdigest()
        assert_eq!(
            fingerprint(CONFIG).unwrap(),
            "b3eacd33433b31b5252351032c9b3e7a2e7aa7738d5decdf0dd6c62680853c06"
        );
        assert!(fingerprint("[Interface]\n").is_err());
    }
}
