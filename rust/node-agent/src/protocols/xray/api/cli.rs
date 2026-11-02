//! `node-plane-agent xray-api …`: the Xray API for runtime helper scripts.
//!
//! The profile intent journal and its helpers stay in Python for now (VPN plan,
//! section 10, item 5). They call this subcommand instead of
//! `docker exec <container> xray api`. Output is JSON on stdout; errors go to
//! stderr with a distinct exit code so helpers can tell "unknown" from "refused".
use std::collections::BTreeMap;

use serde_json::json;

use super::{DEFAULT_ADDRESS, InboundUser, RouteRule, SourceNet, XrayApi, XrayApiError};

/// Bumped when helpers need a new command. `capabilities` reports it.
const VERSION: u32 = 1;

const EXIT_USAGE: i32 = 2;
const EXIT_UNAVAILABLE: i32 = 3;
const EXIT_REJECTED: i32 = 4;

/// Runs the subcommand when the agent was started as `xray-api …`.
/// Returns `None` for an ordinary agent start.
pub(crate) async fn run(args: &[String]) -> Option<i32> {
    let (first, rest) = args.split_first()?;
    if first != "xray-api" {
        return None;
    }
    Some(match execute(rest).await {
        Ok(output) => {
            println!("{output}");
            0
        }
        Err(Failure::Usage(message)) => {
            eprintln!("usage error: {message}");
            EXIT_USAGE
        }
        Err(Failure::Api(err)) => {
            eprintln!("{err}");
            match err {
                XrayApiError::Unavailable(_) => EXIT_UNAVAILABLE,
                XrayApiError::Rejected(_) | XrayApiError::Invalid(_) => EXIT_REJECTED,
            }
        }
    })
}

enum Failure {
    Usage(String),
    Api(XrayApiError),
}

impl From<XrayApiError> for Failure {
    fn from(err: XrayApiError) -> Self {
        Self::Api(err)
    }
}

struct Options {
    command: String,
    values: BTreeMap<String, String>,
}

impl Options {
    fn parse(args: &[String]) -> Result<Self, Failure> {
        let mut command = None;
        let mut values = BTreeMap::new();
        let mut iter = args.iter();
        while let Some(arg) = iter.next() {
            if let Some(name) = arg.strip_prefix("--") {
                let (name, value) = match name.split_once('=') {
                    Some((name, value)) => (name.to_string(), value.to_string()),
                    None => {
                        let value = iter
                            .next()
                            .ok_or_else(|| Failure::Usage(format!("--{name} needs a value")))?;
                        (name.to_string(), value.clone())
                    }
                };
                if values.insert(name.clone(), value).is_some() {
                    return Err(Failure::Usage(format!("--{name} given twice")));
                }
            } else if command.is_none() {
                command = Some(arg.clone());
            } else {
                return Err(Failure::Usage(format!("unexpected argument: {arg}")));
            }
        }
        let command = command.ok_or_else(|| Failure::Usage("missing command".into()))?;
        Ok(Self { command, values })
    }

    fn take(&mut self, name: &str) -> Result<String, Failure> {
        self.values
            .remove(name)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| Failure::Usage(format!("--{name} is required")))
    }

    fn take_or(&mut self, name: &str, default: &str) -> String {
        self.values
            .remove(name)
            .unwrap_or_else(|| default.to_string())
    }

    fn finish(self) -> Result<(), Failure> {
        match self.values.keys().next() {
            Some(name) => Err(Failure::Usage(format!("unknown option --{name}"))),
            None => Ok(()),
        }
    }
}

pub(crate) fn valid_email(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-'))
}

fn valid_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, c)| {
            if matches!(index, 8 | 13 | 18 | 23) {
                c == b'-'
            } else {
                c.is_ascii_digit() || (b'a'..=b'f').contains(&c)
            }
        })
}

async fn execute(args: &[String]) -> Result<String, Failure> {
    let mut options = Options::parse(args)?;
    let server = options.take_or("server", DEFAULT_ADDRESS);
    if options.command == "capabilities" {
        options.finish()?;
        return Ok(json!({"xray_api": VERSION}).to_string());
    }
    let command = options.command.clone();
    let output = match command.as_str() {
        "inbound-users" => {
            let tag = options.take("tag")?;
            options.finish()?;
            let users = XrayApi::connect(&server).await?.inbound_users(&tag).await?;
            // Same shape as `xray api inbounduser` for the existing helpers.
            let users: Vec<_> = users
                .into_iter()
                .map(|user| json!({"email": user.email, "account": {"id": user.id, "flow": user.flow}}))
                .collect();
            json!({"users": users})
        }
        "add-user" => {
            let tag = options.take("tag")?;
            let user = InboundUser {
                email: options.take("email")?,
                id: options.take("uuid")?,
                flow: options.take_or("flow", ""),
            };
            options.finish()?;
            if !valid_email(&user.email) || !valid_uuid(&user.id) {
                return Err(Failure::Usage("invalid email or uuid".into()));
            }
            if !matches!(user.flow.as_str(), "" | "xtls-rprx-vision") {
                return Err(Failure::Usage("unsupported flow".into()));
            }
            XrayApi::connect(&server)
                .await?
                .add_vless_user(&tag, &user)
                .await?;
            json!({"added": 1})
        }
        "remove-user" => {
            let tag = options.take("tag")?;
            let email = options.take("email")?;
            options.finish()?;
            if !valid_email(&email) {
                return Err(Failure::Usage("invalid email".into()));
            }
            XrayApi::connect(&server)
                .await?
                .remove_user(&tag, &email)
                .await?;
            json!({"removed": 1})
        }
        "query-stats" => {
            let pattern = options.take("pattern")?;
            options.finish()?;
            let stats = XrayApi::connect(&server)
                .await?
                .query_stats(&pattern, false)
                .await?;
            // Same shape as `xray api statsquery`.
            let stat: Vec<_> = stats
                .into_iter()
                .map(|(name, value)| json!({"name": name, "value": value}))
                .collect();
            json!({"stat": stat})
        }
        "online-ips" => {
            let email = options.take("email")?;
            options.finish()?;
            if !valid_email(&email) {
                return Err(Failure::Usage("invalid email".into()));
            }
            let ips = XrayApi::connect(&server).await?.online_ips(&email).await?;
            json!({"ips": ips})
        }
        "uptime" => {
            options.finish()?;
            json!({"uptime": XrayApi::connect(&server).await?.uptime().await?})
        }
        "add-rule" => {
            let rule = RouteRule {
                rule_tag: options.take("rule-tag")?,
                outbound_tag: options.take("outbound")?,
                users: vec![options.take("user")?],
                sources: match options.values.remove("source") {
                    Some(source) => vec![
                        SourceNet::parse(&source).map_err(|err| Failure::Usage(err.to_string()))?,
                    ],
                    None => Vec::new(),
                },
            };
            options.finish()?;
            if !rule.rule_tag.starts_with("np-") || !valid_email(&rule.users[0]) {
                return Err(Failure::Usage(
                    "rule tags start with np-; user must be a profile identity".into(),
                ));
            }
            XrayApi::connect(&server).await?.add_rule(&rule).await?;
            json!({"added": rule.rule_tag})
        }
        "remove-rule" => {
            let rule_tag = options.take("rule-tag")?;
            options.finish()?;
            // Static rules from the config file are not the agent's to remove.
            if !rule_tag.starts_with("np-") {
                return Err(Failure::Usage("only np- rules can be removed".into()));
            }
            XrayApi::connect(&server)
                .await?
                .remove_rule(&rule_tag)
                .await?;
            json!({"removed": rule_tag})
        }
        "list-rules" => {
            options.finish()?;
            let rules: Vec<_> = XrayApi::connect(&server)
                .await?
                .list_rules()
                .await?
                .into_iter()
                .map(|(rule_tag, outbound)| json!({"rule_tag": rule_tag, "outbound": outbound}))
                .collect();
            json!({"rules": rules})
        }
        other => return Err(Failure::Usage(format!("unknown command: {other}"))),
    };
    Ok(output.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| value.to_string()).collect()
    }

    #[tokio::test]
    async fn ordinary_agent_start_is_not_a_subcommand() {
        assert!(run(&[]).await.is_none());
        assert!(run(&args(&["--help"])).await.is_none());
    }

    #[tokio::test]
    async fn capabilities_need_no_running_xray() {
        assert_eq!(run(&args(&["xray-api", "capabilities"])).await, Some(0));
    }

    #[tokio::test]
    async fn usage_errors_are_refused_before_dialing() {
        for case in [
            vec!["xray-api"],
            vec!["xray-api", "nope"],
            vec!["xray-api", "inbound-users"],
            vec!["xray-api", "inbound-users", "--tag", "t", "--extra", "x"],
            vec![
                "xray-api", "add-user", "--tag", "t", "--email", "a b", "--uuid", "x",
            ],
            vec![
                "xray-api",
                "add-user",
                "--tag",
                "t",
                "--email",
                "alice",
                "--uuid",
                "11111111-1111-4111-8111-111111111111",
                "--flow",
                "other",
            ],
            vec![
                "xray-api",
                "remove-user",
                "--tag",
                "t",
                "--email",
                "-alice!",
            ],
            vec![
                "xray-api",
                "remove-rule",
                "--rule-tag",
                "node-plane-private",
            ],
            vec![
                "xray-api",
                "add-rule",
                "--rule-tag",
                "custom",
                "--outbound",
                "node-plane-blocked",
                "--user",
                "alice",
            ],
        ] {
            assert_eq!(run(&args(&case)).await, Some(EXIT_USAGE), "{case:?}");
        }
    }

    #[tokio::test]
    async fn unreachable_api_is_unknown_not_refused() {
        let code = run(&args(&[
            "xray-api",
            "inbound-users",
            "--server",
            "127.0.0.1:1",
            "--tag",
            "reality-tcp",
        ]))
        .await;
        assert_eq!(code, Some(EXIT_UNAVAILABLE));
    }

    #[test]
    fn identities_follow_profile_runtime_names_and_canonical_uuids() {
        assert!(valid_email("tmp_0123abcd"));
        assert!(valid_email("alice-1"));
        assert!(!valid_email(""));
        assert!(!valid_email(&"a".repeat(65)));
        assert!(valid_uuid("11111111-1111-4111-8111-111111111111"));
        assert!(!valid_uuid("11111111-1111-4111-8111-11111111111G"));
        assert!(!valid_uuid("11111111111141118111111111111111"));
    }
}
