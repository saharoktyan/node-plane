//! `node-plane-agent awg-peer add NAME`: one AWG peer with its client config
//! and AmneziaVPN key. Replaces `awg-add-user.sh` and the restore path of
//! `awg-peer-state.py`; the profile intent journal still runs
//! `awg-add-user.sh`, which only starts this subcommand.
//!
//! An issued peer keeps its config in `<clients>/<name>.txt`, and a repeated
//! add returns it. A revoked peer comes back from its archive with the same
//! keys and address. Parameter validation stays in `awg_profile.py`, which
//! the controller shares.
use std::collections::BTreeSet;
use std::fs;
use std::io::Write;
use std::net::IpAddr;
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use super::Node;
use super::addresses::{capture_routes, dual_addresses, free_address};
use super::client::{AWG3_FIELDS, LEGACY_FIELDS, vpn_key};
use super::peer_sync::atomic_write;
use super::peers::{Archive, fields, fingerprint, peer_blocks, split_peer};

/// The live interface and key generation in the AWG container.
pub(crate) trait Runtime {
    /// `wg show <interface> <what>`.
    fn show(&self, what: &str) -> Result<String, String>;
    /// A new private key, its public key and a preshared key.
    fn generate(&self) -> Result<[String; 3], String>;
    fn set_peer(&self, key: &str, preshared: &str, allowed: &str) -> Result<(), String>;
    fn remove_peer(&self, key: &str) -> Result<(), String>;
    /// `awg_profile.py validate` of a config file.
    fn validate(&self, config: &Path) -> Result<(), String>;
}

/// The node's AWG runtime with its profile tool.
pub(crate) struct NodeRuntime<'a> {
    pub(crate) node: &'a Node,
    pub(crate) profile_tool: String,
}

fn exec(
    container: &str,
    script: &str,
    args: &[&str],
    input: Option<&str>,
) -> Result<String, String> {
    let mut child = Command::new("docker")
        .args(["exec", "-i", container, "sh", "-c", script, "awg-peer"])
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| format!("docker failed: {err}"))?;
    let mut stdin = child.stdin.take().ok_or("docker stdin missing")?;
    stdin
        .write_all(input.unwrap_or("").as_bytes())
        .map_err(|err| err.to_string())?;
    drop(stdin);
    let output = child.wait_with_output().map_err(|err| err.to_string())?;
    if !output.status.success() {
        return Err(format!(
            "AWG container command failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

impl Runtime for NodeRuntime<'_> {
    fn show(&self, what: &str) -> Result<String, String> {
        self.node.wg_show(what)
    }

    fn generate(&self) -> Result<[String; 3], String> {
        let output = exec(
            &self.node.container,
            r#"umask 077; private=$(wg genkey); public=$(printf "%s" "$private" | wg pubkey); echo "$private $public $(wg genpsk)""#,
            &[],
            None,
        )?;
        let keys: Vec<&str> = output.split_whitespace().collect();
        let [private, public, preshared] = keys.as_slice() else {
            return Err("AWG key generation failed".into());
        };
        Ok([
            private.to_string(),
            public.to_string(),
            preshared.to_string(),
        ])
    }

    fn set_peer(&self, key: &str, preshared: &str, allowed: &str) -> Result<(), String> {
        // The preshared key goes through stdin and a private file, never argv.
        exec(
            &self.node.container,
            r#"umask 077; p=$(mktemp); trap 'rm -f "$p"' EXIT; cat > "$p"; wg set "$1" peer "$2" preshared-key "$p" allowed-ips "$3""#,
            &[&self.node.interface, key, &allowed.replace(' ', "")],
            Some(&format!("{preshared}\n")),
        )
        .map(drop)
    }

    fn remove_peer(&self, key: &str) -> Result<(), String> {
        super::peer_sync::Interface::remove(self.node, key)
    }

    fn validate(&self, config: &Path) -> Result<(), String> {
        let output = Command::new("python3")
            .arg(&self.profile_tool)
            .arg("validate")
            .arg(config)
            .output()
            .map_err(|err| format!("awg_profile.py failed: {err}"))?;
        output.status.success().then_some(()).ok_or_else(|| {
            format!(
                "invalid AWG profile: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            )
        })
    }
}

/// What a new client config needs besides the server config.
pub(crate) struct Issue {
    pub(crate) config: PathBuf,
    pub(crate) clients: PathBuf,
    /// The peer name: `<server key>-<profile>`.
    pub(crate) name: String,
    /// Shown in AmneziaVPN: `<server key> · <profile>`.
    pub(crate) description: String,
    pub(crate) container: String,
    pub(crate) endpoint: String,
    pub(crate) dns: String,
    pub(crate) mtu: String,
    pub(crate) allowed_ips: String,
    pub(crate) keepalive: String,
}

/// AllowedIPs lines get the peer's managed IPv6 address (`awg_ipv6.migrate`).
fn migrate_peer(block: &str) -> Result<String, String> {
    let mut seen_peer = false;
    let mut lines = Vec::new();
    for line in block.split_inclusive('\n') {
        let (content, end) = line.strip_suffix('\n').map_or((line, ""), |c| (c, "\n"));
        seen_peer |= content.trim() == "[Peer]";
        let value = content
            .strip_prefix("AllowedIPs")
            .map(|rest| rest.trim_start_matches([' ', '\t']))
            .and_then(|rest| rest.strip_prefix('='))
            .map(|value| value.trim_start_matches([' ', '\t']));
        match value {
            Some(value) if seen_peer && !value.is_empty() => {
                lines.push(format!("AllowedIPs = {}{end}", dual_addresses(value)?));
            }
            _ => lines.push(line.to_string()),
        }
    }
    Ok(lines.concat())
}

fn split_list(value: &str) -> BTreeSet<String> {
    value
        .split(',')
        .map(|part| part.trim().to_string())
        .collect()
}

/// Brings a revoked peer back from its archive. `false` when there is
/// nothing to restore: no archive, no client config, another server, or
/// its address taken meanwhile (the archive is then dropped).
fn restore(issue: &Issue, runtime: &impl Runtime) -> Result<bool, String> {
    let archive = issue.clients.join(format!("{}.revoked.json", issue.name));
    if !archive.exists() || !issue.clients.join(format!("{}.txt", issue.name)).exists() {
        return Ok(false);
    }
    let text = fs::read_to_string(&issue.config).map_err(|err| err.to_string())?;
    let record: Archive =
        serde_json::from_str(&fs::read_to_string(&archive).map_err(|err| err.to_string())?)
            .map_err(|err| err.to_string())?;
    let drop_archive = || {
        fs::remove_file(&archive)
            .map(|()| false)
            .map_err(|err| err.to_string())
    };
    if record.server != fingerprint(&text)? {
        return drop_archive();
    }
    let peer = migrate_peer(&record.peer)?;
    let data = fields(&peer);
    let (current, present) = split_peer(&text, &issue.name);
    // An older runtime may have reused the revoked peer's address.
    let reserved: BTreeSet<String> = peer_blocks(&current)
        .flat_map(|block| split_list(fields(block).get("AllowedIPs").map_or("", String::as_str)))
        .collect();
    let allowed = data.get("AllowedIPs").cloned().unwrap_or_default();
    if present.is_empty() && !reserved.is_disjoint(&split_list(&allowed)) {
        return drop_archive();
    }
    atomic_write(
        &issue.config,
        &format!("{}\n\n{}\n", current.trim_end(), peer.trim_end()),
    )?;
    let key = data
        .get("PublicKey")
        .ok_or("archived peer has no public key")?;
    let preshared = data
        .get("PresharedKey")
        .ok_or("archived peer has no preshared key")?;
    runtime.set_peer(key, preshared, &allowed)?;
    // The archive stays until the next revocation, so retries are safe.
    Ok(true)
}

/// Removes a peer that has no saved client config: an earlier failure left
/// it without a private key to deliver.
fn remove_orphan(issue: &Issue, text: &str, runtime: &impl Runtime) -> Result<String, String> {
    let lines: Vec<&str> = text.split_inclusive('\n').collect();
    let comment = format!("# {}", issue.name);
    let start = lines
        .iter()
        .position(|line| line.trim_end_matches('\n') == comment)
        .ok_or("peer is missing")?;
    let mut end = start + 1;
    while end < lines.len() && !lines[end].trim().is_empty() {
        end += 1;
    }
    let key = lines[start + 1..end]
        .iter()
        .find_map(|line| line.strip_prefix("PublicKey = "))
        .and_then(|rest| rest.split_whitespace().next())
        .ok_or_else(|| format!("Existing AWG peer has no public key: {}", issue.name))?;
    runtime.remove_peer(key)?;
    // The blank line after the block goes with it.
    let after = (end + 1).min(lines.len());
    let remaining = [&lines[..start], &lines[after..]].concat().concat();
    atomic_write(&issue.config, &remaining)?;
    Ok(remaining)
}

/// Addresses in use: live, on disk and held by archives of this server.
fn used_addresses(text: &str, clients: &Path, live: &str) -> Result<Vec<String>, String> {
    let mut used: Vec<String> = live
        .lines()
        .filter_map(|line| line.split_once('\t').map(|(_, ips)| ips))
        .flat_map(|ips| ips.split([',', ' ']))
        .map(|part| part.split('/').next().unwrap_or("").to_string())
        .collect();
    let address = |value: &str| -> Result<String, String> {
        let ip = value.trim().split('/').next().unwrap_or("");
        ip.parse::<IpAddr>()
            .map(|ip| ip.to_string())
            .map_err(|_| format!("invalid address: {value}"))
    };
    for block in text.split("[Peer]") {
        let data = fields(block);
        let value = data
            .get("AllowedIPs")
            .filter(|value| !value.is_empty())
            .or_else(|| data.get("Address"))
            .map_or("", String::as_str);
        for part in value.split(',').filter(|part| !part.trim().is_empty()) {
            used.push(address(part)?);
        }
    }
    let server = fingerprint(text)?;
    for entry in fs::read_dir(clients)
        .map_err(|err| err.to_string())?
        .flatten()
    {
        if !entry
            .file_name()
            .to_string_lossy()
            .ends_with(".revoked.json")
        {
            continue;
        }
        let record: Archive =
            serde_json::from_str(&fs::read_to_string(entry.path()).map_err(|err| err.to_string())?)
                .map_err(|err| err.to_string())?;
        if record.server == server {
            let data = fields(&record.peer);
            for part in data.get("AllowedIPs").map_or("", String::as_str).split(',') {
                used.push(address(part)?);
            }
        }
    }
    Ok(used)
}

/// The obfuscation parameters of the server config (`key = value` lines).
fn server_parameters(text: &str) -> Vec<(&'static str, String)> {
    let mut values: Vec<(&'static str, String)> = Vec::new();
    for line in text.lines() {
        let Some((key, value)) = line.trim().split_once(" = ") else {
            continue;
        };
        if let Some(known) = LEGACY_FIELDS
            .iter()
            .chain(&AWG3_FIELDS)
            .find(|known| **known == key)
        {
            values.retain(|(name, _)| name != known);
            values.push((known, value.trim().to_string()));
        }
    }
    values
}

fn client_config(
    issue: &Issue,
    text: &str,
    keys: &[String; 3],
    address: &str,
    server_key: &str,
) -> String {
    let parameters = server_parameters(text);
    let value = |key: &str| {
        parameters
            .iter()
            .find(|(name, _)| *name == key)
            .map_or("", |(_, value)| value.as_str())
    };
    let mut lines = vec![
        "[Interface]".to_string(),
        format!("PrivateKey = {}", keys[0]),
        format!("PublicKey = {}", keys[1]),
        format!("Address = {address}"),
        format!("DNS = {}", issue.dns),
        format!("MTU = {}", issue.mtu),
        String::new(),
    ];
    lines.extend(
        LEGACY_FIELDS
            .iter()
            .map(|key| format!("{key} = {}", value(key))),
    );
    // Timing windows follow the switches, as before.
    for key in [
        "HeaderProtectionKey",
        "ContentPaddingAddition",
        "RandomTrailers",
        "DisableCookies",
        "RekeyAfterTime",
        "RekeyTimeout",
        "RejectAfterTime",
        "KeepaliveTimeout",
        "MaxHandshakeAttempts",
    ] {
        if !value(key).is_empty() {
            lines.push(format!("{key} = {}", value(key)));
        }
    }
    lines.extend([
        String::new(),
        "[Peer]".to_string(),
        format!("PublicKey = {server_key}"),
        format!("PresharedKey = {}", keys[2]),
        format!("Endpoint = {}", issue.endpoint),
        format!("AllowedIPs = {}", issue.allowed_ips),
        format!("PersistentKeepalive = {}", issue.keepalive),
    ]);
    lines.join("\n") + "\n"
}

fn write_private(dir: &Path, name: &str, text: &str) -> Result<PathBuf, String> {
    let path = dir.join(name);
    let mut file = fs::File::create(&path).map_err(|err| err.to_string())?;
    file.set_permissions(fs::Permissions::from_mode(0o600))
        .map_err(|err| err.to_string())?;
    file.write_all(text.as_bytes())
        .map_err(|err| err.to_string())?;
    file.sync_all().map_err(|err| err.to_string())?;
    Ok(path)
}

/// Issues the peer, or returns its saved client config; the caller holds
/// the config lock.
pub(crate) fn add(issue: &Issue, runtime: &impl Runtime) -> Result<String, String> {
    runtime.validate(&issue.config)?;
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&issue.clients)
        .map_err(|err| err.to_string())?;
    fs::set_permissions(&issue.clients, fs::Permissions::from_mode(0o700))
        .map_err(|err| err.to_string())?;
    let saved = issue.clients.join(format!("{}.txt", issue.name));
    let read_saved = || fs::read_to_string(&saved).map_err(|err| err.to_string());
    if restore(issue, runtime)? {
        return read_saved();
    }
    let mut text = fs::read_to_string(&issue.config).map_err(|err| err.to_string())?;
    if text.lines().any(|line| line == format!("# {}", issue.name)) {
        if fs::metadata(&saved).is_ok_and(|meta| meta.len() > 0) {
            return read_saved();
        }
        text = remove_orphan(issue, &text, runtime)?;
    }
    let used = used_addresses(&text, &issue.clients, &runtime.show("allowed-ips")?)?;
    let free = free_address(&used).ok_or("No free AWG addresses")?;
    let address = dual_addresses(&format!("{free}/32"))?;
    let keys = runtime.generate()?;
    let server_key = runtime.show("public-key")?.trim().to_string();
    runtime.set_peer(&keys[1], &keys[2], &address)?;
    let block = format!(
        "\n# {}\n[Peer]\nPublicKey = {}\nPresharedKey = {}\nAllowedIPs = {address}\n",
        issue.name, keys[1], keys[2]
    );
    atomic_write(&issue.config, &(text + &block))?;

    let conf = client_config(
        issue,
        &fs::read_to_string(&issue.config).map_err(|err| err.to_string())?,
        &keys,
        &address,
        &server_key,
    );
    let staged = write_private(
        &issue.clients,
        &format!(".client-{}.conf", std::process::id()),
        &conf,
    )?;
    let checked = runtime.validate(&staged);
    let _ = fs::remove_file(&staged);
    checked?;
    let key = vpn_key(&conf, &issue.container, &issue.description)?;
    let result = format!(
        "{conf}\n=========== AMNEZIA TEXT KEY (vpn://) ===========\n{key}\n=================================================\n"
    );
    let staged = write_private(
        &issue.clients,
        &format!(".client-{}", std::process::id()),
        &result,
    )?;
    fs::rename(&staged, &saved).map_err(|err| err.to_string())?;
    Ok(result)
}

/// The issue for `profile` from node.env settings.
pub(crate) fn issue_for(
    node: &Node,
    profile: &str,
    setting: crate::protocols::Setting<'_>,
) -> Result<Issue, String> {
    let server = setting("AWG_SERVER_IP", "");
    if server.is_empty() {
        return Err("AWG_SERVER_IP is not configured in /etc/node-plane/node.env".into());
    }
    let description = if node.server_key.is_empty() {
        profile.to_string()
    } else {
        format!("{} · {profile}", node.server_key)
    };
    Ok(Issue {
        config: node.config.clone(),
        clients: node.clients.clone(),
        name: node.peer_name(profile),
        description,
        container: node.container.clone(),
        endpoint: format!("{server}:{}", setting("AWG_SERVER_PORT", "51820")),
        dns: setting("AWG_DNS", "1.1.1.1"),
        mtu: setting("AWG_MTU", "1280"),
        allowed_ips: capture_routes(&setting("AWG_ALLOWED_IPS", "0.0.0.0/0"))?,
        keepalive: setting("AWG_KEEPALIVE", "25"),
    })
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use super::*;
    use crate::protocols::test_dir;

    const SERVER: &str = "[Interface]\nPrivateKey = serverprivate\nAddress = 10.8.1.1/24\nListenPort = 51820\nJc = 5\nJmin = 40\nJmax = 146\nS1 = 31\nS2 = 68\nS3 = 44\nS4 = 25\nH1 = 1\nH2 = 2\nH3 = 3\nH4 = 4\nI1 = <r 1>\nI2 = <r 1>\nI3 = <r 1>\nI4 = <r 1>\nI5 = <r 1>\nHeaderProtectionKey = 5h02EEnpOPPpN8RVIWwRS0gtOUprabtR2Om1nSsmvC0=\nRekeyAfterTime = 102-130\nRandomTrailers = on\nDisableCookies = off\n";

    #[derive(Default)]
    struct Fake {
        keys: RefCell<Vec<[String; 3]>>,
        live: RefCell<Vec<(String, String)>>,
        log: RefCell<Vec<String>>,
    }

    impl Runtime for Fake {
        fn show(&self, what: &str) -> Result<String, String> {
            Ok(match what {
                "public-key" => "serverpublic\n".into(),
                _ => self
                    .live
                    .borrow()
                    .iter()
                    .map(|(key, ips)| format!("{key}\t{ips}\n"))
                    .collect(),
            })
        }
        fn generate(&self) -> Result<[String; 3], String> {
            Ok(self.keys.borrow_mut().remove(0))
        }
        fn set_peer(&self, key: &str, preshared: &str, allowed: &str) -> Result<(), String> {
            self.log
                .borrow_mut()
                .push(format!("set {key} {preshared} {allowed}"));
            self.live.borrow_mut().retain(|(known, _)| known != key);
            self.live
                .borrow_mut()
                .push((key.into(), allowed.replace(' ', "")));
            Ok(())
        }
        fn remove_peer(&self, key: &str) -> Result<(), String> {
            self.log.borrow_mut().push(format!("remove {key}"));
            self.live.borrow_mut().retain(|(known, _)| known != key);
            Ok(())
        }
        fn validate(&self, _: &Path) -> Result<(), String> {
            Ok(())
        }
    }

    fn keys(name: &str) -> [String; 3] {
        [
            format!("{name}private"),
            format!("{name}public"),
            format!("{name}psk"),
        ]
    }

    fn issue(dir: &Path, profile: &str) -> Issue {
        let node = Node {
            config: dir.join("wg0.conf"),
            clients: dir.join("clients"),
            container: "amnezia-awg".into(),
            interface: "wg0".into(),
            server_key: "msk1".into(),
        };
        let setting = |key: &str, default: &str| match key {
            "AWG_SERVER_IP" => "203.0.113.10".to_string(),
            _ => default.to_string(),
        };
        issue_for(&node, profile, &setting).unwrap()
    }

    #[test]
    fn a_new_peer_gets_the_next_address_and_a_saved_config() {
        let dir = test_dir("awg-issue");
        fs::write(dir.join("wg0.conf"), SERVER).unwrap();
        let fake = Fake::default();
        fake.keys.borrow_mut().extend([keys("a"), keys("b")]);
        fake.live
            .borrow_mut()
            .push(("server".into(), "10.8.1.1/32".into()));
        let alice = issue(&dir, "alice");
        let result = add(&alice, &fake).unwrap();
        assert!(result.contains("Address = 10.8.1.2/32, fd42:6e70:6177:2::a08:102/128"));
        assert!(result.contains("Endpoint = 203.0.113.10:51820\nAllowedIPs = 0.0.0.0/0, ::/0"));
        assert!(result.contains("DisableCookies = off\nRekeyAfterTime = 102-130\n\n[Peer]"));
        assert!(result.contains("\nvpn://"));
        assert!(
            !result.contains("RekeyTimeout"),
            "unset parameters are left out"
        );
        let saved = dir.join("clients/msk1-alice.txt");
        assert_eq!(fs::read_to_string(&saved).unwrap(), result);
        assert_eq!(
            fs::metadata(&saved).unwrap().permissions().mode() & 0o777,
            0o600
        );
        let config = fs::read_to_string(dir.join("wg0.conf")).unwrap();
        assert!(config.ends_with("\n# msk1-alice\n[Peer]\nPublicKey = apublic\nPresharedKey = apsk\nAllowedIPs = 10.8.1.2/32, fd42:6e70:6177:2::a08:102/128\n"));
        // A repeated add returns the saved config without new keys.
        assert_eq!(add(&alice, &fake).unwrap(), result);
        let bob = add(&issue(&dir, "bob"), &fake).unwrap();
        assert!(bob.contains("Address = 10.8.1.3/32"));
        assert_eq!(fs::read_dir(dir.join("clients")).unwrap().count(), 2);
    }

    #[test]
    fn a_revoked_peer_comes_back_with_its_identity() {
        let dir = test_dir("awg-restore");
        fs::write(dir.join("wg0.conf"), SERVER).unwrap();
        let fake = Fake::default();
        fake.keys.borrow_mut().push(keys("a"));
        let alice = issue(&dir, "alice");
        let first = add(&alice, &fake).unwrap();
        struct Remover<'a>(&'a Fake);
        impl super::super::peer_sync::Interface for Remover<'_> {
            fn remove(&self, key: &str) -> Result<(), String> {
                self.0.remove_peer(key)
            }
        }
        super::super::peer_sync::revoke(
            &alice.config,
            &alice.clients,
            &alice.name,
            &Remover(&fake),
        )
        .unwrap();
        assert!(
            !fs::read_to_string(&alice.config)
                .unwrap()
                .contains("# msk1-alice")
        );
        assert_eq!(add(&alice, &fake).unwrap(), first);
        let config = fs::read_to_string(&alice.config).unwrap();
        assert_eq!(config.matches("# msk1-alice").count(), 1);
        assert!(
            fake.log
                .borrow()
                .last()
                .unwrap()
                .starts_with("set apublic apsk 10.8.1.2/32,")
        );
    }

    #[test]
    fn a_peer_without_a_saved_config_is_replaced() {
        let dir = test_dir("awg-orphan");
        let orphan = format!(
            "{SERVER}\n# msk1-alice\n[Peer]\nPublicKey = stale\nAllowedIPs = 10.8.1.2/32\n\n# msk1-bob\n[Peer]\nPublicKey = bobkey\nAllowedIPs = 10.8.1.3/32\n"
        );
        fs::write(dir.join("wg0.conf"), &orphan).unwrap();
        let fake = Fake::default();
        fake.keys.borrow_mut().push(keys("a"));
        let result = add(&issue(&dir, "alice"), &fake).unwrap();
        assert_eq!(fake.log.borrow()[0], "remove stale");
        assert!(result.contains("Address = 10.8.1.2/32"));
        let config = fs::read_to_string(dir.join("wg0.conf")).unwrap();
        assert_eq!(config.matches("# msk1-alice").count(), 1);
        assert!(config.contains("# msk1-bob") && !config.contains("stale"));
    }

    #[test]
    fn archives_get_the_managed_ipv6_address() {
        assert_eq!(
            migrate_peer("# a\n[Peer]\nPublicKey = k\nAllowedIPs = 10.8.1.2/32\n").unwrap(),
            "# a\n[Peer]\nPublicKey = k\nAllowedIPs = 10.8.1.2/32, fd42:6e70:6177:2::a08:102/128\n"
        );
    }
}
