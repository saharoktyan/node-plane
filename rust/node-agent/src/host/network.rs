//! Bytes through the host's network interfaces, for the controller's
//! channel-use history. Counters are cumulative since boot; the controller
//! turns two readings into a rate and treats a smaller reading as a restart.
use serde_json::{Value, json};

/// Received and sent bytes summed over every interface but loopback and
/// virtual ones a container or tunnel adds, so traffic is not counted twice.
pub fn counters() -> Value {
    parse(std::fs::read_to_string("/proc/net/dev").ok().as_deref())
}

fn counted(name: &str) -> bool {
    !(name == "lo"
        || ["veth", "docker", "br-", "awg", "wg", "tun", "tap", "virbr"]
            .iter()
            .any(|prefix| name.starts_with(prefix)))
}

fn parse(raw: Option<&str>) -> Value {
    let Some(raw) = raw else {
        return json!({});
    };
    let (mut received, mut sent, mut seen) = (0u64, 0u64, false);
    for line in raw.lines().skip(2) {
        let Some((name, fields)) = line.split_once(':') else {
            continue;
        };
        if !counted(name.trim()) {
            continue;
        }
        let values: Vec<u64> = fields
            .split_whitespace()
            .map(str::parse)
            .collect::<Result<_, _>>()
            .unwrap_or_default();
        if values.len() < 16 {
            continue;
        }
        let (Some(rx), Some(tx)) = (received.checked_add(values[0]), sent.checked_add(values[8]))
        else {
            return json!({});
        };
        (received, sent, seen) = (rx, tx, true);
    }
    if seen {
        json!({"net_rx_bytes": received, "net_tx_bytes": sent})
    } else {
        json!({})
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const DEV: &str = "Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 500 5 0 0 0 0 0 0 500 5 0 0 0 0 0 0
  eth0: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0
  ens4: 30 1 0 0 0 0 0 0 40 1 0 0 0 0 0 0
awg0: 900 9 0 0 0 0 0 0 900 9 0 0 0 0 0 0
vethab12: 700 7 0 0 0 0 0 0 700 7 0 0 0 0 0 0
";

    #[test]
    fn sums_physical_interfaces_only() {
        assert_eq!(
            parse(Some(DEV)),
            json!({"net_rx_bytes": 1030, "net_tx_bytes": 2040})
        );
    }

    #[test]
    fn unreadable_or_empty_is_unknown() {
        assert_eq!(parse(None), json!({}));
        assert_eq!(
            parse(Some("a\nb\n  lo: 1 1 0 0 0 0 0 0 1 1 0 0 0 0 0 0\n")),
            json!({})
        );
    }
}
