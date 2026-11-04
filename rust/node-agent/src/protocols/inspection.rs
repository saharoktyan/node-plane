//! Read-only inspection of one profile: is it on disk, live, and the
//! expected identity? Replaces `inspect-profile.py`.
//!
//! Runs under the protocol config lock and changes nothing. Errors mean the
//! state is unknown, never absent. Only booleans leave the node.
//!
//! The controller asks through the agent API. The profile intent journal
//! runs `inspect-profile.sh`, which only starts `node-plane-agent
//! inspect-profile PROTOCOL NAME [UUID | --allow-unused]`.
use serde::Serialize;
use tonic::Status;

use super::{Setting, awg, process_setting, valid_name, xray};
use crate::agent::v1::{InspectProfileRequest, ProfileInspection};
use crate::protocols::xray::api::cli::valid_uuid;
use crate::state::AgentState;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
pub(crate) struct Inspection {
    pub(crate) disk_present: bool,
    pub(crate) live_present: bool,
    pub(crate) identity_matches: bool,
    pub(crate) config_available: bool,
}

pub(crate) enum Target {
    /// An empty UUID (deletion intents) compares presence only.
    Xray { uuid: String },
    /// `allow_unused`: a peer that never got a key may be proven absent.
    Awg { allow_unused: bool },
}

pub(crate) async fn inspect(
    setting: Setting<'_>,
    name: &str,
    target: &Target,
) -> Result<Inspection, String> {
    if !valid_name(name) {
        return Err("invalid profile name".into());
    }
    match target {
        Target::Xray { uuid } => xray::inspection::inspect(setting, name, uuid).await,
        Target::Awg { allow_unused } => {
            let (node, name, allow_unused) =
                (awg::Node::read(setting), name.to_string(), *allow_unused);
            tokio::task::spawn_blocking(move || {
                awg::inspection::inspect(&node, &name, allow_unused)
            })
            .await
            .map_err(|err| err.to_string())?
        }
    }
}

impl AgentState {
    pub(crate) async fn inspect_profile(
        &self,
        req: InspectProfileRequest,
    ) -> Result<ProfileInspection, Status> {
        let target = match req.protocol_kind.as_str() {
            "xray"
                if req.expected_xray_uuid.is_empty()
                    || valid_uuid(&req.expected_xray_uuid.to_ascii_lowercase()) =>
            {
                Target::Xray {
                    uuid: req.expected_xray_uuid,
                }
            }
            "xray" => return Err(Status::invalid_argument("xray identity is required")),
            "awg" => Target::Awg {
                allow_unused: false,
            },
            _ => return Err(Status::invalid_argument("invalid inspection target")),
        };
        if !valid_name(&req.profile_name) {
            return Err(Status::invalid_argument("invalid inspection target"));
        }
        let setting = |key: &str, default: &str| self.node_env_value(key, default);
        let found = inspect(&setting, &req.profile_name, &target)
            .await
            .map_err(|_| Status::failed_precondition("live profile inspection unavailable"))?;
        Ok(ProfileInspection {
            disk_present: found.disk_present,
            live_present: found.live_present,
            identity_matches: found.identity_matches,
            config_available: found.config_available,
        })
    }
}

/// Runs the subcommand when the agent was started as `inspect-profile …`.
pub(crate) async fn run(args: &[String]) -> Option<i32> {
    let (first, rest) = args.split_first()?;
    if first != "inspect-profile" {
        return None;
    }
    let target = match rest {
        [protocol, _, uuid] if protocol == "xray" => Some(Target::Xray { uuid: uuid.clone() }),
        [protocol, _] if protocol == "awg" => Some(Target::Awg {
            allow_unused: false,
        }),
        [protocol, _, flag] if protocol == "awg" && flag == "--allow-unused" => {
            Some(Target::Awg { allow_unused: true })
        }
        _ => None,
    };
    let result = match target {
        Some(target) => inspect(&process_setting, &rest[1], &target).await,
        None => Err("invalid inspection arguments".into()),
    };
    Some(match result {
        Ok(found) => {
            println!(
                "{}",
                serde_json::to_string(&found).expect("booleans serialize")
            );
            0
        }
        Err(err) => {
            eprintln!("profile inspection unavailable: {err}");
            1
        }
    })
}
