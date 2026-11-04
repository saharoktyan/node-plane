use std::cmp::Ordering;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use ring::rand::SystemRandom;
use ring::signature::{Ed25519KeyPair, KeyPair};

use super::install::{self, Host, Outcome};
use super::release::{self, ARCHIVE, BINARY, SIGNATURE, STATEMENT};

fn temp(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "node-plane-update-{name}-{}-{}",
        std::process::id(),
        chrono::Utc::now().timestamp_nanos_opt().unwrap()
    ));
    fs::create_dir_all(&path).unwrap();
    path
}

fn key_pair() -> Ed25519KeyPair {
    let pkcs8 = Ed25519KeyPair::generate_pkcs8(&SystemRandom::new()).unwrap();
    Ed25519KeyPair::from_pkcs8(pkcs8.as_ref()).unwrap()
}

/// A published release in `dir`: archive, statement and signature.
fn publish(dir: &Path, keys: &Ed25519KeyPair, version: &str, reports: &str) {
    let build = dir.join("build");
    fs::create_dir_all(&build).unwrap();
    let binary = build.join(BINARY);
    fs::write(&binary, format!("#!/bin/sh\necho {reports}\n")).unwrap();
    fs::set_permissions(&binary, fs::Permissions::from_mode(0o755)).unwrap();
    assert!(
        Command::new("tar")
            .arg("-czf")
            .arg(dir.join(ARCHIVE))
            .arg("-C")
            .arg(&build)
            .arg(BINARY)
            .status()
            .unwrap()
            .success()
    );
    let archive = fs::read(dir.join(ARCHIVE)).unwrap();
    let statement = release::statement(version, &crate::protocols::sha256_hex(&archive));
    fs::write(dir.join(STATEMENT), &statement).unwrap();
    fs::write(dir.join(SIGNATURE), keys.sign(statement.as_bytes())).unwrap();
}

/// Serves `file:///…` style bases straight from disk.
fn local_fetch(url: &str, dest: &Path, _limit: u64) -> Result<(), String> {
    let path = url.strip_prefix("https://release").unwrap();
    fs::copy(path, dest)
        .map(|_| ())
        .map_err(|_| "agent_update_download_failed".into())
}

fn no_guard(_: &Path, _: &Path, _: &Path) -> Result<(), String> {
    Ok(())
}

fn failing_guard(_: &Path, _: &Path, _: &Path) -> Result<(), String> {
    Err("agent_update_guard_failed".into())
}

struct Fixture {
    root: PathBuf,
    host: Host,
    keys: Ed25519KeyPair,
}

impl Fixture {
    fn new(name: &str) -> Self {
        let root = temp(name);
        let binary = root.join("bin/node-plane-agent");
        fs::create_dir_all(binary.parent().unwrap()).unwrap();
        fs::write(&binary, "old").unwrap();
        let keys = key_pair();
        let host = Host {
            binary,
            stage: root.join("state/update"),
            key: Some(keys.public_key().as_ref().to_vec()),
            own: "1.0.0-beta.2".into(),
            fetch: local_fetch,
            schedule: no_guard,
        };
        Self { root, host, keys }
    }

    fn release(&self, version: &str) -> String {
        let dir = self.root.join("release").join(version);
        fs::create_dir_all(&dir).unwrap();
        publish(&dir, &self.keys, version, version);
        format!("https://release{}", dir.display())
    }
}

#[test]
fn versions_order_like_semver() {
    let order = |a, b| release::compare(a, b).unwrap();
    assert_eq!(order("1.0.0", "1.0.0"), Ordering::Equal);
    assert_eq!(order("1.0.0", "1.0.0-beta.11"), Ordering::Greater);
    assert_eq!(order("1.0.0-beta.11", "1.0.0-beta.2"), Ordering::Greater);
    assert_eq!(order("1.0.0-beta.1", "1.0.0-alpha.9"), Ordering::Greater);
    assert_eq!(order("1.0.0-beta", "1.0.0-beta.1"), Ordering::Less);
    assert_eq!(order("1.0.0-1", "1.0.0-alpha"), Ordering::Less);
    assert_eq!(order("0.10.0", "0.9.9"), Ordering::Greater);
    for bad in [
        "1.0",
        "1.0.0.0",
        "v1.0.0",
        "1.0.0-",
        "1.0.0-a..b",
        "1.x.0",
        "",
    ] {
        assert!(!release::valid_version(bad), "{bad}");
    }
}

#[test]
fn statements_are_checked_against_the_key_and_layout() {
    let keys = key_pair();
    let other = key_pair();
    let sha = "a".repeat(64);
    let statement = release::statement("1.2.3", &sha);
    let signature = keys.sign(statement.as_bytes());
    let public = keys.public_key().as_ref();
    assert_eq!(
        release::verify(statement.as_bytes(), signature.as_ref(), public).unwrap(),
        release::Release {
            version: "1.2.3".into(),
            sha256: sha.clone()
        }
    );
    assert_eq!(
        release::verify(
            statement.as_bytes(),
            signature.as_ref(),
            other.public_key().as_ref()
        ),
        Err("agent_update_signature_invalid".into())
    );
    let changed = statement.replace("1.2.3", "1.2.4");
    assert!(release::verify(changed.as_bytes(), signature.as_ref(), public).is_err());
    for bad in [
        statement.replace("linux-amd64\n", "linux-arm64\n"),
        statement.replace("sha256 ", "sha512 "),
        statement.trim_end().to_string(),
        format!("{statement}extra 1\n"),
        statement.replace(&sha, &"A".repeat(64)),
    ] {
        let signature = keys.sign(bad.as_bytes());
        assert_eq!(
            release::verify(bad.as_bytes(), signature.as_ref(), public),
            Err("agent_update_statement_invalid".into()),
            "{bad}"
        );
    }
}

#[test]
fn update_installs_a_signed_release_and_keeps_the_previous_binary() {
    let fixture = Fixture::new("install");
    let base = fixture.release("1.0.0");
    let host = &fixture.host;
    assert_eq!(install::update(host, &base, "1.0.0"), Ok(Outcome::Updating));
    let installed = Command::new(&host.binary).arg("version").output().unwrap();
    assert_eq!(String::from_utf8_lossy(&installed.stdout).trim(), "1.0.0");
    assert_eq!(
        fs::metadata(&host.binary).unwrap().permissions().mode() & 0o777,
        0o755
    );
    assert_eq!(fs::read(install::previous(&host.binary)).unwrap(), b"old");
    assert!(!host.stage.join("download").exists());
    // A second request waits for the started agent to confirm.
    assert_eq!(
        install::update(host, &base, "1.0.0"),
        Err("agent_update_in_progress".into())
    );
    assert!(!install::confirm(&host.stage, "1.0.0-beta.2"));
    assert!(install::confirm(&host.stage, "1.0.0"));
    assert_eq!(install::guard(&host.binary, &host.stage), Ok(None));
    assert_eq!(
        fs::read_to_string(&host.binary).unwrap().trim_end(),
        "#!/bin/sh\necho 1.0.0"
    );
}

#[test]
fn guard_restores_the_previous_binary_unless_confirmed() {
    let fixture = Fixture::new("guard");
    let base = fixture.release("1.0.0");
    let host = &fixture.host;
    install::update(host, &base, "1.0.0").unwrap();
    assert_eq!(
        install::guard(&host.binary, &host.stage),
        Ok(Some("1.0.0".into()))
    );
    assert_eq!(fs::read(&host.binary).unwrap(), b"old");
    assert_eq!(install::rolled_back(&host.stage).as_deref(), Some("1.0.0"));
    // The same version is not retried until asked explicitly.
    assert_eq!(
        install::update(host, &base, "1.0.0"),
        Err("agent_update_rolled_back".into())
    );
    install::clear_rolled_back(&host.stage);
    assert_eq!(install::update(host, &base, "1.0.0"), Ok(Outcome::Updating));
}

#[test]
fn update_refuses_without_changing_the_binary() {
    let fixture = Fixture::new("refuse");
    let base = fixture.release("1.0.0");
    let host = &fixture.host;
    let unchanged = |result: Result<Outcome, String>, code: &str| {
        assert_eq!(result, Err(code.to_string()));
        assert_eq!(fs::read(&host.binary).unwrap(), b"old");
        assert!(!host.stage.join("pending").exists());
    };
    assert_eq!(
        install::update(host, &base, "1.0.0-beta.2"),
        Ok(Outcome::Current)
    );
    unchanged(
        install::update(host, &base, "1.0.0-beta.1"),
        "agent_update_downgrade",
    );
    unchanged(
        install::update(host, &base, "1.0.1"),
        "agent_update_version_mismatch",
    );
    unchanged(
        install::update(host, "https://release/missing", "1.0.0"),
        "agent_update_download_failed",
    );
    unchanged(
        install::update(host, "ftp://x", "1.0.0"),
        "agent_update_invalid",
    );
    unchanged(
        install::update(host, &format!("{base}/"), "1.0.0"),
        "agent_update_invalid",
    );
    unchanged(
        install::update(host, &base, "v1.0.0"),
        "agent_update_invalid",
    );

    // A release published for another version under this name.
    let renamed = fixture.root.join("release/renamed");
    fs::create_dir_all(&renamed).unwrap();
    publish(&renamed, &fixture.keys, "1.0.1", "1.0.1");
    let renamed = format!("https://release{}", renamed.display());
    unchanged(
        install::update(host, &renamed, "1.0.0"),
        "agent_update_version_mismatch",
    );

    // Signed by someone else.
    let forged = fixture.root.join("release/forged");
    fs::create_dir_all(&forged).unwrap();
    publish(&forged, &key_pair(), "1.0.0", "1.0.0");
    let forged_base = format!("https://release{}", forged.display());
    unchanged(
        install::update(host, &forged_base, "1.0.0"),
        "agent_update_signature_invalid",
    );

    // A swapped archive.
    let swapped = fixture.root.join("release/swapped");
    fs::create_dir_all(&swapped).unwrap();
    publish(&swapped, &fixture.keys, "1.0.0", "1.0.0");
    publish(&forged, &fixture.keys, "1.0.0", "other");
    fs::copy(forged.join(ARCHIVE), swapped.join(ARCHIVE)).unwrap();
    let swapped = format!("https://release{}", swapped.display());
    unchanged(
        install::update(host, &swapped, "1.0.0"),
        "agent_update_checksum_mismatch",
    );

    // A binary that does not report the signed version.
    unchanged(
        install::update(host, &forged_base, "1.0.0"),
        "agent_update_self_check_failed",
    );

    let no_key = Host {
        key: None,
        ..clone(host)
    };
    unchanged(
        install::update(&no_key, &base, "1.0.0"),
        "agent_update_key_missing",
    );
    let no_guard = Host {
        schedule: failing_guard,
        ..clone(host)
    };
    unchanged(
        install::update(&no_guard, &base, "1.0.0"),
        "agent_update_guard_failed",
    );
}

fn clone(host: &Host) -> Host {
    Host {
        binary: host.binary.clone(),
        stage: host.stage.clone(),
        key: host.key.clone(),
        own: host.own.clone(),
        fetch: host.fetch,
        schedule: host.schedule,
    }
}

#[test]
fn version_command_reports_the_build_version() {
    assert_eq!(super::run(&["version".to_string()]), Some(0));
    assert_eq!(super::run(&["self-update".to_string()]), Some(1));
    assert_eq!(super::run(&["other".to_string()]), None);
}

/// One-shot HTTP server for the real downloader.
fn serve(body: &'static [u8], status: &'static str) -> String {
    use std::io::{Read, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    std::thread::spawn(move || {
        if let Ok((mut stream, _)) = listener.accept() {
            let mut request = [0u8; 4096];
            let _ = stream.read(&mut request);
            let _ = write!(
                stream,
                "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            let _ = stream.write_all(body);
        }
    });
    format!("http://127.0.0.1:{port}/asset")
}

#[test]
fn downloader_fetches_and_fails_closed() {
    let dir = temp("fetch");
    let dest = dir.join("asset");
    install::fetch(&serve(b"release", "200 OK"), &dest, 1024).unwrap();
    assert_eq!(fs::read(&dest).unwrap(), b"release");
    assert_eq!(
        install::fetch(&serve(b"gone", "404 Not Found"), &dest, 1024),
        Err("agent_update_download_failed".into())
    );
    assert_eq!(
        install::fetch(&serve(&[0u8; 4096], "200 OK"), &dest, 1024),
        Err("agent_update_download_failed".into())
    );
}
