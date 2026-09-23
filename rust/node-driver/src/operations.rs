use std::collections::HashMap;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use chrono::Utc;
use prost::Message;
use tonic::Status;
use uuid::Uuid;

use crate::driver::v1::{DriverError, Operation, StartOperationResponse};

const FILE_HEADER: &[u8] = b"NODE-PLANE-OPERATIONS-v1\n";

#[derive(Clone, Default)]
pub(crate) struct DriverState {
    operations: Arc<Mutex<HashMap<String, Operation>>>,
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
                let mut rest = bytes.strip_prefix(FILE_HEADER).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "invalid operations file header")
                })?;
                let mut items = HashMap::new();
                while !rest.is_empty() {
                    let operation = Operation::decode_length_delimited(&mut rest)
                        .map_err(|err| io::Error::new(io::ErrorKind::InvalidData, err))?;
                    if operation.operation_id.is_empty()
                        || items
                            .insert(operation.operation_id.clone(), operation)
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
        for operation in operations.values_mut() {
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

    pub(crate) fn begin_operation(
        &self,
        kind: &str,
        node_key: &str,
        profile_name: &str,
    ) -> Result<RunningOperation, Status> {
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
        self.put_operation(operation.clone())?;
        Ok(RunningOperation {
            state: self.clone(),
            operation: Some(operation),
        })
    }

    fn persist(path: &Path, operations: &HashMap<String, Operation>) -> io::Result<()> {
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
            sorted.sort_by(|a, b| a.operation_id.cmp(&b.operation_id));
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
        updated.insert(operation_id.clone(), operation);
        if let Some(path) = &self.storage_path {
            Self::persist(path, &updated)
                .map_err(|err| Status::internal(format!("failed to persist operation: {err}")))?;
        }
        *current = updated;
        Ok(StartOperationResponse { operation_id })
    }

    pub(crate) fn missing_agent_operation(
        &self,
        kind: &str,
        node_key: &str,
        profile_name: &str,
    ) -> Result<StartOperationResponse, Status> {
        let timestamp = Utc::now().to_rfc3339();
        let summary = "no node-agent target configured";
        self.put_operation(Operation {
            operation_id: Uuid::new_v4().to_string(),
            kind: kind.to_string(),
            status: "FAILED".to_string(),
            node_key: node_key.to_string(),
            profile_name: profile_name.to_string(),
            started_at: timestamp.clone(),
            updated_at: timestamp.clone(),
            finished_at: timestamp,
            progress_message: summary.to_string(),
            error: Some(DriverError {
                code: "agent_not_configured".to_string(),
                summary: summary.to_string(),
                detail: "Configure the node in NODE_AGENT_TARGETS before retrying.".to_string(),
                retryable: false,
            }),
            result_json: String::new(),
        })
    }

    pub(crate) fn finish_operation(
        &self,
        kind: &str,
        node_key: &str,
        profile_name: &str,
        status: &str,
        message: &str,
    ) -> Result<StartOperationResponse, Status> {
        self.finish_operation_with_result(kind, node_key, profile_name, status, message, "")
    }

    pub(crate) fn finish_operation_with_result(
        &self,
        kind: &str,
        node_key: &str,
        profile_name: &str,
        status: &str,
        message: &str,
        result_json: &str,
    ) -> Result<StartOperationResponse, Status> {
        let timestamp = Utc::now().to_rfc3339();
        self.put_operation(Operation {
            operation_id: Uuid::new_v4().to_string(),
            kind: kind.to_string(),
            status: status.to_string(),
            node_key: node_key.to_string(),
            profile_name: profile_name.to_string(),
            started_at: timestamp.clone(),
            updated_at: timestamp.clone(),
            finished_at: timestamp,
            progress_message: message.to_string(),
            error: None,
            result_json: result_json.to_string(),
        })
    }

    pub(crate) fn get_operation(&self, operation_id: &str) -> Option<Operation> {
        self.operations
            .lock()
            .expect("operations lock poisoned")
            .get(operation_id)
            .cloned()
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
        mut self,
        status: &str,
        summary: &str,
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
