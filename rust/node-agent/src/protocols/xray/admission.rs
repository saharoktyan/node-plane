//! Concurrent source limit for VLESS clients (VPN plan, section 5).
//!
//! A source is an IP (IPv6: its /64) with a new connection within `window`.
//! Over the limit the source that appeared first is evicted: a routing rule
//! `{user, source} -> blackhole` blocks it for `cooldown`. A blocked source
//! is not counted and cannot evict others, so two parties sharing one link
//! swap at most once per cooldown. Blocks are journaled and re-applied after
//! an Xray restart (detected by its uptime going back).
//!
//! Evictions are buffered for the controller without the source address;
//! `ObserveSessions` reads them with per-client source counts.
use std::collections::{BTreeMap, VecDeque};
use std::net::IpAddr;
use std::path::Path;
use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};
use tonic::Status;

use super::api::cli::valid_email;
use super::api::{RouteRule, SourceNet, XrayApi, XrayApiError};
use crate::agent::v1::{
    AdmissionEvent, ApplyAdmissionPolicyRequest, ApplyAdmissionPolicyResponse,
    ObserveSessionsRequest, ObserveSessionsResponse, SessionObservation,
};
use crate::journal::Journal;
use crate::state::AgentState;

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
    /// Observe mode: sources already reported, until when. Reported once
    /// per cooldown, like an eviction would be.
    #[serde(default)]
    reported: BTreeMap<String, u64>,
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
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
        client.reported.retain(|_, until| *until > now);
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
                    Mode::Observe => {
                        if client.reported.contains_key(&source) {
                            continue;
                        }
                        client
                            .reported
                            .insert(source.clone(), now + policy.cooldown_seconds);
                        changes.events.push(Event::WouldEvict {
                            email: email.to_string(),
                            source,
                        });
                    }
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

/// The controller's latest snapshot; journaled so limits keep working
/// while the controller is unreachable.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct Snapshot {
    pub(crate) revision: u64,
    pub(crate) clients: BTreeMap<String, Policy>,
}

impl Event {
    fn email(&self) -> &str {
        match self {
            Event::Evicted { email, .. } | Event::WouldEvict { email, .. } => email,
        }
    }

    fn kind(&self) -> &'static str {
        match self {
            Event::Evicted { .. } => "evicted",
            Event::WouldEvict { .. } => "would_evict",
        }
    }
}

/// Events for the controller, kept in memory for one agent run. Addresses
/// are dropped here: the controller stores counts only.
struct Outbox {
    boot_id: String,
    next_sequence: u64,
    /// Highest sequence dropped to bound memory.
    dropped_through: u64,
    events: VecDeque<AdmissionEvent>,
}

const OUTBOX_LIMIT: usize = 1000;

impl Outbox {
    fn new() -> Self {
        let started = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default();
        Self {
            boot_id: format!("{:x}-{:x}", started.as_nanos(), std::process::id()),
            next_sequence: 1,
            dropped_through: 0,
            events: VecDeque::new(),
        }
    }

    fn push(&mut self, event: &Event, occurred_at: u64) {
        if self.events.len() == OUTBOX_LIMIT
            && let Some(dropped) = self.events.pop_front()
        {
            self.dropped_through = dropped.sequence;
        }
        self.events.push_back(AdmissionEvent {
            sequence: self.next_sequence,
            email: event.email().to_string(),
            kind: event.kind().to_string(),
            occurred_at: i64::try_from(occurred_at).unwrap_or(i64::MAX),
        });
        self.next_sequence += 1;
    }

    /// Events after `after_sequence` of run `boot_id`; another run's cursor
    /// reads everything buffered.
    fn after(&self, boot_id: &str, after_sequence: u64) -> (Vec<AdmissionEvent>, bool) {
        let after = if boot_id == self.boot_id {
            after_sequence
        } else {
            0
        };
        let events = self
            .events
            .iter()
            .filter(|event| event.sequence > after)
            .cloned()
            .collect();
        (events, after < self.dropped_through)
    }
}

const MAX_WINDOW_SECONDS: u64 = 3600;
const MAX_COOLDOWN_SECONDS: u64 = 86_400;

/// Agent-owned limit service: snapshot and enforcement state, both journaled.
pub(crate) struct Service {
    snapshot: Mutex<Snapshot>,
    snapshot_journal: Journal,
    state_journal: Journal,
    outbox: Mutex<Outbox>,
}

impl Service {
    pub(crate) fn open(state_dir: &str) -> Arc<Self> {
        let dir = Path::new(state_dir);
        let snapshot_journal = Journal::new(dir.join("admission-policy.json"));
        let snapshot = snapshot_journal.load().unwrap_or_else(|err| {
            // Keep the damaged file for inspection; the next snapshot replaces it.
            eprintln!("admission policy journal unreadable: {err}");
            Snapshot::default()
        });
        Arc::new(Self {
            snapshot: Mutex::new(snapshot),
            snapshot_journal,
            state_journal: Journal::new(dir.join("admission-state.json")),
            outbox: Mutex::new(Outbox::new()),
        })
    }

    fn snapshot(&self) -> Snapshot {
        self.snapshot
            .lock()
            .map(|snapshot| snapshot.clone())
            .unwrap_or_default()
    }

    /// Stores a newer snapshot and returns the revision now in force.
    pub(crate) fn apply(&self, request: ApplyAdmissionPolicyRequest) -> Result<u64, Status> {
        let mut clients = BTreeMap::new();
        for client in request.clients {
            let mode = match client.mode.as_str() {
                "observe" => Mode::Observe,
                "enforce" => Mode::Enforce,
                _ => return Err(Status::invalid_argument("unknown admission mode")),
            };
            if !valid_email(&client.email)
                || client.max_sources == 0
                || !(1..=MAX_WINDOW_SECONDS).contains(&client.window_seconds)
                || client.cooldown_seconds > MAX_COOLDOWN_SECONDS
            {
                return Err(Status::invalid_argument("invalid admission policy"));
            }
            let policy = Policy {
                max_sources: client.max_sources,
                mode,
                window_seconds: client.window_seconds,
                cooldown_seconds: client.cooldown_seconds,
            };
            if clients.insert(client.email, policy).is_some() {
                return Err(Status::invalid_argument("duplicate admission client"));
            }
        }
        let mut current = self
            .snapshot
            .lock()
            .map_err(|_| Status::internal("admission state unavailable"))?;
        if request.revision <= current.revision {
            return Ok(current.revision);
        }
        let next = Snapshot {
            revision: request.revision,
            clients,
        };
        self.snapshot_journal
            .save(&next)
            .map_err(|_| Status::unavailable("admission policy could not be stored"))?;
        *current = next;
        Ok(current.revision)
    }

    /// One enforcement pass. Without limits and blocks it does not touch Xray.
    pub(crate) async fn tick(&self, address: &str) -> Result<Vec<Event>, String> {
        let policies = self.snapshot().clients;
        let mut state: State = self.state_journal.load().map_err(|err| err.to_string())?;
        if policies.is_empty() && state.clients.is_empty() {
            return Ok(Vec::new());
        }
        let now = chrono::Utc::now().timestamp().max(0).unsigned_abs();
        let result = match XrayApi::connect(address).await {
            Ok(api) => step(&api, &mut state, &policies, now).await,
            Err(err) => Err(err),
        };
        // Journal even after a failure midway: blocks already added are recorded.
        self.state_journal
            .save(&state)
            .map_err(|err| err.to_string())?;
        let events = result.map_err(|err| err.to_string())?;
        if let Ok(mut outbox) = self.outbox.lock() {
            for event in &events {
                outbox.push(event, now);
            }
        }
        Ok(events)
    }

    /// Buffered events and the source counts of every limited client.
    pub(crate) fn observe(
        &self,
        request: &ObserveSessionsRequest,
    ) -> Result<ObserveSessionsResponse, Status> {
        let policies = self.snapshot().clients;
        let state: State = self
            .state_journal
            .load()
            .map_err(|_| Status::unavailable("admission state unreadable"))?;
        let sessions = policies
            .keys()
            .map(|email| {
                let client = state.clients.get(email);
                let count = |len: usize| u32::try_from(len).unwrap_or(u32::MAX);
                SessionObservation {
                    email: email.clone(),
                    active_sources: client.map_or(0, |client| count(client.sources.len())),
                    blocked_sources: client.map_or(0, |client| count(client.blocks.len())),
                }
            })
            .collect();
        let outbox = self
            .outbox
            .lock()
            .map_err(|_| Status::internal("admission state unavailable"))?;
        let (events, truncated) = outbox.after(&request.boot_id, request.after_sequence);
        Ok(ObserveSessionsResponse {
            boot_id: outbox.boot_id.clone(),
            events,
            sessions,
            truncated,
        })
    }
}

impl AgentState {
    pub(crate) fn apply_admission_policy(
        &self,
        request: ApplyAdmissionPolicyRequest,
    ) -> Result<ApplyAdmissionPolicyResponse, Status> {
        Ok(ApplyAdmissionPolicyResponse {
            revision: self.admission.apply(request)?,
        })
    }

    pub(crate) fn observe_sessions(
        &self,
        request: ObserveSessionsRequest,
    ) -> Result<ObserveSessionsResponse, Status> {
        self.admission.observe(&request)
    }
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
    fn observe_reports_a_source_once_per_cooldown() {
        let mut state = State::default();
        let watch = policy(1, Mode::Observe);
        let two = |at| online(&[("198.51.100.1", 1000), ("203.0.113.9", at)]);
        state.observe("phone", &watch, &online(&[("198.51.100.1", 1000)]), 1000);
        assert_eq!(
            state
                .observe("phone", &watch, &two(1005), 1005)
                .events
                .len(),
            1
        );
        assert!(
            state
                .observe("phone", &watch, &two(1010), 1010)
                .events
                .is_empty()
        );
        // After the cooldown the same overlap is reported again.
        let later = 1005 + watch.cooldown_seconds;
        let changes = state.observe(
            "phone",
            &watch,
            &online(&[
                ("198.51.100.1", later as i64),
                ("203.0.113.9", later as i64),
            ]),
            later,
        );
        assert_eq!(changes.events.len(), 1);
    }

    #[test]
    fn the_outbox_numbers_events_without_addresses_and_resets_per_run() {
        let mut outbox = Outbox::new();
        for at in 0..3 {
            outbox.push(
                &Event::Evicted {
                    email: "phone".into(),
                    source: "198.51.100.1/32".into(),
                },
                1000 + at,
            );
        }
        let boot = outbox.boot_id.clone();
        let (events, truncated) = outbox.after(&boot, 1);
        assert!(!truncated);
        assert_eq!(
            events.iter().map(|e| e.sequence).collect::<Vec<_>>(),
            vec![2, 3]
        );
        assert_eq!(
            (
                events[0].email.as_str(),
                events[0].kind.as_str(),
                events[0].occurred_at
            ),
            ("phone", "evicted", 1001)
        );
        // A cursor from another run reads everything buffered.
        assert_eq!(outbox.after("old-run", 3).0.len(), 3);
        for at in 0..OUTBOX_LIMIT as u64 {
            outbox.push(
                &Event::WouldEvict {
                    email: "phone".into(),
                    source: "198.51.100.1/32".into(),
                },
                at,
            );
        }
        let (events, truncated) = outbox.after(&boot, 1);
        assert!(truncated);
        assert_eq!(events.len(), OUTBOX_LIMIT);
        assert!(!outbox.after(&boot, outbox.dropped_through).1);
    }

    #[test]
    fn observe_sessions_counts_only_limited_clients() {
        let dir = state_dir("observe");
        let service = Service::open(&dir);
        service.apply(request(1, &[("phone", "enforce")])).unwrap();
        let mut state = State::default();
        state.observe(
            "phone",
            &policy(1, Mode::Enforce),
            &online(&[("198.51.100.1", 1000), ("203.0.113.9", 1001)]),
            1001,
        );
        state.observe(
            "gone",
            &policy(1, Mode::Enforce),
            &online(&[("198.51.100.2", 1000)]),
            1001,
        );
        service.state_journal.save(&state).unwrap();
        let observed = service
            .observe(&ObserveSessionsRequest {
                boot_id: String::new(),
                after_sequence: 0,
            })
            .unwrap();
        assert_eq!(observed.boot_id, service.outbox.lock().unwrap().boot_id);
        assert_eq!(
            observed
                .sessions
                .iter()
                .map(|s| (s.email.as_str(), s.active_sources, s.blocked_sources))
                .collect::<Vec<_>>(),
            vec![("phone", 1, 1)]
        );
        let _ = std::fs::remove_dir_all(dir);
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

    fn request(revision: u64, clients: &[(&str, &str)]) -> ApplyAdmissionPolicyRequest {
        ApplyAdmissionPolicyRequest {
            revision,
            clients: clients
                .iter()
                .map(|(email, mode)| crate::agent::v1::AdmissionClientPolicy {
                    email: email.to_string(),
                    max_sources: 1,
                    mode: mode.to_string(),
                    window_seconds: 120,
                    cooldown_seconds: 300,
                })
                .collect(),
        }
    }

    fn state_dir(name: &str) -> String {
        let dir = std::env::temp_dir().join(format!("np-admission-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        dir.to_string_lossy().into_owned()
    }

    #[test]
    fn snapshots_only_move_forward_and_survive_a_restart() {
        let dir = state_dir("forward");
        let service = Service::open(&dir);
        assert_eq!(
            service.apply(request(2, &[("phone", "enforce")])).unwrap(),
            2
        );
        // An older or repeated snapshot changes nothing.
        assert_eq!(service.apply(request(1, &[])).unwrap(), 2);
        assert_eq!(service.apply(request(2, &[])).unwrap(), 2);
        assert_eq!(service.snapshot().clients.len(), 1);
        let restarted = Service::open(&dir);
        assert_eq!(restarted.snapshot(), service.snapshot());
        assert_eq!(restarted.apply(request(3, &[])).unwrap(), 3);
        assert!(Service::open(&dir).snapshot().clients.is_empty());
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn invalid_snapshots_are_refused_whole() {
        let dir = state_dir("invalid");
        let service = Service::open(&dir);
        for bad in [
            request(1, &[("phone", "off")]),
            request(1, &[("bad email", "enforce")]),
            request(1, &[("phone", "enforce"), ("phone", "observe")]),
            ApplyAdmissionPolicyRequest {
                revision: 1,
                clients: vec![crate::agent::v1::AdmissionClientPolicy {
                    email: "phone".into(),
                    max_sources: 0,
                    mode: "enforce".into(),
                    window_seconds: 120,
                    cooldown_seconds: 300,
                }],
            },
        ] {
            assert_eq!(
                service.apply(bad).unwrap_err().code(),
                tonic::Code::InvalidArgument
            );
        }
        assert_eq!(service.snapshot(), Snapshot::default());
        let _ = std::fs::remove_dir_all(dir);
    }

    #[tokio::test]
    async fn without_limits_the_loop_never_dials_xray() {
        let dir = state_dir("idle");
        let service = Service::open(&dir);
        // Port 1 refuses connections; an idle tick must not try it.
        assert!(service.tick("127.0.0.1:1").await.unwrap().is_empty());
        service.apply(request(1, &[("phone", "enforce")])).unwrap();
        assert!(service.tick("127.0.0.1:1").await.is_err());
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn blocks_match_user_and_source_so_neighbours_are_untouched() {
        let rule = rule("np-evict-0", "phone", "2001:db8:1:2::/64").unwrap();
        assert_eq!(rule.users, vec!["phone".to_string()]);
        assert_eq!(rule.sources[0].to_string(), "2001:db8:1:2::/64");
        assert_eq!(rule.outbound_tag, BLOCKED_OUTBOUND);
    }
}
