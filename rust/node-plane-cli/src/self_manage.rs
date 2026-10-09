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
    let current = env!("CARGO_PKG_VERSION");
    let alpha = current.contains("-alpha.");
    let releases: Vec<Release> = client()?
        .get(format!("{REPO}/releases?per_page=100"))
        .send()?
        .error_for_status()?
        .json()?;
    Ok(releases
        .into_iter()
        .filter_map(|mut release| {
            if release.draft
                || (!alpha && release.prerelease)
                || !newer(&release.tag_name, current)
                || !has_platform_artifacts(&release)
            {
                return None;
            }
            release.dist_managed = true;
            Some(release)
        })
        .max_by_key(|r| semver::Version::parse(r.tag_name.trim_start_matches('v')).ok()))
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
    self_replace::self_delete()?;
    #[cfg(not(windows))]
    fs::remove_file(executable)?;

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
        "Workstation updated to {}. Restart TUI to use the new version.",
        result.new_version_tag
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

#[cfg(test)]
mod tests {
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
}
