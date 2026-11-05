//! The Xray email of a Node Plane client on one inbound: `NAME.TAG`.
//!
//! Xray keys statistics, online lists and routing rules by email, so a
//! client has its own email on every inbound: its traffic is counted per
//! inbound, and an inbound added to Xray later gets its own counters. The
//! config entry also keeps the client's `name`. Names and tags never
//! contain a dot, so the email splits back unambiguously.
use serde_json::Value;

pub(crate) fn email(name: &str, tag: &str) -> String {
    format!("{name}.{tag}")
}

/// The client's emails on these inbounds.
pub(crate) fn emails<'a>(name: &str, tags: impl IntoIterator<Item = &'a String>) -> Vec<String> {
    tags.into_iter().map(|tag| email(name, tag)).collect()
}

/// Whether `tag` can be part of an email.
pub(crate) fn valid_tag(tag: &str) -> bool {
    !tag.is_empty() && !tag.contains('.')
}

/// Whether `value` is a client's email on an inbound, `NAME.TAG`.
pub(crate) fn valid_email(value: &str) -> bool {
    value.split_once('.').is_some_and(|(name, tag)| {
        super::api::cli::valid_email(name) && super::api::cli::valid_email(tag)
    })
}

/// The client name an email belongs to.
pub(crate) fn name_of(email: &str) -> &str {
    email.split_once('.').map_or(email, |(name, _)| name)
}

/// The client name of a config entry.
pub(crate) fn client_name(client: &Value) -> Option<&str> {
    client
        .get("name")
        .and_then(Value::as_str)
        .or_else(|| client.get("email").and_then(Value::as_str).map(name_of))
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn an_email_names_the_client_and_the_inbound() {
        assert_eq!(email("p_1", "reality-tcp"), "p_1.reality-tcp");
        assert_eq!(name_of("p_1.reality-tcp"), "p_1");
        assert_eq!(name_of("p_1"), "p_1");
        let tags = ["a".to_string(), "b".to_string()];
        assert_eq!(emails("x", &tags), ["x.a", "x.b"]);
        assert!(!valid_tag("in.bound") && !valid_tag("") && valid_tag("reality-xhttp"));
        assert!(valid_email("p_1.reality-tcp"));
        assert!(!valid_email("p_1") && !valid_email("p_1.a.b") && !valid_email(".t"));
        assert_eq!(
            client_name(&json!({"email": "p_1.reality-tcp"})),
            Some("p_1")
        );
        assert_eq!(
            client_name(&json!({"name": "p_1", "email": "p_1.t"})),
            Some("p_1")
        );
    }
}
