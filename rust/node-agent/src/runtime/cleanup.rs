//! Removal of the managed runtime: owned containers, directories and configs.
use std::fs;
use std::path::Path;
use std::process::Command;

use tonic::Status;

use crate::agent::v1::DeleteRuntimeResponse;
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn container_cleanup_plan(
        records: &serde_json::Value,
        protocols: &[(&str, &str, &str, bool)],
    ) -> Result<Vec<(String, bool)>, Status> {
        let records = records
            .as_array()
            .ok_or_else(|| Status::failed_precondition("invalid Docker container inventory"))?;
        let mut plan = Vec::new();
        for record in records {
            let name = record["Name"]
                .as_str()
                .ok_or_else(|| Status::failed_precondition("missing Docker container name"))?
                .trim_start_matches('/');
            for &(configured, source, destination, graceful) in protocols {
                let previous = name
                    .strip_prefix(configured)
                    .and_then(|suffix| suffix.strip_prefix("-previous-"))
                    .is_some_and(|pid| !pid.is_empty() && pid.bytes().all(|b| b.is_ascii_digit()));
                if name != configured && !previous {
                    continue;
                }
                let owned = record["Mounts"].as_array().is_some_and(|mounts| {
                    mounts.iter().any(|mount| {
                        mount["Type"].as_str() == Some("bind")
                            && mount["Source"].as_str() == Some(source)
                            && mount["Destination"].as_str() == Some(destination)
                    })
                });
                if !owned {
                    return Err(Status::failed_precondition(format!(
                        "container ownership could not be verified: {name}"
                    )));
                }
                let id = record["Id"]
                    .as_str()
                    .filter(|id| {
                        id.len() == 64
                            && id
                                .bytes()
                                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                    })
                    .ok_or_else(|| {
                        Status::failed_precondition("invalid Docker container identity")
                    })?;
                if !plan.iter().any(|(existing, _)| existing == id) {
                    plan.push((id.to_string(), graceful));
                }
            }
        }
        Ok(plan)
    }

    pub(crate) fn owned_runtime_containers(&self) -> Result<Vec<(String, bool)>, Status> {
        let read = |args: &[&str]| -> Result<String, Status> {
            let output = Command::new("docker")
                .args(args)
                .output()
                .map_err(|_| Status::failed_precondition("Docker inventory is unavailable"))?;
            if !output.status.success() {
                return Err(Status::failed_precondition(
                    "Docker inventory is unavailable",
                ));
            }
            String::from_utf8(output.stdout)
                .map_err(|_| Status::failed_precondition("invalid Docker inventory encoding"))
        };
        let ids = read(&["ps", "-aq"])?;
        let ids: Vec<&str> = ids.split_whitespace().collect();
        if ids.is_empty() {
            return Ok(Vec::new());
        }
        let mut args = vec!["container", "inspect"];
        args.extend(ids);
        let records = serde_json::from_str(&read(&args)?)
            .map_err(|_| Status::failed_precondition("invalid Docker container inventory"))?;
        let xray = self.node_env_value("XRAY_CONTAINER_NAME", "xray");
        let awg = self.node_env_value("AWG_CONTAINER_NAME", "amnezia-awg");
        let xray_config = self.node_env_value("XRAY_CONFIG", &self.config.xray_config_path);
        let awg_config = self.node_env_value("AWG_CONFIG", &self.config.awg_config_path);
        let parent = |path: &str| -> Result<String, Status> {
            Path::new(path)
                .parent()
                .and_then(Path::to_str)
                .map(str::to_owned)
                .ok_or_else(|| Status::failed_precondition("invalid protocol config path"))
        };
        Self::container_cleanup_plan(
            &records,
            &[
                (&xray, &parent(&xray_config)?, "/etc/xray", false),
                (&awg, &parent(&awg_config)?, "/opt/amnezia/awg", true),
            ],
        )
    }

    pub(crate) fn runtime_cleanup_directories(&self) -> Result<Vec<std::path::PathBuf>, Status> {
        let root = Path::new(&self.config.runtime_root);
        let valid_path = |path: &Path| {
            path.is_absolute()
                && path.components().count() >= 3
                && !path.components().any(|part| {
                    matches!(
                        part,
                        std::path::Component::ParentDir | std::path::Component::CurDir
                    )
                })
                && !path.ancestors().any(|ancestor| {
                    fs::symlink_metadata(ancestor)
                        .map(|metadata| metadata.file_type().is_symlink())
                        .unwrap_or(false)
                })
        };
        if !valid_path(root)
            || root.parent() == Some(Path::new("/home"))
            || matches!(
                self.config.runtime_root.as_str(),
                "/usr/local" | "/var/lib" | "/var/log"
            )
        {
            return Err(Status::failed_precondition(
                "refusing invalid runtime cleanup root",
            ));
        }
        let directories = vec![
            self.node_env_value(
                "AWG_CLIENTS_DIR",
                &format!("{}/awg-clients", self.config.runtime_root),
            ),
            self.node_env_value(
                "XRAY_DOCKER_DIR",
                &format!("{}/xray", self.config.runtime_root),
            ),
            self.node_env_value(
                "AWG_DOCKER_DIR",
                &format!("{}/amnezia-awg", self.config.runtime_root),
            ),
            self.config.runtime_root.clone(),
        ]
        .into_iter()
        .map(std::path::PathBuf::from)
        .collect::<Vec<_>>();
        for path in &directories {
            if !valid_path(path) || !path.starts_with(root) {
                return Err(Status::failed_precondition(
                    "runtime cleanup directory is outside the managed root",
                ));
            }
        }
        for value in [
            self.node_env_value("XRAY_CONFIG", &self.config.xray_config_path),
            self.node_env_value("AWG_CONFIG", &self.config.awg_config_path),
            self.config.node_env_path.clone(),
        ] {
            let path = Path::new(&value);
            if !valid_path(path) || path.is_dir() {
                return Err(Status::failed_precondition(
                    "refusing invalid runtime cleanup file",
                ));
            }
        }
        Ok(directories)
    }

    pub(crate) fn remove_runtime_files(&self) -> Result<(), Status> {
        let directories = self.runtime_cleanup_directories()?;
        // Capture overrides before deleting node.env. Only delete individual
        // config files outside owned runtime directories, never their parents.
        let configs = [
            self.node_env_value("XRAY_CONFIG", &self.config.xray_config_path),
            self.node_env_value("AWG_CONFIG", &self.config.awg_config_path),
        ];
        for config in configs {
            let path = Path::new(&config);
            let parent = path.parent().unwrap_or(Path::new("."));
            let filename = path
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("");
            if parent.exists() {
                for entry in
                    fs::read_dir(parent).map_err(|err| Status::internal(err.to_string()))?
                {
                    let entry = entry.map_err(|err| Status::internal(err.to_string()))?;
                    let name = entry.file_name().to_string_lossy().into_owned();
                    if name == filename
                        || name == format!("{filename}.lock")
                        || name == format!("{filename}.bak")
                        || name == format!("{filename}.node-plane-settings.bak")
                        || name == format!("{filename}.node-plane-settings.bak.absent")
                        || name.starts_with(&format!("{filename}.bak."))
                        || name.starts_with(".awg-deploy-backup-")
                        || name.starts_with(".awg-config-backup-")
                        || name.starts_with(".awg-regenerate-backup-")
                        || name.starts_with(".xray-config-")
                        || name.starts_with(".xray-user-")
                        || name.starts_with(".lease-config-")
                    {
                        fs::remove_file(entry.path()).map_err(|err| {
                            Status::internal(format!("failed to remove config artifact: {err}"))
                        })?;
                    }
                }
            }
        }
        for path in directories {
            if path.exists() {
                fs::remove_dir_all(&path).map_err(|err| {
                    Status::internal(format!("failed to remove runtime directory: {err}"))
                })?;
            }
        }
        for path in [
            self.config.node_env_path.clone(),
            format!("{}.example", self.config.node_env_path),
        ] {
            match fs::remove_file(path) {
                Ok(()) => (),
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => (),
                Err(err) => {
                    return Err(Status::internal(format!(
                        "failed to remove node environment: {err}"
                    )));
                }
            }
        }
        Ok(())
    }

    pub(crate) fn delete_runtime(
        &self,
        preserve_config: bool,
    ) -> Result<DeleteRuntimeResponse, Status> {
        let _guard = self.guard_fenced_maintenance()?;
        self.delete_runtime_unchecked(preserve_config)
    }

    pub(crate) fn delete_runtime_unchecked(
        &self,
        preserve_config: bool,
    ) -> Result<DeleteRuntimeResponse, Status> {
        // Validate every file/directory before touching containers as well.
        if !preserve_config {
            self.runtime_cleanup_directories()?;
        }

        if !self.docker_available()
            && Command::new("docker")
                .arg("--version")
                .output()
                .map(|output| output.status.success())
                .unwrap_or(false)
        {
            return Err(Status::failed_precondition(
                "Docker is unavailable; cannot verify runtime cleanup",
            ));
        }
        if self.docker_available() {
            // Plan all candidates before the first mutation. A name alone is
            // not ownership evidence; immutable IDs avoid deleting replacements.
            for (identity, graceful) in self.owned_runtime_containers()? {
                if graceful {
                    // Allow the AWG entrypoint to remove its interface/NAT rules.
                    self.docker_best_effort(&["stop", "--time", "10", &identity]);
                }
                self.docker_best_effort(&["rm", "-f", &identity]);
            }
            if !self.owned_runtime_containers()?.is_empty() {
                return Err(Status::failed_precondition(
                    "managed containers still present",
                ));
            }
            // Images belong to the host-wide Docker cache, not this node's
            // runtime lifecycle. Keep them even when no container uses them.
        }

        let mut leftovers = Vec::new();
        if !leftovers.is_empty() {
            return Err(Status::failed_precondition(leftovers.join("\n")));
        }
        // Keep node.env until Docker cleanup has been verified: it contains
        // custom container/image names needed for an idempotent retry.
        if !preserve_config {
            self.remove_runtime_files()?;
        }
        if !preserve_config && Path::new(&self.config.node_env_path).exists() {
            leftovers.push("node.env still present".to_string());
        }
        if !preserve_config && Path::new(&self.config.runtime_root).exists() {
            leftovers.push("runtime root still present".to_string());
        }
        if !leftovers.is_empty() {
            return Err(Status::failed_precondition(leftovers.join("\n")));
        }

        let summary = if preserve_config {
            "managed runtime removed; existing config preserved"
        } else {
            "managed runtime removed with configs"
        };
        Ok(DeleteRuntimeResponse {
            summary: summary.to_string(),
        })
    }
}

#[cfg(test)]
mod tests {
    use crate::config::AgentConfig;
    use crate::state::AgentState;
    use std::{
        fs, process,
        time::{SystemTime, UNIX_EPOCH},
    };

    #[test]
    fn container_cleanup_uses_mount_ownership_and_includes_previous_instances() {
        let records = serde_json::json!([
            {"Name": "/xray", "Id": "a".repeat(64), "Mounts": [
                {"Type": "bind", "Source": "/runtime/xray", "Destination": "/etc/xray"}]},
            {"Name": "/xray-previous-123", "Id": "b".repeat(64), "Mounts": [
                {"Type": "bind", "Source": "/runtime/xray", "Destination": "/etc/xray"}]},
            {"Name": "/awg", "Id": "c".repeat(64), "Mounts": [
                {"Type": "bind", "Source": "/runtime/awg/data", "Destination": "/opt/amnezia/awg"}]},
            {"Name": "/xray-previous-unrelated", "Id": "d".repeat(64), "Mounts": []}
        ]);
        let plan = AgentState::container_cleanup_plan(
            &records,
            &[
                ("xray", "/runtime/xray", "/etc/xray", false),
                ("awg", "/runtime/awg/data", "/opt/amnezia/awg", true),
            ],
        )
        .unwrap();
        assert_eq!(
            plan,
            vec![
                ("a".repeat(64), false),
                ("b".repeat(64), false),
                ("c".repeat(64), true)
            ]
        );
    }

    #[test]
    fn unrelated_mount_refuses_the_entire_container_cleanup_plan() {
        let records = serde_json::json!([
            {"Name": "/xray", "Id": "a".repeat(64), "Mounts": [
                {"Type": "bind", "Source": "/runtime/xray", "Destination": "/etc/xray"}]},
            {"Name": "/awg", "Id": "b".repeat(64), "Mounts": [
                {"Type": "bind", "Source": "/another/service", "Destination": "/opt/amnezia/awg"}]}
        ]);
        assert!(
            AgentState::container_cleanup_plan(
                &records,
                &[
                    ("xray", "/runtime/xray", "/etc/xray", false),
                    ("awg", "/runtime/awg/data", "/opt/amnezia/awg", true),
                ]
            )
            .is_err()
        );
    }

    #[test]
    fn malformed_inventory_is_not_an_empty_cleanup_plan() {
        assert!(AgentState::container_cleanup_plan(&serde_json::json!({}), &[]).is_err());
        assert!(
            AgentState::container_cleanup_plan(
                &serde_json::json!([{
                    "Name": "/xray", "Id": "not-an-id", "Mounts": [
                        {"Type": "bind", "Source": "/runtime/xray", "Destination": "/etc/xray"}]
                }]),
                &[("xray", "/runtime/xray", "/etc/xray", false)]
            )
            .is_err()
        );
    }

    #[test]
    fn cleanup_removes_external_configs_and_peer_archives_only() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root =
            std::env::temp_dir().join(format!("node-plane-cleanup-{}-{nonce}", process::id()));
        let runtime = root.join("runtime");
        let external = root.join("external");
        let clients = runtime.join("clients");
        fs::create_dir_all(&runtime).unwrap();
        fs::create_dir_all(&external).unwrap();
        fs::create_dir_all(&clients).unwrap();
        for name in [
            "xray.json",
            "xray.json.bak",
            "xray.json.bak.20260101",
            "xray.json.node-plane-settings.bak",
            "xray.json.node-plane-settings.bak.absent",
            ".xray-config-interrupted.json",
            ".xray-user-interrupted.json",
            "wg.conf",
            "wg.conf.lock",
            "unrelated.txt",
        ] {
            fs::write(external.join(name), "data").unwrap();
        }
        fs::write(clients.join("alice.revoked.json"), "private-key").unwrap();
        let node_env = root.join("node.env");
        fs::write(
            &node_env,
            format!(
                "XRAY_CONFIG={}\nAWG_CONFIG={}\nAWG_CLIENTS_DIR={}\n",
                external.join("xray.json").display(),
                external.join("wg.conf").display(),
                clients.display()
            ),
        )
        .unwrap();
        let state = AgentState::new(AgentConfig {
            runtime_root: runtime.display().to_string(),
            node_env_path: node_env.display().to_string(),
            ..AgentConfig::default()
        });
        state.remove_runtime_files().unwrap();
        assert!(!runtime.exists());
        assert!(!clients.exists());
        assert!(!node_env.exists());
        assert!(!external.join("xray.json").exists());
        assert!(!external.join("xray.json.bak.20260101").exists());
        for name in [
            "xray.json.bak",
            "xray.json.node-plane-settings.bak",
            "xray.json.node-plane-settings.bak.absent",
            ".xray-config-interrupted.json",
            ".xray-user-interrupted.json",
        ] {
            assert!(!external.join(name).exists(), "artifact remains: {name}");
        }
        assert!(!external.join("wg.conf.lock").exists());
        assert!(external.join("unrelated.txt").exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn cleanup_refuses_external_directories_before_deleting_any_config() {
        let root = std::env::temp_dir().join(format!(
            "node-plane-unsafe-cleanup-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let runtime = root.join("runtime");
        let external = root.join("external");
        fs::create_dir_all(&runtime).unwrap();
        fs::create_dir_all(&external).unwrap();
        let config = external.join("xray.json");
        fs::write(&config, "keep").unwrap();
        let node_env = root.join("node.env");
        fs::write(
            &node_env,
            format!(
                "XRAY_CONFIG={}\nAWG_CLIENTS_DIR={}\n",
                config.display(),
                external.display()
            ),
        )
        .unwrap();
        let state = AgentState::new(AgentConfig {
            runtime_root: runtime.display().to_string(),
            node_env_path: node_env.display().to_string(),
            ..AgentConfig::default()
        });
        assert!(state.remove_runtime_files().is_err());
        assert_eq!(fs::read_to_string(&config).unwrap(), "keep");
        assert!(node_env.exists());
        assert!(runtime.exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn cleanup_refuses_symlinked_runtime_even_when_it_points_to_a_real_directory() {
        let root = std::env::temp_dir().join(format!(
            "node-plane-symlink-cleanup-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let external = root.join("external");
        fs::create_dir_all(&external).unwrap();
        std::os::unix::fs::symlink(&external, root.join("runtime")).unwrap();
        let state = AgentState::new(AgentConfig {
            runtime_root: root.join("runtime").display().to_string(),
            ..AgentConfig::default()
        });
        assert!(state.runtime_cleanup_directories().is_err());
        assert!(external.exists());
        fs::remove_dir_all(root).unwrap();
    }
}
