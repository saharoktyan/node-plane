use anyhow::{Result, ensure};
use hyper::Method;
use serde_json::{Value, json};
use uuid::Uuid;

#[derive(Clone)]
pub enum Command {
    Probe,
    Overview,
    Plan {
        action: String,
        cleanup_nodes: bool,
    },
    Queue {
        plan: Uuid,
        phrase: String,
        key: Uuid,
    },
    Job(Uuid),
    Retry(Uuid, Uuid),
    Shutdown(Uuid, Uuid),
    Verify(Value),
}
impl Command {
    pub fn request(&self) -> Result<(Method, String, Option<Value>, Option<Uuid>)> {
        let root = "/api/v1/system/cleanup";
        Ok(match self {
            Self::Probe => anyhow::bail!("Installation detection uses the SSH connection"),
            Self::Overview => (Method::GET, root.into(), None, None),
            Self::Plan {
                action,
                cleanup_nodes,
            } => {
                ensure!(
                    matches!(action.as_str(), "reset" | "remove"),
                    "Invalid cleanup action"
                );
                (
                    Method::POST,
                    format!("{root}/plans"),
                    Some(json!({"action":action,"cleanup_nodes":cleanup_nodes})),
                    Some(Uuid::new_v4()),
                )
            }
            Self::Queue { plan, phrase, key } => (
                Method::POST,
                format!("{root}/jobs"),
                Some(json!({"plan_id":plan,"confirmation_phrase":phrase})),
                Some(*key),
            ),
            Self::Job(id) => (Method::GET, format!("{root}/jobs/{id}"), None, None),
            Self::Retry(id, key) => (
                Method::POST,
                format!("{root}/jobs/{id}/retry"),
                Some(json!({})),
                Some(*key),
            ),
            Self::Shutdown(id, key) => (
                Method::POST,
                format!("{root}/jobs/{id}/shutdown-ack"),
                Some(json!({})),
                Some(*key),
            ),
            Self::Verify(_) => anyhow::bail!("Host verification does not use the backend"),
        })
    }
}
#[derive(Default)]
pub struct Browser {
    pub profile: Option<Uuid>,
    pub overview: Option<Value>,
    pub plan: Option<Value>,
    pub job: Option<Value>,
    pub phrase: String,
    pub key: Option<Uuid>,
    pub cleanup_nodes: bool,
    pub selected: usize,
    pub verifying: bool,
    pub removed: bool,
    pub verification_started: Option<std::time::Instant>,
}
impl Browser {
    pub fn apply(&mut self, command: Command, value: Value) {
        match command {
            Command::Probe => {}
            Command::Overview => {
                self.job = value.get("latest_job").filter(|v| !v.is_null()).cloned();
                self.overview = Some(value);
            }
            Command::Plan { .. } => {
                self.plan = Some(value);
                self.phrase.clear();
                self.key = Some(Uuid::new_v4());
                self.selected = 0;
            }
            Command::Verify(_) => {
                self.removed = value["removed"] == true;
                self.verifying = !self.removed;
            }
            Command::Shutdown(..) => {
                self.job = Some(value);
                self.verifying = true;
                self.verification_started = Some(std::time::Instant::now());
            }
            _ => {
                self.job = Some(value);
                self.plan = None;
                self.phrase.clear();
            }
        }
    }
    pub fn verification_timed_out(&self) -> bool {
        self.verification_started
            .is_some_and(|start| start.elapsed() >= std::time::Duration::from_secs(180))
    }
    pub fn save_verification(
        &self,
        state: &std::path::Path,
        installation: &crate::connections::Installation,
    ) -> Result<()> {
        use std::io::Write;
        let overview = self
            .overview
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("Missing removal manifest"))?;
        verification_script(&overview["deployment"])?;
        let data = json!({"version":1,"host":installation.host,"port":installation.port,"user":installation.user,"overview":overview,"job":self.job});
        std::fs::create_dir_all(state)?;
        let mut temporary = tempfile::NamedTempFile::new_in(state)?;
        temporary.write_all(&serde_json::to_vec(&data)?)?;
        temporary.as_file().sync_all()?;
        temporary.persist(state.join(format!("controller-removal-{}.json", installation.id)))?;
        Ok(())
    }
    pub fn restore_verification(
        &mut self,
        state: &std::path::Path,
        installation: &crate::connections::Installation,
    ) -> Result<()> {
        let path = state.join(format!("controller-removal-{}.json", installation.id));
        if !path.try_exists()? {
            return Ok(());
        }
        let data: Value = serde_json::from_slice(&std::fs::read(path)?)?;
        ensure!(
            data["version"] == 1
                && data["host"] == installation.host
                && data["port"] == installation.port
                && data["user"] == installation.user,
            "Saved removal belongs to a different SSH connection"
        );
        verification_script(&data["overview"]["deployment"])?;
        ensure!(data["job"]["action"] == "remove", "Invalid removal record");
        self.profile = Some(installation.id);
        self.overview = Some(data["overview"].clone());
        self.job = Some(data["job"].clone());
        self.verifying = true;
        self.verification_started = Some(std::time::Instant::now());
        Ok(())
    }
    pub fn forget_verification(state: &std::path::Path, id: Uuid) -> Result<()> {
        let path = state.join(format!("controller-removal-{id}.json"));
        match std::fs::remove_file(path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e.into()),
        }
    }
    pub fn poll(&self) -> Option<Command> {
        if self.verifying && !self.removed {
            if self.verification_timed_out() {
                return None;
            }
            return self
                .overview
                .as_ref()?
                .get("deployment")
                .cloned()
                .map(Command::Verify);
        }
        let job = self.job.as_ref()?;
        if matches!(job["status"].as_str(), Some("queued" | "running")) {
            Some(Command::Job(Uuid::parse_str(job["id"].as_str()?).ok()?))
        } else {
            None
        }
    }
}

pub fn verification_script(deployment: &Value) -> Result<String> {
    ensure!(
        deployment["base_dir"].as_str().is_some()
            && deployment["shared_dir"].as_str().is_some()
            && deployment["units"].is_array(),
        "Missing installation verification manifest"
    );
    // Data is a JSON literal inside Python; no path is interpolated into shell code.
    let data = serde_json::to_string(&serde_json::to_string(deployment)?)?;
    Ok(format!(
        r#"import json, pathlib, subprocess, sys
config = json.loads({data})
paths = [config['base_dir'], config['shared_dir'], '/usr/local/bin/node-plane-driver']
removed = not any(pathlib.Path(p).exists() or pathlib.Path(p).is_symlink() for p in paths)
for unit in config['units']:
    path = pathlib.Path('/etc/systemd/system') / unit
    removed = removed and not (path.exists() or path.is_symlink())
    r = subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True, timeout=10)
    removed = removed and r.returncode in (3, 4) and r.stdout.strip() in ('inactive', 'unknown', 'failed')
if config.get('postgres_container'):
    r = subprocess.run(['docker', 'ps', '-a', '--format', '{{{{.Names}}}}'], capture_output=True, text=True, timeout=15)
    removed = removed and r.returncode == 0 and config['postgres_container'] not in r.stdout.splitlines()
print(json.dumps({{'removed': removed}}))
"#
    ))
}

pub const PROBE_SCRIPT: &str = r#"import json, pathlib, urllib.request
base = pathlib.Path('/opt/node-plane')
marker = base / '.node-plane-installation.json'
manifest = False
if marker.is_file() and not marker.is_symlink():
    record = json.loads(marker.read_text())
    manifest = record.get('format') == 'node-plane-systemd-v1' and record.get('base_dir') == str(base) and (base / 'current/app/backend/http_api.py').is_file()
ready = False
try:
    with urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=3) as response:
        ready = response.status == 200 and json.load(response).get('status') == 'alive'
except (OSError, ValueError):
    pass
print(json.dumps({'installed': manifest or ready, 'backend_ready':ready}))
"#;

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn queue_keeps_confirmation_and_idempotency_key() {
        let key = Uuid::new_v4();
        let plan = Uuid::new_v4();
        let (_, path, body, sent) = Command::Queue {
            plan,
            phrase: "RESET NODE PLANE".into(),
            key,
        }
        .request()
        .unwrap();
        assert!(path.ends_with("/jobs"));
        assert_eq!(sent, Some(key));
        assert_eq!(body.unwrap()["plan_id"], plan.to_string());
    }
    #[test]
    fn removal_requires_independent_verification() {
        let mut browser = Browser::default();
        browser.apply(
            Command::Shutdown(Uuid::new_v4(), Uuid::new_v4()),
            json!({"status":"running"}),
        );
        assert!(browser.verifying);
        assert!(!browser.removed);
        browser.apply(Command::Verify(json!({})), json!({"removed":false}));
        assert!(!browser.removed);
        browser.apply(Command::Verify(json!({})), json!({"removed":true}));
        assert!(browser.removed);
    }
    #[cfg(unix)]
    #[test]
    fn host_verification_checks_paths_services_and_database() {
        use std::io::Write;
        use std::process::{Command as Process, Stdio};
        let dir = tempfile::tempdir().unwrap();
        let base = dir.path().join("missing-controller");
        let shared = dir.path().join("missing-shared");
        let driver = dir.path().join("missing-driver");
        let manifest = json!({"base_dir":base,"shared_dir":shared,"units":["node-plane-backend.service"],"postgres_container":"node-plane-postgres"});
        let script = verification_script(&manifest)
            .unwrap()
            .replace("/usr/local/bin/node-plane-driver", driver.to_str().unwrap());
        for (active, database, failure, expected) in [
            (false, false, false, true),
            (true, false, false, false),
            (false, true, false, false),
            (false, false, true, false),
        ] {
            let prelude = format!(
                "import subprocess, types\nsubprocess.run = lambda args, **kwargs: types.SimpleNamespace(returncode=({docker_code} if args[0]=='docker' else {unit_code}), stdout=({docker_output:?} if args[0]=='docker' else {unit_output:?}))\n",
                docker_code = if failure { 1 } else { 0 },
                unit_code = if active { 0 } else { 3 },
                docker_output = if database { "node-plane-postgres" } else { "" },
                unit_output = if active { "active" } else { "inactive" }
            );
            let mut child = Process::new("python3")
                .arg("-")
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .spawn()
                .unwrap();
            child
                .stdin
                .take()
                .unwrap()
                .write_all(format!("{prelude}{script}").as_bytes())
                .unwrap();
            let output = child.wait_with_output().unwrap();
            assert!(output.status.success());
            let value: Value = serde_json::from_slice(&output.stdout).unwrap();
            assert_eq!(value["removed"], expected);
        }
        std::fs::create_dir(&base).unwrap();
        let mut child = Process::new("python3")
            .arg("-")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let no_units =
            verification_script(&json!({"base_dir":base,"shared_dir":shared,"units":[]}))
                .unwrap()
                .replace("/usr/local/bin/node-plane-driver", driver.to_str().unwrap());
        child
            .stdin
            .take()
            .unwrap()
            .write_all(no_units.as_bytes())
            .unwrap();
        let output = child.wait_with_output().unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&output.stdout).unwrap()["removed"],
            false
        );
    }
    #[test]
    fn verification_resumes_only_for_the_same_saved_connection() {
        let state = tempfile::tempdir().unwrap();
        let installation = crate::connections::Installation {
            id: Uuid::new_v4(),
            name: "Test".into(),
            host: "controller.example".into(),
            port: 22,
            user: "root".into(),
            branch: "dev".into(),
            admin_ids: "42".into(),
            account: String::new(),
            installed: true,
        };
        let browser = Browser {
            overview: Some(
                json!({"deployment":{"base_dir":"/opt/node-plane","shared_dir":"/opt/node-plane/shared","units":[]}}),
            ),
            job: Some(json!({"action":"remove","shutdown_ack":true})),
            ..Default::default()
        };
        browser
            .save_verification(state.path(), &installation)
            .unwrap();
        let mut resumed = Browser::default();
        resumed
            .restore_verification(state.path(), &installation)
            .unwrap();
        assert!(matches!(resumed.poll(), Some(Command::Verify(_))));
        let mut changed = installation.clone();
        changed.host = "other.example".into();
        assert!(
            Browser::default()
                .restore_verification(state.path(), &changed)
                .is_err()
        );
        Browser::forget_verification(state.path(), installation.id).unwrap();
        assert!(
            !state
                .path()
                .join(format!("controller-removal-{}.json", installation.id))
                .exists()
        );
    }
}
