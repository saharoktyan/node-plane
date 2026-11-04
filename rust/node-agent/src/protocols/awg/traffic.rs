//! AWG peer counters from `wg show <interface> transfer`.
use std::fs;

use super::Node;
use super::peers::{fields, split_peer};
use crate::protocols::traffic::{Counters, counter};

fn valid_interface(name: &str) -> bool {
    let bytes = name.as_bytes();
    (1..=15).contains(&bytes.len())
        && bytes[0].is_ascii_alphabetic()
        && bytes
            .iter()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-'))
}

/// The peer's key on disk must still be `identity`.
fn check_config(text: &str, peer: &str, identity: &str) -> Result<(), String> {
    let comment = format!("# {peer}");
    if text.lines().filter(|line| line.trim() == comment).count() != 1 {
        return Err("AWG peer unavailable".into());
    }
    let (_, block) = split_peer(text, peer);
    if fields(&block).get("PublicKey").map(String::as_str) != Some(identity) {
        return Err("AWG identity changed".into());
    }
    Ok(())
}

/// Server receive is client upload; server transmit is client download.
fn parse(output: &str, identity: &str) -> Result<Counters, String> {
    let rows: Vec<Vec<&str>> = output
        .lines()
        .map(|line| line.split_whitespace().collect::<Vec<_>>())
        .filter(|row| row.first() == Some(&identity))
        .collect();
    let [row] = rows.as_slice() else {
        return Err("AWG peer counters unavailable".into());
    };
    let [_, received, sent] = row.as_slice() else {
        return Err("AWG peer counters unavailable".into());
    };
    let number = |value: &str| {
        (value.len() <= 19 && value.bytes().all(|c| c.is_ascii_digit()))
            .then(|| value.parse::<u64>().ok())
            .flatten()
            .ok_or_else(|| "invalid traffic counter".to_string())
            .and_then(counter)
    };
    Ok(Counters {
        uplink: number(received)?,
        downlink: number(sent)?,
    })
}

pub(crate) fn counters(node: &Node, name: &str, identity: &str) -> Result<Counters, String> {
    let text = fs::read_to_string(&node.config).map_err(|err| err.to_string())?;
    check_config(&text, &node.peer_name(name), identity)?;
    if !valid_interface(&node.interface) {
        return Err("invalid AWG interface".into());
    }
    parse(&node.wg_show("transfer")?, identity)
}

#[cfg(test)]
mod tests {
    use super::*;

    const KEY: &str = "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=";

    #[test]
    fn server_receive_is_upload_and_other_peers_are_ignored() {
        let output = format!("other-key\t999\t999\n{KEY}\t150\t300");
        assert_eq!(
            parse(&output, KEY),
            Ok(Counters {
                uplink: 150,
                downlink: 300
            })
        );
        assert!(parse("", KEY).is_err());
        assert!(parse(&format!("{KEY}\t1.5\t3"), KEY).is_err());
        assert!(parse(&format!("{KEY}\t-1\t3"), KEY).is_err());
        assert!(parse(&format!("{KEY}\t9223372036854775808\t3"), KEY).is_err());
    }

    #[test]
    fn the_disk_peer_must_still_hold_the_identity() {
        let text = format!(
            "[Interface]\nPrivateKey = secret\n\n# n1-alice\n[Peer]\nPublicKey = {KEY}\nAllowedIPs = 10.0.0.2/32\n"
        );
        assert!(check_config(&text, "n1-alice", KEY).is_ok());
        assert!(check_config(&text, "n1-alice", "wrong-key").is_err());
        assert!(check_config(&text, "n1-bob", KEY).is_err());
        assert!(valid_interface("wg0") && !valid_interface("0wg") && !valid_interface("wg0;rm"));
    }
}
