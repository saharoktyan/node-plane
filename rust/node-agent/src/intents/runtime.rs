//! The VPN runtime the node commands manage: where node.env puts it, the
//! containers it owns, the ports they hold, and removal of a protocol.
//! Replaces those parts of `backend-node-operation.py`.
use std::collections::BTreeSet;
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::process::Command;

use serde_json::Value;

use super::leases::Host;
use super::scheduler;

/// The runtime of this node, as the node commands see it. Tests point
/// every path elsewhere.
pub(crate) struct Runtime {
    pub(crate) root: PathBuf,
    pub(crate) env: PathBuf,
    pub(crate) units: PathBuf,
    /// The IPv6 router advertisement policy the AWG deployment writes.
    pub(crate) ra_policy: PathBuf,
    pub(crate) instance: String,
}

impl Runtime {
    pub(crate) fn system(root: PathBuf, env: PathBuf, instance: &str) -> Self {
        Self {
            root,
            env,
            units: scheduler::UNITS.into(),
            ra_policy: "/etc/sysctl.d/70-node-plane-ipv6.conf".into(),
            instance: instance.into(),
        }
    }
}

/// `source ENV; printf` of node.env variables, as the entry points see them.
pub(crate) fn sourced(env: &Path, names: &[(&str, &str)]) -> Result<Vec<String>, String> {
    let script = names.iter().fold(
        String::from("source \"$1\""),
        |mut script, (name, default)| {
            script.push_str(&format!("; printf '%s\\n' \"${{{name}:-{default}}}\""));
            script
        },
    );
    let output = Command::new("timeout")
        .args(["30s", "bash", "-c", &script, "node-settings"])
        .arg(env)
        .output()
        .map_err(|err| format!("bash: {err}"))?;
    if !output.status.success() {
        return Err("invalid node environment".into());
    }
    let values: Vec<String> = String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(str::to_string)
        .collect();
    if values.len() != names.len() {
        return Err("invalid node environment".into());
    }
    Ok(values)
}

/// The protocol configs and containers node.env names.
pub(crate) struct Paths {
    pub(crate) xray: PathBuf,
    pub(crate) awg: PathBuf,
    pub(crate) xray_container: String,
    pub(crate) awg_container: String,
}

impl Paths {
    pub(crate) fn read(env: &Path) -> Result<Self, String> {
        let [xray, awg, xray_container, awg_container]: [String; 4] = sourced(
            env,
            &[
                ("XRAY_CONFIG", "/opt/node-plane-runtime/xray/config.json"),
                (
                    "AWG_CONFIG",
                    "/opt/node-plane-runtime/amnezia-awg/data/wg0.conf",
                ),
                ("XRAY_CONTAINER_NAME", "node-plane-xray"),
                ("AWG_CONTAINER_NAME", "node-plane-awg"),
            ],
        )?
        .try_into()
        .map_err(|_| "invalid node environment")?;
        Ok(Self {
            xray: xray.into(),
            awg: awg.into(),
            xray_container,
            awg_container,
        })
    }

    /// The protocol's config, container and config mount.
    fn of(&self, protocol: &str) -> (&Path, &str, &'static str) {
        if protocol == "xray" {
            (&self.xray, &self.xray_container, "/etc/xray")
        } else {
            (&self.awg, &self.awg_container, "/opt/amnezia/awg")
        }
    }

    pub(crate) fn container(&self, protocol: &str) -> &str {
        self.of(protocol).1
    }
}

fn parent(path: &Path) -> &Path {
    path.parent().unwrap_or(Path::new("/"))
}

fn bind_mount(record: &Value, source: &Path, destination: &str) -> bool {
    record["Mounts"].as_array().is_some_and(|mounts| {
        mounts.iter().any(|mount| {
            mount["Type"] == "bind"
                && mount["Source"].as_str() == Some(&*source.to_string_lossy())
                && mount["Destination"] == destination
        })
    })
}

/// Every container record, or none.
fn records(host: &dyn Host) -> Result<Vec<Value>, String> {
    let ids = host.check(&["docker", "ps", "-aq"], Some(30))?;
    let ids: Vec<&str> = ids.split_whitespace().collect();
    if ids.is_empty() {
        return Ok(Vec::new());
    }
    let mut args = vec!["docker", "container", "inspect"];
    args.extend(&ids);
    let records: Value = serde_json::from_str(&host.check(&args, Some(30))?)
        .map_err(|err| format!("docker inspect: {err}"))?;
    Ok(records.as_array().cloned().unwrap_or_default())
}

/// `<name>` or `<name>-previous-<digits>`: a protocol container, including
/// one a crashed deployment left.
fn protocol_name(name: &str, configured: &str) -> bool {
    name == configured
        || name
            .strip_prefix(configured)
            .and_then(|rest| rest.strip_prefix("-previous-"))
            .is_some_and(|digits| !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()))
}

/// The IDs of the protocols' containers, checked by name, config mount and
/// ID; a container of the name without our mount refuses the whole plan.
pub(crate) fn owned_containers(
    host: &dyn Host,
    paths: &Paths,
    protocols: &[&str],
) -> Result<Vec<String>, String> {
    let mut owned: Vec<String> = Vec::new();
    for record in records(host)? {
        let name = record["Name"]
            .as_str()
            .unwrap_or_default()
            .trim_start_matches('/');
        for protocol in ["xray", "awg"] {
            if !protocols.contains(&protocol) {
                continue;
            }
            let (config, container, destination) = paths.of(protocol);
            if !protocol_name(name, container) {
                continue;
            }
            if !bind_mount(&record, parent(config), destination) {
                return Err(format!("container ownership could not be verified: {name}"));
            }
            let id = record["Id"].as_str().unwrap_or_default();
            if id.len() != 64 || !id.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) {
                return Err("invalid container identity".into());
            }
            if !owned.iter().any(|known| known == id) {
                owned.push(id.into());
            }
        }
    }
    Ok(owned)
}

/// Python's non-strict `Path.resolve()`: symlinks of the existing part
/// resolved, the rest appended.
fn resolve(path: &Path) -> PathBuf {
    let mut existing = path.to_path_buf();
    let mut rest: Vec<Component<'_>> = Vec::new();
    let mut parts = path.components();
    loop {
        if let Ok(real) = existing.canonicalize() {
            let mut resolved = real;
            for part in rest.iter().rev() {
                match part {
                    Component::ParentDir => {
                        resolved.pop();
                    }
                    Component::Normal(name) => resolved.push(name),
                    _ => {}
                }
            }
            return resolved;
        }
        match parts.next_back() {
            Some(component) => rest.push(component),
            None => return path.to_path_buf(),
        }
        if !existing.pop() {
            return path.to_path_buf();
        }
    }
}

/// Removes the protocols' containers, network and directories; never the
/// agent's identity, journal or entry points. The whole plan is checked
/// before the first change.
pub(crate) fn remove_protocols(
    host: &dyn Host,
    runtime: &Runtime,
    protocols: &[&str],
) -> Result<(), String> {
    if protocols.iter().any(|p| !matches!(*p, "xray" | "awg")) {
        return Err("invalid cleanup protocols".into());
    }
    let paths = Paths::read(&runtime.env)?;
    if runtime.root.ancestors().any(Path::is_symlink) {
        return Err("runtime cleanup root must not contain symlinks".into());
    }
    let root = resolve(&runtime.root);
    let mut plan: Vec<(PathBuf, PathBuf)> = Vec::new();
    if protocols.contains(&"xray") {
        plan.push((parent(&paths.xray).into(), root.join("xray")));
    }
    if protocols.contains(&"awg") {
        plan.push((parent(parent(&paths.awg)).into(), root.join("amnezia-awg")));
        plan.push((runtime.root.join("awg-clients"), root.join("awg-clients")));
    }
    for (directory, owned) in &plan {
        if directory.is_symlink() || resolve(directory) != *owned {
            return Err("runtime cleanup path is outside managed directories".into());
        }
    }
    let containers = owned_containers(host, &paths, protocols)?;
    scheduler::stop(host, &runtime.units, protocols)?;
    for id in &containers {
        host.check(&["docker", "rm", "-f", id], Some(1200))?;
    }
    if protocols.contains(&"awg") {
        let network = host.run(
            &["docker", "network", "inspect", "node-plane-awg"],
            Some(30),
        )?;
        if network.code == Some(0) {
            let records: Value = serde_json::from_str(&network.stdout).unwrap_or_default();
            if let [record] = records.as_array().map(Vec::as_slice).unwrap_or_default()
                && record["Driver"] == "bridge"
                && record["Labels"]["org.node-plane.network"] == "awg"
            {
                // Docker refuses while another container uses the network.
                host.check(&["docker", "network", "rm", "node-plane-awg"], Some(1200))?;
            }
        }
        if runtime.ra_policy.is_file()
            && fs::read_to_string(&runtime.ra_policy)
                .is_ok_and(|text| text.starts_with("# Managed by Node Plane:"))
        {
            fs::remove_file(&runtime.ra_policy)
                .map_err(|err| format!("{}: {err}", runtime.ra_policy.display()))?;
        }
    }
    for (directory, _) in &plan {
        if directory.exists() {
            fs::remove_dir_all(directory)
                .map_err(|err| format!("{}: {err}", directory.display()))?;
        }
    }
    Ok(())
}

/// The ports running protocol containers with our config mounts hold: the
/// Xray inbounds, and AWG's actual publications.
pub(crate) fn managed_ports(host: &dyn Host, env: &Path) -> BTreeSet<(&'static str, u16)> {
    let mut owned = BTreeSet::new();
    let (Ok(paths), Ok(records)) = (Paths::read(env), records(host)) else {
        return owned;
    };
    for record in records {
        if record["State"]["Running"] != true {
            continue;
        }
        let name = record["Name"]
            .as_str()
            .unwrap_or_default()
            .trim_start_matches('/');
        let protocol = if name == paths.xray_container {
            "xray"
        } else if name == paths.awg_container {
            "awg"
        } else {
            continue;
        };
        let (config, _, destination) = paths.of(protocol);
        if !bind_mount(&record, parent(config), destination) {
            continue;
        }
        if protocol == "xray" {
            let Ok(data) = fs::read_to_string(config)
                .map_err(drop)
                .and_then(|raw| serde_json::from_str::<Value>(&raw).map_err(drop))
            else {
                continue;
            };
            for inbound in data["inbounds"].as_array().into_iter().flatten() {
                if inbound["protocol"] == "vless"
                    && let Some(port) = inbound["port"].as_u64().and_then(|p| u16::try_from(p).ok())
                {
                    owned.insert(("tcp", port));
                }
            }
            continue;
        }
        let mut mappings = record["HostConfig"]["PortBindings"]
            .as_object()
            .cloned()
            .unwrap_or_default();
        for (key, bindings) in record["NetworkSettings"]["Ports"]
            .as_object()
            .into_iter()
            .flatten()
        {
            if bindings.as_array().is_some_and(|items| !items.is_empty()) {
                mappings.insert(key.clone(), bindings.clone());
            }
        }
        for (key, bindings) in &mappings {
            if !key.ends_with("/udp") {
                continue;
            }
            for binding in bindings.as_array().into_iter().flatten() {
                if let Some(port) = binding["HostPort"].as_str().and_then(|p| p.parse().ok()) {
                    owned.insert(("udp", port));
                }
            }
        }
        if record["HostConfig"]["NetworkMode"] == "host"
            && let Ok(text) = fs::read_to_string(config)
            && let Some(port) = listen_port(&text)
        {
            owned.insert(("udp", port));
        }
    }
    owned
}

/// The `ListenPort` of an AWG config.
pub(crate) fn listen_port(config: &str) -> Option<u16> {
    config.lines().find_map(|line| {
        let (key, value) = line.split_once('=')?;
        (key.trim_start() == key && key.trim_end() == "ListenPort")
            .then(|| value.trim().parse().ok())
            .flatten()
    })
}

/// The first word of a shell value, as `shlex.split` reads it.
fn first_word(value: &str) -> Option<String> {
    let mut word = String::new();
    let mut chars = value.trim_start().chars();
    let mut quote: Option<char> = None;
    while let Some(c) = chars.next() {
        match (quote, c) {
            (None, c) if c.is_whitespace() => break,
            (None, '\'' | '"') => quote = Some(c),
            (Some(open), c) if c == open => quote = None,
            (None | Some('"'), '\\') => word.push(chars.next()?),
            (_, c) => word.push(c),
        }
    }
    quote.is_none().then_some(word)
}

/// SERVER_KEY of node.env, read without running it: node.env may belong to
/// another installation.
pub(crate) fn owner(env: &Path) -> Result<String, String> {
    let raw = fs::read_to_string(env).map_err(|err| format!("{}: {err}", env.display()))?;
    let mut owner = String::new();
    for line in raw.lines() {
        if let Some((name, value)) = line.trim().split_once('=')
            && name == "SERVER_KEY"
        {
            owner = first_word(value).unwrap_or_default();
        }
    }
    Ok(owner)
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::rc::Rc;

    use serde_json::json;

    use super::*;
    use crate::intents::leases::fake::Scripted;
    use crate::protocols::test_dir;

    fn runtime(root: &Path) -> Runtime {
        Runtime {
            root: root.into(),
            env: root.join("node.env"),
            units: root.join("units"),
            ra_policy: root.join("ipv6-ra.conf"),
            instance: "agent".into(),
        }
    }

    fn write_env(root: &Path, xray: &Path, awg: &Path) {
        fs::write(
            root.join("node.env"),
            format!(
                "SERVER_KEY=n1\nXRAY_CONFIG={}\nAWG_CONFIG={}\n",
                xray.display(),
                awg.display()
            ),
        )
        .unwrap();
    }

    /// A Docker host holding `records`.
    fn docker(records: Value) -> Scripted {
        Scripted::new(move |args| match args {
            ["docker", "ps", ..] => {
                let ids: Vec<String> = records
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|r| r["Id"].as_str().unwrap_or("x").to_string())
                    .collect();
                (0, ids.join("\n"))
            }
            ["docker", "container", "inspect", ..] => (0, records.to_string()),
            ["docker", "network", "inspect", ..] => (1, String::new()),
            _ => (0, String::new()),
        })
    }

    fn layout(name: &str) -> PathBuf {
        let root = test_dir(name);
        for folder in ["xray", "amnezia-awg/data", "awg-clients"] {
            fs::create_dir_all(root.join(folder)).unwrap();
            fs::write(root.join(folder).join("config"), "owned config").unwrap();
        }
        fs::write(root.join("agent-journal"), "keep").unwrap();
        fs::write(root.join("helper.sh"), "keep").unwrap();
        write_env(
            &root,
            &root.join("xray/config.json"),
            &root.join("amnezia-awg/data/wg0.conf"),
        );
        root
    }

    #[test]
    fn a_selective_cleanup_keeps_the_other_protocol_and_the_agent() {
        for removed in ["awg", "xray"] {
            let root = layout(&format!("cleanup-{removed}"));
            let host = docker(json!([
                {"Name": "/node-plane-xray", "Id": "a".repeat(64), "Mounts": [{"Type": "bind",
                    "Source": root.join("xray").to_str().unwrap(), "Destination": "/etc/xray"}]},
                {"Name": "/node-plane-awg", "Id": "b".repeat(64), "Mounts": [{"Type": "bind",
                    "Source": root.join("amnezia-awg/data").to_str().unwrap(), "Destination": "/opt/amnezia/awg"}]},
                {"Name": "/unrelated", "Id": "c".repeat(64), "Mounts": []},
            ]));
            remove_protocols(&host, &runtime(&root), &[removed]).unwrap();
            let deleted: Vec<String> = host
                .ran(&["docker", "rm", "-f"])
                .into_iter()
                .map(|call| call[3].clone())
                .collect();
            assert_eq!(
                deleted,
                [if removed == "awg" { "b" } else { "a" }.repeat(64)]
            );
            assert_eq!(root.join("xray").exists(), removed != "xray");
            assert_eq!(root.join("amnezia-awg").exists(), removed != "awg");
            assert_eq!(root.join("awg-clients").exists(), removed != "awg");
            assert!(root.join("agent-journal").exists() && root.join("helper.sh").exists());
        }
    }

    #[test]
    fn previous_containers_are_planned_and_an_unowned_one_refuses_everything() {
        let root = layout("cleanup-previous");
        let paths = Paths::read(&root.join("node.env")).unwrap();
        let mount = json!([{"Type": "bind", "Source": root.join("xray").to_str().unwrap(), "Destination": "/etc/xray"}]);
        let host = docker(json!([
            {"Name": "/node-plane-xray", "Id": "a".repeat(64), "Mounts": mount},
            {"Name": "/node-plane-xray-previous-123", "Id": "b".repeat(64), "Mounts": mount},
            {"Name": "/node-plane-xray-previous-x", "Id": "d".repeat(64), "Mounts": []},
            {"Name": "/unrelated", "Id": "c".repeat(64), "Mounts": []},
        ]));
        assert_eq!(
            owned_containers(&host, &paths, &["xray", "awg"]).unwrap(),
            ["a".repeat(64), "b".repeat(64)]
        );
        let host = docker(json!([
            {"Name": "/node-plane-xray", "Id": "a".repeat(64), "Mounts": mount},
            {"Name": "/node-plane-awg", "Id": "b".repeat(64), "Mounts": []},
        ]));
        assert!(
            remove_protocols(&host, &runtime(&root), &["xray", "awg"])
                .unwrap_err()
                .contains("ownership")
        );
        assert!(host.ran(&["docker", "rm"]).is_empty());
        assert_eq!(
            fs::read_to_string(root.join("xray/config")).unwrap(),
            "owned config"
        );
    }

    #[test]
    fn a_path_outside_the_runtime_refuses_before_any_change() {
        // An AWG config outside the runtime.
        let root = layout("cleanup-outside");
        write_env(
            &root,
            &root.join("xray/config.json"),
            &root.join("unrelated/data/wg0.conf"),
        );
        let host = docker(json!([]));
        assert!(remove_protocols(&host, &runtime(&root), &["xray", "awg"]).is_err());
        assert!(host.calls.borrow().is_empty());
        assert!(root.join("xray/config").exists());
        // Symlinked clients.
        let root = layout("cleanup-symlink");
        let external = root.join("external");
        fs::create_dir(&external).unwrap();
        fs::write(external.join("private.conf"), "keep").unwrap();
        fs::remove_dir_all(root.join("awg-clients")).unwrap();
        std::os::unix::fs::symlink(&external, root.join("awg-clients")).unwrap();
        assert!(remove_protocols(&host, &runtime(&root), &["awg"]).is_err());
        assert!(external.join("private.conf").exists());
        // A symlinked runtime root.
        let base = test_dir("cleanup-alias");
        let real = layout("cleanup-alias-real");
        std::os::unix::fs::symlink(&real, base.join("runtime")).unwrap();
        let alias = base.join("runtime");
        write_env(
            &real,
            &alias.join("xray/config.json"),
            &alias.join("amnezia-awg/data/wg0.conf"),
        );
        let mut aliased = runtime(&alias);
        aliased.env = real.join("node.env");
        assert!(remove_protocols(&host, &aliased, &["xray"]).is_err());
        assert!(real.join("xray/config").exists());
        assert!(host.calls.borrow().is_empty());
    }

    #[test]
    fn only_our_router_advertisement_policy_and_awg_network_are_removed() {
        let root = layout("cleanup-policy");
        let runtime = runtime(&root);
        fs::write(&runtime.ra_policy, "administrator-owned settings\n").unwrap();
        let network = Rc::new(RefCell::new(
            json!([{"Driver": "bridge", "Labels": {"org.node-plane.network": "awg"}}]),
        ));
        let shared = network.clone();
        let host = Scripted::new(move |args| match args {
            ["docker", "network", "inspect", ..] => (0, shared.borrow().to_string()),
            _ => (0, String::new()),
        });
        remove_protocols(&host, &runtime, &["awg"]).unwrap();
        assert_eq!(
            fs::read_to_string(&runtime.ra_policy).unwrap(),
            "administrator-owned settings\n"
        );
        assert_eq!(host.ran(&["docker", "network", "rm"]).len(), 1);
        fs::write(
            &runtime.ra_policy,
            "# Managed by Node Plane: IPv6 router advertisements\n",
        )
        .unwrap();
        *network.borrow_mut() = json!([{"Driver": "bridge", "Labels": {}}]);
        remove_protocols(&host, &runtime, &["awg"]).unwrap();
        assert!(!runtime.ra_policy.exists());
        assert_eq!(host.ran(&["docker", "network", "rm"]).len(), 1);
    }

    #[test]
    fn managed_ports_need_a_running_container_with_our_mount() {
        let root = test_dir("managed-ports");
        let (xray, awg) = (root.join("xray/config.json"), root.join("awg/wg0.conf"));
        fs::create_dir_all(xray.parent().unwrap()).unwrap();
        fs::create_dir_all(awg.parent().unwrap()).unwrap();
        fs::write(&xray, json!({"inbounds": [{"protocol": "vless", "port": 443}, {"protocol": "socks", "port": 1080}]}).to_string()).unwrap();
        fs::write(&awg, "[Interface]\nListenPort = 51820\n").unwrap();
        fs::write(
            root.join("node.env"),
            format!(
                "XRAY_CONFIG={}\nAWG_CONFIG={}\n",
                xray.display(),
                awg.display()
            ),
        )
        .unwrap();
        let records = |running: bool, mounted: bool| {
            let mount = |source: &Path, destination: &str| {
                if mounted {
                    json!([{"Type": "bind", "Source": source.to_str().unwrap(), "Destination": destination}])
                } else {
                    json!([])
                }
            };
            json!([
                {"Name": "/node-plane-xray", "Id": "a", "State": {"Running": true}, "Mounts": mount(xray.parent().unwrap(), "/etc/xray")},
                {"Name": "/node-plane-awg", "Id": "b", "State": {"Running": running},
                 "Mounts": mount(awg.parent().unwrap(), "/opt/amnezia/awg"),
                 "HostConfig": {"NetworkMode": "host"},
                 "NetworkSettings": {"Ports": {"443/udp": [{"HostPort": "8443"}]}}},
            ])
        };
        let ports = managed_ports(&docker(records(true, true)), &root.join("node.env"));
        assert_eq!(
            ports,
            BTreeSet::from([("tcp", 443), ("udp", 8443), ("udp", 51820)])
        );
        assert!(managed_ports(&docker(records(false, false)), &root.join("node.env")).is_empty());
    }

    #[test]
    fn the_owner_is_read_without_running_node_env() {
        let root = test_dir("runtime-owner");
        let env = root.join("node.env");
        let marker = root.join("ran");
        fs::write(
            &env,
            format!(
                "XRAY_CONFIG=/x\nSERVER_KEY='ger2'\nAWG=$(touch {})\n",
                marker.display()
            ),
        )
        .unwrap();
        assert_eq!(owner(&env).unwrap(), "ger2");
        fs::write(&env, "XRAY_CONFIG=/x\n").unwrap();
        assert_eq!(owner(&env).unwrap(), "");
        fs::write(&env, "SERVER_KEY=\"a b\"\nSERVER_KEY=c\\ d e\n").unwrap();
        assert_eq!(owner(&env).unwrap(), "c d");
        assert!(!marker.exists());
        assert_eq!(listen_port("[Interface]\nListenPort = 5300\n"), Some(5300));
        assert_eq!(listen_port(" ListenPort = 5300\n"), None);
    }
}
