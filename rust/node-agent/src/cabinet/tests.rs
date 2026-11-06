use std::cell::RefCell;

use super::*;
use crate::agent::v1::CabinetFile;

/// Records what the cabinet asked of the host.
#[derive(Default)]
struct Fake {
    calls: RefCell<Vec<String>>,
}

impl Host for Fake {
    fn user(&self) -> Result<(u32, u32), String> {
        Ok((unsafe { libc::getuid() }, unsafe { libc::getgid() }))
    }
    fn systemctl(&self, args: &[&str]) -> Result<bool, String> {
        self.calls.borrow_mut().push(args.join(" "));
        Ok(true)
    }
    fn firewall(&self, _port: u16, _allow: bool) {}
}

fn paths(name: &str) -> Paths {
    let dir = std::env::temp_dir().join(format!("cabinet-agent-{}-{name}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    Paths {
        root: dir.join("cabinet"),
        relay: relay::Paths {
            unit: dir.join("node-plane-relay.service"),
            env: dir.join("relay.env"),
            state: dir.join("state"),
            version: dir.join("relay.version"),
            cabinet_env: dir.join("relay-cabinet.env"),
        },
    }
}

fn request(version: &str, files: &[(&str, &str)]) -> ApplyCabinetRequest {
    ApplyCabinetRequest {
        version: version.into(),
        files: files
            .iter()
            .map(|(path, content)| CabinetFile {
                path: (*path).into(),
                content: content.as_bytes().to_vec(),
            })
            .collect(),
    }
}

fn with_relay(paths: &Paths) {
    fs::write(&paths.relay.unit, "old unit").unwrap();
}

#[test]
fn files_are_checked() {
    for (request, code) in [
        (
            request("", &[("index.html", "")]),
            "cabinet_version_invalid",
        ),
        (
            request("../x", &[("index.html", "")]),
            "cabinet_version_invalid",
        ),
        (request("1.0", &[("app.js", "")]), "cabinet_files_invalid"),
        (
            request("1.0", &[("index.html", ""), ("../x.js", "")]),
            "cabinet_files_invalid",
        ),
        (
            request("1.0", &[("index.html", ""), ("/etc/x.js", "")]),
            "cabinet_files_invalid",
        ),
        (
            request("1.0", &[("index.html", ""), ("run.sh", "")]),
            "cabinet_files_invalid",
        ),
    ] {
        assert_eq!(check(&request), Err(code.to_string()), "{request:?}");
    }
    assert_eq!(
        check(&request("1.0.0-beta.1+abc", &[("index.html", "")])),
        Ok(())
    );
}

#[test]
fn needs_the_relay() {
    let paths = paths("no-relay");
    let host = Fake::default();
    assert_eq!(
        apply(&host, &paths, &request("1.0", &[("index.html", "")])),
        Err("cabinet_needs_relay".to_string())
    );
}

#[test]
fn a_version_is_written_whole_then_served() {
    let paths = paths("apply");
    with_relay(&paths);
    let host = Fake::default();
    let first = request(
        "1.0",
        &[("index.html", "one"), ("_app/immutable/a.js", "a")],
    );
    assert_eq!(apply(&host, &paths, &first).unwrap().version, "1.0");
    let current = paths.root.join("current");
    assert_eq!(
        fs::read_to_string(current.join("index.html")).unwrap(),
        "one"
    );
    assert_eq!(
        fs::read_to_string(current.join("_app/immutable/a.js")).unwrap(),
        "a"
    );
    assert!(
        fs::read_to_string(&paths.relay.cabinet_env)
            .unwrap()
            .contains(&format!(
                "NODE_PLANE_RELAY_CABINET_DIR={}",
                current.display()
            ))
    );
    // The relay's unit now reads that file, and the relay restarts once.
    assert!(
        fs::read_to_string(&paths.relay.unit)
            .unwrap()
            .contains(&format!(
                "EnvironmentFile=-{}",
                paths.relay.cabinet_env.display()
            ))
    );
    assert_eq!(
        *host.calls.borrow(),
        ["daemon-reload", "try-restart node-plane-relay.service"]
    );
    // The same version again changes nothing.
    host.calls.borrow_mut().clear();
    apply(&host, &paths, &first).unwrap();
    assert!(host.calls.borrow().is_empty());
    // A new version replaces the old one without a restart.
    apply(&host, &paths, &request("1.1", &[("index.html", "two")])).unwrap();
    assert_eq!(
        fs::read_to_string(current.join("index.html")).unwrap(),
        "two"
    );
    assert!(!paths.root.join("1.0").exists());
    assert!(host.calls.borrow().is_empty());
    let mode = fs::metadata(current.join("index.html"))
        .unwrap()
        .permissions()
        .mode();
    assert_eq!(mode & 0o777, 0o644);
}

#[test]
fn removal_takes_the_files_and_restarts_the_relay() {
    let paths = paths("remove");
    with_relay(&paths);
    let host = Fake::default();
    apply(&host, &paths, &request("1.0", &[("index.html", "one")])).unwrap();
    host.calls.borrow_mut().clear();
    assert_eq!(remove(&host, &paths).unwrap().version, "");
    assert!(!paths.root.exists() && !paths.relay.cabinet_env.exists());
    assert_eq!(
        *host.calls.borrow(),
        ["try-restart node-plane-relay.service"]
    );
    // Again: nothing to do.
    host.calls.borrow_mut().clear();
    remove(&host, &paths).unwrap();
    assert!(host.calls.borrow().is_empty());
}
