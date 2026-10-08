//! Embedded SSH with explicit host trust and a workstation-owned Ed25519 key.
//!
//! This module never invokes a local SSH executable or persists passwords.

use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, bail, ensure};
use fs2::FileExt;
use russh::client;
use russh::keys::{Algorithm, HashAlg, PrivateKey, PrivateKeyWithHashAlg, PublicKeyOrCertificate};
use russh::{ChannelMsg, Disconnect};
use tempfile::NamedTempFile;
use tokio::net::TcpStream;
use tokio::time::timeout;
use zeroize::Zeroizing;

const CONNECT_TIMEOUT: Duration = Duration::from_secs(15);
const AUTH_TIMEOUT: Duration = Duration::from_secs(30);
const COMMAND_IDLE_TIMEOUT: Duration = Duration::from_secs(600);
const KEY_COMMENT: &str = "node-plane-workstation";

pub struct SshOptions {
    pub host: String,
    pub port: u16,
    pub user: String,
    pub state_dir: PathBuf,
}

pub trait Interaction: Send + Sync {
    fn confirm_host_key(&self, host: &str, fingerprint: &str) -> Result<bool>;
    fn password(&self, user: &str, host: &str) -> Result<Zeroizing<String>>;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OutputStream {
    Stdout,
    Stderr,
}

struct HostVerifier {
    endpoint: String,
    state_dir: PathBuf,
    interaction: Arc<dyn Interaction>,
    verified_key: Arc<std::sync::Mutex<Option<String>>>,
}

impl client::Handler for HostVerifier {
    type Error = anyhow::Error;

    async fn check_server_key(&mut self, key: &PublicKeyOrCertificate) -> Result<bool> {
        ensure!(
            key.certificate().is_none(),
            "SSH host certificates are not supported yet"
        );
        let fingerprint = key.public_key().fingerprint(HashAlg::Sha256).to_string();
        let endpoint = self.endpoint.clone();
        let state_dir = self.state_dir.clone();
        let interaction = self.interaction.clone();
        // The terminal interaction and filesystem lock must not block Tokio's I/O thread.
        let accepted = tokio::task::spawn_blocking(move || {
            verify_fingerprint(&state_dir, &endpoint, &fingerprint, interaction.as_ref())
        })
        .await
        .context("Host confirmation task failed")??;
        if accepted {
            *self
                .verified_key
                .lock()
                .map_err(|_| anyhow::anyhow!("SSH host identity lock failed"))? =
                Some(key.public_key().to_openssh()?);
        }
        Ok(accepted)
    }
}

pub struct SshSession {
    session: client::Handle<HostVerifier>,
    server_key: String,
    pub ssh_user: String,
    pub workstation_fingerprint: String,
    pub endpoint: String,
}

/// Connect with our local key first. A password is requested only after verified
/// host identity and rejected key authentication, then the public key is enrolled
/// and a fresh connection must authenticate with it before returning success.
pub async fn connect(
    options: &SshOptions,
    interaction: Arc<dyn Interaction>,
) -> Result<SshSession> {
    validate_options(options)?;
    let state_dir = options.state_dir.clone();
    let key = Arc::new(
        tokio::task::spawn_blocking(move || load_or_create_key(&state_dir))
            .await
            .context("Local key task failed")??,
    );
    let workstation_fingerprint = key.public_key().fingerprint(HashAlg::Sha256).to_string();
    let (mut session, server_key) = open_connection(options, interaction.clone()).await?;
    if authenticate_key(&mut session, &options.user, key.clone()).await? {
        return Ok(SshSession {
            session,
            server_key,
            ssh_user: options.user.clone(),
            workstation_fingerprint,
            endpoint: endpoint(options),
        });
    }

    let user = options.user.clone();
    let endpoint = endpoint(options);
    let prompt = interaction.clone();
    let password = tokio::task::spawn_blocking(move || prompt.password(&user, &endpoint))
        .await
        .context("Password prompt task failed")??;
    ensure!(!password.is_empty(), "SSH password was not provided");
    // russh owns a transient String for its authentication packet. The UI/input
    // buffer is zeroized, and neither buffer is written to disk or application logs.
    let accepted = timeout(
        AUTH_TIMEOUT,
        session.authenticate_password(options.user.clone(), password.as_str()),
    )
    .await
    .context("SSH password authentication timed out")?
    .context("SSH password authentication failed")?
    .success();
    drop(password);
    ensure!(
        accepted,
        "SSH password was rejected. Check the user, password and server login policy"
    );

    let public_key = key.public_key().to_openssh()?;
    let mut initial = SshSession {
        session,
        server_key,
        ssh_user: options.user.clone(),
        workstation_fingerprint: workstation_fingerprint.clone(),
        endpoint: self::endpoint(options),
    };
    let command = initial.audited_enrollment_command(
        &public_key,
        "workstation-key-enrollment",
        uuid::Uuid::new_v4(),
    )?;
    let code = initial.run(&command, None, |_, _| {}).await.context(
        "Could not add the workstation public key. Check the SSH home directory and permissions",
    )?;
    ensure!(
        code == 0,
        "Could not add the workstation public key (exit {code}). Check the SSH home directory and permissions"
    );

    // Test a completely new SSH authentication; successful file creation alone
    // does not prove sshd reads this authorized_keys file or permits public keys.
    let (mut verified, server_key) = open_connection(options, interaction).await?;
    let key_accepted = authenticate_key(&mut verified, &options.user, key).await?;
    let _ = initial.close().await;
    ensure!(
        key_accepted,
        "Public key was added, but a fresh SSH login rejected it. Check sshd AuthorizedKeysFile and public-key policy; the password was not saved"
    );
    Ok(SshSession {
        session: verified,
        server_key,
        ssh_user: options.user.clone(),
        workstation_fingerprint,
        endpoint: self::endpoint(options),
    })
}

fn validate_options(options: &SshOptions) -> Result<()> {
    ensure!(
        !options.host.is_empty() && options.host.len() <= 253,
        "Invalid SSH host"
    );
    ensure!(
        !options
            .host
            .chars()
            .any(|c| c.is_whitespace() || c.is_control()),
        "Invalid SSH host"
    );
    ensure!(
        !options.user.is_empty() && options.user.len() <= 64,
        "Invalid SSH user"
    );
    ensure!(
        options
            .user
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "_.-".contains(c)),
        "Invalid SSH user"
    );
    ensure!(options.port != 0, "SSH port must be between 1 and 65535");
    Ok(())
}

fn endpoint(options: &SshOptions) -> String {
    format!("[{}]:{}", options.host.to_ascii_lowercase(), options.port)
}

async fn open_connection(
    options: &SshOptions,
    interaction: Arc<dyn Interaction>,
) -> Result<(client::Handle<HostVerifier>, String)> {
    let stream = timeout(
        CONNECT_TIMEOUT,
        TcpStream::connect((options.host.as_str(), options.port)),
    )
    .await
    .context("SSH connection timed out. Check the address, port and firewall")?
    .context("Could not connect to SSH. Check the address, port and firewall")?;
    let config = Arc::new(client::Config {
        inactivity_timeout: Some(COMMAND_IDLE_TIMEOUT),
        keepalive_interval: Some(Duration::from_secs(15)),
        keepalive_max: 3,
        ..Default::default()
    });
    let verified_key = Arc::new(std::sync::Mutex::new(None));
    let verifier = HostVerifier {
        endpoint: endpoint(options),
        state_dir: options.state_dir.clone(),
        interaction,
        verified_key: verified_key.clone(),
    };
    // Includes first-use host confirmation. TCP itself has a separate short bound.
    let session = timeout(
        Duration::from_secs(300),
        client::connect_stream(config, stream, verifier),
    )
    .await
    .context("SSH handshake or host confirmation timed out")?
    .context("SSH handshake failed")?;
    let server_key = verified_key
        .lock()
        .map_err(|_| anyhow::anyhow!("SSH host identity lock failed"))?
        .clone()
        .context("SSH handshake did not establish a verified host key")?;
    Ok((session, server_key))
}

async fn authenticate_key(
    session: &mut client::Handle<HostVerifier>,
    user: &str,
    key: Arc<PrivateKey>,
) -> Result<bool> {
    Ok(timeout(
        AUTH_TIMEOUT,
        session.authenticate_publickey(user, PrivateKeyWithHashAlg::new(key, None)),
    )
    .await
    .context("SSH key authentication timed out")?
    .context("SSH key authentication failed")?
    .success())
}

impl SshSession {
    /// Exact OpenSSH host public key accepted by the workstation's trust store.
    /// This can pin a controller-side verification without trusting ssh-keyscan.
    pub fn server_key(&self) -> String {
        self.server_key.clone()
    }

    /// Access the controller API through SSH without opening a listening port on
    /// the workstation or exposing the API on the controller's public network.
    /// The destination is intentionally fixed: this is not a general forwarding API.
    pub async fn backend_stream(&self) -> Result<russh::ChannelStream<client::Msg>> {
        let channel = timeout(
            AUTH_TIMEOUT,
            self.session
                .channel_open_direct_tcpip("127.0.0.1", 8080, "127.0.0.1", 0),
        )
        .await
        .context("Timed out connecting to the controller API through SSH")?
        .context("Could not connect to the controller API through SSH. Check the backend service and sshd forwarding policy")?;
        Ok(channel.into_stream())
    }

    /// Add a controller's public key to this SSH user's authorized_keys while
    /// preserving every existing key. This does not establish that the controller
    /// can log in: a separate controller-side probe must verify that connection.
    pub async fn enroll_public_key(
        &mut self,
        public_key: &str,
        operation_id: uuid::Uuid,
    ) -> Result<()> {
        let command =
            self.audited_enrollment_command(public_key, "controller-key-enrollment", operation_id)?;
        let code = self.run(&command, None, |_, _| {}).await.context(
            "Could not add the controller public key. Check the SSH home directory and permissions",
        )?;
        ensure!(
            code == 0,
            "Could not add the controller public key (exit {code}). Check the SSH home directory and permissions"
        );
        Ok(())
    }

    fn audited_enrollment_command(
        &self,
        public_key: &str,
        action: &str,
        operation_id: uuid::Uuid,
    ) -> Result<String> {
        let command = enrollment_command(public_key)?;
        let key = russh::keys::PublicKey::from_openssh(public_key)?;
        let metadata = serde_json::json!({
            "operation_id": operation_id, "action": action,
            "ssh_user": self.ssh_user, "target": self.endpoint,
            "device_fingerprint": self.workstation_fingerprint,
            "key_fingerprint": key.fingerprint(HashAlg::Sha256).to_string(),
            "identity_source": "ssh_workstation"
        });
        Ok(format!(
            "/bin/bash -c {}",
            shell_quote(&crate::audit::script(&command, &metadata))
        ))
    }

    /// Stream both output channels and require a real remote exit status.
    /// Lost connections are unconfirmed outcomes and are never replayed here.
    pub async fn run(
        &mut self,
        command: &str,
        input: Option<&[u8]>,
        mut on_output: impl FnMut(OutputStream, &[u8]),
    ) -> Result<i32> {
        let mut channel = timeout(AUTH_TIMEOUT, self.session.channel_open_session())
            .await
            .context("Timed out opening an SSH command channel")?
            .context("Could not open an SSH command channel")?;
        timeout(AUTH_TIMEOUT, channel.exec(true, command))
            .await
            .context("Timed out starting the remote operation; its outcome is unconfirmed")?
            .context("Could not start the remote operation; its outcome is unconfirmed")?;
        if let Some(input) = input {
            timeout(AUTH_TIMEOUT, channel.data(input))
                .await
                .context("Timed out sending operation input; its outcome is unconfirmed")?
                .context("Could not send operation input; its outcome is unconfirmed")?;
        }
        channel
            .eof()
            .await
            .context("Could not finish operation input; its outcome is unconfirmed")?;
        let mut exit_status = None;
        loop {
            let msg = match timeout(COMMAND_IDLE_TIMEOUT, channel.wait()).await {
                Ok(msg) => msg,
                Err(_) => {
                    let _ = channel.close().await;
                    bail!(
                        "The remote operation stopped responding. Its outcome is unconfirmed; diagnose the server before retrying"
                    );
                }
            };
            match msg {
                Some(ChannelMsg::Data { data }) => on_output(OutputStream::Stdout, &data),
                Some(ChannelMsg::ExtendedData { data, .. }) => {
                    on_output(OutputStream::Stderr, &data)
                }
                Some(ChannelMsg::ExitStatus {
                    exit_status: status,
                }) => exit_status = Some(status),
                Some(ChannelMsg::ExitSignal { .. }) => bail!(
                    "The remote operation was terminated by a signal. Check the server before retrying"
                ),
                Some(ChannelMsg::Failure) => bail!("The SSH server refused the command"),
                Some(ChannelMsg::Close) | None => break,
                _ => {}
            }
        }
        let status = exit_status.context("SSH disconnected without an exit status. The operation's outcome is unconfirmed; diagnose the server before retrying")?;
        i32::try_from(status).context("SSH server returned an invalid exit status")
    }

    pub fn is_closed(&self) -> bool {
        self.session.is_closed()
    }

    pub async fn close(self) -> Result<()> {
        timeout(
            AUTH_TIMEOUT,
            self.session.disconnect(Disconnect::ByApplication, "", "en"),
        )
        .await
        .context("Timed out closing SSH")?
        .context("Could not close SSH")
    }
}

pub fn secure_state_directory(path: &Path) -> Result<()> {
    ensure!(
        path.file_name().is_some(),
        "Choose a dedicated SSH state directory, not a filesystem root"
    );
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            ensure!(
                metadata.is_dir() && !metadata.file_type().is_symlink(),
                "SSH state directory must be a real directory"
            );
            check_owner(&metadata)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                ensure!(
                    metadata.permissions().mode() & 0o022 == 0,
                    "SSH state directory is writable by other users; choose a private directory"
                );
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            fs::create_dir_all(path).context("Could not create SSH state directory")?;
        }
        Err(error) => return Err(error).context("Could not inspect SSH state directory"),
    }
    restrict_permissions(path, true)
}

/// Restrict an already-created log or other workstation state file.
pub fn secure_file(path: &Path) -> Result<()> {
    ensure!(check_regular(path)?, "Local state file is missing");
    Ok(())
}

fn check_regular(path: &Path) -> Result<bool> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            ensure!(
                metadata.is_file() && !metadata.file_type().is_symlink(),
                "SSH state file must be a regular file: {}",
                path.display()
            );
            check_owner(&metadata)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                ensure!(
                    metadata.nlink() == 1,
                    "Refusing a hard-linked SSH state file"
                );
            }
            restrict_permissions(path, false)?;
            Ok(true)
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error).context("Could not inspect SSH state file"),
    }
}

#[cfg(unix)]
fn check_owner(metadata: &fs::Metadata) -> Result<()> {
    use std::os::unix::fs::MetadataExt;
    unsafe extern "C" {
        fn geteuid() -> u32;
    }
    ensure!(
        metadata.uid() == unsafe { geteuid() },
        "SSH state is not owned by this user"
    );
    Ok(())
}

#[cfg(not(unix))]
fn check_owner(_metadata: &fs::Metadata) -> Result<()> {
    Ok(())
}

#[cfg(unix)]
fn restrict_permissions(path: &Path, directory: bool) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(
        path,
        fs::Permissions::from_mode(if directory { 0o700 } else { 0o600 }),
    )
    .context("Could not secure local SSH state permissions")
}

#[cfg(windows)]
fn restrict_permissions(path: &Path, _directory: bool) -> Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use std::ptr::null_mut;
    use windows_sys::Win32::Foundation::{CloseHandle, LocalFree};
    use windows_sys::Win32::Security::Authorization::{
        ConvertSidToStringSidW, ConvertStringSecurityDescriptorToSecurityDescriptorW,
        SDDL_REVISION_1,
    };
    use windows_sys::Win32::Security::{
        DACL_SECURITY_INFORMATION, GetTokenInformation, PROTECTED_DACL_SECURITY_INFORMATION,
        SetFileSecurityW, TOKEN_QUERY, TOKEN_USER, TokenUser,
    };
    use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

    // Protected ACL: only this Windows user and LocalSystem can access the state;
    // inherited broad ACLs on a custom state directory do not expose private keys.
    unsafe {
        let mut token = null_mut();
        ensure!(
            OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) != 0,
            "Could not query Windows user for SSH state ACL"
        );
        let mut length = 0;
        GetTokenInformation(token, TokenUser, null_mut(), 0, &mut length);
        let mut buffer = vec![0u64; (length as usize).div_ceil(8)];
        let loaded = GetTokenInformation(
            token,
            TokenUser,
            buffer.as_mut_ptr().cast(),
            length,
            &mut length,
        );
        CloseHandle(token);
        ensure!(loaded != 0, "Could not read Windows user for SSH state ACL");
        let user = &*(buffer.as_ptr().cast::<TOKEN_USER>());
        let mut sid = null_mut();
        ensure!(
            ConvertSidToStringSidW(user.User.Sid, &mut sid) != 0,
            "Could not encode Windows user SID"
        );
        let mut sid_len = 0;
        while *sid.add(sid_len) != 0 {
            sid_len += 1;
        }
        let sid_text = String::from_utf16(std::slice::from_raw_parts(sid, sid_len));
        LocalFree(sid.cast());
        let sid_text = sid_text.context("Invalid Windows user SID")?;
        let sddl: Vec<u16> = format!("D:P(A;OICI;FA;;;{sid_text})(A;OICI;FA;;;SY)")
            .encode_utf16()
            .chain(Some(0))
            .collect();
        let mut descriptor = null_mut();
        ensure!(
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
                SDDL_REVISION_1,
                &mut descriptor,
                null_mut()
            ) != 0,
            "Could not create private SSH state ACL"
        );
        let wide_path: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
        let applied = SetFileSecurityW(
            wide_path.as_ptr(),
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            descriptor,
        );
        LocalFree(descriptor.cast());
        ensure!(applied != 0, "Could not secure Windows SSH state ACL");
        Ok(())
    }
}

struct StateLock(File);

impl Drop for StateLock {
    fn drop(&mut self) {
        // Explicit unlock also releases a transient fork's inherited open-file
        // description, rather than waiting for that child to reach exec.
        let _ = FileExt::unlock(&self.0);
    }
}

fn state_lock(state_dir: &Path) -> Result<StateLock> {
    secure_state_directory(state_dir)?;
    let path = state_dir.join("ssh-state.lock");
    check_regular(&path)?;
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true).truncate(false);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let file = options
        .open(&path)
        .context("Could not open SSH state lock")?;
    restrict_permissions(&path, false)?;
    file.try_lock_exclusive()
        .context("Another Node Plane process is changing SSH state. Wait for it to finish")?;
    Ok(StateLock(file))
}

fn atomic_write(path: &Path, data: &[u8], overwrite: bool) -> Result<()> {
    check_regular(path)?;
    let parent = path.parent().context("SSH state path has no parent")?;
    let mut temp =
        NamedTempFile::new_in(parent).context("Could not create temporary SSH state file")?;
    restrict_permissions(temp.path(), false)?;
    temp.write_all(data).context("Could not write SSH state")?;
    temp.as_file()
        .sync_all()
        .context("Could not sync SSH state")?;
    if overwrite {
        temp.persist(path).map_err(|e| e.error)?;
    } else {
        temp.persist_noclobber(path).map_err(|e| e.error)?;
    }
    #[cfg(unix)]
    File::open(parent)?.sync_all()?;
    Ok(())
}

fn load_or_create_key(state_dir: &Path) -> Result<PrivateKey> {
    let _lock = state_lock(state_dir)?;
    let private_path = state_dir.join("id_ed25519");
    let public_path = state_dir.join("id_ed25519.pub");
    let key = if check_regular(&private_path)? {
        let data = Zeroizing::new(fs::read(&private_path).context("Could not read local SSH key")?);
        let key = PrivateKey::from_openssh(data.as_slice())
            .context("Local SSH key is invalid; it was not replaced")?;
        ensure!(
            key.algorithm() == Algorithm::Ed25519 && !key.is_encrypted(),
            "Local SSH key must be an unencrypted Ed25519 key"
        );
        key
    } else {
        ensure!(
            !check_regular(&public_path)?,
            "Public key exists without its private key. Restore the private key or choose a new SSH state directory"
        );
        let mut key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519)
            .context("Could not generate local Ed25519 key")?;
        key.set_comment(KEY_COMMENT);
        let encoded = key.to_openssh(russh::keys::ssh_key::LineEnding::LF)?;
        atomic_write(&private_path, encoded.as_bytes(), false)?;
        key
    };
    let public = format!("{}\n", key.public_key().to_openssh()?);
    if check_regular(&public_path)? {
        ensure!(
            fs::read_to_string(&public_path)?.trim_end() == public.trim_end(),
            "Public/private SSH key files do not match; neither was replaced"
        );
    } else {
        atomic_write(&public_path, public.as_bytes(), false)?;
    }
    Ok(key)
}

pub fn forget_host(state_dir: &Path, host: &str, port: u16) -> Result<()> {
    let _lock = state_lock(state_dir)?;
    let path = state_dir.join("known_hosts.json");
    if !check_regular(&path)? {
        return Ok(());
    }
    let mut hosts: BTreeMap<String, String> = serde_json::from_slice(&fs::read(&path)?)
        .context("Saved profile removed, but the invalid known-host file was left unchanged")?;
    if hosts
        .remove(&format!("[{}]:{port}", host.to_ascii_lowercase()))
        .is_some()
    {
        atomic_write(&path, &serde_json::to_vec_pretty(&hosts)?, true)?;
    }
    Ok(())
}

fn verify_fingerprint(
    state_dir: &Path,
    host: &str,
    fingerprint: &str,
    interaction: &dyn Interaction,
) -> Result<bool> {
    let _lock = state_lock(state_dir)?;
    let path = state_dir.join("known_hosts.json");
    let mut hosts: BTreeMap<String, String> = if check_regular(&path)? {
        serde_json::from_slice(&fs::read(&path)?)
            .context("SSH known-host file is invalid; refusing to reset host trust")?
    } else {
        BTreeMap::new()
    };
    if let Some(known) = hosts.get(host) {
        ensure!(
            known == fingerprint,
            "SSH host key changed for {host}. Refusing connection before authentication. Verify the server independently before editing known_hosts.json"
        );
        return Ok(true);
    }
    if !interaction.confirm_host_key(host, fingerprint)? {
        return Ok(false);
    }
    hosts.insert(host.to_owned(), fingerprint.to_owned());
    atomic_write(&path, &serde_json::to_vec_pretty(&hosts)?, true)?;
    Ok(true)
}

fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

fn enrollment_command(public_key: &str) -> Result<String> {
    ensure!(
        public_key.len() <= 4096
            && !public_key.is_empty()
            && !public_key.chars().any(char::is_control),
        "Public key must occupy one line and contain no control characters"
    );
    let parsed =
        russh::keys::PublicKey::from_openssh(public_key).context("Invalid SSH public key")?;
    ensure!(
        parsed.algorithm() == Algorithm::Ed25519,
        "SSH key must be Ed25519"
    );
    Ok(format!(
        "sh -c {} node-plane-key {}",
        shell_quote(ENROLLMENT_SCRIPT),
        shell_quote(public_key)
    ))
}

const ENROLLMENT_SCRIPT: &str = r#"
set -eu
umask 077
uid=$(id -u)
case "$HOME" in /*) ;; *) exit 71 ;; esac
[ -d "$HOME" ] && [ ! -L "$HOME" ] || exit 71
[ "$(stat -c '%u' -- "$HOME")" = "$uid" ] || exit 71
mode=$(stat -c '%a' -- "$HOME")
[ $((0$mode & 0022)) -eq 0 ] || exit 71
dir="$HOME/.ssh"
[ ! -L "$dir" ] || exit 72
if [ ! -e "$dir" ]; then mkdir -m 700 -- "$dir"; fi
[ -d "$dir" ] && [ "$(stat -c '%u' -- "$dir")" = "$uid" ] || exit 72
mode=$(stat -c '%a' -- "$dir")
[ $((0$mode & 0022)) -eq 0 ] || exit 72
keys="$dir/authorized_keys"
lock="$dir/.node-plane-key.lock"
[ ! -L "$lock" ] || exit 73
if [ -e "$lock" ]; then
    [ -f "$lock" ] && [ "$(stat -c '%u' -- "$lock")" = "$uid" ] && [ "$(stat -c '%h' -- "$lock")" = 1 ] || exit 73
fi
exec 9>>"$lock"
chmod 600 -- "$lock"
flock -x -w 15 9 || exit 74
[ ! -L "$keys" ] || exit 75
if [ -e "$keys" ]; then
    [ -f "$keys" ] && [ "$(stat -c '%u' -- "$keys")" = "$uid" ] && [ "$(stat -c '%h' -- "$keys")" = 1 ] || exit 75
    mode=$(stat -c '%a' -- "$keys")
    [ $((0$mode & 0022)) -eq 0 ] || exit 75
else
    (set -C; : > "$keys") || exit 75
fi
if ! grep -Fqx -- "$1" "$keys"; then printf '\n%s\n' "$1" >> "$keys"; fi
chmod 700 -- "$dir"
chmod 600 -- "$keys"
"#;

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[derive(Default)]
    struct Prompts {
        confirmations: AtomicUsize,
        passwords: AtomicUsize,
        reject_host: bool,
        wrong_password: bool,
    }

    impl Interaction for Prompts {
        fn confirm_host_key(&self, _host: &str, fingerprint: &str) -> Result<bool> {
            assert!(fingerprint.starts_with("SHA256:"));
            self.confirmations.fetch_add(1, Ordering::SeqCst);
            Ok(!self.reject_host)
        }
        fn password(&self, _user: &str, _host: &str) -> Result<Zeroizing<String>> {
            self.passwords.fetch_add(1, Ordering::SeqCst);
            Ok(Zeroizing::new(
                if self.wrong_password {
                    "incorrect"
                } else {
                    "test-password"
                }
                .to_owned(),
            ))
        }
    }

    #[test]
    fn local_key_is_reused_and_public_key_is_repaired_after_partial_creation() {
        let dir = tempfile::tempdir().unwrap();
        let state = dir.path().join("state");
        let first = load_or_create_key(&state).unwrap();
        let bytes = fs::read(state.join("id_ed25519")).unwrap();
        fs::remove_file(state.join("id_ed25519.pub")).unwrap();
        let second = load_or_create_key(&state).unwrap();
        assert_eq!(first.public_key(), second.public_key());
        assert_eq!(bytes, fs::read(state.join("id_ed25519")).unwrap());
        assert!(state.join("id_ed25519.pub").is_file());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(&state).unwrap().permissions().mode() & 0o777,
                0o700
            );
            for name in ["id_ed25519", "id_ed25519.pub", "ssh-state.lock"] {
                assert_eq!(
                    fs::metadata(state.join(name)).unwrap().permissions().mode() & 0o777,
                    0o600
                );
            }
        }
    }

    #[test]
    fn invalid_existing_private_key_is_never_replaced() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("id_ed25519");
        fs::write(&path, b"invalid-key").unwrap();
        assert!(load_or_create_key(dir.path()).is_err());
        assert_eq!(fs::read(path).unwrap(), b"invalid-key");
    }

    #[test]
    fn host_trust_is_explicit_persistent_and_port_specific() {
        let dir = tempfile::tempdir().unwrap();
        let prompts = Prompts::default();
        assert!(
            verify_fingerprint(dir.path(), "[vps.example]:22", "SHA256:first", &prompts).unwrap()
        );
        assert!(
            verify_fingerprint(dir.path(), "[vps.example]:22", "SHA256:first", &prompts).unwrap()
        );
        assert_eq!(prompts.confirmations.load(Ordering::SeqCst), 1);
        let changed =
            verify_fingerprint(dir.path(), "[vps.example]:22", "SHA256:changed", &prompts)
                .unwrap_err();
        assert!(changed.to_string().contains("host key changed"));
        assert_eq!(prompts.confirmations.load(Ordering::SeqCst), 1);
        assert!(
            verify_fingerprint(dir.path(), "[vps.example]:2222", "SHA256:other", &prompts).unwrap()
        );
        assert_eq!(prompts.confirmations.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn refused_first_use_does_not_persist_trust() {
        let dir = tempfile::tempdir().unwrap();
        let prompts = Prompts {
            reject_host: true,
            ..Default::default()
        };
        assert!(
            !verify_fingerprint(dir.path(), "[vps.example]:22", "SHA256:unknown", &prompts)
                .unwrap()
        );
        assert!(!dir.path().join("known_hosts.json").exists());
    }

    #[test]
    fn corrupt_trust_file_is_not_silently_reset() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("known_hosts.json"), b"broken").unwrap();
        let prompts = Prompts::default();
        assert!(verify_fingerprint(dir.path(), "[vps]:22", "SHA256:key", &prompts).is_err());
        assert_eq!(prompts.confirmations.load(Ordering::SeqCst), 0);
    }

    #[cfg(unix)]
    #[test]
    fn local_state_rejects_symlinks_and_hard_links() {
        use std::os::unix::fs::symlink;
        let dir = tempfile::tempdir().unwrap();
        let external = dir.path().join("external");
        fs::write(&external, b"do-not-touch").unwrap();
        let state = dir.path().join("state");
        fs::create_dir(&state).unwrap();
        symlink(&external, state.join("id_ed25519")).unwrap();
        assert!(load_or_create_key(&state).is_err());
        fs::remove_file(state.join("id_ed25519")).unwrap();
        fs::hard_link(&external, state.join("id_ed25519")).unwrap();
        assert!(load_or_create_key(&state).is_err());
        assert_eq!(fs::read(external).unwrap(), b"do-not-touch");
    }

    #[cfg(unix)]
    #[test]
    fn state_directory_refuses_shared_writable_paths_without_chmod() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let shared = dir.path().join("shared");
        fs::create_dir(&shared).unwrap();
        fs::set_permissions(&shared, fs::Permissions::from_mode(0o777)).unwrap();
        assert!(secure_state_directory(&shared).is_err());
        assert_eq!(
            fs::metadata(&shared).unwrap().permissions().mode() & 0o777,
            0o777
        );
        assert!(secure_state_directory(Path::new("/")).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn enrollment_preserves_existing_keys_and_refuses_unsafe_files() {
        use std::os::unix::fs::{PermissionsExt, symlink};
        use std::process::Command;
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join("home");
        fs::create_dir(&home).unwrap();
        fs::set_permissions(&home, fs::Permissions::from_mode(0o700)).unwrap();
        let ssh = home.join(".ssh");
        fs::create_dir(&ssh).unwrap();
        fs::set_permissions(&ssh, fs::Permissions::from_mode(0o700)).unwrap();
        let keys = ssh.join("authorized_keys");
        let previous = b"# administrator key\nssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIplaceholder administrator\n";
        fs::write(&keys, previous).unwrap();
        let key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let public = key.public_key().to_openssh().unwrap();
        let command = enrollment_command(&public).unwrap();
        let run = || {
            Command::new("sh")
                .arg("-c")
                .arg(&command)
                .env("HOME", &home)
                .output()
                .unwrap()
        };
        assert!(run().status.success());
        let content = fs::read(&keys).unwrap();
        assert!(content.starts_with(previous));
        assert!(run().status.success());
        assert_eq!(content, fs::read(&keys).unwrap());
        fs::remove_file(&keys).unwrap();
        let external = dir.path().join("external");
        fs::write(&external, b"preserved").unwrap();
        symlink(&external, &keys).unwrap();
        assert!(!run().status.success());
        assert_eq!(fs::read(&external).unwrap(), b"preserved");
        fs::remove_file(&keys).unwrap();
        fs::write(&keys, b"unsafe").unwrap();
        fs::set_permissions(&keys, fs::Permissions::from_mode(0o666)).unwrap();
        assert!(!run().status.success());
        assert_eq!(fs::read(&keys).unwrap(), b"unsafe");
    }

    #[test]
    fn enrollment_rejects_malformed_multiline_and_control_character_keys() {
        let key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
        let public = key.public_key().to_openssh().unwrap();
        for invalid in [
            String::new(),
            "ssh-ed25519 invalid".to_owned(),
            format!("{public}\n{public}"),
            format!("{public}\rcomment"),
            format!("{public}\tcomment"),
            format!("{public}\0comment"),
            "x".repeat(4097),
        ] {
            assert!(enrollment_command(&invalid).is_err());
        }
        // Authorized-key options are deliberately not accepted: they would
        // change the semantics of a controller's connection.
        assert!(enrollment_command(&format!("command=\"false\" {public}")).is_err());
        assert!(enrollment_command(&public).is_ok());
    }

    #[cfg(unix)]
    mod wire {
        use super::*;
        use russh::keys::PublicKey;
        use russh::{Channel, ChannelId, server};
        use std::collections::HashMap;
        use std::os::unix::fs::PermissionsExt;
        use std::process::Stdio;
        use std::sync::Mutex;
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        type ForwardRequests = Arc<Mutex<Vec<(String, u32, String, u32)>>>;

        struct Peer {
            home: PathBuf,
            deny_keys: Arc<std::sync::atomic::AtomicBool>,
            commands: HashMap<ChannelId, String>,
            inputs: HashMap<ChannelId, Vec<u8>>,
            forwards: ForwardRequests,
        }

        impl server::Handler for Peer {
            type Error = anyhow::Error;

            async fn auth_password(&mut self, user: &str, password: &str) -> Result<server::Auth> {
                Ok(if user == "test" && password == "test-password" {
                    server::Auth::Accept
                } else {
                    server::Auth::reject()
                })
            }

            async fn auth_publickey(
                &mut self,
                user: &str,
                public_key: &PublicKey,
            ) -> Result<server::Auth> {
                let keys =
                    fs::read_to_string(self.home.join(".ssh/authorized_keys")).unwrap_or_default();
                let accepted = !self.deny_keys.load(Ordering::SeqCst)
                    && user == "test"
                    && keys.lines().any(|line| {
                        PublicKey::from_openssh(line)
                            .is_ok_and(|key| key.key_data() == public_key.key_data())
                    });
                Ok(if accepted {
                    server::Auth::Accept
                } else {
                    server::Auth::reject()
                })
            }

            async fn channel_open_session(
                &mut self,
                _channel: Channel<server::Msg>,
                reply: server::ChannelOpenHandle,
                _session: &mut server::Session,
            ) -> Result<()> {
                reply.accept().await;
                Ok(())
            }

            async fn channel_open_direct_tcpip(
                &mut self,
                channel: Channel<server::Msg>,
                host: &str,
                port: u32,
                originator: &str,
                originator_port: u32,
                reply: server::ChannelOpenHandle,
                _session: &mut server::Session,
            ) -> Result<()> {
                self.forwards.lock().unwrap().push((
                    host.to_owned(),
                    port,
                    originator.to_owned(),
                    originator_port,
                ));
                if host != "127.0.0.1" || port != 8080 {
                    return Ok(()); // Dropping the reply explicitly rejects an unexpected target.
                }
                reply.accept().await;
                let fixture_home = self.home.clone();
                tokio::spawn(async move {
                    let mut stream = channel.into_stream();
                    let mut request = Vec::new();
                    loop {
                        let mut byte = [0u8; 1];
                        if stream.read_exact(&mut byte).await.is_err() {
                            return;
                        }
                        request.push(byte[0]);
                        if request.ends_with(b"\r\n\r\n") {
                            break;
                        }
                        assert!(request.len() < 4096);
                    }
                    if fixture_home.join("nodes-fixture").exists() {
                        use std::io::Write;
                        let request_line = String::from_utf8_lossy(&request)
                            .lines()
                            .next()
                            .unwrap()
                            .to_owned();
                        writeln!(
                            OpenOptions::new()
                                .create(true)
                                .append(true)
                                .open(fixture_home.join("api-requests"))
                                .unwrap(),
                            "{request_line}"
                        )
                        .unwrap();
                        if request_line.starts_with("POST ") {
                            // An accepted mutation with a lost HTTP response is
                            // deliberately uncertain and must never be replayed.
                            stream.shutdown().await.unwrap();
                            return;
                        }
                        let path = request_line.split_whitespace().nth(1).unwrap();
                        let node = serde_json::json!({"key":"lv1","title":"Latvia","region":"Europe","flag":"", "enabled":true,"protocols":["awg"],"xray_transports":[],"desired_revision":1,"applied_revision":1,"transport":"ssh","ssh_target":"root@node.example"});
                        let (status, body) = if path.ends_with("/services") {
                            if let Ok(code) = fs::read_to_string(fixture_home.join("service-error"))
                            {
                                (
                                    if code == "node_agent_unconfigured" {
                                        409
                                    } else {
                                        503
                                    },
                                    serde_json::json!({"error":{"code":code}}),
                                )
                            } else {
                                (
                                    200,
                                    serde_json::json!({"docker":true,"awg_running":true,"xray_running":false}),
                                )
                            }
                        } else if path.ends_with("/overview") {
                            (
                                200,
                                serde_json::json!({"state":"applied_unverified","settings_complete":true,"last_job":null}),
                            )
                        } else if path.starts_with("/api/v1/nodes?") {
                            (200, serde_json::json!({"items":[node],"next_cursor":null}))
                        } else {
                            (200, node)
                        };
                        let body = serde_json::to_string(&body).unwrap();
                        stream.write_all(format!("HTTP/1.1 {status} Fixture\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).as_bytes()).await.unwrap();
                        stream.shutdown().await.unwrap();
                        return;
                    }
                    assert_eq!(
                        request,
                        b"GET /health/ready HTTP/1.1\r\nHost: localhost\r\n\r\n"
                    );
                    stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 15\r\nConnection: close\r\n\r\n{\"ready\":true}").await.unwrap();
                    stream.shutdown().await.unwrap();
                });
                Ok(())
            }

            async fn exec_request(
                &mut self,
                channel: ChannelId,
                data: &[u8],
                session: &mut server::Session,
            ) -> Result<()> {
                self.commands
                    .insert(channel, String::from_utf8(data.to_vec())?);
                session.channel_success(channel)?;
                Ok(())
            }

            async fn data(
                &mut self,
                channel: ChannelId,
                data: &[u8],
                _session: &mut server::Session,
            ) -> Result<()> {
                self.inputs
                    .entry(channel)
                    .or_default()
                    .extend_from_slice(data);
                Ok(())
            }

            async fn channel_eof(
                &mut self,
                channel: ChannelId,
                session: &mut server::Session,
            ) -> Result<()> {
                let Some(command) = self.commands.remove(&channel) else {
                    return Ok(()); // A direct-tcpip channel is handled by its stream fixture.
                };
                if command == "NP_NO_EXIT_STATUS" {
                    session.close(channel)?;
                    return Ok(());
                }
                let home = self.home.clone();
                let input = self.inputs.remove(&channel).unwrap_or_default();
                if home.join("nodes-fixture").exists()
                    && command.contains("NODE_PLANE_APP_DIR=/opt/node-plane/current")
                {
                    use std::io::Write;
                    let input: serde_json::Value = serde_json::from_slice(&input)?;
                    writeln!(
                        OpenOptions::new()
                            .create(true)
                            .append(true)
                            .open(home.join("backend-actions"))?,
                        "{input}"
                    )?;
                    let response = if input["action"] == "authenticate" {
                        if home.join("select-admin").exists()
                            && input["account_id"].is_null()
                            && !home.join("bound-admin").exists()
                        {
                            serde_json::json!({"ok":false,"error":{"code":"admin_selection_required","choices":[
                                {"account_id":"00000000-0000-4000-8000-000000000001","label":"101 · @first"},
                                {"account_id":"00000000-0000-4000-8000-000000000002","label":"102"}]}})
                        } else {
                            let account = input["account_id"]
                                .as_str()
                                .map(str::to_owned)
                                .or_else(|| fs::read_to_string(home.join("bound-admin")).ok())
                                .unwrap_or_else(|| "00000000-0000-4000-8000-000000000001".into());
                            if home.join("select-admin").exists() {
                                fs::write(home.join("bound-admin"), &account)?;
                            }
                            serde_json::json!({"ok":true,"token":format!("np_{}", "a".repeat(76)),"account_id":account})
                        }
                    } else {
                        serde_json::json!({"ok":true})
                    };
                    session.data(channel, serde_json::to_vec(&response)?)?;
                    session.exit_status_request(channel, 0)?;
                    session.close(channel)?;
                    return Ok(());
                }
                let output =
                    tokio::task::spawn_blocking(move || -> Result<std::process::Output> {
                        let mut child = std::process::Command::new("/bin/bash")
                            .arg("-c")
                            .arg(format!("logger() {{ printf '%s\\n' \"${{@:4}}\" >> \"$HOME/audit.jsonl\"; }}; export -f logger; {command}"))
                            .env("HOME", home)
                            .stdin(Stdio::piped())
                            .stdout(Stdio::piped())
                            .stderr(Stdio::piped())
                            .spawn()?;
                        child.stdin.take().context("No stdin")?.write_all(&input)?;
                        Ok(child.wait_with_output()?)
                    })
                    .await??;
                session.data(channel, output.stdout)?;
                session.extended_data(channel, 1, output.stderr)?;
                session.exit_status_request(channel, output.status.code().unwrap_or(255) as u32)?;
                session.close(channel)?;
                Ok(())
            }
        }

        struct MockServer {
            _directory: tempfile::TempDir,
            home: PathBuf,
            port: u16,
            host_key: Arc<Mutex<PrivateKey>>,
            deny_keys: Arc<std::sync::atomic::AtomicBool>,
            forwards: ForwardRequests,
            listener: tokio::task::JoinHandle<()>,
            connections: Arc<Mutex<Vec<tokio::task::JoinHandle<()>>>>,
        }

        impl MockServer {
            async fn start() -> Self {
                let directory = tempfile::tempdir().unwrap();
                let home = directory.path().join("home");
                fs::create_dir(&home).unwrap();
                fs::set_permissions(&home, fs::Permissions::from_mode(0o700)).unwrap();
                let host_key = Arc::new(Mutex::new(
                    PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap(),
                ));
                let socket = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
                let port = socket.local_addr().unwrap().port();
                let key = host_key.clone();
                let deny_keys = Arc::new(std::sync::atomic::AtomicBool::new(false));
                let deny = deny_keys.clone();
                let home2 = home.clone();
                let connections = Arc::new(Mutex::new(Vec::new()));
                let connection_tasks = connections.clone();
                let forwards = Arc::new(Mutex::new(Vec::new()));
                let forward_requests = forwards.clone();
                let listener = tokio::spawn(async move {
                    while let Ok((stream, _)) = socket.accept().await {
                        let config = Arc::new(server::Config {
                            keys: vec![key.lock().unwrap().clone()],
                            auth_rejection_time: Duration::ZERO,
                            auth_rejection_time_initial: Some(Duration::ZERO),
                            ..Default::default()
                        });
                        let handler = Peer {
                            home: home2.clone(),
                            deny_keys: deny.clone(),
                            commands: HashMap::new(),
                            inputs: HashMap::new(),
                            forwards: forward_requests.clone(),
                        };
                        let task = tokio::spawn(async move {
                            if let Ok(session) = server::run_stream(config, stream, handler).await {
                                let _ = session.await;
                            }
                        });
                        connection_tasks.lock().unwrap().push(task);
                    }
                });
                Self {
                    _directory: directory,
                    home,
                    port,
                    host_key,
                    deny_keys,
                    forwards,
                    listener,
                    connections,
                }
            }

            fn options(&self, state: &Path) -> SshOptions {
                SshOptions {
                    host: "127.0.0.1".to_owned(),
                    port: self.port,
                    user: "test".to_owned(),
                    state_dir: state.to_path_buf(),
                }
            }
        }

        impl Drop for MockServer {
            fn drop(&mut self) {
                self.listener.abort();
                for task in self.connections.lock().unwrap().iter() {
                    task.abort();
                }
            }
        }

        fn nodes_request(server: &MockServer, state: &Path) -> crate::config::Request {
            crate::config::Request {
                action: crate::config::Action::Diagnose,
                host: "127.0.0.1".into(),
                port: server.port,
                user: "test".into(),
                state_dir: state.into(),
                tag: String::new(),
                branch: "dev".into(),
                admin_ids: String::new(),
                bot_token: Zeroizing::new(String::new()),
                workflow: crate::config::WorkflowOptions::default(),
            }
        }
        fn prepare_nodes_fixture(server: &MockServer, state: &Path) {
            secure_state_directory(state).unwrap();
            let key = load_or_create_key(state).unwrap();
            fs::create_dir(server.home.join(".ssh")).unwrap();
            fs::write(
                server.home.join(".ssh/authorized_keys"),
                key.public_key().to_openssh().unwrap(),
            )
            .unwrap();
            fs::write(server.home.join("nodes-fixture"), "enabled").unwrap();
        }
        async fn nodes_command(
            worker: &crate::nodes::Worker,
            request: crate::config::Request,
            command: crate::nodes::Command,
        ) -> (
            std::result::Result<String, String>,
            Vec<crate::nodes::Update>,
        ) {
            use crate::events::{Answer, Event, Prompt};
            let (tx, rx) = std::sync::mpsc::channel();
            worker.submit(request, command, tx).unwrap();
            tokio::task::spawn_blocking(move || {
                let mut updates = Vec::new();
                loop {
                    match rx.recv_timeout(Duration::from_secs(10)).unwrap() {
                        Event::Prompt(prompt, reply) => {
                            reply
                                .send(match prompt {
                                    Prompt::Password { .. } => {
                                        Answer::Password(Zeroizing::new("test-password".into()))
                                    }
                                    Prompt::SelectAdministrator { .. } => Answer::Selection(1),
                                    _ => Answer::Confirm(true),
                                })
                                .unwrap();
                        }
                        Event::Nodes(update) => updates.push(update),
                        Event::Finished(result) => return (result, updates),
                        _ => {}
                    }
                }
            })
            .await
            .unwrap()
        }
        #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
        async fn first_nodes_connection_binds_selected_admin_and_reuses_binding() {
            let server = MockServer::start().await;
            let state = tempfile::tempdir().unwrap();
            prepare_nodes_fixture(&server, state.path());
            fs::write(server.home.join("select-admin"), "enabled").unwrap();
            let mut worker = crate::nodes::Worker::new().unwrap();
            for _ in 0..2 {
                let (result, _) = nodes_command(
                    &worker,
                    nodes_request(&server, state.path()),
                    crate::nodes::Command::List,
                )
                .await;
                assert!(result.is_ok(), "{result:?}");
            }
            assert_eq!(
                fs::read_to_string(server.home.join("bound-admin")).unwrap(),
                "00000000-0000-4000-8000-000000000002"
            );
            let actions: Vec<serde_json::Value> =
                fs::read_to_string(server.home.join("backend-actions"))
                    .unwrap()
                    .lines()
                    .map(|line| serde_json::from_str(line).unwrap())
                    .filter(|value: &serde_json::Value| value["action"] == "authenticate")
                    .collect();
            assert_eq!(actions.len(), 3);
            assert!(actions[0]["account_id"].is_null());
            assert_eq!(
                actions[1]["account_id"],
                "00000000-0000-4000-8000-000000000002"
            );
            assert!(actions[2]["account_id"].is_null());
            worker.shutdown().unwrap();
        }
        #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
        async fn nodes_reuse_ssh_with_fresh_authorization_and_close_on_idle_reset_and_exit() {
            use crate::nodes::{Command, ConnectionState, Worker};
            let server = MockServer::start().await;
            let state = tempfile::tempdir().unwrap();
            prepare_nodes_fixture(&server, state.path());
            let mut worker = Worker::with_idle_timeout(Duration::from_millis(500)).unwrap();
            for command in [Command::List, Command::Card("lv1".into())] {
                let (result, _) =
                    nodes_command(&worker, nodes_request(&server, state.path()), command).await;
                assert!(result.is_ok(), "{result:?}");
            }
            assert_eq!(server.connections.lock().unwrap().len(), 1);
            assert_eq!(worker.state(), ConnectionState::Connected);
            let log: Vec<serde_json::Value> =
                fs::read_to_string(server.home.join("backend-actions"))
                    .unwrap()
                    .lines()
                    .map(|s| serde_json::from_str(s).unwrap())
                    .collect();
            let auth: Vec<_> = log
                .iter()
                .filter(|v| v["action"] == "authenticate")
                .collect();
            assert_eq!(auth.len(), 2);
            assert_ne!(auth[0]["session_id"], auth[1]["session_id"]);
            for session in auth {
                assert!(
                    log.iter().any(
                        |v| v["action"] == "revoke" && v["session_id"] == session["session_id"]
                    )
                );
            }
            for _ in 0..3 {
                tokio::time::sleep(Duration::from_millis(250)).await;
                worker.touch();
            }
            assert_eq!(worker.state(), ConnectionState::Connected);
            tokio::time::sleep(Duration::from_millis(1100)).await;
            assert_eq!(worker.state(), ConnectionState::Closed);
            let (result, _) =
                nodes_command(&worker, nodes_request(&server, state.path()), Command::List).await;
            assert!(result.is_ok(), "{result:?}");
            assert_eq!(server.connections.lock().unwrap().len(), 2);
            worker.reset();
            tokio::time::sleep(Duration::from_millis(100)).await;
            assert_eq!(worker.state(), ConnectionState::Closed);
            let (result, _) =
                nodes_command(&worker, nodes_request(&server, state.path()), Command::List).await;
            assert!(result.is_ok(), "{result:?}");
            assert_eq!(server.connections.lock().unwrap().len(), 3);
            worker.shutdown().unwrap();
            assert_eq!(worker.state(), ConnectionState::Closed);
        }

        #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
        async fn nodes_probe_distinguishes_missing_from_unavailable_and_never_replays_uncertain_setup()
         {
            use crate::nodes::{AgentState, Command, Update, Worker};
            let server = MockServer::start().await;
            let state = tempfile::tempdir().unwrap();
            prepare_nodes_fixture(&server, state.path());
            let mut worker = Worker::new().unwrap();
            let mut card = None;
            for (code, expected) in [
                (None, AgentState::Ready),
                (Some("node_agent_unconfigured"), AgentState::Missing),
                (Some("node_agent_unavailable"), AgentState::Unavailable),
            ] {
                let path = server.home.join("service-error");
                if let Some(code) = code {
                    fs::write(path, code).unwrap();
                } else if path.exists() {
                    fs::remove_file(path).unwrap();
                }
                let (result, updates) = nodes_command(
                    &worker,
                    nodes_request(&server, state.path()),
                    Command::Card("lv1".into()),
                )
                .await;
                assert!(result.is_ok(), "{result:?}");
                let node = updates
                    .into_iter()
                    .find_map(|u| {
                        if let Update::Card(n) = u {
                            Some(n)
                        } else {
                            None
                        }
                    })
                    .unwrap();
                assert_eq!(node.agent, expected);
                card = Some(node);
            }
            let (result, updates) = nodes_command(
                &worker,
                nodes_request(&server, state.path()),
                Command::Setup(card.unwrap()),
            )
            .await;
            assert!(result.is_err());
            let operation = updates
                .into_iter()
                .find_map(|u| {
                    if let Update::Operation(o) = u {
                        Some(o)
                    } else {
                        None
                    }
                })
                .unwrap();
            assert_eq!(operation.status, "unconfirmed");
            assert!(operation.command_id.is_some());
            let (result, _) = nodes_command(
                &worker,
                nodes_request(&server, state.path()),
                Command::Observe(operation),
            )
            .await;
            assert!(result.is_ok(), "{result:?}");
            let requests = fs::read_to_string(server.home.join("api-requests")).unwrap();
            assert_eq!(
                requests.lines().filter(|s| s.starts_with("POST ")).count(),
                1
            );
            worker.shutdown().unwrap();
        }

        #[tokio::test]
        async fn password_enrollment_fresh_key_login_and_subsequent_password_free_login() {
            let server = MockServer::start().await;
            let state = tempfile::tempdir().unwrap();
            let ssh = server.home.join(".ssh");
            fs::create_dir(&ssh).unwrap();
            fs::set_permissions(&ssh, fs::Permissions::from_mode(0o700)).unwrap();
            let existing = b"# existing administrator key must remain\n";
            fs::write(ssh.join("authorized_keys"), existing).unwrap();
            let prompts = Arc::new(Prompts::default());
            let options = server.options(state.path());
            let first = connect(&options, prompts.clone()).await.unwrap();
            assert_eq!(prompts.confirmations.load(Ordering::SeqCst), 1);
            assert_eq!(prompts.passwords.load(Ordering::SeqCst), 1);
            first.close().await.unwrap();
            let installed = fs::read(ssh.join("authorized_keys")).unwrap();
            assert!(installed.starts_with(existing));
            let mut second = connect(&options, prompts.clone()).await.unwrap();
            assert_eq!(prompts.passwords.load(Ordering::SeqCst), 1);
            let mut stdout = Vec::new();
            let mut stderr = Vec::new();
            let code = second
                .run(
                    "cat; printf 'separate-error' >&2; exit 7",
                    Some(b"roundtrip-input"),
                    |kind, data| match kind {
                        OutputStream::Stdout => stdout.extend_from_slice(data),
                        OutputStream::Stderr => stderr.extend_from_slice(data),
                    },
                )
                .await
                .unwrap();
            assert_eq!(code, 7);
            assert_eq!(stdout, b"roundtrip-input");
            assert_eq!(stderr, b"separate-error");
            assert_eq!(installed, fs::read(ssh.join("authorized_keys")).unwrap());
            second.close().await.unwrap();
        }

        #[tokio::test]
        async fn backend_forwarding_targets_only_loopback_api_and_retains_trusted_host_key() {
            let server = MockServer::start().await;
            let state = tempfile::tempdir().unwrap();
            let session = connect(&server.options(state.path()), Arc::new(Prompts::default()))
                .await
                .unwrap();
            let observed = russh::keys::PublicKey::from_openssh(&session.server_key()).unwrap();
            assert_eq!(observed, *server.host_key.lock().unwrap().public_key());
            let trusted: BTreeMap<String, String> =
                serde_json::from_slice(&fs::read(state.path().join("known_hosts.json")).unwrap())
                    .unwrap();
            assert_eq!(
                trusted
                    .get(&endpoint(&server.options(state.path())))
                    .unwrap(),
                &observed.fingerprint(HashAlg::Sha256).to_string(),
            );
            let mut stream = session.backend_stream().await.unwrap();
            stream
                .write_all(b"GET /health/ready HTTP/1.1\r\nHost: localhost\r\n\r\n")
                .await
                .unwrap();
            let mut response = Vec::new();
            timeout(Duration::from_secs(5), stream.read_to_end(&mut response))
                .await
                .unwrap()
                .unwrap();
            assert_eq!(response, b"HTTP/1.1 200 OK\r\nContent-Length: 15\r\nConnection: close\r\n\r\n{\"ready\":true}");
            assert_eq!(
                *server.forwards.lock().unwrap(),
                vec![("127.0.0.1".to_owned(), 8080, "127.0.0.1".to_owned(), 0)],
            );
            session.close().await.unwrap();
        }

        #[tokio::test]
        async fn controller_enrollment_is_idempotent_and_preserves_administrator_and_workstation_keys()
         {
            let server = MockServer::start().await;
            let state = tempfile::tempdir().unwrap();
            let mut session = connect(&server.options(state.path()), Arc::new(Prompts::default()))
                .await
                .unwrap();
            let path = server.home.join(".ssh/authorized_keys");
            let administrator = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
            let administrator_public = administrator.public_key().to_openssh().unwrap();
            let mut existing = fs::read(&path).unwrap();
            existing
                .extend_from_slice(format!("\n{administrator_public} administrator\n").as_bytes());
            fs::write(&path, &existing).unwrap();
            let controller = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
            let public = controller.public_key().to_openssh().unwrap();
            session
                .enroll_public_key(&public, uuid::Uuid::new_v4())
                .await
                .unwrap();
            let after = fs::read(&path).unwrap();
            let journal = fs::read_to_string(server.home.join("audit.jsonl")).unwrap();
            let events: Vec<serde_json::Value> = journal
                .lines()
                .map(|line| serde_json::from_str(line).unwrap())
                .collect();
            let enrollment: Vec<_> = events
                .iter()
                .filter(|event| event["action"] == "controller-key-enrollment")
                .collect();
            assert_eq!(enrollment.len(), 2);
            assert_eq!(enrollment[0]["phase"], "admitted");
            assert_eq!(enrollment[1]["exit_status"], 0);
            assert_eq!(enrollment[0]["ssh_user"], "test");
            assert_eq!(
                enrollment[0]["device_fingerprint"],
                session.workstation_fingerprint
            );
            assert_eq!(
                enrollment[0]["key_fingerprint"],
                controller
                    .public_key()
                    .fingerprint(HashAlg::Sha256)
                    .to_string()
            );
            assert_eq!(enrollment[0]["target"], session.endpoint);
            assert!(!journal.contains(&public));
            assert!(!journal.contains("test-password"));
            assert!(
                events
                    .iter()
                    .any(|event| event["action"] == "workstation-key-enrollment")
            );
            assert!(after.starts_with(&existing));
            session
                .enroll_public_key(&public, uuid::Uuid::new_v4())
                .await
                .unwrap();
            assert_eq!(after, fs::read(&path).unwrap());
            assert!(
                session
                    .enroll_public_key(&format!("{public}\n{public}"), uuid::Uuid::new_v4())
                    .await
                    .is_err()
            );
            assert_eq!(after, fs::read(&path).unwrap());
            assert_eq!(
                String::from_utf8(after)
                    .unwrap()
                    .lines()
                    .filter(|line| *line == public)
                    .count(),
                1
            );
            session.close().await.unwrap();
        }

        #[tokio::test]
        async fn changed_host_key_refuses_connection_before_password_prompt() {
            let server = MockServer::start().await;
            let state = tempfile::tempdir().unwrap();
            let prompts = Arc::new(Prompts::default());
            let options = server.options(state.path());
            connect(&options, prompts.clone())
                .await
                .unwrap()
                .close()
                .await
                .unwrap();
            *server.host_key.lock().unwrap() =
                PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
            let error = match connect(&options, prompts.clone()).await {
                Ok(_) => panic!("Changed host key was accepted"),
                Err(error) => error,
            };
            assert!(format!("{error:#}").contains("host key changed"));
            assert_eq!(prompts.confirmations.load(Ordering::SeqCst), 1);
            assert_eq!(prompts.passwords.load(Ordering::SeqCst), 1);
        }

        #[tokio::test]
        async fn rejected_password_does_not_enroll_and_missing_exit_status_is_uncertain() {
            let server = MockServer::start().await;
            let state = tempfile::tempdir().unwrap();
            let wrong = Arc::new(Prompts {
                wrong_password: true,
                ..Default::default()
            });
            assert!(connect(&server.options(state.path()), wrong).await.is_err());
            assert!(!server.home.join(".ssh/authorized_keys").exists());
            let mut session = connect(&server.options(state.path()), Arc::new(Prompts::default()))
                .await
                .unwrap();
            let error = session
                .run("NP_NO_EXIT_STATUS", None, |_, _| {})
                .await
                .unwrap_err();
            assert!(error.to_string().contains("unconfirmed"));
            session.close().await.unwrap();
        }

        #[tokio::test]
        async fn host_refusal_never_requests_password_and_failed_fresh_key_login_is_not_success() {
            let server = MockServer::start().await;
            let state = tempfile::tempdir().unwrap();
            let prompts = Arc::new(Prompts {
                reject_host: true,
                ..Default::default()
            });
            assert!(
                connect(&server.options(state.path()), prompts.clone())
                    .await
                    .is_err()
            );
            assert_eq!(prompts.passwords.load(Ordering::SeqCst), 0);
            assert!(!server.home.join(".ssh/authorized_keys").exists());
            server.deny_keys.store(true, Ordering::SeqCst);
            let error = match connect(&server.options(state.path()), Arc::new(Prompts::default()))
                .await
            {
                Ok(_) => panic!("Enrollment without successful key login was reported as success"),
                Err(error) => error,
            };
            assert!(format!("{error:#}").contains("fresh SSH login rejected"));
            assert!(server.home.join(".ssh/authorized_keys").is_file());
        }
    }
}
