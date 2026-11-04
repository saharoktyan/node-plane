//! VPN container images of the delivered runtime (VPN plan, 11m).
//!
//! Earlier releases wrote their default image into node.env, where it kept
//! overriding the default of every later release. A node.env value that some
//! release used as its default is managed: a runtime update removes it, and
//! deployments run the release pin below instead.
//! Any other value is an administrator's override and stays.
//!
//! A runtime update then redeploys installed containers that run another
//! image, through the deploy entry points with their rollback.
use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::path::Path;
use std::process::Command;

use serde::Serialize;
use tonic::Status;

use super::files::env_value;
use crate::state::AgentState;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Kind {
    Xray,
    Awg,
}

impl Kind {
    pub(crate) const ALL: [Kind; 2] = [Kind::Xray, Kind::Awg];

    fn name(self) -> &'static str {
        match self {
            Kind::Xray => "xray",
            Kind::Awg => "awg",
        }
    }

    fn variable(self) -> &'static str {
        match self {
            Kind::Xray => "XRAY_DOCKER_IMAGE",
            Kind::Awg => "AWG_DOCKER_IMAGE",
        }
    }

    fn container(self) -> (&'static str, &'static str) {
        match self {
            Kind::Xray => ("XRAY_CONTAINER_NAME", "xray"),
            Kind::Awg => ("AWG_CONTAINER_NAME", "amnezia-awg"),
        }
    }

    fn deploy_script(self) -> &'static str {
        match self {
            Kind::Xray => "deploy-xray.sh",
            Kind::Awg => "deploy-awg.sh",
        }
    }

    /// The image deployments of this release run by default: a local tag
    /// that names Node Plane and the version, made from `upstream`.
    pub(crate) fn release(self) -> &'static str {
        match self {
            Kind::Xray => "node-plane-xray:26.10.31-np.2",
            Kind::Awg => "node-plane-awg:3.1.20260828",
        }
    }

    /// The digest-pinned image the release tag is made from. The AWG image
    /// runs the agent's start script (`protocols/awg/start.sh`).
    pub(crate) fn upstream(self) -> &'static str {
        match self {
            Kind::Xray => {
                "ghcr.io/saharoktyan/xray-core:v26.10.31-np.2@sha256:43c919899d9a7daf94f988552d65e98fb8abfcc7b804fb39f94a3812755c08c0"
            }
            Kind::Awg => {
                "amneziavpn/amneziawg-go:3.1.20260828@sha256:cbafc02b8373a83f428272db6d8001b37bc02e6211cbd8c0cb4e2e3759b12b72"
            }
        }
    }

    /// Defaults of earlier releases. Add the old release value here when
    /// changing it.
    fn former(self) -> &'static [&'static str] {
        match self {
            Kind::Xray => &[
                "ghcr.io/xtls/xray-core:25.12.8",
                "ghcr.io/xtls/xray-core:26.3.27",
                "ghcr.io/xtls/xray-core:26.10.29@sha256:c9443f3ee297f6481bc245269d61720c6f989195ee9b58fa90fdd99f03c21044",
                "ghcr.io/saharoktyan/xray-core:v26.10.31-np.2@sha256:43c919899d9a7daf94f988552d65e98fb8abfcc7b804fb39f94a3812755c08c0",
            ],
            // Built on the node by earlier releases.
            Kind::Awg => &[
                "node-plane-amnezia-awg:0.2.16",
                "node-plane-amnezia-awg:3.1.20260828",
            ],
        }
    }

    pub(crate) fn managed(self, configured: &str) -> bool {
        let configured = configured.trim();
        configured.is_empty() || configured == self.release() || self.former().contains(&configured)
    }

    /// The image a deployment runs for this node.env value.
    pub(crate) fn resolve(self, configured: &str) -> String {
        if self.managed(configured) {
            self.release().to_string()
        } else {
            configured.trim().to_string()
        }
    }
}

/// node.env without its managed image lines, or None when it has none.
fn without_managed_pins(raw: &str) -> Option<String> {
    let mut changed = false;
    let mut kept = Vec::new();
    for line in raw.lines() {
        let managed = Kind::ALL
            .iter()
            .any(|kind| env_value(line, kind.variable()).is_some_and(|value| kind.managed(&value)));
        if managed {
            changed = true;
        } else {
            kept.push(line);
        }
    }
    changed.then(|| {
        let mut text = kept.join("\n");
        if raw.ends_with('\n') {
            text.push('\n');
        }
        text
    })
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub(crate) struct ContainerImage {
    pub(crate) container: String,
    pub(crate) image: String,
    pub(crate) running: bool,
    pub(crate) desired: String,
    pub(crate) managed: bool,
    pub(crate) current: bool,
}

/// `{{.Config.Image}} {{.State.Running}}` of a container.
fn parse_state(output: &str) -> Option<(String, bool)> {
    let (image, running) = output.trim().rsplit_once(' ')?;
    (!image.is_empty()).then(|| (image.to_string(), running == "true"))
}

/// The image reference a container was started with and whether it runs;
/// None without such a container.
fn container_state(container: &str) -> Result<Option<(String, bool)>, String> {
    let output = Command::new("docker")
        .args([
            "container",
            "inspect",
            "-f",
            "{{.Config.Image}} {{.State.Running}}",
            container,
        ])
        .output()
        .map_err(|err| err.to_string())?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        if stderr.to_lowercase().contains("no such") {
            return Ok(None);
        }
        return Err(stderr.trim().to_string());
    }
    parse_state(&String::from_utf8_lossy(&output.stdout))
        .map(Some)
        .ok_or_else(|| "invalid container inspection".to_string())
}

impl AgentState {
    /// Images of the installed VPN containers. A digest-pinned reference
    /// names exact content.
    pub(crate) fn container_images(&self) -> Result<BTreeMap<String, ContainerImage>, String> {
        let mut report = BTreeMap::new();
        for kind in Kind::ALL {
            let (variable, default) = kind.container();
            let container = self.node_env_value(variable, default);
            let Some((image, running)) = container_state(&container)? else {
                continue;
            };
            let configured = self.node_env_value(kind.variable(), "");
            let desired = kind.resolve(&configured);
            report.insert(
                kind.name().to_string(),
                ContainerImage {
                    current: image == desired,
                    container,
                    image,
                    running,
                    desired,
                    managed: kind.managed(&configured),
                },
            );
        }
        Ok(report)
    }

    /// Removes managed image pins from node.env, keeping its mode.
    fn migrate_image_pins(&self) -> Result<(), Status> {
        let path = Path::new(&self.config.node_env_path);
        let Ok(raw) = fs::read_to_string(path) else {
            return Ok(());
        };
        let Some(text) = without_managed_pins(&raw) else {
            return Ok(());
        };
        let failed =
            |err: std::io::Error| Status::internal(format!("node.env update failed: {err}"));
        let permissions = fs::metadata(path).map_err(failed)?.permissions();
        let tmp = path.with_extension("env.images-tmp");
        {
            let mut file = fs::File::create(&tmp).map_err(failed)?;
            file.set_permissions(permissions).map_err(failed)?;
            file.write_all(text.as_bytes()).map_err(failed)?;
            file.sync_all().map_err(failed)?;
        }
        fs::rename(&tmp, path).map_err(failed)
    }

    /// Runtime update: follow the release image and redeploy every installed
    /// container that runs another one. Fails unless each ends up running
    /// the image it should.
    pub(crate) fn refresh_container_images(&self) -> Result<serde_json::Value, Status> {
        self.migrate_image_pins()?;
        let unverified = |detail: String| {
            Status::failed_precondition(format!("container images unverified: {detail}"))
        };
        let mut report = serde_json::Map::new();
        for (kind_name, item) in self.container_images().map_err(unverified)? {
            let kind = Kind::ALL
                .into_iter()
                .find(|kind| kind.name() == kind_name)
                .expect("reported kinds are known");
            let replaced = !item.current;
            let item = if replaced {
                self.run_runtime_command(kind.deploy_script(), &[])?;
                self.container_images()
                    .map_err(unverified)?
                    .remove(&kind_name)
                    .filter(|after| after.current && after.running)
                    .ok_or_else(|| {
                        Status::failed_precondition(format!(
                            "{kind_name} container does not run the release image after deployment"
                        ))
                    })?
            } else {
                item
            };
            report.insert(
                kind_name,
                serde_json::json!({"image": item.image, "replaced": replaced}),
            );
        }
        // Images earlier releases built on the node; Docker keeps one that
        // a container still uses.
        for image in Kind::ALL
            .iter()
            .flat_map(|kind| kind.former())
            .filter(|image| image.starts_with("node-plane-"))
        {
            let _ = Command::new("docker").args(["image", "rm", image]).output();
        }
        Ok(serde_json::Value::Object(report))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const OLD_XRAY: &str = "ghcr.io/xtls/xray-core:26.10.29@sha256:c9443f3ee297f6481bc245269d61720c6f989195ee9b58fa90fdd99f03c21044";

    #[test]
    fn release_and_former_defaults_follow_the_release_and_overrides_stay() {
        for configured in [
            "",
            "  ",
            Kind::Xray.release(),
            OLD_XRAY,
            "ghcr.io/xtls/xray-core:25.12.8",
        ] {
            assert!(Kind::Xray.managed(configured), "{configured}");
            assert_eq!(Kind::Xray.resolve(configured), Kind::Xray.release());
        }
        for former in [
            "node-plane-amnezia-awg:0.2.16",
            "node-plane-amnezia-awg:3.1.20260828",
        ] {
            assert_eq!(Kind::Awg.resolve(former), Kind::Awg.release());
        }
        assert!(Kind::Xray.managed(Kind::Xray.upstream()));
        assert!(!Kind::Xray.managed("registry.example/xray:1"));
        assert_eq!(
            Kind::Xray.resolve("registry.example/xray:1"),
            "registry.example/xray:1"
        );
        for kind in Kind::ALL {
            assert!(!kind.former().contains(&kind.release()));
            assert!(kind.release().starts_with("node-plane-"));
            assert!(kind.upstream().contains("@sha256:"));
        }
    }

    #[test]
    fn only_managed_pins_leave_node_env() {
        let raw = format!(
            "SERVER_KEY=n1\nXRAY_DOCKER_IMAGE='{OLD_XRAY}'\nAWG_DOCKER_IMAGE=custom/awg:1\nAWG_MTU=1280\n"
        );
        assert_eq!(
            without_managed_pins(&raw).as_deref(),
            Some("SERVER_KEY=n1\nAWG_DOCKER_IMAGE=custom/awg:1\nAWG_MTU=1280\n")
        );
        assert_eq!(
            without_managed_pins("SERVER_KEY=n1\nAWG_DOCKER_IMAGE=custom/awg:1\n"),
            None
        );
    }

    #[test]
    fn container_state_is_the_reference_and_whether_it_runs() {
        assert_eq!(
            parse_state(&format!("{OLD_XRAY} true\n")),
            Some((OLD_XRAY.to_string(), true))
        );
        assert_eq!(
            parse_state("node-plane-amnezia-awg:3.1.20260828 false"),
            Some(("node-plane-amnezia-awg:3.1.20260828".into(), false))
        );
        assert_eq!(parse_state(""), None);
    }
}
