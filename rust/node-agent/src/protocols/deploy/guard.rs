//! Ownership of a protocol's containers during a deployment. Every mutation
//! names a container by the ID Docker reported, checked here first: its
//! name, the bind mount of the protocol's data and, once any Node Plane label
//! is present, every installation label. A container this deployment started
//! must also carry its deployment label.
use std::fs;
use std::os::unix::fs::DirBuilderExt;
use std::path::{Path, PathBuf};

use serde_json::Value;

use super::{Docker, random_hex, uuid4};
use crate::protocols::Setting;

const MARKER: &str = "/var/lib/node-plane-agent/controller-installation-id";

fn hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

fn installation_id(value: &str) -> bool {
    let parts: Vec<&str> = value.split('-').collect();
    parts.len() == 5
        && parts
            .iter()
            .zip([8, 4, 4, 4, 12])
            .all(|(part, length)| hex(part, length))
}

/// What a container must show to be this deployment's.
struct Expected<'a> {
    id: &'a str,
    name: &'a str,
    source: &'a str,
    destination: &'a str,
    installation: &'a str,
    node: &'a str,
    service: &'a str,
    deployment: Option<&'a str>,
}

fn check(records: &Value, expected: &Expected<'_>) -> Result<(), String> {
    let [record] = records.as_array().map(Vec::as_slice).unwrap_or_default() else {
        return Err("Invalid Docker container inventory".into());
    };
    if record["Id"].as_str() != Some(expected.id)
        || record["Name"].as_str() != Some(&format!("/{}", expected.name))
    {
        return Err("Container identity changed during deployment".into());
    }
    let mounts: Vec<&Value> = record["Mounts"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|mount| mount["Destination"].as_str() == Some(expected.destination))
        .collect();
    let owned = match mounts.as_slice() {
        [mount] => {
            mount["Type"].as_str() == Some("bind")
                && mount["Source"].as_str() == Some(expected.source)
                // Xray only reads its config.
                && (expected.service != "xray" || mount["RW"] == Value::Bool(false))
        }
        _ => false,
    };
    if !owned {
        return Err("Container mount ownership could not be verified".into());
    }
    let empty = serde_json::Map::new();
    let labels = record["Config"]["Labels"].as_object().unwrap_or(&empty);
    let label = |key: &str| labels.get(key).and_then(Value::as_str);
    // Pre-label installations rest on the exact bind mount, as cleanup does.
    // Any Node Plane label activates the full label contract.
    let tagged = labels.keys().any(|key| key.starts_with("org.node-plane."));
    let required = [
        ("org.node-plane.managed", "1"),
        ("org.node-plane.installation", expected.installation),
        ("org.node-plane.node", expected.node),
        ("org.node-plane.service", expected.service),
    ];
    if (tagged || expected.deployment.is_some())
        && required
            .iter()
            .any(|(key, value)| label(key) != Some(value))
    {
        return Err("Container installation ownership could not be verified".into());
    }
    if let Some(deployment) = expected.deployment
        && label("org.node-plane.deployment") != Some(deployment)
    {
        return Err("Container belongs to another deployment".into());
    }
    Ok(())
}

/// A deployment of one protocol's container, with its private scratch
/// directory for Docker's container ID file.
pub(crate) struct Guard<'a> {
    docker: &'a Docker,
    service: &'static str,
    container: String,
    source: String,
    destination: &'static str,
    installation: String,
    node: String,
    deployment: String,
    scratch: PathBuf,
}

impl<'a> Guard<'a> {
    /// `source` is the host directory the container mounts at `destination`.
    pub(crate) fn new(
        docker: &'a Docker,
        setting: Setting<'_>,
        service: &'static str,
        container: &str,
        source: &Path,
        destination: &'static str,
    ) -> Result<Self, String> {
        let source = fs::canonicalize(source)
            .map_err(|err| format!("{}: {err}", source.display()))?
            .to_string_lossy()
            .into_owned();
        let node = setting("SERVER_KEY", "");
        let container_name = container.bytes().enumerate().all(|(index, b)| {
            b.is_ascii_alphanumeric() || (index > 0 && matches!(b, b'_' | b'.' | b'-'))
        });
        let node_name = (1..=64).contains(&node.len())
            && node
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'));
        if container.is_empty() || !container_name || !node_name {
            return Err("Invalid managed container or node identity".into());
        }
        let installation = fs::read_to_string(setting("NODE_PLANE_INSTALLATION_MARKER", MARKER))
            .map(|text| text.trim_end_matches('\n').to_string())
            .ok()
            .filter(|value| installation_id(value))
            .ok_or("Agent installation identity is unavailable")?;
        let scratch = std::env::temp_dir().join(format!("node-plane-deploy-{}", random_hex(8)?));
        fs::DirBuilder::new()
            .mode(0o700)
            .create(&scratch)
            .map_err(|err| format!("{}: {err}", scratch.display()))?;
        Ok(Self {
            docker,
            service,
            container: container.to_string(),
            source,
            destination,
            installation,
            node,
            deployment: uuid4()?,
            scratch,
        })
    }

    /// Where `docker run --cidfile` records the new container.
    pub(crate) fn cidfile(&self) -> PathBuf {
        self.scratch.join("container-id")
    }

    /// `--label` arguments of a new container.
    pub(crate) fn labels(&self) -> Vec<String> {
        [
            ("managed", "1"),
            ("installation", &self.installation),
            ("node", &self.node),
            ("service", self.service),
            ("deployment", &self.deployment),
        ]
        .iter()
        .flat_map(|(key, value)| {
            [
                "--label".to_string(),
                format!("org.node-plane.{key}={value}"),
            ]
        })
        .collect()
    }

    /// The container `id`, when it is ours and named `name`; `created` also
    /// requires this deployment's label.
    pub(crate) fn validate(&self, id: &str, name: &str, created: bool) -> Result<String, String> {
        if !hex(id, 64) {
            return Err("Invalid Docker container identity".into());
        }
        let record = self.docker.output(&["container", "inspect", id])?;
        let records: Value = serde_json::from_str(&record)
            .map_err(|_| "Invalid Docker container inventory".to_string())?;
        check(
            &records,
            &Expected {
                id,
                name,
                source: &self.source,
                destination: self.destination,
                installation: &self.installation,
                node: &self.node,
                service: self.service,
                deployment: created.then_some(self.deployment.as_str()),
            },
        )?;
        Ok(id.to_string())
    }

    /// The protocol's current container, when there is one; two containers
    /// of that name or one that is not ours stop the deployment.
    pub(crate) fn existing(&self) -> Result<Option<String>, String> {
        let inventory =
            self.docker
                .output(&["ps", "-a", "--no-trunc", "--format", "{{.ID}} {{.Names}}"])?;
        let mut found = None;
        for line in inventory.lines() {
            let (identity, name) = line.trim().split_once(' ').unwrap_or((line.trim(), ""));
            if name.trim() != self.container {
                continue;
            }
            if found.is_some() {
                return Err(format!(
                    "More than one container is named {}",
                    self.container
                ));
            }
            found = Some(self.validate(identity, &self.container, false)?);
        }
        Ok(found)
    }

    /// The container this deployment started, once Docker recorded it.
    pub(crate) fn created(&self) -> Result<Option<String>, String> {
        let id = fs::read_to_string(self.cidfile()).unwrap_or_default();
        let id = id.trim_end_matches('\n');
        if id.is_empty() {
            return Ok(None);
        }
        self.validate(id, &self.container, true).map(Some)
    }

    pub(crate) fn remove_created(&self) -> Result<(), String> {
        if let Some(id) = self.created()? {
            self.docker.status(&["rm", "-f", &id])?;
        }
        Ok(())
    }
}

impl Drop for Guard<'_> {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.scratch);
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    const ID: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const OWNER: &str = "0b5e3c1a-2f4d-4e6a-8b7c-9d0e1f2a3b4c";

    fn expected(deployment: Option<&str>) -> Expected<'_> {
        Expected {
            id: ID,
            name: "xray",
            source: "/opt/node-plane-runtime/xray",
            destination: "/etc/xray",
            installation: OWNER,
            node: "n1",
            service: "xray",
            deployment,
        }
    }

    fn record(rw: bool, labels: Value) -> Value {
        json!([{"Id": ID, "Name": "/xray", "Config": {"Labels": labels}, "Mounts": [
            {"Type": "bind", "Source": "/opt/node-plane-runtime/xray", "Destination": "/etc/xray", "RW": rw}]}])
    }

    fn owned() -> Value {
        json!({"org.node-plane.managed": "1", "org.node-plane.installation": OWNER,
               "org.node-plane.node": "n1", "org.node-plane.service": "xray",
               "org.node-plane.deployment": "token-1"})
    }

    #[test]
    fn a_container_is_ours_by_mount_and_then_by_every_label() {
        assert_eq!(check(&record(false, json!(null)), &expected(None)), Ok(()));
        assert_eq!(
            check(&record(false, owned()), &expected(Some("token-1"))),
            Ok(())
        );
        let partial = json!({"org.node-plane.managed": "1"});
        assert_eq!(
            check(&record(false, partial), &expected(None)).unwrap_err(),
            "Container installation ownership could not be verified"
        );
        assert_eq!(
            check(&record(false, json!({})), &expected(Some("token-1"))).unwrap_err(),
            "Container installation ownership could not be verified"
        );
        assert_eq!(
            check(&record(false, owned()), &expected(Some("token-2"))).unwrap_err(),
            "Container belongs to another deployment"
        );
    }

    #[test]
    fn identity_and_mount_must_match_exactly() {
        let mut moved = record(false, json!({}));
        moved[0]["Id"] = json!("b".repeat(64));
        assert_eq!(
            check(&moved, &expected(None)).unwrap_err(),
            "Container identity changed during deployment"
        );
        let mut renamed = record(false, json!({}));
        renamed[0]["Name"] = json!("/other");
        assert_eq!(
            check(&renamed, &expected(None)).unwrap_err(),
            "Container identity changed during deployment"
        );
        assert_eq!(
            check(&record(true, json!({})), &expected(None)).unwrap_err(),
            "Container mount ownership could not be verified"
        );
        let mut foreign = record(false, json!({}));
        foreign[0]["Mounts"][0]["Source"] = json!("/srv/other");
        assert!(check(&foreign, &expected(None)).is_err());
        let mut twice = record(false, json!({}));
        let mount = twice[0]["Mounts"][0].clone();
        twice[0]["Mounts"].as_array_mut().unwrap().push(mount);
        assert!(check(&twice, &expected(None)).is_err());
        assert_eq!(
            check(&json!([]), &expected(None)).unwrap_err(),
            "Invalid Docker container inventory"
        );
    }

    #[test]
    fn identities_are_lowercase_hex_and_installation_uuids() {
        assert!(hex(ID, 64));
        assert!(!hex(&ID.to_uppercase(), 64));
        assert!(!hex("not-a-64-hex-id", 64));
        assert!(installation_id(OWNER));
        assert!(!installation_id("0b5e3c1a2f4d4e6a8b7c9d0e1f2a3b4c"));
    }
}
