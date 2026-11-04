//! Node settings in the AWG server config: the listen port and the
//! obfuscation preset. Generating and validating parameters stays in
//! `awg_profile.py`, which the controller shares.
use std::fs;
use std::path::{Path, PathBuf};

/// Remembers which preset generated the current parameters, or `manual`.
pub(crate) const PRESET_FILE: &str = ".node-plane-i1-preset";

/// Sets the first `ListenPort = N` line.
pub(crate) fn listen_port(text: &str, port: &str) -> Result<String, String> {
    let mut done = false;
    let lines: Vec<String> = text
        .split_inclusive('\n')
        .map(|line| {
            let (content, end) = line.strip_suffix('\n').map_or((line, ""), |c| (c, "\n"));
            let is_port = content.split_once('=').is_some_and(|(key, value)| {
                key.trim_end() == "ListenPort"
                    && !value.trim().is_empty()
                    && value.trim().bytes().all(|c| c.is_ascii_digit())
                    && !content.starts_with(char::is_whitespace)
            });
            if is_port && !done {
                done = true;
                format!("ListenPort = {port}{end}")
            } else {
                line.to_string()
            }
        })
        .collect();
    if !done {
        return Err("AWG ListenPort is missing".into());
    }
    Ok(lines.concat())
}

/// The preset of the current parameters: recorded, or recognized from I1.
pub(crate) fn current_preset(config: &Path, text: &str) -> String {
    let recorded = config.with_file_name(PRESET_FILE);
    if let Ok(preset) = fs::read_to_string(recorded) {
        return preset.trim_end_matches('\n').to_string();
    }
    let i1 = text
        .lines()
        .find_map(|line| line.split_once(" = ").filter(|(key, _)| *key == "I1"))
        .map(|(_, value)| value)
        .unwrap_or_default();
    if i1.starts_with("<b 0xc000000001>") {
        "quic"
    } else if i1.starts_with("<rc 2><b 0x01000001000000000000>") {
        "dns"
    } else {
        "chaos"
    }
    .to_string()
}

/// Without `config`, the only other `*.conf` next to it becomes it.
pub(crate) fn adopt_config(config: &Path) -> Result<(), String> {
    if config.is_file() {
        return Ok(());
    }
    let dir = config.parent().unwrap_or(Path::new("."));
    let others: Vec<PathBuf> = fs::read_dir(dir)
        .map_err(|err| err.to_string())?
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| {
            path.is_file()
                && path.extension().is_some_and(|ext| ext == "conf")
                && path.file_name() != config.file_name()
        })
        .collect();
    let [only] = others.as_slice() else {
        return Err(format!(
            "AWG config missing: {}; cannot identify a unique existing interface config",
            config.display()
        ));
    };
    super::super::settings::copy_preserving(only, config)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocols::test_dir;

    #[test]
    fn only_the_first_listen_port_line_changes() {
        let text = "[Interface]\nListenPort = 51820\nAddress = 10.8.1.1/24\n# ListenPort = 1\n";
        assert_eq!(
            listen_port(text, "443").unwrap(),
            "[Interface]\nListenPort = 443\nAddress = 10.8.1.1/24\n# ListenPort = 1\n"
        );
        assert_eq!(listen_port("ListenPort=1", "2").unwrap(), "ListenPort = 2");
        assert!(listen_port("[Interface]\n", "443").is_err());
    }

    #[test]
    fn the_preset_is_recorded_or_recognized() {
        let dir = test_dir("awg-preset");
        let config = dir.join("wg0.conf");
        assert_eq!(current_preset(&config, "I1 = <b 0xc000000001>ff\n"), "quic");
        assert_eq!(
            current_preset(&config, "I1 = <rc 2><b 0x01000001000000000000>\n"),
            "dns"
        );
        assert_eq!(current_preset(&config, "I1 = <r 80>\n"), "chaos");
        fs::write(dir.join(PRESET_FILE), "manual\n").unwrap();
        assert_eq!(current_preset(&config, "I1 = <b 0xc000000001>\n"), "manual");
    }

    #[test]
    fn a_unique_existing_interface_config_is_adopted() {
        let dir = test_dir("awg-adopt");
        let config = dir.join("wg0.conf");
        assert!(adopt_config(&config).is_err());
        fs::write(dir.join("awg0.conf"), "[Interface]\n").unwrap();
        adopt_config(&config).unwrap();
        assert_eq!(fs::read_to_string(&config).unwrap(), "[Interface]\n");
        fs::remove_file(&config).unwrap();
        fs::write(dir.join("other.conf"), "").unwrap();
        assert!(adopt_config(&config).is_err());
    }
}
