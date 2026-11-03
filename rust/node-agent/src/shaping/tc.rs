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
    /// Protocol, priority and handle: what tells this filter apart from
    /// its neighbours, enough to delete it. A mark is its own handle;
    /// addresses get `handle` so one client's filters can go alone.
    fn key(&self, handle: u32) -> String {
        let flower = |address: &str| {
            let (protocol, prio) = if address.contains(':') {
                ("ipv6", 2)
            } else {
                ("ip", 1)
            };
            format!("protocol {protocol} prio {prio} handle {handle:#x} flower")
        };
        match self {
            Match::Mark(mark) => format!("protocol all prio 3 handle {mark:#x} fw"),
            Match::Destination(address) | Match::Source(address) => flower(address),
        }
    }

    /// The selector part of `tc filter add`.
    fn selector(&self, handle: u32) -> String {
        match self {
            Match::Mark(mark) => format!("protocol all prio 3 handle {mark:#x}/0xffffffff fw"),
            Match::Destination(address) => format!("{} dst_ip {address}", self.key(handle)),
            Match::Source(address) => format!("{} src_ip {address}", self.key(handle)),
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

impl Class {
    fn id(&self) -> String {
        format!("1:{:x}", self.minor)
    }

    /// Filter handles are unique per class and match.
    fn filters(&self) -> impl Iterator<Item = (u32, &Match)> {
        self.matches
            .iter()
            .enumerate()
            .map(|(index, item)| ((u32::from(self.minor) << 8) | (index as u32 + 1), item))
    }
}

/// Where a class's filters sit: classifying on its HTB root, or
/// redirecting ingress packets to an ifb device.
#[derive(Clone, Copy)]
enum Filters<'a> {
    Classify,
    Redirect(&'a str),
}

impl Filters<'_> {
    fn add(self, dev: &str, class: &Class) -> Vec<String> {
        class
            .filters()
            .map(|(handle, item)| match self {
                Self::Classify => format!(
                    "filter add dev {dev} parent 1: {} classid {}",
                    item.selector(handle),
                    class.id()
                ),
                Self::Redirect(ifb) => format!(
                    "filter add dev {dev} ingress {} action mirred egress redirect dev {ifb}",
                    item.selector(handle)
                ),
            })
            .collect()
    }

    fn delete(self, dev: &str, class: &Class) -> Vec<String> {
        let parent = match self {
            Self::Classify => "parent 1:",
            Self::Redirect(_) => "ingress",
        };
        class
            .filters()
            .map(|(handle, item)| format!("filter del dev {dev} {parent} {}", item.key(handle)))
            .collect()
    }
}

fn class_rate(verb: &str, dev: &str, class: &Class) -> String {
    format!(
        "class {verb} dev {dev} parent 1: classid {} htb rate {kbit}kbit ceil {kbit}kbit",
        class.id(),
        kbit = class.kbit
    )
}

fn add_class(dev: &str, class: &Class) -> Vec<String> {
    let mut lines = vec![
        class_rate("add", dev, class),
        // Flows of one client share its rate fairly.
        format!("qdisc add dev {dev} parent {} fq_codel", class.id()),
    ];
    lines.extend(Filters::Classify.add(dev, class));
    lines
}

/// Root HTB on `dev`: one class per client, everything else unlimited.
pub(crate) fn htb(dev: &str, classes: &[Class]) -> Vec<String> {
    let mut lines = vec![
        format!("qdisc add dev {dev} root handle 1: htb default {DEFAULT_CLASS:x}"),
        format!("class add dev {dev} parent 1: classid 1:{DEFAULT_CLASS:x} htb rate {UNLIMITED}"),
    ];
    for class in classes {
        lines.extend(add_class(dev, class));
    }
    lines
}

/// Ingress filters on `dev` that send packets of `classes` to `ifb`.
pub(crate) fn redirect_ingress(dev: &str, ifb: &str, classes: &[Class]) -> Vec<String> {
    classes
        .iter()
        .flat_map(|class| Filters::Redirect(ifb).add(dev, class))
        .collect()
}

/// Commands that turn classes `old` into `new` without touching the
/// others, so their queues and counters stay: removed classes and
/// changed filters go first, then rates change and new classes come.
fn changes(
    dev: &str,
    filters: Filters,
    old: &[Class],
    new: &[Class],
    classes: bool,
) -> Vec<String> {
    let find = |list: &[Class], minor| list.iter().find(|class| class.minor == minor).cloned();
    let mut removals = Vec::new();
    let mut additions = Vec::new();
    for class in old {
        match find(new, class.minor) {
            None => {
                removals.extend(filters.delete(dev, class));
                if classes {
                    removals.push(format!("class del dev {dev} classid {}", class.id()));
                }
            }
            Some(next) if next.matches != class.matches => {
                removals.extend(filters.delete(dev, class))
            }
            Some(_) => {}
        }
    }
    for class in new {
        match find(old, class.minor) {
            None if classes => additions.extend(add_class(dev, class)),
            None => additions.extend(filters.add(dev, class)),
            Some(previous) => {
                if classes && previous.kbit != class.kbit {
                    additions.push(class_rate("change", dev, class));
                }
                if previous.matches != class.matches {
                    additions.extend(filters.add(dev, class));
                }
            }
        }
    }
    removals.extend(additions);
    removals
}

/// In-place changes of an HTB tree built by `htb`.
pub(crate) fn htb_changes(dev: &str, old: &[Class], new: &[Class]) -> Vec<String> {
    changes(dev, Filters::Classify, old, new, true)
}

/// In-place changes of filters built by `redirect_ingress`.
pub(crate) fn redirect_changes(dev: &str, ifb: &str, old: &[Class], new: &[Class]) -> Vec<String> {
    changes(dev, Filters::Redirect(ifb), old, new, false)
}

/// Keeps the numbers of `names` that have one and gives new names the
/// lowest free numbers; the others are released. Returns the new names.
pub(crate) fn allocate<'a>(
    numbers: &mut std::collections::BTreeMap<String, u16>,
    names: impl IntoIterator<Item = &'a String>,
) -> Result<std::collections::BTreeSet<String>, ShapeError> {
    let names: std::collections::BTreeSet<&String> = names.into_iter().collect();
    numbers.retain(|name, _| names.contains(name));
    let mut used: std::collections::BTreeSet<u16> = numbers.values().copied().collect();
    let mut fresh = std::collections::BTreeSet::new();
    let mut next = 1;
    for name in names {
        if numbers.contains_key(name) {
            continue;
        }
        while used.contains(&next) {
            next += 1;
        }
        if next > MAX_CLASS {
            return Err(ShapeError::Failed("too many limited clients".into()));
        }
        used.insert(next);
        numbers.insert(name.clone(), next);
        fresh.insert(name.clone());
    }
    Ok(fresh)
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

    fn class(minor: u16, kbit: u64, addresses: &[&str]) -> Class {
        Class {
            minor,
            kbit,
            matches: addresses
                .iter()
                .map(|address| Match::Destination(address.to_string()))
                .collect(),
        }
    }

    #[test]
    fn htb_gives_each_client_a_class_and_leaves_others_unlimited() {
        let lines = htb("wg0", &[class(2, 8000, &["10.8.1.2/32", "fd42::2/128"])]);
        assert_eq!(
            lines,
            vec![
                "qdisc add dev wg0 root handle 1: htb default ffff",
                "class add dev wg0 parent 1: classid 1:ffff htb rate 100gbit",
                "class add dev wg0 parent 1: classid 1:2 htb rate 8000kbit ceil 8000kbit",
                "qdisc add dev wg0 parent 1:2 fq_codel",
                "filter add dev wg0 parent 1: protocol ip prio 1 handle 0x201 flower dst_ip 10.8.1.2/32 classid 1:2",
                "filter add dev wg0 parent 1: protocol ipv6 prio 2 handle 0x202 flower dst_ip fd42::2/128 classid 1:2",
            ]
        );
    }

    #[test]
    fn marks_and_sources_render_as_filters() {
        let up = Class {
            minor: 1,
            kbit: 1,
            matches: vec![Match::Source("10.8.1.2/32".into())],
        };
        assert_eq!(
            redirect_ingress("wg0", "np-aup", &[up]),
            vec![
                "filter add dev wg0 ingress protocol ip prio 1 handle 0x101 flower src_ip 10.8.1.2/32 action mirred egress redirect dev np-aup"
            ]
        );
        assert_eq!(
            Match::Mark(0x4e50_0001).selector(0x101),
            "protocol all prio 3 handle 0x4e500001/0xffffffff fw"
        );
    }

    #[test]
    fn changes_touch_only_the_classes_that_differ() {
        let old = [
            class(1, 1000, &["10.8.1.2/32"]),
            class(2, 2000, &["10.8.1.3/32"]),
            class(3, 3000, &["10.8.1.4/32"]),
        ];
        let new = [
            class(1, 1000, &["10.8.1.2/32"]),
            class(2, 5000, &["10.8.1.5/32"]),
            class(4, 4000, &["10.8.1.6/32"]),
        ];
        assert_eq!(
            htb_changes("wg0", &old, &new),
            vec![
                "filter del dev wg0 parent 1: protocol ip prio 1 handle 0x201 flower",
                "filter del dev wg0 parent 1: protocol ip prio 1 handle 0x301 flower",
                "class del dev wg0 classid 1:3",
                "class change dev wg0 parent 1: classid 1:2 htb rate 5000kbit ceil 5000kbit",
                "filter add dev wg0 parent 1: protocol ip prio 1 handle 0x201 flower dst_ip 10.8.1.5/32 classid 1:2",
                "class add dev wg0 parent 1: classid 1:4 htb rate 4000kbit ceil 4000kbit",
                "qdisc add dev wg0 parent 1:4 fq_codel",
                "filter add dev wg0 parent 1: protocol ip prio 1 handle 0x401 flower dst_ip 10.8.1.6/32 classid 1:4",
            ]
        );
        assert!(htb_changes("wg0", &old, &old).is_empty());
        // Redirects have no classes: a new rate alone changes nothing.
        let faster = [class(1, 9000, &["10.8.1.2/32"])];
        assert!(redirect_changes("wg0", "np-aup", &old[..1], &faster).is_empty());
        assert_eq!(
            Filters::Classify.delete(
                "np-xup",
                &Class {
                    minor: 1,
                    kbit: 1,
                    matches: vec![Match::Mark(0x4e50_0001)]
                }
            ),
            vec!["filter del dev np-xup parent 1: protocol all prio 3 handle 0x4e500001 fw"]
        );
    }

    #[test]
    fn numbers_stay_with_their_name_and_freed_ones_are_reused() {
        let names = |list: &[&str]| list.iter().map(|name| name.to_string()).collect::<Vec<_>>();
        let mut numbers = std::collections::BTreeMap::new();
        assert_eq!(
            allocate(&mut numbers, &names(&["a", "b", "c"]))
                .unwrap()
                .len(),
            3
        );
        let fresh = allocate(&mut numbers, &names(&["a", "c", "d"])).unwrap();
        assert_eq!(fresh, std::collections::BTreeSet::from(["d".to_string()]));
        assert_eq!((numbers["c"], numbers["d"]), (3, 2));
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
