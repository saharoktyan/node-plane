use crate::{
    config::{Action, Request, shell_quote},
    events::Event,
    progress::{Lines, Progress, Tracker, redact},
    ssh::{self, Interaction, OutputStream, SshOptions, SshSession},
};
use anyhow::{Context, Result, ensure};
use std::{
    fs::{File, OpenOptions},
    io::Write,
    path::PathBuf,
    sync::{Arc, mpsc},
    time::{SystemTime, UNIX_EPOCH},
};

struct Journal {
    file: File,
    path: PathBuf,
    token: String,
    last_lines: Vec<String>,
    written: usize,
    truncated: bool,
    operation_id: uuid::Uuid,
    action: &'static str,
    step: usize,
}
impl Journal {
    fn open(request: &Request) -> Result<Self> {
        ssh::secure_state_directory(&request.state_dir)?;
        let timestamp = SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis();
        let path = request
            .state_dir
            .join(format!("operation-{timestamp}-{}.log", std::process::id()));
        let mut options = OpenOptions::new();
        options.create_new(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let file = options
            .open(&path)
            .context("Cannot create private operation log")?;
        ssh::secure_file(&path)?;
        Ok(Self {
            file,
            path,
            token: request.bot_token.to_string(),
            last_lines: Vec::new(),
            written: 0,
            truncated: false,
            operation_id: uuid::Uuid::new_v4(),
            action: request.action.label(),
            step: 0,
        })
    }
    fn line(&mut self, raw: &str) -> Result<String> {
        let line = redact(raw, &self.token);
        if self.written + line.len() <= 16 * 1024 * 1024 {
            writeln!(self.file, "{line}")?;
            self.written += line.len() + 1;
        } else if !self.truncated {
            writeln!(
                self.file,
                "[Log size limit reached; further command output omitted]"
            )?;
            self.truncated = true;
        }
        if !line.trim().is_empty() {
            self.last_lines.push(line.clone());
            if self.last_lines.len() > 8 {
                self.last_lines.remove(0);
            }
        }
        Ok(line)
    }
}
impl Drop for Journal {
    fn drop(&mut self) {
        use zeroize::Zeroize;
        self.token.zeroize();
    }
}

pub async fn execute(
    request: Request,
    interaction: Arc<dyn Interaction>,
    tx: mpsc::Sender<Event>,
) -> Result<String> {
    request.validate()?;
    let mut journal = Journal::open(&request)?;
    let log_path = journal.path.clone();
    let result = run(&request, interaction, &tx, &mut journal).await;
    match result {
        Ok(message) => Ok(format!("{message}\nLog: {}", log_path.display())),
        Err(error) => {
            let clean = redact(&format!("{error:#}"), &request.bot_token);
            let _ = journal.line(&clean);
            Err(anyhow::anyhow!(
                "{clean}\nLog: {}\nNo remote operation was retried automatically. Check installation before retrying.",
                log_path.display()
            ))
        }
    }
}

async fn run(
    request: &Request,
    interaction: Arc<dyn Interaction>,
    tx: &mpsc::Sender<Event>,
    journal: &mut Journal,
) -> Result<String> {
    let stage = |label: &str| {
        let _ = tx.send(Event::Stage(label.into()));
    };
    stage("Connecting to SSH and verifying the workstation key");
    let mut session = ssh::connect(
        &SshOptions {
            host: request.host.clone(),
            port: request.port,
            user: request.user.clone(),
            state_dir: request.state_dir.clone(),
        },
        interaction,
    )
    .await?;
    stage("Checking the server and administrator permissions");
    checked(&mut session, PREFLIGHT, None, journal, false, tx).await?;
    if request.action == Action::Diagnose {
        stage("Checking controller services, database, release and maintenance state");
        let mut report = diagnosis(&mut session, journal, tx).await?;
        let before = report.render();
        if request.workflow.repair {
            for check in &report.checks {
                let Some(unit) = &check.repair else { continue };
                ensure!(
                    RECOVERABLE_UNITS.contains(&unit.as_str()),
                    "Unsupported recovery action"
                );
                if crate::events::confirm(
                    tx,
                    "Recover stopped service?",
                    format!(
                        "Errors: {} · Warnings: {}\n\nDetected problem: {}\n\nStart {unit}? Maintenance and service state will be checked again. Running services, configuration and operation journals are preserved.\n\nCancel skips this repair.",
                        report.errors, report.warnings, check.detail
                    ),
                )? {
                    stage(&format!("Recovering {unit}"));
                    journal.line(&format!("Confirmed service recovery: {unit}"))?;
                    let script = diagnostics_script(Some(unit));
                    let output = checked(&mut session, &script, None, journal, false, tx).await?;
                    let result: serde_json::Value = serde_json::from_str(&output.join("\n"))?;
                    ensure!(
                        result["status"] == "dispatched",
                        "Service recovery was not confirmed: {}.\n{}",
                        result["status"],
                        before
                    );
                }
            }
            stage("Verifying the installation after recovery");
            report = diagnosis(&mut session, journal, tx).await?;
        }
        if report
            .checks
            .iter()
            .any(|check| check.id == "api" && check.status == "ok")
        {
            stage("Inspecting queued and blocked backend operations");
            match crate::workstation::recover_operations(request, &mut session, tx).await {
                Ok(detail) => report.checks.push(DiagnosticCheck {
                    id: "operations".into(),
                    status: "ok".into(),
                    detail,
                    repair: None,
                }),
                Err(_) => {
                    report.warnings += 1;
                    report.checks.push(DiagnosticCheck { id: "operations".into(), status: "warning".into(),
                        detail: "Backend operation inventory unavailable. Check administrator selection and whether the installed backend supports recovery inventory. No operation was replayed.".into(), repair: None });
                }
            }
        }
        let _ = session.close().await;
        let text = report.render();
        ensure!(report.errors == 0, "{text}");
        return Ok(text);
    }
    stage("Checking installation paths and preparing prerequisites");
    checked(&mut session, PREPARE_HOST, None, journal, false, tx).await?;
    stage("Preparing a private controller archive installation");
    let prepare = format!(
        "{}\nbranch={}\ntag={}\n{}",
        PREPARE_SOURCE_PREFIX,
        shell_quote(&request.branch),
        shell_quote(&request.tag),
        PREPARE_SOURCE_SUFFIX
    );
    let output = checked(&mut session, &prepare, None, journal, false, tx).await?;
    let work = output
        .iter()
        .find_map(|l| l.strip_prefix("NODE_PLANE_WORK_DIR "))
        .context("Server did not return its private installation directory")?;
    ensure!(
        work.starts_with("/opt/node-plane-assistant/run-")
            && work
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"/._-".contains(&b)),
        "Invalid installation directory returned by the server"
    );
    stage("Uploading the installer and private configuration");
    let upload = format!(
        "set -eu\numask 077\ncd {}\n{}\ntar -xf -\n",
        shell_quote(work),
        CHECK_WORK_OWNER
    );
    checked(
        &mut session,
        &upload,
        Some(&bundle(request)?),
        journal,
        false,
        tx,
    )
    .await?;
    let command = format!(
        "set -eu\ncd {}\n{}\ntrap 'rm -f -- installer.env' EXIT\nexport GIT_TERMINAL_PROMPT=0\npython3 source/scripts/controller_release.py download --branch {} --ref {} --destination controller\nbash controller/scripts/install.sh --progress-json --non-interactive --mode simple --install-systemd --branch {} {} --env-file \"$PWD/installer.env\"",
        shell_quote(work),
        CHECK_WORK_OWNER,
        shell_quote(&request.branch),
        shell_quote(&request.tag),
        shell_quote(&request.branch),
        if request.tag.is_empty() {
            String::new()
        } else {
            format!("--ref {}", shell_quote(&request.tag))
        }
    );
    stage("Starting the seven installation steps");
    checked(&mut session, &command, None, journal, true, tx).await?;
    stage("Verifying services, Telegram startup and backend readiness");
    checked(&mut session, VERIFY, None, journal, false, tx).await?;
    stage("Registering this workstation key with the controller administrator");
    let session_id = uuid::Uuid::new_v4();
    crate::backend::authenticate(&mut session, session_id, &request.workflow.account, tx).await?;
    crate::backend::revoke(&mut session, session_id).await?;
    let _ = session.close().await;
    Ok("Node Plane is installed. Open your Telegram bot and send /start.\nThe workstation public key is saved on the server; future connections do not need a password.".into())
}

async fn checked(
    session: &mut SshSession,
    script: &str,
    input: Option<&[u8]>,
    journal: &mut Journal,
    progress: bool,
    tx: &mpsc::Sender<Event>,
) -> Result<Vec<String>> {
    journal.step += 1;
    let attribution = serde_json::json!({
        "operation_id": journal.operation_id, "action": journal.action,
        "step": journal.step, "ssh_user": session.ssh_user,
        "device_fingerprint": session.workstation_fingerprint,
        "identity_source": "privileged_ssh_workstation"
    });
    let command = privileged(&crate::audit::script(script, &attribution));
    let mut stdout = Lines::default();
    let mut stderr = Lines::default();
    let mut output = Vec::new();
    let mut tracker = Tracker::default();
    let mut failure = None;
    let mut emit = |raw: String| {
        if failure.is_some() {
            return;
        }
        match journal.line(&raw) {
            Ok(line) => {
                if let Some(json) = line.strip_prefix("NODE_PLANE_EVENT ") {
                    if progress {
                        match serde_json::from_str::<Progress>(json)
                            .context("Malformed installer progress event")
                            .and_then(|p| {
                                tracker.accept(&p)?;
                                Ok(p)
                            }) {
                            Ok(p) => {
                                let _ = tx.send(Event::Progress(p));
                            }
                            Err(e) => failure = Some(e),
                        }
                    }
                } else if !progress && output.len() < 100 {
                    output.push(line);
                }
            }
            Err(e) => failure = Some(e),
        }
    };
    let status = session
        .run(&command, input, |stream, bytes| match stream {
            OutputStream::Stdout => stdout.feed(bytes, &mut emit),
            OutputStream::Stderr => stderr.feed(bytes, &mut emit),
        })
        .await?;
    stdout.finish(&mut emit);
    stderr.finish(&mut emit);
    if let Some(error) = failure {
        return Err(error);
    }
    ensure!(
        status == 0,
        "Remote step failed (exit {status}).\n{}",
        journal.last_lines.join("\n")
    );
    if progress {
        ensure!(
            tracker.is_complete(),
            "Installer ended without confirmed completion of all seven steps."
        );
    }
    Ok(output)
}

fn privileged(script: &str) -> String {
    let script = shell_quote(script);
    format!(
        "if [ \"$(id -u)\" = 0 ]; then /bin/bash -c {script}; else sudo -n /bin/bash -c {script}; fi"
    )
}

fn bundle(request: &Request) -> Result<zeroize::Zeroizing<Vec<u8>>> {
    let mut tar = tar::Builder::new(Vec::new());
    let contents = normalize_shell(include_bytes!("../../../scripts/controller_release.py"))?;
    append(
        &mut tar,
        "source/scripts/controller_release.py",
        contents.as_bytes(),
        0o600,
    )?;
    append(
        &mut tar,
        "installer.env",
        request.env_file().as_bytes(),
        0o600,
    )?;
    Ok(zeroize::Zeroizing::new(tar.into_inner()?))
}
fn normalize_shell(bytes: &[u8]) -> Result<String> {
    Ok(std::str::from_utf8(bytes)
        .context("Embedded installer must be UTF-8")?
        .replace("\r\n", "\n"))
}
fn append(tar: &mut tar::Builder<Vec<u8>>, path: &str, data: &[u8], mode: u32) -> Result<()> {
    let mut header = tar::Header::new_gnu();
    header.set_size(data.len() as u64);
    header.set_mode(mode);
    header.set_uid(0);
    header.set_gid(0);
    header.set_mtime(0);
    header.set_cksum();
    tar.append_data(&mut header, path, data)?;
    Ok(())
}

const PREFLIGHT: &str = r#"
set -eu
test "$(id -u)" = 0 || { echo 'Root or passwordless sudo is required.'; exit 10; }
test -d /run/systemd/system || { echo 'A running systemd host is required.'; exit 11; }
command -v stat >/dev/null
command -v tar >/dev/null
command -v flock >/dev/null
echo 'SSH, root permissions and systemd: available'
"#;

const PREPARE_HOST: &str = r#"
set -eu
umask 077
. /etc/os-release
case "$ID" in debian|ubuntu) ;; *) echo 'Automatic installation currently supports Debian/Ubuntu with Python 3.11 or 3.12.'; exit 12;; esac
for dir in /opt /opt/node-plane /opt/node-plane/shared /opt/node-plane/shared/data /opt/node-plane/shared/ssh /opt/node-plane/releases /opt/node-plane-assistant; do
    [ ! -L "$dir" ] || { echo 'An installation directory is a symlink. Refusing to modify it.'; exit 13; }
    if [ -e "$dir" ]; then
        [ -d "$dir" ] && [ "$(stat -c %u "$dir")" = 0 ] || exit 13
        mode=$(stat -c %a "$dir")
        [ $((0$mode & 0022)) -eq 0 ] || { echo 'Installation directory is writable by other users.'; exit 13; }
    fi
done
if [ -e /opt/node-plane/shared/.env ]; then
    [ ! -L /opt/node-plane/shared/.env ] && [ -f /opt/node-plane/shared/.env ] && [ "$(stat -c %u /opt/node-plane/shared/.env)" = 0 ] && [ "$(stat -c %h /opt/node-plane/shared/.env)" = 1 ] || {
        echo 'Shared configuration is not a regular root-owned file.'; exit 13;
    }
    mode=$(stat -c %a /opt/node-plane/shared/.env)
    [ $((0$mode & 0022)) -eq 0 ] || { echo 'Shared configuration is writable by other users.'; exit 13; }
fi
if [ -e /opt/node-plane/shared/data/workstation-install.incomplete ] || [ -L /opt/node-plane/shared/data/workstation-install.incomplete ]; then
    marker=/opt/node-plane/shared/data/workstation-install.incomplete
    [ ! -L "$marker" ] && [ -f "$marker" ] && [ "$(stat -c %u "$marker")" = 0 ] && [ "$(stat -c %h "$marker")" = 1 ] && [ "$(stat -c %a "$marker")" = 600 ] || {
        echo 'Partial-install marker ownership or permissions are unsafe.'; exit 13;
    }
fi
if [ -d /opt/node-plane ] && [ -n "$(ls -A /opt/node-plane)" ]; then
    [ -f /opt/node-plane/shared/data/workstation-install.incomplete ] && [ ! -L /opt/node-plane/shared/data/workstation-install.incomplete ] && [ "$(stat -c %u /opt/node-plane/shared/data/workstation-install.incomplete)" = 0 ] || {
        echo 'The installation root is not empty and has no recognized partial-install marker. It will not be changed.'; exit 14;
    }
fi
if [ -e /opt/node-plane/current ] || [ -L /opt/node-plane/current ]; then
    [ -f /opt/node-plane/shared/data/workstation-install.incomplete ] && [ ! -L /opt/node-plane/shared/data/workstation-install.incomplete ] || {
        echo 'Node Plane is already installed. Use coordinated updates in the bot; install never replaces an active stack.'; exit 14;
    }
fi
if [ -e /opt/node-plane-assistant ]; then
    [ -f /opt/node-plane-assistant/.owner ] && [ ! -L /opt/node-plane-assistant/.owner ] && [ "$(cat /opt/node-plane-assistant/.owner)" = node-plane-workstation-v1 ] || {
        echo 'The assistant directory contains unrecognized files.'; exit 15;
    }
else
    mkdir -m 700 /opt/node-plane-assistant
    printf '%s\n' node-plane-workstation-v1 > /opt/node-plane-assistant/.owner
fi
export DEBIAN_FRONTEND=noninteractive
timeout 600 apt-get update
timeout 600 apt-get install -y git ca-certificates curl python3 openssl openssh-client sudo util-linux systemd tar gzip coreutils findutils
selected=''
for candidate in python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1; then selected="$candidate"; break; fi
done
if [ -z "$selected" ]; then
    for candidate in python3.12 python3.11; do
        if apt-cache show "$candidate" >/dev/null 2>&1; then selected="$candidate"; break; fi
    done
fi
if [ -n "$selected" ]; then
    timeout 600 apt-get install -y "$selected"
else
    echo 'uv will provision an isolated Python 3.12 runtime; system Python remains unchanged'
fi
echo 'Host prerequisites are ready'
"#;

const PREPARE_SOURCE_PREFIX: &str = "set -eu\numask 077\nexport GIT_TERMINAL_PROMPT=0";
const PREPARE_SOURCE_SUFFIX: &str = r#"
work=$(mktemp -d /opt/node-plane-assistant/run-XXXXXXXX)
printf '%s\n' node-plane-workstation-v1 > "$work/.owner"
mkdir -p "$work/source/scripts"
printf 'NODE_PLANE_WORK_DIR %s\n' "$work"
"#;
const CHECK_WORK_OWNER: &str = r#"
[ ! -L "$PWD" ] && [ "$(stat -c %u .)" = 0 ] && [ "$(stat -c %a .)" = 700 ]
[ -f .owner ] && [ ! -L .owner ] && [ "$(cat .owner)" = node-plane-workstation-v1 ]
"#;

const VERIFY: &str = r#"
set -eu
for attempt in $(seq 1 20); do
    healthy=1
    for unit in node-plane-backend.service node-plane-telegram.service node-plane-driver.service node-plane-backend-worker.timer; do
        systemctl is-active --quiet "$unit" || healthy=0
    done
    curl -fsS --max-time 3 http://127.0.0.1:8080/health/ready >/dev/null || healthy=0
    [ "$healthy" = 0 ] || break
    sleep 2
done
[ "$healthy" = 1 ] || { echo 'Some services or backend readiness are unavailable. Run Check installation before retrying.'; exit 20; }
# A restart loop can briefly be active; require a stable Telegram process.
pid=$(systemctl show -p MainPID --value node-plane-telegram.service)
sleep 8
test "$pid" != 0 && test "$(systemctl show -p MainPID --value node-plane-telegram.service)" = "$pid"
systemctl is-active --quiet node-plane-telegram.service
# Validate getMe through the installed runtime without putting the token in argv.
NODE_PLANE_APP_DIR=/opt/node-plane/current NODE_PLANE_SHARED_DIR=/opt/node-plane/shared PYTHONPATH=/opt/node-plane/current/app /opt/node-plane/current/.venv/bin/python - <<'PY'
import asyncio
from config import BOT_TOKEN
from aiogram import Bot
async def check():
    async with Bot(BOT_TOKEN) as bot:
        await bot.get_me(request_timeout=10)
try:
    asyncio.run(check())
except Exception:
    raise SystemExit('Telegram token or network validation failed. Check the bot credentials and connectivity.')
PY
echo 'Backend, Telegram, driver and worker timer are ready'
"#;
const DIAGNOSE: &str = r#"
set -eu
umask 077
diagnostics_file=$(mktemp /tmp/node-plane-diagnostics.XXXXXX.py)
trap 'rm -f -- "$diagnostics_file"' EXIT
"#;

const RECOVERABLE_UNITS: &[&str] = &[
    "node-plane-backend.service",
    "node-plane-telegram.service",
    "node-plane-driver.service",
    "node-plane-backend-worker.timer",
];

fn diagnostics_script(repair: Option<&str>) -> String {
    format!(
        "{DIAGNOSE}\ncat > \"$diagnostics_file\" <<'NODE_PLANE_DIAGNOSTICS_SOURCE'\n{}\nNODE_PLANE_DIAGNOSTICS_SOURCE\npython3 \"$diagnostics_file\" {}\n",
        include_str!("../../../scripts/installation_diagnostics.py"),
        repair.map_or(String::new(), |unit| format!(
            "--repair {}",
            shell_quote(unit)
        ))
    )
}

#[derive(serde::Deserialize)]
struct Diagnosis {
    version: u32,
    checks: Vec<DiagnosticCheck>,
    errors: usize,
    warnings: usize,
}
#[derive(serde::Deserialize)]
struct DiagnosticCheck {
    id: String,
    status: String,
    detail: String,
    repair: Option<String>,
}
impl Diagnosis {
    fn render(&self) -> String {
        let mut lines = vec![
            "Summary".to_string(),
            format!("Errors: {} · Warnings: {}", self.errors, self.warnings),
            if self.errors > 0 {
                "Installation needs attention"
            } else if self.warnings > 0 {
                "Installation is usable; follow-up required"
            } else {
                "Installation is healthy"
            }
            .into(),
            "Suggested Fixes".into(),
        ];
        for check in &self.checks {
            lines.push(format!(
                "[{}] {}: {}",
                check.status.to_uppercase(),
                check.id,
                check.detail
            ));
            if let Some(unit) = &check.repair {
                lines.push(format!(
                    "  Available recovery: start {unit} (enable service recovery and confirm)."
                ));
            }
        }
        lines.join("\n")
    }
}
async fn diagnosis(
    session: &mut SshSession,
    journal: &mut Journal,
    tx: &mpsc::Sender<Event>,
) -> Result<Diagnosis> {
    let output = checked(session, &diagnostics_script(None), None, journal, false, tx).await?;
    let report: Diagnosis = serde_json::from_str(&output.join("\n"))
        .context("Invalid installation diagnostics response")?;
    ensure!(
        report.version == 1 && report.checks.len() <= 100,
        "Unsupported diagnostic report"
    );
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(unix)]
    #[test]
    fn host_audit_preserves_exit_status_and_never_logs_script_or_input() {
        let metadata =
            serde_json::json!({"ssh_user":"deploy", "device_fingerprint":"SHA256:device"});
        let script = crate::audit::script("read -r token; exit 17", &metadata);
        // A shell fixture records only logger arguments, with no real journal writes.
        let output = std::process::Command::new("/bin/bash")
            .arg("-c")
            .arg(format!(
                "logger() {{ printf '%s\\n' \"${{@:4}}\"; }}\n{script}"
            ))
            .stdin(std::process::Stdio::null())
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(17));
        let admitted: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        let completed: serde_json::Value = serde_json::from_slice(&output.stderr).unwrap();
        assert_eq!(admitted["phase"], "admitted");
        assert_eq!(completed["exit_status"], 17);
        assert!(
            !String::from_utf8(output.stdout)
                .unwrap()
                .contains("read -r token")
        );
    }

    #[test]
    fn bundle_contains_no_private_key_or_extra_files() {
        let r = Request {
            action: Action::Install,
            host: "vps.example".into(),
            port: 22,
            user: "root".into(),
            state_dir: PathBuf::new(),
            tag: "v0.4.3-alpha.48".into(),
            branch: "dev".into(),
            admin_ids: "42".into(),
            bot_token: zeroize::Zeroizing::new("123:abcdefghijklmnopqrstuvwx".into()),
            workflow: crate::config::WorkflowOptions::default(),
        };
        let bytes = bundle(&r).unwrap();
        let mut archive = tar::Archive::new(&bytes[..]);
        let paths: Vec<_> = archive
            .entries()
            .unwrap()
            .map(|e| {
                let mut e = e.unwrap();
                assert_eq!(e.header().uid().unwrap(), 0);
                let path = e.path().unwrap().to_str().unwrap().to_owned();
                if path.ends_with(".sh") {
                    use std::io::Read;
                    let mut text = String::new();
                    e.read_to_string(&mut text).unwrap();
                    assert!(!text.contains("\r\n"));
                } else {
                    assert_eq!(e.header().mode().unwrap(), 0o600);
                }
                path
            })
            .collect();
        assert_eq!(paths.len(), 2);
        assert!(paths.contains(&"installer.env".to_owned()));
        assert!(paths.contains(&"source/scripts/controller_release.py".to_owned()));
        assert!(paths.iter().all(|p| !p.contains("id_ed25519")));
    }
    #[test]
    fn windows_sources_have_linux_line_endings() {
        assert_eq!(
            normalize_shell(b"#!/bin/bash\r\nset -eu\r\n").unwrap(),
            "#!/bin/bash\nset -eu\n"
        );
    }
}
