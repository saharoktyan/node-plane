//! A stored AWG client config rebuilt from the live server config, keeping
//! the peer's keys: new obfuscation parameters, endpoint and server key.
//! Replaces `refresh-awg-config.py`.
use std::fs;

use tonic::Status;

use super::Node;
use super::addresses::{capture_routes, dual_addresses};
use super::client::{LEGACY_FIELDS, all_fields, parse, vpn_key};
use super::peer_issue::{NodeRuntime, Runtime};
use crate::agent::v1::{RefreshAwgConfigRequest, RefreshAwgConfigResponse};
use crate::state::AgentState;

/// The refreshed client config. `validate` checks an interface's
/// parameters (`awg_profile.py validate` of a file holding it).
fn refresh(
    old: &str,
    server: &str,
    endpoint: &str,
    server_key: &str,
    validate: &dyn Fn(&str) -> Result<(), String>,
) -> Result<String, String> {
    let mut client = parse(old);
    let current = parse(server);
    let (interface, peer) = (&mut client.interface, &mut client.peer);
    let reusable = ["PrivateKey", "PublicKey", "Address"]
        .iter()
        .all(|key| interface.value(key).is_some())
        && peer.value("PresharedKey").is_some();
    if !reusable {
        return Err("Stored AWG config does not contain reusable peer credentials".into());
    }
    if endpoint.starts_with(':') || server_key.is_empty() {
        return Err("AWG server endpoint or public key is missing".into());
    }
    for key in all_fields() {
        match current.interface.value(key) {
            Some(value) => interface.set(key, value),
            None if LEGACY_FIELDS.contains(&key) => {
                return Err(format!("AWG server config is missing {key}"));
            }
            None => interface.remove(key),
        }
    }
    let rendered = |interface: &super::client::Section| {
        std::iter::once("[Interface]".to_string())
            .chain(interface.lines())
            .collect::<Vec<_>>()
            .join("\n")
            + "\n"
    };
    validate(&rendered(interface))?;
    let address = dual_addresses(interface.get("Address").unwrap_or(""))?;
    interface.set("Address", &address);
    let routes = capture_routes(peer.get("AllowedIPs").unwrap_or("0.0.0.0/0"))?;
    peer.set("AllowedIPs", &routes);
    peer.set("PublicKey", server_key);
    peer.set("Endpoint", endpoint);
    let lines: Vec<String> = std::iter::once("[Interface]".to_string())
        .chain(interface.lines())
        .chain(["".to_string(), "[Peer]".to_string()])
        .chain(peer.lines())
        .collect();
    Ok(lines.join("\n") + "\n")
}

impl AgentState {
    pub(crate) fn refresh_awg_config(
        &self,
        request: RefreshAwgConfigRequest,
    ) -> Result<RefreshAwgConfigResponse, Status> {
        let failed =
            |err: String| Status::failed_precondition(format!("AWG refresh failed: {err}"));
        let setting = |key: &str, default: &str| self.node_env_value(key, default);
        let node = Node::read(&setting);
        let runtime = NodeRuntime {
            node: &node,
            profile_tool: setting("AWG_PROFILE_TOOL", "/opt/node-plane-runtime/awg_profile.py"),
        };
        let server = fs::read_to_string(&node.config).map_err(|err| failed(err.to_string()))?;
        runtime.validate(&node.config).map_err(failed)?;
        // Interfaces hold private keys: check them in the private clients dir.
        let validate = |text: &str| {
            fs::create_dir_all(&node.clients).map_err(|err| err.to_string())?;
            let staged = node
                .clients
                .join(format!(".refresh-{}.conf", std::process::id()));
            fs::write(&staged, text).map_err(|err| err.to_string())?;
            let result = runtime.validate(&staged);
            let _ = fs::remove_file(&staged);
            result
        };
        let server_key = node
            .wg_show("public-key")
            .unwrap_or_default()
            .trim()
            .to_string();
        let endpoint = format!(
            "{}:{}",
            setting("AWG_SERVER_IP", ""),
            setting("AWG_SERVER_PORT", "51820")
        );
        let refreshed = refresh(&request.wg_conf, &server, &endpoint, &server_key, &validate)
            .map_err(failed)?;
        let client_key = parse(&refreshed)
            .interface
            .get("PublicKey")
            .unwrap_or("")
            .to_string();
        if !server.contains(&format!("PublicKey = {client_key}")) {
            return Err(failed("Stored AWG peer no longer exists on server".into()));
        }
        let description = match request.profile_name.trim() {
            "" => "AmneziaWG",
            name => name,
        };
        let key = vpn_key(&refreshed, description).map_err(failed)?;
        Ok(RefreshAwgConfigResponse {
            wg_conf: refreshed,
            vpn_key: key,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SERVER: &str = "[Interface]\nPrivateKey = server\nJc = 6\nJmin = 40\nJmax = 146\nS1 = 31\nS2 = 68\nS3 = 44\nS4 = 25\nH1 = 1\nH2 = 2\nH3 = 3\nH4 = 4\nI1 = <r 2>\nI2 = <r 1>\nI3 = <r 1>\nI4 = <r 1>\nI5 = <r 1>\nHeaderProtectionKey = new\nRandomTrailers = on\nDisableCookies = off\n";
    const OLD: &str = "[Interface]\nPrivateKey = private\nPublicKey = public\nAddress = 10.8.1.2/32\nDNS = 1.1.1.1\nJc = 4\nI1 = <r 1>\nRekeyTimeout = 4-8\n\n[Peer]\nPublicKey = oldserver\nPresharedKey = psk\nEndpoint = old.example:51820\nAllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 25\n";

    #[test]
    fn the_peer_keeps_its_keys_and_takes_the_server_parameters() {
        let checked = std::cell::RefCell::new(Vec::new());
        let validate = |text: &str| {
            checked.borrow_mut().push(text.to_string());
            Ok(())
        };
        let refreshed = refresh(OLD, SERVER, "new.example:443", "serverkey", &validate).unwrap();
        let conf = parse(&refreshed);
        assert_eq!(conf.interface.get("PrivateKey"), Some("private"));
        assert_eq!(conf.interface.get("Jc"), Some("6"));
        assert_eq!(conf.interface.get("I1"), Some("<r 2>"));
        assert_eq!(
            conf.interface.get("RekeyTimeout"),
            None,
            "dropped by the server"
        );
        assert_eq!(
            conf.interface.get("Address"),
            Some("10.8.1.2/32, fd42:6e70:6177:2::a08:102/128")
        );
        assert_eq!(conf.peer.get("Endpoint"), Some("new.example:443"));
        assert_eq!(conf.peer.get("PublicKey"), Some("serverkey"));
        assert_eq!(conf.peer.get("AllowedIPs"), Some("0.0.0.0/0, ::/0"));
        assert!(refreshed.starts_with("[Interface]\nPrivateKey = private\nPublicKey = public\n"));
        assert!(refreshed.contains("\n\n[Peer]\n"));
        assert!(checked.borrow()[0].starts_with("[Interface]\nPrivateKey = private"));
    }

    #[test]
    fn missing_credentials_or_server_parameters_are_refused() {
        let ok = |_: &str| Ok(());
        let no_psk = OLD.replace("PresharedKey = psk\n", "");
        assert!(refresh(&no_psk, SERVER, "h:1", "k", &ok).is_err());
        assert!(refresh(OLD, SERVER, "h:1", "", &ok).is_err());
        assert!(refresh(OLD, &SERVER.replace("Jc = 6\n", ""), "h:1", "k", &ok).is_err());
        let invalid = |_: &str| Err("invalid".to_string());
        assert!(refresh(OLD, SERVER, "h:1", "k", &invalid).is_err());
    }
}
