use anyhow::{Context, Result, ensure};
use clap::Subcommand;
use reqwest::blocking::Client;
use serde::Deserialize;
use std::{
    fs,
    path::{Path, PathBuf},
    time::Duration,
};

const REPO: &str = "https://api.github.com/repos/saharoktyan/node-plane";
const DIST_APP: &str = "node-plane-cli";

#[derive(Subcommand)]
pub enum Command {
    /// Remove the managed binary; retain profiles and SSH keys.
    Uninstall,
    /// Check the matching stable/alpha channel and update the managed binary.
    Update {
        /// Update without the interactive confirmation.
        #[arg(long)]
        yes: bool,
    },
}

#[derive(Clone, Deserialize)]
pub struct Release {
    pub tag_name: String,
    draft: bool,
    prerelease: bool,
    #[serde(default)]
    assets: Vec<Asset>,
    #[serde(skip)]
    pub dist_managed: bool,
}

#[derive(Clone, Deserialize)]
struct Asset {
    name: String,
}

fn platform_archive() -> String {
    let target = if cfg!(windows) {
        "x86_64-pc-windows-msvc.zip"
    } else if cfg!(target_os = "macos") && cfg!(target_arch = "aarch64") {
        "aarch64-apple-darwin.tar.gz"
    } else if cfg!(target_os = "macos") {
        "x86_64-apple-darwin.tar.gz"
    } else {
        "x86_64-unknown-linux-gnu.tar.gz"
    };
    format!("node-plane-cli-{target}")
}

fn has_platform_artifacts(release: &Release) -> bool {
    let archive = platform_archive();
    [
        archive.clone(),
        format!("{archive}.sha256"),
        "dist-manifest.json".into(),
    ]
    .iter()
    .all(|name| release.assets.iter().any(|asset| &asset.name == name))
}

fn client() -> Result<Client> {
    Ok(Client::builder()
        .user_agent("node-plane-workstation")
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(20))
        .build()?)
}

fn newer(tag: &str, current: &str) -> bool {
    match (
        semver::Version::parse(tag.trim_start_matches('v')),
        semver::Version::parse(current),
    ) {
        (Ok(candidate), Ok(installed)) => candidate > installed,
        _ => false,
    }
}

pub fn discover() -> Result<Option<Release>> {
    let releases: Vec<Release> = client()?
        .get(format!("{REPO}/releases?per_page=100"))
        .send()?
        .error_for_status()?
        .json()?;
    Ok(select_release(releases, env!("CARGO_PKG_VERSION")))
}

fn select_release(releases: Vec<Release>, current: &str) -> Option<Release> {
    let alpha = current.contains('-');
    releases
        .into_iter()
        .filter_map(|mut release| {
            if release.draft
                || release.prerelease != alpha
                || release.tag_name.contains('-') != alpha
                || !newer(&release.tag_name, current)
                || !has_platform_artifacts(&release)
            {
                return None;
            }
            release.dist_managed = true;
            Some(release)
        })
        .max_by_key(|r| semver::Version::parse(r.tag_name.trim_start_matches('v')).ok())
}

/// Called before starting any threads. Channel installers use isolated receipts.
pub fn prepare_channel_receipt_environment() -> Result<()> {
    if std::env::var_os("AXOUPDATER_CONFIG_PATH").is_some()
        || std::env::var_os("AXOUPDATER_CONFIG_WORKING_DIR").is_some()
    {
        return Ok(());
    }
    let root = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| {
            if cfg!(windows) {
                std::env::var_os("LOCALAPPDATA").map(PathBuf::from)
            } else {
                dirs::home_dir().map(|p| p.join(".config"))
            }
        })
        .context("Cannot locate Workstation install receipts")?;
    let base = root
        .join("node-plane-workstation")
        .join(crate::config::workstation_channel());
    let receipts = base.join(DIST_APP);
    let receipt = receipts.join(format!("{DIST_APP}-receipt.json"));
    if !receipt.is_file() {
        return Ok(());
    }
    let value: serde_json::Value = serde_json::from_slice(&fs::read(&receipt)?)?;
    let executable = std::env::current_exe()?.canonicalize()?;
    let prefix = value["install_prefix"]
        .as_str()
        .context("Invalid Workstation install receipt")?;
    if fs::canonicalize(prefix).ok().as_deref() != executable.parent() {
        return Ok(());
    }
    // This runs at process startup, before the TUI/network workers are created.
    unsafe {
        std::env::set_var("AXOUPDATER_CONFIG_PATH", receipts);
        std::env::set_var("XDG_CONFIG_HOME", base);
    }
    Ok(())
}

fn dist_updater() -> Option<axoupdater::AxoUpdater> {
    let mut updater = axoupdater::AxoUpdater::new_for(DIST_APP);
    updater.load_receipt().ok()?;
    let source = updater.source.as_ref()?;
    if source.owner != "saharoktyan" || source.name != "node-plane" || source.app_name != DIST_APP {
        return None;
    }
    if !updater.check_receipt_is_for_this_executable().ok()? {
        return None;
    }
    updater.disable_installer_output();
    Some(updater)
}

fn uninstall_dist() -> Result<()> {
    let _lock = installation_lock(&home()?)?;
    let _updater =
        dist_updater().context("This executable is not a cargo-dist managed installation")?;
    let executable = std::env::current_exe()?.canonicalize()?;
    let parent = executable
        .parent()
        .context("Missing installation directory")?;
    let updater_name = if cfg!(windows) {
        "node-plane-cli-update.exe"
    } else {
        "node-plane-cli-update"
    };
    let updater_path = parent.join(updater_name);
    if updater_path.is_file() {
        fs::remove_file(updater_path)?;
    }
    #[cfg(windows)]
    {
        if let Some(appdata) = dirs::data_dir() {
            let separate = std::env::var_os("AXOUPDATER_CONFIG_PATH").is_some();
            remove_start_menu_shortcut(
                &appdata,
                if separate {
                    Some(crate::config::workstation_channel())
                } else {
                    None
                },
            )?;
        }
        self_replace::self_delete()?;
    }
    #[cfg(not(windows))]
    {
        let launcher = if crate::config::workstation_channel() == "dev" {
            "node-plane-dev"
        } else {
            "node-plane"
        };
        for alias in [
            parent.join(launcher),
            home()?.join(".local/bin").join(launcher),
        ] {
            if fs::read_link(&alias).ok().as_deref() == Some(executable.as_path()) {
                fs::remove_file(alias)?;
            }
        }
        fs::remove_file(executable)?;
    }
    #[cfg(windows)]
    {
        let launcher = parent.join(if crate::config::workstation_channel() == "dev" {
            "node-plane-dev.cmd"
        } else {
            "node-plane.cmd"
        });
        if launcher.is_file() {
            fs::remove_file(launcher)?;
        }
    }

    // Retain cargo-dist's env scripts: shell startup files source these and
    // ~/.local/bin can contain other applications. Retain user data as before.
    let mut config_paths = Vec::new();
    if std::env::var_os("AXOUPDATER_CONFIG_WORKING_DIR").is_some() {
        config_paths.push(std::env::current_dir()?);
    } else if let Some(path) = std::env::var_os("AXOUPDATER_CONFIG_PATH") {
        config_paths.push(PathBuf::from(path));
    } else {
        if let Some(path) = std::env::var_os("XDG_CONFIG_HOME") {
            config_paths.push(PathBuf::from(path).join(DIST_APP));
        }
        if cfg!(windows) {
            if let Some(path) = std::env::var_os("LOCALAPPDATA") {
                config_paths.push(PathBuf::from(path).join(DIST_APP));
            }
        } else {
            config_paths.push(home()?.join(".config").join(DIST_APP));
        }
    }
    for config in config_paths {
        let receipt = config.join(format!("{DIST_APP}-receipt.json"));
        if receipt.is_file() {
            fs::remove_file(receipt)?;
            break;
        }
    }
    Ok(())
}

#[cfg(any(windows, test))]
fn remove_start_menu_shortcut(appdata: &Path, channel: Option<&str>) -> Result<()> {
    let name = channel.map_or_else(
        || "Node Plane Workstation.lnk".to_string(),
        |channel| {
            if channel == "dev" {
                "Node Plane (Dev).lnk".into()
            } else {
                "Node Plane.lnk".into()
            }
        },
    );
    let legacy = channel.map(|channel| format!("Node Plane Workstation ({channel}).lnk"));
    for name in std::iter::once(name).chain(legacy) {
        let shortcut = appdata
            .join("Microsoft/Windows/Start Menu/Programs")
            .join(name);
        match fs::remove_file(shortcut) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(error).context("Could not remove the Workstation Start menu shortcut");
            }
        }
    }
    Ok(())
}

fn home() -> Result<PathBuf> {
    dirs::home_dir().context("Cannot locate your home directory.")
}

fn installation_lock(home: &Path) -> Result<fs::File> {
    let parent = home.join(".local/bin");
    fs::create_dir_all(&parent)?;
    let file = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(parent.join(".node-plane-install.lock"))?;
    fs2::FileExt::try_lock_exclusive(&file)
        .context("Another workstation installation/update is running")?;
    Ok(file)
}

pub fn update(release: &Release) -> Result<String> {
    let _lock = installation_lock(&home()?)?;
    let mut updater = dist_updater().context(
        "The cargo-dist install receipt is missing; reinstall Workstation with its release installer.",
    )?;
    updater.configure_version_specifier(axoupdater::UpdateRequest::SpecificTag(
        release.tag_name.clone(),
    ));
    let result = updater
        .run_sync()
        .context("cargo-dist could not update Workstation")?;
    let Some(result) = result else {
        return Ok("Workstation is already up to date.".into());
    };
    Ok(format!(
        "Workstation updated to {} · {}. Restart TUI to use the new version.",
        crate::config::workstation_channel(),
        result.new_version_tag.trim_start_matches('v')
    ))
}

pub fn execute(command: &Command) -> Result<()> {
    match command {
        Command::Uninstall => {
            uninstall_dist()?;
            println!(
                "Workstation uninstalled. Profiles, SSH keys and shared shell PATH entries are retained."
            );
        }
        Command::Update { yes } => {
            if let Some(release) = discover()? {
                if !yes {
                    ensure!(
                        crate::read_line(&format!(
                            "Update workstation to {}? [y/N]: ",
                            release.tag_name
                        ))?
                        .eq_ignore_ascii_case("y"),
                        "Update cancelled."
                    );
                }
                println!("{}", update(&release)?);
            } else {
                println!("Workstation is up to date.");
            }
        }
    }
    Ok(())
}

/// Reopen the saved installation catalog without replaying a workflow command.
pub fn restart_workstation(executable: &Path, state_dir: &Path) -> Result<()> {
    let mut command = std::process::Command::new(executable);
    command.arg("--state-dir").arg(state_dir);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        Err(command.exec()).context("Could not restart the workstation")
    }
    #[cfg(not(unix))]
    {
        let status = command
            .status()
            .context("Could not restart the workstation")?;
        ensure!(
            status.success(),
            "Restarted workstation exited with {status}"
        );
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn shortcut_removal_preserves_other_start_menu_entries() {
        let root = tempfile::tempdir().unwrap();
        let programs = root.path().join("Microsoft/Windows/Start Menu/Programs");
        std::fs::create_dir_all(&programs).unwrap();
        std::fs::write(programs.join("Node Plane Workstation.lnk"), "shortcut").unwrap();
        std::fs::write(programs.join("Other.lnk"), "other").unwrap();
        super::remove_start_menu_shortcut(root.path(), None).unwrap();
        assert!(!programs.join("Node Plane Workstation.lnk").exists());
        assert!(programs.join("Other.lnk").exists());
        super::remove_start_menu_shortcut(root.path(), None).unwrap();
    }

    use super::*;

    #[test]
    fn versions_respect_alpha_numbers_and_stable_order() {
        assert!(newer("v0.4.3-alpha.50", "0.4.3-alpha.49"));
        assert!(!newer("v0.4.3-alpha.9", "0.4.3-alpha.49"));
        assert!(newer("v0.4.3", "0.4.3-alpha.49"));
        assert!(!newer("garbage", "0.4.3"));
    }

    #[test]
    fn discovery_requires_complete_modern_artifacts_for_this_platform() {
        let mut release: Release = serde_json::from_value(serde_json::json!({
            "tag_name": "v0.4.3-alpha.99", "draft": false, "prerelease": true,
            "assets": [{"name": "node-plane-cli-linux-amd64.tar.gz"}]
        }))
        .unwrap();
        assert!(!has_platform_artifacts(&release));
        let archive = platform_archive();
        release.assets = vec![
            Asset {
                name: archive.clone(),
            },
            Asset {
                name: format!("{archive}.sha256"),
            },
        ];
        assert!(!has_platform_artifacts(&release));
        release.assets.push(Asset {
            name: "dist-manifest.json".into(),
        });
        assert!(has_platform_artifacts(&release));
    }
    #[test]
    fn discovery_never_crosses_channels_even_when_stable_is_newer() {
        fn complete(tag: &str, pre: bool) -> Release {
            let mut release: Release = serde_json::from_value(
                serde_json::json!({"tag_name":tag,"draft":false,"prerelease":pre}),
            )
            .unwrap();
            let archive = platform_archive();
            release.assets = [
                archive.clone(),
                format!("{archive}.sha256"),
                "dist-manifest.json".into(),
            ]
            .into_iter()
            .map(|name| Asset { name })
            .collect();
            release
        }
        let releases = vec![complete("v1.0.0-alpha.4", true), complete("v1.0.0", false)];
        assert_eq!(
            select_release(releases.clone(), "1.0.0-alpha.3")
                .unwrap()
                .tag_name,
            "v1.0.0-alpha.4"
        );
        assert_eq!(
            select_release(releases, "0.9.0").unwrap().tag_name,
            "v1.0.0"
        );
        assert!(select_release(vec![complete("v1.0.0", false)], "1.0.0-alpha.3").is_none());
        assert!(select_release(vec![complete("v2.0.0-alpha.1", true)], "1.0.0").is_none());
    }
    #[test]
    fn channel_shortcut_uninstall_preserves_the_other_channel() {
        let root = tempfile::tempdir().unwrap();
        let programs = root.path().join("Microsoft/Windows/Start Menu/Programs");
        fs::create_dir_all(&programs).unwrap();
        for channel in ["dev", "stable"] {
            fs::write(
                programs.join(format!("Node Plane Workstation ({channel}).lnk")),
                "fixture",
            )
            .unwrap();
        }
        remove_start_menu_shortcut(root.path(), Some("dev")).unwrap();
        assert!(!programs.join("Node Plane Workstation (dev).lnk").exists());
        assert!(
            programs
                .join("Node Plane Workstation (stable).lnk")
                .exists()
        );
    }
}
