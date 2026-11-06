//! The installation's services as the controller's catalog reports them
//! (`GET /api/v1/services`): system ones (the web cabinet's relay, the
//! Telegram bot), built-in VPN and, later, services from manifests.
use serde_json::Value;

const KINDS: [(&str, &str, &str); 3] = [
    ("system", "System", "What the installation itself runs."),
    (
        "builtin",
        "Built-in",
        "Part of Node Plane; people get access to them.",
    ),
    ("user", "Custom", "Added from manifests."),
];

pub fn name(service: &str) -> &str {
    match service {
        "vpn" => "VPN",
        "relay" => "Relay",
        "web_cabinet" => "Web cabinet",
        "telegram_bot" => "Telegram bot",
        other => other,
    }
}

fn state(item: &Value) -> &str {
    match item["state"].as_str() {
        Some("off") => "off",
        Some("active") => "on",
        Some("changes_pending") => "applying",
        Some("failed") => "failed",
        Some("removing") => "removing",
        Some(other) => other,
        None => "",
    }
}

fn items(catalog: Option<&Value>) -> &[Value] {
    catalog
        .and_then(|value| value["items"].as_array())
        .map_or(&[], Vec::as_slice)
}

pub fn has(catalog: Option<&Value>, service: &str) -> bool {
    items(catalog).iter().any(|item| item["service"] == service)
}

/// The page: each kind with its services, their state and instances.
pub fn text(catalog: Option<&Value>) -> String {
    let mut text = String::from("Settings › Services");
    if catalog.is_none() {
        text.push_str("\n\nLoading services…");
        return text;
    }
    for (kind, title, hint) in KINDS {
        let rows: Vec<String> = items(catalog)
            .iter()
            .filter(|item| item["kind"] == kind)
            .map(|item| {
                let mut row = format!("  {}", name(item["service"].as_str().unwrap_or("")));
                let state = state(item);
                if !state.is_empty() {
                    row.push_str(&format!(" · {state}"));
                }
                if let Some(count) = item["instances"].as_u64() {
                    row.push_str(&format!(" · instances: {count}"));
                }
                row
            })
            .collect();
        if !rows.is_empty() {
            text.push_str(&format!("\n\n{title} — {hint}\n{}", rows.join("\n")));
        }
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn kinds_in_order_with_state_and_instances() {
        let catalog = json!({"items": [
            {"service": "vpn", "kind": "builtin", "state": null, "instances": 3},
            {"service": "relay", "kind": "system", "state": "changes_pending", "instances": 1},
            {"service": "telegram_bot", "kind": "system", "state": "off", "instances": null},
        ]});
        let page = text(Some(&catalog));
        assert!(page.find("System").unwrap() < page.find("Built-in").unwrap());
        assert!(page.contains("  Relay · applying · instances: 1"));
        assert!(page.contains("  Telegram bot · off\n"));
        assert!(page.ends_with("  VPN · instances: 3"));
        assert!(!page.contains("Custom"));
        assert!(has(Some(&catalog), "relay"));
        assert!(text(None).ends_with("Loading services…"));
    }
}
