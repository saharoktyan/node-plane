//! Node retirement follows the backend's worker-driven removal saga.
use anyhow::{Result, ensure};
use serde_json::Value;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Full,
    RegistryOnly,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Section {
    #[default]
    Root,
    Reinstall,
    Removal,
}

#[derive(Clone, Debug)]
pub struct Browser {
    pub key: String,
    pub snapshot: Value,
    pub confirmation: Option<Mode>,
    pub scroll: usize,
    pub section: Section,
}
impl Browser {
    pub fn new(key: String, snapshot: Value, confirmation: Option<Mode>) -> Result<Self> {
        ensure!(
            snapshot["node_key"] == key,
            "Maintenance belongs to another server"
        );
        Ok(Self {
            key,
            snapshot,
            confirmation,
            scroll: 0,
            section: if confirmation.is_some() {
                Section::Removal
            } else {
                Section::Root
            },
        })
    }
    pub fn removed(&self) -> bool {
        matches!(
            self.snapshot["status"].as_str(),
            Some("removed" | "removed_registry_only" | "removed_unprovisioned")
        )
    }
    pub fn active(&self) -> bool {
        self.confirmation.is_none()
            && matches!(
                self.snapshot["removal_status"].as_str(),
                Some("queued" | "running" | "blocked")
            )
    }
    pub fn blocked(&self) -> bool {
        self.snapshot["removal_status"] == "blocked"
    }
    pub fn lines(&self) -> Vec<String> {
        let profiles = self.snapshot["affected_profiles"].as_u64().unwrap_or(0);
        if let Some(mode) = self.confirmation {
            return match mode {
                Mode::Full => vec![
                    format!("• Revoke access for {profiles} profiles."),
                    "• Remove VPN containers, configuration and runtime files.".into(),
                    "• Remove the agent, its service and credentials.".into(),
                    "• Remove the server record after cleanup is verified.".into(),
                ],
                Mode::RegistryOnly => vec![
                    format!(
                        "• Remove this server from {profiles} profiles and future access rules."
                    ),
                    "• Remove the server record from Node Plane.".into(),
                    "• Files, containers and the agent remain on the server.".into(),
                    "Existing VPN configurations may continue working.".into(),
                ],
            };
        }
        match self.snapshot["status"].as_str() {
            Some("removed") => return vec!["Server removed. Cleanup verified.".into()],
            Some("removed_registry_only") => {
                return vec![
                    "Server record removed.".into(),
                    "Existing VPN configurations may continue working.".into(),
                ];
            }
            Some("removed_unprovisioned") => return vec!["Unused server record removed.".into()],
            _ => {}
        }
        let mut lines = vec![format!(
            "Status: {}",
            if self.blocked() {
                "Needs attention"
            } else if self.active() {
                "Removing"
            } else if self.snapshot["status"] == "draining" {
                "Draining"
            } else {
                "Ready"
            }
        )];
        if self.active() || self.blocked() || self.snapshot["status"] == "draining" {
            let phase = match self.snapshot["cleanup_phase"]
                .as_str()
                .unwrap_or("not_started")
            {
                "not_started" if self.snapshot["revocations_complete"] != true => {
                    "Revoking profile access"
                }
                "not_started" | "preparing" => "Preparing cleanup",
                "prepared" => "Removing VPN runtime",
                "runtime_deleted" => "Removing the agent",
                "uninstall_scheduled" | "uninstall_uncertain" => "Verifying cleanup",
                _ => "Waiting for the worker",
            };
            lines.push(format!("Step: {phase}"));
            lines.push(format!(
                "Pending tasks: {} · Blocked: {}",
                self.snapshot["pending_tasks"].as_u64().unwrap_or(0),
                self.snapshot["blocked_tasks"].as_u64().unwrap_or(0)
            ));
        }
        if let Some(code) = self.snapshot["error_code"]
            .as_str()
            .filter(|code| !code.is_empty())
        {
            let reason = match code {
                "node_agent_unavailable" | "node_agent_unconfigured" => {
                    "The agent is unreachable. Check the connection before retrying."
                }
                "host_verification_failed" => {
                    "Cleanup could not be verified. Some server resources may remain."
                }
                "node_cleanup_scope_unconfirmed" => {
                    "VPN runtime is present or could not be checked, but this node has no confirmed VPN installation. Full removal stopped."
                }
                "node_revocations_blocked" => "Profile access revocation needs recovery.",
                "verification_key_unavailable" | "independent_verification_key_required" => {
                    "SSH access is required to verify cleanup."
                }
                "node_settings_uncertain"
                | "node_operation_pending"
                | "agent_rollout_uncertain"
                | "node_bootstrap_pending" => {
                    "Recover the unfinished installation operation first."
                }
                _ => "Cleanup needs attention. Check diagnostics and worker logs.",
            };
            lines.push(reason.into());
            lines.push(format!("Code: {code}"));
        }
        lines
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn pending_and_blocked_removal_are_polled_and_terminal_modes_remain_distinct() {
        let mut state = Browser::new("lv1".into(), json!({"node_key":"lv1","status":"draining","removal_status":"running","cleanup_phase":"uninstall_scheduled"}), None).unwrap();
        assert!(state.active());
        assert!(
            state
                .lines()
                .iter()
                .any(|line| line.contains("Verifying cleanup"))
        );
        state.confirmation = Some(Mode::Full);
        assert!(!state.active());
        state.confirmation = None;
        state.snapshot["removal_status"] = json!("blocked");
        assert!(state.active());
        assert!(state.blocked());
        for status in ["removed", "removed_registry_only", "removed_unprovisioned"] {
            state.snapshot = json!({"node_key":"lv1","status":status});
            assert!(state.removed());
            assert!(!state.active());
            assert_eq!(
                state
                    .lines()
                    .iter()
                    .any(|line| line.contains("may continue")),
                status == "removed_registry_only"
            );
        }
        assert!(Browser::new("lv1".into(), json!({"node_key":"lv2"}), None).is_err());
    }
    #[test]
    fn confirmation_explains_affected_profiles_and_remote_cleanup() {
        let state = Browser::new(
            "lv1".into(),
            json!({"node_key":"lv1","affected_profiles":3}),
            Some(Mode::Full),
        )
        .unwrap();
        assert!(state.lines()[0].contains("3 profiles"));
        let mut registry = state;
        registry.confirmation = Some(Mode::RegistryOnly);
        assert!(
            registry
                .lines()
                .iter()
                .any(|line| line.contains("remain on the server"))
        );
    }
}
