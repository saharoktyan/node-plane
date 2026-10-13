use anyhow::{Result, ensure};
use std::{
    path::PathBuf,
    sync::{Arc, atomic::AtomicBool},
};
use zeroize::Zeroizing;

pub fn default_branch() -> &'static str {
    if env!("CARGO_PKG_VERSION").contains('-') {
        "dev"
    } else {
        "main"
    }
}

pub fn workstation_channel() -> &'static str {
    if default_branch() == "dev" {
        "dev"
    } else {
        "stable"
    }
}

pub fn default_state_dir(channel: &str) -> Result<PathBuf> {
    ensure!(
        matches!(channel, "dev" | "stable"),
        "Workstation channel must be dev or stable."
    );
    let root = dirs::data_local_dir().ok_or_else(|| {
        anyhow::anyhow!("Cannot locate your private application data directory. Use --state-dir.")
    })?;
    // Stable and development workstations keep separate private catalogs.
    Ok(root.join(if channel == "dev" {
        "node-plane-dev"
    } else {
        "node-plane"
    }))
}

pub fn validate_install_channel(version: &str, branch: &str, tag: &str) -> Result<()> {
    let expected = if version.contains('-') { "dev" } else { "main" };
    ensure!(
        branch == expected,
        "This Workstation build manages the {expected} controller channel. Transfer the profile to the matching Workstation to change channels."
    );
    if !tag.is_empty() {
        ensure!(
            tag.contains('-') == version.contains('-'),
            "The release tag must match this Workstation's channel."
        );
    }
    Ok(())
}

#[cfg(test)]
mod channel_tests {
    use super::*;
    #[test]
    fn stable_workstation_requires_stable_installation_channel_and_tag() {
        assert!(validate_install_channel("1.0.0", "main", "v1.0.0").is_ok());
        assert!(validate_install_channel("1.0.0", "dev", "").is_err());
        assert!(validate_install_channel("1.0.0", "main", "v1.0.0-alpha.1").is_err());
        assert!(validate_install_channel("1.0.0-alpha.1", "dev", "v1.0.0-alpha.1").is_ok());
        assert!(validate_install_channel("1.0.0-alpha.1", "main", "v1.0.0").is_err());
    }
}

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
            Self::Diagnose => "Diagnostic",
            Self::Update => "Node Plane Updates",
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
            validate_install_channel(env!("CARGO_PKG_VERSION"), &self.branch, &self.tag)?;
            ensure!(
                !self.admin_ids.chars().any(char::is_control),
                "Administrator IDs must fit on a single line."
            );
            let (bot_id, secret) = self.bot_token.split_once(':').unwrap_or_default();
            ensure!(
                valid_telegram_id(bot_id)
                    && secret.len() >= 20
                    && secret
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-'),
                "Telegram bot token: paste the token from BotFather (numeric bot ID, colon, then the secret)."
            );
            ensure!(
                !self.admin_ids.is_empty()
                    && self
                        .admin_ids
                        .split(',')
                        .all(|id| valid_telegram_id(id.trim())),
                "Administrator IDs: enter positive numeric Telegram user IDs, separated by commas; usernames and bot tokens are not IDs."
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
            "BOT_TOKEN={}\nADMIN_IDS={}\nMODE=simple\nNODE_PLANE_BASE_DIR=/opt/node-plane\nNODE_PLANE_APP_DIR=/opt/node-plane/current\nNODE_PLANE_SHARED_DIR=/opt/node-plane/shared\nSSH_KEY=/opt/node-plane/shared/ssh/id_ed25519\nDB_BACKEND=postgres\nNODE_PLANE_UPDATE_BRANCH={}\nNODE_PLANE_CONTROLLER_HOST={}\n",
            *self.bot_token,
            self.admin_ids
                .split(',')
                .map(str::trim)
                .collect::<Vec<_>>()
                .join(","),
            self.branch,
            self.host
        ))
    }
}

fn valid_telegram_id(value: &str) -> bool {
    !value.is_empty()
        && value.bytes().all(|b| b.is_ascii_digit())
        && value.parse::<i64>().is_ok_and(|id| id > 0)
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
        r.branch = default_branch().into();
        assert!(r.validate().is_ok());
        for token in [
            "123456",
            "0:abcdefghijklmnopqrstuvwx",
            "-1:abcdefghijklmnopqrstuvwx",
            "123:short",
            "123:abcdefghijklmnop qrstuvwx",
            "123:abcdefghijklmnopqrstuvwx:extra",
            "9223372036854775808:abcdefghijklmnopqrstuvwx",
        ] {
            r.bot_token = Zeroizing::new(token.into());
            assert!(r.validate().is_err(), "invalid token format accepted");
        }
        r.bot_token = Zeroizing::new("123:abcdefghijklmnopqrstuvwx".into());
        for ids in [
            "",
            "0",
            "-42",
            "@admin",
            "42,",
            "42,,78",
            "42:token",
            "9223372036854775808",
            "42\n78",
        ] {
            r.admin_ids = ids.into();
            assert!(
                r.validate().is_err(),
                "invalid administrator ID format accepted"
            );
        }
        r.admin_ids = "42, 78".into();
        r.bot_token.push('\n');
        assert!(r.validate().is_err());
        r.bot_token.pop();
        r.admin_ids = "42\nBOT_TOKEN=other".into();
        assert!(r.validate().is_err());
        r.action = Action::Diagnose;
        assert!(r.validate().is_ok());
    }
}
