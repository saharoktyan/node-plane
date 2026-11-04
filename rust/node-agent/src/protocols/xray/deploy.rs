//! The Xray container: the image checks the config, the previous container
//! stops under a temporary name, and comes back when the new one does not
//! run. Replaces the body of `deploy-xray.sh`.
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use crate::protocols::Setting;
use crate::protocols::deploy::guard::Guard;
use crate::protocols::deploy::{
    Docker, fetch, leased, leased_deploy, open_directory, prepare_leased, settle,
};
use crate::protocols::settings::runtime;
use crate::runtime::images::Kind;

pub(crate) fn deploy(setting: Setting<'_>) -> Result<String, String> {
    let docker_dir = PathBuf::from(setting("XRAY_DOCKER_DIR", "/opt/node-plane-runtime/xray"));
    let image = Kind::Xray.resolve(&setting("XRAY_DOCKER_IMAGE", ""));
    let container = setting("XRAY_CONTAINER_NAME", "node-plane-xray");
    let config = PathBuf::from(setting(
        "XRAY_CONFIG",
        "/opt/node-plane-runtime/xray/config.json",
    ));
    fs::create_dir_all(&docker_dir).map_err(|err| format!("{}: {err}", docker_dir.display()))?;
    let runtime = runtime();
    for dir in [
        runtime.parent().unwrap_or(Path::new("/")),
        &runtime,
        &docker_dir,
    ] {
        open_directory(dir);
    }
    if config.is_dir() {
        let stamp = chrono::Local::now().format("%Y%m%d-%H%M%S");
        let aside = format!("{}.dirbak.{stamp}", config.display());
        fs::rename(&config, &aside).map_err(|err| format!("{aside}: {err}"))?;
    }
    if !config.is_file() {
        return Err(format!("Xray config not found: {}", config.display()));
    }
    let _ = fs::set_permissions(&config, fs::Permissions::from_mode(0o600));
    let source = config.parent().unwrap_or(Path::new("/"));
    let mount = format!("{}:/etc/xray:ro", source.display());

    let docker = Docker::connect()?;
    let guard = Guard::new(&docker, setting, "xray", &container, source, "/etc/xray")?;
    let previous_id = guard.existing()?;
    prepare_leased("xray")?;
    fetch(&docker, Kind::Xray, &image)?;
    docker.status(&[
        "run",
        "--rm",
        "--user",
        "0:0",
        "-v",
        &mount,
        &image,
        "run",
        "-test",
        "-c",
        "/etc/xray/config.json",
    ])?;

    let previous = match previous_id {
        Some(id) => {
            let renamed = format!("{container}-previous-{}", std::process::id());
            guard.validate(&id, &container, false)?;
            docker.status(&["rename", &id, &renamed])?;
            guard.validate(&id, &renamed, false)?;
            if docker.status(&["stop", &id]).is_err() {
                guard.validate(&id, &renamed, false)?;
                docker.status(&["rename", &id, &container])?;
                return Err("Could not stop the previous Xray container".into());
            }
            Some((id, renamed))
        }
        None => None,
    };
    let restore = || -> Result<(), String> {
        guard.remove_created()?;
        if let Some((id, renamed)) = &previous {
            guard.validate(id, renamed, false)?;
            docker.status(&["rename", id, &container])?;
            prepare_leased("xray")?;
            guard.validate(id, &container, false)?;
            docker
                .status(&["start", id])
                .map_err(|_| "Previous Xray container could not be restarted")?;
        }
        Ok(())
    };
    let fail = |reason: &str| match restore() {
        Ok(()) => format!("{reason}; rollback completed"),
        Err(err) => format!("{err}\n{reason}; rollback could not be confirmed, recovery required"),
    };

    let cidfile = guard.cidfile().to_string_lossy().into_owned();
    let labels = guard.labels();
    let mut run = vec!["run", "-d", "--name", &container, "--cidfile", &cidfile];
    run.extend(labels.iter().map(String::as_str));
    run.extend([
        "--restart",
        "no",
        "--user",
        "0:0",
        "--network",
        "host",
        "-v",
        &mount,
        &image,
        "run",
        "-c",
        "/etc/xray/config.json",
    ]);
    if docker.status(&run).is_err() {
        return Err(fail("Xray container failed to start"));
    }
    settle(setting, 2);
    let new_id = guard
        .created()?
        .ok_or("New Xray container identity was not recorded; recovery required")?;
    let state = docker
        .output(&["inspect", "-f", "{{.State.Status}}", &new_id])
        .unwrap_or_else(|_| "unknown".into());
    if state != "running" {
        docker.report(&["logs", &new_id]);
        return Err(fail("Xray container did not start with the current config"));
    }
    if leased("xray") {
        if leased_deploy("xray").is_err() {
            return Err(fail("Xray lease supervisor could not be started"));
        }
    } else {
        guard.validate(&new_id, &container, true)?;
        docker.status(&["update", "--restart", "unless-stopped", &new_id])?;
    }
    if let Some((id, renamed)) = &previous {
        guard.validate(id, renamed, false)?;
        docker.status(&["rm", "-f", id])?;
    }
    Ok(format!("Xray container deployed: {container} ({image})"))
}
