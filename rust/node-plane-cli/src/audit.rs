//! Secret-free host journal envelopes for remote mutations.
use crate::config::shell_quote;

pub fn script(script: &str, attribution: &serde_json::Value) -> String {
    let mut admitted = attribution.clone();
    admitted["phase"] = serde_json::json!("admitted");
    let mut completed = attribution.clone();
    completed["phase"] = serde_json::json!("completed");
    let completed = completed.to_string();
    let completed_prefix = format!("{},\"exit_status\":", &completed[..completed.len() - 1]);
    // Do not put the actual script, stdin, token or output into the host journal.
    // The end event contains only the exit status. A missing end event is unknown.
    format!(
        "logger -t node-plane-workstation -- {} || exit 78\n/bin/bash -c {}\naudit_exit=$?\nlogger -t node-plane-workstation -- {}\"$audit_exit\"'}}' >&2\nexit \"$audit_exit\"",
        shell_quote(&admitted.to_string()),
        shell_quote(script),
        shell_quote(&completed_prefix)
    )
}
