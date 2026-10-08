//! Registry browsing uses persisted state; host probes and mutations are explicit.
use crate::{
    backend,
    config::Request,
    events::{self, Event, UiInteraction},
    ssh::{self, SshOptions},
};
use anyhow::{Context, Result, ensure};
use hyper::Method;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::mpsc,
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
    pub fn can_bootstrap(&self) -> bool {
        self.state() == "not_installed"
            && self
                .overview
                .as_ref()
                .is_some_and(|v| v["settings_complete"] == true)
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
        state.join(format!("node-operation-{}.json", self.operation.id))
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
        let path = record.path(&request.state_dir);
        if path.exists() {
            ssh::secure_file(&path)?;
        }
        let mut temporary = tempfile::NamedTempFile::new_in(&request.state_dir)?;
        ssh::secure_file(temporary.path())?;
        temporary.write_all(&serde_json::to_vec(&record)?)?;
        temporary.as_file().sync_all()?;
        temporary.persist(path).map_err(|e| e.error)?;
        Ok(())
    }
    fn latest(request: &Request, account_id: &str, key: &str) -> Result<Option<Operation>> {
        let mut latest = None;
        for item in std::fs::read_dir(&request.state_dir)? {
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
                        "node-jobs" | "agent-rollouts"
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
#[derive(Clone)]
pub enum Command {
    List,
    Card(String),
    Setup(Node),
    Bootstrap(Node),
    Inspect(String),
    Docker(Node),
    Observe(Operation),
}
pub enum Update {
    List(Vec<Node>),
    Card(Node),
    Operation(Operation),
    Services(String, Value),
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
    })
}

pub fn spawn(
    request: Request,
    command: Command,
    tx: mpsc::Sender<Event>,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let result = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(anyhow::Error::from)
            .and_then(|runtime| runtime.block_on(execute(request, command, &tx)));
        let _ = tx.send(Event::Finished(result.map_err(|e| format!("{e:#}"))));
    })
}
async fn execute(request: Request, command: Command, tx: &mpsc::Sender<Event>) -> Result<String> {
    let _lock = crate::operation_store::OperationLock::acquire(&request.state_dir)?;
    let _ = tx.send(Event::Stage("Connecting to the controller".into()));
    let mut session = ssh::connect(
        &SshOptions {
            host: request.host.clone(),
            port: request.port,
            user: request.user.clone(),
            state_dir: request.state_dir.clone(),
        },
        UiInteraction::shared(tx.clone()),
    )
    .await?;
    let id = Uuid::new_v4();
    let result = async {
        let _ = tx.send(Event::Stage("Authorizing the controller administrator".into()));
        let credential = backend::authenticate(&mut session, id, &request.workflow.account, tx).await?;
        let result = async {
            let setup = matches!(&command, Command::Setup(_));
            let docker = matches!(&command, Command::Docker(_));
            match command {
                Command::List => {
                    let _ = tx.send(Event::Stage("Loading the server registry".into()));
                    let mut items = Vec::new();
                    let mut cursor: Option<String> = None;
                    let mut cursors = BTreeSet::new();
                    loop {
                        let mut path = "/api/v1/nodes?limit=100&order=region&include_summary=true".to_string();
                        if let Some(cursor) = &cursor { path.push_str(&format!("&cursor={}", encode(cursor))); }
                        let value = backend::request(&session, &credential, Method::GET, &path, None, None).await?;
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
                Command::Card(key) => {
                    let path = format!("/api/v1/nodes/{key}");
                    let mut item = node(backend::request(&session, &credential, Method::GET, &path, None, None).await?)?;
                    item.overview = Some(backend::request(&session, &credential, Method::GET,
                        &format!("{path}/overview"), None, None).await?);
                    let mut saved = SavedOperation::latest(&request, &credential.account_id, &key)?;
                    if saved.is_none() && let Some(job) = item.overview.as_ref().and_then(|v| v.get("last_job")).filter(|v| v.is_object()) {
                        let mut job = job.clone();
                        job["node_key"] = json!(key);
                        saved = Some(operation(&job, "node-jobs", &key)?);
                    }
                    if let Some(operation) = saved {
                        let _ = tx.send(Event::Nodes(Update::Operation(operation)));
                    }
                    let _ = tx.send(Event::Nodes(Update::Card(item)));
                }
                Command::Inspect(key) => {
                    let facts = backend::request(&session, &credential, Method::GET,
                        &format!("/api/v1/nodes/{key}/services"), None, None).await?;
                    let _ = tx.send(Event::Nodes(Update::Services(key, facts)));
                }
                Command::Setup(item) | Command::Bootstrap(item) | Command::Docker(item) => {
                    let title = if setup { "Install node agent?" } else if docker { "Install Docker?" } else { "Bootstrap VPN protocols?" };
                    ensure!(events::confirm(tx, title, format!("{} · {}\n\nThis submits one backend operation. An uncertain response is never automatically replayed.", item.region, item.title))?, "Node action cancelled");
                    let command_id = Uuid::new_v4();
                    let (path, body, kind) = if setup {
                        let transport = item.transport.as_deref().context("Node transport is not configured")?;
                        (format!("/api/v1/nodes/{}/agent-rollouts", item.key), json!({"transport":transport,
                            "ssh_target":item.ssh_target,"ssh_port":22,"install_rust":false}), "agent-rollouts")
                    } else {
                        ensure!(docker || item.can_bootstrap(), "Refresh the node card before bootstrap");
                        (format!("/api/v1/nodes/{}/actions", item.key), json!({"action":if docker { "install_docker" } else { "bootstrap" }, "revision":item.desired_revision}), "node-jobs")
                    };
                    let pending = Operation { kind: kind.into(), id: command_id, node_key: item.key.clone(),
                        status: "unconfirmed".into(), error: String::new(), command_id: Some(command_id) };
                    SavedOperation::save(&request, &credential.account_id, &pending)?;
                    let _ = tx.send(Event::Nodes(Update::Operation(pending)));
                    let value = backend::request(&session, &credential, Method::POST, &path, Some(body), Some(command_id)).await
                        .with_context(|| format!("Node action was not confirmed. Do not resubmit until backend diagnostics confirms its outcome. Command: {command_id}"))?;
                    let mut operation = operation(&value, kind, &item.key)?;
                    operation.command_id = Some(command_id);
                    let _ = tx.send(Event::Nodes(Update::Operation(operation.clone())));
                    SavedOperation::save(&request, &credential.account_id, &operation)
                        .context("The operation was accepted, but its local record could not be saved. Do not resubmit")?;
                }
                Command::Observe(previous) => {
                    let mut previous = previous;
                    if previous.status == "unconfirmed" {
                        let command_id = previous.command_id.context("Missing saved command identity")?;
                        let result = backend::helper(&mut session, json!({"version":1,"action":"lookup-node",
                            "session_id":id,"command_id":command_id})).await?;
                        if result["job"].is_null() {
                            previous.error = "No confirmed backend operation; inspect recovery before retrying".into();
                            let _ = tx.send(Event::Nodes(Update::Operation(previous)));
                            return Ok("The command was not replayed".into());
                        }
                        ensure!(result["job"]["kind"] == previous.kind, "Node command kind changed");
                        let mut found = operation(&result["job"], &previous.kind, &previous.node_key)?;
                        found.command_id = previous.command_id;
                        previous = found;
                    }
                    let value = backend::request(&session, &credential, Method::GET,
                        &format!("/api/v1/{}/{}", previous.kind, previous.id), None, None).await?;
                    let mut observed = operation(&value, &previous.kind, &previous.node_key)?;
                    observed.command_id = previous.command_id;
                    ensure!(observed.id == previous.id, "Backend returned another operation");
                    SavedOperation::save(&request, &credential.account_id, &observed)?;
                    let _ = tx.send(Event::Nodes(Update::Operation(observed)));
                }
            }
            Ok("Node registry ready".into())
        }.await;
        let _ = backend::revoke(&mut session, id).await;
        result
    }.await;
    let _ = session.close().await;
    result
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
