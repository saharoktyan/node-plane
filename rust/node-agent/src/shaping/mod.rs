//! Speed limits per client (VPN plan, section 11d).
//!
//! The controller sends the node's full snapshot of limits with a revision
//! that only grows; it is journaled, so limits keep working while the
//! controller is unreachable. Each pass compares what is wanted with what
//! was built and rebuilds a protocol's tree only when they differ or the
//! tree is gone (reboot, AWG container restart). Protocol specifics live in
//! `protocols::xray::rate` and `protocols::awg::rate`; this module holds the
//! snapshot, the journal and the per-protocol status.
pub(crate) mod tc;

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};
use tonic::Status;

use crate::agent::v1::{ApplyRatePolicyRequest, ApplyRatePolicyResponse, RateProtocolStatus};
use crate::journal::Journal;
use crate::protocols::awg::rate as awg_rate;
use crate::protocols::xray::api::cli::valid_email;
use crate::protocols::xray::rate as xray_rate;
use crate::state::AgentState;

/// 100 Gbit/s: above any real uplink, below HTB's limits.
const MAX_KBIT: u64 = 100_000_000;
pub(crate) const PROTOCOLS: [&str; 2] = ["xray", "awg"];

/// Limits of one client in kbit/s; 0 leaves that direction unlimited.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub(crate) struct Rate {
    pub(crate) up_kbit: u64,
    pub(crate) down_kbit: u64,
}

/// The controller's latest snapshot.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct Snapshot {
    pub(crate) revision: u64,
    /// protocol -> client name -> limits.
    pub(crate) clients: BTreeMap<String, BTreeMap<String, Rate>>,
}

impl Snapshot {
    pub(crate) fn protocol(&self, protocol: &str) -> BTreeMap<String, Rate> {
        self.clients.get(protocol).cloned().unwrap_or_default()
    }
}

/// What the agent built, per protocol; journaled to find what to tear down.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct State {
    #[serde(default)]
    pub(crate) xray: xray_rate::State,
    #[serde(default)]
    pub(crate) awg: awg_rate::State,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Outcome {
    /// "applied", "failed" or "unsupported".
    pub(crate) state: &'static str,
    pub(crate) detail: String,
    /// Snapshot revision the pass worked from.
    pub(crate) revision: u64,
}

impl Outcome {
    pub(crate) fn from_result(revision: u64, result: Result<(), tc::ShapeError>) -> Self {
        let (state, detail) = match result {
            Ok(()) => ("applied", String::new()),
            Err(tc::ShapeError::Unsupported(detail)) => ("unsupported", detail),
            Err(tc::ShapeError::Failed(detail)) => ("failed", detail),
        };
        Self {
            state,
            detail,
            revision,
        }
    }
}

/// Where the protocol runtimes live on this node, read from node.env.
#[derive(Debug, Clone)]
pub(crate) struct Host {
    pub(crate) node_key: String,
    pub(crate) xray_api: String,
    pub(crate) xray_config: String,
    pub(crate) awg_container: String,
    pub(crate) awg_config: String,
    /// `SHAPING_UPLINKS` in node.env: interfaces Xray traffic leaves
    /// through, for hosts where the default route does not name them.
    /// Empty uses the interfaces of the default routes.
    pub(crate) uplinks: Vec<String>,
}

impl Host {
    pub(crate) fn of(state: &AgentState) -> Self {
        Self {
            node_key: state.config.node_key.clone(),
            xray_api: std::env::var("NODE_PLANE_XRAY_API")
                .unwrap_or_else(|_| crate::protocols::xray::api::DEFAULT_ADDRESS.into()),
            xray_config: state.node_env_value("XRAY_CONFIG", &state.config.xray_config_path),
            awg_container: state.node_env_value("AWG_CONTAINER_NAME", "amnezia-awg"),
            awg_config: state.node_env_value("AWG_CONFIG", &state.config.awg_config_path),
            uplinks: state
                .node_env_value("SHAPING_UPLINKS", "")
                .split([',', ' '])
                .filter(|name| !name.is_empty())
                .map(str::to_string)
                .collect(),
        }
    }

    /// The tunnel interface is named after the config file (`wg0.conf`).
    pub(crate) fn awg_interface(&self) -> String {
        Path::new(&self.awg_config)
            .file_stem()
            .and_then(|stem| stem.to_str())
            .unwrap_or("wg0")
            .to_string()
    }
}

fn valid_peer(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 128
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

pub(crate) struct Service {
    snapshot: Mutex<Snapshot>,
    snapshot_journal: Journal,
    state_journal: Journal,
    outcomes: Mutex<BTreeMap<&'static str, Outcome>>,
}

impl Service {
    pub(crate) fn open(state_dir: &str) -> Arc<Self> {
        let dir = Path::new(state_dir);
        let snapshot_journal = Journal::new(dir.join("rate-policy.json"));
        let snapshot = snapshot_journal.load().unwrap_or_else(|err| {
            eprintln!("rate policy journal unreadable: {err}");
            Snapshot::default()
        });
        Arc::new(Self {
            snapshot: Mutex::new(snapshot),
            snapshot_journal,
            state_journal: Journal::new(dir.join("rate-state.json")),
            outcomes: Mutex::new(BTreeMap::new()),
        })
    }

    pub(crate) fn snapshot(&self) -> Snapshot {
        self.snapshot
            .lock()
            .map(|snapshot| snapshot.clone())
            .unwrap_or_default()
    }

    /// Stores a newer snapshot and returns the revision now in force.
    pub(crate) fn apply(&self, request: ApplyRatePolicyRequest) -> Result<u64, Status> {
        let mut clients: BTreeMap<String, BTreeMap<String, Rate>> = BTreeMap::new();
        for client in request.clients {
            let valid_name = match client.protocol.as_str() {
                "xray" => valid_email(&client.name),
                "awg" => valid_peer(&client.name),
                _ => return Err(Status::invalid_argument("unknown rate protocol")),
            };
            if !valid_name
                || client.up_kbit > MAX_KBIT
                || client.down_kbit > MAX_KBIT
                || (client.up_kbit == 0 && client.down_kbit == 0)
            {
                return Err(Status::invalid_argument("invalid rate policy"));
            }
            let rate = Rate {
                up_kbit: client.up_kbit,
                down_kbit: client.down_kbit,
            };
            if clients
                .entry(client.protocol)
                .or_default()
                .insert(client.name, rate)
                .is_some()
            {
                return Err(Status::invalid_argument("duplicate rate client"));
            }
        }
        let mut current = self
            .snapshot
            .lock()
            .map_err(|_| Status::internal("rate state unavailable"))?;
        if request.revision <= current.revision {
            return Ok(current.revision);
        }
        let next = Snapshot {
            revision: request.revision,
            clients,
        };
        self.snapshot_journal
            .save(&next)
            .map_err(|_| Status::unavailable("rate policy could not be stored"))?;
        *current = next;
        Ok(current.revision)
    }

    /// Outcome of the latest pass per protocol; "pending" until a pass has
    /// worked from the snapshot in force. Protocols never limited are left out.
    pub(crate) fn statuses(&self) -> Vec<RateProtocolStatus> {
        let snapshot = self.snapshot();
        let outcomes = self
            .outcomes
            .lock()
            .map(|outcomes| outcomes.clone())
            .unwrap_or_default();
        PROTOCOLS
            .iter()
            .filter_map(|protocol| {
                let outcome = outcomes.get(protocol);
                if outcome.is_none() && !snapshot.clients.contains_key(*protocol) {
                    return None;
                }
                let (state, detail) = match outcome {
                    Some(outcome) if outcome.revision >= snapshot.revision => {
                        (outcome.state, outcome.detail.clone())
                    }
                    _ => ("pending", String::new()),
                };
                Some(RateProtocolStatus {
                    protocol: protocol.to_string(),
                    state: state.to_string(),
                    detail,
                })
            })
            .collect()
    }

    /// One pass over both protocols. Without limits and nothing built it
    /// touches neither Xray nor the kernel.
    pub(crate) async fn tick(&self, host: &Host) -> Result<(), String> {
        let snapshot = self.snapshot();
        let mut state: State = self.state_journal.load().map_err(|err| err.to_string())?;
        let mut outcomes = Vec::new();
        let xray = snapshot.protocol("xray");
        if !xray.is_empty() || state.xray.built() {
            let result = xray_rate::step(host, &mut state.xray, &xray).await;
            outcomes.push(("xray", Outcome::from_result(snapshot.revision, result)));
        }
        let awg = snapshot.protocol("awg");
        if !awg.is_empty() || state.awg.built() {
            let host = host.clone();
            let mut awg_state = state.awg.clone();
            let (awg_state, result) = tokio::task::spawn_blocking(move || {
                let result = awg_rate::step(&host, &mut awg_state, &awg);
                (awg_state, result)
            })
            .await
            .map_err(|err| err.to_string())?;
            state.awg = awg_state;
            outcomes.push(("awg", Outcome::from_result(snapshot.revision, result)));
        }
        // Journal even after a failure: what was built must be torn down later.
        self.state_journal
            .save(&state)
            .map_err(|err| err.to_string())?;
        if let Ok(mut current) = self.outcomes.lock() {
            for (protocol, outcome) in outcomes {
                current.insert(protocol, outcome);
            }
        }
        Ok(())
    }

    /// Failures of the latest pass, for the agent log.
    pub(crate) fn failures(&self) -> Vec<String> {
        self.outcomes
            .lock()
            .map(|outcomes| {
                outcomes
                    .iter()
                    .filter(|(_, outcome)| outcome.state != "applied")
                    .map(|(protocol, outcome)| {
                        format!("{protocol} {}: {}", outcome.state, outcome.detail)
                    })
                    .collect()
            })
            .unwrap_or_default()
    }
}

impl AgentState {
    pub(crate) fn apply_rate_policy(
        &self,
        request: ApplyRatePolicyRequest,
    ) -> Result<ApplyRatePolicyResponse, Status> {
        let revision = self.shaping.apply(request)?;
        Ok(ApplyRatePolicyResponse {
            revision,
            protocols: self.shaping.statuses(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agent::v1::RateClientPolicy;

    fn request(revision: u64, clients: &[(&str, &str, u64, u64)]) -> ApplyRatePolicyRequest {
        ApplyRatePolicyRequest {
            revision,
            clients: clients
                .iter()
                .map(|(protocol, name, up, down)| RateClientPolicy {
                    protocol: protocol.to_string(),
                    name: name.to_string(),
                    up_kbit: *up,
                    down_kbit: *down,
                })
                .collect(),
        }
    }

    fn state_dir(name: &str) -> String {
        let dir = std::env::temp_dir().join(format!("np-shaping-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        dir.to_string_lossy().into_owned()
    }

    #[test]
    fn snapshots_only_move_forward_and_survive_a_restart() {
        let dir = state_dir("forward");
        let service = Service::open(&dir);
        let limits = [
            ("xray", "phone", 5000, 8000),
            ("awg", "srv-laptop", 0, 2000),
        ];
        assert_eq!(service.apply(request(2, &limits)).unwrap(), 2);
        assert_eq!(service.apply(request(1, &[])).unwrap(), 2);
        let snapshot = service.snapshot();
        assert_eq!(
            snapshot.protocol("xray")["phone"],
            Rate {
                up_kbit: 5000,
                down_kbit: 8000
            }
        );
        assert_eq!(Service::open(&dir).snapshot(), snapshot);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn invalid_snapshots_are_refused_whole() {
        let dir = state_dir("invalid");
        let service = Service::open(&dir);
        for bad in [
            request(1, &[("ssh", "phone", 1, 1)]),
            request(1, &[("xray", "bad email", 1, 1)]),
            request(1, &[("awg", "bad/name", 1, 1)]),
            request(1, &[("xray", "phone", 0, 0)]),
            request(1, &[("xray", "phone", MAX_KBIT + 1, 1)]),
            request(1, &[("xray", "phone", 1, 1), ("xray", "phone", 2, 2)]),
        ] {
            assert_eq!(
                service.apply(bad).unwrap_err().code(),
                tonic::Code::InvalidArgument
            );
        }
        assert_eq!(service.snapshot(), Snapshot::default());
        // The same name under two protocols is two clients.
        service
            .apply(request(
                1,
                &[("xray", "phone", 1, 1), ("awg", "phone", 1, 1)],
            ))
            .unwrap();
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn statuses_are_pending_until_a_pass_uses_the_snapshot() {
        let dir = state_dir("status");
        let service = Service::open(&dir);
        assert!(service.statuses().is_empty());
        service
            .apply(request(3, &[("xray", "phone", 1000, 0)]))
            .unwrap();
        let states = |service: &Service| {
            service
                .statuses()
                .into_iter()
                .map(|status| (status.protocol, status.state))
                .collect::<Vec<_>>()
        };
        assert_eq!(states(&service), vec![("xray".into(), "pending".into())]);
        service.outcomes.lock().unwrap().insert(
            "xray",
            Outcome::from_result(
                3,
                Err(tc::ShapeError::Unsupported("nft is not installed".into())),
            ),
        );
        assert_eq!(
            states(&service),
            vec![("xray".into(), "unsupported".into())]
        );
        service.apply(request(4, &[])).unwrap();
        // Limits were lifted; the old outcome is outdated until the teardown pass.
        assert_eq!(states(&service), vec![("xray".into(), "pending".into())]);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[tokio::test]
    async fn without_limits_a_pass_touches_nothing() {
        let dir = state_dir("idle");
        let service = Service::open(&dir);
        let host = Host {
            node_key: "node".into(),
            xray_api: "127.0.0.1:1".into(),
            xray_config: "/nonexistent".into(),
            awg_container: "np-no-such-container".into(),
            awg_config: "/nonexistent/wg0.conf".into(),
            uplinks: Vec::new(),
        };
        service.tick(&host).await.unwrap();
        assert!(service.statuses().is_empty());
        service
            .apply(request(1, &[("xray", "phone", 1000, 0)]))
            .unwrap();
        service.tick(&host).await.unwrap();
        assert_eq!(service.statuses()[0].state, "failed");
        let _ = std::fs::remove_dir_all(dir);
    }
}

/// Real kernel and Xray inside a disposable privileged container; driven by
/// `tests/e2e/shaping/run.sh`. `NP_TEST_SHAPING` holds the state directory,
/// the revision and the clients; the outcome is printed as JSON.
#[cfg(test)]
mod e2e {
    use super::*;

    #[tokio::test]
    #[ignore = "changes the network of the host it runs on"]
    async fn shaping_pass() {
        let input: serde_json::Value =
            serde_json::from_str(&std::env::var("NP_TEST_SHAPING").expect("NP_TEST_SHAPING"))
                .unwrap();
        let service = Service::open(input["state_dir"].as_str().unwrap());
        let clients = input["clients"]
            .as_array()
            .unwrap()
            .iter()
            .map(|client| crate::agent::v1::RateClientPolicy {
                protocol: client[0].as_str().unwrap().into(),
                name: client[1].as_str().unwrap().into(),
                up_kbit: client[2].as_u64().unwrap(),
                down_kbit: client[3].as_u64().unwrap(),
            })
            .collect();
        service
            .apply(ApplyRatePolicyRequest {
                revision: input["revision"].as_u64().unwrap(),
                clients,
            })
            .unwrap();
        let host = Host {
            node_key: "node".into(),
            xray_api: "127.0.0.1:10085".into(),
            xray_config: input["xray_config"].as_str().unwrap().into(),
            awg_container: "amnezia-awg".into(),
            awg_config: input["awg_config"].as_str().unwrap().into(),
            uplinks: input["uplinks"]
                .as_array()
                .map(|names| {
                    names
                        .iter()
                        .filter_map(|name| name.as_str().map(str::to_string))
                        .collect()
                })
                .unwrap_or_default(),
        };
        service.tick(&host).await.unwrap();
        let statuses: Vec<_> = service
            .statuses()
            .into_iter()
            .map(|status| serde_json::json!([status.protocol, status.state, status.detail]))
            .collect();
        println!("NP_SHAPING_RESULT {}", serde_json::Value::from(statuses));
    }
}
