//! AWG client configs and their AmneziaVPN keys (`vpn://`). Replaces
//! `conf2vpn.py` and `amnezia-config-decoder.py`.
//!
//! A key is the AmneziaVPN connection JSON, zlib-compressed behind its
//! 4-byte big-endian length, in unpadded URL-safe base64.
use serde_json::{Value, json};

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

pub(crate) fn all_fields() -> impl Iterator<Item = &'static str> {
    LEGACY_FIELDS.into_iter().chain(AWG3_FIELDS)
}

const TEMPLATE: &str = include_str!("amnezia-template.json");

/// One section's `key = value` lines, in order.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct Section(Vec<(String, String)>);

impl Section {
    pub(crate) fn get(&self, key: &str) -> Option<&str> {
        self.0
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value.as_str())
    }

    /// The value, unless missing or empty.
    pub(crate) fn value(&self, key: &str) -> Option<&str> {
        self.get(key).filter(|value| !value.is_empty())
    }

    /// Replaces the value in place, or appends it.
    pub(crate) fn set(&mut self, key: &str, value: &str) {
        match self.0.iter_mut().find(|(name, _)| name == key) {
            Some(entry) => entry.1 = value.to_string(),
            None => self.0.push((key.to_string(), value.to_string())),
        }
    }

    pub(crate) fn remove(&mut self, key: &str) {
        self.0.retain(|(name, _)| name != key);
    }

    pub(crate) fn lines(&self) -> impl Iterator<Item = String> + '_ {
        self.0.iter().map(|(key, value)| format!("{key} = {value}"))
    }
}

/// The `[Interface]` and `[Peer]` sections of a config; comments, blank
/// lines and other sections are skipped.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct Conf {
    pub(crate) interface: Section,
    pub(crate) peer: Section,
}

pub(crate) fn parse(text: &str) -> Conf {
    let mut conf = Conf::default();
    let mut current: Option<bool> = None;
    for line in text.lines().map(str::trim) {
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            current = match line[1..line.len() - 1].to_ascii_lowercase().as_str() {
                "interface" => Some(true),
                "peer" => Some(false),
                _ => None,
            };
            continue;
        }
        let (Some(interface), Some((key, value))) = (current, line.split_once('=')) else {
            continue;
        };
        let section = if interface {
            &mut conf.interface
        } else {
            &mut conf.peer
        };
        section.set(key.trim(), value.trim());
    }
    conf
}

/// `3.1` with header protection, `2` without any AWG 3 field.
pub(crate) fn protocol_version(interface: &Section) -> Result<&'static str, String> {
    if interface.value("HeaderProtectionKey").is_some() {
        let switch = |key| {
            matches!(
                interface
                    .get(key)
                    .unwrap_or("")
                    .to_ascii_lowercase()
                    .as_str(),
                "on" | "off"
            )
        };
        if !switch("RandomTrailers") || !switch("DisableCookies") {
            return Err("Incomplete AWG 3.1 profile".into());
        }
        return Ok("3.1");
    }
    if AWG3_FIELDS.iter().any(|key| interface.value(key).is_some()) {
        return Err("AWG 3.x fields without HeaderProtectionKey".into());
    }
    Ok("2")
}

fn csv(value: &str) -> Vec<String> {
    value
        .split(',')
        .map(str::trim)
        .filter(|part| !part.is_empty())
        .map(str::to_string)
        .collect()
}

/// The AmneziaVPN connection for a client config. `container` names the
/// node's container for AWG 2 clients; `description` is the shown name.
fn connection(text: &str, container: &str, description: &str) -> Result<Value, String> {
    let text = format!("{}\n", text.trim());
    let conf = parse(&text);
    let (interface, peer) = (&conf.interface, &conf.peer);
    let version = protocol_version(interface)?;
    let get = |key: &str| interface.get(key).unwrap_or("").to_string();
    let address = interface.get("Address").unwrap_or("10.8.1.0/24");
    let first_address = address.split(',').next().unwrap_or("").trim();
    let client_ip = first_address.split('/').next().unwrap_or("").to_string();
    let endpoint = peer.get("Endpoint").unwrap_or("");
    let (host, port) = endpoint
        .rsplit_once(':')
        .ok_or("AWG client config has no endpoint")?;
    let port: u16 = port.parse().map_err(|_| "invalid AWG endpoint port")?;
    let mut allowed = csv(peer.get("AllowedIPs").unwrap_or(""));
    if allowed.is_empty() {
        allowed = vec!["0.0.0.0/0".into(), "::/0".into()];
    }
    let dns = csv(interface.get("DNS").unwrap_or(""));
    let subnet = address.split('/').next().unwrap_or("");
    let subnet = format!(
        "{}.0",
        subnet
            .rsplit_once('.')
            .map_or(subnet, |(network, _)| network)
    );

    let mut last = json!({
        "allowed_ips": allowed,
        "clientId": get("PublicKey"),
        "client_ip": client_ip,
        "client_priv_key": get("PrivateKey"),
        "client_pub_key": get("PublicKey"),
        "config": text,
        "hostName": host,
        "mtu": interface.get("MTU").unwrap_or("1280"),
        "persistent_keep_alive": peer.get("PersistentKeepalive").unwrap_or("25"),
        "port": port,
        "psk_key": peer.get("PresharedKey").unwrap_or(""),
        "server_pub_key": peer.get("PublicKey").unwrap_or(""),
    });
    for key in LEGACY_FIELDS {
        last[key] = get(key).into();
    }
    let mut out: Value = serde_json::from_str(TEMPLATE).map_err(|err| err.to_string())?;
    // AmneziaVPN recognizes modern AWG by its protocol container marker,
    // not by the node's Docker container name.
    let marker = if version == "3.1" {
        "amnezia-awg2"
    } else {
        container
    };
    out["hostName"] = host.into();
    out["description"] = description.into();
    out["dns1"] = dns.first().map_or("1.1.1.1", String::as_str).into();
    out["dns2"] = dns.get(1).map_or("1.0.0.1", String::as_str).into();
    out["defaultContainer"] = marker.into();
    let entry = &mut out["containers"][0];
    entry["container"] = marker.into();
    let awg = entry["awg"]
        .as_object_mut()
        .ok_or("invalid AmneziaVPN template")?;
    awg.insert("port".into(), port.to_string().into());
    awg.insert("transport_proto".into(), "udp".into());
    awg.insert("protocol_version".into(), version.into());
    awg.insert("subnet_address".into(), subnet.into());
    for key in all_fields() {
        match interface.value(key) {
            Some(value) => {
                awg.insert(key.into(), value.into());
                last[key] = value.into();
            }
            None => {
                awg.remove(key);
            }
        }
    }
    let last = serde_json::to_string_pretty(&last).map_err(|err| err.to_string())?;
    awg.insert("last_config".into(), last.into());
    Ok(out)
}

fn base64_url(data: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut text = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let bytes = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let group = u32::from(bytes[0]) << 16 | u32::from(bytes[1]) << 8 | u32::from(bytes[2]);
        for index in 0..=chunk.len() {
            text.push(char::from(
                ALPHABET[(group >> (18 - 6 * index) & 63) as usize],
            ));
        }
    }
    text
}

fn encode(connection: &Value) -> Result<String, String> {
    let json = serde_json::to_vec(connection).map_err(|err| err.to_string())?;
    let length = u32::try_from(json.len()).map_err(|_| "AmneziaVPN config too large")?;
    let mut data = length.to_be_bytes().to_vec();
    data.extend(miniz_oxide::deflate::compress_to_vec_zlib(&json, 9));
    Ok(format!("vpn://{}", base64_url(&data)))
}

/// The AmneziaVPN key for a client config.
pub(crate) fn vpn_key(text: &str, container: &str, description: &str) -> Result<String, String> {
    encode(&connection(text, container, description)?)
}

#[cfg(test)]
mod tests {
    use super::*;

    const CLIENT: &str = "[Interface]\nPrivateKey = private\nPublicKey = public\nAddress = 10.8.1.2/32, fd42:6e70:6177:2::a08:102/128\nDNS = 1.1.1.1\nMTU = 1280\n\nJc = 5\nJmin = 40\nJmax = 146\nS1 = 31\nS2 = 68\nS3 = 44\nS4 = 25\nH1 = 1\nH2 = 2\nH3 = 3\nH4 = 4\nI1 = <b 0xc000000001><rc 8><r 900>\nI2 = <r 1>\nI3 = <r 1>\nI4 = <r 1>\nI5 = <r 1>\nHeaderProtectionKey = 5h02EEnpOPPpN8RVIWwRS0gtOUprabtR2Om1nSsmvC0=\nRandomTrailers = on\nDisableCookies = off\nRekeyAfterTime = 102-130\n\n[Peer]\nPublicKey = serverpublic\nPresharedKey = preshared\nEndpoint = 203.0.113.10:51820\nAllowedIPs = 0.0.0.0/0, ::/0\nPersistentKeepalive = 25\n";

    fn decode(key: &str) -> Value {
        let text = key.strip_prefix("vpn://").unwrap();
        let alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
        let mut bits = Vec::new();
        for c in text.chars() {
            let value = alphabet.find(c).unwrap() as u8;
            bits.extend((0..6).rev().map(|shift| (value >> shift) & 1));
        }
        let bytes: Vec<u8> = bits
            .chunks(8)
            .filter(|chunk| chunk.len() == 8)
            .map(|chunk| chunk.iter().fold(0, |byte, bit| byte << 1 | bit))
            .collect();
        let length = u32::from_be_bytes(bytes[..4].try_into().unwrap()) as usize;
        let json = miniz_oxide::inflate::decompress_to_vec_zlib(&bytes[4..]).unwrap();
        assert_eq!(json.len(), length);
        serde_json::from_slice(&json).unwrap()
    }

    #[test]
    fn modern_awg_keys_carry_the_protocol_marker_and_all_parameters() {
        let connection = decode(&vpn_key(CLIENT, "amnezia-awg", "msk1 · alice").unwrap());
        assert_eq!(connection["description"], "msk1 · alice");
        assert_eq!(connection["hostName"], "203.0.113.10");
        assert_eq!(connection["defaultContainer"], "amnezia-awg2");
        let awg = &connection["containers"][0]["awg"];
        assert_eq!(awg["port"], "51820");
        assert_eq!(awg["protocol_version"], "3.1");
        assert_eq!(awg["subnet_address"], "10.8.1.0");
        assert_eq!(awg["RekeyAfterTime"], "102-130");
        assert!(
            awg.get("RekeyTimeout").is_none(),
            "unset fields are dropped"
        );
        let last: Value = serde_json::from_str(awg["last_config"].as_str().unwrap()).unwrap();
        assert_eq!(last["allowed_ips"], json!(["0.0.0.0/0", "::/0"]));
        assert_eq!(last["client_ip"], "10.8.1.2");
        assert_eq!(last["port"], 51820);
        assert_eq!(last["psk_key"], "preshared");
        assert!(
            last["config"]
                .as_str()
                .unwrap()
                .contains("fd42:6e70:6177:2::a08:102/128")
        );
    }

    #[test]
    fn legacy_profiles_name_the_node_container() {
        let legacy: String = CLIENT
            .lines()
            .filter(|line| {
                !AWG3_FIELDS
                    .iter()
                    .any(|key| line.starts_with(&format!("{key} =")))
            })
            .map(|line| format!("{line}\n"))
            .collect();
        let connection = decode(&vpn_key(&legacy, "amnezia-awg", "AmneziaWG").unwrap());
        assert_eq!(connection["defaultContainer"], "amnezia-awg");
        assert_eq!(connection["containers"][0]["awg"]["protocol_version"], "2");
        let partial = CLIENT.replace("RandomTrailers = on\n", "");
        assert!(vpn_key(&partial, "amnezia-awg", "x").is_err());
    }

    #[test]
    fn keys_are_unpadded_url_safe_base64() {
        assert_eq!(base64_url(b"\xfb\xff"), "-_8");
        assert_eq!(base64_url(b"abc"), "YWJj");
        assert_eq!(base64_url(b"ab"), "YWI");
    }
}
