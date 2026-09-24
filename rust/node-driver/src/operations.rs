use std::collections::{HashMap, HashSet};
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use chrono::Utc;
use prost::Message;
use sha2::{Digest, Sha256};
use tonic::{Request, Status};
use uuid::Uuid;

use crate::driver::v1::{DriverError, Operation, StartOperationResponse};

const LEGACY_FILE_HEADER: &[u8] = b"NODE-PLANE-OPERATIONS-v1\n";
const FILE_HEADER: &[u8] = b"NODE-PLANE-OPERATIONS-v2\n";

#[derive(Clone, PartialEq, Message)]
struct StoredOperation {
    #[prost(message, required, tag = "1")]
    operation: Operation,
    #[prost(string, tag = "2")]
    command_id: String,
    #[prost(bytes = "vec", tag = "3")]
    request_hash: Vec<u8>,
}

pub(crate) struct CommandIdentity {
    command_id: String,
    request_hash: Vec<u8>,
}

impl CommandIdentity {
    pub(crate) fn from_request<T: Message>(request: &Request<T>) -> Result<Option<Self>, Status> {
        let mut values = request.metadata().get_all("x-node-plane-command-id").iter();
        let Some(value) = values.next() else {
            return Ok(None);
        };
        let command_id = value
            .to_str()
            .map_err(|_| Status::invalid_argument("invalid command id"))?;
        if values.next().is_some()
            || command_id.is_empty()
            || command_id.len() > 128
            || !command_id
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"-_.:".contains(&c))
        {
            return Err(Status::invalid_argument(
                "command id must be a single 1-128 character ASCII token",
            ));
        }
        Ok(Some(Self {
            command_id: command_id.to_string(),
            request_hash: Sha256::digest(request.get_ref().encode_to_vec()).to_vec(),
        }))
    }
}

pub(crate) enum CommandStart {
    New(RunningOperation),
    Existing(StartOperationResponse),
}

#[derive(Clone, Default)]
pub(crate) struct DriverState {
    operations: Arc<Mutex<HashMap<String, StoredOperation>>>,
    storage_path: Option<Arc<PathBuf>>,
    // Keep the advisory lock alive for every clone of this state.
    _storage_lock: Option<Arc<fs::File>>,
}

impl DriverState {
    pub(crate) fn open(path: PathBuf) -> io::Result<Self> {
        let path = if path.is_absolute() {
            path
        } else {
            std::env::current_dir()?.join(path)
        };
        let parent = path.parent().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "operations path has no parent")
        })?;
        fs::create_dir_all(parent)?;
        // Lock a separate inode: the snapshot itself is replaced on each write.
        let mut lock_path = path.as_os_str().to_os_string();
        lock_path.push(".lock");
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .open(PathBuf::from(lock_path))?;
        lock.try_lock().map_err(io::Error::other)?;
        let mut operations = match fs::read(&path) {
            Ok(bytes) => {
                let legacy = bytes.starts_with(LEGACY_FILE_HEADER);
                let mut rest = bytes
                    .strip_prefix(if legacy {
                        LEGACY_FILE_HEADER
                    } else {
                        FILE_HEADER
                    })
                    .ok_or_else(|| {
                        io::Error::new(io::ErrorKind::InvalidData, "invalid operations file header")
                    })?;
                let mut items = HashMap::new();
                let mut command_ids = HashSet::new();
                while !rest.is_empty() {
                    let record = if legacy {
                        Operation::decode_length_delimited(&mut rest).map(|operation| {
                            StoredOperation {
                                operation,
                                command_id: String::new(),
                                request_hash: Vec::new(),
                            }
                        })
                    } else {
                        StoredOperation::decode_length_delimited(&mut rest)
                    }
                    .map_err(|err| io::Error::new(io::ErrorKind::InvalidData, err))?;
                    if !record.command_id.is_empty()
                        && (record.request_hash.len() != 32
                            || !command_ids.insert(record.command_id.clone()))
                    {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "invalid or duplicate command identity",
                        ));
                    }
                    if record.operation.operation_id.is_empty()
                        || items
                            .insert(record.operation.operation_id.clone(), record)
                            .is_some()
                    {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "duplicate or empty operation id",
                        ));
                    }
                }
                items
            }
            Err(err) if err.kind() == io::ErrorKind::NotFound => HashMap::new(),
            Err(err) => return Err(err),
        };
        let mut recovered = false;
        for record in operations.values_mut() {
            let operation = &mut record.operation;
            if matches!(operation.status.as_str(), "PENDING" | "RUNNING") {
                mark_interrupted(
                    operation,
                    "driver restarted before recording a terminal result",
                );
                recovered = true;
            }
        }
        if recovered {
            Self::persist(&path, &operations)?;
        }
        Ok(Self {
            operations: Arc::new(Mutex::new(operations)),
            storage_path: Some(Arc::new(path)),
            _storage_lock: Some(Arc::new(lock)),
        })
    }

    #[cfg(test)]
    pub(crate) fn begin_operation(
        &self,
        kind: &str,
        node_key: &str,
        profile_name: &str,
    ) -> Result<RunningOperation, Status> {
        match self.begin_command(kind, node_key, profile_name, None)? {
            CommandStart::New(operation) => Ok(operation),
            CommandStart::Existing(_) => unreachable!("anonymous commands are never deduplicated"),
        }
    }

    pub(crate) fn begin_command(
        &self,
        kind: &str,
        node_key: &str,
        profile_name: &str,
        identity: Option<CommandIdentity>,
    ) -> Result<CommandStart, Status> {
        let mut current = self.operations.lock().expect("operations lock poisoned");
        if let Some(identity) = &identity {
            if let Some(record) = current
                .values()
                .find(|record| record.command_id == identity.command_id)
            {
                if record.operation.kind != kind
                    || record.operation.node_key != node_key
                    || record.operation.profile_name != profile_name
                    || record.request_hash != identity.request_hash
                {
                    return Err(Status::already_exists(
                        "command id was used with a different request",
                    ));
                }
                return Ok(CommandStart::Existing(StartOperationResponse {
                    operation_id: record.operation.operation_id.clone(),
                }));
            }
        }
        let timestamp = Utc::now().to_rfc3339();
        let operation = Operation {
            operation_id: Uuid::new_v4().to_string(),
            kind: kind.to_string(),
            status: "RUNNING".to_string(),
            node_key: node_key.to_string(),
            profile_name: profile_name.to_string(),
            started_at: timestamp.clone(),
            updated_at: timestamp,
            progress_message: "execution started".to_string(),
            ..Default::default()
        };
        let (command_id, request_hash) = identity
            .map(|identity| (identity.command_id, identity.request_hash))
            .unwrap_or_default();
        let mut updated = current.clone();
        updated.insert(
            operation.operation_id.clone(),
            StoredOperation {
                operation: operation.clone(),
                command_id,
                request_hash,
            },
        );
        self.save_updated(&mut current, updated)?;
        Ok(CommandStart::New(RunningOperation {
            state: self.clone(),
            operation: Some(operation),
        }))
    }

    fn persist(path: &Path, operations: &HashMap<String, StoredOperation>) -> io::Result<()> {
        let parent = path.parent().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "operations path has no parent")
        })?;
        fs::create_dir_all(parent)?;
        let temporary = path.with_extension(format!("{}.tmp", Uuid::new_v4()));
        let result = (|| {
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&temporary)?;
            file.write_all(FILE_HEADER)?;
            let mut sorted: Vec<_> = operations.values().collect();
            sorted.sort_by(|a, b| a.operation.operation_id.cmp(&b.operation.operation_id));
            for operation in sorted {
                let mut bytes = Vec::new();
                operation
                    .encode_length_delimited(&mut bytes)
                    .map_err(io::Error::other)?;
                file.write_all(&bytes)?;
            }
            file.sync_all()?;
            fs::rename(&temporary, path)?;
            fs::File::open(parent)?.sync_all()?;
            Ok(())
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        result
    }

    pub(crate) fn put_operation(
        &self,
        operation: Operation,
    ) -> Result<StartOperationResponse, Status> {
        let operation_id = operation.operation_id.clone();
        let mut current = self.operations.lock().expect("operations lock poisoned");
        let mut updated = current.clone();
        let record = updated.entry(operation_id.clone()).or_default();
        record.operation = operation;
        self.save_updated(&mut current, updated)?;
        Ok(StartOperationResponse { operation_id })
    }

    fn save_updated(
        &self,
        current: &mut HashMap<String, StoredOperation>,
        updated: HashMap<String, StoredOperation>,
    ) -> Result<(), Status> {
        if let Some(path) = &self.storage_path {
            Self::persist(path, &updated)
                .map_err(|err| Status::internal(format!("failed to persist operation: {err}")))?;
        }
        *current = updated;
        Ok(())
    }

    pub(crate) fn get_operation(&self, operation_id: &str) -> Option<Operation> {
        self.operations
            .lock()
            .expect("operations lock poisoned")
            .get(operation_id)
            .map(|record| record.operation.clone())
    }

    pub(crate) fn list_operations(
        &self,
        node_key: &str,
        profile_name: &str,
        status: &str,
        limit: u32,
    ) -> Vec<Operation> {
        let mut items: Vec<Operation> = self
            .operations
            .lock()
            .expect("operations lock poisoned")
            .values()
            .map(|record| &record.operation)
            .filter(|op| node_key.is_empty() || op.node_key == node_key)
            .filter(|op| profile_name.is_empty() || op.profile_name == profile_name)
            .filter(|op| status.is_empty() || op.status == status)
            .cloned()
            .collect();
        items.sort_by(|a, b| {
            b.updated_at
                .cmp(&a.updated_at)
                .then_with(|| a.operation_id.cmp(&b.operation_id))
        });
        items.truncate(limit.max(1) as usize);
        items
    }
}

// The guard lives across awaits. Dropping the RPC future does not imply that
// the remote action stopped; retain a terminal record with an unknown outcome.
pub(crate) struct RunningOperation {
    state: DriverState,
    operation: Option<Operation>,
}

impl RunningOperation {
    pub(crate) fn finish(
        self,
        status: &str,
        summary: &str,
    ) -> Result<StartOperationResponse, Status> {
        self.finish_with_result(status, summary, "")
    }

    pub(crate) fn finish_with_result(
        self,
        status: &str,
        summary: &str,
        result_json: &str,
    ) -> Result<StartOperationResponse, Status> {
        self.complete(status, summary, result_json, None)
    }

    pub(crate) fn missing_agent(self) -> Result<StartOperationResponse, Status> {
        let summary = "no node-agent target configured";
        self.complete(
            "FAILED",
            summary,
            "",
            Some(DriverError {
                code: "agent_not_configured".to_string(),
                summary: summary.to_string(),
                detail: "Configure the node target, then submit a new command identity."
                    .to_string(),
                retryable: false,
            }),
        )
    }

    fn complete(
        mut self,
        status: &str,
        summary: &str,
        result_json: &str,
        error: Option<DriverError>,
    ) -> Result<StartOperationResponse, Status> {
        let mut operation = self
            .operation
            .as_ref()
            .expect("running operation missing")
            .clone();
        operation.status = status.to_string();
        operation.updated_at = Utc::now().to_rfc3339();
        operation.finished_at = operation.updated_at.clone();
        operation.progress_message = summary.to_string();
        operation.result_json = result_json.to_string();
        operation.error = error;
        let response = self.state.put_operation(operation)?;
        self.operation = None;
        Ok(response)
    }
}

fn mark_interrupted(operation: &mut Operation, reason: &str) {
    operation.status = "FAILED".to_string();
    operation.updated_at = Utc::now().to_rfc3339();
    operation.finished_at = operation.updated_at.clone();
    operation.progress_message = "execution interrupted; node outcome is unknown".to_string();
    operation.error = Some(DriverError {
        code: "execution_interrupted".to_string(),
        summary: operation.progress_message.clone(),
        detail: format!("{reason}. Inspect the node state before issuing another command."),
        retryable: false,
    });
}

impl Drop for RunningOperation {
    fn drop(&mut self) {
        if let Some(mut operation) = self.operation.take() {
            mark_interrupted(
                &mut operation,
                "execution ended before recording a terminal result",
            );
            let id = operation.operation_id.clone();
            if let Err(err) = self.state.put_operation(operation) {
                // Keep credentials and response payloads out of logs. The persisted
                // RUNNING record will be recovered on the next startup.
                eprintln!(
                    "failed to record interruption for operation {id}: {}",
                    err.code()
                );
            }
        }
    }
}
