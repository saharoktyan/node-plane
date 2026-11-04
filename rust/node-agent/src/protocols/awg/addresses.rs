//! Tunnel addresses: a stable IPv6 address for every IPv4 peer address,
//! and full-tunnel capture for both families. Replaces `awg_ipv6.py`, which
//! stays for `awg-peer-state.py` of the profile intent journal.
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

/// `fd42:6e70:6177:2::/64`; a peer's IPv4 address is its host part.
const NETWORK: u128 = 0xfd42_6e70_6177_0002_0000_0000_0000_0000;
/// The server's address on the tunnel network.
const SERVER: &str = "fd42:6e70:6177:2::1/64";

fn interface(value: &str) -> Result<(IpAddr, u8), String> {
    let (address, prefix) = value.split_once('/').unwrap_or((value, ""));
    let address: IpAddr = address
        .trim()
        .parse()
        .map_err(|_| format!("invalid address: {value}"))?;
    let maximum = if address.is_ipv4() { 32 } else { 128 };
    let prefix = if prefix.is_empty() {
        maximum
    } else {
        prefix
            .trim()
            .parse::<u8>()
            .ok()
            .filter(|prefix| *prefix <= maximum)
            .ok_or_else(|| format!("invalid prefix: {value}"))?
    };
    Ok((address, prefix))
}

/// The peer's addresses with its managed IPv6 address added.
pub(crate) fn dual_addresses(value: &str) -> Result<String, String> {
    let mut addresses: Vec<String> = value
        .split(',')
        .map(str::trim)
        .filter(|part| !part.is_empty())
        .map(str::to_string)
        .collect();
    let parsed = addresses
        .iter()
        .map(|part| interface(part))
        .collect::<Result<Vec<_>, _>>()?;
    if let Some((IpAddr::V4(ipv4), _)) = parsed.iter().find(|(address, _)| address.is_ipv4()) {
        let managed = IpAddr::V6(Ipv6Addr::from(NETWORK + u128::from(u32::from(*ipv4))));
        if !parsed.iter().any(|(address, _)| *address == managed) {
            addresses.push(format!("{managed}/128"));
        }
    }
    Ok(addresses.join(", "))
}

/// Client routes; a full IPv4 tunnel also captures IPv6.
pub(crate) fn capture_routes(value: &str) -> Result<String, String> {
    let mut routes = Vec::new();
    for part in value
        .split(',')
        .map(str::trim)
        .filter(|part| !part.is_empty())
    {
        let (address, prefix) = interface(part)?;
        // Networks only, as Python's strict ip_network.
        let host_bits = match address {
            IpAddr::V4(ip) => u32::from(ip).checked_shl(u32::from(prefix)).unwrap_or(0) != 0,
            IpAddr::V6(ip) => u128::from(ip).checked_shl(u32::from(prefix)).unwrap_or(0) != 0,
        };
        if host_bits {
            return Err(format!("{part} has host bits set"));
        }
        routes.push(format!("{address}/{prefix}"));
    }
    if routes.iter().any(|route| route == "0.0.0.0/0")
        && !routes.iter().any(|route| route == "::/0")
    {
        routes.push("::/0".into());
    }
    Ok(routes.join(", "))
}

/// `KEY = VALUE` lines of `text` with VALUE rewritten.
fn rewrite(
    text: &str,
    key: &str,
    value: impl Fn(&str) -> Result<String, String>,
) -> Result<String, String> {
    let mut lines = Vec::new();
    for line in text.split_inclusive('\n') {
        let (content, end) = line.strip_suffix('\n').map_or((line, ""), |c| (c, "\n"));
        let current = content
            .strip_prefix(key)
            .map(|rest| rest.trim_start_matches([' ', '\t']))
            .and_then(|rest| rest.strip_prefix('='))
            .map(|rest| rest.trim_start_matches([' ', '\t']))
            .filter(|rest| !rest.is_empty() && !rest.contains('\r'));
        match current {
            Some(current) => lines.push(format!("{key} = {}{end}", value(current)?)),
            None => lines.push(line.to_string()),
        }
    }
    Ok(lines.concat())
}

/// The server config with the tunnel's IPv6 addresses: the server's on its
/// interface, each peer's in its AllowedIPs.
pub(crate) fn migrate(text: &str) -> Result<String, String> {
    let server = interface(SERVER)?.0;
    let mut parts: Vec<String> = text.split("[Peer]").map(str::to_string).collect();
    parts[0] = rewrite(&parts[0], "Address", |value| {
        let mut addresses: Vec<&str> = value.split(',').map(str::trim).collect();
        let mut present = false;
        for address in &addresses {
            present |= interface(address)?.0 == server;
        }
        if !present {
            addresses.push(SERVER);
        }
        Ok(addresses.join(", "))
    })?;
    for part in &mut parts[1..] {
        *part = rewrite(part, "AllowedIPs", dual_addresses)?;
    }
    Ok(parts.join("[Peer]"))
}

/// The first free `10.8.1.x` address.
pub(crate) fn free_address(used: &[String]) -> Option<Ipv4Addr> {
    (1..=254)
        .map(|host| Ipv4Addr::new(10, 8, 1, host))
        .find(|candidate| !used.iter().any(|address| *address == candidate.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn peers_get_their_stable_ipv6_address_once() {
        assert_eq!(
            dual_addresses("10.8.1.2/32").unwrap(),
            "10.8.1.2/32, fd42:6e70:6177:2::a08:102/128"
        );
        assert_eq!(
            dual_addresses("10.8.1.2/32, fd42:6e70:6177:2::a08:102/128").unwrap(),
            "10.8.1.2/32, fd42:6e70:6177:2::a08:102/128"
        );
        assert_eq!(dual_addresses("fd00::2/128").unwrap(), "fd00::2/128");
        assert!(dual_addresses("10.8.1.x/32").is_err());
    }

    #[test]
    fn a_full_ipv4_tunnel_captures_ipv6() {
        assert_eq!(capture_routes("0.0.0.0/0").unwrap(), "0.0.0.0/0, ::/0");
        assert_eq!(
            capture_routes("0.0.0.0/0, ::/0").unwrap(),
            "0.0.0.0/0, ::/0"
        );
        assert_eq!(capture_routes("10.0.0.0/8").unwrap(), "10.0.0.0/8");
        assert_eq!(capture_routes("192.168.1.1").unwrap(), "192.168.1.1/32");
        assert!(capture_routes("10.0.0.1/8").is_err());
    }

    #[test]
    fn the_server_and_its_peers_get_their_ipv6_addresses_once() {
        let text = "[Interface]\nAddress = 10.8.1.1/24\nListenPort = 51820\n\n# a\n[Peer]\nPublicKey = k\nAllowedIPs = 10.8.1.2/32\n";
        let migrated = migrate(text).unwrap();
        assert_eq!(
            migrated,
            "[Interface]\nAddress = 10.8.1.1/24, fd42:6e70:6177:2::1/64\nListenPort = 51820\n\n# a\n[Peer]\nPublicKey = k\nAllowedIPs = 10.8.1.2/32, fd42:6e70:6177:2::a08:102/128\n"
        );
        assert_eq!(migrate(&migrated).unwrap(), migrated);
        assert!(migrate("[Interface]\nAddress = 10.8.1.x/24\n").is_err());
    }

    #[test]
    fn the_first_unused_address_is_free() {
        let used = ["10.8.1.1".to_string(), "10.8.1.2".to_string()];
        assert_eq!(free_address(&used), Some(Ipv4Addr::new(10, 8, 1, 3)));
        let all: Vec<String> = (1..=254).map(|host| format!("10.8.1.{host}")).collect();
        assert_eq!(free_address(&all), None);
    }
}
