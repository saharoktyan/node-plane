//! Signed self-update of the agent.
//!
//! The controller names a release and where it is published; the agent
//! fetches it itself and installs it only when the release key signed it.
mod install;
pub(crate) mod release;

#[cfg(test)]
mod tests;

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use tonic::Status;

use crate::agent::v1::{UpdateAgentRequest, UpdateAgentResponse};
use crate::config::AgentConfig;
use crate::state::AgentState;

pub(crate) use install::Outcome;

const OWN: &str = env!("NODE_PLANE_BINARY_VERSION");
static RUNNING: Mutex<()> = Mutex::new(());

fn stage(state_dir: &str) -> PathBuf {
    Path::new(state_dir).join("update")
}

fn host(state_dir: &str) -> Result<install::Host, String> {
    let binary = std::env::current_exe().map_err(|_| "agent_update_binary_unknown".to_string())?;
    Ok(install::Host {
        binary,
        stage: stage(state_dir),
        key: release::release_key(),
        own: OWN.to_string(),
        fetch: install::fetch,
        schedule: install::schedule,
    })
}

fn status(code: String) -> Status {
    match code.as_str() {
        "agent_update_invalid" => Status::invalid_argument(code),
        "agent_update_download_failed" | "agent_update_downloader_missing" => {
            Status::unavailable(code)
        }
        _ => Status::failed_precondition(code),
    }
}

/// Install `version` from `base`; the caller restarts the agent on `Updating`.
pub(crate) fn update(state_dir: &str, base: &str, version: &str) -> Result<Outcome, String> {
    let _running = RUNNING
        .try_lock()
        .map_err(|_| "agent_update_in_progress".to_string())?;
    install::update(&host(state_dir)?, base, version)
}

/// Called once the agent serves: the update to this version worked.
pub(crate) fn confirm(config: &AgentConfig) {
    if install::confirm(&stage(&config.state_dir), OWN) {
        println!("agent update to {OWN} confirmed");
    }
}

impl AgentState {
    pub(crate) fn update_agent(
        &self,
        request: UpdateAgentRequest,
    ) -> Result<(UpdateAgentResponse, Outcome), Status> {
        let outcome = update(
            &self.config.state_dir,
            &request.release_url,
            &request.version,
        )
        .map_err(status)?;
        let status = match outcome {
            Outcome::Current => "current",
            Outcome::Updating => "updating",
        };
        Ok((
            UpdateAgentResponse {
                status: status.to_string(),
                version: OWN.to_string(),
                rolled_back: install::rolled_back(&stage(&self.config.state_dir))
                    .unwrap_or_default(),
            },
            outcome,
        ))
    }

    pub(crate) fn update_rolled_back(&self) -> String {
        install::rolled_back(&stage(&self.config.state_dir)).unwrap_or_default()
    }
}

pub(crate) fn restart() {
    let _ = std::process::Command::new("systemctl")
        .args(["restart", "--no-block", "node-plane-agent.service"])
        .status();
}

pub(crate) fn run(args: &[String]) -> Option<i32> {
    let (first, rest) = args.split_first()?;
    let result = match (first.as_str(), rest) {
        ("version", []) => Ok(OWN.to_string()),
        ("self-update", [base, version]) => {
            let config = AgentConfig::load();
            // An explicit run retries a version the guard rolled back.
            install::clear_rolled_back(&stage(&config.state_dir));
            update(&config.state_dir, base, version).map(|outcome| match outcome {
                Outcome::Current => format!("agent {OWN} is current"),
                Outcome::Updating => {
                    restart();
                    format!("agent updated to {version}; restarting")
                }
            })
        }
        ("self-update", _) => Err("usage: self-update RELEASE_URL VERSION".into()),
        ("update-guard", [binary, stage]) => install::guard(Path::new(binary), Path::new(stage))
            .map(|rolled| match rolled {
                Some(version) => {
                    restart();
                    format!("agent update to {version} was not confirmed; rolled back")
                }
                None => "agent update confirmed".to_string(),
            }),
        ("update-guard", _) => Err("usage: update-guard BINARY STAGE".into()),
        _ => return None,
    };
    Some(match result {
        Ok(text) => {
            println!("{text}");
            0
        }
        Err(err) => {
            eprintln!("{err}");
            1
        }
    })
}
