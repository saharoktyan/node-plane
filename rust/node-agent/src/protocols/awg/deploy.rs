//! The AWG container: the image is built on the node, the config takes the
//! current parameters and tunnel addresses, and a failed start brings back
//! the previous config and container. Replaces the body of `deploy-awg.sh`.
use std::fs;
use std::os::unix::fs::FileTypeExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use super::addresses::migrate;
use super::peer_sync::atomic_write;
use super::setup::{profile, profile_tool};
use crate::protocols::Setting;
use crate::protocols::deploy::guard::Guard;
use crate::protocols::deploy::{Docker, leased, leased_deploy, prepare_leased, settle};
use crate::protocols::settings::Backup;
use crate::runtime::images::Kind;

const NETWORK: &str = "node-plane-awg";
const NETWORK_PREFIX: &str = "fd42:6e70:6177:1::/64";
const DATA: &str = "/opt/amnezia/awg";

fn stdout(program: &str, args: &[&str]) -> Option<String> {
    let output = Command::new(program)
        .args(args)
        .stderr(Stdio::null())
        .output()
        .ok()?;
    output
        .status
        .success()
        .then(|| String::from_utf8_lossy(&output.stdout).into_owned())
}

/// Whether the host routes IPv6 to the internet.
fn ipv6_uplink() -> bool {
    stdout("ip", &["-6", "route", "show", "default"]).is_some_and(|out| !out.trim().is_empty())
        && stdout("ip", &["-6", "address", "show", "scope", "global"])
            .is_some_and(|out| out.contains("inet6 "))
}

/// Docker turns on forwarding, which stops the kernel from taking router
/// advertisements on the uplink; accept_ra = 2 keeps them, also after a
/// reboot.
fn keep_router_advertisements() -> Result<(), String> {
    let routes =
        stdout("ip", &["-6", "route", "show", "default", "proto", "ra"]).unwrap_or_default();
    let uplink = routes.lines().find_map(|line| {
        let fields: Vec<&str> = line.split_whitespace().collect();
        let at = fields.iter().position(|field| *field == "dev")?;
        fields.get(at + 1).copied()
    });
    let Some(uplink) = uplink.filter(|name| {
        !name.is_empty()
            && name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b':' | b'-'))
    }) else {
        return Ok(());
    };
    let setting = format!("/proc/sys/net/ipv6/conf/{uplink}/accept_ra");
    if fs::read_to_string(&setting).unwrap_or_default().trim() != "1" {
        return Ok(());
    }
    let failed = |err: std::io::Error| format!("IPv6 router advertisements: {err}");
    fs::create_dir_all("/etc/sysctl.d").map_err(failed)?;
    fs::write(
        "/etc/sysctl.d/70-node-plane-ipv6.conf",
        format!(
            "# Managed by Node Plane: IPv6 router advertisements\n-net/ipv6/conf/{uplink}/accept_ra = 2\n"
        ),
    )
    .map_err(failed)?;
    fs::write(&setting, "2").map_err(failed)
}

/// The IPv6 bridge network of the container, created when missing.
fn network(docker: &Docker) -> Result<(), String> {
    if !docker.quiet(&["network", "inspect", NETWORK]) {
        docker.status(&[
            "network",
            "create",
            "--driver",
            "bridge",
            "--ipv6",
            "--subnet",
            NETWORK_PREFIX,
            "--label",
            "org.node-plane.network=awg",
            NETWORK,
        ])?;
    }
    let info: serde_json::Value =
        serde_json::from_str(&docker.output(&["network", "inspect", NETWORK])?)
            .map_err(|err| format!("{NETWORK}: {err}"))?;
    let network = &info[0];
    let owned = network["Driver"] == "bridge"
        && network["EnableIPv6"] == true
        && network["Labels"]["org.node-plane.network"] == "awg"
        && network["IPAM"]["Config"]
            .as_array()
            .is_some_and(|config| config.iter().any(|item| item["Subnet"] == NETWORK_PREFIX));
    owned.then_some(()).ok_or_else(|| {
        format!(
            "Existing {NETWORK} network does not match Node Plane ownership or IPv6 configuration"
        )
    })
}

/// The previous container while it is renamed, and whether the new one was
/// started: what a rollback has to undo.
#[derive(Default)]
struct Progress {
    renamed: Option<(String, String)>,
    attempted: bool,
}

/// The settings a deployment starts the container with.
struct Deployment<'a> {
    setting: Setting<'a>,
    docker: &'a Docker,
    guard: &'a Guard<'a>,
    image: String,
    container: String,
    config: PathBuf,
    data: PathBuf,
    port: String,
    /// Arguments of the container and of its diagnostic run.
    host: Vec<String>,
}

impl Deployment<'_> {
    fn run(&self, progress: &mut Progress, previous: Option<&str>) -> Result<(), String> {
        let config = self.config.to_string_lossy();
        let report = profile(
            &profile_tool(self.setting),
            &["migrate", &config, &(self.setting)("AWG_I1_PRESET", "quic")],
        )?;
        if !report.trim().is_empty() {
            println!("{}", report.trim_end());
        }
        profile(&profile_tool(self.setting), &["validate", &config])?;
        let text = fs::read_to_string(&self.config).map_err(|err| format!("{config}: {err}"))?;
        let addressed = migrate(&text)?;
        if addressed != text {
            atomic_write(&self.config, &addressed)?;
        }

        let (docker, guard, container) = (self.docker, self.guard, &self.container);
        if let Some(id) = previous {
            let renamed = format!("{container}-previous-{}", std::process::id());
            guard.validate(id, container, false)?;
            docker.status(&["rename", id, &renamed])?;
            progress.renamed = Some((id.to_string(), renamed.clone()));
            guard.validate(id, &renamed, false)?;
            docker.status(&["stop", id])?;
        }

        progress.attempted = true;
        prepare_leased("awg")?;
        let cidfile = guard.cidfile().to_string_lossy().into_owned();
        let published = format!("{0}:{0}/udp", self.port);
        let volume = format!("{}:{DATA}", self.data.display());
        let mut run: Vec<&str> = vec!["run", "-d", "--name", container, "--cidfile", &cidfile];
        let labels = guard.labels();
        run.extend(labels.iter().map(String::as_str));
        run.extend(["--restart", "no"]);
        run.extend(self.host.iter().map(String::as_str));
        run.extend(["-p", &published, "-v", &volume, &self.image]);
        docker.status(&run)?;

        settle(self.setting, 3);
        let new_id = guard
            .created()?
            .ok_or("New AWG container identity was not recorded; recovery required")?;
        let state = docker
            .output(&["inspect", "-f", "{{.State.Status}}", &new_id])
            .unwrap_or_else(|_| "unknown".into());
        if state != "running" {
            self.diagnose(&new_id, &state);
            return Err(format!("AWG container is {state}"));
        }
        if leased("awg") {
            leased_deploy("awg")?;
        } else {
            guard.validate(&new_id, container, true)?;
            docker.status(&["update", "--restart", "unless-stopped", &new_id])?;
        }
        Ok(())
    }

    /// Why the new container stopped: its state, logs, and a traced run.
    fn diagnose(&self, id: &str, state: &str) {
        let docker = self.docker;
        eprintln!("AWG container failed to start.\nstate: {state}");
        docker.report(&[
            "inspect", "-f",
            "exit_code={{.State.ExitCode}} error={{.State.Error}} oom_killed={{.State.OOMKilled}} started_at={{.State.StartedAt}} finished_at={{.State.FinishedAt}}",
            id,
        ]);
        let arch = stdout("uname", &["-m"]).unwrap_or_default();
        eprintln!(
            "host_arch: {}",
            if arch.trim().is_empty() {
                "unknown"
            } else {
                arch.trim()
            }
        );
        docker.report(&[
            "image",
            "inspect",
            &self.image,
            "-f",
            "image_os={{.Os}} image_arch={{.Architecture}}",
        ]);
        docker.report(&["logs", id]);
        eprintln!("manual_run_output:");
        let volume = format!("{}:{DATA}", self.data.display());
        let mut run: Vec<&str> = vec!["run", "--rm", "--entrypoint", "/bin/sh"];
        run.extend(self.host.iter().map(String::as_str));
        run.extend([
            "-v",
            &volume,
            &self.image,
            "-c",
            r#"
      echo "shell: $(command -v sh 2>/dev/null || echo missing)"
      ls -l /opt/amnezia/start.sh || true
      wc -c /opt/amnezia/start.sh || true
      echo "--- start.sh head ---"
      sed -n "1,120p" /opt/amnezia/start.sh || true
      echo "--- run ---"
      /bin/sh -x /opt/amnezia/start.sh
    "#,
        ]);
        docker.report(&run);
    }

    /// Puts the previous config and container back; what came of it.
    fn rollback(&self, progress: &Progress, backup: &Backup) -> String {
        let (docker, guard, container) = (self.docker, self.guard, &self.container);
        backup.restore();
        if progress.attempted
            && let Err(err) = guard.remove_created()
        {
            return err;
        }
        let mut restored = false;
        if let Some((id, renamed)) = &progress.renamed {
            if let Err(err) = guard.validate(id, renamed, false) {
                return err;
            }
            if docker.quiet(&["rename", id, container]) {
                if let Err(err) = prepare_leased("awg") {
                    return err;
                }
                if let Err(err) = guard.validate(id, container, false) {
                    return err;
                }
                restored = docker.quiet(&["start", id]);
            }
        }
        if restored {
            "AWG deployment failed; previous config and container restored".into()
        } else {
            "AWG deployment failed; previous config restored, but the container needs attention"
                .into()
        }
    }
}

/// Builds the image from the delivered Dockerfile, so its tag names the
/// version.
fn build(docker: &Docker, guard: &Guard<'_>, image: &str, dir: &Path) -> Result<(), String> {
    let log_path = guard.scratch().join("build.log");
    let log = fs::File::create(&log_path).map_err(|err| err.to_string())?;
    let built = docker
        .command(&["build", "--no-cache", "-t", image, &dir.to_string_lossy()])
        .stdout(log.try_clone().map_err(|err| err.to_string())?)
        .stderr(log)
        .status()
        .is_ok_and(|status| status.success());
    if !built {
        let output = fs::read_to_string(&log_path).unwrap_or_default();
        let lines: Vec<&str> = output.lines().collect();
        let tail = lines[lines.len().saturating_sub(120)..].join("\n");
        return Err(format!("AWG image build failed.\n{tail}"));
    }
    println!("AWG image built: {image}");
    let output = docker
        .command(&[
            "run",
            "--rm",
            "--entrypoint",
            "/bin/sh",
            image,
            "-n",
            "/opt/amnezia/start.sh",
        ])
        .output()
        .map_err(|err| format!("docker failed: {err}"))?;
    if !output.status.success() {
        return Err(format!(
            "AWG image syntax check failed.\n{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    Ok(())
}

pub(crate) fn deploy(setting: Setting<'_>) -> Result<String, String> {
    let docker_dir = PathBuf::from(setting(
        "AWG_DOCKER_DIR",
        "/opt/node-plane-runtime/amnezia-awg",
    ));
    let image = Kind::Awg.resolve(&setting("AWG_DOCKER_IMAGE", ""));
    let container = setting("AWG_CONTAINER_NAME", "amnezia-awg");
    let config = PathBuf::from(setting(
        "AWG_CONFIG",
        "/opt/node-plane-runtime/amnezia-awg/data/wg0.conf",
    ));
    let port = setting("AWG_SERVER_PORT", "51820");
    let tun = setting("AWG_TUN_DEVICE", "/dev/net/tun");
    let docker = Docker::connect()?;

    let mut host: Vec<String> = [
        "--cap-add",
        "NET_ADMIN",
        "--device",
        &format!("{tun}:/dev/net/tun"),
        "--sysctl",
        "net.ipv4.ip_forward=1",
    ]
    .map(String::from)
    .to_vec();
    if Path::new("/proc/sys/net/ipv6/conf/all/disable_ipv6").exists() {
        host.extend(
            [
                "--sysctl",
                "net.ipv6.conf.all.disable_ipv6=0",
                "--sysctl",
                "net.ipv6.conf.all.forwarding=1",
            ]
            .map(String::from),
        );
    }
    let ipv6 = setting("IPV6_OUTBOUND", "on") == "on" && ipv6_uplink();
    if ipv6 {
        keep_router_advertisements()?;
        network(&docker)?;
        host.extend(["--network".into(), NETWORK.into()]);
    }
    let file_name = config
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    for (key, value) in [
        (
            "IPV6_OUTBOUND",
            if ipv6 { "on".into() } else { "off".into() },
        ),
        ("AWG_IFACE", setting("AWG_IFACE", "wg0")),
        ("AWG_CONFIG_FILE", format!("{DATA}/{file_name}")),
        ("AWG_NETWORK", setting("AWG_NETWORK", "10.8.1.0/24")),
        ("AWG_LISTEN_PORT", port.clone()),
    ] {
        host.extend(["-e".into(), format!("{key}={value}")]);
    }

    let character_device = fs::metadata(&tun).is_ok_and(|meta| meta.file_type().is_char_device());
    if !character_device {
        return Err(format!(
            "{tun} is not available on this host. AWG userspace runtime cannot start."
        ));
    }
    let modules = stdout("lsmod", &[]).unwrap_or_default();
    if modules.lines().any(|line| line.starts_with("amneziawg ")) {
        return Err("Host kernel module amneziawg is loaded. Refusing to start AWG userspace runtime on a dirty host.".into());
    }
    let data = docker_dir.join("data");
    fs::create_dir_all(&data).map_err(|err| format!("{}: {err}", data.display()))?;
    if fs::metadata(&config).map_or(true, |meta| meta.len() == 0) {
        return Err(format!(
            "AWG config is missing or empty: {}",
            config.display()
        ));
    }

    let guard = Guard::new(&docker, setting, "awg", &container, &data, DATA)?;
    let previous = guard.existing()?;
    build(&docker, &guard, &image, &docker_dir)?;

    let backup = Backup::take(&config, ".awg-deploy-backup-")?;
    let deployment = Deployment {
        setting,
        docker: &docker,
        guard: &guard,
        image,
        container: container.clone(),
        config,
        data,
        port,
        host,
    };
    let mut progress = Progress::default();
    if let Err(err) = deployment.run(&mut progress, previous.as_deref()) {
        return Err(format!(
            "{err}\n{}",
            deployment.rollback(&progress, &backup)
        ));
    }
    drop(backup);
    if let Some((id, renamed)) = &progress.renamed {
        guard.validate(id, renamed, false)?;
        docker.status(&["rm", "-f", id])?;
    }
    Ok(format!("AWG container deployed: {container}"))
}
