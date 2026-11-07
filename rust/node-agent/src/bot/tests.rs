use std::cell::RefCell;

use super::*;
use crate::agent::v1::CabinetFile;

/// Records what the bot asked of the host; uv makes the environment's python.
#[derive(Default)]
struct Fake {
    calls: RefCell<Vec<String>>,
    uv_fails: bool,
}

impl Host for Fake {
    fn user(&self) -> Result<(u32, u32), String> {
        Ok((unsafe { libc::getuid() }, unsafe { libc::getgid() }))
    }
    fn systemctl(&self, args: &[&str]) -> Result<bool, String> {
        self.calls.borrow_mut().push(args.join(" "));
        Ok(args[0] != "is-active")
    }
    fn uv(&self, uv: &Path, args: &[&str], env: &[(&str, PathBuf)]) -> Result<(), String> {
        assert_eq!(fs::read(uv).unwrap(), b"uv binary");
        assert!(env.iter().any(|(name, _)| *name == "UV_CACHE_DIR"));
        if self.uv_fails {
            return Err("bot_install_failed: no network".into());
        }
        self.calls.borrow_mut().push(format!("uv {}", args[0]));
        if args[0] == "venv" {
            let bin = Path::new(args.last().unwrap()).join("bin");
            fs::create_dir_all(&bin).unwrap();
            fs::write(bin.join("python"), "").unwrap();
        }
        Ok(())
    }
}

fn paths(name: &str) -> Paths {
    let dir = std::env::temp_dir().join(format!("bot-agent-{}-{name}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    Paths {
        root: dir.join("root"),
        env: dir.join("bot.env"),
        unit: dir.join("node-plane-bot.service"),
    }
}

fn file(path: &str, content: &[u8]) -> CabinetFile {
    CabinetFile {
        path: path.into(),
        content: content.to_vec(),
    }
}

fn request(version: &str) -> ApplyBotRequest {
    ApplyBotRequest {
        version: version.into(),
        files: vec![
            file("app/telegram_client/main.py", b"print()"),
            file("app/config.py", b""),
            file("VERSION", version.as_bytes()),
            file("requirements.txt", b"aiogram==3.31.0 --hash=sha256:00"),
            file(
                "uv.zlib",
                &miniz_oxide::deflate::compress_to_vec_zlib(b"uv binary", 6),
            ),
        ],
        bot_token: "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ".into(),
        api_url: "https://cab.example.com".into(),
        relay_ca: String::new(),
        key_id: "0123456789abcdef0123456789abcdef".into(),
    }
}

#[test]
fn requests_are_checked() {
    assert_eq!(check(&request("1.0.0")), Ok(()));
    let mut wrong = request("1.0.0");
    wrong.files.push(file("../etc/passwd.py", b""));
    assert_eq!(check(&wrong), Err("bot_files_invalid".into()));
    let mut wrong = request("1.0.0");
    wrong.files.push(file("app/x.so", b""));
    assert_eq!(check(&wrong), Err("bot_files_invalid".into()));
    let mut wrong = request("1.0.0");
    wrong.files.retain(|f| f.path != "uv.zlib");
    assert_eq!(check(&wrong), Err("bot_files_invalid".into()));
    let mut wrong = request("1.0.0");
    wrong.bot_token = "123:abc\nEVIL=1".into();
    assert_eq!(check(&wrong), Err("bot_token_invalid".into()));
    let mut wrong = request("1.0.0");
    wrong.key_id = "0123456789ABCDEF0123456789ABCDEF".into();
    assert_eq!(check(&wrong), Err("bot_key_id_invalid".into()));
    let mut wrong = request("1.0.0");
    wrong.api_url = "http://cab.example.com".into();
    assert_eq!(check(&wrong), Err("bot_api_url_invalid".into()));
    assert_eq!(check(&request("../x")), Err("bot_version_invalid".into()));
    assert_eq!(check(&request("tools")), Err("bot_version_invalid".into()));
}

#[test]
fn the_key_is_made_once_before_the_bot() {
    let host = Fake::default();
    let paths = paths("key");
    assert_eq!(
        apply(&host, &paths, &request("1.0.0")).unwrap_err(),
        "bot_key_missing"
    );
    let first = key(&host, &paths).unwrap().public_key;
    assert_eq!(first.len(), 32);
    assert_eq!(key(&host, &paths).unwrap().public_key, first);
    assert_eq!(
        fs::metadata(paths.root.join("bot.key"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o400
    );
    fs::remove_dir_all(paths.root.parent().unwrap()).unwrap();
}

#[test]
fn a_version_is_installed_whole_and_run() {
    let host = Fake::default();
    let paths = paths("apply");
    key(&host, &paths).unwrap();
    let state = apply(&host, &paths, &request("1.0.0")).unwrap();
    assert_eq!(state.version, "1.0.0");
    let current = paths.root.join("current");
    assert!(current.join("app/telegram_client/main.py").is_file());
    assert!(!current.join("uv.zlib").exists(), "uv is a tool, not code");
    assert_eq!(fs::read(paths.root.join("tools/uv")).unwrap(), b"uv binary");
    let env = fs::read_to_string(&paths.env).unwrap();
    assert!(env.contains("BOT_TOKEN=123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ\n"));
    assert!(env.contains("NODE_PLANE_BACKEND_URL=https://cab.example.com\n"));
    assert!(env.contains(&format!(
        "NODE_PLANE_BOT_KEY_FILE={}",
        paths.root.join("bot.key").display()
    )));
    assert!(env.contains("NODE_PLANE_BOT_KEY_ID=0123456789abcdef0123456789abcdef\n"));
    assert!(!env.contains("RELAY_CA"));
    assert_eq!(
        fs::metadata(&paths.env).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let unit = fs::read_to_string(&paths.unit).unwrap();
    assert!(unit.contains("User=node-plane-bot") && unit.contains("-m telegram_client.main"));
    assert!(
        host.calls
            .borrow()
            .contains(&"restart node-plane-bot.service".to_string())
    );
    assert_eq!(
        host.calls
            .borrow()
            .iter()
            .filter(|c| c.starts_with("uv "))
            .count(),
        2
    );

    // The same version again builds nothing; a new one replaces it.
    host.calls.borrow_mut().clear();
    apply(&host, &paths, &request("1.0.0")).unwrap();
    assert!(!host.calls.borrow().iter().any(|c| c.starts_with("uv ")));
    let mut next = request("1.1.0");
    next.relay_ca = "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n".into();
    apply(&host, &paths, &next).unwrap();
    assert!(!paths.root.join("1.0.0").exists());
    assert!(paths.root.join("tools/uv").is_file() && paths.root.join("state").is_dir());
    assert!(
        fs::read_to_string(&paths.env)
            .unwrap()
            .contains("NODE_PLANE_RELAY_CA_FILE=")
    );
    assert!(paths.root.join("relay-ca.pem").is_file());

    // A failed build keeps the running version.
    let failing = Fake {
        uv_fails: true,
        ..Fake::default()
    };
    let error = apply(&failing, &paths, &request("1.2.0")).unwrap_err();
    assert!(error.starts_with("bot_install_failed"));
    assert_eq!(installed(&paths), "1.1.0");

    remove(&host, &paths).unwrap();
    assert!(!paths.root.exists() && !paths.unit.exists() && !paths.env.exists());
    fs::remove_dir_all(paths.root.parent().unwrap()).unwrap();
}
