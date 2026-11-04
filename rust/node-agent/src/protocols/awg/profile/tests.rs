use super::*;

/// Cases the controller's `app/awg_profile.py` decides the same way.
const CASES: &str = include_str!("../../../../../../tests/fixtures/awg_profile_cases.json");

fn values(object: &Value) -> Values {
    let mut values = Values::default();
    for (key, value) in object.as_object().unwrap() {
        values.set(key, value.as_str().unwrap().to_string());
    }
    values
}

fn pairs(list: &Value) -> Values {
    let mut values = Values::default();
    for pair in list.as_array().unwrap() {
        values.set(pair[0].as_str().unwrap(), pair[1].as_str().unwrap().into());
    }
    values
}

fn cases() -> Value {
    serde_json::from_str(CASES).unwrap()
}

#[test]
fn validation_matches_the_controller() {
    for case in cases()["validate"].as_array().unwrap() {
        let values = values(&case["values"]);
        assert_eq!(
            validate(&values, true).is_ok(),
            case["valid"].as_bool().unwrap(),
            "{:?}: {:?}",
            case["values"],
            validate(&values, true)
        );
        assert_eq!(
            validate(&values, false).is_ok(),
            case["valid_any"].as_bool().unwrap(),
            "{:?}",
            case["values"]
        );
    }
}

#[test]
fn interface_lines_are_replaced_as_the_controller_does() {
    for case in cases()["replace"].as_array().unwrap() {
        let config = case["config"].as_str().unwrap();
        assert_eq!(
            replace_interface_values(config, &pairs(&case["values"])),
            case["expected"].as_str().unwrap(),
            "{config:?}"
        );
        assert_eq!(interface_values(config), pairs(&case["interface"]));
    }
}

#[test]
fn cps_lengths_are_bounded_as_the_controller_does() {
    for case in cases()["bound_lengths"].as_array().unwrap() {
        assert_eq!(
            bound_lengths(case["expression"].as_str().unwrap()),
            case["bounded"].as_str().unwrap()
        );
    }
}

#[test]
fn generated_profiles_are_valid_and_differ() {
    for preset in ["quic", "dns", "chaos"] {
        let first = new_profile(preset).unwrap();
        let second = new_profile(preset).unwrap();
        assert_ne!(
            first.get("HeaderProtectionKey"),
            second.get("HeaderProtectionKey")
        );
        let keys: Vec<&str> = first.iter().map(|(key, _)| key).collect();
        assert_eq!(keys[..7], ["Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4"]);
        assert_eq!(keys.len(), LEGACY_FIELDS.len() + AWG3_FIELDS.len());
        assert!((3..=7).contains(&number(first.get("Jc"))));
    }
    assert_eq!(new_profile("dns").unwrap().get("I3"), "<r 48>");
    assert!(new_profile("other").is_err());
}

#[test]
fn private_keys_are_clamped() {
    let key = unbase64(&new_private_key()).unwrap();
    assert_eq!(key.len(), 32);
    assert_eq!(key[0] & 7, 0);
    assert_eq!(key[31] & 0xc0, 0x40);
}

fn temp(name: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("np-awg-profile-{name}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn an_older_config_is_upgraded_once_with_a_backup() {
    let dir = temp("migrate");
    let path = dir.join("wg0.conf");
    let original =
        "[Interface]\nPrivateKey = k\nJc = 3\nI1 = <r 5000><b 0x01>\n\n[Peer]\nPublicKey = p\n";
    fs::write(&path, original).unwrap();
    assert_eq!(migrate_file(&path, "quic").unwrap(), Some(UPGRADED));
    let upgraded = fs::read_to_string(&path).unwrap();
    let values = interface_values(&upgraded);
    validate(&values, true).unwrap();
    assert_eq!(values.get("I1"), "<r 1000><b 0x01>");
    assert!(upgraded.contains("[Peer]\nPublicKey = p"));
    assert_eq!(
        fs::read_to_string(dir.join("wg0.conf.pre-awg31")).unwrap(),
        original
    );
    // A 3.1 config stays as it is.
    assert_eq!(migrate_file(&path, "quic").unwrap(), None);
    assert_eq!(fs::read_to_string(&path).unwrap(), upgraded);
    validate_file(&path).unwrap();
    // Regeneration keeps the key and the first backup.
    regenerate_file(&path, "dns").unwrap();
    let regenerated = interface_values(&fs::read_to_string(&path).unwrap());
    assert_eq!(regenerated.get("PrivateKey"), "k");
    assert_eq!(regenerated.get("I3"), "<r 48>");
    assert_eq!(
        fs::read_to_string(dir.join("wg0.conf.pre-awg31")).unwrap(),
        original
    );
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn manual_parameters_replace_the_profile_and_invalid_ones_change_nothing() {
    let dir = temp("apply");
    let path = dir.join("wg0.conf");
    let profile = new_profile("quic").unwrap();
    let mut config = String::from("[Interface]\nPrivateKey = k\n");
    for (key, value) in profile.iter() {
        config.push_str(&format!("{key} = {value}\n"));
    }
    fs::write(&path, &config).unwrap();
    let mut parameters: serde_json::Map<String, Value> = profile
        .iter()
        .map(|(key, value)| (key.to_string(), Value::from(value)))
        .collect();
    parameters.insert("Jc".into(), "6".into());
    parameters.remove("ContentPaddingAddition");
    let file = dir.join("parameters.json");
    fs::write(&file, Value::Object(parameters.clone()).to_string()).unwrap();
    apply_file(&path, &file).unwrap();
    let applied = interface_values(&fs::read_to_string(&path).unwrap());
    assert_eq!(applied.get("Jc"), "6");
    assert_eq!(applied.get("ContentPaddingAddition"), "");
    assert_eq!(applied.get("PrivateKey"), "k");
    let before = fs::read_to_string(&path).unwrap();
    for bad in [
        json!({"Unknown": "1"}),
        json!({"Jc": 4}),
        json!({"Jc": "4\u{7f}"}),
        json!([]),
    ] {
        fs::write(&file, bad.to_string()).unwrap();
        assert!(apply_file(&path, &file).is_err(), "{bad}");
    }
    parameters.insert("Jmin".into(), "999".into());
    fs::write(&file, Value::Object(parameters).to_string()).unwrap();
    assert!(apply_file(&path, &file).is_err());
    assert_eq!(fs::read_to_string(&path).unwrap(), before);
    fs::remove_dir_all(&dir).unwrap();
}

use serde_json::json;

#[test]
fn every_preset_generates_complete_safe_profiles() {
    for preset in ["quic", "dns", "chaos"] {
        for _ in 0..20 {
            let profile = new_profile(preset).unwrap();
            assert!(AWG3_FIELDS.iter().all(|key| !profile.get(key).is_empty()));
            let headers: Vec<u64> = (1..=4)
                .map(|i| number(profile.get(&format!("H{i}"))))
                .collect();
            assert!(headers.windows(2).all(|pair| pair[0] < pair[1]));
            assert!(
                headers
                    .iter()
                    .all(|h| (5..=GENERATED_HEADER_MAX).contains(h))
            );
            let high = |key: &str| number(profile.get(key).rsplit('-').next().unwrap());
            let low = |key: &str| number(profile.get(key).split('-').next().unwrap());
            assert!(high("RekeyAfterTime") < low("RejectAfterTime"));
            assert!(high("KeepaliveTimeout") < 25);
            assert_eq!(profile.get("DisableCookies"), "off");
            match preset {
                "quic" => assert_eq!(profile.get("I1"), "<b 0xc000000001><rc 8><r 900>"),
                "dns" => assert_eq!(profile.get("I1"), "<rc 2><b 0x01000001000000000000><r 32>"),
                _ => {
                    let i1 = profile.get("I1");
                    assert!(
                        i1.starts_with("<b 0x") && i1[5..13].bytes().all(|b| b.is_ascii_hexdigit())
                    );
                    let r = number(i1.rsplit("<r ").next().unwrap().trim_end_matches('>'));
                    assert!((500..=900).contains(&r));
                }
            }
        }
    }
}

const OLD_SERVER: &str = "[Interface]\nPrivateKey = serverprivate\nAddress = 10.8.1.1/24\nListenPort = 51820\nJc = 4\nJmin = 40\nJmax = 120\nS1 = 12\nS2 = 14\nS3 = 16\nS4 = 18\nH1 = 100\nH2 = 200\nH3 = 300\nH4 = 400\nI1 = <b 0xc000000001><rc 8><r 1000>\nI2 = <b 0x40><rc 4><r 100>\nI3 = <r 1200>\nI4 = <r 100>\nI5 = <r 1200>\n\n[Peer]\nPublicKey = clientpublic\nPresharedKey = clientpsk\nAllowedIPs = 10.8.1.2/32\n";

#[test]
fn a_legacy_config_keeps_its_peer_and_i_sequence() {
    let migrated = migrate(OLD_SERVER, "quic").unwrap();
    let values = interface_values(&migrated);
    validate(&values, true).unwrap();
    assert_eq!(protocol_version(&values).unwrap(), "3.1");
    assert_eq!(values.get("I1"), "<b 0xc000000001><rc 8><r 1000>");
    assert_eq!(values.get("I3"), "<r 1000>");
    assert_eq!(values.get("I5"), "<r 1000>");
    assert!(migrated.contains("PublicKey = clientpublic"));
    assert_eq!(migrate(&migrated, "quic").unwrap(), migrated);
}

#[test]
fn padding_limits_and_single_timers_are_valid() {
    let mut profile = new_profile("dns").unwrap();
    for (key, limit) in PADDING_LIMITS {
        profile.set(key, limit.to_string());
    }
    // Maximum S1/S2 happen to produce equal packet sizes; use S2 one byte lower.
    profile.set("S2", (PADDING_LIMITS[1].1 - 1).to_string());
    for (key, value) in [
        ("RekeyAfterTime", "120"),
        ("RejectAfterTime", "180"),
        ("RekeyTimeout", "5"),
        ("KeepaliveTimeout", "10"),
        ("MaxHandshakeAttempts", "18"),
    ] {
        profile.set(key, value.into());
    }
    validate(&profile, true).unwrap();
    for (key, value) in [("S1", "1553"), ("S2", "1609"), ("S3", "1637"), ("S4", "33")] {
        let mut wrong = new_profile("quic").unwrap();
        wrong.set(key, value.into());
        assert!(validate(&wrong, true).is_err(), "{key}");
    }
}

#[test]
fn a_31_config_without_timers_is_not_rotated() {
    let mut profile = new_profile("quic").unwrap();
    for i in 1..=4 {
        profile.set(&format!("S{i}"), "24".into());
        profile.set(&format!("H{i}"), i.to_string());
    }
    profile
        .0
        .retain(|(key, _)| !TIMING_FIELDS.contains(&key.as_str()));
    let mut config = String::from("[Interface]\nPrivateKey = existing-server-key\n");
    for (key, value) in profile.iter() {
        config.push_str(&format!("{key} = {value}\n"));
    }
    config.push_str("\n# existing-device\n[Peer]\nPublicKey = existing-peer-key\nPresharedKey = existing-psk\nAllowedIPs = 10.8.1.2/32\n");
    validate(&profile, true).unwrap();
    assert_eq!(migrate(&config, "dns").unwrap(), config);
}

#[test]
fn regeneration_keeps_the_server_and_peer_keys() {
    let dir = temp("regenerate");
    let path = dir.join("wg0.conf");
    let profile = new_profile("quic").unwrap();
    let peers = "\n# device\n[Peer]\nPublicKey = peer-key\nPresharedKey = peer-psk\nAllowedIPs = 10.8.1.2/32\n";
    let mut original = String::from("[Interface]\nPrivateKey = server-key\nListenPort = 443\n");
    for (key, value) in profile.iter() {
        original.push_str(&format!("{key} = {value}\n"));
    }
    original.push_str(peers);
    fs::write(&path, &original).unwrap();
    assert_eq!(regenerate_file(&path, "dns").unwrap(), Some(UPGRADED));
    let updated = fs::read_to_string(&path).unwrap();
    assert_eq!(
        updated.split_once("[Peer]").unwrap().1,
        original.split_once("[Peer]").unwrap().1
    );
    assert!(
        updated.contains("PrivateKey = server-key\n") && updated.contains("ListenPort = 443\n")
    );
    assert_eq!(
        fs::read_to_string(dir.join("wg0.conf.pre-awg31")).unwrap(),
        original
    );
    let values = interface_values(&updated);
    assert_ne!(
        values.get("HeaderProtectionKey"),
        profile.get("HeaderProtectionKey")
    );
    validate(&values, true).unwrap();
    fs::remove_dir_all(&dir).unwrap();
}
