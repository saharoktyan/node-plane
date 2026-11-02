//! Client for the Xray gRPC API: HandlerService, StatsService, RoutingService.
//!
//! The Xray container runs with the host network, so the API listens on node
//! loopback and the agent dials it directly instead of `docker exec xray api`.
//! Behaviour of the pinned runtime is recorded in the VPN plan, section 5.7.
pub(crate) mod cli;
mod proto;

use std::collections::BTreeMap;
use std::fmt;
use std::net::IpAddr;
use std::time::Duration;

use prost::Message;
use tonic::transport::{Channel, Endpoint};

use proto::VlessAccount;
use proto::xray::app::proxyman::command::handler_service_client::HandlerServiceClient;
use proto::xray::app::proxyman::command::{
    AddUserOperation, AlterInboundRequest, GetInboundUserRequest, RemoveUserOperation,
};
use proto::xray::app::router::command::routing_service_client::RoutingServiceClient;
use proto::xray::app::router::command::{AddRuleRequest, ListRuleRequest, RemoveRuleRequest};
use proto::xray::app::router::{Config as RouterConfig, RoutingRule, routing_rule::TargetTag};
use proto::xray::app::stats::command::stats_service_client::StatsServiceClient;
use proto::xray::app::stats::command::{GetStatsRequest, QueryStatsRequest, SysStatsRequest};
use proto::xray::common::geodata::{Cidr, CidrRule, IpRule, ip_rule};
use proto::xray::common::protocol::User;
use proto::xray::common::serial::TypedMessage;

/// The API inbound every Node Plane Xray config declares.
pub(crate) const DEFAULT_ADDRESS: &str = "127.0.0.1:10085";
const TIMEOUT: Duration = Duration::from_secs(3);
const VLESS_ACCOUNT: &str = "xray.proxy.vless.Account";

#[derive(Debug)]
pub(crate) enum XrayApiError {
    /// The API could not be reached; state on the node is unknown.
    Unavailable(String),
    /// Xray answered and refused the call.
    Rejected(String),
    /// Bad input or an answer the agent does not understand.
    Invalid(String),
}

impl fmt::Display for XrayApiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unavailable(detail) => write!(f, "Xray API unavailable: {detail}"),
            Self::Rejected(detail) => write!(f, "Xray API rejected the call: {detail}"),
            Self::Invalid(detail) => write!(f, "invalid Xray API exchange: {detail}"),
        }
    }
}

impl From<tonic::Status> for XrayApiError {
    fn from(status: tonic::Status) -> Self {
        match status.code() {
            tonic::Code::Unavailable | tonic::Code::DeadlineExceeded => {
                Self::Unavailable(status.message().to_string())
            }
            _ => Self::Rejected(status.message().to_string()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct InboundUser {
    pub(crate) email: String,
    pub(crate) id: String,
    pub(crate) flow: String,
}

/// A source network in a routing rule: one address or a prefix.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SourceNet {
    address: IpAddr,
    prefix: u8,
}

impl SourceNet {
    /// Parses `203.0.113.7`, `2001:db8::/64` or `[2001:db8::1]` (the online
    /// list reports IPv6 in brackets). Host bits are cleared.
    pub(crate) fn parse(value: &str) -> Result<Self, XrayApiError> {
        let invalid = || XrayApiError::Invalid(format!("invalid source network: {value}"));
        let (address, prefix) = match value.split_once('/') {
            Some((address, prefix)) => (address, Some(prefix)),
            None => (value, None),
        };
        let address = address
            .strip_prefix('[')
            .and_then(|rest| rest.strip_suffix(']'))
            .unwrap_or(address);
        let address: IpAddr = address.parse().map_err(|_| invalid())?;
        let max = if address.is_ipv4() { 32 } else { 128 };
        let prefix = match prefix {
            Some(prefix) => prefix.parse::<u8>().map_err(|_| invalid())?,
            None => max,
        };
        if prefix > max {
            return Err(invalid());
        }
        let address = match address {
            IpAddr::V4(ip) => {
                let mask = u32::MAX.checked_shl(u32::from(32 - prefix)).unwrap_or(0);
                IpAddr::from((u32::from(ip) & mask).to_be_bytes())
            }
            IpAddr::V6(ip) => {
                let mask = u128::MAX.checked_shl(u32::from(128 - prefix)).unwrap_or(0);
                IpAddr::from((u128::from(ip) & mask).to_be_bytes())
            }
        };
        Ok(Self { address, prefix })
    }

    fn to_rule(&self) -> IpRule {
        let ip = match self.address {
            IpAddr::V4(ip) => ip.octets().to_vec(),
            IpAddr::V6(ip) => ip.octets().to_vec(),
        };
        IpRule {
            value: Some(ip_rule::Value::Custom(CidrRule {
                cidr: Some(Cidr {
                    ip,
                    prefix: u32::from(self.prefix),
                }),
                reverse_match: false,
            })),
        }
    }
}

impl fmt::Display for SourceNet {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}/{}", self.address, self.prefix)
    }
}

/// A rule that sends traffic of the given users (and sources) to an outbound.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RouteRule {
    pub(crate) rule_tag: String,
    pub(crate) outbound_tag: String,
    pub(crate) users: Vec<String>,
    pub(crate) sources: Vec<SourceNet>,
}

impl RouteRule {
    fn to_config(&self) -> Result<RouterConfig, XrayApiError> {
        // An empty match list would turn the rule into a catch-all.
        if self.rule_tag.is_empty() || self.outbound_tag.is_empty() || self.users.is_empty() {
            return Err(XrayApiError::Invalid(
                "a rule needs a tag, an outbound and at least one user".into(),
            ));
        }
        Ok(RouterConfig {
            rule: vec![RoutingRule {
                target_tag: Some(TargetTag::Tag(self.outbound_tag.clone())),
                rule_tag: self.rule_tag.clone(),
                user_email: self.users.clone(),
                source_ip: self.sources.iter().map(SourceNet::to_rule).collect(),
                ..Default::default()
            }],
            ..Default::default()
        })
    }
}

fn typed<M: Message>(type_name: &str, message: &M) -> TypedMessage {
    TypedMessage {
        r#type: type_name.to_string(),
        value: message.encode_to_vec(),
    }
}

#[derive(Clone)]
pub(crate) struct XrayApi {
    channel: Channel,
}

impl XrayApi {
    pub(crate) async fn connect(address: &str) -> Result<Self, XrayApiError> {
        let endpoint = Endpoint::from_shared(format!("http://{address}"))
            .map_err(|_| XrayApiError::Invalid(format!("invalid API address: {address}")))?
            .connect_timeout(TIMEOUT)
            .timeout(TIMEOUT);
        let channel = endpoint
            .connect()
            .await
            .map_err(|err| XrayApiError::Unavailable(err.to_string()))?;
        Ok(Self { channel })
    }

    fn handler(&self) -> HandlerServiceClient<Channel> {
        HandlerServiceClient::new(self.channel.clone())
    }

    fn stats(&self) -> StatsServiceClient<Channel> {
        StatsServiceClient::new(self.channel.clone())
    }

    fn routing(&self) -> RoutingServiceClient<Channel> {
        RoutingServiceClient::new(self.channel.clone())
    }

    /// All users of a VLESS inbound. Fails on non-VLESS accounts rather than
    /// reporting them as absent.
    pub(crate) async fn inbound_users(&self, tag: &str) -> Result<Vec<InboundUser>, XrayApiError> {
        let response = self
            .handler()
            .get_inbound_users(GetInboundUserRequest {
                tag: tag.to_string(),
                email: String::new(),
            })
            .await?
            .into_inner();
        response
            .users
            .into_iter()
            .map(|user| {
                let account = user
                    .account
                    .filter(|account| account.r#type == VLESS_ACCOUNT)
                    .ok_or_else(|| XrayApiError::Invalid("inbound user is not VLESS".into()))?;
                let account = VlessAccount::decode(account.value.as_slice())
                    .map_err(|err| XrayApiError::Invalid(err.to_string()))?;
                Ok(InboundUser {
                    email: user.email,
                    id: account.id,
                    flow: account.flow,
                })
            })
            .collect()
    }

    pub(crate) async fn add_vless_user(
        &self,
        tag: &str,
        user: &InboundUser,
    ) -> Result<(), XrayApiError> {
        let account = VlessAccount {
            id: user.id.clone(),
            flow: user.flow.clone(),
            encryption: String::new(),
        };
        let operation = AddUserOperation {
            user: Some(User {
                level: 0,
                email: user.email.clone(),
                account: Some(typed(VLESS_ACCOUNT, &account)),
            }),
        };
        self.handler()
            .alter_inbound(AlterInboundRequest {
                tag: tag.to_string(),
                operation: Some(typed(
                    "xray.app.proxyman.command.AddUserOperation",
                    &operation,
                )),
            })
            .await?;
        Ok(())
    }

    pub(crate) async fn remove_user(&self, tag: &str, email: &str) -> Result<(), XrayApiError> {
        let operation = RemoveUserOperation {
            email: email.to_string(),
        };
        self.handler()
            .alter_inbound(AlterInboundRequest {
                tag: tag.to_string(),
                operation: Some(typed(
                    "xray.app.proxyman.command.RemoveUserOperation",
                    &operation,
                )),
            })
            .await?;
        Ok(())
    }

    /// Counters whose names contain `pattern`, e.g. `user>>>alice>>>`.
    pub(crate) async fn query_stats(
        &self,
        pattern: &str,
        reset: bool,
    ) -> Result<BTreeMap<String, i64>, XrayApiError> {
        let response = self
            .stats()
            .query_stats(QueryStatsRequest {
                pattern: pattern.to_string(),
                reset,
            })
            .await?
            .into_inner();
        Ok(response
            .stat
            .into_iter()
            .map(|stat| (stat.name, stat.value))
            .collect())
    }

    /// Seconds since Xray started. A smaller value than before means a restart.
    pub(crate) async fn uptime(&self) -> Result<u32, XrayApiError> {
        Ok(self
            .stats()
            .get_sys_stats(SysStatsRequest {})
            .await?
            .into_inner()
            .uptime)
    }

    /// Sources with live connections of a user and the Unix time of their
    /// latest new connection.
    ///
    /// Xray creates the online map on a user's first connection, so a user
    /// who never connected is reported as empty. Xray answers the same when
    /// `statsUserOnline` is off: callers must check the policy in the config.
    pub(crate) async fn online_ips(
        &self,
        email: &str,
    ) -> Result<BTreeMap<String, i64>, XrayApiError> {
        let response = self
            .stats()
            .get_stats_online_ip_list(GetStatsRequest {
                name: format!("user>>>{email}>>>online"),
                reset: false,
            })
            .await;
        match response {
            Ok(response) => Ok(response.into_inner().ips.into_iter().collect()),
            Err(status) if status.code() == tonic::Code::NotFound => Ok(BTreeMap::new()),
            Err(status) => Err(status.into()),
        }
    }

    /// Appends a rule. Never replaces the rule list: without the `api` rule
    /// a replacement locks the agent out until Xray restarts (plan 5.7).
    pub(crate) async fn add_rule(&self, rule: &RouteRule) -> Result<(), XrayApiError> {
        let config = rule.to_config()?;
        self.routing()
            .add_rule(AddRuleRequest {
                config: Some(typed("xray.app.router.Config", &config)),
                should_append: true,
            })
            .await?;
        Ok(())
    }

    /// Removes a rule by tag. Removing an absent tag succeeds.
    pub(crate) async fn remove_rule(&self, rule_tag: &str) -> Result<(), XrayApiError> {
        self.routing()
            .remove_rule(RemoveRuleRequest {
                rule_tag: rule_tag.to_string(),
            })
            .await?;
        Ok(())
    }

    /// `(rule_tag, outbound_tag)` in routing order.
    pub(crate) async fn list_rules(&self) -> Result<Vec<(String, String)>, XrayApiError> {
        Ok(self
            .routing()
            .list_rule(ListRuleRequest {})
            .await?
            .into_inner()
            .rules
            .into_iter()
            .map(|item| (item.rule_tag, item.tag))
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_networks_parse_online_list_forms_and_clear_host_bits() {
        assert_eq!(
            SourceNet::parse("203.0.113.7").unwrap().to_string(),
            "203.0.113.7/32"
        );
        assert_eq!(
            SourceNet::parse("[2001:db8:1::26]").unwrap().to_string(),
            "2001:db8:1::26/128"
        );
        assert_eq!(
            SourceNet::parse("2001:db8:1:2:3::9/64")
                .unwrap()
                .to_string(),
            "2001:db8:1:2::/64"
        );
        assert_eq!(
            SourceNet::parse("203.0.113.77/24").unwrap().to_string(),
            "203.0.113.0/24"
        );
        assert_eq!(
            SourceNet::parse("0.0.0.0/0").unwrap().to_string(),
            "0.0.0.0/0"
        );
        for bad in ["", "host", "203.0.113.7/33", "2001:db8::/129", "1.2.3.4/x"] {
            assert!(SourceNet::parse(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn source_rule_carries_address_bytes_by_family() {
        let rule = SourceNet::parse("203.0.113.7").unwrap().to_rule();
        let Some(ip_rule::Value::Custom(custom)) = rule.value else {
            panic!("custom CIDR expected");
        };
        let cidr = custom.cidr.unwrap();
        assert_eq!(cidr.ip, vec![203, 0, 113, 7]);
        assert_eq!(cidr.prefix, 32);
        let rule = SourceNet::parse("2001:db8::/64").unwrap().to_rule();
        let Some(ip_rule::Value::Custom(custom)) = rule.value else {
            panic!("custom CIDR expected");
        };
        assert_eq!(custom.cidr.unwrap().ip.len(), 16);
    }

    #[test]
    fn rules_without_users_are_refused_instead_of_matching_everything() {
        let rule = RouteRule {
            rule_tag: "np-test".into(),
            outbound_tag: "node-plane-blocked".into(),
            users: Vec::new(),
            sources: vec![SourceNet::parse("203.0.113.7").unwrap()],
        };
        assert!(rule.to_config().is_err());
        let rule = RouteRule {
            users: vec!["alice".into()],
            ..rule
        };
        let config = rule.to_config().unwrap();
        assert_eq!(config.rule[0].user_email, vec!["alice".to_string()]);
        assert_eq!(config.rule[0].rule_tag, "np-test");
        assert_eq!(
            config.rule[0].target_tag,
            Some(TargetTag::Tag("node-plane-blocked".into()))
        );
    }

    #[test]
    fn vless_account_round_trips_with_upstream_field_numbers() {
        let account = VlessAccount {
            id: "11111111-1111-4111-8111-111111111111".into(),
            flow: "xtls-rprx-vision".into(),
            encryption: String::new(),
        };
        let bytes = account.encode_to_vec();
        // Field 1 (id) and field 2 (flow) as length-delimited strings.
        assert_eq!(bytes[0], 0x0a);
        assert_eq!(bytes[2 + 36], 0x12);
        assert_eq!(VlessAccount::decode(bytes.as_slice()).unwrap(), account);
    }
}
