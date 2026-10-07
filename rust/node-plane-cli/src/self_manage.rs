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
const ASSET: &str = "node-plane-cli-linux-amd64.tar.gz";
const MEMBER: &str = "node-plane-cli-linux-amd64";
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
    ensure!(
        cfg!(all(target_os = "linux", target_arch = "x86_64")),
        "Self-update currently supports Linux x86_64 only."
    );
    let current = env!("CARGO_PKG_VERSION");
    let alpha = current.contains("-alpha.");
    let releases: Vec<Release> = client()?
        .get(format!("{REPO}/releases?per_page=100"))
        .send()?
        .error_for_status()?
        .json()?;
    Ok(releases
        .into_iter()
        .filter(|r| {
            !r.draft
                && (alpha || !r.prerelease)
                && newer(&r.tag_name, current)
                && r.assets.iter().any(|a| a.name == ASSET)
                && r.assets.iter().any(|a| a.name == "SHA256SUMS.txt")
        })
        .max_by_key(|r| semver::Version::parse(r.tag_name.trim_start_matches('v')).ok()))
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
    let sums = String::from_utf8(download("SHA256SUMS.txt", 1024 * 1024)?)?;
    let archive = download(ASSET, 100 * 1024 * 1024)?;
    install_at(&home, &verified_binary(&archive, &sums)?)?;
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
            (fields.next()?.trim_start_matches('*') == ASSET).then_some(checksum)
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
            entry.path()?.as_ref() == Path::new(MEMBER)
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
pub fn execute(command: &Command) -> Result<()> {
    ensure!(
        cfg!(target_os = "linux"),
        "Local installation currently supports Linux only."
    );
    let home = home()?;
    match command {
        Command::Install => {
            install_at(&home, &fs::read(std::env::current_exe()?)?)?;
            println!(
                "Installed {}. Open a new terminal to refresh PATH. Profiles and SSH keys are retained.",
                destination(&home).display()
            );
        }
        Command::Uninstall => {
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
            managed(&home)?;
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
        let valid = archive(MEMBER, b"\x7fELFtest");
        let sums = format!("{}  {}\n", hash(&valid), ASSET);
        assert_eq!(verified_binary(&valid, &sums).unwrap(), b"\x7fELFtest");
        assert!(verified_binary(&valid, &format!("{}  {}\n", "0".repeat(64), ASSET)).is_err());
        assert!(verified_binary(&valid, "").is_err());
        for invalid in [
            archive("wrong-binary", b"\x7fELFtest"),
            archive(MEMBER, b"not executable"),
        ] {
            assert!(
                verified_binary(&invalid, &format!("{}  {}\n", hash(&invalid), ASSET)).is_err()
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
