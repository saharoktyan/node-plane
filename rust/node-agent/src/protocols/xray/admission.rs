//! Concurrent source limit for VLESS clients (VPN plan, section 5).
//!
//! A source is an IP (IPv6: its /64) with a new connection within `window`.
//! Over the limit the source that appeared first is evicted: a routing rule
//! `{user, source} -> blackhole` blocks it for `cooldown`. A blocked source
//! is not counted and cannot evict others, so two parties sharing one link
//! swap at most once per cooldown. Blocks are journaled and re-applied after
//! an Xray restart (detected by its uptime going back).
use std::collections::BTreeMap;
use std::net::IpAddr;

use serde::{Deserialize, Serialize};

use super::api::{RouteRule, SourceNet, XrayApi, XrayApiError};

/// Outbound that drops traffic; created with node settings.
const BLOCKED_OUTBOUND: &str = "node-plane-blocked";
const IPV4_PREFIX: u8 = 32;
const IPV6_PREFIX: u8 = 64;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Mode {
    /// Report what would be evicted; block nothing.
    Observe,
    Enforce,
}

/// Effective policy for one Xray client, resolved by the controller.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct Policy {
    pub(crate) max_sources: u32,
    pub(crate) mode: Mode,
    pub(crate) window_seconds: u64,
    pub(crate) cooldown_seconds: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct Source {
    first_seen: u64,
    last_seen: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct Block {
    pub(crate) rule_tag: String,
    pub(crate) until: u64,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
struct Client {
    sources: BTreeMap<String, Source>,
    blocks: BTreeMap<String, Block>,
}

/// Journaled state of every limited client on this node.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct State {
    clients: BTreeMap<String, Client>,
    /// Makes rule tags unique for the life of the journal.
    next_rule: u64,
    /// Xray uptime at the last step; a smaller value means a restart.
    xray_uptime: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Event {
    Evicted { email: String, source: String },
    WouldEvict { email: String, source: String },
}

/// Routing changes one step needs.
#[derive(Debug, Default, PartialEq, Eq)]
pub(crate) struct Changes {
    pub(crate) add: Vec<(String, String, String)>,
    pub(crate) remove: Vec<String>,
    pub(crate) events: Vec<Event>,
}

/// `ip` as reported by the online list, as the network the limit counts.
fn source_key(ip: &str) -> Option<String> {
    let address: IpAddr = ip
        .trim_start_matches('[')
        .trim_end_matches(']')
        .parse()
        .ok()?;
    let prefix = if address.is_ipv4() {
        IPV4_PREFIX
    } else {
        IPV6_PREFIX
    };
    SourceNet::parse(&format!("{address}/{prefix}"))
        .ok()
        .map(|net| net.to_string())
}

impl State {
    /// Every block that should exist in Xray now.
    pub(crate) fn blocks(&self) -> impl Iterator<Item = (&str, &str, &Block)> {
        self.clients.iter().flat_map(|(email, client)| {
            client
                .blocks
                .iter()
                .map(move |(source, block)| (email.as_str(), source.as_str(), block))
        })
    }

    /// One observation of `email`. `online` maps IPs to the time of their
    /// latest new connection. Returns the rules to add and remove.
    pub(crate) fn observe(
        &mut self,
        email: &str,
        policy: &Policy,
        online: &BTreeMap<String, i64>,
        now: u64,
    ) -> Changes {
        let mut changes = Changes::default();
        let client = self.clients.entry(email.to_string()).or_default();
        client.blocks.retain(|_, block| {
            let keep = block.until > now;
            if !keep {
                changes.remove.push(block.rule_tag.clone());
            }
            keep
        });
        // A client vanishing without FIN stays in the online list; only a
        // recent new connection counts (spike, 5.7).
        let since = now.saturating_sub(policy.window_seconds);
        let mut active: BTreeMap<String, u64> = BTreeMap::new();
        for (ip, seen) in online {
            let seen = u64::try_from(*seen).unwrap_or(0);
            let Some(key) = source_key(ip) else { continue };
            if seen < since || client.blocks.contains_key(&key) {
                continue;
            }
            let entry = active.entry(key).or_insert(seen);
            *entry = (*entry).max(seen);
        }
        client.sources.retain(|key, _| active.contains_key(key));
        for (key, seen) in active {
            client
                .sources
                .entry(key)
                .and_modify(|source| source.last_seen = source.last_seen.max(seen))
                .or_insert(Source {
                    first_seen: now,
                    last_seen: seen,
                });
        }
        let limit = usize::try_from(policy.max_sources).unwrap_or(usize::MAX);
        if client.sources.len() > limit {
            let mut order: Vec<_> = client
                .sources
                .iter()
                .map(|(key, source)| (source.first_seen, key.clone()))
                .collect();
            order.sort();
            let excess = client.sources.len() - limit;
            for (_, source) in order.into_iter().take(excess) {
                match policy.mode {
                    Mode::Observe => changes.events.push(Event::WouldEvict {
                        email: email.to_string(),
                        source,
                    }),
                    Mode::Enforce => {
                        client.sources.remove(&source);
                        let rule_tag = format!("np-evict-{}", self.next_rule);
                        self.next_rule += 1;
                        client.blocks.insert(
                            source.clone(),
                            Block {
                                rule_tag: rule_tag.clone(),
                                until: now + policy.cooldown_seconds,
                            },
                        );
                        changes
                            .add
                            .push((rule_tag, email.to_string(), source.clone()));
                        changes.events.push(Event::Evicted {
                            email: email.to_string(),
                            source,
                        });
                    }
                }
            }
        }
        changes
    }

    /// Drops clients without a policy (limit off, device deleted) and
    /// returns the rules of their blocks to remove.
    pub(crate) fn retain_policies(&mut self, policies: &BTreeMap<String, Policy>) -> Vec<String> {
        let mut remove = Vec::new();
        self.clients.retain(|email, client| {
            let policy = policies.get(email);
            if policy.is_none_or(|policy| policy.mode == Mode::Observe) {
                remove.extend(client.blocks.values().map(|block| block.rule_tag.clone()));
                client.blocks.clear();
            }
            policy.is_some()
        });
        remove
    }

    /// Records Xray's uptime; true when Xray restarted and lost dynamic rules.
    pub(crate) fn xray_restarted(&mut self, uptime: u32) -> bool {
        let restarted = self.xray_uptime.is_some_and(|previous| uptime < previous);
        self.xray_uptime = Some(uptime);
        restarted
    }
}

fn rule(rule_tag: &str, email: &str, source: &str) -> Result<RouteRule, XrayApiError> {
    Ok(RouteRule {
        rule_tag: rule_tag.to_string(),
        outbound_tag: BLOCKED_OUTBOUND.to_string(),
        users: vec![email.to_string()],
        sources: vec![SourceNet::parse(source)?],
    })
}

/// One pass over every limited client: observe, then apply rule changes.
/// The caller journals `state` afterwards, also when this fails midway:
/// recorded blocks are re-added on the next restart signal.
pub(crate) async fn step(
    api: &XrayApi,
    state: &mut State,
    policies: &BTreeMap<String, Policy>,
    now: u64,
) -> Result<Vec<Event>, XrayApiError> {
    for rule_tag in state.retain_policies(policies) {
        api.remove_rule(&rule_tag).await?;
    }
    if state.xray_restarted(api.uptime().await?) {
        let blocks: Vec<_> = state
            .blocks()
            .map(|(email, source, block)| rule(&block.rule_tag, email, source))
            .collect::<Result<_, _>>()?;
        for block in blocks {
            api.add_rule(&block).await?;
        }
    }
    let mut events = Vec::new();
    for (email, policy) in policies {
        let online = api.online_ips(email).await?;
        let changes = state.observe(email, policy, &online, now);
        for rule_tag in &changes.remove {
            api.remove_rule(rule_tag).await?;
        }
        for (rule_tag, email, source) in &changes.add {
            api.add_rule(&rule(rule_tag, email, source)?).await?;
        }
        events.extend(changes.events);
    }
    Ok(events)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn policy(max_sources: u32, mode: Mode) -> Policy {
        Policy {
            max_sources,
            mode,
            window_seconds: 120,
            cooldown_seconds: 300,
        }
    }

    fn online(items: &[(&str, i64)]) -> BTreeMap<String, i64> {
        items.iter().map(|(ip, at)| (ip.to_string(), *at)).collect()
    }

    #[test]
    fn ipv6_privacy_addresses_are_one_source_and_ipv4_is_exact() {
        assert_eq!(
            source_key("[2001:db8:1:2::a]").as_deref(),
            Some("2001:db8:1:2::/64")
        );
        assert_eq!(
            source_key("[2001:db8:1:2:ffff::1]"),
            source_key("[2001:db8:1:2::a]")
        );
        assert_eq!(source_key("203.0.113.7").as_deref(), Some("203.0.113.7/32"));
        assert_eq!(source_key("not an ip"), None);
    }

    #[test]
    fn a_new_network_evicts_the_oldest_source_once() {
        let mut state = State::default();
        let limit = policy(1, Mode::Enforce);
        let first = state.observe("phone", &limit, &online(&[("198.51.100.1", 1000)]), 1000);
        assert_eq!(first, Changes::default());
        // Wi-Fi -> LTE: the new source stays, the old one is evicted.
        let second = state.observe(
            "phone",
            &limit,
            &online(&[("198.51.100.1", 1000), ("203.0.113.9", 1010)]),
            1010,
        );
        assert_eq!(
            second.add,
            vec![(
                "np-evict-0".to_string(),
                "phone".to_string(),
                "198.51.100.1/32".to_string()
            )]
        );
        assert_eq!(
            second.events,
            vec![Event::Evicted {
                email: "phone".into(),
                source: "198.51.100.1/32".into()
            }]
        );
        // Old connections keep the blocked IP in the list; it is not counted.
        let third = state.observe(
            "phone",
            &limit,
            &online(&[("198.51.100.1", 1015), ("203.0.113.9", 1015)]),
            1020,
        );
        assert_eq!(third, Changes::default());
    }

    #[test]
    fn sharing_parties_swap_at_most_once_per_cooldown() {
        let mut state = State::default();
        let limit = policy(1, Mode::Enforce);
        state.observe("link", &limit, &online(&[("198.51.100.1", 0)]), 0);
        let mut evictions = 0;
        for now in (10..1300).step_by(10) {
            // Both parties keep opening connections.
            let seen = i64::try_from(now).unwrap();
            let changes = state.observe(
                "link",
                &limit,
                &online(&[("198.51.100.1", seen), ("203.0.113.9", seen)]),
                now,
            );
            evictions += changes.add.len();
            assert!(state.blocks().count() <= 1);
        }
        // One eviction at the start, then one per expired cooldown.
        assert_eq!(evictions, 1 + 1290 / 300);
    }

    #[test]
    fn stale_sources_do_not_count_and_observe_blocks_nothing() {
        let mut state = State::default();
        // A ghost without FIN keeps an old timestamp; it falls out of the window.
        let changes = state.observe(
            "phone",
            &policy(1, Mode::Enforce),
            &online(&[("198.51.100.1", 100), ("203.0.113.9", 1000)]),
            1000,
        );
        assert_eq!(changes, Changes::default());
        let mut state = State::default();
        let watch = policy(1, Mode::Observe);
        state.observe("phone", &watch, &online(&[("198.51.100.1", 1000)]), 1000);
        let changes = state.observe(
            "phone",
            &watch,
            &online(&[("198.51.100.1", 1000), ("203.0.113.9", 1005)]),
            1005,
        );
        assert!(changes.add.is_empty());
        assert_eq!(
            changes.events,
            vec![Event::WouldEvict {
                email: "phone".into(),
                source: "198.51.100.1/32".into()
            }]
        );
    }

    #[test]
    fn expired_blocks_are_removed_and_policy_removal_clears_rules() {
        let mut state = State::default();
        let limit = policy(1, Mode::Enforce);
        state.observe("phone", &limit, &online(&[("198.51.100.1", 0)]), 0);
        state.observe(
            "phone",
            &limit,
            &online(&[("198.51.100.1", 0), ("203.0.113.9", 5)]),
            5,
        );
        let expired = state.observe("phone", &limit, &online(&[("203.0.113.9", 400)]), 400);
        assert_eq!(expired.remove, vec!["np-evict-0".to_string()]);
        state.observe(
            "phone",
            &limit,
            &online(&[("203.0.113.9", 400), ("198.51.100.1", 410)]),
            410,
        );
        assert_eq!(state.blocks().count(), 1);
        assert_eq!(
            state.retain_policies(&BTreeMap::new()),
            vec!["np-evict-1".to_string()]
        );
        assert_eq!(
            state,
            State {
                next_rule: 2,
                ..State::default()
            }
        );
    }

    #[test]
    fn switching_to_observe_lifts_blocks_but_keeps_tracking() {
        let mut state = State::default();
        let limit = policy(1, Mode::Enforce);
        state.observe("phone", &limit, &online(&[("198.51.100.1", 0)]), 0);
        state.observe(
            "phone",
            &limit,
            &online(&[("198.51.100.1", 0), ("203.0.113.9", 5)]),
            5,
        );
        let policies = BTreeMap::from([("phone".to_string(), policy(1, Mode::Observe))]);
        assert_eq!(
            state.retain_policies(&policies),
            vec!["np-evict-0".to_string()]
        );
        assert_eq!(state.blocks().count(), 0);
        assert_eq!(state.clients["phone"].sources.len(), 1);
    }

    #[test]
    fn a_smaller_uptime_means_xray_restarted() {
        let mut state = State::default();
        assert!(!state.xray_restarted(100));
        assert!(!state.xray_restarted(160));
        assert!(state.xray_restarted(3));
    }

    /// Against a real Xray with HandlerService, StatsService, RoutingService,
    /// `statsUserOnline` and a `node-plane-blocked` blackhole:
    /// `NP_TEST_XRAY_API=127.0.0.1:10085 cargo test -- --ignored admission`.
    #[tokio::test]
    #[ignore = "needs a running Xray"]
    async fn journaled_blocks_return_after_a_restart_and_leave_with_the_policy() {
        let address = std::env::var("NP_TEST_XRAY_API").expect("NP_TEST_XRAY_API");
        let api = XrayApi::connect(&address).await.unwrap();
        let tags = |rules: Vec<(String, String)>| -> Vec<String> {
            rules
                .into_iter()
                .filter(|(tag, _)| tag.starts_with("np-evict-"))
                .map(|(tag, _)| tag)
                .collect()
        };
        let policies = BTreeMap::from([("phone".to_string(), policy(1, Mode::Enforce))]);
        let mut state = State::default();
        // Nobody online: nothing to observe and nothing to block.
        assert!(
            step(&api, &mut state, &policies, 1000)
                .await
                .unwrap()
                .is_empty()
        );
        assert!(tags(api.list_rules().await.unwrap()).is_empty());
        // A journaled block from before an Xray restart is applied again.
        state
            .clients
            .entry("phone".into())
            .or_default()
            .blocks
            .insert(
                "203.0.113.0/24".into(),
                Block {
                    rule_tag: "np-evict-41".into(),
                    until: 2000,
                },
            );
        state.xray_uptime = Some(u32::MAX);
        step(&api, &mut state, &policies, 1000).await.unwrap();
        assert_eq!(tags(api.list_rules().await.unwrap()), vec!["np-evict-41"]);
        // Turning the limit off removes the rule.
        step(&api, &mut state, &BTreeMap::new(), 1001)
            .await
            .unwrap();
        assert!(tags(api.list_rules().await.unwrap()).is_empty());
        assert_eq!(state.blocks().count(), 0);
    }

    #[test]
    fn blocks_match_user_and_source_so_neighbours_are_untouched() {
        let rule = rule("np-evict-0", "phone", "2001:db8:1:2::/64").unwrap();
        assert_eq!(rule.users, vec!["phone".to_string()]);
        assert_eq!(rule.sources[0].to_string(), "2001:db8:1:2::/64");
        assert_eq!(rule.outbound_tag, BLOCKED_OUTBOUND);
    }
}
