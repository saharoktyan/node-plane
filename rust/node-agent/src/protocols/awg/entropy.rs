//! The AWG obfuscation parameters of the server config, as the controller
//! shows them. Replaces `show-awg-entropy.sh`, which only starts
//! `node-plane-agent awg-entropy` for `regenerate-awg-entropy.sh`.
use std::fs;
use std::path::Path;

use tonic::Status;

use crate::agent::v1::RuntimeCommandResponse;
use crate::protocols::{Setting, process_setting};
use crate::state::AgentState;

const KEYS: [&str; 25] = [
    "Jc",
    "Jmin",
    "Jmax",
    "S1",
    "S2",
    "S3",
    "S4",
    "H1",
    "H2",
    "H3",
    "H4",
    "I1",
    "I2",
    "I3",
    "I4",
    "I5",
    "HeaderProtectionKey",
    "ContentPaddingAddition",
    "RekeyAfterTime",
    "RekeyTimeout",
    "RejectAfterTime",
    "KeepaliveTimeout",
    "MaxHandshakeAttempts",
    "RandomTrailers",
    "DisableCookies",
];

/// `preset: …` then `Key: value` per parameter; `—` when unset, and only
/// `[set]` for the header protection key.
fn report(config: &str, preset: &str) -> String {
    let mut values = [""; KEYS.len()];
    for line in config.lines() {
        let Some((key, value)) = line.trim().split_once(" = ") else {
            continue;
        };
        if let Some(index) = KEYS.iter().position(|known| *known == key) {
            values[index] = value.trim();
        }
    }
    let mut lines = vec![format!("preset: {preset}")];
    for (key, value) in KEYS.iter().zip(values) {
        let shown = match value {
            "" => "—",
            _ if *key == "HeaderProtectionKey" => "[set]",
            value => value,
        };
        lines.push(format!("{key}: {shown}"));
    }
    lines.join("\n")
}

fn read(setting: Setting<'_>) -> Result<String, String> {
    let config = setting(
        "AWG_CONFIG",
        "/opt/node-plane-runtime/amnezia-awg/data/wg0.conf",
    );
    if !Path::new(&config).is_file() {
        return Err(format!("AWG config not found: {config}"));
    }
    let bytes = fs::read(&config).map_err(|err| err.to_string())?;
    Ok(report(
        &String::from_utf8_lossy(&bytes),
        &setting("AWG_I1_PRESET", "quic"),
    ))
}

impl AgentState {
    pub(crate) fn get_awg_entropy(&self) -> Result<RuntimeCommandResponse, Status> {
        let setting = |key: &str, default: &str| self.node_env_value(key, default);
        let summary = read(&setting).map_err(|err| {
            Status::failed_precondition(format!("show-awg-entropy.sh failed: {err}"))
        })?;
        Ok(RuntimeCommandResponse {
            summary,
            payload_json: String::new(),
        })
    }
}

/// Runs the subcommand when the agent was started as `awg-entropy`.
pub(crate) fn run(args: &[String]) -> Option<i32> {
    if args.first().map(String::as_str) != Some("awg-entropy") {
        return None;
    }
    Some(match read(&process_setting) {
        Ok(text) => {
            println!("{text}");
            0
        }
        Err(err) => {
            eprintln!("{err}");
            1
        }
    })
}

#[cfg(test)]
mod tests {
    #[test]
    fn the_report_hides_the_header_protection_key_and_marks_unset_values() {
        let config = "[Interface]\nPrivateKey = secret\nJc = 4\nH1 = 10-20\nHeaderProtectionKey = hidden\nI1 = <r 80>\nS1=7\n";
        let report = super::report(config, "dns");
        let lines: Vec<&str> = report.lines().collect();
        assert_eq!(lines[0], "preset: dns");
        assert_eq!(lines[1], "Jc: 4");
        assert!(lines.contains(&"H1: 10-20"));
        assert!(lines.contains(&"I1: <r 80>"));
        assert!(lines.contains(&"HeaderProtectionKey: [set]"));
        // Only `key = value` with spaces counts, as before.
        assert!(lines.contains(&"S1: —"));
        assert!(!report.contains("secret") && !report.contains("hidden"));
        assert_eq!(lines.len(), 26);
    }
}
