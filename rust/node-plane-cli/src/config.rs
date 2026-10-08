use anyhow::{Result, ensure};
use std::{
    path::PathBuf,
    sync::{Arc, atomic::AtomicBool},
};
use zeroize::Zeroizing;

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Install,
    Diagnose,
    Update,
    PrepareNode,
}
impl Action {
    pub const ALL: [Self; 4] = [
        Self::Install,
        Self::Update,
        Self::PrepareNode,
        Self::Diagnose,
    ];
    pub fn label(self) -> &'static str {
        match self {
            Self::Install => "Install Node Plane",
            Self::Diagnose => "Check installation",
            Self::Update => "Update Node Plane",
            Self::PrepareNode => "Nodes",
        }
    }
}
pub struct WorkflowOptions {
    pub yes: bool,
    pub repair: bool,
    pub stop: Arc<AtomicBool>,
    pub account: String,
    pub operation: Option<uuid::Uuid>,
    pub target_host: String,
    pub target_port: u16,
    pub target_user: String,
}
impl Default for WorkflowOptions {
    fn default() -> Self {
        Self {
            yes: false,
            repair: false,
            stop: Arc::new(AtomicBool::new(false)),
            account: String::new(),
            operation: None,
            target_host: String::new(),
            target_port: 22,
            target_user: "root".into(),
        }
    }
}

pub struct Request {
    pub action: Action,
    pub host: String,
    pub port: u16,
    pub user: String,
    pub state_dir: PathBuf,
    pub tag: String,
    pub branch: String,
    pub admin_ids: String,
    pub bot_token: Zeroizing<String>,
    pub workflow: WorkflowOptions,
}

impl Request {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            !self.host.is_empty()
                && self.host.len() <= 253
                && self
                    .host
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || ".:-".contains(c)),
            "Enter a hostname or IP address without a scheme, username or path."
        );
        ensure!(self.port != 0, "SSH port must be between 1 and 65535.");
        ensure!(
            !self.user.is_empty()
                && self.user.len() <= 64
                && self
                    .user
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || "_-".contains(c)),
            "Enter a valid SSH username."
        );
        ensure!(
            matches!(self.branch.as_str(), "main" | "dev")
                || (self.action != Action::Install && self.branch.is_empty()),
            "Release branch must be main or dev."
        );
        if !self.tag.is_empty() {
            ensure!(
                valid_tag(&self.tag),
                "Release must be a version tag, for example v0.4.3-alpha.48; leave blank for latest."
            );
        }
        if self.action == Action::Install {
            ensure!(
                !self.admin_ids.chars().any(char::is_control),
                "Administrator IDs must fit on a single line."
            );
            let (bot_id, secret) = self.bot_token.split_once(':').unwrap_or_default();
            ensure!(
                !bot_id.is_empty()
                    && bot_id.bytes().all(|b| b.is_ascii_digit())
                    && secret.len() >= 20
                    && secret
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-'),
                "Enter the Telegram bot token from BotFather."
            );
            ensure!(
                !self.admin_ids.is_empty()
                    && self.admin_ids.split(',').all(|id| id
                        .trim()
                        .bytes()
                        .all(|b| b.is_ascii_digit())
                        && id.trim().parse::<u64>().is_ok_and(|id| id > 0)),
                "Administrator IDs must be positive Telegram numeric IDs, separated by commas."
            );
        }
        if matches!(
            self.action,
            Action::Update | Action::PrepareNode | Action::Diagnose
        ) && !self.workflow.account.is_empty()
        {
            ensure!(
                uuid::Uuid::parse_str(&self.workflow.account).is_ok()
                    || (self.workflow.account.bytes().all(|b| b.is_ascii_digit())
                        && self
                            .workflow
                            .account
                            .parse::<u64>()
                            .is_ok_and(|id| id > 0 && id < 2_u64.pow(63))),
                "Account must be a backend UUID or a Telegram administrator numeric ID."
            );
        }
        if self.action == Action::PrepareNode {
            ensure!(
                !self.workflow.target_host.is_empty()
                    && self.workflow.target_host.len() <= 253
                    && self
                        .workflow
                        .target_host
                        .chars()
                        .all(|c| c.is_ascii_alphanumeric() || ".:-".contains(c)),
                "Enter the target node hostname or IP address."
            );
            ensure!(
                self.workflow.target_port > 0
                    && !self.workflow.target_user.is_empty()
                    && self
                        .workflow
                        .target_user
                        .chars()
                        .all(|c| c.is_ascii_alphanumeric() || "_-".contains(c)),
                "Enter a valid target SSH user and port."
            );
        }
        Ok(())
    }

    pub fn env_file(&self) -> Zeroizing<String> {
        Zeroizing::new(format!(
            "BOT_TOKEN={}\nADMIN_IDS={}\nMODE=simple\nNODE_PLANE_BASE_DIR=/opt/node-plane\nNODE_PLANE_APP_DIR=/opt/node-plane/current\nNODE_PLANE_SHARED_DIR=/opt/node-plane/shared\nSSH_KEY=/opt/node-plane/shared/ssh/id_ed25519\nDB_BACKEND=postgres\nNODE_PLANE_UPDATE_BRANCH={}\n",
            *self.bot_token,
            self.admin_ids
                .split(',')
                .map(str::trim)
                .collect::<Vec<_>>()
                .join(","),
            self.branch
        ))
    }
}

fn valid_tag(tag: &str) -> bool {
    let value = tag.strip_prefix('v').unwrap_or(tag);
    let (version, suffix) = value
        .split_once('-')
        .map_or((value, None), |(v, s)| (v, Some(s)));
    let pieces: Vec<_> = version.split('.').collect();
    pieces.len() == 3
        && pieces
            .iter()
            .all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()))
        && suffix.is_none_or(|s| {
            s.strip_prefix("alpha.")
                .is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
        })
}

pub fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_version_refs() {
        for tag in ["v0.4.3-alpha.48", "0.4.2", "v1.0.0"] {
            assert!(valid_tag(tag));
        }
        for tag in [
            "main",
            "v1.2",
            "v1.2.3;touch /tmp/pwn",
            "v1.2.3$(id)",
            "v1.2.3-alpha.",
            "--help",
        ] {
            assert!(!valid_tag(tag));
        }
    }
    #[test]
    fn quoting_preserves_literals() {
        assert_eq!(shell_quote("a'b$(id)"), "'a'\"'\"'b$(id)'");
    }
    #[test]
    fn rejects_multiline_secrets_and_bad_ids() {
        let mut r = Request {
            action: Action::Install,
            host: "vps.example".into(),
            port: 22,
            user: "root".into(),
            state_dir: PathBuf::new(),
            tag: String::new(),
            branch: "dev".into(),
            admin_ids: "42, 78".into(),
            bot_token: Zeroizing::new("123:abcdefghijklmnopqrstuvwx".into()),
            workflow: WorkflowOptions::default(),
        };
        assert!(r.validate().is_ok());
        r.bot_token.push('\n');
        assert!(r.validate().is_err());
        r.bot_token.pop();
        r.admin_ids = "42\nBOT_TOKEN=other".into();
        assert!(r.validate().is_err());
        r.action = Action::Diagnose;
        assert!(r.validate().is_ok());
    }
}
