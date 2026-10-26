//! Backend-owned presets and a reversible node creation form.
use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;

#[derive(Clone, Debug, Deserialize)]
pub struct Template {
    pub code: String,
    pub title: String,
    pub region: String,
    pub flag: String,
    pub draft: Value,
}
#[derive(Clone, Debug, Deserialize)]
pub struct Options {
    pub local_available: bool,
    pub defaults: Value,
    pub templates: Vec<Template>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Draft {
    pub command_id: Uuid,
    pub body: Value,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Step {
    Transport,
    Template,
    Key,
    Title,
    Region,
    CustomRegion,
    Flag,
    Target,
    Host,
    Protocols,
    Review,
    Created,
}
pub struct Wizard {
    pub options: Options,
    pub step: Step,
    pub selected: usize,
    pub transport: String,
    pub template: Option<usize>,
    pub key: String,
    pub title: String,
    pub region: String,
    pub flag: String,
    pub target: String,
    pub host: String,
    pub protocols: Vec<String>,
    pub transports: Vec<String>,
    pub settings: Value,
    pub command_id: Uuid,
    pub created_key: Option<String>,
}
impl Wizard {
    pub fn new(options: Options) -> Result<Self> {
        ensure!(
            !options.templates.is_empty() && options.templates.len() <= 64,
            "The controller has no compatible node templates; update the controller first"
        );
        let protocols = serde_json::from_value(options.defaults["protocols"].clone())?;
        let transports = serde_json::from_value(options.defaults["xray_transports"].clone())?;
        let settings = options.defaults["settings"].clone();
        ensure!(settings.is_object(), "Invalid installation defaults");
        Ok(Self {
            options,
            step: Step::Transport,
            selected: 0,
            transport: "ssh".into(),
            template: None,
            key: String::new(),
            title: String::new(),
            region: String::new(),
            flag: String::new(),
            target: String::new(),
            host: String::new(),
            protocols,
            transports,
            settings,
            command_id: Uuid::new_v4(),
            created_key: None,
        })
    }
    pub fn title(&self) -> &'static str {
        match self.step {
            Step::Transport => "Node type",
            Step::Template => "Location template",
            Step::Key => "Server code",
            Step::Title => "Server name",
            Step::Region | Step::CustomRegion => "Region",
            Step::Flag => "Flag (optional)",
            Step::Target => "SSH address",
            Step::Host => "Public address for VPN clients",
            Step::Protocols => "VPN protocols",
            Step::Review => "Review new node",
            Step::Created => "Node created",
        }
    }
    pub fn selection_step(&self) -> bool {
        matches!(self.step, Step::Transport | Step::Template | Step::Region)
    }
    pub fn choices(&self) -> Vec<String> {
        match self.step {
            Step::Transport => {
                if self.options.local_available {
                    vec!["Local node".into(), "SSH node".into()]
                } else {
                    vec!["SSH node".into()]
                }
            }
            Step::Template => self
                .options
                .templates
                .iter()
                .map(|t| format!("{} {} · {}", t.flag, t.title, t.region))
                .chain(std::iter::once("Custom location".into()))
                .collect(),
            Step::Region => [
                "🌍 Europe",
                "🌏 Asia",
                "🌎 North America",
                "🌎 South America",
                "🌍 Africa",
                "🌏 Oceania",
                "Other",
            ]
            .iter()
            .map(|s| (*s).into())
            .collect(),
            Step::Protocols => ["awg", "xray"]
                .iter()
                .map(|p| {
                    format!(
                        "{} {}",
                        if self.protocols.iter().any(|s| s == p) {
                            "✓"
                        } else {
                            "○"
                        },
                        if *p == "awg" { "AmneziaWG" } else { "VLESS" }
                    )
                })
                .collect(),
            _ => vec![],
        }
    }
    pub fn field(&self) -> Option<&String> {
        match self.step {
            Step::Key => Some(&self.key),
            Step::Title => Some(&self.title),
            Step::Flag => Some(&self.flag),
            Step::Target => Some(&self.target),
            Step::Host => Some(&self.host),
            Step::CustomRegion => Some(&self.region),
            _ => None,
        }
    }
    pub fn field_mut(&mut self) -> Option<&mut String> {
        match self.step {
            Step::Key => Some(&mut self.key),
            Step::Title => Some(&mut self.title),
            Step::Flag => Some(&mut self.flag),
            Step::Target => Some(&mut self.target),
            Step::Host => Some(&mut self.host),
            Step::CustomRegion => Some(&mut self.region),
            _ => None,
        }
    }
    pub fn append(&mut self, text: &str) {
        if let Some(field) = self.field_mut() {
            let remaining = 255usize.saturating_sub(field.chars().count());
            field.extend(text.chars().filter(|c| !c.is_control()).take(remaining));
        }
    }
    fn set_step(&mut self, step: Step) {
        self.step = step;
        self.selected = 0;
    }
    pub fn choose(&mut self, index: usize) -> Result<()> {
        ensure!(index < self.choices().len(), "Invalid selection");
        self.selected = index;
        match self.step {
            Step::Transport => {
                self.transport = if self.options.local_available && index == 0 {
                    "local"
                } else {
                    "ssh"
                }
                .into();
                self.set_step(Step::Template);
            }
            Step::Template => {
                if index == self.options.templates.len() {
                    if self.template.take().is_some() {
                        self.key.clear();
                        self.title.clear();
                        self.region.clear();
                        self.flag.clear();
                    }
                    self.set_step(Step::Key);
                } else {
                    let t = &self.options.templates[index];
                    if self.template != Some(index) {
                        self.key = t.draft["key"].as_str().unwrap_or_default().into();
                        self.title = t.draft["title"].as_str().unwrap_or_default().into();
                        self.region = t.region.clone();
                        self.flag = t.flag.clone();
                    }
                    self.template = Some(index);
                    self.set_step(if self.transport == "ssh" {
                        Step::Target
                    } else {
                        Step::Host
                    });
                }
            }
            Step::Region if index < 6 => {
                self.region = [
                    "Europe",
                    "Asia",
                    "North America",
                    "South America",
                    "Africa",
                    "Oceania",
                ][index]
                    .into();
                self.set_step(Step::Flag);
            }
            Step::Region => self.set_step(Step::CustomRegion),
            Step::Protocols => {
                let p = ["awg", "xray"][index];
                if self.protocols.iter().any(|s| s == p) {
                    self.protocols.retain(|s| s != p);
                } else {
                    self.protocols.push(p.into());
                }
            }
            _ => {}
        }
        Ok(())
    }
    pub fn next(&mut self) -> Result<Option<Draft>> {
        match self.step {
            Step::Transport | Step::Template => {
                self.choose(self.selected)?;
            }
            Step::Key => {
                self.key = self.key.trim().into();
                ensure!(
                    !self.key.is_empty()
                        && self.key.len() <= 64
                        && self.key.as_bytes()[0].is_ascii_alphanumeric()
                        && self
                            .key
                            .bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b)),
                    "Use a code of up to 64 letters, numbers, underscores or hyphens"
                );
                self.set_step(Step::Title);
            }
            Step::Title => {
                ensure!(
                    !self.title.trim().is_empty() && self.title.chars().count() <= 128,
                    "Enter a server name (up to 128 characters)"
                );
                self.title = self.title.trim().into();
                self.set_step(Step::Region);
            }
            Step::Region => {
                self.choose(self.selected)?;
            }
            Step::CustomRegion => {
                ensure!(
                    !self.region.trim().is_empty() && self.region.chars().count() <= 128,
                    "Enter the region name"
                );
                self.region = self.region.trim().into();
                self.set_step(Step::Flag);
            }
            Step::Flag => {
                ensure!(self.flag.chars().count() <= 16, "The flag is too long");
                self.set_step(if self.transport == "ssh" {
                    Step::Target
                } else {
                    Step::Host
                });
            }
            Step::Target => {
                let target = self
                    .target
                    .trim()
                    .strip_suffix(":22")
                    .unwrap_or(self.target.trim());
                ensure!(
                    valid_target(target),
                    "Enter user@hostname or user@IP (SSH port 22)"
                );
                self.target = target.into();
                self.set_step(Step::Host);
            }
            Step::Host => {
                self.host = self.host.trim().into();
                ensure!(
                    valid_public_host(&self.host) || self.transport == "local" && self.host == ".",
                    "Enter a valid public DNS name or IPv4 address, without a user, URL scheme or port"
                );
                self.set_step(Step::Protocols);
            }
            Step::Protocols => {
                self.set_step(Step::Review);
            }
            Step::Review => return Ok(Some(self.draft())),
            Step::Created => {}
        }
        Ok(None)
    }
    pub fn back(&mut self) -> bool {
        let step = match self.step {
            Step::Transport | Step::Created => return false,
            Step::Template => Step::Transport,
            Step::Key => Step::Template,
            Step::Title => Step::Key,
            Step::Region => Step::Title,
            Step::CustomRegion => Step::Region,
            Step::Flag => Step::Region,
            Step::Target => {
                if self.template.is_some() {
                    Step::Template
                } else {
                    Step::Flag
                }
            }
            Step::Host if self.transport == "ssh" => Step::Target,
            Step::Host => {
                if self.template.is_some() {
                    Step::Template
                } else {
                    Step::Flag
                }
            }
            Step::Protocols => Step::Host,
            Step::Review => Step::Protocols,
        };
        self.set_step(step);
        true
    }
    pub fn draft(&self) -> Draft {
        let mut settings = self.settings.clone();
        settings["public_host"] = json!(self.host);
        let mut body = json!({"key":self.key, "title":self.title, "region":self.region, "flag":self.flag,
            "transport":self.transport, "ssh_target":if self.transport == "ssh" { Some(&self.target) } else { None },
            "protocols":self.protocols,"xray_transports":if self.protocols.iter().any(|p| p == "xray") { if self.transports.is_empty() { vec!["tcp".into(), "xhttp".into()] } else { self.transports.clone() } } else { vec![] }, "settings":settings});
        if let Some(index) = self.template {
            body["template"] = json!(self.options.templates[index].code);
        }
        Draft {
            command_id: self.command_id,
            body,
        }
    }
    pub fn summary(&self) -> String {
        format!(
            "{} {} · {}\nCode: {}{}\nType: {}\nSSH: {}\nPublic address: {}\nProtocols: {}\nAWG preset: {} · Port: {}\n\nCreates a registry entry. Install the agent and protocols from its card.",
            self.flag,
            self.title,
            self.region,
            self.key,
            if self.template.is_some() {
                " (next available number)"
            } else {
                ""
            },
            self.transport,
            if self.transport == "ssh" {
                &self.target
            } else {
                "—"
            },
            self.host,
            if self.protocols.is_empty() {
                "None (agent only)".into()
            } else {
                self.protocols.join(" · ")
            },
            self.settings["awg_i1_preset"].as_str().unwrap_or("quic"),
            self.settings["awg_port_mode"].as_str().unwrap_or("auto")
        )
    }
}
pub(crate) fn valid_target(target: &str) -> bool {
    if target.is_empty() || target.len() > 255 {
        return false;
    }
    let Some((user, host)) = target.split_once('@') else {
        return false;
    };
    if user.is_empty()
        || !(user.as_bytes()[0].is_ascii_alphabetic() || user.starts_with('_'))
        || !user
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
    {
        return false;
    }
    if host.starts_with('[') && host.ends_with(']') {
        host[1..host.len() - 1]
            .parse::<std::net::Ipv6Addr>()
            .is_ok()
    } else {
        valid_public_host(host)
    }
}

pub(crate) fn valid_public_host(host: &str) -> bool {
    if host.parse::<std::net::Ipv4Addr>().is_ok() {
        return true;
    }
    if host.is_empty() || host.len() > 253 || !host.is_ascii() {
        return false;
    }
    // A numeric target is not a DNS name; malformed IPv4 must not fall back to DNS.
    if !host
        .rsplit('.')
        .next()
        .is_some_and(|label| label.bytes().any(|b| b.is_ascii_alphabetic()))
    {
        return false;
    }
    host.split('.').all(|label| {
        !label.is_empty()
            && label.len() <= 63
            && label.as_bytes()[0].is_ascii_alphanumeric()
            && label.as_bytes()[label.len() - 1].is_ascii_alphanumeric()
            && label
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-')
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn options(local_available: bool) -> Options {
        serde_json::from_value(json!({"local_available":local_available,
            "defaults":{"protocols":["awg","xray"],"xray_transports":["xhttp"],"settings":{"awg_i1_preset":"dns","awg_port_mode":"auto"}},
            "templates":[{"code":"lv","title":"Latvia","region":"Europe","flag":"🇱🇻","draft":{"key":"lv3","title":"Latvia #3"}}]})).unwrap()
    }
    #[test]
    fn switching_from_preset_to_custom_clears_preset_identity_but_keeps_custom_draft() {
        let mut wizard = Wizard::new(options(false)).unwrap();
        wizard.choose(0).unwrap();
        wizard.choose(0).unwrap();
        assert_eq!(wizard.key, "lv3");
        wizard.back();
        wizard.choose(1).unwrap();
        assert!(
            wizard.key.is_empty()
                && wizard.title.is_empty()
                && wizard.region.is_empty()
                && wizard.flag.is_empty()
        );
        wizard.append("custom1");
        wizard.back();
        wizard.choose(1).unwrap();
        assert_eq!(wizard.key, "custom1");
    }
    #[test]
    fn existing_local_is_not_offered_and_ssh_template_needs_only_addresses() {
        let mut wizard = Wizard::new(options(false)).unwrap();
        assert_eq!(wizard.choices(), vec!["SSH node"]);
        wizard.choose(0).unwrap();
        wizard.choose(0).unwrap();
        assert_eq!(wizard.step, Step::Target);
        wizard.append("root@vpn.example:22");
        wizard.next().unwrap();
        wizard.append("vpn.example");
        wizard.next().unwrap();
        wizard.next().unwrap();
        let draft = wizard.next().unwrap().unwrap();
        assert_eq!(draft.body["key"], "lv3");
        assert_eq!(draft.body["template"], "lv");
        assert_eq!(draft.body["ssh_target"], "root@vpn.example");
        assert_eq!(draft.body["settings"]["awg_i1_preset"], "dns");
        assert_eq!(draft.body["xray_transports"], json!(["xhttp"]));
        assert_eq!(draft.command_id, wizard.next().unwrap().unwrap().command_id);
    }
    #[test]
    fn local_template_skips_ssh_and_back_retains_address_and_protocols() {
        let mut wizard = Wizard::new(options(true)).unwrap();
        wizard.choose(0).unwrap();
        wizard.choose(0).unwrap();
        assert_eq!(wizard.step, Step::Host);
        wizard.append("local.example");
        wizard.next().unwrap();
        wizard.choose(1).unwrap(); // AWG only
        wizard.back();
        wizard.back();
        wizard.choose(0).unwrap();
        assert_eq!(wizard.host, "local.example");
        wizard.next().unwrap();
        wizard.next().unwrap();
        let body = wizard.next().unwrap().unwrap().body;
        assert_eq!(body["transport"], "local");
        assert!(body["ssh_target"].is_null());
        assert_eq!(body["xray_transports"], json!([]));
    }
    #[test]
    fn custom_fields_and_selection_validation() {
        let mut wizard = Wizard::new(options(true)).unwrap();
        wizard.choose(1).unwrap();
        wizard.choose(1).unwrap();
        assert!(wizard.next().is_err());
        wizard.append("../bad");
        assert!(wizard.next().is_err());
        wizard.key = "private1".into();
        wizard.next().unwrap();
        wizard.append("Private VPN");
        wizard.next().unwrap();
        wizard.choose(6).unwrap();
        wizard.append("Custom region");
        wizard.next().unwrap();
        wizard.next().unwrap();
        assert_eq!(wizard.step, Step::Target);
        wizard.append("root@bad;command");
        assert!(wizard.next().is_err());
        wizard.target = "root@[2001:db8::1]".into();
        wizard.next().unwrap();
        wizard.append("vpn.example");
        wizard.next().unwrap();
        wizard.choose(0).unwrap();
        wizard.choose(1).unwrap();
        wizard.next().unwrap();
        assert_eq!(wizard.step, Step::Review);
        assert!(
            wizard.draft().body["protocols"]
                .as_array()
                .unwrap()
                .is_empty()
        );
        assert!(wizard.back());
        wizard.choose(0).unwrap();
        wizard.next().unwrap();
        assert!(
            wizard
                .next()
                .unwrap()
                .unwrap()
                .body
                .get("template")
                .is_none()
        );
        assert!(wizard.back());
        assert_eq!(wizard.step, Step::Protocols);
    }
    #[test]
    fn addresses_are_validated_before_advancing() {
        for target in [
            "123",
            "vpn.example",
            "root@123",
            "root@999.1.2.3",
            "root@-vpn.example",
            "root@vpn..example",
            "root@vpn.example:2222",
            "root@vpn.example:22:22",
            "root@@vpn.example",
            "root@https://vpn.example",
        ] {
            let mut wizard = Wizard::new(options(false)).unwrap();
            wizard.step = Step::Target;
            wizard.target = target.into();
            assert!(wizard.next().is_err(), "accepted {target}");
            assert_eq!(wizard.step, Step::Target);
            assert_eq!(wizard.target, target);
            assert!(wizard.next().is_err());
        }
        for target in [
            "root@vpn.example",
            "ubuntu@203.0.113.10",
            "root@[2001:db8::1]",
            "root@vpn.example:22",
        ] {
            let mut wizard = Wizard::new(options(false)).unwrap();
            wizard.step = Step::Target;
            wizard.target = target.into();
            wizard.next().unwrap();
            assert_eq!(wizard.step, Step::Host);
        }
        for host in [
            "123",
            "999.1.2.3",
            "1.2.3",
            "vpn..example",
            "-vpn.example",
            "vpn-.example",
            "root@vpn.example",
            "https://vpn.example",
            "vpn.example:443",
            "vpn.example/path",
            ".",
        ] {
            let mut wizard = Wizard::new(options(false)).unwrap();
            wizard.step = Step::Host;
            wizard.host = host.into();
            assert!(wizard.next().is_err(), "accepted {host}");
            assert_eq!(wizard.step, Step::Host);
        }
        for host in [
            "vpn.example",
            "vpn-1.example.com",
            "203.0.113.10",
            "localhost",
        ] {
            assert!(valid_public_host(host), "rejected {host}");
        }
        assert!(!valid_public_host(&format!("{}.example", "a".repeat(64))));
        let mut local = Wizard::new(options(true)).unwrap();
        local.transport = "local".into();
        local.step = Step::Host;
        local.host = ".".into();
        local.next().unwrap();
        assert_eq!(local.step, Step::Protocols);
    }
}
