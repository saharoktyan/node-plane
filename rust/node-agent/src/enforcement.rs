//! One loop for the enforcement modules, every `POLL`.
//!
//! Order matters: Xray rate rules must follow eviction rules, so shaping
//! runs right after admission and fixes the order in the same pass.
use std::time::Duration;

use crate::shaping::Host;
use crate::state::AgentState;

const POLL: Duration = Duration::from_secs(5);

/// Logs a failure once, not every poll.
fn report(name: &str, last: &mut Option<String>, error: Option<String>) {
    match error {
        Some(error) if last.as_ref() != Some(&error) => {
            eprintln!("{name} step failed: {error}");
            *last = Some(error);
        }
        Some(_) => {}
        None => *last = None,
    }
}

pub(crate) async fn run(state: AgentState) {
    let mut ticker = tokio::time::interval(POLL);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let (mut admission_error, mut shaping_error) = (None, None);
    loop {
        ticker.tick().await;
        // node.env may change between passes (deploys, settings).
        let host = Host::of(&state);
        match state.admission.tick(&host.xray_api).await {
            Ok(events) => {
                for event in events {
                    println!("admission {event:?}");
                }
                report("admission", &mut admission_error, None);
            }
            Err(err) => report("admission", &mut admission_error, Some(err)),
        }
        let error = match state.shaping.tick(&host).await {
            Ok(()) => Some(state.shaping.failures().join("; ")).filter(|text| !text.is_empty()),
            Err(err) => Some(err),
        };
        report("shaping", &mut shaping_error, error);
    }
}
