use std::cell::RefCell;

use super::*;

/// Records what the relay asked of the host.
#[derive(Default)]
struct Fake {
    calls: RefCell<Vec<String>>,
    start_fails: bool,
}

impl Host for Fake {
    fn user(&self) -> Result<(u32, u32), String> {
        // The test's own ids: chown to them needs no privileges.
        Ok((unsafe { libc::getuid() }, unsafe { libc::getgid() }))
    }
    fn systemctl(&self, args: &[&str]) -> Result<bool, String> {
        self.calls.borrow_mut().push(args.join(" "));
        Ok(!(self.start_fails && matches!(args[0], "start" | "restart")))
    }
    fn firewall(&self, port: u16, allow: bool) {
        self.calls.borrow_mut().push(format!(
            "ufw {} {port}",
            if allow { "allow" } else { "delete" }
        ));
    }
}

fn paths(name: &str) -> Paths {
    let dir = std::env::temp_dir().join(format!("relay-agent-{}-{name}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    Paths {
        unit: dir.join("node-plane-relay.service"),
        env: dir.join("relay.env"),
        state: dir.join("state"),
        version: dir.join("relay.version"),
    }
}

fn request(tls: &str, port: u32) -> ApplyRelayRequest {
    ApplyRelayRequest {
        domain: "Cab.Example.com".into(),
        port,
        tls: tls.into(),
        acme_contact: "ops@example.com".into(),
        certificate_path: "/etc/ssl/cab.pem".into(),
        key_path: "/etc/ssl/cab.key".into(),
        link_key: vec![7; 32],
    }
}

#[test]
fn settings_are_checked() {
    assert!(Settings::parse(request("acme", 443)).is_ok());
    let error = |r| Settings::parse(r).unwrap_err();
    assert_eq!(error(request("acme", 8443)), "relay_acme_needs_port_443");
    assert_eq!(error(request("tls", 443)), "relay_tls_invalid");
    assert_eq!(error(request("proxy", 0)), "relay_port_invalid");
    assert_eq!(error(request("proxy", 70000)), "relay_port_invalid");
    for domain in ["localhost", "a..b", "-a.b", "a b.c", "a.b\nX=1"] {
        let mut r = request("proxy", 8443);
        r.domain = domain.into();
        assert_eq!(error(r), "relay_domain_invalid", "{domain}");
    }
    let mut r = request("files", 443);
    r.certificate_path = "cab.pem".into();
    assert_eq!(error(r), "relay_certificate_path_invalid");
    let mut r = request("files", 443);
    r.key_path = "/etc/ssl/$(x)".into();
    assert_eq!(error(r), "relay_certificate_path_invalid");
    let mut r = request("acme", 443);
    r.acme_contact = "ops@example.com\nNODE_PLANE_RELAY_TLS=proxy".into();
    assert_eq!(error(r), "relay_acme_contact_invalid");
    let mut r = request("acme", 443);
    r.link_key = vec![1; 31];
    assert_eq!(error(r), "relay_link_key_invalid");
}

#[test]
fn apply_installs_a_sandboxed_relay_with_the_pinned_key() {
    let paths = paths("apply");
    let host = Fake::default();
    let settings = Settings::parse(request("acme", 443)).unwrap();
    let state = apply(&host, &paths, &settings).unwrap();
    assert!(state.installed && state.active);
    assert_eq!(state.relay_key.len(), 32);
    assert_eq!(state.link, "disconnected");
    assert_eq!(
        fs::read_to_string(paths.state.join("link.pub")).unwrap(),
        keys::hex(&[7; 32])
    );
    let env = fs::read_to_string(&paths.env).unwrap();
    assert!(env.contains("NODE_PLANE_RELAY_LISTEN=0.0.0.0:443\n"));
    assert!(env.contains("NODE_PLANE_RELAY_DOMAIN=cab.example.com\n"));
    assert!(
        env.contains("NODE_PLANE_RELAY_TLS=acme\nNODE_PLANE_RELAY_ACME_CONTACT=ops@example.com\n")
    );
    let unit = fs::read_to_string(&paths.unit).unwrap();
    for line in [
        "User=node-plane-relay",
        "ExecStart=/usr/local/bin/node-plane-agent relay-serve",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=CAP_NET_BIND_SERVICE",
        "ProtectSystem=strict",
    ] {
        assert!(unit.contains(line), "{line}");
    }
    assert!(unit.contains(&format!("ReadWritePaths={}", paths.state.display())));
    let mode = |p: &Path| fs::metadata(p).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode(&paths.state), 0o700);
    assert_eq!(mode(&paths.state.join("relay.key")), 0o600);
    assert_eq!(mode(&paths.env), 0o600);
    assert_eq!(
        *host.calls.borrow(),
        [
            "ufw allow 443",
            "daemon-reload",
            "enable node-plane-relay.service",
            "restart node-plane-relay.service",
            "is-active --quiet node-plane-relay.service"
        ]
    );

    // The same settings with a new controller key: no restart, same relay key.
    let host = Fake::default();
    let mut moved = request("acme", 443);
    moved.link_key = vec![9; 32];
    let again = apply(&host, &paths, &Settings::parse(moved).unwrap()).unwrap();
    assert_eq!(again.relay_key, state.relay_key);
    assert!(
        host.calls
            .borrow()
            .contains(&"start node-plane-relay.service".to_string())
    );
    assert_eq!(
        fs::read_to_string(paths.state.join("link.pub")).unwrap(),
        keys::hex(&[9; 32])
    );

    // Behind a proxy: loopback only, and the public port closes again.
    let host = Fake::default();
    apply(
        &host,
        &paths,
        &Settings::parse(request("proxy", 8443)).unwrap(),
    )
    .unwrap();
    assert!(
        fs::read_to_string(&paths.env)
            .unwrap()
            .contains("NODE_PLANE_RELAY_LISTEN=127.0.0.1:8443\n")
    );
    assert_eq!(host.calls.borrow()[0], "ufw delete 443");
    assert!(
        host.calls
            .borrow()
            .contains(&"restart node-plane-relay.service".to_string())
    );

    // Removal takes the unit, settings and keys.
    let host = Fake::default();
    let removed = remove(&host, &paths).unwrap();
    assert!(!removed.installed && removed.relay_key.is_empty());
    assert!(!paths.unit.exists() && !paths.env.exists() && !paths.state.exists());
    assert_eq!(
        host.calls.borrow()[0],
        "disable --now node-plane-relay.service"
    );
    let _ = fs::remove_dir_all(paths.unit.parent().unwrap());
}

#[test]
fn a_relay_that_does_not_start_is_reported() {
    let paths = paths("fails");
    let host = Fake {
        start_fails: true,
        ..Fake::default()
    };
    let settings = Settings::parse(request("proxy", 8443)).unwrap();
    assert_eq!(
        apply(&host, &paths, &settings).unwrap_err(),
        "relay_start_failed"
    );
    let _ = fs::remove_dir_all(paths.unit.parent().unwrap());
}

#[test]
fn the_link_state_comes_from_the_relay() {
    let paths = paths("status");
    let host = Fake::default();
    apply(
        &host,
        &paths,
        &Settings::parse(request("proxy", 8443)).unwrap(),
    )
    .unwrap();
    fs::write(
        paths.state.join("status.json"),
        r#"{"link":"connected","since":1700000000}"#,
    )
    .unwrap();
    let current = state(&host, &paths);
    assert_eq!(
        (current.link.as_str(), current.link_since),
        ("connected", 1_700_000_000)
    );
    let _ = fs::remove_dir_all(paths.unit.parent().unwrap());
}

#[test]
fn an_update_restarts_the_relay_once() {
    let paths = paths("update");
    let host = Fake::default();
    apply(
        &host,
        &paths,
        &Settings::parse(request("proxy", 8443)).unwrap(),
    )
    .unwrap();
    let host = Fake::default();
    follow_update(&host, &paths);
    assert!(host.calls.borrow().is_empty());
    fs::write(&paths.version, "0.0.1").unwrap();
    follow_update(&host, &paths);
    assert_eq!(
        *host.calls.borrow(),
        ["try-restart node-plane-relay.service"]
    );
    assert_eq!(fs::read_to_string(&paths.version).unwrap(), VERSION);
    let _ = fs::remove_dir_all(paths.unit.parent().unwrap());
}
