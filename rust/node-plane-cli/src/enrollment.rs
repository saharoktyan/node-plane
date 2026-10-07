//! Verify a target using the controller's existing key without exporting it.
//!
//! Workstation host trust is used as the bootstrap pin. A public-key file write
//! alone is not considered proof that the controller can authenticate.

use crate::ssh::{OutputStream, SshOptions, SshSession};
use anyhow::{Context, Result, ensure};
use serde::Serialize;

#[derive(Serialize)]
struct Target<'a> {
    host: &'a str,
    port: u16,
    user: &'a str,
    host_key: &'a str,
}

fn validate_target(target: &SshOptions, trusted_host_key: &str) -> Result<()> {
    ensure!(
        !target.host.is_empty()
            && target.host.len() <= 253
            && !target.host.starts_with('-')
            && target
                .host
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b".-:".contains(&b)),
        "Invalid target SSH host"
    );
    ensure!(
        !target.user.is_empty()
            && target.user.len() <= 64
            && !target.user.starts_with('-')
            && target
                .user
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b)),
        "Invalid target SSH user"
    );
    ensure!(target.port != 0, "Invalid target SSH port");
    ensure!(
        !trusted_host_key.is_empty()
            && trusted_host_key.len() <= 4096
            && !trusted_host_key.chars().any(char::is_control),
        "Invalid target SSH host key"
    );
    russh::keys::PublicKey::from_openssh(trusted_host_key)
        .context("Invalid target SSH host key")?;
    Ok(())
}

/// Authenticate from the controller with its actual configured private key, then
/// preserve the workstation-confirmed host pin for future node setup operations.
/// This operation never retrieves the private key or requests a controller key's
/// passphrase. SSH and the known-host merge execute on the controller as root.
pub async fn verify_controller_login(
    controller: &mut SshSession,
    target: &SshOptions,
    trusted_host_key: &str,
) -> Result<()> {
    validate_target(target, trusted_host_key)?;
    let input = serde_json::to_vec(&Target {
        host: &target.host,
        port: target.port,
        user: &target.user,
        host_key: trusted_host_key,
    })?;
    let quote = |value: &str| format!("'{}'", value.replace('\'', "'\\''"));
    let script = format!(
        "set -eu\nexport NODE_PLANE_APP_DIR=/opt/node-plane/current\nexport NODE_PLANE_SHARED_DIR=/opt/node-plane/shared\nexport PYTHONPATH=/opt/node-plane/current/app\ntest -x /opt/node-plane/current/.venv/bin/python || {{ echo NODE_PLANE_ENROLLMENT_ERROR:installation_missing; exit 1; }}\nexec /opt/node-plane/current/.venv/bin/python -c {}",
        quote(CONTROLLER_LOGIN_SCRIPT),
    );
    let command = format!(
        "if [ \"$(id -u)\" = 0 ]; then /bin/bash -c {}; else sudo -n /bin/bash -c {}; fi",
        quote(&script),
        quote(&script),
    );
    let mut output = Vec::new();
    let mut oversized = false;
    let code = controller
        .run(&command, Some(&input), |stream, data| {
            if stream == OutputStream::Stdout {
                if output.len() + data.len() <= 4096 {
                    output.extend_from_slice(data);
                } else {
                    oversized = true;
                }
            }
        })
        .await
        .context("Controller-to-node login verification could not be confirmed")?;
    ensure!(
        !oversized,
        "Controller login verification returned unexpected output"
    );
    if code == 0 && output == b"NODE_PLANE_CONTROLLER_LOGIN_VERIFIED\n" {
        return Ok(());
    }
    let error = String::from_utf8_lossy(&output);
    let reason = if error.contains("NODE_PLANE_ENROLLMENT_ERROR:host_key_conflict") {
        "The controller already has a different host key for this node. Verify and resolve its known-host entry before trying again"
    } else if error.contains("NODE_PLANE_ENROLLMENT_ERROR:login_failed") {
        "The public key was added, but the controller could not authenticate. Check node connectivity, sshd AuthorizedKeysFile and the SSH user"
    } else if error.contains("NODE_PLANE_ENROLLMENT_ERROR:unsafe_state") {
        "Controller SSH files have unsafe ownership, permissions or links. Check the configured private key and known-host paths"
    } else if error.contains("NODE_PLANE_ENROLLMENT_ERROR:configuration_missing") {
        "The controller SSH private key is not configured or unavailable"
    } else if error.contains("NODE_PLANE_ENROLLMENT_ERROR:installation_missing") {
        "No supported controller installation was found"
    } else {
        "Controller-to-node login verification failed. Check controller SSH configuration and node connectivity"
    };
    anyhow::bail!("{reason}")
}

// This helper uses the installed Python runtime on the controller. Python is not
// a workstation dependency. Errors intentionally contain codes, never config or
// subprocess output (which could contain credentials or private paths).
const CONTROLLER_LOGIN_SCRIPT: &str = r#"
import base64
import fcntl
import hashlib
import hmac
import json
import os
import pathlib
import re
import selectors
import stat
import subprocess
import sys
import tempfile
import time

class EnrollmentError(Exception):
    pass

def fail(code):
    raise EnrollmentError(code)

def safe_directory(path):
    path = pathlib.Path(path)
    if not path.is_absolute():
        fail('unsafe_state')
    for ancestor in [*reversed(path.parents), path]:
        try:
            details = ancestor.lstat()
        except FileNotFoundError:
            if ancestor != path:
                fail('unsafe_state')
            ancestor.mkdir(mode=0o700)
            details = ancestor.lstat()
        if not stat.S_ISDIR(details.st_mode) or details.st_uid not in (0, os.geteuid()):
            fail('unsafe_state')
        # Root-owned sticky ancestors such as /tmp are acceptable; the final SSH
        # state directory must always be private and not writable by others.
        shared_sticky = ancestor != path and details.st_uid == 0 and details.st_mode & stat.S_ISVTX
        if details.st_mode & 0o022 and not shared_sticky:
            fail('unsafe_state')
    return path

def safe_file(path, private=False, missing_ok=False):
    path = pathlib.Path(path)
    try:
        details = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        fail('configuration_missing')
    if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
            or details.st_uid != os.geteuid() or details.st_mode & (0o077 if private else 0o022)):
        fail('unsafe_state')
    if details.st_size > 1024 * 1024:
        fail('unsafe_state')
    return details

def validate_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {'host', 'port', 'user', 'host_key'}:
        fail('invalid_target')
    host, user, port, key = (payload[k] for k in ('host', 'user', 'port', 'host_key'))
    if (not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}', host)
            or not isinstance(user, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', user)
            or type(port) is not int or not 1 <= port <= 65535
            or not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in key)):
        fail('invalid_target')
    fields = key.split()
    if len(fields) < 2 or not fields[0].startswith(('ssh-', 'ecdsa-', 'sk-')):
        fail('invalid_target')
    try:
        encoded = base64.b64decode(fields[1], validate=True)
        length = int.from_bytes(encoded[:4], 'big')
        if length <= 0 or encoded[4:4 + length].decode('ascii') != fields[0]:
            fail('invalid_target')
    except (ValueError, UnicodeError):
        fail('invalid_target')
    return host.lower(), user, port, f'{fields[0]} {fields[1]}'

def hashed_matches(pattern, endpoint):
    try:
        empty, version, salt, expected = pattern.split('|')
        if empty or version != '1':
            return False
        salt = base64.b64decode(salt, validate=True)
        expected = base64.b64decode(expected, validate=True)
        actual = hmac.new(salt, endpoint.encode('utf-8'), hashlib.sha1).digest()
        return hmac.compare_digest(expected, actual)
    except (ValueError, UnicodeError):
        return False

def hosts_match(patterns, endpoints):
    matched = False
    for pattern in patterns.split(','):
        negated = pattern.startswith('!')
        pattern = pattern[1:] if negated else pattern
        # OpenSSH patterns use '*' and '?'; brackets in [host]:port are literal.
        expression = re.escape(pattern.lower()).replace(r'\*', '.*').replace(r'\?', '.')
        hit = any(hashed_matches(pattern, endpoint) if pattern.startswith('|')
                  else re.fullmatch(expression, endpoint.lower()) is not None for endpoint in endpoints)
        if hit and negated:
            return False
        matched = matched or hit
    return matched

def confirm_existing(contents, endpoints, key):
    already_pinned = False
    for line in contents.splitlines():
        fields = line.split()
        if not fields or fields[0].startswith('#'):
            continue
        marked = fields[0].startswith('@')
        offset = 1 if marked else 0
        if len(fields) < offset + 3 or not hosts_match(fields[offset], endpoints):
            continue
        if marked or ' '.join(fields[offset + 1:offset + 3]) != key:
            fail('host_key_conflict')
        already_pinned = True
    return already_pinned

def run_ssh(arguments):
    # stderr is deliberately not returned. A node can run a forced command;
    # stdout is bounded and must equal our fixed probe marker exactly.
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    output = bytearray()
    deadline = time.monotonic() + 20
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                fail('login_failed')
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > 4096:
                    fail('login_failed')
        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return code, bytes(output)
    except (OSError, subprocess.TimeoutExpired):
        fail('login_failed')
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()

def atomic_merge(path, original, pin):
    details = safe_file(path, missing_ok=True)
    mode = stat.S_IMODE(details.st_mode) if details else 0o600
    descriptor, temporary = tempfile.mkstemp(prefix='.node-plane-host-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write((original + ('\n' if original and not original.endswith('\n') else '') + pin + '\n').encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

def verify(payload, private_key, known_hosts, fallback_known_hosts=None, runner=run_ssh):
    host, user, port, key = validate_payload(payload)
    private_key = pathlib.Path(private_key).expanduser()
    if not private_key.is_absolute():
        fail('configuration_missing')
    safe_directory(private_key.parent)
    safe_file(private_key, private=True)
    paths = {pathlib.Path(known_hosts).expanduser()}
    if fallback_known_hosts is not None:
        paths.add(pathlib.Path(fallback_known_hosts).expanduser())
    endpoints = [f'[{host}]:{port}']
    if port == 22:
        endpoints.insert(0, host)
    pin = ','.join(endpoints) + ' ' + key
    locked, locked_parents, snapshots = [], set(), []
    try:
        for path in sorted(paths):
            safe_directory(path.parent)
            if path.parent not in locked_parents:
                lock = path.parent / '.node-plane-known-hosts.lock'
                safe_file(lock, private=True, missing_ok=True)
                descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                handle = os.fdopen(descriptor, 'r+b')
                locked.append(handle)
                locked_parents.add(path.parent)
                # Never wait indefinitely for another enrollment/update process.
                deadline = time.monotonic() + 10
                while True:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            fail('unsafe_state')
                        time.sleep(0.05)
            details = safe_file(path, missing_ok=True)
            contents = path.read_text(encoding='utf-8') if details else ''
            pinned = confirm_existing(contents, endpoints, key)
            snapshots.append((path, contents, pinned))
        with tempfile.TemporaryDirectory(prefix='.node-plane-login-', dir=private_key.parent) as temporary:
            temporary_hosts = pathlib.Path(temporary) / 'known_hosts'
            temporary_hosts.write_text(pin + '\n', encoding='utf-8')
            temporary_hosts.chmod(0o600)
            arguments = ['ssh', '-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                         '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={temporary_hosts}',
                         '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=10',
                         '-o', 'CheckHostIP=no',
                         '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
                         '-o', 'UpdateHostKeys=no', '-i', str(private_key), '-p', str(port),
                         '-l', user, '--', host, 'printf NODE_PLANE_KEY_VERIFIED']
            code, output = runner(arguments)
            if code != 0 or output != b'NODE_PLANE_KEY_VERIFIED':
                fail('login_failed')
        for path, contents, pinned in snapshots:
            if not pinned:
                atomic_merge(path, contents, pin)
    finally:
        for handle in reversed(locked):
            handle.close()

def configured_paths():
    from config import SSH_KEY, SSH_KNOWN_HOSTS_PATH
    # This is the same managed identity used by GET /api/v1/system/ssh-key on
    # fresh systemd installations where SSH_KEY has not been written yet.
    private_key = SSH_KEY or '/opt/node-plane/shared/ssh/id_ed25519'
    if not SSH_KNOWN_HOSTS_PATH:
        fail('configuration_missing')
    fallback = None if os.environ.get('SSH_KNOWN_HOSTS_PATH') else pathlib.Path.home() / '.ssh/known_hosts'
    return private_key, SSH_KNOWN_HOSTS_PATH, fallback

def main():
    try:
        if os.geteuid() != 0:
            fail('unsafe_state')
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            fail('invalid_target')
        payload = json.loads(raw)
        private_key, known_hosts, fallback = configured_paths()
        verify(payload, private_key, known_hosts, fallback)
        print('NODE_PLANE_CONTROLLER_LOGIN_VERIFIED')
    except EnrollmentError as error:
        print('NODE_PLANE_ENROLLMENT_ERROR:' + str(error))
        raise SystemExit(1)
    except Exception:
        print('NODE_PLANE_ENROLLMENT_ERROR:verification_failed')
        raise SystemExit(1)

if __name__ == '__main__':
    main()
"#;

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn controller_login_rejects_target_shell_and_option_injection() {
        let key =
            russh::keys::PrivateKey::random(&mut rand::rng(), russh::keys::Algorithm::Ed25519)
                .unwrap();
        let public = key.public_key().to_openssh().unwrap();
        let mut options = SshOptions {
            host: "node.example".into(),
            port: 22,
            user: "root".into(),
            state_dir: PathBuf::new(),
        };
        assert!(validate_target(&options, &public).is_ok());
        for invalid in [
            "-oProxyCommand=evil",
            "node;touch /tmp/file",
            "root@node.example",
            "node\nother",
            "node.example'",
            "",
        ] {
            options.host = invalid.into();
            assert!(validate_target(&options, &public).is_err());
        }
        options.host = "2001:db8::1".into();
        assert!(validate_target(&options, &public).is_ok());
        options.user = "-oControlMaster=yes".into();
        assert!(validate_target(&options, &public).is_err());
        options.user = "root".into();
        options.port = 0;
        assert!(validate_target(&options, &public).is_err());
        options.port = 22;
        assert!(validate_target(&options, &format!("{public}\nextra")).is_err());
    }
}
