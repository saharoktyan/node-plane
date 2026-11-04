//! AmneziaWG 3.1 profiles: the server key, the obfuscation parameters a
//! preset generates, their validation and the upgrade of older configs.
//! Replaces `awg_profile.py`; the controller checks manual parameters with
//! the same rules (`app/awg_profile.py`), and both run the cases in
//! `tests/fixtures/awg_profile_cases.json`.
//!
//! `node-plane-agent awg-profile genkey | init|migrate|regenerate|validate
//! PATH [PRESET] | apply PATH PARAMETERS_JSON` runs it by hand.
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use ring::rand::{SecureRandom, SystemRandom};
use serde_json::Value;

use crate::enrollment::der::{base64, unbase64};

pub(crate) const LEGACY_FIELDS: [&str; 16] = [
    "Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4",
    "I5",
];
pub(crate) const AWG3_FIELDS: [&str; 9] = [
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
const TIMING_FIELDS: [&str; 5] = [
    "RekeyAfterTime",
    "RekeyTimeout",
    "RejectAfterTime",
    "KeepaliveTimeout",
    "MaxHandshakeAttempts",
];
// The pinned engine uses 12-byte header nonces and a 1700-byte iOS receive
// buffer. Bound server padding for clients on every supported platform.
const PADDING_LIMITS: [(&str, u64); 4] = [
    ("S1", 1700 - 148),
    ("S2", 1700 - 92),
    ("S3", 1700 - 64),
    // NP policy: limit overhead on every data packet.
    ("S4", 32),
];
// Also fits signed client configuration editors.
const GENERATED_HEADER_MAX: u64 = (1 << 31) - 1;
const UPGRADED: &str = "AWG profile upgraded; existing client configs must be reimported";

/// Interface values in config order.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct Values(Vec<(String, String)>);

impl Values {
    pub(crate) fn get(&self, key: &str) -> &str {
        self.0
            .iter()
            .find(|(name, _)| name == key)
            .map_or("", |(_, value)| value.as_str())
    }

    fn set(&mut self, key: &str, value: String) {
        match self.0.iter_mut().find(|(name, _)| name == key) {
            Some(entry) => entry.1 = value,
            None => self.0.push((key.to_string(), value)),
        }
    }

    pub(crate) fn iter(&self) -> impl Iterator<Item = (&str, &str)> {
        self.0
            .iter()
            .map(|(key, value)| (key.as_str(), value.as_str()))
    }
}

impl<const N: usize> From<[(&str, &str); N]> for Values {
    fn from(pairs: [(&str, &str); N]) -> Self {
        let mut values = Values::default();
        for (key, value) in pairs {
            values.set(key, value.to_string());
        }
        values
    }
}

fn random_int(low: u64, high: u64) -> u64 {
    let span = high - low + 1;
    // Rejection sampling keeps every value equally likely.
    let limit = u64::MAX - u64::MAX % span;
    loop {
        let mut bytes = [0u8; 8];
        SystemRandom::new()
            .fill(&mut bytes)
            .expect("system random source");
        let value = u64::from_le_bytes(bytes);
        if value < limit {
            return low + value % span;
        }
    }
}

fn random_bytes<const N: usize>() -> [u8; N] {
    let mut bytes = [0u8; N];
    SystemRandom::new()
        .fill(&mut bytes)
        .expect("system random source");
    bytes
}

/// A clamped X25519 private key in WireGuard's base64 format.
pub(crate) fn new_private_key() -> String {
    let mut key = random_bytes::<32>();
    key[0] &= 248;
    key[31] = (key[31] & 127) | 64;
    base64(&key)
}

pub(crate) fn interface_values(config: &str) -> Values {
    let mut section = String::new();
    let mut values = Values::default();
    for raw in config.lines() {
        let line = raw.trim();
        if line.starts_with('[') && line.ends_with(']') {
            section = line.to_lowercase();
        } else if section == "[interface]" && line.contains('=') && !line.starts_with('#') {
            let (key, value) = line.split_once('=').expect("contains =");
            values.set(key.trim(), value.trim().to_string());
        }
    }
    values
}

pub(crate) fn protocol_version(values: &Values) -> Result<&'static str, String> {
    if !values.get("HeaderProtectionKey").is_empty() {
        let switch = |key: &str| matches!(values.get(key).to_lowercase().as_str(), "on" | "off");
        if !switch("RandomTrailers") || !switch("DisableCookies") {
            return Err("Incomplete AWG 3.1 profile".into());
        }
        return Ok("3.1");
    }
    if AWG3_FIELDS.iter().any(|key| !values.get(key).is_empty()) {
        return Err("AWG 3.x fields without HeaderProtectionKey".into());
    }
    Ok("2")
}

fn digits(value: &str) -> bool {
    !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_digit())
}

/// A decimal number; one too large for u64 is larger than every bound.
fn number(value: &str) -> u64 {
    value.parse().unwrap_or(u64::MAX)
}

fn range(value: &str, maximum: u64, minimum: u64) -> Result<(u64, u64), String> {
    let (low, high) = value.split_once('-').unwrap_or((value, value));
    if !digits(low) || !digits(high) {
        return Err(format!("Invalid numeric range: {value:?}"));
    }
    let (low, high) = (number(low), number(high));
    if low < minimum || low > high || high > maximum {
        return Err(format!("Out-of-range value: {value:?}"));
    }
    Ok((low, high))
}

fn single(value: &str, maximum: u64) -> Result<u64, String> {
    if !digits(value) || number(value) > maximum {
        return Err(format!("Invalid integer: {value:?}"));
    }
    Ok(number(value))
}

/// The tags of a CPS expression, `<b 0xHEX>`, `<r N>`, `<rc N>`, `<rd N>`
/// and `<t>`; None when it is not one.
fn cps_tags(expression: &str) -> Option<Vec<(&str, &str)>> {
    let mut tags = Vec::new();
    let mut rest = expression;
    while !rest.is_empty() {
        let inner = rest.strip_prefix('<')?;
        let end = inner.find('>')?;
        let tag = &inner[..end];
        rest = &inner[end + 1..];
        let parsed = match tag.split_once(' ') {
            None if tag == "t" => ("t", ""),
            Some(("b", hex)) => {
                let hex = hex.strip_prefix("0x")?;
                (!hex.is_empty() && hex.bytes().all(|b| b.is_ascii_hexdigit()))
                    .then_some(("b", hex))?
            }
            Some((name @ ("r" | "rc" | "rd"), length)) => {
                digits(length).then_some((name, length))?
            }
            _ => return None,
        };
        tags.push(parsed);
    }
    (!tags.is_empty()).then_some(tags)
}

/// Python's `b64decode(value, validate=True)`: the alphabet, padding only
/// at the end, and a length of whole groups.
fn strict_base64(value: &str) -> Option<Vec<u8>> {
    let body = value.trim_end_matches('=');
    let valid = value.len() % 4 == 0
        && value.len() - body.len() <= 2
        && body
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'+' || b == b'/');
    if !valid {
        return None;
    }
    unbase64(value)
}

pub(crate) fn validate(values: &Values, require_31: bool) -> Result<(), String> {
    let version = protocol_version(values)?;
    if require_31 && version != "3.1" {
        return Err("AWG 3.1 profile is required".into());
    }
    for key in LEGACY_FIELDS {
        if values.get(key).is_empty() {
            return Err(format!("Missing AWG field: {key}"));
        }
    }
    if single(values.get("Jmin"), 65535)? > single(values.get("Jmax"), 65535)? {
        return Err("Jmin exceeds Jmax".into());
    }
    single(values.get("Jc"), 65535)?;
    for (key, maximum) in PADDING_LIMITS {
        let size = single(values.get(key), maximum)?;
        if version == "3.1" && size < 12 {
            return Err(format!("Invalid {key} for header protection"));
        }
    }
    if number(values.get("S1")) + 56 == number(values.get("S2")) {
        return Err("S1 and S2 produce equal handshake packet sizes".into());
    }
    let mut ranges: Vec<(u64, u64)> = Vec::new();
    for key in ["H1", "H2", "H3", "H4"] {
        let (low, high) = range(values.get(key), 4294967295, 0)?;
        if ranges
            .iter()
            .any(|(other_low, other_high)| low <= *other_high && *other_low <= high)
        {
            return Err("Overlapping H1-H4 ranges".into());
        }
        ranges.push((low, high));
    }
    for key in ["I1", "I2", "I3", "I4", "I5"] {
        let tags = cps_tags(values.get(key)).ok_or(format!("Invalid {key} CPS expression"))?;
        if tags
            .iter()
            .any(|(tag, hex)| *tag == "b" && hex.len() % 2 == 1)
        {
            return Err(format!("{key} contains odd-length hex data"));
        }
        for (tag, length) in tags {
            if matches!(tag, "r" | "rc" | "rd") && number(length) > 1000 {
                return Err(format!("{key} {tag} length exceeds 1000"));
            }
        }
    }
    if version == "3.1" {
        let key = strict_base64(values.get("HeaderProtectionKey"))
            .ok_or("Invalid HeaderProtectionKey")?;
        if key.len() != 32 {
            return Err("HeaderProtectionKey must contain 32 bytes".into());
        }
        if !values.get("ContentPaddingAddition").is_empty() {
            range(values.get("ContentPaddingAddition"), 65535, 0)?;
        }
        for key in TIMING_FIELDS {
            if !values.get(key).is_empty() {
                range(values.get(key), 65535, 1)?;
            }
        }
        // Missing timing fields retain the pinned engine's 120s/180s defaults.
        let or = |key: &str, default: &'static str| match values.get(key) {
            "" => default.to_string(),
            value => value.to_string(),
        };
        let rekey_high = range(&or("RekeyAfterTime", "120"), 65535, 1)?.1;
        let reject_low = range(&or("RejectAfterTime", "180"), 65535, 1)?.0;
        if rekey_high >= reject_low {
            return Err("RekeyAfterTime must finish before RejectAfterTime".into());
        }
    }
    Ok(())
}

fn i_values(preset: &str) -> Result<[String; 5], String> {
    Ok(match preset {
        "dns" => [
            "<rc 2><b 0x01000001000000000000><r 32>".into(),
            "<rc 2><b 0x01000001000000000000><r 64>".into(),
            "<r 48>".into(),
            "<r 80>".into(),
            "<r 40>".into(),
        ],
        "chaos" => {
            let hex: String = random_bytes::<4>()
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect();
            let r = || format!("<r {}>", random_int(100, 900));
            [
                format!("<b 0x{hex}><rc 4><r {}>", random_int(500, 900)),
                r(),
                r(),
                r(),
                r(),
            ]
        }
        "quic" => [
            "<b 0xc000000001><rc 8><r 900>".into(),
            "<b 0x40><rc 4><r 100>".into(),
            "<r 900>".into(),
            "<r 100>".into(),
            "<r 900>".into(),
        ],
        _ => return Err(format!("Unknown I1 preset: {preset}")),
    })
}

pub(crate) fn new_profile(preset: &str) -> Result<Values, String> {
    let s1 = random_int(16, 128);
    let mut s2 = random_int(16, 128);
    while s1 + 56 == s2 {
        s2 = random_int(16, 128);
    }
    let jmin = random_int(40, 100);
    // Separate bands guarantee distinct single-value headers without retries.
    // Avoid wide H ranges with RandomTrailers: padded data can be mistaken for
    // handshake packets by the engine's receive classifier.
    let band = (GENERATED_HEADER_MAX - 4) / 4;
    let padding_low = random_int(8, 24);
    let (rekey_low, rekey_high) = (random_int(100, 115), random_int(125, 145));
    let pair =
        |a: (u64, u64), b: (u64, u64)| format!("{}-{}", random_int(a.0, a.1), random_int(b.0, b.1));
    let mut values = Values::default();
    values.set("Jc", random_int(3, 7).to_string());
    values.set("Jmin", jmin.to_string());
    values.set("Jmax", random_int(jmin + 40, 250).to_string());
    values.set("S1", s1.to_string());
    values.set("S2", s2.to_string());
    values.set("S3", random_int(12, 48).to_string());
    values.set("S4", random_int(12, 32).to_string());
    for i in 0..4 {
        values.set(
            &format!("H{}", i + 1),
            random_int(5 + i * band, 4 + (i + 1) * band).to_string(),
        );
    }
    for (index, value) in i_values(preset)?.into_iter().enumerate() {
        values.set(&format!("I{}", index + 1), value);
    }
    values.set("HeaderProtectionKey", base64(&random_bytes::<32>()));
    values.set(
        "ContentPaddingAddition",
        format!("{padding_low}-{}", random_int(padding_low + 8, 64)),
    );
    values.set("RekeyAfterTime", format!("{rekey_low}-{rekey_high}"));
    values.set("RejectAfterTime", pair((180, 200), (220, 260)));
    values.set("RekeyTimeout", pair((3, 5), (6, 8)));
    values.set("KeepaliveTimeout", pair((8, 10), (12, 18)));
    values.set("MaxHandshakeAttempts", pair((16, 20), (24, 32)));
    values.set("RandomTrailers", "on".into());
    values.set("DisableCookies", "off".into());
    validate(&values, true)?;
    Ok(values)
}

/// `config` with its interface holding `values`: each replaces its line,
/// an empty one removes it, and the others are appended to the interface.
pub(crate) fn replace_interface_values(config: &str, values: &Values) -> String {
    let mut output: Vec<String> = Vec::new();
    let mut section = String::new();
    let mut seen: Vec<&str> = Vec::new();
    let append = |output: &mut Vec<String>, seen: &[&str]| {
        output.extend(
            values
                .iter()
                .filter(|(key, value)| !seen.contains(key) && !value.is_empty())
                .map(|(key, value)| format!("{key} = {value}")),
        );
    };
    for line in config.lines() {
        let stripped = line.trim();
        if stripped.starts_with('[') && stripped.ends_with(']') {
            if section == "[interface]" {
                append(&mut output, &seen);
            }
            section = stripped.to_lowercase();
        }
        if section == "[interface]" && stripped.contains('=') && !stripped.starts_with('#') {
            let key = stripped.split_once('=').expect("contains =").0.trim();
            if let Some((name, value)) = values.iter().find(|(name, _)| *name == key) {
                if !seen.contains(&name) && !value.is_empty() {
                    output.push(format!("{name} = {value}"));
                }
                seen.push(name);
                continue;
            }
        }
        output.push(line.to_string());
    }
    if section == "[interface]" {
        append(&mut output, &seen);
    }
    format!("{}\n", output.join("\n").trim_end())
}

/// `<r N>`, `<rc N>` and `<rd N>` of `expression` with N at most 1000.
fn bound_lengths(expression: &str) -> String {
    let mut output = String::new();
    let mut rest = expression;
    while let Some(start) = rest.find('<') {
        output.push_str(&rest[..start]);
        let candidate = &rest[start + 1..];
        let bounded = candidate.find('>').and_then(|end| {
            let (name, length) = candidate[..end].split_once(' ')?;
            (matches!(name, "r" | "rc" | "rd") && digits(length))
                .then(|| (format!("<{name} {}>", number(length).min(1000)), end))
        });
        match bounded {
            Some((tag, end)) => {
                output.push_str(&tag);
                rest = &candidate[end + 1..];
            }
            None => {
                output.push('<');
                rest = candidate;
            }
        }
    }
    output.push_str(rest);
    output
}

/// An AWG 3.1 config unchanged; an older one with new parameters, keeping
/// its CPS patterns where valid.
pub(crate) fn migrate(config: &str, preset: &str) -> Result<String, String> {
    let current = interface_values(config);
    if protocol_version(&current)? == "3.1" {
        validate(&current, true)?;
        return Ok(config.to_string());
    }
    let mut profile = new_profile(preset)?;
    for key in ["I1", "I2", "I3", "I4", "I5"] {
        if !current.get(key).is_empty() {
            profile.set(key, bound_lengths(current.get(key)));
        }
    }
    validate(&profile, true)?;
    Ok(replace_interface_values(config, &profile))
}

/// Manual parameters from a JSON object, checked as the controller does.
pub(crate) fn manual(parameters: &Value) -> Result<Values, String> {
    let invalid = || "invalid manual AWG parameters".to_string();
    let object = parameters.as_object().ok_or_else(invalid)?;
    let mut values = Values::default();
    for (key, value) in object {
        let known = LEGACY_FIELDS.contains(&key.as_str()) || AWG3_FIELDS.contains(&key.as_str());
        let text = value.as_str().ok_or_else(invalid)?;
        if !known
            || text.is_empty()
            || text.len() > 4096
            || text.bytes().any(|byte| !(32..=126).contains(&byte))
        {
            return Err(invalid());
        }
        values.set(key, text.to_string());
    }
    validate(&values, true)?;
    Ok(values)
}

/// Replaces `path` with `updated` unless equal, keeping the first original as
/// `.pre-awg31`; the report of a change.
fn rewrite(path: &Path, original: &str, updated: &str) -> Result<Option<&'static str>, String> {
    if updated == original {
        return Ok(None);
    }
    let failed = |err: std::io::Error| format!("{}: {err}", path.display());
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let backup = path.with_file_name(format!("{name}.pre-awg31"));
    if !backup.exists() {
        fs::write(&backup, original).map_err(failed)?;
        fs::set_permissions(&backup, fs::Permissions::from_mode(0o600)).map_err(failed)?;
    }
    let temporary = path.with_file_name(format!("{name}.new"));
    fs::write(&temporary, updated).map_err(failed)?;
    fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600)).map_err(failed)?;
    fs::rename(&temporary, path).map_err(failed)?;
    Ok(Some(UPGRADED))
}

fn read(path: &Path) -> Result<String, String> {
    fs::read_to_string(path).map_err(|err| format!("{}: {err}", path.display()))
}

/// The interface lines of a new profile.
pub(crate) fn init_lines(preset: &str) -> Result<String, String> {
    Ok(new_profile(preset)?
        .iter()
        .map(|(key, value)| format!("{key} = {value}\n"))
        .collect())
}

pub(crate) fn validate_file(path: &Path) -> Result<(), String> {
    validate(&interface_values(&read(path)?), true)
}

/// Upgrades an older config in place; the report of a change.
pub(crate) fn migrate_file(path: &Path, preset: &str) -> Result<Option<&'static str>, String> {
    let original = read(path)?;
    rewrite(path, &original, &migrate(&original, preset)?)
}

/// New parameters for `preset` in place.
pub(crate) fn regenerate_file(path: &Path, preset: &str) -> Result<Option<&'static str>, String> {
    let original = read(path)?;
    let updated = replace_interface_values(&original, &new_profile(preset)?);
    rewrite(path, &original, &updated)
}

/// The manual parameters of `parameters` (a JSON file) in place; fields
/// they omit are removed.
pub(crate) fn apply_file(path: &Path, parameters: &Path) -> Result<Option<&'static str>, String> {
    let original = read(path)?;
    let raw: Value = serde_json::from_str(&read(parameters)?).map_err(|err| err.to_string())?;
    let values = manual(&raw)?;
    let mut all = Values::default();
    for key in LEGACY_FIELDS.iter().chain(&AWG3_FIELDS) {
        all.set(key, values.get(key).to_string());
    }
    rewrite(path, &original, &replace_interface_values(&original, &all))
}

/// Runs the subcommand when the agent was started as `awg-profile`.
pub(crate) fn run(args: &[String]) -> Option<i32> {
    if args.first().map(String::as_str) != Some("awg-profile") {
        return None;
    }
    let args: Vec<&str> = args[1..].iter().map(String::as_str).collect();
    let result = match args.as_slice() {
        ["genkey"] => Ok(Some(format!("{}\n", new_private_key()))),
        ["init", _path, rest @ ..] if rest.len() <= 1 => {
            init_lines(rest.first().copied().unwrap_or("quic")).map(Some)
        }
        ["validate", path, ..] => validate_file(Path::new(path)).map(|()| None),
        ["migrate", path, rest @ ..] if rest.len() <= 1 => {
            migrate_file(Path::new(path), rest.first().copied().unwrap_or("quic"))
                .map(|report| report.map(|text| format!("{text}\n")))
        }
        ["regenerate", path, rest @ ..] if rest.len() <= 1 => {
            regenerate_file(Path::new(path), rest.first().copied().unwrap_or("quic"))
                .map(|report| report.map(|text| format!("{text}\n")))
        }
        ["apply", path, parameters] => apply_file(Path::new(path), Path::new(parameters))
            .map(|report| report.map(|text| format!("{text}\n"))),
        _ => {
            eprintln!(
                "usage: node-plane-agent awg-profile genkey | init|migrate|regenerate|validate PATH [PRESET] | apply PATH PARAMETERS_JSON"
            );
            return Some(2);
        }
    };
    match result {
        Ok(output) => {
            print!("{}", output.unwrap_or_default());
            Some(0)
        }
        Err(err) => {
            eprintln!("{err}");
            Some(1)
        }
    }
}

#[cfg(test)]
mod tests;
