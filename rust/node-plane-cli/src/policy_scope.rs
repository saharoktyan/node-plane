//! VPN policies of one profile, device, node or tariff, with the same keys,
//! presets and wording as the Telegram bot. A profile value overrides its
//! tariff, which overrides the installation; a device value overrides its
//! profile, and a node value overrides the installation on that node.
//! "Inherited" removes the own value.
use serde_json::{Value, json};

const GIB: u64 = 1 << 30;
pub const INSTALLATION: &str = "/api/v1/vpn-policies/installation";

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Scope {
    Profile {
        id: String,
        title: String,
    },
    Device {
        profile: String,
        id: String,
        title: String,
    },
    Node {
        key: String,
        title: String,
        xray: bool,
    },
    /// A tariff's limits: one document written whole at its revision.
    Tariff {
        id: String,
        title: String,
    },
}

impl Scope {
    /// Where the scope's values are read and written.
    pub fn path(&self) -> String {
        match self {
            Self::Profile { id, .. } => format!("/api/v1/profiles/{id}/vpn-policies"),
            Self::Device { profile, id, .. } => {
                format!("/api/v1/profiles/{profile}/devices/{id}/vpn-policies")
            }
            Self::Node { .. } => INSTALLATION.into(),
            Self::Tariff { id, .. } => format!("/api/v1/tariffs/{id}"),
        }
    }

    pub fn heading(&self) -> String {
        match self {
            Self::Profile { title, .. } => format!("Profile limits: {title}"),
            Self::Device { title, .. } => format!("Device limits: {title}"),
            Self::Node { title, .. } => format!("Limits on node: {title}"),
            Self::Tariff { title, .. } => format!("Tariff limits: {title}"),
        }
    }

    pub fn profile(&self) -> Option<&str> {
        match self {
            Self::Profile { id, .. } | Self::Device { profile: id, .. } => Some(id),
            Self::Node { .. } | Self::Tariff { .. } => None,
        }
    }

    fn node(&self) -> Option<&str> {
        match self {
            Self::Node { key, .. } => Some(key),
            _ => None,
        }
    }

    /// Keys shown for this scope; `data` decides the conditional ones.
    pub fn keys(&self, data: &Value) -> Vec<&'static str> {
        let limits = [
            "connections.mode",
            "connections.max_sources",
            "speed.up_kbit",
            "speed.down_kbit",
        ];
        match self {
            Self::Profile { .. } | Self::Tariff { .. } => {
                let mut keys = vec!["devices.max"];
                keys.extend(limits);
                keys.extend(["quota.bytes", "quota.reset"]);
                if effective(self, data, "quota.reset") == "interval" {
                    keys.push("quota.interval_days");
                }
                keys.push("quota.on_exhausted");
                if effective(self, data, "quota.on_exhausted") == "throttle" {
                    keys.push("quota.throttle_kbit");
                }
                keys
            }
            Self::Device { .. } => limits.to_vec(),
            Self::Node { xray, .. } => {
                let mut keys = if *xray {
                    vec!["xray.bittorrent"]
                } else {
                    Vec::new()
                };
                keys.extend(limits);
                keys
            }
        }
    }
}

/// Preset values in cycle order; the bot offers the same.
pub fn presets(key: &str) -> Vec<Value> {
    let kbit = || json!([0, 1_000, 5_000, 10_000, 20_000, 50_000, 100_000]);
    let value = match key {
        "devices.max" => json!([1, 2, 3, 5, 10]),
        "connections.mode" => json!(["off", "observe", "enforce"]),
        "connections.max_sources" => json!([1, 2, 3, 5]),
        "speed.up_kbit" | "speed.down_kbit" => kbit(),
        // 0 exempts a profile from the installation quota.
        "quota.bytes" => json!([0, 10 * GIB, 50 * GIB, 100 * GIB, 200 * GIB, 500 * GIB]),
        "quota.reset" => json!(["calendar_month", "monthly_anchor", "interval", "none"]),
        "quota.interval_days" => json!([7, 30]),
        "quota.on_exhausted" => json!(["freeze", "notify", "throttle"]),
        "quota.throttle_kbit" => json!([512, 1_000, 2_000, 5_000]),
        "xray.bittorrent" => json!(["allow", "block"]),
        _ => json!([]),
    };
    value.as_array().cloned().unwrap_or_default()
}

pub fn label(key: &str) -> &'static str {
    match key {
        "devices.max" => "Devices per profile",
        "connections.mode" => "Simultaneous networks",
        "connections.max_sources" => "Networks per VLESS device",
        "speed.up_kbit" => "Upload limit per device",
        "speed.down_kbit" => "Download limit per device",
        "quota.bytes" => "Traffic quota",
        "quota.reset" => "Quota reset",
        "quota.interval_days" => "Quota interval",
        "quota.on_exhausted" => "When quota is used up",
        "quota.throttle_kbit" => "Speed when used up",
        "quota.timezone" => "Quota time zone",
        "xray.bittorrent" => "BitTorrent over VLESS",
        _ => "Policy",
    }
}

pub fn speed(kbit: u64) -> String {
    match kbit {
        0 => "No limit".into(),
        kbit if kbit % 1000 == 0 => format!("{} Mbit/s", kbit / 1000),
        kbit => format!("{kbit} kbit/s"),
    }
}

/// A value as the bot shows it.
pub fn format(key: &str, value: &Value) -> String {
    match key {
        "speed.up_kbit" | "speed.down_kbit" | "quota.throttle_kbit" => {
            speed(value.as_u64().unwrap_or(0))
        }
        "quota.bytes" => match value.as_u64().filter(|bytes| *bytes > 0) {
            Some(bytes) if bytes % GIB == 0 => format!("{} GiB", bytes / GIB),
            Some(bytes) => format!("{bytes} bytes"),
            None => "No limit".into(),
        },
        "devices.max" => value
            .as_u64()
            .map_or_else(|| "No limit".into(), |n| n.to_string()),
        "quota.interval_days" => format!("{} days", value.as_u64().unwrap_or(30)),
        _ => match value.as_str() {
            Some("off") => "Off".into(),
            Some("observe") => "Observe".into(),
            Some("enforce") => "Enforce".into(),
            Some("calendar_month") => "On the 1st".into(),
            Some("monthly_anchor") => "Monthly on the start day".into(),
            Some("interval") => "Every N days".into(),
            Some("none") => "Never".into(),
            Some("freeze") => "Suspend access".into(),
            Some("notify") => "Only notify".into(),
            Some("throttle") => "Limit speed".into(),
            Some("allow") => "Allowed".into(),
            Some("block") => "Blocked".into(),
            Some(other) => other.into(),
            None if value.is_null() => "No limit".into(),
            None => value.to_string(),
        },
    }
}

fn listing_key<'a>(listing: &'a Value, key: &str) -> Option<&'a Value> {
    listing["keys"]
        .as_array()?
        .iter()
        .find(|item| item["key"] == key)
}

/// The scope's own value (Null when inherited) and its revision.
pub fn own(scope: &Scope, data: &Value, key: &str) -> (Value, u64) {
    if matches!(scope, Scope::Tariff { .. }) {
        let tariff = &data["subject"];
        return (
            tariff["policies"][key].clone(),
            tariff["revision"].as_u64().unwrap_or(0),
        );
    }
    let item = match scope.node() {
        Some(node) => data["installation"]["values"]
            .as_array()
            .and_then(|values| {
                values.iter().find(|item| {
                    item["key"] == key && item["node_key"] == node && item["protocol"].is_null()
                })
            }),
        None => listing_key(&data["subject"], key),
    };
    item.map_or((Value::Null, 0), |item| {
        (
            item["value"].clone(),
            item["revision"].as_u64().unwrap_or(0),
        )
    })
}

/// The profile's tariff value of `key`, if its tariff sets one.
fn tariff_value(scope: &Scope, data: &Value, key: &str) -> Option<Value> {
    if !matches!(scope, Scope::Profile { .. }) {
        return None;
    }
    listing_key(&data["subject"], key)
        .map(|item| item["tariff"].clone())
        .filter(|value| !value.is_null())
}

/// What the scope would get without its own value.
pub fn inherited(scope: &Scope, data: &Value, key: &str) -> Value {
    if let Some(value) = tariff_value(scope, data, key) {
        return value;
    }
    let parent = match scope {
        Scope::Device { .. } => &data["profile"],
        _ => &data["installation"],
    };
    listing_key(parent, key).map_or(Value::Null, |item| item["effective"].clone())
}

pub fn effective(scope: &Scope, data: &Value, key: &str) -> Value {
    let (own, _) = own(scope, data, key);
    if own.is_null() {
        inherited(scope, data, key)
    } else {
        own
    }
}

/// "Label: value" for an installation value.
pub fn line_value(key: &str, value: &Value) -> String {
    format!("{}: {}", label(key), format(key, value))
}

/// Delivery of node-enforced limits, as the bot's Nodes section: how many
/// nodes have the current limits and how many cannot apply them.
pub fn delivery_line(name: &str, delivery: &Value) -> Option<String> {
    let items = delivery["items"]
        .as_array()
        .filter(|items| !items.is_empty())?;
    let current = items
        .iter()
        .filter(|item| item["status"] == "current")
        .count();
    let mut line = format!("{name} on nodes: {current} of {} up to date", items.len());
    let unable = items
        .iter()
        .filter(|item| {
            item["protocols"].as_array().is_some_and(|protocols| {
                protocols
                    .iter()
                    .any(|p| matches!(p["state"].as_str(), Some("failed" | "unsupported")))
            })
        })
        .count();
    if unable > 0 {
        line += &format!(" · {unable} cannot apply them");
    }
    Some(line)
}

/// The menu line for a key.
pub fn line(scope: &Scope, data: &Value, key: &str) -> String {
    let (own, _) = own(scope, data, key);
    if own.is_null() {
        let from = if matches!(scope, Scope::Device { .. }) {
            "profile"
        } else if tariff_value(scope, data, key).is_some() {
            "tariff"
        } else {
            "installation"
        };
        format!(
            "{}: as {from} ({})",
            label(key),
            format(key, &inherited(scope, data, key))
        )
    } else {
        format!("{}: {}", label(key), format(key, &own))
    }
}

/// The change one press makes: inherited → first preset → … → inherited.
pub fn next_change(scope: &Scope, data: &Value, key: &str) -> Value {
    let (own, revision) = own(scope, data, key);
    let presets = presets(key);
    let next = if own.is_null() {
        presets.first().cloned().unwrap_or(Value::Null)
    } else {
        presets
            .iter()
            .position(|preset| *preset == own)
            .and_then(|index| presets.get(index + 1))
            .cloned()
            .unwrap_or(Value::Null)
    };
    if matches!(scope, Scope::Tariff { .. }) {
        // A tariff is written whole; its revision travels as If-Match.
        let mut policies = data["subject"]["policies"].clone();
        if let Some(policies) = policies.as_object_mut() {
            if next.is_null() {
                policies.remove(key);
            } else {
                policies.insert(key.into(), next);
            }
        }
        return json!({"policies":policies,"_revision":revision});
    }
    let mut change = json!({"key":key,"value":next,"revision":revision});
    if let Some(node) = scope.node() {
        change["node_key"] = json!(node);
    }
    json!({"changes":[change]})
}

/// A save body without the If-Match revision a tariff body carries.
pub fn split_revision(body: &Value) -> (Value, Option<u64>) {
    let mut body = body.clone();
    let revision = body
        .as_object_mut()
        .and_then(|body| body.remove("_revision"))
        .and_then(|revision| revision.as_u64());
    (body, revision)
}

/// The profile's open quota period in one line.
pub fn quota_line(quota: &Value) -> Option<String> {
    let period = quota.get("period").filter(|period| period.is_object())?;
    let limit = quota["limit_bytes"].as_u64()?;
    let used = period["used_bytes"].as_u64().unwrap_or(0);
    let gib = |bytes: u64| format!("{:.1} GiB", bytes as f64 / GIB as f64);
    let mut line = format!("Quota period: {} of {}", gib(used), gib(limit));
    if let Some(end) = period["end"].as_str() {
        line += &format!(" · until {}", &end[..end.len().min(10)]);
    }
    if period["blocked"] == true {
        line += " · access suspended";
    } else if let Some(kbit) = quota["throttled"].as_u64() {
        line += &format!(" · speed limited to {}", speed(kbit));
    }
    Some(line)
}

/// Whether the profile's quota has a start day an administrator can move.
pub fn has_anchor(quota: &Value) -> bool {
    quota["anchor_at"].is_string()
        && matches!(quota["reset"].as_str(), Some("monthly_anchor" | "interval"))
}

/// A typed anchor day: YYYY-MM-DD with a real calendar date.
pub fn valid_day(input: &str) -> bool {
    let input = input.trim();
    let parts: Vec<u32> = input
        .split('-')
        .filter_map(|part| part.parse().ok())
        .collect();
    let [year, month, day] = parts[..] else {
        return false;
    };
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let days = match month {
        2 if leap => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        1..=12 => 31,
        _ => 0,
    };
    input.len() == 10 && (1970..=9999).contains(&year) && (1..=days).contains(&day)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn node() -> Scope {
        Scope::Node {
            key: "n1".into(),
            title: "Riga".into(),
            xray: true,
        }
    }

    #[test]
    fn node_values_come_from_the_installation_values() {
        let data = json!({"installation":{"keys":[{"key":"speed.up_kbit","effective":20000}],
            "values":[{"key":"speed.up_kbit","node_key":"n1","protocol":null,"value":5000,"revision":2},
                      {"key":"speed.up_kbit","node_key":"n2","protocol":null,"value":1000,"revision":1}]}});
        assert_eq!(own(&node(), &data, "speed.up_kbit"), (json!(5000), 2));
        assert_eq!(
            line(&node(), &data, "speed.up_kbit"),
            "Upload limit per device: 5 Mbit/s"
        );
        assert_eq!(
            next_change(&node(), &data, "speed.up_kbit"),
            json!({"changes":[{"key":"speed.up_kbit","value":10000,"revision":2,"node_key":"n1"}]})
        );
        assert_eq!(
            line(&node(), &data, "speed.down_kbit"),
            "Download limit per device: as installation (No limit)"
        );
        assert_eq!(node().keys(&data)[0], "xray.bittorrent");
    }

    #[test]
    fn the_cycle_starts_and_ends_at_inherited() {
        let device = Scope::Device {
            profile: "p".into(),
            id: "d".into(),
            title: "Phone".into(),
        };
        let data = |value: Value| {
            json!({"subject":{"keys":[{"key":"connections.mode","value":value,"revision":4}]},
                   "profile":{"keys":[{"key":"connections.mode","effective":"enforce"}]}})
        };
        assert_eq!(
            line(&device, &data(Value::Null), "connections.mode"),
            "Simultaneous networks: as profile (Enforce)"
        );
        assert_eq!(
            next_change(&device, &data(Value::Null), "connections.mode")["changes"][0]["value"],
            "off"
        );
        assert_eq!(
            next_change(&device, &data(json!("enforce")), "connections.mode")["changes"][0]["value"],
            Value::Null
        );
        assert_eq!(device.path(), "/api/v1/profiles/p/devices/d/vpn-policies");
    }

    #[test]
    fn tariff_values_are_written_whole_and_profiles_inherit_them() {
        let tariff = Scope::Tariff {
            id: "t".into(),
            title: "Basic".into(),
        };
        let data = json!({"subject":{"policies":{"speed.up_kbit":5000},"revision":3},
            "installation":{"keys":[{"key":"speed.down_kbit","effective":20000}]}});
        assert_eq!(own(&tariff, &data, "speed.up_kbit"), (json!(5000), 3));
        assert_eq!(
            line(&tariff, &data, "speed.down_kbit"),
            "Download limit per device: as installation (20 Mbit/s)"
        );
        let body = next_change(&tariff, &data, "speed.down_kbit");
        assert_eq!(
            split_revision(&body),
            (
                json!({"policies":{"speed.up_kbit":5000,"speed.down_kbit":0}}),
                Some(3)
            )
        );
        assert_eq!(
            next_change(&tariff, &data, "speed.up_kbit")["policies"],
            json!({"speed.up_kbit":10000})
        );
        assert_eq!(tariff.path(), "/api/v1/tariffs/t");
        let profile = Scope::Profile {
            id: "p".into(),
            title: "Alice".into(),
        };
        let data = json!({"subject":{"keys":[{"key":"speed.up_kbit","value":null,"revision":0,"tariff":5000}]},
            "installation":{"keys":[{"key":"speed.up_kbit","effective":20000}]}});
        assert_eq!(
            line(&profile, &data, "speed.up_kbit"),
            "Upload limit per device: as tariff (5 Mbit/s)"
        );
    }

    #[test]
    fn profile_keys_follow_the_effective_quota_settings() {
        let profile = Scope::Profile {
            id: "p".into(),
            title: "Alice".into(),
        };
        let data = json!({"subject":{"keys":[{"key":"quota.reset","value":"interval","revision":1}]},
            "installation":{"keys":[{"key":"quota.on_exhausted","effective":"throttle"}]}});
        let keys = profile.keys(&data);
        assert!(keys.contains(&"quota.interval_days") && keys.contains(&"quota.throttle_kbit"));
        assert!(valid_day("2026-02-28") && !valid_day("2026-02-30") && !valid_day("2026-2-1"));
        let quota = json!({"limit_bytes":GIB,"throttled":1000,"reset":"interval","anchor_at":"2026-01-01T00:00:00+00:00",
            "period":{"used_bytes":GIB,"end":"2026-02-01T00:00:00+00:00","blocked":false}});
        assert_eq!(
            quota_line(&quota).unwrap(),
            "Quota period: 1.0 GiB of 1.0 GiB · until 2026-02-01 · speed limited to 1 Mbit/s"
        );
        assert!(has_anchor(&quota));
    }
}
