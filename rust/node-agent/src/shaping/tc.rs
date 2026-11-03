//! Kernel side of speed limits: HTB trees fed through ifb devices, built
//! with `tc -batch` and run on the host or inside a container's network
//! namespace. Results of the spike are in the VPN plan, section 11d.
use std::fmt::Write as _;
use std::io::Write as _;
use std::process::{Command, Stdio};

/// Classes leaving room for everyone without a limit.
const UNLIMITED: &str = "100gbit";
pub(crate) const DEFAULT_CLASS: u16 = 0xffff;
/// Highest class a limited client can take; `DEFAULT_CLASS` is reserved.
pub(crate) const MAX_CLASS: u16 = DEFAULT_CLASS - 1;

/// How packets of one client are recognised.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, serde::Serialize, serde::Deserialize)]
pub(crate) enum Match {
    /// Socket mark (Xray outbound `sockopt.mark`, restored from conntrack
    /// on ingress).
    Mark(u32),
    /// Destination address, e.g. an AWG peer's tunnel IP.
    Destination(String),
    /// Source address.
    Source(String),
}

impl Match {
    /// The selector part of `tc filter add`: protocol, priority and match.
    fn selector(&self) -> String {
        let flower = |address: &str, field: &str| {
            let (protocol, prio) = if address.contains(':') {
                ("ipv6", 2)
            } else {
                ("ip", 1)
            };
            format!("protocol {protocol} prio {prio} flower {field} {address}")
        };
        match self {
            Match::Mark(mark) => format!("protocol all prio 3 handle {mark:#x}/0xffffffff fw"),
            Match::Destination(address) => flower(address, "dst_ip"),
            Match::Source(address) => flower(address, "src_ip"),
        }
    }
}

/// One limited client in one direction.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, serde::Serialize, serde::Deserialize)]
pub(crate) struct Class {
    pub(crate) minor: u16,
    pub(crate) kbit: u64,
    pub(crate) matches: Vec<Match>,
}

/// Root HTB on `dev`: one class per client, everything else unlimited.
pub(crate) fn htb(dev: &str, classes: &[Class]) -> Vec<String> {
    let mut lines = vec![
        format!("qdisc add dev {dev} root handle 1: htb default {DEFAULT_CLASS:x}"),
        format!("class add dev {dev} parent 1: classid 1:{DEFAULT_CLASS:x} htb rate {UNLIMITED}"),
    ];
    for class in classes {
        let id = format!("1:{:x}", class.minor);
        lines.push(format!(
            "class add dev {dev} parent 1: classid {id} htb rate {kbit}kbit ceil {kbit}kbit",
            kbit = class.kbit
        ));
        // Flows of one client share its rate fairly.
        lines.push(format!("qdisc add dev {dev} parent {id} fq_codel"));
        for item in &class.matches {
            lines.push(format!(
                "filter add dev {dev} parent 1: {} classid {id}",
                item.selector()
            ));
        }
    }
    lines
}

/// Ingress filters on `dev` that send matching packets to `ifb`.
pub(crate) fn redirect_ingress(dev: &str, ifb: &str, matches: &[Match]) -> Vec<String> {
    matches
        .iter()
        .map(|item| {
            format!(
                "filter add dev {dev} ingress {} action mirred egress redirect dev {ifb}",
                item.selector()
            )
        })
        .collect()
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ShapeError {
    /// The host lacks a tool or kernel feature; retrying will not help.
    Unsupported(String),
    Failed(String),
}

impl std::fmt::Display for ShapeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Unsupported(detail) | Self::Failed(detail) => f.write_str(detail),
        }
    }
}

/// Runs `program args` with `input` on stdin, inside the network namespace
/// of `netns_pid` when given. Returns stdout.
pub(crate) fn run(
    netns_pid: Option<u32>,
    program: &str,
    args: &[&str],
    input: &str,
) -> Result<String, ShapeError> {
    let mut command = match netns_pid {
        Some(pid) => {
            let mut command = Command::new("nsenter");
            command.args(["-t", &pid.to_string(), "-n", program]);
            command
        }
        None => Command::new(program),
    };
    let tool = if netns_pid.is_some() {
        "nsenter"
    } else {
        program
    };
    let mut child = command
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| match err.kind() {
            std::io::ErrorKind::NotFound => {
                ShapeError::Unsupported(format!("{tool} is not installed"))
            }
            _ => ShapeError::Failed(format!("{tool} could not start: {err}")),
        })?;
    if let Some(mut stdin) = child.stdin.take() {
        stdin
            .write_all(input.as_bytes())
            .map_err(|err| ShapeError::Failed(format!("{program}: {err}")))?;
    }
    let output = child
        .wait_with_output()
        .map_err(|err| ShapeError::Failed(format!("{program}: {err}")))?;
    let stderr = String::from_utf8_lossy(&output.stderr);
    if output.status.success() {
        return Ok(String::from_utf8_lossy(&output.stdout).into_owned());
    }
    let detail = stderr
        .lines()
        .filter(|line| !line.contains("quantum of class"))
        .collect::<Vec<_>>()
        .join("; ");
    if netns_pid.is_some()
        && stderr.contains("No such file or directory")
        && detail.contains("nsenter")
    {
        return Err(ShapeError::Failed(format!(
            "network namespace is gone: {detail}"
        )));
    }
    if detail.contains("Unknown device type")
        || detail.contains("Unknown filter")
        || detail.contains("Unknown action")
    {
        return Err(ShapeError::Unsupported(format!("{program}: {detail}")));
    }
    Err(ShapeError::Failed(format!("{program}: {detail}")))
}

/// Runs a batch; `tolerant` keeps going past errors (teardown of things
/// that may not exist) and never fails.
pub(crate) fn batch(
    netns_pid: Option<u32>,
    program: &str,
    lines: &[String],
    tolerant: bool,
) -> Result<(), ShapeError> {
    if lines.is_empty() {
        return Ok(());
    }
    let mut input = String::new();
    for line in lines {
        let _ = writeln!(input, "{line}");
    }
    let args: &[&str] = if tolerant {
        &["-force", "-batch", "-"]
    } else {
        &["-batch", "-"]
    };
    match run(netns_pid, program, args, &input) {
        Err(ShapeError::Failed(_)) if tolerant => Ok(()),
        result => result.map(|_| ()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn htb_gives_each_client_a_class_and_leaves_others_unlimited() {
        let lines = htb(
            "wg0",
            &[Class {
                minor: 2,
                kbit: 8000,
                matches: vec![
                    Match::Destination("10.8.1.2/32".into()),
                    Match::Destination("fd42::2/128".into()),
                ],
            }],
        );
        assert_eq!(
            lines,
            vec![
                "qdisc add dev wg0 root handle 1: htb default ffff",
                "class add dev wg0 parent 1: classid 1:ffff htb rate 100gbit",
                "class add dev wg0 parent 1: classid 1:2 htb rate 8000kbit ceil 8000kbit",
                "qdisc add dev wg0 parent 1:2 fq_codel",
                "filter add dev wg0 parent 1: protocol ip prio 1 flower dst_ip 10.8.1.2/32 classid 1:2",
                "filter add dev wg0 parent 1: protocol ipv6 prio 2 flower dst_ip fd42::2/128 classid 1:2",
            ]
        );
    }

    #[test]
    fn marks_and_sources_render_as_filters() {
        assert_eq!(
            redirect_ingress("wg0", "np-aup", &[Match::Source("10.8.1.2/32".into())]),
            vec![
                "filter add dev wg0 ingress protocol ip prio 1 flower src_ip 10.8.1.2/32 action mirred egress redirect dev np-aup"
            ]
        );
        assert_eq!(
            Match::Mark(0x4e50_0001).selector(),
            "protocol all prio 3 handle 0x4e500001/0xffffffff fw"
        );
    }

    #[test]
    fn a_missing_tool_is_unsupported_and_tolerant_batches_never_fail() {
        assert!(matches!(
            run(None, "np-no-such-tool", &[], ""),
            Err(ShapeError::Unsupported(_))
        ));
        assert_eq!(batch(None, "false", &["x".into()], true), Ok(()));
        assert!(matches!(
            batch(None, "false", &["x".into()], false),
            Err(ShapeError::Failed(_))
        ));
    }
}
