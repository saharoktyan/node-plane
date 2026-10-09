use anyhow::{Context, Result, ensure};
use clap::Subcommand;
use reqwest::blocking::Client;
use serde::Deserialize;
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    time::Duration,
};

const REPO: &str = "https://api.github.com/repos/saharoktyan/node-plane";
const LEGACY_ASSET: &str = "node-plane-cli-linux-amd64.tar.gz";
const LEGACY_MEMBER: &str = "node-plane-cli-linux-amd64";
const DIST_APP: &str = "node-plane-cli";
const MARKER: &str = "# Node Plane workstation PATH";
const PATH_BLOCK: &str = "# Node Plane workstation PATH\nexport PATH=\"$HOME/.local/bin:$PATH\"\n# End Node Plane workstation PATH\n";

#[derive(Subcommand)]
pub enum Command {
    /// Install this binary in ~/.local/bin and configure shell PATH.
    Install,
    /// Remove the managed binary and PATH block; retain profiles and SSH keys.
    Uninstall,
    /// Check the matching stable/alpha channel and atomically replace the managed binary.
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
    assets: Vec<Asset>,
    #[serde(skip)]
    pub dist_managed: bool,
    #[serde(skip)]
    asset_name: String,
    #[serde(skip)]
    checksum_name: String,
}
#[derive(Clone, Deserialize)]
struct Asset {
    name: String,
    browser_download_url: String,
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
    let dist_managed = dist_updater().is_some();
    let platform_asset = platform_archive();
    let releases: Vec<Release> = client()?
        .get(format!("{REPO}/releases?per_page=100"))
        .send()?
        .error_for_status()?
        .json()?;
    Ok(releases
        .into_iter()
        .filter_map(|mut release| {
            if release.draft || (!alpha && release.prerelease) || !newer(&release.tag_name, current)
            {
                return None;
            }
            let target_asset = platform_asset
                .as_ref()
                .filter(|asset| release.assets.iter().any(|a| &a.name == *asset));
            if dist_managed {
                let name = target_asset?;
                let checksum = format!("{name}.sha256");
                release.assets.iter().any(|a| a.name == checksum).then(|| {
                    release.dist_managed = true;
                    release.asset_name = name.clone();
                    release.checksum_name = checksum;
                    release
                })
            } else if let Some(name) = target_asset {
                let checksum = format!("{name}.sha256");
                if release.assets.iter().any(|a| a.name == checksum) {
                    release.asset_name = name.clone();
                    release.checksum_name = checksum;
                    return Some(release);
                }
                legacy_release(&mut release)
            } else {
                legacy_release(&mut release)
            }
        })
        .max_by_key(|r| semver::Version::parse(r.tag_name.trim_start_matches('v')).ok()))
}

fn legacy_release(release: &mut Release) -> Option<Release> {
    if !cfg!(all(target_os = "linux", target_arch = "x86_64"))
        || !release.assets.iter().any(|a| a.name == LEGACY_ASSET)
        || !release.assets.iter().any(|a| a.name == "SHA256SUMS.txt")
    {
        return None;
    }
    release.asset_name = LEGACY_ASSET.into();
    release.checksum_name = "SHA256SUMS.txt".into();
    Some(release.clone())
}

fn platform_archive() -> Option<String> {
    let target = if cfg!(all(target_os = "linux", target_arch = "x86_64")) {
        "x86_64-unknown-linux-gnu"
    } else if cfg!(all(target_os = "macos", target_arch = "x86_64")) {
        "x86_64-apple-darwin"
    } else if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        "aarch64-apple-darwin"
    } else if cfg!(all(target_os = "windows", target_arch = "x86_64")) {
        "x86_64-pc-windows-msvc"
    } else {
        return None;
    };
    let extension = if cfg!(windows) { "zip" } else { "tar.gz" };
    Some(format!("{DIST_APP}-{target}.{extension}"))
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
fn destination(home: &Path) -> PathBuf {
    home.join(".local/bin/node-plane")
}
fn receipt(home: &Path) -> PathBuf {
    home.join(".local/bin/.node-plane-workstation")
}
fn hash(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    format!("{:x}", Sha256::digest(bytes))
}
fn atomic(path: &Path, bytes: &[u8], executable: bool) -> Result<()> {
    #[cfg(not(unix))]
    let _ = executable;
    let parent = path.parent().context("Missing destination directory")?;
    fs::create_dir_all(parent)?;
    let mut tmp = tempfile::NamedTempFile::new_in(parent)?;
    tmp.write_all(bytes)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        tmp.as_file()
            .set_permissions(fs::Permissions::from_mode(if executable {
                0o755
            } else {
                0o600
            }))?;
    }
    tmp.as_file().sync_all()?;
    tmp.persist(path).map_err(|e| e.error)?;
    Ok(())
}
fn managed(home: &Path) -> Result<()> {
    let installed = fs::read(destination(home)).context("Run `node-plane self install` first.")?;
    ensure!(
        fs::read_to_string(receipt(home))?.trim() == hash(&installed),
        "The installed binary was changed outside Node Plane; refusing to overwrite or remove it."
    );
    Ok(())
}
fn shell_paths(home: &Path) -> Vec<PathBuf> {
    let mut paths = vec![home.join(".profile")];
    for name in [".bashrc", ".zshrc"] {
        let path = home.join(name);
        if path.exists()
            || (name == ".zshrc" && std::env::var("SHELL").unwrap_or_default().ends_with("zsh"))
        {
            paths.push(path);
        }
    }
    paths
}
fn configure_path(home: &Path, install: bool) -> Result<()> {
    for path in shell_paths(home) {
        let original = match fs::read_to_string(&path) {
            Ok(s) => s,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
            Err(e) => return Err(e.into()),
        };
        let mut updated = original.replace(PATH_BLOCK, "");
        ensure!(
            !updated.contains(MARKER),
            "Unrecognized Node Plane PATH block in {}",
            path.display()
        );
        if install {
            if !updated.is_empty() && !updated.ends_with('\n') {
                updated.push('\n');
            }
            updated.push_str(PATH_BLOCK);
        }
        if original != updated {
            // Preserve permissions and symlink targets of existing shell configuration.
            fs::write(&path, updated)?;
        }
    }
    Ok(())
}
fn install_at(home: &Path, bytes: &[u8]) -> Result<()> {
    let _lock = installation_lock(home)?;
    let path = destination(home);
    if path.exists() {
        managed(home)?;
    }
    atomic(&path, bytes, true)?;
    atomic(&receipt(home), hash(bytes).as_bytes(), false)?;
    configure_path(home, true)
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
    if release.dist_managed {
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
        return Ok(format!(
            "Workstation updated to {}. Restart TUI to use the new version.",
            result.new_version_tag
        ));
    }
    let home = home()?;
    managed(&home)?;
    let client = client()?;
    let download = |name: &str, limit: u64| -> Result<Vec<u8>> {
        let asset = release
            .assets
            .iter()
            .find(|a| a.name == name)
            .context("Missing release asset")?;
        ensure!(
            asset
                .browser_download_url
                .starts_with("https://github.com/saharoktyan/node-plane/releases/download/"),
            "Unexpected asset download origin"
        );
        let response = client
            .get(&asset.browser_download_url)
            .send()?
            .error_for_status()?;
        let mut bytes = Vec::new();
        response.take(limit + 1).read_to_end(&mut bytes)?;
        ensure!(
            bytes.len() as u64 <= limit,
            "Release asset exceeds size limit"
        );
        Ok(bytes)
    };
    let checksum = String::from_utf8(download(&release.checksum_name, 1024 * 1024)?)?;
    let archive = download(&release.asset_name, 100 * 1024 * 1024)?;
    let binary = if release.asset_name == LEGACY_ASSET {
        verified_binary(&archive, &checksum)?
    } else {
        verified_dist_linux_binary(&archive, &checksum, &release.asset_name)?
    };
    install_at(&home, &binary)?;
    Ok(format!(
        "Workstation updated to {}. Restart TUI to use the new version.",
        release.tag_name
    ))
}
fn verified_binary(archive: &[u8], sums: &str) -> Result<Vec<u8>> {
    let expected = sums
        .lines()
        .find_map(|line| {
            let mut fields = line.split_whitespace();
            let checksum = fields.next()?;
            (fields.next()?.trim_start_matches('*') == LEGACY_ASSET).then_some(checksum)
        })
        .context("Workstation checksum missing")?;
    ensure!(
        hash(archive) == expected,
        "Workstation archive checksum mismatch"
    );
    let mut tar = tar::Archive::new(flate2::read::GzDecoder::new(archive));
    let mut binary = None;
    for entry in tar.entries()? {
        let entry = entry?;
        ensure!(
            entry.path()?.as_ref() == Path::new(LEGACY_MEMBER)
                && entry.header().entry_type().is_file()
                && binary.is_none(),
            "Unexpected workstation archive member"
        );
        let mut bytes = Vec::new();
        entry.take(100 * 1024 * 1024 + 1).read_to_end(&mut bytes)?;
        ensure!(
            bytes.len() <= 100 * 1024 * 1024 && bytes.starts_with(b"\x7fELF"),
            "Invalid workstation binary"
        );
        binary = Some(bytes);
    }
    binary.context("Empty workstation archive")
}

fn verified_dist_linux_binary(
    archive: &[u8],
    checksum: &str,
    archive_name: &str,
) -> Result<Vec<u8>> {
    ensure!(
        cfg!(all(target_os = "linux", target_arch = "x86_64")),
        "Only cargo-dist managed installs can update Workstation on this platform."
    );
    let expected = checksum
        .split_whitespace()
        .next()
        .context("Workstation archive checksum is empty")?;
    ensure!(
        expected.len() == 64 && expected.bytes().all(|c| c.is_ascii_hexdigit()),
        "Invalid Workstation archive checksum"
    );
    ensure!(
        hash(archive) == expected,
        "Workstation archive checksum mismatch"
    );
    let root = archive_name.trim_end_matches(".tar.gz");
    let member = format!("{root}/node-plane");
    let mut tar = tar::Archive::new(flate2::read::GzDecoder::new(archive));
    let mut binary = None;
    for entry in tar.entries()? {
        let entry = entry?;
        if entry.header().entry_type().is_dir() && entry.path()?.as_ref() == Path::new(root) {
            continue;
        }
        ensure!(
            entry.path()?.as_ref() == Path::new(&member)
                && entry.header().entry_type().is_file()
                && binary.is_none(),
            "Unexpected Workstation archive member"
        );
        let mut bytes = Vec::new();
        entry.take(100 * 1024 * 1024 + 1).read_to_end(&mut bytes)?;
        ensure!(
            bytes.len() <= 100 * 1024 * 1024 && bytes.starts_with(b"\x7fELF"),
            "Invalid Workstation binary"
        );
        binary = Some(bytes);
    }
    binary.context("Workstation archive did not contain the expected executable")
}
pub fn execute(command: &Command) -> Result<()> {
    let home = home()?;
    match command {
        Command::Install => {
            if dist_updater().is_some() {
                println!(
                    "Workstation is already installed. Run `node-plane self update` to update it."
                );
                return Ok(());
            }
            ensure!(
                cfg!(target_os = "linux"),
                "Use the generated release installer to install Workstation on this platform."
            );
            install_at(&home, &fs::read(std::env::current_exe()?)?)?;
            println!(
                "Installed {}. Open a new terminal to refresh PATH. Profiles and SSH keys are retained.",
                destination(&home).display()
            );
        }
        Command::Uninstall => {
            if dist_updater().is_some() {
                uninstall_dist()?;
                println!(
                    "Workstation uninstalled. Profiles, SSH keys and shared shell PATH entries are retained."
                );
                return Ok(());
            }
            ensure!(
                cfg!(target_os = "linux"),
                "Remove Workstation using the uninstall instructions for the generated release installer."
            );
            let _lock = installation_lock(&home)?;
            managed(&home)?;
            configure_path(&home, false)?;
            fs::remove_file(destination(&home))?;
            fs::remove_file(receipt(&home))?;
            println!(
                "Workstation uninstalled. Profiles, SSH keys and server installations are retained."
            );
        }
        Command::Update { yes } => {
            if dist_updater().is_none() {
                managed(&home)?;
            }
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
    fn archive(member: &str, binary: &[u8]) -> Vec<u8> {
        let encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        let mut builder = tar::Builder::new(encoder);
        let mut header = tar::Header::new_gnu();
        header.set_size(binary.len() as u64);
        header.set_mode(0o755);
        header.set_cksum();
        builder.append_data(&mut header, member, binary).unwrap();
        builder.into_inner().unwrap().finish().unwrap()
    }
    #[test]
    fn release_verification_rejects_tampering_and_unexpected_members() {
        let valid = archive(LEGACY_MEMBER, b"\x7fELFtest");
        let sums = format!("{}  {}\n", hash(&valid), LEGACY_ASSET);
        assert_eq!(verified_binary(&valid, &sums).unwrap(), b"\x7fELFtest");
        assert!(
            verified_binary(&valid, &format!("{}  {}\n", "0".repeat(64), LEGACY_ASSET)).is_err()
        );
        assert!(verified_binary(&valid, "").is_err());
        for invalid in [
            archive("wrong-binary", b"\x7fELFtest"),
            archive(LEGACY_MEMBER, b"not executable"),
        ] {
            assert!(
                verified_binary(&invalid, &format!("{}  {}\n", hash(&invalid), LEGACY_ASSET))
                    .is_err()
            );
        }
    }
    #[test]
    fn versions_respect_alpha_numbers_and_stable_order() {
        assert!(newer("v0.4.3-alpha.50", "0.4.3-alpha.49"));
        assert!(!newer("v0.4.3-alpha.9", "0.4.3-alpha.49"));
        assert!(newer("v0.4.3", "0.4.3-alpha.49"));
        assert!(!newer("garbage", "0.4.3"));
    }
    #[test]
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    fn cargo_dist_archive_checks_its_root_checksum_and_executable() {
        let name = "node-plane-cli-x86_64-unknown-linux-gnu.tar.gz";
        let member = "node-plane-cli-x86_64-unknown-linux-gnu/node-plane";
        let valid = archive(member, b"\x7fELFtest");
        let checksum = format!("{}  {name}\n", hash(&valid));
        assert_eq!(
            verified_dist_linux_binary(&valid, &checksum, name).unwrap(),
            b"\x7fELFtest"
        );
        assert!(verified_dist_linux_binary(&valid, &"0".repeat(64), name).is_err());
        for (path, bytes) in [
            ("unexpected/node-plane", b"\x7fELFtest".as_slice()),
            (member, b"not executable".as_slice()),
        ] {
            let invalid = archive(path, bytes);
            assert!(verified_dist_linux_binary(&invalid, &hash(&invalid), name).is_err());
        }
    }
    #[test]
    fn install_is_repeatable_and_preserves_shell_content_and_user_data() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join(".profile"), "# user settings\n").unwrap();
        install_at(dir.path(), b"first").unwrap();
        install_at(dir.path(), b"second").unwrap();
        let shell = fs::read_to_string(dir.path().join(".profile")).unwrap();
        assert_eq!(shell.matches(MARKER).count(), 1);
        assert!(shell.starts_with("# user settings\n"));
        managed(dir.path()).unwrap();
        configure_path(dir.path(), false).unwrap();
        assert_eq!(
            fs::read_to_string(dir.path().join(".profile")).unwrap(),
            "# user settings\n"
        );
        fs::write(destination(dir.path()), b"foreign").unwrap();
        assert!(managed(dir.path()).is_err());
        assert!(install_at(dir.path(), b"replacement").is_err());
    }
}
