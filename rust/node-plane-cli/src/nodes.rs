//! Registry browsing uses persisted state; host probes and mutations are explicit.
use crate::{
    backend,
    config::Request,
    events::{self, Event, UiInteraction},
    ssh::{self, SshOptions, SshSession},
};
use anyhow::{Context, Result, ensure};
use hyper::Method;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{
        Arc,
        atomic::{AtomicU8, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};
use uuid::Uuid;

#[derive(Clone, Debug, Deserialize)]
pub struct Node {
    pub key: String,
    pub title: String,
    pub region: String,
    pub flag: String,
    pub enabled: bool,
    pub protocols: Vec<String>,
    pub xray_transports: Vec<String>,
    pub desired_revision: u64,
    pub applied_revision: u64,
    pub transport: Option<String>,
    pub ssh_target: Option<String>,
    pub overview: Option<Value>,
    #[serde(default)]
    pub services: Option<Value>,
    #[serde(skip)]
    pub agent: AgentState,
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum AgentState {
    #[default]
    Unknown,
    Ready,
    Missing,
    Unavailable,
}
impl AgentState {
    pub fn label(self) -> &'static str {
        match self {
            Self::Unknown => "Not checked",
            Self::Ready => "Ready",
            Self::Missing => "Not installed",
            Self::Unavailable => "Unreachable",
        }
    }
}
impl Node {
    pub fn state(&self) -> &str {
        self.overview
            .as_ref()
            .and_then(|v| v["state"].as_str())
            .unwrap_or(if self.applied_revision == 0 {
                "not_installed"
            } else if !self.enabled {
                "inactive"
            } else if self.applied_revision < self.desired_revision {
                "changes_pending"
            } else {
                "applied_unverified"
            })
    }
    pub fn state_label(&self) -> &str {
        match self.state() {
            "not_installed" => "Not bootstrapped",
            "agent_only" => "Agent-only server",
            "inactive" => "Disabled",
            "changes_pending" => "Pending changes",
            "applied_unverified" => "Configured",
            "applying" => "Applying",
            "needs_attention" => "Needs attention",
            "deleting" => "Deleting",
            "deletion_blocked" => "Deletion needs attention",
            _ => "Unknown",
        }
    }
    pub fn needs_observation(&self, operation: Option<&Operation>) -> bool {
        self.state() == "applying"
            || operation.is_some_and(|operation| {
                operation.node_key == self.key
                    && matches!(
                        operation.status.as_str(),
                        "awaiting_executor" | "queued" | "running" | "blocked"
                    )
            })
    }
    pub fn can_bootstrap(&self) -> bool {
        self.state() == "not_installed"
            && self
                .overview
                .as_ref()
                .is_some_and(|v| v["settings_complete"] == true)
    }
    pub fn needs_setup(&self) -> bool {
        self.overview
            .as_ref()
            .is_some_and(|v| v["settings_complete"] == true)
            && (self.can_bootstrap()
                || self.agent == AgentState::Missing
                || self
                    .services
                    .as_ref()
                    .is_some_and(|facts| facts["docker"] == false))
    }
    pub fn protocol_status(&self, protocol: &str) -> &'static str {
        let Some(facts) = &self.services else {
            return "not checked";
        };
        if facts[format!("{protocol}_running")] == true {
            return "running";
        }
        if facts["docker"] == false {
            return "unavailable";
        }
        match facts[format!("{protocol}_installed")].as_bool() {
            Some(true) => "stopped",
            Some(false) => "missing",
            None if facts[format!("{protocol}_config_valid")] == true => "stopped",
            None if self.applied_revision == 0
                || !self.protocols.iter().any(|item| item == protocol) =>
            {
                "missing"
            }
            None => "not checked",
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Operation {
    pub kind: String,
    pub id: Uuid,
    pub node_key: String,
    pub status: String,
    pub error: String,
    #[serde(default)]
    pub command_id: Option<Uuid>,
    #[serde(default)]
    pub action: String,
}
impl Operation {
    pub fn label(&self) -> &str {
        if self.kind == "node-bootstraps" && !self.action.is_empty() {
            return &self.action;
        }
        if self.kind == "node-settings-operations" {
            return "Apply settings";
        }
        if self.kind == "agent-rollouts" {
            return if self.action.is_empty() {
                "Agent setup"
            } else {
                &self.action
            };
        }
        if self.action.starts_with("Check ports") {
            return &self.action;
        }
        match self.action.as_str() {
            "bootstrap" => "Bootstrap protocols",
            "install_docker" => "Install Docker",
            "reinstall_clean" | "reinstall_keep" => "Reinstall protocols",
            "apply_settings" => "Apply settings",
            _ => "Node operation",
        }
    }
}

#[derive(Serialize, Deserialize)]
struct SavedOperation {
    host: String,
    port: u16,
    user: String,
    account_id: String,
    operation: Operation,
}
impl SavedOperation {
    fn path(&self, state: &std::path::Path) -> std::path::PathBuf {
        state
            .join("operations")
            .join(format!("node-operation-{}.json", self.operation.id))
    }
    fn save(request: &Request, account_id: &str, operation: &Operation) -> Result<()> {
        use std::io::Write;
        let record = Self {
            host: request.host.clone(),
            port: request.port,
            user: request.user.clone(),
            account_id: account_id.into(),
            operation: operation.clone(),
        };
        crate::state_layout::directory(&request.state_dir, "operations")?;
        let path = record.path(&request.state_dir);
        if path.exists() {
            ssh::secure_file(&path)?;
        }
        let mut temporary = tempfile::NamedTempFile::new_in(crate::state_layout::directory(
            &request.state_dir,
            "operations",
        )?)?;
        ssh::secure_file(temporary.path())?;
        temporary.write_all(&serde_json::to_vec(&record)?)?;
        temporary.as_file().sync_all()?;
        temporary.persist(path).map_err(|e| e.error)?;
        Ok(())
    }
    fn latest(request: &Request, account_id: &str, key: &str) -> Result<Option<Operation>> {
        let mut latest = None;
        for item in std::fs::read_dir(crate::state_layout::directory(
            &request.state_dir,
            "operations",
        )?)? {
            let path = item?.path();
            let name = path
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or_default();
            if !name.starts_with("node-operation-") || !name.ends_with(".json") {
                continue;
            }
            ssh::secure_file(&path)?;
            ensure!(
                path.metadata()?.len() < 16384,
                "Saved node operation is oversized"
            );
            let record: Self = serde_json::from_slice(&std::fs::read(&path)?)?;
            ensure!(
                record.path(&request.state_dir) == path
                    && matches!(
                        record.operation.kind.as_str(),
                        "node-jobs"
                            | "agent-rollouts"
                            | "node-bootstraps"
                            | "node-settings-operations"
                    ),
                "Invalid saved node operation"
            );
            if record.host.eq_ignore_ascii_case(&request.host)
                && record.port == request.port
                && record.user == request.user
                && record.account_id == account_id
                && record.operation.node_key == key
            {
                let modified = path.metadata()?.modified()?;
                if latest.as_ref().is_none_or(|(time, _)| modified > *time) {
                    latest = Some((modified, record.operation));
                }
            }
        }
        Ok(latest.map(|(_, operation)| operation))
    }
}
#[derive(Serialize, Deserialize)]
struct SavedCreation {
    host: String,
    port: u16,
    user: String,
    account_id: String,
    draft: crate::node_wizard::Draft,
    status: String,
}
impl SavedCreation {
    fn path(&self, request: &Request) -> std::path::PathBuf {
        request
            .state_dir
            .join("operations")
            .join(format!("node-creation-{}.json", self.draft.command_id))
    }
    fn save(&self, request: &Request) -> Result<()> {
        use std::io::Write;
        let path = self.path(request);
        if path.exists() {
            ssh::secure_file(&path)?;
        }
        let mut temporary = tempfile::NamedTempFile::new_in(crate::state_layout::directory(
            &request.state_dir,
            "operations",
        )?)?;
        ssh::secure_file(temporary.path())?;
        temporary.write_all(&serde_json::to_vec(self)?)?;
        temporary.as_file().sync_all()?;
        temporary.persist(path).map_err(|e| e.error)?;
        Ok(())
    }
    fn pending(request: &Request, account_id: &str) -> Result<Option<Self>> {
        for entry in std::fs::read_dir(crate::state_layout::directory(
            &request.state_dir,
            "operations",
        )?)? {
            let path = entry?.path();
            let name = path
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or_default();
            if !name.starts_with("node-creation-") || !name.ends_with(".json") {
                continue;
            }
            ssh::secure_file(&path)?;
            ensure!(
                path.metadata()?.len() < 32768,
                "Saved creation is oversized"
            );
            let record: Self = serde_json::from_slice(&std::fs::read(&path)?)?;
            ensure!(
                record.path(request) == path,
                "Invalid saved creation identity"
            );
            if record.host.eq_ignore_ascii_case(&request.host)
                && record.port == request.port
                && record.user == request.user
                && record.account_id == account_id
                && record.status == "unconfirmed"
            {
                return Ok(Some(record));
            }
        }
        Ok(None)
    }
}
#[derive(Clone)]
pub enum Command {
    Controller(Option<Uuid>, crate::controller::Command),
    Access(crate::access::Command),
    Temporary(crate::temporary::Command),
    EditContext(String),
    EditReview(crate::node_editor::Draft),
    EditSave(crate::node_editor::Draft),
    ApplySettings(Node, Uuid),
    Diagnostics(String, usize),
    Recover(String, Value, String),
    Maintenance(String, Option<crate::node_maintenance::Mode>),
    MaintenancePoll(String),
    Remove(String, crate::node_maintenance::Mode, bool),
    CreationOptions,
    Create(crate::node_wizard::Draft),
    List,
    Card(String),
    Observe(Node, Option<Operation>),
    Setup(Node),
    Bootstrap(Node),
    ProtocolAction(Node, &'static str),
}
pub enum Update {
    Controller(Option<Uuid>, crate::controller::Command, Value),
    Access(crate::access::Command, Value),
    Temporary(crate::temporary::Command, Value),
    EditContext(Value, Value),
    EditReview(crate::node_editor::Draft, Value),
    Edited(Node),
    Diagnostics(String, Value, Value),
    Maintenance(crate::node_maintenance::Browser, bool),
    CreationOptions(crate::node_wizard::Options),
    Created(Node),
    List(Vec<Node>),
    Card(Node),
    Observation(Node),
    Operation(Operation),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Row {
    Region(String, usize),
    Node(usize),
}
#[derive(Default)]
pub struct Browser {
    pub items: Vec<Node>,
    pub loaded: bool,
    pub search: String,
    pub collapsed: BTreeSet<String>,
    pub page: usize,
    pub card: Option<Node>,
    pub operation: Option<Operation>,
    pub profile_id: Option<Uuid>,
    pub editor: Option<crate::node_editor::Editor>,
    pub diagnostics: Option<(Value, Value)>,
    pub recovery_confirm: Option<(Value, String)>,
    pub diagnostic_item: Option<Value>,
    pub diagnostic_page: usize,
    pub diagnostic_scroll: std::cell::Cell<usize>,
    pub diagnostic_max_scroll: std::cell::Cell<usize>,
    pub diagnostic_visible: std::cell::Cell<usize>,
    pub maintenance: Option<crate::node_maintenance::Browser>,
    pub card_scroll: usize,
}
impl Browser {
    pub fn pages(&self, capacity: usize) -> Vec<Vec<Row>> {
        let capacity = capacity.max(2);
        let term = self.search.trim().to_lowercase();
        let mut regions: BTreeMap<String, Vec<usize>> = BTreeMap::new();
        for (index, node) in self.items.iter().enumerate() {
            if [&node.title, &node.region, &node.key]
                .iter()
                .any(|s| s.to_lowercase().contains(&term))
            {
                regions.entry(node.region.clone()).or_default().push(index);
            }
        }
        let mut pages = vec![Vec::new()];
        for (region, mut nodes) in regions {
            nodes.sort_by_key(|&index| (&self.items[index].title, &self.items[index].key));
            let count = nodes.len();
            if self.collapsed.contains(&region) {
                if pages.last().unwrap().len() == capacity {
                    pages.push(Vec::new());
                }
                pages.last_mut().unwrap().push(Row::Region(region, count));
                continue;
            }
            for index in nodes {
                let page = pages.last_mut().unwrap();
                let has_region = page
                    .iter()
                    .any(|row| matches!(row, Row::Region(name, _) if name == &region));
                let needed = if has_region { 1 } else { 2 };
                if page.len() + needed > capacity {
                    pages.push(Vec::new());
                }
                let page = pages.last_mut().unwrap();
                if !page
                    .iter()
                    .any(|row| matches!(row, Row::Region(name, _) if name == &region))
                {
                    page.push(Row::Region(region.clone(), count));
                }
                page.push(Row::Node(index));
            }
        }
        pages
    }
}

fn clean(value: &Value) -> String {
    value
        .as_str()
        .unwrap_or_default()
        .chars()
        .filter(|c| !c.is_control())
        .take(240)
        .collect()
}
fn node(value: Value) -> Result<Node> {
    let mut item: Node = serde_json::from_value(value).context("Invalid node response")?;
    ensure!(
        !item.key.is_empty()
            && item.key.len() <= 64
            && item
                .key
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c)),
        "Invalid node key"
    );
    for text in [&mut item.title, &mut item.region, &mut item.flag] {
        *text = text.chars().filter(|c| !c.is_control()).take(128).collect();
    }
    Ok(item)
}
async fn maintenance_snapshot(
    session: &SshSession,
    credential: &backend::Credential,
    key: &str,
) -> Result<Value> {
    let path = format!("/api/v1/nodes/{key}");
    match backend::request(
        session,
        credential,
        Method::GET,
        &format!("{path}/removal"),
        None,
        None,
    )
    .await
    {
        Ok(value) => Ok(value),
        Err(error) if error.status == Some(404) => Ok(backend::request(
            session,
            credential,
            Method::GET,
            &format!("{path}/maintenance"),
            None,
            None,
        )
        .await?),
        Err(error) => Err(error.into()),
    }
}

async fn diagnostic_snapshot(
    session: &SshSession,
    credential: &backend::Credential,
    key: &str,
    offset: usize,
) -> Result<(Value, Value)> {
    let diagnostics_path = format!("/api/v1/nodes/{key}/diagnostics");
    let recovery_path = format!(
        "/api/v1/system/recovery?node_key={}&offset={offset}",
        encode(key)
    );
    let (facts, inventory) = tokio::join!(
        backend::request(
            session,
            credential,
            Method::GET,
            &diagnostics_path,
            None,
            None
        ),
        backend::request(session, credential, Method::GET, &recovery_path, None, None)
    );
    let facts = match facts {
        Ok(value) => {
            ensure!(
                value["node_key"] == key,
                "Diagnostics belong to another server"
            );
            value
        }
        // Recovery remains accessible when the agent is absent or unreachable.
        Err(error)
            if [
                "node_agent_unconfigured",
                "node_agent_unavailable",
                "driver_unavailable",
            ]
            .contains(&error.code.as_str()) =>
        {
            json!({"error_code":error.code})
        }
        Err(error) => return Err(error.into()),
    };
    let inventory = inventory?;
    ensure!(
        inventory["items"]
            .as_array()
            .is_some_and(|items| items.iter().all(|item| item["node_key"] == key)),
        "Recovery belongs to another server"
    );
    Ok((facts, inventory))
}

async fn apply_settings(
    session: &SshSession,
    credential: &backend::Credential,
    request: &Request,
    item: &Node,
    key: Uuid,
    tx: &mpsc::Sender<Event>,
) -> Result<()> {
    let value = backend::request_revision(
        session,
        credential,
        Method::POST,
        &format!("/api/v1/nodes/{}/apply-settings", item.key),
        None,
        Some(key),
        Some(item.desired_revision),
    )
    .await?;
    let operation = operation(&value, "node-settings-operations", &item.key)?;
    SavedOperation::save(request, &credential.account_id, &operation)?;
    let _ = tx.send(Event::Nodes(Update::Operation(operation)));
    Ok(())
}

fn operation(value: &Value, kind: &str, key: &str) -> Result<Operation> {
    ensure!(
        value["node_key"] == key,
        "Backend returned another node operation"
    );
    Ok(Operation {
        kind: kind.into(),
        id: Uuid::parse_str(value["id"].as_str().context("Missing operation ID")?)?,
        node_key: key.into(),
        status: clean(&value["status"]),
        error: clean(
            value
                .get("failure_code")
                .filter(|v| !v.is_null())
                .or_else(|| value.get("error_code"))
                .or_else(|| value.pointer("/result/error_code"))
                .unwrap_or(&Value::Null),
        ),
        command_id: None,
        action: if kind == "node-bootstraps" {
            let mut label = format!("Server setup · {}", clean(&value["phase"]));
            if let Some(detail) = value.pointer("/progress/label").and_then(Value::as_str) {
                label.push_str(" · ");
                label.push_str(&clean(&Value::String(detail.into())));
            }
            label
        } else if kind == "agent-rollouts" {
            value
                .pointer("/progress/label")
                .filter(|value| value.is_string())
                .map(clean)
                .unwrap_or_else(|| "Agent setup".into())
        } else if value["action"] == "check_ports" {
            let mut label = "Check ports".to_string();
            if value["status"] == "succeeded" {
                if let Some(ports) = value.pointer("/result/ports").and_then(Value::as_array) {
                    for port in ports {
                        label.push_str(&format!(
                            "\n{} / {} · {}",
                            port["port"]
                                .as_u64()
                                .map(|v| v.to_string())
                                .unwrap_or_else(|| clean(&port["port"])),
                            clean(&port["protocol"]),
                            clean(&port["status"])
                        ));
                    }
                }
                if value
                    .pointer("/result/firewall")
                    .is_some_and(|v| v == "unmanaged")
                {
                    label.push_str("\nFirewall is not managed by Node Plane");
                }
            }
            label
        } else {
            clean(&value["action"])
        },
    })
}

pub fn traffic_lines(overview: &Value) -> Vec<String> {
    let Some(traffic) = overview.get("traffic").filter(|value| value.is_object()) else {
        return Vec::new();
    };
    let amount = |bytes: Option<u64>, status: &str| {
        let Some(bytes) = bytes else {
            return "—".to_string();
        };
        let mut value = bytes as f64;
        let mut unit = "B";
        for next in ["KiB", "MiB", "GiB", "TiB", "PiB", "EiB"] {
            if value < 1024.0 {
                break;
            }
            value /= 1024.0;
            unit = next;
        }
        let number = if unit == "B" {
            bytes.to_string()
        } else {
            format!("{value:.1}")
        };
        format!(
            "{}{number} {unit}",
            if status == "unknown" { "≈ " } else { "" }
        )
    };
    let status = traffic["status"].as_str().unwrap_or("unknown");
    let mut parts = vec![format!(
        "All: {}",
        amount(traffic["total_bytes"].as_u64(), status)
    )];
    for (protocol, label) in [("xray", "VLESS"), ("awg", "AWG")] {
        let item = traffic["items"]
            .as_array()
            .and_then(|items| items.iter().find(|item| item["protocol"] == protocol));
        let bytes = item.and_then(|item| {
            item["uplink_bytes"]
                .as_u64()?
                .checked_add(item["downlink_bytes"].as_u64()?)
        });
        parts.push(format!(
            "{label}: {}",
            amount(
                bytes,
                item.and_then(|item| item["status"].as_str())
                    .unwrap_or("waiting")
            )
        ));
    }
    let mut lines = vec![
        format!("Traffic · {} (UTC)", clean(&traffic["month"])),
        parts.join(" · "),
    ];
    if status == "unknown" {
        lines.push("Some traffic totals may be incomplete.".into());
    }
    lines
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum ConnectionState {
    #[default]
    Closed,
    Connecting,
    Connected,
}
impl ConnectionState {
    pub fn label(self) -> &'static str {
        match self {
            Self::Closed => "SSH closed",
            Self::Connecting => "SSH connecting",
            Self::Connected => "SSH connected",
        }
    }
}
#[derive(PartialEq, Eq)]
struct SessionKey {
    host: String,
    port: u16,
    user: String,
    account: String,
    state: std::path::PathBuf,
}
impl SessionKey {
    fn from_request(request: &Request) -> Self {
        Self {
            host: request.host.to_ascii_lowercase(),
            port: request.port,
            user: request.user.clone(),
            account: request.workflow.account.clone(),
            state: request.state_dir.clone(),
        }
    }
}
enum WorkerMessage {
    Run(Box<Request>, Option<Box<Command>>, mpsc::Sender<Event>),
    Touch,
    Reset,
    Shutdown,
}
pub struct Worker {
    tx: mpsc::Sender<WorkerMessage>,
    state: Arc<AtomicU8>,
    thread: Option<std::thread::JoinHandle<()>>,
}
impl Worker {
    pub fn new() -> Result<Self> {
        Self::with_idle_timeout(Duration::from_secs(300))
    }
    pub(crate) fn with_idle_timeout(idle_timeout: Duration) -> Result<Self> {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()?;
        let (tx, rx) = mpsc::channel();
        let state = Arc::new(AtomicU8::new(0));
        let status = state.clone();
        let thread = std::thread::spawn(move || {
            let mut session: Option<(SessionKey, SshSession)> = None;
            let mut last_activity = Instant::now();
            loop {
                match rx.recv_timeout(idle_timeout.min(Duration::from_secs(1))) {
                    Ok(WorkerMessage::Run(request, command, events)) => {
                        let key = SessionKey::from_request(&request);
                        if session
                            .as_ref()
                            .is_some_and(|(saved, ssh)| saved != &key || ssh.is_closed())
                        {
                            close_session(&runtime, &mut session, &status);
                        }
                        let result = runtime.block_on(async {
                            if session.is_none() {
                                status.store(1, Ordering::Release);
                                let _ = events
                                    .send(Event::Stage("Connecting to the controller".into()));
                                let mut ssh = ssh::connect(
                                    &SshOptions {
                                        host: request.host.clone(),
                                        port: request.port,
                                        user: request.user.clone(),
                                        state_dir: request.state_dir.clone(),
                                    },
                                    UiInteraction::shared(events.clone()),
                                )
                                .await?;
                                ssh.persistent = true;
                                session = Some((key, ssh));
                                status.store(2, Ordering::Release);
                            }
                            // A fresh, audited backend credential belongs to each
                            // command, even while the verified SSH transport is reused.
                            let ssh = &mut session.as_mut().expect("connected").1;
                            if let Some(command) = command {
                                execute(*request, *command, &events, ssh).await
                            } else {
                                match request.action {
                                    crate::config::Action::Install
                                    | crate::config::Action::Diagnose => {
                                        crate::installer::execute_connected(
                                            *request,
                                            ssh,
                                            events.clone(),
                                        )
                                        .await
                                    }
                                    crate::config::Action::Update
                                    | crate::config::Action::PrepareNode => {
                                        crate::workstation::execute_connected(
                                            *request,
                                            ssh,
                                            UiInteraction::shared(events.clone()),
                                            events.clone(),
                                        )
                                        .await
                                    }
                                }
                            }
                        });
                        if session.as_ref().is_some_and(|(_, ssh)| {
                            ssh.is_closed() || backend::rejected_authorization(ssh)
                        }) {
                            close_session(&runtime, &mut session, &status);
                        }
                        last_activity = Instant::now();
                        let _ = events.send(Event::Finished(result.map_err(|e| format!("{e:#}"))));
                    }
                    Ok(WorkerMessage::Touch) => last_activity = Instant::now(),
                    Ok(WorkerMessage::Reset) => close_session(&runtime, &mut session, &status),
                    Ok(WorkerMessage::Shutdown) | Err(mpsc::RecvTimeoutError::Disconnected) => {
                        break;
                    }
                    Err(mpsc::RecvTimeoutError::Timeout) => {
                        if last_activity.elapsed() >= idle_timeout
                            || session.as_ref().is_some_and(|(_, ssh)| ssh.is_closed())
                        {
                            close_session(&runtime, &mut session, &status);
                        }
                    }
                }
            }
            close_session(&runtime, &mut session, &status);
        });
        Ok(Self {
            tx,
            state,
            thread: Some(thread),
        })
    }
    pub fn submit(
        &self,
        request: Request,
        command: Command,
        events: mpsc::Sender<Event>,
    ) -> Result<()> {
        self.tx
            .send(WorkerMessage::Run(
                Box::new(request),
                Some(Box::new(command)),
                events,
            ))
            .context("Node connection worker is unavailable")
    }
    pub fn workflow(&self, request: Request, events: mpsc::Sender<Event>) -> Result<()> {
        self.tx
            .send(WorkerMessage::Run(Box::new(request), None, events))
            .context("Connection worker is unavailable")
    }
    pub fn touch(&self) {
        let _ = self.tx.send(WorkerMessage::Touch);
    }
    pub fn reset(&self) {
        let _ = self.tx.send(WorkerMessage::Reset);
    }
    pub fn state(&self) -> ConnectionState {
        match self.state.load(Ordering::Acquire) {
            1 => ConnectionState::Connecting,
            2 => ConnectionState::Connected,
            _ => ConnectionState::Closed,
        }
    }
    pub fn shutdown(&mut self) -> Result<()> {
        let _ = self.tx.send(WorkerMessage::Shutdown);
        if let Some(thread) = self.thread.take() {
            thread
                .join()
                .map_err(|_| anyhow::anyhow!("Node connection worker failed"))?;
        }
        Ok(())
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        let _ = self.tx.send(WorkerMessage::Shutdown);
    }
}
fn close_session(
    runtime: &tokio::runtime::Runtime,
    session: &mut Option<(SessionKey, SshSession)>,
    state: &AtomicU8,
) {
    if let Some((_, ssh)) = session.take() {
        let _ = runtime.block_on(ssh.close());
    }
    state.store(0, Ordering::Release);
}

async fn execute(
    request: Request,
    command: Command,
    tx: &mpsc::Sender<Event>,
    session: &mut SshSession,
) -> Result<String> {
    crate::timings::action(
        "workstation_action",
        execute_inner(request, command, tx, session),
    )
    .await
}

/// A cheap absence check: an unhealthy or partially updated installation is still present.
async fn observe_controller_removal(
    session: &mut SshSession,
    request: &Request,
    tx: &mpsc::Sender<Event>,
) -> bool {
    let command = "sudo -n /bin/sh -c 'if [ -e /opt/node-plane ] || [ -L /opt/node-plane ] || [ -e /etc/systemd/system/node-plane-backend.service ] || [ -L /etc/systemd/system/node-plane-backend.service ]; then exit 0; else exit 42; fi'";
    if matches!(session.run(command, None, |_, _| {}).await, Ok(42)) {
        *session.authorization.lock().unwrap() = None;
        let value = json!({"installed":false,"external_removal":true,"host":request.host,"port":request.port,"user":request.user});
        let _ = tx.send(Event::Nodes(Update::Controller(
            None,
            crate::controller::Command::Probe,
            value,
        )));
        true
    } else {
        false
    }
}

pub(crate) fn controller_branch(updates: &Value) -> Result<&str> {
    updates["current_version"]
        .as_str()
        .and_then(|version| semver::Version::parse(version.trim_start_matches('v')).ok())
        .map(|version| {
            if version.pre.is_empty() {
                "main"
            } else {
                "dev"
            }
        })
        .or_else(|| updates["branch"].as_str())
        .context("Controller did not report its channel")
}

pub(crate) fn validate_controller_channel(
    command: &Command,
    installed: &str,
    expected: &str,
) -> Result<()> {
    ensure!(
        matches!(installed, "main" | "dev"),
        "Controller reported an unsupported channel."
    );
    let migration_view = matches!(
        command,
        Command::Controller(
            _,
            crate::controller::Command::Updates
                | crate::controller::Command::UpdateResult(_)
                | crate::controller::Command::UpdateVersions(_)
                | crate::controller::Command::UpdateCheck
        )
    );
    let migration_request = matches!(command, Command::Controller(_, crate::controller::Command::UpdatePreferences(value))
        if value.as_object().is_some_and(|v| v.len() == 1) && value["branch"] == expected);
    ensure!(
        installed == expected || migration_view || migration_request,
        "Controller channel is {installed}; this NPW manages {expected}. Open Node Plane Updates and switch the controller to this NPW channel, or use the matching NPW."
    );
    if let Command::Controller(_, crate::controller::Command::UpdatePreferences(value)) = command
        && let Some(branch) = value["branch"].as_str()
    {
        ensure!(
            branch == expected,
            "Transfer the profile to the matching NPW before switching controller channels."
        );
    }
    Ok(())
}

async fn execute_inner(
    mut request: Request,
    command: Command,
    tx: &mpsc::Sender<Event>,
    session: &mut SshSession,
) -> Result<String> {
    let _lock = crate::operation_store::OperationLock::acquire(&request.state_dir)?;
    let id = Uuid::new_v4();
    async {
        if matches!(command, Command::Controller(_, crate::controller::Command::Presence)) {
            observe_controller_removal(session, &request, tx).await;
            return Ok("Installation presence checked".into());
        }
        if let Command::Controller(profile, crate::controller::Command::Probe) = &command {
            let mut output = Vec::new();
            let status = session.run("sudo -n python3 -",Some(crate::controller::PROBE_SCRIPT.as_bytes()),|stream,bytes| {
                if matches!(stream,crate::ssh::OutputStream::Stdout) { output.extend_from_slice(bytes); }
            }).await?;
            ensure!(status==0,"Could not confirm the installation. Use Check installation to retry.");
            let value: Value = serde_json::from_slice(&output)?;
            if value["installed"] != true { *session.authorization.lock().unwrap()=None; }
            let _ = tx.send(Event::Nodes(Update::Controller(*profile,crate::controller::Command::Probe,value)));
            return Ok("Installation checked".into());
        }
        if let Command::Controller(profile, crate::controller::Command::Verify(deployment)) = &command {
            let script = crate::controller::verification_script(deployment)?;
            let mut output = Vec::new();
            let status = session.run("sudo -n python3 -", Some(script.as_bytes()), |stream, bytes| {
                if matches!(stream, crate::ssh::OutputStream::Stdout) { output.extend_from_slice(bytes); }
            }).await?;
            ensure!(status == 0, "Node Plane removal could not be verified. Check the systemd removal journal.");
            let value: Value = serde_json::from_slice(&output)?;
            if value["removed"] == true { *session.authorization.lock().unwrap() = None; }
            let _ = tx.send(Event::Nodes(Update::Controller(*profile, crate::controller::Command::Verify(deployment.clone()), value)));
            return Ok("Removal verification finished".into());
        }
        let fresh_authorization = !backend::authorized(session, &request.workflow.account);
        if fresh_authorization {
            let _ = tx.send(Event::Stage("Authorizing the controller administrator".into()));
        }
        let credential = match backend::authenticate(session, id, &request.workflow.account, tx).await {
            Ok(credential) => credential,
            Err(error) => {
                if observe_controller_removal(session, &request, tx).await {
                    anyhow::bail!("Node Plane is no longer installed on this host.");
                }
                return Err(error);
            }
        };
        let result = async {
            let updates = backend::request(session, &credential, Method::GET, "/api/v1/system/updates", None, None).await?;
            let installed_branch = controller_branch(&updates)?;
            if let Err(error) = validate_controller_channel(&command, installed_branch, crate::config::default_branch()) {
                if let Command::Controller(profile, _) = &command {
                    let _ = tx.send(Event::Nodes(Update::Controller(*profile, crate::controller::Command::Updates, updates.clone())));
                }
                return Err(error);
            }
            if fresh_authorization && !request.host.contains(':') {
                // Publish the selected installation address for Telegram’s local-node shorthand.
                // Older controllers may not expose this optional metadata endpoint yet.
                if let Err(error) = backend::request(session, &credential, Method::PATCH, "/api/v1/system/controller-address", Some(json!({"host":request.host})), None).await
                    && error.status != Some(404) {
                    return Err(error.into());
                }
            }
            let setup = matches!(&command, Command::Setup(_));
            let protocol_action = match &command { Command::ProtocolAction(_, action) => Some(*action), _ => None };
            match command {
                Command::Controller(profile, command) => {
                    if matches!(&command, crate::controller::Command::TelegramSettings | crate::controller::Command::TelegramToken(_) | crate::controller::Command::TelegramAdmin(..)) {
                        let mut input = json!({"version":1,"session_id":id,"action":"telegram-settings"});
                        match &command {
                            crate::controller::Command::TelegramToken(token) => {
                                ensure!(events::confirm(tx, "Change bot token?", "Verify and save the new token, then restart the Telegram bot.".into())?, "Token change cancelled");
                                input["action"] = json!("telegram-token");
                                input["token"] = json!(token.as_str());
                            }
                            crate::controller::Command::TelegramAdmin(add, telegram_id, key) => {
                                ensure!(events::confirm(tx, if *add { "Add administrator?" } else { "Remove administrator?" },
                                    format!("{} administrator rights for Telegram ID {telegram_id}.", if *add { "Grant" } else { "Remove" }))?, "Administrator change cancelled");
                                input["action"] = json!(if *add { "telegram-admin-add" } else { "telegram-admin-remove" });
                                input["telegram_id"] = json!(telegram_id);
                                input["command_id"] = json!(key);
                            }
                            _ => {}
                        }
                        let value = backend::helper(session, input).await?;
                        let _ = tx.send(Event::Nodes(Update::Controller(profile, command, value)));
                        return Ok("Telegram configuration loaded".into());
                    }
                    if matches!(&command, crate::controller::Command::Abort(_)) {
                        ensure!(events::confirm(tx, "Cancel removal?", "Keep the controller and stop further removal preparation. Nodes already removed cannot be restored by cancellation.".into())?, "Removal cancellation declined");
                    }
                    if let crate::controller::Command::BackupDelete(_, _) = &command {
                        ensure!(events::confirm(tx,"Delete backup?","This backup will be permanently deleted. Other backups and the running installation are kept.".into())?, "Backup deletion cancelled");
                    }
                    if let crate::controller::Command::BackupQueue(body, key) = &command {
                        let restore = body["action"] == "restore";
                        ensure!(events::confirm(tx, if restore { "Restore backup?" } else { "Create backup?" }, if restore { "Controller accounts, profiles, nodes and settings will be replaced with this backup. VPN access will be reconciled after restoration." } else { "Create a backup of controller data and settings." }.into())?, "Backup action cancelled");
                        use std::io::Write;
                        std::fs::create_dir_all(&request.state_dir)?;
                        let mut file = tempfile::NamedTempFile::new_in(crate::state_layout::directory(&request.state_dir, "operations")?)?;
                        file.write_all(&serde_json::to_vec(&json!({"host":request.host,"port":request.port,"user":request.user,"account":credential.account_id,"command_id":key,"body":body}))?)?;
                        file.as_file().sync_all()?;
                        file.persist(crate::state_layout::directory(&request.state_dir, "operations")?.join(format!("backup-command-{key}.json")))?;
                    }
                    if let crate::controller::Command::UpdatePreferences(value) = &command
                        && value.get("branch").is_some() {
                        ensure!(events::confirm(tx, "Change update branch?", "Switching branches can install incompatible versions and break this installation. Continue?".into())?, "Branch change cancelled");
                    }
                    if let crate::controller::Command::AnnouncementCreate(_, _, recipients) = &command {
                        ensure!(events::confirm(tx, "Send announcement?",
                            format!("This will send the message to {recipients} eligible users."))?,
                            "Announcement cancelled");
                    }
                    if let crate::controller::Command::RecoveryAction(kind, id, action, offset) = &command {
                        let explanation = match action.as_str() { "recheck" => "Check the recorded result of this exact command. Missing evidence keeps it blocked.", "resolve" => "Recover the exact driver journal before reconciling current settings. Unconfirmed outcomes stay blocked.", "resume" => "Continue the saved operation checkpoint, preserving existing child commands.", "cancel" => "Cancel only before execution. The backend checks this boundary again.", _ => anyhow::bail!("Unsupported recovery action") };
                        if !events::confirm(tx, "Recover this operation?", format!("{kind}: {id}\n\n{explanation}"))? { return Ok("Recovery cancelled".into()); }
                        let (method, path, body, key) = command.request()?;
                        let note = match backend::request(session, &credential, method, &path, body, key).await {
                            Ok(result) => format!("Operation {id}: {}", result["status"].as_str().unwrap_or("result unconfirmed")),
                            Err(error) => format!("Recovery was not confirmed: {error}. Refresh this operation before trying again."),
                        };
                        let mut page = backend::request(session, &credential, Method::GET, &format!("/api/v1/system/recovery?offset={offset}"), None, None).await?;
                        page["recovery_result"] = json!(note);
                        let _ = tx.send(Event::Nodes(Update::Controller(profile, command, page)));
                        return Ok("Recovery state loaded".into());
                    }
                    let (method, path, mut body, key) = command.request()?;
                    if let crate::controller::Command::BackupImport(file) = &command {
                        use std::io::Read;
                        let mut content = Vec::new();
                        std::fs::File::open(file)?.take(64 * 1024 * 1024 + 1).read_to_end(&mut content)?;
                        ensure!(content.len() <= 64 * 1024 * 1024, "Backup exceeds 64 MiB");
                        body = Some(serde_json::from_slice(&content)?);
                    }
                    let revision = match &command { crate::controller::Command::DefaultsSave(_,revision) => Some(*revision), _ => None };
                    let mut value = backend::request_revision(session, &credential, method, &path, body, key, revision).await.map_err(|error| anyhow::Error::new(error).context(if matches!(&command, crate::controller::Command::BackupQueue(_, _)) { "Backup submission failed or is unconfirmed. Refresh Backups before submitting another operation." } else { "Controller request failed" }))?;
                    if let crate::controller::Command::BackupExport(id, file) = &command {
                        use std::io::Write;
                        let parent = file.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or_else(|| std::path::Path::new("."));
                        let mut staged = tempfile::NamedTempFile::new_in(parent)?;
                        staged.write_all(&serde_json::to_vec_pretty(&value)?)?;
                        staged.as_file().sync_all()?;
                        staged.persist_noclobber(file).map_err(|e| e.error)?;
                        value = backend::request(session, &credential, Method::GET, &format!("/api/v1/system/backups/catalog/{id}"), None, None).await?;
                        value["exported_path"] = json!(std::fs::canonicalize(file)?.display().to_string());
                    }
                    if matches!(&command, crate::controller::Command::UpdateCheck | crate::controller::Command::UpdatePreferences(_)) {
                        value = backend::request(session, &credential, Method::GET, "/api/v1/system/updates", None, None).await?;
                    }
                    if matches!(&command, crate::controller::Command::BackupPreferences(_) | crate::controller::Command::BackupDelete(_, _)) {
                        value = backend::request(session, &credential, Method::GET, "/api/v1/system/backups", None, None).await?;
                    }
                    if matches!(&command,crate::controller::Command::Policies | crate::controller::Command::PolicySave(_, _)) {
                        let traffic = backend::request(session,&credential,Method::GET,"/api/v1/system/traffic",None,None).await?;
                        let requests = backend::request(session,&credential,Method::GET,"/api/v1/system/access-requests",None,None).await?;
                        value = json!({"traffic":traffic,"requests":requests});
                    }
                    if matches!(&command,crate::controller::Command::AlertPreferences(_) | crate::controller::Command::AlertDismiss(_)) { value = backend::request(session,&credential,Method::GET,"/api/v1/system/alerts",None,None).await?; }
                    let _ = tx.send(Event::Nodes(Update::Controller(profile, command, value)));
                    return Ok("Controller state loaded".into());
                }
                Command::Access(command) => {
                    if let crate::access::Command::EditContext { id, owner } = &command {
                        let mut context = json!({});
                        for (name, root) in [("nodes", "/api/v1/nodes"), ("regions", "/api/v1/regions")] {
                            let mut items = Vec::new(); let mut cursor = None::<String>; let mut seen = BTreeSet::new();
                            loop {
                                let path = format!("{root}?limit=100{}", cursor.as_ref().map(|c| format!("&cursor={}", encode(c))).unwrap_or_default());
                                let value = backend::request(session, &credential, Method::GET, &path, None, None).await?;
                                items.extend(value["items"].as_array().context("Missing access options")?.iter().cloned());
                                ensure!(items.len() <= 20_000, "Too many access options");
                                cursor = value["next_cursor"].as_str().map(str::to_owned);
                                let Some(next) = &cursor else { break; };
                                ensure!(seen.insert(next.clone()), "Access pagination did not advance");
                            }
                            context[name] = json!(items);
                        }
                        if let Some(owner) = owner {
                            context["owner_account"] = backend::request(session, &credential, Method::GET, &format!("/api/v1/accounts/{owner}"), None, None).await?;
                        }
                        if let Some(id) = id {
                            context["profile"] = backend::request(session, &credential, Method::GET,
                                &format!("/api/v1/profiles/{id}"), None, None).await?;
                            context["policy"] = backend::request(session, &credential, Method::GET,
                                &format!("/api/v1/profiles/{id}/access-policy"), None, None).await?;
                            ensure!(context["profile"]["desired_revision"] == context["policy"]["revision"], "Profile changed; reopen the editor");
                            if let Some(owner)=context["profile"]["owner_account_id"].as_str() {
                                let account=backend::request(session,&credential,Method::GET,&format!("/api/v1/accounts/{owner}"),None,None).await?;
                                let me=backend::request(session,&credential,Method::GET,"/api/v1/me",None,None).await?;
                                context["name_editable"]=json!(account["role"]!="admin" || account["id"]==me["id"]);
                                context["owner_account"]=account;
                            }

                        }
                        let _ = tx.send(Event::Nodes(Update::Access(command, context)));
                        return Ok("Access options loaded".into());
                    }
                    let (method, path, body, key) = command.request();
                    let revision = match &command { crate::access::Command::Mutate { revision, .. } => *revision, crate::access::Command::AccountMutate { revision, .. } | crate::access::Command::DeviceMutate { revision, .. } => Some(*revision), _ => None };
                    let mut value = backend::request_revision(session, &credential, method, &path, body, key, revision).await?;
                    if matches!(&command, crate::access::Command::Card(crate::access::Tab::Profiles, _) | crate::access::Command::ProfileProgress(_))
                        && let Some(owner) = value["owner_account_id"].as_str() {
                        let account = backend::request(session, &credential, Method::GET, &format!("/api/v1/accounts/{owner}"), None, None).await?;
                        value["_account"] = account;
                    }
                    if matches!(&command, crate::access::Command::ProfileProgress(_) | crate::access::Command::Card(crate::access::Tab::Profiles, _)) {
                        let id = value["id"].as_str().context("Missing profile identity")?;
                        let operation = backend::request(session, &credential, Method::GET, &format!("/api/v1/profiles/{id}/operation"), None, None).await?;
                        value["operation"] = operation;
                    }
                    if matches!(&command, crate::access::Command::Mutate { .. })
                        && let Some(id) = value["profile"]["id"].as_str() {
                            let operation = backend::request(session, &credential, Method::GET, &format!("/api/v1/profiles/{id}/operation"), None, None).await?;
                            value["profile"]["operation"] = operation;
                        if let Some(owner) = value["profile"]["owner_account_id"].as_str() {
                            let account = backend::request(session, &credential, Method::GET, &format!("/api/v1/accounts/{owner}"), None, None).await?;
                            value["profile"]["_account"] = account;
                        }
                    }
                    if let crate::access::Command::Devices(profile) | crate::access::Command::DeviceMutate { profile, .. } = &command {
                        let result = if matches!(command, crate::access::Command::DeviceMutate { .. }) { value.clone() } else { Value::Null };
                        let devices = if result.is_null() { value } else { backend::request(session, &credential, Method::GET, &format!("/api/v1/profiles/{profile}/devices"), None, None).await? };
                        let profile_value = backend::request(session, &credential, Method::GET, &format!("/api/v1/profiles/{profile}"), None, None).await?;
                        let operation = backend::request(session, &credential, Method::GET, &format!("/api/v1/profiles/{profile}/operation"), None, None).await?;
                        value = json!({"profile": profile_value, "devices": devices, "operation": operation, "result":result});
                    }
                    if matches!(&command, crate::access::Command::Decision { .. })
                        && let Some(account) = value["account_id"].as_str() {
                            value["_account"] = backend::request(session, &credential, Method::GET, &format!("/api/v1/accounts/{account}"), None, None).await?;
                    }
                    if let crate::access::Command::List(tab) = &command {
                        let mut cursors = BTreeSet::new();
                        while let Some(cursor) = value["next_cursor"].as_str().map(str::to_owned) {
                            ensure!(cursors.insert(cursor.clone()), "Access pagination did not advance");
                            let next = backend::request(session, &credential, Method::GET,
                                &format!("{}?limit=100&cursor={}", tab.root(), encode(&cursor)), None, None).await?;
                            let extra = next["items"].as_array().context("Missing access records")?;
                            let items = value["items"].as_array_mut().context("Missing access records")?;
                            ensure!(items.len() + extra.len() <= 20_000, "Too many access records");
                            items.extend(extra.iter().cloned()); value["next_cursor"] = next["next_cursor"].clone();
                        }
                        if *tab == crate::access::Tab::Profiles {
                            let mut accounts = backend::request(session, &credential, Method::GET, "/api/v1/accounts?limit=100", None, None).await?;
                            let mut cursors = BTreeSet::new();
                            while let Some(cursor) = accounts["next_cursor"].as_str().map(str::to_owned) {
                                ensure!(cursors.insert(cursor.clone()), "Account pagination did not advance");
                                let next = backend::request(session, &credential, Method::GET, &format!("/api/v1/accounts?limit=100&cursor={}", encode(&cursor)), None, None).await?;
                                let extra = next["items"].as_array().context("Missing accounts")?;
                                let items = accounts["items"].as_array_mut().context("Missing accounts")?;
                                ensure!(items.len() + extra.len() <= 20_000, "Too many accounts");
                                items.extend(extra.iter().cloned()); accounts["next_cursor"] = next["next_cursor"].clone();
                            }
                            let items = value["items"].as_array_mut().context("Missing profiles")?;
                            for account in accounts["items"].as_array().into_iter().flatten() {
                                let owned: Vec<_> = items.iter_mut().filter(|item| item["owner_account_id"] == account["id"]).collect();
                                if owned.is_empty() {
                                    let mut item = account.clone();
                                    item["_account_only"] = json!(true);
                                    item["display_name"] = json!(crate::access::label(crate::access::Tab::Accounts, account));
                                    items.push(item);
                                } else {
                                    for item in owned { item["_account"] = account.clone(); }
                                }
                            }
                        }
                    }
                    let _ = tx.send(Event::Nodes(Update::Access(command, value)));
                }
                Command::Temporary(command) => {
                    use crate::temporary::Command as Temporary;
                    let root = "/api/v1/system/temporary-configs";
                    let (method,path,body,key) = match &command {
                        Temporary::List { node,page,page_size } => (Method::GET,
                            format!("{root}?page={page}&page_size={page_size}{}",node.as_ref().map(|n|format!("&node_key={}",encode(n))).unwrap_or_default()),None,None),
                        Temporary::Servers => (Method::GET,"/api/v1/nodes?limit=100&order=region&include_summary=true".into(),None,None),
                        Temporary::Card(id) => (Method::GET,format!("{root}/{id}"),None,None),
                        Temporary::Artifact(id) => (Method::GET,format!("{root}/{id}/artifact"),None,None),
                        Temporary::Create { id,body } => (Method::POST,root.into(),Some(body.clone()),Some(*id)),
                        Temporary::Revoke(id) => (Method::POST,format!("{root}/{id}/revoke"),None,None),
                    };
                    let mut value = backend::request(session,&credential,method,&path,body,key).await?;
                    if matches!(command,Temporary::Servers) {
                        let mut cursors = BTreeSet::new();
                        while let Some(cursor) = value["next_cursor"].as_str().map(str::to_owned) {
                            ensure!(cursors.insert(cursor.clone()),"Node pagination did not advance");
                            let next = backend::request(session,&credential,Method::GET,
                                &format!("/api/v1/nodes?limit=100&order=region&include_summary=true&cursor={}",encode(&cursor)),None,None).await?;
                            let extra = next["items"].as_array().context("Missing nodes")?;
                            let items = value["items"].as_array_mut().context("Missing nodes")?;
                            ensure!(items.len()+extra.len()<20_000,"Too many nodes");
                            items.extend(extra.iter().cloned());
                            value["next_cursor"] = next["next_cursor"].clone();
                        }
                    }
                    let _ = tx.send(Event::Nodes(Update::Temporary(command,value)));
                }
                Command::CreationOptions => {
                    let _ = tx.send(Event::Stage("Loading node templates and installation defaults".into()));
                    let value = backend::request(session, &credential, Method::GET, "/api/v1/nodes/creation-options", None, None).await?;
                    let options = serde_json::from_value(value).context("Update the controller to use the shared node creation wizard")?;
                    let _ = tx.send(Event::Nodes(Update::CreationOptions(options)));
                    if let Some(mut saved) = SavedCreation::pending(&request, &credential.account_id)? {
                        let found = backend::helper(session, json!({"version":1,"action":"lookup-node-create",
                            "session_id":id,"command_id":saved.draft.command_id})).await?;
                        if !found["node"].is_null() {
                            let created = node(found["node"].clone())?;
                            saved.status = "succeeded".into(); saved.save(&request)?;
                            let _ = tx.send(Event::Nodes(Update::Created(created)));
                            return Ok("Original node creation confirmed; no command was replayed".into());
                        }
                        anyhow::bail!("Node creation remains unconfirmed. Command: {}. No request was replayed; check controller diagnostics before creating another node", saved.draft.command_id);
                    }

                }
                Command::Create(mut draft) => {
                    if draft.body["transport"] == "local" && draft.body["settings"]["public_host"] == "." {
                        draft.body["settings"]["public_host"] = json!(request.host);
                    }
                    ensure!(SavedCreation::pending(&request, &credential.account_id)?.is_none(), "Resolve the previous node creation before submitting another");
                    let mut saved = SavedCreation { host:request.host.clone(), port:request.port, user:request.user.clone(),
                        account_id:credential.account_id.clone(), draft, status:"unconfirmed".into() };
                    saved.save(&request)?;
                    let _ = tx.send(Event::Stage("Creating the server registry entry".into()));
                    let value = match backend::request(session, &credential, Method::POST, "/api/v1/nodes", Some(saved.draft.body.clone()), Some(saved.draft.command_id)).await {
                        Ok(value) => value,
                        Err(error) if error.status.is_some_and(|status| (400..500).contains(&status) && status != 408) => {
                            saved.status = "rejected".into(); saved.save(&request)?;
                            return Err(error.into());
                        }
                        Err(error) => return Err(anyhow::Error::new(error).context(format!("Creation was not confirmed. Reopen New node to check the original command without replaying it. Command: {}", saved.draft.command_id))),
                    };
                    let created = node(value)?;
                    // Publish success before saving; a local write failure must not
                    // invite another create POST. Recovery uses the original command.
                    let _ = tx.send(Event::Nodes(Update::Created(created)));
                    saved.status = "succeeded".into(); saved.save(&request)?;
                }
                Command::List => {
                    let _ = tx.send(Event::Stage("Loading the server registry".into()));
                    let mut items = Vec::new();
                    let mut cursor: Option<String> = None;
                    let mut cursors = BTreeSet::new();
                    loop {
                        let mut path = "/api/v1/nodes?limit=100&order=region&include_summary=true".to_string();
                        if let Some(cursor) = &cursor { path.push_str(&format!("&cursor={}", encode(cursor))); }
                        let value = backend::request(session, &credential, Method::GET, &path, None, None).await?;
                        for item in value["items"].as_array().context("Missing node list")? {
                            ensure!(items.len() < 20_000, "Node registry is too large");
                            items.push(node(item.clone())?);
                        }
                        cursor = value["next_cursor"].as_str().map(str::to_owned);
                        match &cursor {
                            None => break,
                            Some(next) => ensure!(cursors.insert(next.clone()), "Node pagination did not advance"),
                        }
                    }
                    let _ = tx.send(Event::Nodes(Update::List(items)));
                }
                Command::Observe(previous, current_operation) => {
                    let path = format!("/api/v1/nodes/{}", previous.key);
                    let mut item = node(backend::request(session, &credential, Method::GET, &path, None, None).await?)?;
                    item.overview = Some(backend::request(session, &credential, Method::GET,
                        &format!("{path}/overview"), None, None).await?);
                    // Background observation reads registry state only; do not probe the host.
                    item.services = previous.services;
                    item.agent = previous.agent;
                    let _ = tx.send(Event::Nodes(Update::Observation(item)));
                    if let Some(previous) = current_operation {
                        let value = backend::request(session, &credential, Method::GET,
                            &format!("/api/v1/{}/{}", previous.kind, previous.id), None, None).await?;
                        let mut observed = operation(&value, &previous.kind, &previous.node_key)?;
                        if observed.action.is_empty() { observed.action = previous.action; }
                        observed.command_id = previous.command_id;
                        let _ = tx.send(Event::Nodes(Update::Operation(observed)));
                    }
                }
                Command::EditContext(key) => {
                    let source = backend::request(session, &credential, Method::GET, &format!("/api/v1/nodes/{key}"), None, None).await?;
                    ensure!(source["key"] == key, "Backend returned another server");
                    let mut defaults = backend::request(session, &credential, Method::GET, "/api/v1/system/installation-defaults", None, None).await?;
                    if let Ok(facts) = backend::request(session, &credential, Method::GET, &format!("/api/v1/nodes/{key}/services"), None, None).await
                        && facts["awg_parameters"].as_object().is_some_and(|v| !v.is_empty()) {
                        defaults["settings"]["awg_parameters"] = facts["awg_parameters"].clone();
                    }
                    let _ = tx.send(Event::Nodes(Update::EditContext(source, defaults)));
                }
                Command::EditReview(draft) => {
                    let preview = if let Some(region) = draft.body["region"].as_str() {
                        backend::request(session, &credential, Method::GET, &format!("/api/v1/nodes/{}/region-access-preview?region={}", draft.node, encode(region)), None, None).await?
                    } else { json!({"affected_profiles":0}) };
                    let _ = tx.send(Event::Nodes(Update::EditReview(draft, preview)));
                }
                Command::EditSave(draft) => {
                    let saved = backend::request_revision(session, &credential, Method::PATCH, &format!("/api/v1/nodes/{}", draft.node), Some(draft.body), Some(draft.key), Some(draft.revision)).await?;
                    let mut item = node(saved)?;
                    ensure!(item.key == draft.node, "Backend returned another server");
                    item.overview = backend::request(session, &credential, Method::GET, &format!("/api/v1/nodes/{}/overview", item.key), None, None).await.ok();
                    let apply = item.applied_revision > 0 && item.applied_revision < item.desired_revision && !item.protocols.is_empty();
                    let _ = tx.send(Event::Nodes(Update::Edited(item.clone())));
                    if apply { apply_settings(session, &credential, &request, &item, draft.apply_key, tx).await?; }
                }
                Command::ApplySettings(item, key) => { apply_settings(session, &credential, &request, &item, key, tx).await?; }
                Command::Diagnostics(key, offset) => {
                    let (facts, inventory) = diagnostic_snapshot(session, &credential, &key, offset).await?;
                    let _ = tx.send(Event::Nodes(Update::Diagnostics(key, facts, inventory)));
                }
                Command::Maintenance(key, confirmation) => {
                    let snapshot = maintenance_snapshot(session, &credential, &key).await?;
                    let view = crate::node_maintenance::Browser::new(key, snapshot, confirmation)?;
                    let _ = tx.send(Event::Nodes(Update::Maintenance(view, false)));
                }
                Command::MaintenancePoll(key) => {
                    let snapshot = maintenance_snapshot(session, &credential, &key).await?;
                    let view = crate::node_maintenance::Browser::new(key, snapshot, None)?;
                    let _ = tx.send(Event::Nodes(Update::Maintenance(view, true)));
                }
                Command::Remove(key, mode, retry) => {
                    use crate::node_maintenance::Mode;
                    let (path, body) = match mode {
                        Mode::Full => (format!("/api/v1/nodes/{key}/remove-step"), json!({"retry":retry})),
                        Mode::RegistryOnly => (format!("/api/v1/nodes/{key}/retire-registry-only"), json!({
                            "accept_unverified_runtime":true,
                            "reason":"Administrator explicitly removed the server record through Workstation; remote runtime may remain."})),
                    };
                    let value = match backend::request(session, &credential, Method::POST, &path, Some(body), None).await {
                        Ok(value) => value,
                        Err(error) if error.status.is_none_or(|status| status == 408 || status >= 500) => {
                            // Observe the original node-keyed removal; never replay the mutation
                            // automatically after losing its response.
                            if let Ok(snapshot) = maintenance_snapshot(session, &credential, &key).await {
                                let view = crate::node_maintenance::Browser::new(key, snapshot, None)?;
                                let _ = tx.send(Event::Nodes(Update::Maintenance(view, false)));
                            }
                            return Err(anyhow::Error::new(error).context("Removal response was unavailable. Use Refresh to check the result before retrying"));
                        }
                        Err(error) => return Err(error.into()),
                    };
                    ensure!(value["node_key"] == key, "Removal belongs to another server");
                    let snapshot = if mode == Mode::RegistryOnly {
                        ensure!(value["mode"] == "registry_only", "Unexpected retirement result");
                        json!({"node_key":key,"status":"removed_registry_only"})
                    } else { value };
                    let view = crate::node_maintenance::Browser::new(key, snapshot, None)?;
                    let _ = tx.send(Event::Nodes(Update::Maintenance(view, false)));
                }
                Command::Recover(key, item, action) => {
                    ensure!(item["node_key"] == key, "Recovery belongs to another server");
                    ensure!(["recheck", "resolve"].contains(&action.as_str()), "Unsupported recovery action");
                    let kind = item["kind"].as_str().context("Missing recovery kind")?;
                    ensure!(["node", "settings", "agent", "bootstrap", "profile"].contains(&kind), "Unsupported recovery kind");
                    let identity = Uuid::parse_str(item["id"].as_str().context("Missing recovery identity")?)?;
                    // The backend validates currently allowed actions under the worker lock.
                    ensure!(item["actions"].as_array().is_some_and(|actions| actions.iter().any(|v| v == &action)), "Recovery action unavailable");
                    backend::request(session, &credential, Method::POST,
                        &format!("/api/v1/system/recovery/{kind}/{identity}/{action}"), None, None).await?;
                    let (facts, inventory) = diagnostic_snapshot(session, &credential, &key, 0).await?;
                    let _ = tx.send(Event::Nodes(Update::Diagnostics(key, facts, inventory)));
                }
                Command::Card(key) => {
                    let path = format!("/api/v1/nodes/{key}");
                    let mut item = node(backend::request(session, &credential, Method::GET, &path, None, None).await?)?;
                    let overview_path = format!("{path}/overview");
                    let services_path = format!("{path}/services");
                    let (overview, services) = tokio::join!(
                        backend::request(session, &credential, Method::GET, &overview_path, None, None),
                        backend::request(session, &credential, Method::GET, &services_path, None, None));
                    item.overview = Some(overview?);
                    match services {
                        Ok(facts) => { item.services = Some(facts); item.agent = AgentState::Ready; }
                        Err(error) if error.code == "node_agent_unconfigured" => item.agent = AgentState::Missing,
                        Err(error) if error.code == "node_agent_unavailable" => item.agent = AgentState::Unavailable,
                        Err(error) => return Err(error.into()),
                    }
                    let mut saved = SavedOperation::latest(&request, &credential.account_id, &key)?;
                    if saved.is_none() && let Some(parent) = item.overview.as_ref().and_then(|v| v.get("bootstrap")).filter(|v| v.is_object()) {
                        let mut parent = parent.clone();
                        parent["node_key"] = json!(key);
                        saved = Some(operation(&parent, "node-bootstraps", &key)?);
                    }
                    if saved.is_none() && let Some(job) = item.overview.as_ref().and_then(|v| v.get("last_job")).filter(|v| v.is_object()) {
                        let mut job = job.clone();
                        job["node_key"] = json!(key);
                        saved = Some(operation(&job, "node-jobs", &key)?);
                    }
                    let _ = tx.send(Event::Nodes(Update::Card(item)));
                    if let Some(mut previous) = saved {
                        if previous.status == "rejected" {
                            let _ = tx.send(Event::Nodes(Update::Operation(previous)));
                            return Ok("Node registry ready".into());
                        }
                        if previous.status == "unconfirmed" {
                            let command_id = previous.command_id.context("Missing saved command identity")?;
                            let result = backend::helper(session, json!({"version":1,"action":"lookup-node",
                                "session_id":id,"command_id":command_id})).await?;
                            if result["job"].is_null() {
                                previous.error = "No confirmed backend operation; inspect recovery before retrying".into();
                                let _ = tx.send(Event::Nodes(Update::Operation(previous)));
                                return Ok("The command was not replayed".into());
                            }
                            ensure!(result["job"]["kind"] == previous.kind, "Node command kind changed");
                            let mut found = operation(&result["job"], &previous.kind, &previous.node_key)?;
                            if found.action.is_empty() { found.action = previous.action; }
                            found.command_id = previous.command_id;
                            previous = found;
                        }
                        let value = backend::request(session, &credential, Method::GET,
                            &format!("/api/v1/{}/{}", previous.kind, previous.id), None, None).await?;
                        let mut observed = operation(&value, &previous.kind, &previous.node_key)?;
                        if observed.action.is_empty() { observed.action = previous.action.clone(); }
                        observed.command_id = previous.command_id;
                        ensure!(observed.id == previous.id, "Backend returned another operation");
                        SavedOperation::save(&request, &credential.account_id, &observed)?;
                        let _ = tx.send(Event::Nodes(Update::Operation(observed)));
                    }
                }
                Command::Setup(item) | Command::Bootstrap(item) | Command::ProtocolAction(item, _) => {
                    let title = match protocol_action { Some("reinstall_keep") => "Reinstall protocols and keep configurations?", Some("reinstall_clean") => "Reinstall protocols with new configurations?", Some("check_ports") => "Check protocol ports?", _ if setup => "Install node agent?", _ => "Set up server?" };
                    ensure!(events::confirm(tx, title, format!("{} · {}\n\n{}", item.region, item.title, if protocol_action == Some("reinstall_clean") { "Existing configurations will be replaced. Users will need new configurations." } else { "The operation will run on this server." }))?, "Node action cancelled");
                    if item.transport.as_deref() == Some("ssh")
                        && item.agent != AgentState::Ready {
                        let target = item.ssh_target.as_deref().context("Node SSH address is missing")?;
                        let (user, host) = target.split_once('@').unwrap_or(("root", target));
                        request.workflow.target_user = user.into();
                        request.workflow.target_host = host.trim_matches(['[', ']']).into();
                        request.workflow.target_port = 22;
                        request.workflow.yes = true;
                        crate::workstation::prepare_node(&request, session,
                            UiInteraction::shared(tx.clone()), tx).await?;
                    }
                    let command_id = Uuid::new_v4();
                    let (path, body, kind) = if let Some(action) = protocol_action {
                        ensure!(matches!(action, "reinstall_keep" | "reinstall_clean" | "check_ports"), "Unsupported protocol action");
                        (format!("/api/v1/nodes/{}/actions", item.key), json!({"action":action,"revision":item.desired_revision}), "node-jobs")
                    } else if setup {
                        let transport = item.transport.as_deref().context("Node transport is not configured")?;
                        (format!("/api/v1/nodes/{}/agent-rollouts", item.key), json!({"transport":transport,
                            "ssh_target":item.ssh_target,"ssh_port":22,"install_rust":false}), "agent-rollouts")
                    } else {
                        ensure!(item.needs_setup(), "Refresh the node card before server setup");
                        (format!("/api/v1/nodes/{}/bootstrap", item.key),
                         json!({"action":"bootstrap", "revision":item.desired_revision}), "node-bootstraps")
                    };
                    let pending = Operation { kind: kind.into(), id: command_id, node_key: item.key.clone(), action: clean(&body["action"]),
                        status: "unconfirmed".into(), error: String::new(), command_id: Some(command_id) };
                    SavedOperation::save(&request, &credential.account_id, &pending)?;
                    let _ = tx.send(Event::Nodes(Update::Operation(pending.clone())));
                    let value = match backend::request(session, &credential, Method::POST, &path, Some(body), Some(command_id)).await {
                        Ok(value) => value,
                        Err(error) if error.status.is_some_and(|status| (400..500).contains(&status) && status != 408) => {
                            let rejected = Operation { status:"rejected".into(), error:error.code.clone(), ..pending };
                            SavedOperation::save(&request, &credential.account_id, &rejected)?;
                            let _ = tx.send(Event::Nodes(Update::Operation(rejected)));
                            return Err(error.into());
                        }
                        Err(error) => return Err(anyhow::Error::new(error).context(format!(
                            "Node action was not confirmed. Use Refresh to check its original command; do not resubmit. Command: {command_id}"))),
                    };
                    let mut operation = operation(&value, kind, &item.key)?;
                    if operation.action.is_empty() { operation.action = pending.action; }
                    operation.command_id = Some(command_id);
                    let _ = tx.send(Event::Nodes(Update::Operation(operation.clone())));
                    SavedOperation::save(&request, &credential.account_id, &operation)
                        .context("The operation was accepted, but its local record could not be saved. Do not resubmit")?;
                }
            }
            Ok("Node registry ready".into())
        }.await;
        if result.as_ref().err().is_some_and(|error| error.downcast_ref::<backend::ApiError>().is_some_and(backend::ApiError::transient)) {
            observe_controller_removal(session, &request, tx).await;
        }
        let _ = backend::revoke(session, id).await;
        result
    }.await
}
fn encode(text: &str) -> String {
    text.bytes()
        .map(|byte| {
            if byte.is_ascii_alphanumeric() || b"-_.~".contains(&byte) {
                (byte as char).to_string()
            } else {
                format!("%{byte:02X}")
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture(key: &str, region: &str) -> Node {
        node(
            json!({"key":key,"title":key,"region":region,"flag":"", "enabled":true,
            "protocols":["awg"],"xray_transports":[],"desired_revision":1,"applied_revision":0}),
        )
        .unwrap()
    }
    #[test]
    fn port_check_result_is_visible_and_scoped_to_node() {
        let value = json!({"id":Uuid::new_v4(),"node_key":"lv1","action":"check_ports","status":"succeeded","result":{"ports":[{"port":443,"protocol":"udp","status":"open"}],"firewall":"unmanaged"}});
        let observed = operation(&value, "node-jobs", "lv1").unwrap();
        assert!(observed.label().contains("443 / udp · open"));
        assert!(observed.label().contains("Firewall is not managed"));
        assert!(operation(&value, "node-jobs", "lv2").is_err());
    }

    #[test]
    fn bootstrap_observation_displays_installer_detail_without_changing_status() {
        let value = json!({"id": Uuid::new_v4(), "node_key": "lv1", "phase": "agent",
            "status": "running", "progress": {"stage": "check_ssh", "label": "Checking SSH access"}});
        let observed = operation(&value, "node-bootstraps", "lv1").unwrap();
        assert_eq!(
            observed.action,
            "Server setup · agent · Checking SSH access"
        );
        assert_eq!(observed.status, "running");
        assert_eq!(
            observed.label(),
            "Server setup · agent · Checking SSH access"
        );
        let agent = operation(
            &json!({"id": Uuid::new_v4(), "node_key": "lv1",
            "status": "succeeded", "progress": null}),
            "agent-rollouts",
            "lv1",
        )
        .unwrap();
        assert_eq!(agent.action, "Agent setup");
        assert_eq!(agent.label(), "Agent setup");
        let mut completed = value;
        completed["status"] = json!("succeeded");
        completed["phase"] = json!("done");
        completed["progress"] = Value::Null;
        assert_eq!(
            operation(&completed, "node-bootstraps", "lv1")
                .unwrap()
                .action,
            "Server setup · done"
        );
    }

    #[test]
    fn observations_stop_after_completion_and_ignore_other_nodes() {
        let mut item = fixture("lv1", "Europe");
        let mut operation = Operation {
            kind: "node-jobs".into(),
            id: Uuid::new_v4(),
            node_key: "lv1".into(),
            status: "running".into(),
            error: String::new(),
            command_id: None,
            action: "bootstrap".into(),
        };
        assert!(item.needs_observation(Some(&operation)));
        operation.status = "succeeded".into();
        assert!(!item.needs_observation(Some(&operation)));
        operation.status = "running".into();
        operation.node_key = "lv2".into();
        assert!(!item.needs_observation(Some(&operation)));
        item.overview = Some(json!({"state":"applying"}));
        assert!(item.needs_observation(None));
        item.overview = Some(json!({"state":"needs_attention"}));
        assert!(!item.needs_observation(None));
    }

    #[test]
    fn monthly_traffic_formats_missing_and_partial_measurements() {
        assert!(traffic_lines(&json!({})).is_empty());
        let lines = traffic_lines(&json!({"traffic":{"month":"2026-10","status":"current",
            "total_bytes":2048,"items":[{"protocol":"xray","status":"current",
            "uplink_bytes":1024,"downlink_bytes":0},{"protocol":"awg","status":"current",
            "uplink_bytes":512,"downlink_bytes":512}]}}));
        assert!(
            lines
                .join("\n")
                .contains("All: 2.0 KiB · VLESS: 1.0 KiB · AWG: 1.0 KiB")
        );
        let lines = traffic_lines(&json!({"traffic":{"month":"2026-10","status":"unknown",
            "total_bytes":null,"items":[{"protocol":"xray","status":"unknown",
            "uplink_bytes":1024,"downlink_bytes":0}]}}));
        assert!(
            lines
                .join("\n")
                .contains("All: — · VLESS: ≈ 1.0 KiB · AWG: —")
        );
    }

    #[test]
    fn protocol_status_distinguishes_missing_stopped_and_running_with_legacy_fallback() {
        let mut node = fixture("lv1", "Europe");
        for protocol in ["awg", "xray"] {
            for (installed, running, expected) in [
                (false, false, "missing"),
                (true, false, "stopped"),
                (true, true, "running"),
            ] {
                node.services = Some(
                    json!({"docker":true, format!("{protocol}_installed"):installed, format!("{protocol}_running"):running}),
                );
                assert_eq!(node.protocol_status(protocol), expected);
            }
            node.services = Some(json!({"docker":true, format!("{protocol}_running"):false}));
            assert_eq!(node.protocol_status(protocol), "missing");
        }
        node.applied_revision = 1;
        node.services = Some(json!({"docker":true,"awg_running":false,"awg_config_valid":true}));
        assert_eq!(node.protocol_status("awg"), "stopped");
        node.services = Some(json!({"docker":false}));
        assert_eq!(node.protocol_status("awg"), "unavailable");
    }
    #[test]
    fn pagination_repeats_region_headers_and_never_orphans_them() {
        let browser = Browser {
            items: (0..15)
                .map(|i| fixture(&format!("n{i:02}"), "Europe"))
                .collect(),
            ..Browser::default()
        };
        for capacity in 2..12 {
            let pages = browser.pages(capacity);
            assert!(
                pages
                    .iter()
                    .all(|page| page.len() <= capacity && matches!(page[0], Row::Region(_, 15)))
            );
            assert_eq!(
                pages
                    .iter()
                    .flatten()
                    .filter(|r| matches!(r, Row::Node(_)))
                    .count(),
                15
            );
        }
    }
    #[test]
    fn search_matches_region_name_and_key_and_collapsing_keeps_the_region() {
        let mut browser = Browser {
            items: vec![fixture("lv1", "Europe"), fixture("jp1", "Asia")],
            ..Browser::default()
        };
        browser.search = "EUROPE".into();
        assert_eq!(
            browser.pages(10)[0],
            vec![Row::Region("Europe".into(), 1), Row::Node(0)]
        );
        browser.collapsed.insert("Europe".into());
        assert_eq!(browser.pages(10)[0], vec![Row::Region("Europe".into(), 1)]);
        browser.search = "jp1".into();
        assert_eq!(
            browser.pages(10)[0],
            vec![Row::Region("Asia".into(), 1), Row::Node(1)]
        );
        browser.search = "absent".into();
        assert_eq!(browser.pages(10), vec![Vec::new()]);
    }
    #[test]
    fn cursor_query_encoding_does_not_inject_parameters() {
        assert_eq!(encode("a+/=&b"), "a%2B%2F%3D%26b");
    }
    #[test]
    fn saved_progress_is_scoped_to_controller_account_and_node() {
        let dir = tempfile::tempdir().unwrap();
        let request = Request {
            action: crate::config::Action::Diagnose,
            host: "controller.example".into(),
            port: 22,
            user: "root".into(),
            state_dir: dir.path().into(),
            tag: String::new(),
            branch: String::new(),
            admin_ids: String::new(),
            bot_token: zeroize::Zeroizing::new(String::new()),
            workflow: crate::config::WorkflowOptions::default(),
        };
        ssh::secure_state_directory(dir.path()).unwrap();
        let account = Uuid::new_v4().to_string();
        let operation = Operation {
            kind: "agent-rollouts".into(),
            action: String::new(),
            id: Uuid::new_v4(),
            node_key: "lv1".into(),
            status: "queued".into(),
            error: String::new(),
            command_id: None,
        };
        SavedOperation::save(&request, &account, &operation).unwrap();
        assert_eq!(
            SavedOperation::latest(&request, &account, "lv1")
                .unwrap()
                .unwrap()
                .id,
            operation.id
        );
        assert!(
            SavedOperation::latest(&request, "another-account", "lv1")
                .unwrap()
                .is_none()
        );
        assert!(
            SavedOperation::latest(&request, &account, "other-node")
                .unwrap()
                .is_none()
        );
    }
    #[test]
    fn failed_bootstrap_extracts_nested_backend_error_and_rejects_other_nodes() {
        let value = json!({"id":Uuid::new_v4(), "node_key":"lv1", "status":"blocked",
            "result":{"error_code":"node_agent_unavailable"}});
        assert_eq!(
            operation(&value, "node-jobs", "lv1").unwrap().error,
            "node_agent_unavailable"
        );
        assert!(operation(&value, "node-jobs", "other").is_err());
    }
}

#[cfg(test)]
mod channel_policy_tests {
    use super::*;
    #[test]
    fn installed_version_is_authoritative_during_a_channel_transition() {
        assert_eq!(
            controller_branch(&json!({"current_version":"1.0.0","branch":"dev"})).unwrap(),
            "main"
        );
        assert_eq!(
            controller_branch(&json!({"current_version":"v1.0.0-alpha.3","branch":"main"}))
                .unwrap(),
            "dev"
        );
        assert_eq!(controller_branch(&json!({"branch":"dev"})).unwrap(), "dev");
        assert!(controller_branch(&json!({})).is_err());
    }
    #[test]
    fn mismatched_controller_only_allows_transition_to_this_workstations_channel() {
        for (installed, expected) in [("main", "dev"), ("dev", "main")] {
            assert!(validate_controller_channel(&Command::List, installed, expected).is_err());
            assert!(validate_controller_channel(&Command::List, expected, expected).is_ok());
            assert!(
                validate_controller_channel(
                    &Command::Controller(None, crate::controller::Command::Updates),
                    installed,
                    expected
                )
                .is_ok()
            );
            assert!(
                validate_controller_channel(
                    &Command::Controller(
                        None,
                        crate::controller::Command::UpdatePreferences(json!({"branch":expected}))
                    ),
                    installed,
                    expected
                )
                .is_ok()
            );
            assert!(
                validate_controller_channel(
                    &Command::Controller(
                        None,
                        crate::controller::Command::UpdatePreferences(json!({"branch":installed}))
                    ),
                    installed,
                    expected
                )
                .is_err()
            );
            assert!(
                validate_controller_channel(
                    &Command::Controller(
                        None,
                        crate::controller::Command::UpdatePreferences(
                            json!({"branch":expected,"auto_check_enabled":true})
                        )
                    ),
                    installed,
                    expected
                )
                .is_err()
            );
        }
    }
}
