//! Speed limits for Xray clients (VPN plan, section 11d).
//!
//! Xray has no per-user shaping. Each limited client gets a freedom outbound
//! `np-rate-<n>` whose sockets carry mark `0x4e50<n>`, and a routing rule
//! `{user} -> np-rate-<n>`. The kernel shapes by that mark on the uplink:
//!
//! - upload leaves through the uplink: an egress filter sends marked packets
//!   to ifb `np-xup`, where HTB has a class per mark;
//! - download comes back unmarked: nft copies the socket mark into conntrack
//!   on output, an ingress `connmark` action restores it, and marked packets
//!   go to ifb `np-xdown`.
//!
//! The uplink's root qdisc is not touched and unmarked traffic never enters
//! the ifb devices. Static routing rules (private destinations, BitTorrent,
//! IPv6) come before runtime rules and keep working. Rate rules must also
//! follow every eviction rule, or an evicted source of a limited client is
//! routed to its rate outbound instead of being blocked; each pass moves
//! rate rules that ended up in front of an eviction rule to the end.
use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use super::api::{RouteRule, XrayApi, XrayApiError};
use crate::shaping::tc::{self, Class, Match, ShapeError};
use crate::shaping::{Host, Rate};

pub(crate) const MARK_BASE: u32 = 0x4e50_0000;
const MARK_MASK: u32 = 0xffff_0000;
const TAG_PREFIX: &str = "np-rate-";
const EVICT_PREFIX: &str = "np-evict-";
const UP_IFB: &str = "np-xup";
const DOWN_IFB: &str = "np-xdown";
const NFT_TABLE: &str = "node_plane_rate";
/// Priorities of our filters on the shared uplink hooks.
const PRIO_REDIRECT: u32 = 20048;
const PRIO_CLASSIFY: u32 = 20049;

/// The kernel side as built.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct Kernel {
    uplinks: Vec<String>,
    up: Vec<Class>,
    down: Vec<Class>,
    /// False when the last build failed midway.
    complete: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct State {
    /// Client email -> class number; stable while the client stays limited.
    marks: BTreeMap<String, u16>,
    /// Domain strategy the rate outbounds were created with.
    domain_strategy: Option<i32>,
    kernel: Option<Kernel>,
}

impl State {
    pub(crate) fn built(&self) -> bool {
        !self.marks.is_empty() || self.kernel.is_some()
    }

    /// Keeps numbers of clients still limited and numbers new ones from the
    /// lowest free value. Returns tags given out in this call.
    fn allocate(
        &mut self,
        clients: &BTreeMap<String, Rate>,
    ) -> Result<BTreeSet<String>, ShapeError> {
        let fresh = tc::allocate(&mut self.marks, clients.keys())?;
        Ok(fresh.iter().map(|email| tag(self.marks[email])).collect())
    }

    fn kernel_for(&self, clients: &BTreeMap<String, Rate>, uplinks: Vec<String>) -> Kernel {
        let class = |email: &String, kbit: u64| {
            let minor = self.marks[email];
            (kbit > 0).then(|| Class {
                minor,
                kbit,
                matches: vec![Match::Mark(MARK_BASE | u32::from(minor))],
            })
        };
        Kernel {
            uplinks,
            up: clients
                .iter()
                .filter_map(|(email, rate)| class(email, rate.up_kbit))
                .collect(),
            down: clients
                .iter()
                .filter_map(|(email, rate)| class(email, rate.down_kbit))
                .collect(),
            complete: true,
        }
    }
}

fn tag(minor: u16) -> String {
    format!("{TAG_PREFIX}{minor}")
}

/// `xray.transport.internet.DomainStrategy` of the config's `direct`
/// outbound; rate outbounds resolve domains the same way.
pub(crate) fn domain_strategy(config: &str) -> i32 {
    let value: serde_json::Value = serde_json::from_str(config).unwrap_or_default();
    let name = value["outbounds"]
        .as_array()
        .and_then(|outbounds| outbounds.iter().find(|item| item["tag"] == "direct"))
        .and_then(|direct| direct["settings"]["domainStrategy"].as_str())
        .unwrap_or("AsIs");
    match name {
        "UseIP" => 1,
        "UseIPv4" => 2,
        "UseIPv6" => 3,
        "UseIPv4v6" => 4,
        "UseIPv6v4" => 5,
        "ForceIP" => 6,
        "ForceIPv4" => 7,
        "ForceIPv6" => 8,
        "ForceIPv4v6" => 9,
        "ForceIPv6v4" => 10,
        _ => 0,
    }
}

/// Brings rate outbounds and rules in line with `wanted` (tag -> email).
async fn routing(
    api: &XrayApi,
    wanted: &BTreeMap<String, String>,
    fresh: &BTreeSet<String>,
    recreate: bool,
    strategy: i32,
) -> Result<(), XrayApiError> {
    let outbounds: BTreeSet<String> = api
        .outbound_tags()
        .await?
        .into_iter()
        .filter(|tag| tag.starts_with(TAG_PREFIX))
        .collect();
    let rules = api.list_rules().await?;
    let last_evict = rules
        .iter()
        .rposition(|(tag, _)| tag.starts_with(EVICT_PREFIX));
    let mut present = BTreeSet::new();
    for (position, (tag, _)) in rules.iter().enumerate() {
        if !tag.starts_with(TAG_PREFIX) {
            continue;
        }
        // A tag given out just now may still carry a former client's rule.
        let stale = !wanted.contains_key(tag) || fresh.contains(tag);
        if stale || last_evict.is_some_and(|evict| position < evict) {
            api.remove_rule(tag).await?;
        } else {
            present.insert(tag.clone());
        }
    }
    for tag in &outbounds {
        if !wanted.contains_key(tag) || fresh.contains(tag) || recreate {
            api.remove_outbound(tag).await?;
        }
    }
    for (tag, email) in wanted {
        if !outbounds.contains(tag) || fresh.contains(tag) || recreate {
            let minor = tag[TAG_PREFIX.len()..]
                .parse::<u32>()
                .map_err(|_| XrayApiError::Invalid(format!("bad rate tag {tag}")))?;
            api.add_marked_outbound(tag, MARK_BASE | minor, strategy)
                .await?;
        }
        if !present.contains(tag) {
            api.add_rule(&RouteRule {
                rule_tag: tag.clone(),
                outbound_tag: tag.clone(),
                users: vec![email.clone()],
                sources: Vec::new(),
            })
            .await?;
        }
    }
    Ok(())
}

/// The configured uplinks, or the interfaces of the default routes, IPv4
/// and IPv6.
fn uplinks(configured: &[String]) -> Result<Vec<String>, ShapeError> {
    if !configured.is_empty() {
        for name in configured {
            let valid = name.len() <= 15
                && name
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-' | '@'));
            if !valid || !std::path::Path::new(&format!("/sys/class/net/{name}")).exists() {
                return Err(ShapeError::Failed(format!(
                    "uplink {name} from SHAPING_UPLINKS does not exist"
                )));
            }
        }
        return Ok(configured
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect());
    }
    let mut devices = BTreeSet::new();
    for family in ["-4", "-6"] {
        let raw = tc::run(None, "ip", &["-j", family, "route", "show", "default"], "")?;
        let routes: Vec<serde_json::Value> = serde_json::from_str(&raw).unwrap_or_default();
        devices.extend(
            routes
                .iter()
                .filter_map(|route| route["dev"].as_str().map(str::to_string)),
        );
    }
    if devices.is_empty() {
        return Err(ShapeError::Failed("no default route".into()));
    }
    Ok(devices.into_iter().collect())
}

fn nft_script() -> String {
    format!(
        "table inet {NFT_TABLE} {{\n  chain output {{\n    type route hook output priority mangle; policy accept;\n    meta mark & {MARK_MASK:#x} == {MARK_BASE:#x} ct mark set meta mark\n  }}\n}}\n"
    )
}

/// Filters on the uplinks: marked upload to `np-xup`; on ingress the
/// conntrack mark is restored and marked download goes to `np-xdown`.
fn uplink_filters(uplink: &str) -> Vec<String> {
    let fw = format!("handle {MARK_BASE:#x}/{MARK_MASK:#x} fw");
    vec![
        format!(
            "filter add dev {uplink} egress protocol all prio {PRIO_REDIRECT} {fw} action mirred egress redirect dev {UP_IFB}"
        ),
        format!(
            "filter add dev {uplink} ingress protocol all prio {PRIO_REDIRECT} matchall action connmark continue"
        ),
        format!(
            "filter add dev {uplink} ingress protocol all prio {PRIO_CLASSIFY} {fw} action mirred egress redirect dev {DOWN_IFB}"
        ),
    ]
}

fn teardown(uplinks: &[String]) -> Result<(), ShapeError> {
    let filters: Vec<String> = uplinks
        .iter()
        .flat_map(|uplink| {
            [
                format!("filter del dev {uplink} egress protocol all prio {PRIO_REDIRECT}"),
                format!("filter del dev {uplink} ingress protocol all prio {PRIO_REDIRECT}"),
                format!("filter del dev {uplink} ingress protocol all prio {PRIO_CLASSIFY}"),
            ]
        })
        .collect();
    tc::batch(None, "tc", &filters, true)?;
    tc::batch(
        None,
        "ip",
        &[format!("link del {UP_IFB}"), format!("link del {DOWN_IFB}")],
        true,
    )?;
    match tc::run(None, "nft", &["delete", "table", "inet", NFT_TABLE], "") {
        Err(ShapeError::Unsupported(detail)) => Err(ShapeError::Unsupported(detail)),
        _ => Ok(()),
    }
}

fn build(kernel: &Kernel) -> Result<(), ShapeError> {
    tc::batch(
        None,
        "ip",
        &[
            format!("link add {UP_IFB} type ifb"),
            format!("link set {UP_IFB} up"),
            format!("link add {DOWN_IFB} type ifb"),
            format!("link set {DOWN_IFB} up"),
        ],
        false,
    )?;
    // The uplink may already have clsact with other owners' filters.
    let clsact: Vec<String> = kernel
        .uplinks
        .iter()
        .map(|uplink| format!("qdisc add dev {uplink} clsact"))
        .collect();
    tc::batch(None, "tc", &clsact, true)?;
    let mut lines = tc::htb(UP_IFB, &kernel.up);
    lines.extend(tc::htb(DOWN_IFB, &kernel.down));
    lines.extend(
        kernel
            .uplinks
            .iter()
            .flat_map(|uplink| uplink_filters(uplink)),
    );
    tc::batch(None, "tc", &lines, false)?;
    tc::run(None, "nft", &["-f", "-"], &nft_script()).map(|_| ())
}

/// The tree is still there: after a reboot or a firewall reload it is not.
fn intact() -> bool {
    std::path::Path::new(&format!("/sys/class/net/{UP_IFB}")).exists()
        && std::path::Path::new(&format!("/sys/class/net/{DOWN_IFB}")).exists()
        && tc::run(None, "nft", &["list", "table", "inet", NFT_TABLE], "").is_ok()
}

/// Brings the kernel side in line with `wanted`: changed limits are
/// applied to the classes in place; the tree is rebuilt when it is gone,
/// the uplinks changed or an in-place change failed.
fn kernel(state: &mut State, wanted: Option<Kernel>) -> Result<(), ShapeError> {
    if state.kernel == wanted && (wanted.is_none() || intact()) {
        return Ok(());
    }
    if let (Some(old), Some(new)) = (&state.kernel, &wanted)
        && old.complete
        && old.uplinks == new.uplinks
        && intact()
    {
        let mut lines = tc::htb_changes(UP_IFB, &old.up, &new.up);
        lines.extend(tc::htb_changes(DOWN_IFB, &old.down, &new.down));
        if tc::batch(None, "tc", &lines, false).is_ok() {
            state.kernel = wanted;
            return Ok(());
        }
    }
    let mut old: BTreeSet<String> = state
        .kernel
        .as_ref()
        .map(|kernel| kernel.uplinks.iter().cloned().collect())
        .unwrap_or_default();
    if let Some(wanted) = &wanted {
        old.extend(wanted.uplinks.iter().cloned());
    }
    teardown(&old.into_iter().collect::<Vec<_>>())?;
    state.kernel = None;
    let Some(wanted) = wanted else {
        return Ok(());
    };
    let result = build(&wanted);
    state.kernel = Some(Kernel {
        complete: result.is_ok(),
        ..wanted
    });
    result
}

/// One pass: Xray routing first, then the kernel. The tree is built only
/// after routing succeeded; lifting limits tears it down even when Xray
/// cannot be reached.
pub(crate) async fn step(
    host: &Host,
    state: &mut State,
    clients: &BTreeMap<String, Rate>,
) -> Result<(), ShapeError> {
    let fresh = state.allocate(clients)?;
    let wanted: BTreeMap<String, String> = state
        .marks
        .iter()
        .map(|(email, minor)| (tag(*minor), email.clone()))
        .collect();
    let strategy = domain_strategy(&std::fs::read_to_string(&host.xray_config).unwrap_or_default());
    let recreate = state
        .domain_strategy
        .is_some_and(|applied| applied != strategy);
    let api_result = match XrayApi::connect(&host.xray_api).await {
        Ok(api) => routing(&api, &wanted, &fresh, recreate, strategy).await,
        Err(err) => Err(err),
    };
    match &api_result {
        Ok(()) => state.domain_strategy = (!wanted.is_empty()).then_some(strategy),
        // Without routing a tree would shape nothing; only tear down.
        Err(err) if !clients.is_empty() => return Err(ShapeError::Failed(err.to_string())),
        Err(_) => {}
    }
    let mut taken = state.clone();
    let clients = clients.clone();
    let configured = host.uplinks.clone();
    let (taken, kernel_result) = tokio::task::spawn_blocking(move || {
        let wanted = if clients.is_empty() {
            Ok(None)
        } else {
            uplinks(&configured).map(|uplinks| Some(taken.kernel_for(&clients, uplinks)))
        };
        let result = wanted.and_then(|wanted| kernel(&mut taken, wanted));
        (taken, result)
    })
    .await
    .map_err(|err| ShapeError::Failed(err.to_string()))?;
    *state = taken;
    kernel_result?;
    api_result.map_err(|err| ShapeError::Failed(err.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rates(names: &[&str]) -> BTreeMap<String, Rate> {
        names
            .iter()
            .map(|name| {
                (
                    name.to_string(),
                    Rate {
                        up_kbit: 1000,
                        down_kbit: 0,
                    },
                )
            })
            .collect()
    }

    #[test]
    fn numbers_stay_with_their_client_and_freed_ones_are_reused() {
        let mut state = State::default();
        let fresh = state.allocate(&rates(&["a", "b", "c"])).unwrap();
        assert_eq!(fresh.len(), 3);
        assert_eq!(state.marks["b"], 2);
        let fresh = state.allocate(&rates(&["a", "c", "d"])).unwrap();
        assert_eq!(fresh, BTreeSet::from(["np-rate-2".to_string()]));
        assert_eq!((state.marks["c"], state.marks["d"]), (3, 2));
    }

    #[test]
    fn classes_exist_only_for_limited_directions() {
        let mut state = State::default();
        let mut clients = rates(&["a"]);
        clients.insert(
            "b".into(),
            Rate {
                up_kbit: 0,
                down_kbit: 8000,
            },
        );
        state.allocate(&clients).unwrap();
        let kernel = state.kernel_for(&clients, vec!["eth0".into()]);
        assert_eq!(
            kernel.up,
            vec![Class {
                minor: 1,
                kbit: 1000,
                matches: vec![Match::Mark(0x4e50_0001)]
            }]
        );
        assert_eq!(kernel.down[0].minor, 2);
        assert_eq!(kernel.down[0].kbit, 8000);
    }

    #[test]
    fn configured_uplinks_must_exist() {
        assert_eq!(
            uplinks(&["lo".into(), "lo".into()]).unwrap(),
            vec!["lo".to_string()]
        );
        for bad in ["np-no-such-dev", "../lo", "an-overlong-device-name"] {
            assert!(matches!(
                uplinks(&[bad.into()]),
                Err(ShapeError::Failed(detail)) if detail.contains("SHAPING_UPLINKS")
            ));
        }
    }

    #[test]
    fn rate_outbounds_copy_the_direct_domain_strategy() {
        let config = r#"{"outbounds":[{"tag":"direct","protocol":"freedom","settings":{"domainStrategy":"ForceIPv4"}}]}"#;
        assert_eq!(domain_strategy(config), 7);
        assert_eq!(domain_strategy(r#"{"outbounds":[{"tag":"direct"}]}"#), 0);
        assert_eq!(domain_strategy("not json"), 0);
    }

    #[test]
    fn uplink_filters_redirect_only_marked_traffic() {
        assert_eq!(
            uplink_filters("eth0"),
            vec![
                "filter add dev eth0 egress protocol all prio 20048 handle 0x4e500000/0xffff0000 fw action mirred egress redirect dev np-xup",
                "filter add dev eth0 ingress protocol all prio 20048 matchall action connmark continue",
                "filter add dev eth0 ingress protocol all prio 20049 handle 0x4e500000/0xffff0000 fw action mirred egress redirect dev np-xdown",
            ]
        );
        assert!(
            nft_script().contains("meta mark & 0xffff0000 == 0x4e500000 ct mark set meta mark")
        );
    }
}
