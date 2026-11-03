//! Tariffs as the Telegram bot shows them: a term, nodes and linked limits.
//! A tariff's limits apply to its profiles while it is assigned; its term
//! and nodes are copied when it is assigned or renewed.
use serde_json::{Value, json};

/// Access terms offered in days, in cycle order after "No term".
pub const TERMS: [u64; 5] = [7, 30, 90, 180, 365];

pub fn term_label(days: &Value) -> String {
    days.as_u64()
        .map_or_else(|| "No term".into(), |days| format!("{days} days"))
}

/// The next term: No term → 7 → … → 365 → No term.
pub fn next_term(days: &Value) -> Value {
    match days.as_u64() {
        None => json!(TERMS[0]),
        Some(days) => TERMS
            .iter()
            .find(|term| **term > days)
            .map_or(Value::Null, |term| json!(term)),
    }
}

fn every_node(policy: &Value) -> bool {
    policy["rules"]
        .as_array()
        .is_some_and(|rules| rules.iter().any(|rule| rule["scope"] == "all"))
}

fn chosen_regions(policy: &Value) -> Vec<String> {
    policy["rules"]
        .as_array()
        .map(|rules| {
            rules
                .iter()
                .filter(|rule| rule["scope"] == "region")
                .filter_map(|rule| rule["region_id"].as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default()
}

pub fn region_chosen(policy: &Value, region: &str) -> bool {
    chosen_regions(policy).iter().any(|id| id == region)
}

/// The nodes a tariff grants in one line.
pub fn access_label(policy: &Value, regions: &Value) -> String {
    if policy.is_null() {
        return "Keep profile nodes".into();
    }
    if every_node(policy) {
        return "All nodes".into();
    }
    let names: Vec<String> = chosen_regions(policy)
        .iter()
        .map(|id| {
            regions
                .as_array()
                .and_then(|regions| regions.iter().find(|region| region["id"] == id.as_str()))
                .and_then(|region| region["title"].as_str())
                .unwrap_or("?")
                .to_owned()
        })
        .collect();
    if names.is_empty() {
        "No nodes".into()
    } else {
        names.join(", ")
    }
}

fn policy(rules: Vec<Value>) -> Value {
    if rules.is_empty() {
        Value::Null
    } else {
        json!({"explicit_grants":[],"exclusions":[],"rules":rules})
    }
}

/// Keep profile nodes ↔ all nodes.
pub fn toggle_all(current: &Value) -> Value {
    if every_node(current) {
        Value::Null
    } else {
        policy(vec![
            json!({"scope":"all","region_id":null,"protocols":["awg","xray"]}),
        ])
    }
}

/// Adds or removes one region; "all nodes" gives way to chosen regions.
pub fn toggle_region(current: &Value, region: &str) -> Value {
    let mut chosen = chosen_regions(current);
    if let Some(index) = chosen.iter().position(|id| id == region) {
        chosen.remove(index);
    } else {
        chosen.push(region.to_owned());
    }
    policy(
        chosen
            .into_iter()
            .map(|id| json!({"scope":"region","region_id":id,"protocols":["awg","xray"]}))
            .collect(),
    )
}

/// The tariff's own limits, one line each, as the bot lists them.
pub fn limit_lines(tariff: &Value) -> Vec<String> {
    let Some(policies) = tariff["policies"].as_object().filter(|p| !p.is_empty()) else {
        return vec!["Limits: as in the installation".into()];
    };
    policies
        .iter()
        .map(|(key, value)| crate::policy_scope::line_value(key, value))
        .collect()
}

/// What assigning does with the term, as the bot words it.
pub fn expiry_hint(expiry: &str, days: &Value) -> String {
    let days = days.as_u64().unwrap_or(0);
    match expiry {
        "start" => format!("Access lasts {days} days from now."),
        "extend" => format!("Adds {days} days after the remaining time."),
        _ => "The access term does not change.".into(),
    }
}

/// keep → start → extend → keep.
pub fn next_expiry(expiry: &str) -> &'static str {
    match expiry {
        "keep" => "start",
        "start" => "extend",
        _ => "keep",
    }
}

pub fn expiry_label(expiry: &str) -> &'static str {
    match expiry {
        "start" => "Start term",
        "extend" => "Extend",
        _ => "Keep term",
    }
}

/// A tariff error code in the bot's words.
pub fn error_text(code: &str) -> Option<&'static str> {
    Some(match code {
        "tariff_exists" => "A tariff with this name already exists.",
        "tariff_in_use" => "The tariff is assigned to profiles. Assign them another tariff first.",
        "tariff_without_term" => "This tariff has no access term.",
        "tariff_without_access" => "This tariff has no nodes.",
        "admin_access_permanent" => "Administrator profiles keep their term and nodes.",
        "tariff_not_found" => "The tariff no longer exists.",
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terms_cycle_through_no_term() {
        assert_eq!(next_term(&Value::Null), json!(7));
        assert_eq!(next_term(&json!(30)), json!(90));
        assert_eq!(next_term(&json!(365)), Value::Null);
        // A term set elsewhere moves to the next preset.
        assert_eq!(next_term(&json!(45)), json!(90));
        assert_eq!(term_label(&json!(30)), "30 days");
    }

    #[test]
    fn nodes_switch_between_keep_all_and_regions() {
        let regions = json!([{"id":"r1","title":"Europe"},{"id":"r2","title":"Asia"}]);
        let all = toggle_all(&Value::Null);
        assert_eq!(access_label(&all, &regions), "All nodes");
        assert_eq!(toggle_all(&all), Value::Null);
        let europe = toggle_region(&all, "r1");
        let both = toggle_region(&europe, "r2");
        assert_eq!(access_label(&both, &regions), "Europe, Asia");
        assert!(region_chosen(&both, "r2"));
        assert_eq!(
            access_label(&toggle_region(&europe, "r1"), &regions),
            "Keep profile nodes"
        );
    }

    #[test]
    fn limits_and_assignment_texts_match_the_bot() {
        let tariff = json!({"policies":{"speed.up_kbit":5000,"devices.max":2}});
        assert_eq!(
            limit_lines(&tariff),
            [
                "Devices per profile: 2",
                "Upload limit per device: 5 Mbit/s"
            ]
        );
        assert_eq!(next_expiry("extend"), "keep");
        assert_eq!(
            expiry_hint("extend", &json!(30)),
            "Adds 30 days after the remaining time."
        );
        assert!(error_text("tariff_in_use").is_some() && error_text("other").is_none());
    }
}
