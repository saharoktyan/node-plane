//! Speed limits for AmneziaWG peers (VPN plan, section 11d).
//!
//! The tunnel interface lives in the AWG container's network namespace; the
//! agent enters it with `nsenter`. Download is shaped by an HTB root on the
//! tunnel interface (by the peer's tunnel address as destination), upload by
//! redirecting the peer's packets on ingress to ifb `np-aup` with its own
//! HTB. A new container has a new namespace, so a changed PID rebuilds.
use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::shaping::tc::{self, Class, Match, ShapeError};
use crate::shaping::{Host, Rate};

const UP_IFB: &str = "np-aup";

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct Kernel {
    pid: u32,
    interface: String,
    up: Vec<Class>,
    down: Vec<Class>,
    complete: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct State {
    kernel: Option<Kernel>,
}

impl State {
    pub(crate) fn built(&self) -> bool {
        self.kernel.is_some()
    }
}

/// Peer name (the comment above `[Peer]`) -> its tunnel addresses.
pub(crate) fn peers(config: &str) -> BTreeMap<String, Vec<String>> {
    let mut result = BTreeMap::new();
    let mut comment = None;
    let mut current: Option<String> = None;
    for line in config.lines().map(str::trim) {
        if let Some(name) = line.strip_prefix('#') {
            comment = Some(name.trim().to_string());
        } else if line == "[Peer]" {
            current = comment.take();
        } else if line.starts_with('[') {
            current = None;
            comment = None;
        } else if let (Some(name), Some((key, value))) = (&current, line.split_once('='))
            && key.trim() == "AllowedIPs"
        {
            let addresses = value
                .split(',')
                .map(str::trim)
                .filter(|address| !address.is_empty())
                .map(str::to_string)
                .collect();
            result.insert(name.clone(), addresses);
        }
    }
    result
}

/// Peers are written as `<node key>-<name>` when the node has a key.
fn addresses<'a>(
    peers: &'a BTreeMap<String, Vec<String>>,
    node_key: &str,
    name: &str,
) -> Option<&'a Vec<String>> {
    peers
        .get(name)
        .or_else(|| peers.get(&format!("{node_key}-{name}")))
}

fn wanted(
    pid: u32,
    interface: &str,
    clients: &BTreeMap<String, Rate>,
    peers: &BTreeMap<String, Vec<String>>,
    node_key: &str,
) -> Kernel {
    let mut kernel = Kernel {
        pid,
        interface: interface.to_string(),
        complete: true,
        ..Kernel::default()
    };
    let mut minor = 0;
    for (name, rate) in clients {
        let Some(addresses) = addresses(peers, node_key, name) else {
            // Not added on this node yet; the next pass picks it up.
            continue;
        };
        minor += 1;
        let class = |kbit, to: fn(String) -> Match| Class {
            minor,
            kbit,
            matches: addresses.iter().cloned().map(to).collect(),
        };
        if rate.up_kbit > 0 {
            kernel.up.push(class(rate.up_kbit, Match::Source));
        }
        if rate.down_kbit > 0 {
            kernel.down.push(class(rate.down_kbit, Match::Destination));
        }
    }
    kernel
}

fn build_lines(kernel: &Kernel) -> Vec<String> {
    let dev = &kernel.interface;
    let mut lines = tc::htb(dev, &kernel.down);
    if !kernel.up.is_empty() {
        lines.push(format!("qdisc add dev {dev} clsact"));
        let sources: Vec<Match> = kernel
            .up
            .iter()
            .flat_map(|class| class.matches.iter().cloned())
            .collect();
        lines.extend(tc::redirect_ingress(dev, UP_IFB, &sources));
        lines.extend(tc::htb(UP_IFB, &kernel.up));
    }
    lines
}

fn teardown(pid: u32, interface: &str) -> Result<(), ShapeError> {
    tc::batch(
        Some(pid),
        "tc",
        &[
            format!("qdisc del dev {interface} root"),
            format!("qdisc del dev {interface} clsact"),
        ],
        true,
    )?;
    tc::batch(Some(pid), "ip", &[format!("link del {UP_IFB}")], true)
}

fn build(kernel: &Kernel) -> Result<(), ShapeError> {
    if !kernel.up.is_empty() {
        tc::batch(
            Some(kernel.pid),
            "ip",
            &[
                format!("link add {UP_IFB} type ifb"),
                format!("link set {UP_IFB} up"),
            ],
            false,
        )?;
    }
    tc::batch(Some(kernel.pid), "tc", &build_lines(kernel), false)
}

/// The HTB root is still on the interface; restarting the tunnel inside
/// the container drops it.
fn intact(kernel: &Kernel) -> bool {
    tc::run(
        Some(kernel.pid),
        "tc",
        &["qdisc", "show", "dev", &kernel.interface],
        "",
    )
    .is_ok_and(|output| output.contains("htb 1:"))
}

fn container_pid(container: &str) -> Option<u32> {
    tc::run(
        None,
        "docker",
        &["inspect", "-f", "{{.State.Pid}}", container],
        "",
    )
    .ok()
    .and_then(|output| output.trim().parse().ok())
    .filter(|pid| *pid > 0)
}

pub(crate) fn step(
    host: &Host,
    state: &mut State,
    clients: &BTreeMap<String, Rate>,
) -> Result<(), ShapeError> {
    let pid = container_pid(&host.awg_container);
    let Some(pid) = pid else {
        // No container, no namespace: nothing of ours is left to remove.
        state.kernel = None;
        return if clients.is_empty() {
            Ok(())
        } else {
            Err(ShapeError::Failed("AWG container is not running".into()))
        };
    };
    let wanted = (!clients.is_empty()).then(|| {
        let config = std::fs::read_to_string(&host.awg_config).unwrap_or_default();
        wanted(
            pid,
            &host.awg_interface(),
            clients,
            &peers(&config),
            &host.node_key,
        )
    });
    if state.kernel == wanted && wanted.as_ref().is_none_or(intact) {
        return Ok(());
    }
    if let Some(old) = &state.kernel
        && old.pid == pid
    {
        teardown(pid, &old.interface)?;
    }
    state.kernel = None;
    let Some(wanted) = wanted else {
        return Ok(());
    };
    // Leftovers of an earlier agent run in the same namespace.
    teardown(pid, &wanted.interface)?;
    let result = build(&wanted);
    state.kernel = Some(Kernel {
        complete: result.is_ok(),
        ..wanted
    });
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    const CONFIG: &str = "[Interface]\n# not a peer\nPrivateKey = x\nAddress = 10.8.1.1/24\n\n# srv-phone\n[Peer]\nPublicKey = a\nAllowedIPs = 10.8.1.2/32, fd42:6e70:6177:1::2/128\n\n# srv-laptop\n[Peer]\nPublicKey = b\nAllowedIPs = 10.8.1.3/32\n";

    #[test]
    fn peers_are_read_by_their_comment() {
        let peers = peers(CONFIG);
        assert_eq!(
            peers["srv-phone"],
            vec!["10.8.1.2/32", "fd42:6e70:6177:1::2/128"]
        );
        assert_eq!(peers.len(), 2);
        assert_eq!(
            addresses(&peers, "srv", "laptop"),
            Some(&vec!["10.8.1.3/32".to_string()])
        );
        assert_eq!(
            addresses(&peers, "srv", "srv-laptop").map(Vec::len),
            Some(1)
        );
        assert_eq!(addresses(&peers, "srv", "tablet"), None);
    }

    #[test]
    fn download_shapes_the_tunnel_and_upload_goes_through_ifb() {
        let clients = BTreeMap::from([
            (
                "phone".to_string(),
                Rate {
                    up_kbit: 5000,
                    down_kbit: 8000,
                },
            ),
            (
                "tablet".to_string(),
                Rate {
                    up_kbit: 1,
                    down_kbit: 1,
                },
            ),
        ]);
        let kernel = wanted(42, "wg0", &clients, &peers(CONFIG), "srv");
        assert_eq!(kernel.down[0].matches.len(), 2);
        let lines = build_lines(&kernel);
        assert!(lines.contains(
            &"filter add dev wg0 parent 1: protocol ipv6 prio 2 flower dst_ip fd42:6e70:6177:1::2/128 classid 1:1".to_string()
        ));
        assert!(lines.contains(&"qdisc add dev wg0 clsact".to_string()));
        assert!(lines.contains(
            &"filter add dev wg0 ingress protocol ip prio 1 flower src_ip 10.8.1.2/32 action mirred egress redirect dev np-aup".to_string()
        ));
        assert!(
            lines.contains(
                &"class add dev np-aup parent 1: classid 1:1 htb rate 5000kbit ceil 5000kbit"
                    .to_string()
            )
        );
        // A download-only limit needs no ifb.
        let clients = BTreeMap::from([(
            "phone".to_string(),
            Rate {
                up_kbit: 0,
                down_kbit: 8000,
            },
        )]);
        let lines = build_lines(&wanted(42, "wg0", &clients, &peers(CONFIG), "srv"));
        assert!(
            !lines
                .iter()
                .any(|line| line.contains("np-aup") || line.contains("clsact"))
        );
    }
}
