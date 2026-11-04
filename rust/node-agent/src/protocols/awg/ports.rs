//! Preset-aware UDP port selection for the AWG server. Replaces the node
//! copy of `awg_ports.py`; the controller keeps its own for planning.
use std::collections::{BTreeMap, BTreeSet};
use std::net::UdpSocket;
use std::path::Path;

use serde_json::Value;

use crate::intents::leases::Host;

const QUIC: [u16; 4] = [443, 8443, 4433, 4443];
const DNS: [u16; 4] = [53, 5353, 5300, 8053];

/// The ports a preset may use, the preferred one first.
pub(crate) fn candidates(preset: &str, preferred: i64) -> Result<Vec<u16>, String> {
    let preferred = u16::try_from(preferred)
        .ok()
        .filter(|port| *port >= 1)
        .ok_or("invalid AWG port selection")?;
    let fallbacks = match preset {
        "quic" => QUIC,
        "dns" => DNS,
        "chaos" => {
            if !(1024..=9999).contains(&preferred) {
                return Err("Chaos port must be between 1024 and 9999".into());
            }
            return Ok((0..128)
                .map(|offset| 1024 + (preferred - 1024 + offset) % 8976)
                .collect());
        }
        _ => return Err("invalid AWG port selection".into()),
    };
    let mut ports = vec![preferred];
    ports.extend(fallbacks.into_iter().filter(|port| *port != preferred));
    Ok(ports)
}

/// Published UDP ports: each with whether its publishers are the AWG
/// container, and the ones the running AWG container holds.
type Bindings = (BTreeMap<u16, Vec<bool>>, BTreeSet<u16>);

fn bindings(host: &dyn Host, config: &Path, container: &str) -> Result<Bindings, String> {
    let ids = host.check(&["docker", "ps", "-aq"], Some(30))?;
    let ids: Vec<&str> = ids.split_whitespace().collect();
    let mut reserved: BTreeMap<u16, Vec<bool>> = BTreeMap::new();
    let mut owned = BTreeSet::new();
    if ids.is_empty() {
        return Ok((reserved, owned));
    }
    let mut args = vec!["docker", "container", "inspect"];
    args.extend(&ids);
    let records: Value = serde_json::from_str(&host.check(&args, Some(30))?)
        .map_err(|err| format!("docker inspect: {err}"))?;
    let source = config.parent().unwrap_or(Path::new("/")).to_string_lossy();
    for record in records.as_array().into_iter().flatten() {
        let ours = record["Name"]
            .as_str()
            .map(|name| name.trim_start_matches('/'))
            == Some(container)
            && record["Mounts"].as_array().is_some_and(|mounts| {
                mounts.iter().any(|mount| {
                    mount["Type"] == "bind"
                        && mount["Source"].as_str() == Some(&source)
                        && mount["Destination"] == "/opt/amnezia/awg"
                })
            });
        let mut mappings = record["HostConfig"]["PortBindings"]
            .as_object()
            .cloned()
            .unwrap_or_default();
        for (protocol, bindings) in record["NetworkSettings"]["Ports"]
            .as_object()
            .into_iter()
            .flatten()
        {
            if !bindings.is_null() && bindings.as_array().is_none_or(|items| !items.is_empty()) {
                mappings.insert(protocol.clone(), bindings.clone());
            }
        }
        for (protocol, bindings) in &mappings {
            if !protocol.ends_with("/udp") {
                continue;
            }
            for binding in bindings.as_array().into_iter().flatten() {
                let Some(port) = binding["HostPort"]
                    .as_str()
                    .filter(|port| !port.is_empty())
                    .and_then(|port| port.parse::<u16>().ok())
                else {
                    continue;
                };
                reserved.entry(port).or_default().push(ours);
                if ours && record["State"]["Running"] == true {
                    owned.insert(port);
                }
            }
        }
    }
    Ok((reserved, owned))
}

/// Whether nothing listens on the UDP port, for IPv4 and IPv6 when the host
/// has it. Docker without a userland proxy publishes ports with no
/// listener: `select` also reads its inventory.
fn can_bind(port: u16) -> bool {
    if UdpSocket::bind(("0.0.0.0", port)).is_err() {
        return false;
    }
    match UdpSocket::bind(("::", port)) {
        Ok(_) => true,
        Err(err) => matches!(
            err.raw_os_error(),
            Some(libc::EAFNOSUPPORT | libc::EADDRNOTAVAIL)
        ),
    }
}

/// The first free candidate; the AWG container's own ports count as free.
pub(crate) fn select(
    host: &dyn Host,
    preset: &str,
    preferred: i64,
    config: &Path,
    container: &str,
) -> Result<u16, String> {
    let (reserved, owned) = bindings(host, config, container)?;
    for port in candidates(preset, preferred)? {
        if reserved
            .get(&port)
            .is_some_and(|owners| !owners.iter().all(|ours| *ours))
        {
            continue;
        }
        if owned.contains(&port) || can_bind(port) {
            return Ok(port);
        }
    }
    Err("no available UDP port for AWG preset".into())
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::intents::leases::fake::Scripted;

    #[test]
    fn candidates_follow_the_preset() {
        assert_eq!(candidates("quic", 443).unwrap(), [443, 8443, 4433, 4443]);
        assert_eq!(
            candidates("quic", 51820).unwrap(),
            [51820, 443, 8443, 4433, 4443]
        );
        assert_eq!(candidates("dns", 5353).unwrap(), [5353, 53, 5300, 8053]);
        let chaos = candidates("chaos", 9998).unwrap();
        assert_eq!(chaos.len(), 128);
        assert_eq!(&chaos[..3], [9998, 9999, 1024]);
        assert!(candidates("chaos", 80).is_err());
        assert!(candidates("other", 443).is_err());
        assert!(candidates("quic", 0).is_err());
        assert!(candidates("quic", 65536).is_err());
    }

    fn host(records: Value) -> Scripted {
        Scripted::new(move |args| match args {
            ["docker", "ps", ..] => (0, "a b\n".into()),
            ["docker", "container", "inspect", ..] => (0, records.to_string()),
            _ => (1, String::new()),
        })
    }

    #[test]
    fn a_port_published_by_another_container_is_skipped_and_our_own_is_kept() {
        let config = Path::new("/runtime/amnezia-awg/data/wg0.conf");
        let ours = |port: &str, running: bool| {
            json!({"Name": "/amnezia-awg", "State": {"Running": running},
                "Mounts": [{"Type": "bind", "Source": "/runtime/amnezia-awg/data",
                            "Destination": "/opt/amnezia/awg"}],
                "HostConfig": {"PortBindings": {"443/udp": [{"HostPort": port}]}}})
        };
        let foreign = |port: &str| {
            json!({"Name": "/other", "State": {"Running": true}, "Mounts": [],
                "NetworkSettings": {"Ports": {"53/udp": [{"HostPort": port}], "80/tcp": null}}})
        };
        // A free fallback port is found by binding: use high ports for that.
        let selected = select(
            &host(json!([foreign("443")])),
            "quic",
            443,
            config,
            "amnezia-awg",
        );
        assert_ne!(selected.unwrap(), 443);
        let own = host(json!([ours("443", true)]));
        assert_eq!(
            select(&own, "quic", 443, config, "amnezia-awg").unwrap(),
            443
        );
        let shared = host(json!([ours("443", true), foreign("443")]));
        assert_ne!(
            select(&shared, "quic", 443, config, "amnezia-awg").unwrap(),
            443
        );
    }

    #[test]
    fn a_held_port_is_not_selected() {
        let socket = UdpSocket::bind(("0.0.0.0", 0)).unwrap();
        let port = socket.local_addr().unwrap().port();
        let empty = Scripted::new(|_| (0, String::new()));
        assert!(!can_bind(port));
        let selected = select(&empty, "chaos", 5000, Path::new("/x/wg0.conf"), "awg").unwrap();
        assert_ne!(selected, port);
    }
}
