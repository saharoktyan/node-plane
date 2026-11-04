//! What revoking a VLESS user does to its established connections, as
//! temporary access receipts report it (VPN plan, 11q).
//!
//! Removal always stops new connections. Revocations remove users with
//! `close_connections`, which the Node Plane Xray image honours and upstream
//! Xray ignores, so established connections close only on the former. A
//! receipt says `connections_closed` only when the running Xray implements
//! the close call on every VLESS inbound; anything else, including an
//! unreachable API, keeps the conservative `new_connections_only`.
use super::api::{XrayApi, XrayApiError};

pub(crate) const NEW_CONNECTIONS_ONLY: &str = "new_connections_only";
pub(crate) const CONNECTIONS_CLOSED: &str = "connections_closed";

/// Not a valid profile name, so the probe never closes a real connection.
const PROBE_EMAIL: &str = "node-plane:revocation-probe";

fn mode(results: &[Result<u64, XrayApiError>]) -> &'static str {
    if !results.is_empty() && results.iter().all(Result::is_ok) {
        CONNECTIONS_CLOSED
    } else {
        NEW_CONNECTIONS_ONLY
    }
}

/// The revocation mode of the Xray behind `address`.
pub(crate) async fn current_mode(address: &str) -> &'static str {
    let Ok(api) = XrayApi::connect(address).await else {
        return NEW_CONNECTIONS_ONLY;
    };
    let Ok(tags) = api.vless_inbound_tags().await else {
        return NEW_CONNECTIONS_ONLY;
    };
    let mut results = Vec::with_capacity(tags.len());
    for tag in &tags {
        results.push(api.close_user_connections(tag, PROBE_EMAIL, None).await);
    }
    mode(&results)
}

/// Replaces the script's conservative mode in a VLESS temporary access
/// result. `temporary_ensure` receipts promise the mode of a later
/// revocation; a fresh `temporary_revoke` reports the one that just ran.
pub(crate) async fn refine(
    action: &str,
    recover: bool,
    intent_json: &str,
    result: &mut serde_json::Value,
) {
    let reports = match action {
        "temporary_ensure" => true,
        // A recovered revocation may predate the running image.
        "temporary_revoke" => !recover,
        _ => false,
    };
    let xray = serde_json::from_str::<serde_json::Value>(intent_json)
        .is_ok_and(|intent| intent["protocol"] == "xray");
    if !reports || !xray || result["revocation_mode"] != NEW_CONNECTIONS_ONLY {
        return;
    }
    let address =
        std::env::var("NODE_PLANE_XRAY_API").unwrap_or_else(|_| super::api::DEFAULT_ADDRESS.into());
    result["revocation_mode"] = current_mode(&address).await.into();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_a_runtime_closing_on_every_inbound_reports_closed_connections() {
        assert_eq!(mode(&[Ok(0), Ok(0)]), CONNECTIONS_CLOSED);
        assert_eq!(
            mode(&[Ok(0), Err(XrayApiError::Unsupported("upstream".into()))]),
            NEW_CONNECTIONS_ONLY
        );
        assert_eq!(
            mode(&[Err(XrayApiError::Rejected("not found".into()))]),
            NEW_CONNECTIONS_ONLY
        );
        assert_eq!(mode(&[]), NEW_CONNECTIONS_ONLY);
        assert!(!super::super::api::cli::valid_email(PROBE_EMAIL));
    }

    #[tokio::test]
    async fn results_are_refined_only_for_vless_and_fresh_revocations() {
        let xray = r#"{"protocol":"xray"}"#;
        let awg = r#"{"protocol":"awg"}"#;
        let peer = serde_json::json!({"revocation_mode": "peer_removed"});
        let mut result = peer.clone();
        refine("temporary_revoke", false, awg, &mut result).await;
        assert_eq!(result, peer);
        let conservative = serde_json::json!({"revocation_mode": NEW_CONNECTIONS_ONLY});
        for (action, recover) in [("temporary_revoke", true), ("temporary_status", true)] {
            let mut result = conservative.clone();
            refine(action, recover, xray, &mut result).await;
            assert_eq!(result, conservative, "{action}");
        }
        assert_eq!(current_mode("127.0.0.1:1").await, NEW_CONNECTIONS_ONLY);
    }

    /// Against a real Xray with HandlerService and a VLESS inbound:
    /// `NP_TEST_XRAY_API=127.0.0.1:10085 NP_TEST_REVOCATION_MODE=connections_closed
    /// cargo test -- --ignored revocation` (`new_connections_only` for upstream).
    #[tokio::test]
    #[ignore = "needs a running Xray"]
    async fn the_probe_tells_the_node_plane_image_from_upstream() {
        let address = std::env::var("NP_TEST_XRAY_API").expect("NP_TEST_XRAY_API");
        let expected = std::env::var("NP_TEST_REVOCATION_MODE").expect("NP_TEST_REVOCATION_MODE");
        assert_eq!(current_mode(&address).await, expected);
    }
}
