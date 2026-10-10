//! Backend-owned updates and controller-key enrollment. A lost dispatch response
//! is reconciled by its durable identity, never by replaying the installation.
use crate::{
    backend::{self, Credential},
    config::{Action, Request},
    enrollment,
    events::{self, Event},
    operation_store::{self, Intent, OperationLock, UpdateRecord},
    ssh::{self, Interaction, SshOptions, SshSession},
};
use anyhow::{Context, Result, bail, ensure};
use hyper::Method;
use serde_json::{Value, json};
use std::{
    sync::{Arc, atomic::Ordering, mpsc},
    time::{Duration, Instant},
};
use uuid::Uuid;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NodeSnapshot {
    pub key: String,
    pub title: String,
    pub region: String,
    pub status: String,
    pub phase: String,
    pub error: String,
}
impl NodeSnapshot {
    pub fn status_label(&self) -> String {
        if self.phase.is_empty() {
            self.status.clone()
        } else {
            format!("{} · {}", self.status, self.phase)
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UpdateSnapshot {
    pub id: Uuid,
    pub status: String,
    pub phase: String,
    pub components: Vec<(String, String)>,
    pub nodes: Vec<NodeSnapshot>,
    pub error: String,
    pub rollback: String,
}

fn text(value: &Value) -> String {
    value
        .as_str()
        .unwrap_or_default()
        .chars()
        .filter(|c| !c.is_control())
        .take(240)
        .collect()
}
impl UpdateSnapshot {
    fn parse(value: &Value, expected: Uuid) -> Result<Self> {
        ensure!(
            value
                .get("id")
                .and_then(Value::as_str)
                .and_then(|s| Uuid::parse_str(s).ok())
                == Some(expected),
            "Backend returned another update's identity."
        );
        ensure!(
            value["kind"] == "stack",
            "Backend returned a different operation type."
        );
        let result = &value["result"];
        let mut nodes = value["items"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|item| NodeSnapshot {
                key: text(&item["node_key"]),
                title: text(&item["title"]),
                region: text(&item["region"]),
                status: text(&item["status"]),
                phase: text(&item["phase"]),
                error: text(&item["error_code"]),
            })
            .collect::<Vec<_>>();
        nodes.sort_by(|a, b| (&a.region, &a.title, &a.key).cmp(&(&b.region, &b.title, &b.key)));
        Ok(Self {
            id: expected,
            status: text(&value["status"]),
            phase: text(&result["phase"]),
            components: ["backend", "worker", "driver", "telegram"]
                .iter()
                .map(|name| {
                    (
                        name.to_string(),
                        result["components"][name]
                            .as_str()
                            .unwrap_or("waiting")
                            .to_string(),
                    )
                })
                .collect(),
            nodes,
            error: text(&result["error_code"]),
            rollback: text(&result["rollback_status"]),
        })
    }
    fn terminal(&self) -> bool {
        matches!(
            self.status.as_str(),
            "succeeded" | "partial" | "rolled_back" | "cancelled"
        )
    }
    fn outcome(&self) -> Result<String> {
        match self.status.as_str() {
            "succeeded" => Ok(format!(
                "The whole stack update completed. Operation: {}",
                self.id
            )),
            "partial" => {
                let nodes = self
                    .nodes
                    .iter()
                    .filter(|n| matches!(n.status.as_str(), "blocked" | "failed"))
                    .map(|n| {
                        format!(
                            "{}: {}{}",
                            if n.title.is_empty() { &n.key } else { &n.title },
                            n.error,
                            if n.phase.is_empty() {
                                String::new()
                            } else {
                                format!(" ({})", n.phase)
                            }
                        )
                    })
                    .collect::<Vec<_>>()
                    .join("\n");
                bail!(
                    "Controller updated; some nodes need attention.\n{nodes}\nOperation: {}",
                    self.id
                )
            }
            "rolled_back" => bail!(
                "The controller update failed; the backend confirmed restoration of the previous stack. {}\nOperation: {}",
                self.error,
                self.id
            ),
            "cancelled" => bail!(
                "The backend cancelled this update before completion. Operation: {}",
                self.id
            ),
            _ => bail!(
                "The update needs attention: {}. No uncertain action was replayed. Operation: {}",
                self.error,
                self.id
            ),
        }
    }
}

fn controller_options(request: &Request) -> SshOptions {
    SshOptions {
        host: request.host.clone(),
        port: request.port,
        user: request.user.clone(),
        state_dir: request.state_dir.clone(),
    }
}
fn stage(tx: &mpsc::Sender<Event>, label: &str) {
    let _ = tx.send(Event::Stage(label.into()));
}

pub async fn execute(
    request: Request,
    interaction: Arc<dyn Interaction>,
    tx: mpsc::Sender<Event>,
) -> Result<String> {
    request.validate()?;
    stage(&tx, "Connecting to the controller and checking host trust");
    let mut session = ssh::connect(&controller_options(&request), interaction.clone()).await?;
    let result = execute_connected(request, &mut session, interaction, tx).await;
    let _ = session.close().await;
    result
}

pub async fn execute_connected(
    request: Request,
    session: &mut SshSession,
    interaction: Arc<dyn Interaction>,
    tx: mpsc::Sender<Event>,
) -> Result<String> {
    request.validate()?;
    let _lock = OperationLock::acquire(&request.state_dir)?;
    let mut result = match request.action {
        Action::Update => update(&request, session, interaction, &tx).await,
        Action::PrepareNode => prepare_node(&request, session, interaction, &tx).await,
        _ => bail!("Unsupported workstation operation"),
    };
    if result
        .as_ref()
        .is_err_and(|error| !format!("{error:#}").contains("Saved operation:"))
        && request.action == Action::Update
        && let Ok(Some(record)) = operation_store::unfinished(&request)
    {
        let error = result.unwrap_err();
        result = Err(anyhow::anyhow!(
            "{error:#}\n{}",
            resume_message(
                &request,
                &record,
                "The existing operation identity is retained."
            )
        ));
    }
    result
}

pub(crate) async fn prepare_node(
    request: &Request,
    controller: &mut SshSession,
    interaction: Arc<dyn Interaction>,
    tx: &mpsc::Sender<Event>,
) -> Result<String> {
    let session_id = Uuid::new_v4();
    if !backend::authorized(controller, &request.workflow.account) {
        stage(tx, "Authorizing the controller administrator");
    }
    let credential =
        backend::authenticate(controller, session_id, &request.workflow.account, tx).await?;
    let result=async {
        let response=backend::request(controller,&credential,Method::GET,"/api/v1/system/ssh-key",None,None).await?;
        let public=response["public_key"].as_str().context("Controller did not return its SSH public key")?;
        let key=russh::keys::PublicKey::from_openssh(public).context("Controller returned an invalid SSH public key")?;
        let options=SshOptions{host:request.workflow.target_host.clone(),port:request.workflow.target_port,user:request.workflow.target_user.clone(),state_dir:request.state_dir.clone()};
        stage(tx,"Connecting to the target node; enrolling the workstation key if needed");
        let target_key = format!("{}@{}:{}", options.user, options.host.to_ascii_lowercase(), options.port);
        let cached = controller.peers.remove(&target_key);
        let mut target = match cached {
            Some(target) if !target.is_closed() => target,
            Some(target) => { let _ = target.close().await; ssh::connect(&options, interaction).await? },
            None => ssh::connect(&options, interaction).await?,
        };
        let result=async {
            let description=format!("Allow this controller to connect to {}@{}:{}?\n\nController public key: {}\nThe existing authorized keys remain. No VPN protocols or agent are installed by this step.",options.user,options.host,options.port,key.fingerprint(russh::keys::ssh_key::HashAlg::Sha256));
            ensure!(request.workflow.yes || events::confirm(tx,"Prepare SSH access",description)?,"Node preparation cancelled.");
            stage(tx,"Adding the controller public key and verifying controller-to-node login");
            let operation_id = Uuid::new_v4();
            let address = format!("{}@[{}]:{}", options.user, options.host, options.port);
            let fingerprint = key.fingerprint(russh::keys::ssh_key::HashAlg::Sha256).to_string();
            backend::audit_enrollment(controller, session_id, operation_id, &address, &fingerprint, "admitted").await?;
            let enrolled = async {
                target.enroll_public_key(public, operation_id).await?;
                enrollment::verify_controller_login(controller,&options,&target.server_key()).await?;
                Ok::<(), anyhow::Error>(())
            }.await;
            let outcome = if enrolled.is_ok() {"succeeded"} else {"unconfirmed"};
            let audited = backend::audit_enrollment(controller, session_id, operation_id, &address, &fingerprint, outcome).await;
            enrolled?;
            if audited.is_err() {
                anyhow::bail!("Controller SSH access is verified, but audit completion is unconfirmed. Do not repeat enrollment solely for this error. Operation: {operation_id}");
            }
            Ok(format!("Controller SSH access to {} is verified. Add the SSH node in the bot to install its agent and protocols.",options.host))
        }.await;
        if controller.persistent { controller.peers.insert(target_key, target); }
        else { let _=target.close().await; }
        result
    }.await;
    let _ = backend::revoke(controller, session_id).await;
    result
}

fn rollout_needs_update(rollout: &serde_json::Value) -> Result<bool> {
    let agents = rollout["agents_required"]
        .as_bool()
        .context("Backend did not report whether agent updates are required")?;
    let runtimes = rollout["runtimes_required"]
        .as_bool()
        .context("Backend did not report whether runtime updates are required")?;
    if agents || runtimes || rollout["driver_status"] == "required" {
        return Ok(true);
    }
    let nodes = rollout["nodes"]
        .as_array()
        .context("Backend did not report node versions")?;
    let known = rollout["driver_status"] == "current"
        && nodes
            .iter()
            .all(|node| node["agent_status"] == "current" && node["runtime_status"] == "current");
    ensure!(
        known,
        "The controller is up to date, but agent/runtime versions could not be verified. Check node connectivity in the bot; no update was submitted."
    );
    Ok(false)
}

async fn discover(
    request: &Request,
    session: &SshSession,
    credential: &Credential,
    tx: &mpsc::Sender<Event>,
) -> Result<Option<Intent>> {
    stage(
        tx,
        "Checking available releases in the installed update channel",
    );
    backend::request(
        session,
        credential,
        Method::POST,
        "/api/v1/system/updates/check",
        None,
        None,
    )
    .await?;
    let overview = backend::request(
        session,
        credential,
        Method::GET,
        "/api/v1/system/updates",
        None,
        None,
    )
    .await?;
    ensure!(
        overview["update_supported"] != false,
        "This installation does not support coordinated backend updates."
    );
    let branch = overview["branch"]
        .as_str()
        .context("Backend did not report its update channel")?;
    ensure!(
        request.branch.is_empty() || request.branch == branch,
        "The requested branch differs from the installed channel. Change the channel in the bot first."
    );
    ensure!(
        matches!(branch, "main" | "dev"),
        "Unsupported installed update channel."
    );
    let target = if request.tag.is_empty() {
        if overview["update_available"] != true {
            stage(
                tx,
                "Checking whether installed agents or runtimes need an update",
            );
            let rollout = backend::request(
                session,
                credential,
                Method::GET,
                "/api/v1/system/updates/rollout",
                None,
                None,
            )
            .await?;
            if !rollout_needs_update(&rollout)? {
                return Ok(None);
            }
            String::new()
        } else {
            text(&overview["upstream_ref"])
        }
    } else {
        request.tag.clone()
    };
    let mut offset = 0_u64;
    for _ in 0..128 {
        let page = backend::request(
            session,
            credential,
            Method::GET,
            &format!("/api/v1/system/updates/versions?offset={offset}"),
            None,
            None,
        )
        .await?;
        if let Some(items) = page["items"].as_array() {
            for item in items {
                let selected = if target.is_empty() {
                    item["action"] == "current"
                } else {
                    item["ref"] == target
                        || item["version"] == target
                        || text(&item["ref"]).trim_start_matches('v')
                            == target.trim_start_matches('v')
                };
                if selected {
                    ensure!(
                        item["allowed"] == true || item["action"] == "current",
                        "Selected release is blocked: {}",
                        text(&item["reason"])
                    );
                    // The catalog is authoritative even when the overview is stale
                    // or the user explicitly supplied the installed release tag.
                    if item["action"] == "current" {
                        let rollout = backend::request(
                            session,
                            credential,
                            Method::GET,
                            "/api/v1/system/updates/rollout",
                            None,
                            None,
                        )
                        .await?;
                        if !rollout_needs_update(&rollout)? {
                            return Ok(None);
                        }
                    }
                    let target_ref = text(&item["ref"]);
                    ensure!(
                        !target_ref.is_empty(),
                        "Selected release has no update reference."
                    );
                    return Ok(Some(Intent {
                        kind: "stack".into(),
                        target_ref,
                        branch: branch.into(),
                    }));
                }
            }
        }
        match page["next_offset"].as_u64() {
            Some(next) if next > offset => offset = next,
            _ => break,
        }
    }
    bail!(
        "The selected release is not in the backend's allowed release catalog. Check for updates in the bot."
    )
}

/// Inventory and explicit recovery through existing backend contracts. No
/// installation dispatch is replayed and no generic unlock endpoint exists.
pub async fn recover_operations(
    request: &Request,
    session: &mut SshSession,
    tx: &mpsc::Sender<Event>,
) -> Result<String> {
    let session_id = Uuid::new_v4();
    let credential =
        backend::authenticate(session, session_id, &request.workflow.account, tx).await?;
    let result = async {
        let mut lines = vec!["Operation recovery".to_string()];
        let mut offset = 0;
        let mut reviewed = std::collections::HashSet::new();
        loop {
            let page = backend::request(session, &credential, Method::GET,
                &format!("/api/v1/system/recovery?offset={offset}"), None, None).await?;
            let items = page["items"].as_array().context("Invalid recovery inventory")?;
            for item in items {
                let id = text(&item["id"]);
                let kind = text(&item["kind"]);
                if !reviewed.insert((kind.clone(), id.clone())) { continue; }
                let status = text(&item["status"]);
                let node = text(&item["node_key"]);
                lines.push(format!("{kind} · {node} · {id}: {status}"));
                lines.push(format!("  {}", recovery_reason(&text(&item["error_code"]))));
                lines.push(format!("  {}", recovery_hint(&text(&item["next_step"]))));
                if let Some(child) = item["related_id"].as_str().filter(|id| !id.is_empty()) {
                    lines.push(format!("  Installation step: {child}"));
                }
                if !request.workflow.repair { continue; }
                for action in item["actions"].as_array().into_iter().flatten() {
                    let action = action.as_str().context("Invalid recovery action")?;
                    let path = recovery_path(&kind, &id, action)?;
                    let explanation = match action {
                        "cancel" => "Cancel only an update that has not started; the backend rechecks the execution boundary.",
                        "recheck" if kind == "agent" => "Verify the configured agent identity and current build. The installer will not run again.",
                        "recheck" if kind == "bootstrap" => "Check the recorded installation step. A confirmed result lets setup continue.",
                        "recheck" if kind != "update" => "Read the saved result of this exact command. Missing evidence keeps the block.",
                        "recheck" => "Inspect existing update/rollback evidence. No installation is repeated; missing evidence keeps the block.",
                        "resolve" if kind == "bootstrap" => "Resume setup before dispatch, or adopt an already recorded child. No uncertain child is replayed.",
                        "resolve" if kind == "profile" => "Inspect and retire the uncertain journal entry after agent restart, then queue the current profile access again.",
                        "resolve" if kind == "settings" => "Inspect and retire the uncertain settings command after agent restart, then apply a new revision of current settings.",
                        "resolve" => "Resolve this exact node operation through the driver journal. Unconfirmed outcomes stay blocked.",
                        _ => bail!("Unsupported recovery action"),
                    };
                    if !events::confirm(tx, "Recover this operation?", format!("{kind}: {id}\nStatus: {status}\nNode: {node}\n\n{explanation}"))? { continue; }
                    stage(tx, "Checking the existing operation outcome");
                    match backend::request(session, &credential, Method::POST, &path, None, None).await {
                        Ok(result) => {
                            lines.push(format!("  {action}: {}", text(&result["status"])));
                            if result["status"] == "blocked" { lines.push("  The outcome is still unconfirmed; the block remains.".into()); }
                            if let Some(id) = result["replacement_id"].as_str() { lines.push(format!("  Recovery queued: {id}")); }
                        },
                        Err(_) => lines.push(format!("  {action}: result unconfirmed. Check this exact operation again; no automatic retry.")),
                    }
                }
            }
            let total = page["total"].as_u64().context("Invalid recovery inventory total")?;
            let size = page["page_size"].as_u64().filter(|v| *v > 0 && *v <= 100).context("Invalid recovery page size")?;
            offset = page["offset"].as_u64().context("Invalid recovery inventory offset")? + size;
            if offset >= total || reviewed.len() >= 100 { break; }
        }
        if reviewed.is_empty() { lines.push("No queued, running or blocked operations.".into()); }
        if reviewed.len() >= 100 { lines.push("Inventory limited to 100 operations; use the bot pagination for additional items.".into()); }
        Ok(lines.join("\n"))
    }.await;
    let _ = backend::revoke(session, session_id).await;
    result
}

fn recovery_path(kind: &str, id: &str, action: &str) -> Result<String> {
    let id = Uuid::parse_str(id).context("Invalid recovery operation identity")?;
    match (kind, action) {
        ("update", "cancel" | "recheck") => {
            Ok(format!("/api/v1/system/updates/jobs/{id}/{action}"))
        }
        ("node" | "settings" | "profile" | "bootstrap", "resolve" | "recheck")
        | ("agent", "recheck") => Ok(format!("/api/v1/system/recovery/{kind}/{id}/{action}")),
        _ => bail!("Unsupported recovery action"),
    }
}

fn recovery_reason(code: &str) -> &str {
    match code {
        "waiting_worker" => "Waiting for the worker.",
        "operation_running" => "The operation is in progress.",
        "ssh_authentication" => "The controller cannot log in over SSH.",
        "ssh_host_key" => "The SSH host key is missing or has changed.",
        "ssh_prerequisites" => "SSH needs root or passwordless sudo, systemd and sha256sum.",
        "rust_required" => "No suitable binary is available; Rust is needed to build it.",
        "build_resources" => "Not enough resources to build the agent.",
        "child_operation_blocked" => "An installation step is blocked.",
        "child_operation_missing" => "The installation step record is missing.",
        "revision_conflict" => "Server settings changed during installation.",
        "permission_denied" => "The initiating administrator no longer has access.",
        "node_agent_unavailable" => "The agent cannot be reached.",
        "node_agent_unconfigured" => "No agent connection is configured.",
        "operation_timeout" => "The operation timed out; its result is unconfirmed.",
        "journal_unconfirmed" => "The agent journal has no confirmed result.",
        "update_version_mismatch" => "The installed version differs from the requested version.",
        "update_verification_unavailable" => "The controller update outcome could not be verified.",
        _ => "The operation outcome is unconfirmed.",
    }
}

fn recovery_hint(hint: &str) -> &str {
    match hint {
        "maintenance" => "Finish controller maintenance before recovering this operation.",
        "child" => "Recover the installation step, then check the parent operation again.",
        "agent" => {
            "Check SSH access and the agent service; recheck verifies the bound agent and current build."
        }
        "journal" => "Check the recorded result first, then use journal recovery if necessary.",
        "worker" => "Refresh to see the latest status.",
        "backup" => {
            "Check the restore phase in Backups; profiles stay frozen until revocations are confirmed."
        }
        "removal" => "Open the removal status; check host access and verification credentials.",
        "update" => "Check controller services and the update journal in Diagnostics.",
        "bootstrap" => "Check the server connection and settings before continuing setup.",
        _ => "Open the operation to inspect its current state.",
    }
}

fn bind_lookup(record: &mut UpdateRecord, result: &Value) -> Result<bool> {
    let job = &result["job"];
    if job.is_null() {
        return Ok(false);
    }
    ensure!(
        job["kind"] == record.intent.kind
            && job["target_ref"] == record.intent.target_ref
            && job["branch"] == record.intent.branch,
        "The stored command identity belongs to a different update intent. No action was replayed."
    );
    let id = job["id"]
        .as_str()
        .and_then(|value| Uuid::parse_str(value).ok())
        .context("Backend lookup returned an invalid update identity")?;
    if let Some(previous) = record.job_id {
        ensure!(
            previous == id,
            "The command identity resolved to a different update job."
        );
    }
    record.job_id = Some(id);
    Ok(true)
}

async fn lookup(session: &mut SshSession, record: &mut UpdateRecord) -> Result<bool> {
    let value = backend::helper(
        session,
        json!({"version":1,"action":"lookup-update","session_id":record.id,"command_id":record.id}),
    )
    .await?;
    bind_lookup(record, &value)
}

async fn update(
    request: &Request,
    session: &mut SshSession,
    interaction: Arc<dyn Interaction>,
    tx: &mpsc::Sender<Event>,
) -> Result<String> {
    let previous = operation_store::unfinished(request)?;
    if let Some(record) = &previous {
        ensure!(
            !record.complete,
            "This update already has a confirmed terminal result. Its nonsecret history is stored locally."
        );
    }
    let auth_id = previous
        .as_ref()
        .map_or_else(Uuid::new_v4, |record| record.id);
    let account = if request.workflow.account.is_empty() {
        previous
            .as_ref()
            .map_or("", |record| record.account_id.as_str())
    } else {
        request.workflow.account.as_str()
    };
    if !backend::authorized(session, account) {
        stage(
            tx,
            "Authorizing the administrator for backend-owned updates",
        );
    }
    let mut credential = backend::authenticate(session, auth_id, account, tx).await?;
    let mut record = if let Some(record) = previous {
        ensure!(
            credential.account_id == record.account_id,
            "Update account changed; the previous operation was not replayed."
        );
        ensure!(
            request.branch.is_empty() || request.branch == record.intent.branch,
            "The unfinished update uses a different branch. Resume it without a new --branch selection."
        );
        ensure!(
            request.tag.is_empty()
                || request.tag.trim_start_matches('v')
                    == record.intent.target_ref.trim_start_matches('v'),
            "The unfinished update uses a different target. Resume that operation before selecting another release."
        );
        record
    } else {
        let intent = match discover(request, session, &credential, tx).await {
            Ok(Some(intent)) => intent,
            Ok(None) => {
                let _ = backend::revoke(session, auth_id).await;
                return Ok("The controller, agents and runtimes are up to date.".into());
            }
            Err(error) => {
                let _ = backend::revoke(session, auth_id).await;
                return Err(error);
            }
        };
        UpdateRecord {
            version: 1,
            id: auth_id,
            host: request.host.clone(),
            port: request.port,
            user: request.user.clone(),
            account_id: credential.account_id.clone(),
            intent,
            dispatch_started: false,
            job_id: None,
            complete: false,
        }
    };
    if !record.dispatch_started {
        let description = format!(
            "Update the entire stack on {} to {} ({})?\n\nBackend, worker, driver and Telegram client are updated together, followed by node agents and runtimes. The backend manages rollback if a controller component fails.\n\nOperation: {}",
            request.host, record.intent.target_ref, record.intent.branch, record.id
        );
        if !request.workflow.yes && !events::confirm(tx, "Update the whole stack", description)? {
            record.complete = true;
            if record.path(&request.state_dir).exists() {
                record.save(&request.state_dir)?;
            }
            let _ = backend::revoke(session, auth_id).await;
            return Ok("Update cancelled before submission.".into());
        }
        record.save(&request.state_dir)?;
        // Persist this fence before any bytes of the mutation leave the PC.
        record.dispatch_started = true;
        record.save(&request.state_dir)?;
        stage(tx, "Submitting the durable whole-stack update");
        let response=backend::request(session,&credential,Method::POST,"/api/v1/system/updates/run",Some(json!({"kind":record.intent.kind,"target_ref":record.intent.target_ref,"branch":record.intent.branch})),Some(record.id)).await;
        match response {
            Ok(job) => {
                // Queue replies omit identity-bound intent lookup fields on some
                // releases. Validate both UUID and immutable intent before poll.
                bind_lookup(&mut record, &json!({"job":job}))?;
                record.save(&request.state_dir)?;
            }
            Err(error) => {
                // The controller can restart after accepting the command and
                // before replying. An HTTP500 is also an uncertain dispatch.
                if lookup(session, &mut record).await.unwrap_or(false) {
                    record.save(&request.state_dir)?;
                } else if error
                    .status
                    .is_some_and(|code| (400..500).contains(&code) && code != 429)
                {
                    // Do not remove the fence unless a fresh exact lookup proves
                    // that this command was never accepted.
                    if matches!(lookup(session, &mut record).await, Ok(false)) {
                        record.complete = true;
                        record.save(&request.state_dir)?;
                        let _ = backend::revoke(session, auth_id).await;
                        return Err(error.into());
                    }
                }
            }
        }
    }
    let began = Instant::now();
    let mut authorized = Instant::now();
    let mut previous_snapshot = None;
    let mut rechecked = false;
    loop {
        if request.workflow.stop.load(Ordering::Relaxed) {
            return Ok(resume_message(
                request,
                &record,
                "Stopped observing; the backend worker continues independently.",
            ));
        }
        if began.elapsed() > Duration::from_secs(3600) {
            bail!(
                "{}",
                resume_message(
                    request,
                    &record,
                    "Observation timed out; the update was not cancelled."
                )
            );
        }
        if authorized.elapsed() > Duration::from_secs(1800) {
            credential = backend::authenticate(session, auth_id, &record.account_id, tx).await?;
            authorized = Instant::now();
        }
        if record.job_id.is_none() {
            stage(
                tx,
                "Recovering the exact submitted command identity; installation is not replayed",
            );
            match lookup(session, &mut record).await {
                Ok(true) => record.save(&request.state_dir)?,
                Ok(false) => {
                    if began.elapsed() > Duration::from_secs(30) {
                        bail!(
                            "{}",
                            resume_message(
                                request,
                                &record,
                                "Submission could not be confirmed. No action was replayed."
                            )
                        );
                    }
                }
                Err(_) => {
                    if began.elapsed() > Duration::from_secs(60) {
                        bail!(
                            "{}",
                            resume_message(
                                request,
                                &record,
                                "Controller unavailable while reconciling submission."
                            )
                        );
                    }
                    reconnect(session, request, interaction.clone()).await?;
                    credential =
                        backend::authenticate(session, auth_id, &record.account_id, tx).await?;
                }
            }
        } else if let Some(id) = record.job_id {
            match backend::request(
                session,
                &credential,
                Method::GET,
                &format!("/api/v1/system/updates/jobs/{id}"),
                None,
                None,
            )
            .await
            {
                Ok(job) => {
                    let snapshot = UpdateSnapshot::parse(&job, id)?;
                    if previous_snapshot.as_ref() != Some(&snapshot) {
                        let _ = tx.send(Event::Update(snapshot.clone()));
                        previous_snapshot = Some(snapshot.clone());
                    }
                    if snapshot.terminal() {
                        record.complete = true;
                        record.save(&request.state_dir)?;
                        let _ = backend::revoke(session, auth_id).await;
                        return snapshot.outcome();
                    }
                    if snapshot.status == "blocked" {
                        if !rechecked
                            && snapshot.phase == "core"
                            && snapshot.error == "update_verification_unavailable"
                            && snapshot.rollback != "failed"
                        {
                            // Reconcile the existing job through the backend's
                            // recovery contract. This endpoint observes outcome
                            // evidence; it never dispatches another installation.
                            rechecked = true;
                            stage(
                                tx,
                                "Rechecking the existing update outcome; no installation is repeated",
                            );
                            backend::request(
                                session,
                                &credential,
                                Method::POST,
                                &format!("/api/v1/system/updates/jobs/{id}/recheck"),
                                None,
                                None,
                            )
                            .await
                            .context("The existing update could not be rechecked. Older backends may lack recovery access through the maintenance gate; update the backend or ask an administrator to recover this job. No installation was repeated.")?;
                            continue;
                        }
                        bail!(
                            "{}\n{}",
                            snapshot.outcome().unwrap_err(),
                            resume_message(
                                request,
                                &record,
                                "The backend requires recovery. Nothing was replayed."
                            )
                        );
                    }
                    stage(
                        tx,
                        "The backend worker is updating components; Esc detaches observation",
                    );
                }
                Err(error) if error.status == Some(401) => {
                    credential =
                        backend::authenticate(session, auth_id, &record.account_id, tx).await?;
                    authorized = Instant::now();
                }
                Err(error) if error.transient() => {
                    stage(
                        tx,
                        "Waiting for the controller backend to restart; observing the same update",
                    );
                    // SSH usually survives a backend restart. Reconnect only if
                    // transport remains unavailable, keeping the same account.
                    if error.status.is_none() {
                        reconnect(session, request, interaction.clone()).await?;
                    }
                }
                Err(error) => bail!(
                    "{error}\n{}",
                    resume_message(
                        request,
                        &record,
                        "Update observation stopped; its result is unconfirmed."
                    )
                ),
            }
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

async fn reconnect(
    session: &mut SshSession,
    request: &Request,
    interaction: Arc<dyn Interaction>,
) -> Result<()> {
    let mut fresh=ssh::connect(&controller_options(request),interaction).await.context("Controller SSH is unavailable. Reopen update with its saved --operation identity after the host returns")?;
    fresh.persistent = session.persistent;
    let previous = std::mem::replace(session, fresh);
    let _ = previous.close().await;
    Ok(())
}
fn resume_message(request: &Request, record: &UpdateRecord, reason: &str) -> String {
    format!(
        "{reason}\nResume: node-plane update {} --user {} --port {} --operation {}\nUse the same --state-dir: {}\nSaved operation: {}",
        request.host,
        request.user,
        request.port,
        record.id,
        request.state_dir.display(),
        record.path(&request.state_dir).display()
    )
}

#[cfg(test)]
mod tests {
    #[test]
    fn current_release_is_not_reinstalled_when_all_components_are_current() {
        let current = serde_json::json!({"agents_required": false, "runtimes_required": false,
            "driver_status": "current", "nodes": []});
        assert!(!super::rollout_needs_update(&current).unwrap());
        let mut nodes = current.clone();
        nodes["nodes"] =
            serde_json::json!([{"agent_status": "current", "runtime_status": "current"}]);
        assert!(!super::rollout_needs_update(&nodes).unwrap());
        nodes["nodes"][0]["agent_status"] = "unknown".into();
        assert!(super::rollout_needs_update(&nodes).is_err());
        assert!(super::rollout_needs_update(&serde_json::json!({})).is_err());
        for field in ["agents_required", "runtimes_required"] {
            let mut required = current.clone();
            required[field] = true.into();
            assert!(super::rollout_needs_update(&required).unwrap());
        }
        let mut driver = current;
        driver["driver_status"] = "required".into();
        assert!(super::rollout_needs_update(&driver).unwrap());
    }

    #[test]
    fn recovery_routes_preserve_identity_and_reject_unknown_actions_or_injection() {
        let id = uuid::Uuid::new_v4().to_string();
        assert_eq!(
            super::recovery_path("update", &id, "recheck").unwrap(),
            format!("/api/v1/system/updates/jobs/{id}/recheck")
        );
        assert!(super::recovery_path("profile", &id, "unlock").is_err());
        assert!(super::recovery_path("update", "../updates/run", "cancel").is_err());
        assert!(super::recovery_path("node", &id, "install").is_err());
    }

    use super::*;
    fn record() -> UpdateRecord {
        UpdateRecord {
            version: 1,
            id: Uuid::new_v4(),
            host: "vps.example".into(),
            port: 22,
            user: "root".into(),
            account_id: Uuid::new_v4().to_string(),
            intent: Intent {
                kind: "stack".into(),
                target_ref: "v0.4.3-alpha.48".into(),
                branch: "dev".into(),
            },
            dispatch_started: true,
            job_id: None,
            complete: false,
        }
    }
    #[test]
    fn ambiguous_submission_binds_only_exact_intent_and_identity() {
        let mut record = record();
        assert!(!bind_lookup(&mut record, &json!({"job":null})).unwrap());
        let id = Uuid::new_v4();
        let mut job =
            json!({"id":id,"kind":"stack","target_ref":record.intent.target_ref,"branch":"dev"});
        job["branch"] = json!("main");
        assert!(bind_lookup(&mut record, &json!({"job":job.clone()})).is_err());
        assert!(record.job_id.is_none());
        job["branch"] = json!("dev");
        assert!(bind_lookup(&mut record, &json!({"job":job.clone()})).unwrap());
        assert_eq!(record.job_id, Some(id));
        job["id"] = json!(Uuid::new_v4());
        assert!(bind_lookup(&mut record, &json!({"job":job})).is_err());
        assert_eq!(record.job_id, Some(id));
    }
    #[test]
    fn failure_and_partial_states_are_not_success_and_empty_fleet_is_supported() {
        let id = Uuid::new_v4();
        let mut job = json!({"id":id,"kind":"stack","status":"succeeded","items":[],"result":{"components":{"backend":"succeeded"}}});
        let snapshot = UpdateSnapshot::parse(&job, id).unwrap();
        assert!(snapshot.terminal());
        assert!(snapshot.outcome().is_ok());
        assert_eq!(snapshot.components.len(), 4);
        assert!(snapshot.nodes.is_empty());
        for status in ["partial", "rolled_back", "cancelled", "blocked"] {
            job["status"] = json!(status);
            let snapshot = UpdateSnapshot::parse(&job, id).unwrap();
            assert!(snapshot.outcome().is_err());
            assert_eq!(snapshot.terminal(), status != "blocked");
        }
        assert!(UpdateSnapshot::parse(&job, Uuid::new_v4()).is_err());
    }
    #[test]
    fn nodes_sorted_by_region_and_untrusted_controls_removed() {
        let id = Uuid::new_v4();
        let job = json!({"id":id,"kind":"stack","status":"running","items":[{"node_key":"b","title":"Latvia\u{001b}[2J","region":"Europe"},{"node_key":"a","title":"Japan","region":"Asia"}]});
        let snapshot = UpdateSnapshot::parse(&job, id).unwrap();
        assert_eq!(snapshot.nodes[0].key, "a");
        assert!(!snapshot.nodes[1].title.contains('\u{001b}'));
    }
    #[test]
    fn partial_update_identifies_runtime_failure_without_blaming_agent() {
        let id = Uuid::new_v4();
        let job = json!({"id":id,"kind":"stack","status":"partial","items":[{
            "node_key":"lv1","title":"Latvia #1","status":"blocked",
            "phase":"runtime","error_code":"update_version_mismatch"
        }]});
        let snapshot = UpdateSnapshot::parse(&job, id).unwrap();
        assert_eq!(snapshot.nodes[0].status_label(), "blocked · runtime");
        let error = snapshot.outcome().unwrap_err().to_string();
        assert!(error.contains("Latvia #1: update_version_mismatch (runtime)"));
    }
}
