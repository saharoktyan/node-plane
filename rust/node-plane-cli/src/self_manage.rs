use anyhow::{Context, Result, ensure};
use clap::Subcommand;
use reqwest::blocking::Client;
use serde::Deserialize;
use std::{
    collections::HashMap,
    fs,
    io::Read,
    path::{Path, PathBuf},
    time::Duration,
};

const REPO: &str = "https://api.github.com/repos/saharoktyan/node-plane";
const DIST_APP: &str = "node-plane-cli";

#[derive(Subcommand)]
pub enum Command {
    /// Remove the managed binary; retain profiles and SSH keys.
    Uninstall,
    /// Check the matching stable/development channel and update the managed binary.
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
    #[serde(skip)]
    channel_verified: bool,
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
    let client = client()?;
    let mut releases = Vec::new();
    for page in 1..=20 {
        let items: Vec<Release> = client
            .get(format!("{REPO}/releases?per_page=100&page={page}"))
            .send()?
            .error_for_status()?
            .json()?;
        let count = items.len();
        releases.extend(items);
        if count < 100 {
            break;
        }
    }
    for release in &mut releases {
        if !eligible(release, env!("CARGO_PKG_VERSION"))
            || !release
                .assets
                .iter()
                .any(|a| a.name == "RELEASE_METADATA.txt")
        {
            continue;
        }
        let response = client.get(format!("https://github.com/saharoktyan/node-plane/releases/download/{}/RELEASE_METADATA.txt", release.tag_name))
            .send()?.error_for_status()?;
        let mut metadata = String::new();
        response.take(4097).read_to_string(&mut metadata)?;
        release.channel_verified = valid_metadata(
            &metadata,
            &release.tag_name,
            crate::config::default_branch(),
        );
    }
    Ok(select_release(releases, env!("CARGO_PKG_VERSION")))
}

fn valid_metadata(metadata: &str, tag: &str, branch: &str) -> bool {
    if metadata.len() > 4096 {
        return false;
    }
    let mut fields = HashMap::new();
    for line in metadata.lines() {
        let Some((key, value)) = line.split_once('=') else {
            return false;
        };
        if key.is_empty() || fields.insert(key, value).is_some() {
            return false;
        }
    }
    let channel = if branch == "main" { "stable" } else { branch };
    fields.get("format") == Some(&"2")
        && fields.get("channel") == Some(&channel)
        && fields.get("tag") == Some(&tag)
        && fields.get("version") == Some(&tag.strip_prefix('v').unwrap_or(""))
        && fields.get("commit").is_some_and(|s| {
            s.len() == 40
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
        && tag.starts_with('v')
        && tag
            .strip_prefix('v')
            .and_then(|v| crate::config::version_branch(v).ok())
            == Some(branch)
}

fn eligible(release: &Release, current: &str) -> bool {
    let Ok(branch) = crate::config::version_branch(current) else {
        return false;
    };
    !release.draft
        && release.prerelease == (branch != "main")
        && release.tag_name.starts_with('v')
        && release
            .tag_name
            .strip_prefix('v')
            .and_then(|v| crate::config::version_branch(v).ok())
            == Some(branch)
        && newer(&release.tag_name, current)
        && has_platform_artifacts(release)
}

fn select_release(releases: Vec<Release>, current: &str) -> Option<Release> {
    releases
        .into_iter()
        .filter_map(|mut release| {
            if !release.channel_verified || !eligible(&release, current) {
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

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ChannelStatus {
    Found(PathBuf),
    NotInstalled,
    Error(String),
}

impl ChannelStatus {
    #[allow(dead_code)]
    pub fn is_found(&self) -> bool {
        matches!(self, Self::Found(_))
    }

    pub fn label(&self) -> String {
        match self {
            Self::Found(_) => "Found".into(),
            Self::NotInstalled => "Not installed".into(),
            Self::Error(err) => format!("Error: {err}"),
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct InstallationRoots {
    pub home: Option<PathBuf>,
    pub config: Option<PathBuf>,
    pub data: Option<PathBuf>,
}

pub fn detect_channel_installation(
    channel: &str,
    roots: Option<&InstallationRoots>,
) -> ChannelStatus {
    let home = roots.and_then(|r| r.home.clone()).or_else(dirs::home_dir);
    let config = roots.and_then(|r| r.config.clone()).or_else(|| {
        std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .or_else(|| {
                if cfg!(windows) {
                    std::env::var_os("LOCALAPPDATA").map(PathBuf::from)
                } else {
                    home.as_ref().map(|p| p.join(".config"))
                }
            })
    });
    // Installers use HOME/.local/share on every platform, independently of
    // the platform's application-data directory used for saved profiles.
    let install_root = home.as_ref().map(|p| p.join(".local/share"));
    let injected_data = roots.and_then(|r| r.data.as_ref());

    let exe_name = if cfg!(windows) {
        "node-plane.exe"
    } else {
        "node-plane"
    };
    let launcher_name = crate::config::channel_launcher(channel);

    // Startup scopes XDG_CONFIG_HOME to this channel for cargo-dist. Recover
    // the shared root before looking up a sibling channel's receipt.
    if let Some(config_dir) = config {
        let config_dir = if config_dir.parent().and_then(Path::file_name)
            == Some(std::ffi::OsStr::new("node-plane-workstation"))
            && matches!(
                config_dir.file_name().and_then(|s| s.to_str()),
                Some("stable" | "beta" | "dev")
            ) {
            config_dir
                .parent()
                .and_then(Path::parent)
                .unwrap()
                .to_path_buf()
        } else {
            config_dir
        };
        let receipt_path = config_dir
            .join("node-plane-workstation")
            .join(channel)
            .join(DIST_APP)
            .join(format!("{DIST_APP}-receipt.json"));
        match fs::read(&receipt_path) {
            Ok(bytes) => {
                let value: serde_json::Value = match serde_json::from_slice(&bytes) {
                    Ok(value) => value,
                    Err(error) => {
                        return ChannelStatus::Error(format!(
                            "Cannot read install receipt: {error}"
                        ));
                    }
                };
                let Some(prefix) = value["install_prefix"].as_str() else {
                    return ChannelStatus::Error("Install receipt has no installation path".into());
                };
                let candidate = PathBuf::from(prefix).join(exe_name);
                if installed_executable(&candidate) {
                    return ChannelStatus::Found(candidate);
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return ChannelStatus::Error(format!("Cannot read install receipt: {error}"));
            }
        }
    }

    for root in install_root.as_ref().into_iter().chain(injected_data) {
        let candidate = root
            .join("node-plane-workstation")
            .join(channel)
            .join(exe_name);
        if installed_executable(&candidate) {
            return ChannelStatus::Found(candidate);
        }
    }
    // Named Unix launchers also identify custom/source installations. Broken
    // symlinks and files without executable permissions do not count.
    #[cfg(unix)]
    if let Some(home_dir) = home {
        let candidate = home_dir.join(".local/bin").join(launcher_name);
        if installed_executable(&candidate) {
            return ChannelStatus::Found(candidate);
        }
    }
    #[cfg(not(unix))]
    let _ = launcher_name;

    ChannelStatus::NotInstalled
}

fn installed_executable(path: &Path) -> bool {
    let Ok(metadata) = fs::metadata(path) else {
        return false;
    };
    if !metadata.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        metadata.permissions().mode() & 0o111 != 0
    }
    #[cfg(not(unix))]
    {
        true
    }
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
        let launcher = crate::config::workstation_launcher();
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
        let name = format!("{}.cmd", crate::config::workstation_launcher());
        for launcher in [parent.join(&name), parent.join("launchers").join(&name)] {
            if launcher.is_file() {
                fs::remove_file(launcher)?;
            }
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
            } else if channel == "beta" {
                "Node Plane (Beta).lnk".into()
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
    ensure!(
        release.channel_verified && eligible(release, env!("CARGO_PKG_VERSION")),
        "Release is outside this Workstation's verified update channel"
    );
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
    fn versions_respect_prerelease_numbers_and_stable_order() {
        assert!(newer("v0.4.3-alpha.50", "0.4.3-alpha.49"));
        assert!(!newer("v0.4.3-alpha.9", "0.4.3-alpha.49"));
        assert!(newer("v0.4.3", "0.4.3-alpha.49"));
        assert!(!newer("garbage", "0.4.3"));
        assert!(newer("v1.0.0-beta.1", "1.0.0-alpha.99"));
        assert!(newer("v1.0.0-beta.11", "1.0.0-beta.2"));
        assert!(newer("v1.0.0-rc.1", "1.0.0-beta.99"));
        assert!(newer("v1.0.0-rc.10", "1.0.0-rc.2"));
        assert!(newer("v1.0.0", "1.0.0-rc.10"));
    }

    #[test]
    fn metadata_requires_exact_channel_and_rejects_legacy_and_duplicate_fields() {
        let tag = "v0.5.0-beta.2";
        let metadata = format!(
            "format=2\nchannel=beta\ntag={tag}\nversion=0.5.0-beta.2\ncommit={}\n",
            "a".repeat(40)
        );
        assert!(valid_metadata(&metadata, tag, "beta"));
        assert!(!valid_metadata(&metadata, tag, "dev"));
        assert!(!valid_metadata(&metadata, "v0.5.0-beta.3", "beta"));
        assert!(!valid_metadata(
            &(metadata.clone() + "channel=beta\n"),
            tag,
            "beta"
        ));
        assert!(!valid_metadata(
            "tag=v1.0.0-beta.11\ncommit=ce88545\n",
            "v1.0.0-beta.11",
            "beta"
        ));
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
            release.channel_verified = true;
            release
        }
        let candidates = vec![
            complete("v1.0.0-beta.12", true),
            complete("v1.0.0-rc.2", true),
            complete("v1.0.0-rc.10", true),
            complete("v1.0.0", false),
        ];
        assert_eq!(
            select_release(candidates, "1.0.0-beta.11")
                .unwrap()
                .tag_name,
            "v1.0.0-rc.10"
        );
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
        let mut old = complete("v1.0.0-beta.11", true);
        old.channel_verified = false;
        assert_eq!(
            select_release(
                vec![
                    old,
                    complete("v0.5.0-beta.2", true),
                    complete("v0.5.0-alpha.99", true)
                ],
                "0.5.0-beta.1"
            )
            .unwrap()
            .tag_name,
            "v0.5.0-beta.2"
        );
    }
    #[test]
    fn channel_shortcut_uninstall_preserves_the_other_channel() {
        let root = tempfile::tempdir().unwrap();
        let programs = root.path().join("Microsoft/Windows/Start Menu/Programs");
        fs::create_dir_all(&programs).unwrap();
        for channel in ["dev", "beta", "stable"] {
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
