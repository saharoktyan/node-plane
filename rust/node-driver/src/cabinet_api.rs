//! CabinetService: the backend has the relay's node serve the web cabinet.
//! The build lies on this host, in the controller's release; the driver
//! reads it and the node's agent writes it beside the relay.
use std::fs;
use std::path::{Path, PathBuf};

use tonic::{Request, Response, Status};

use crate::DriverContext;
use crate::agent::v1::{ApplyCabinetRequest as AgentApplyCabinet, CabinetFile};
use crate::driver::v1::cabinet_service_server::CabinetService;
use crate::driver::v1::{ApplyCabinetRequest, CabinetNodeRequest, CabinetStatus};
use crate::relay_api::agent;

/// More than any build holds; the agent sets its own limits too.
const MAX_BYTES: u64 = 32 << 20;

pub(crate) struct CabinetApi {
    pub(crate) ctx: DriverContext,
}

/// The build's regular files by their paths inside it; links are refused,
/// so the build cannot reach outside its directory.
pub(crate) fn read(root: &Path) -> Result<Vec<CabinetFile>, String> {
    read_tree(root, "index.html", "cabinet", MAX_BYTES)
}

/// A directory of the controller's release, `marker` in it, as files for an
/// agent; errors are named after `prefix`.
pub(crate) fn read_tree(
    root: &Path,
    marker: &str,
    prefix: &str,
    max_bytes: u64,
) -> Result<Vec<CabinetFile>, String> {
    if !root.is_absolute() || !root.join(marker).is_file() {
        return Err(format!("{prefix}_build_missing"));
    }
    let mut files = Vec::new();
    let mut total = 0;
    let mut pending: Vec<PathBuf> = vec![root.to_path_buf()];
    while let Some(dir) = pending.pop() {
        let entries = fs::read_dir(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
        for entry in entries {
            let entry = entry.map_err(|e| format!("{}: {e}", dir.display()))?;
            let path = entry.path();
            let kind = entry
                .file_type()
                .map_err(|e| format!("{}: {e}", path.display()))?;
            if kind.is_dir() {
                pending.push(path);
            } else if kind.is_file() {
                let content = fs::read(&path).map_err(|e| format!("{}: {e}", path.display()))?;
                total += content.len() as u64;
                if total > max_bytes {
                    return Err(format!("{prefix}_files_too_large"));
                }
                let name = path
                    .strip_prefix(root)
                    .ok()
                    .and_then(|p| p.to_str())
                    .ok_or_else(|| format!("{prefix}_files_invalid"))?;
                files.push(CabinetFile {
                    path: name.to_string(),
                    content,
                });
            } else {
                return Err(format!("{prefix}_files_invalid"));
            }
        }
    }
    files.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(files)
}

fn refused(error: String) -> Status {
    if error.starts_with("cabinet_") && !error.contains(' ') {
        Status::invalid_argument(error)
    } else {
        Status::internal(error)
    }
}

#[tonic::async_trait]
impl CabinetService for CabinetApi {
    async fn apply_cabinet(
        &self,
        request: Request<ApplyCabinetRequest>,
    ) -> Result<Response<CabinetStatus>, Status> {
        let request = request.into_inner();
        let root = PathBuf::from(&request.root);
        let files = tokio::task::spawn_blocking(move || read(&root))
            .await
            .map_err(|_| Status::internal("cabinet reader unavailable"))?
            .map_err(refused)?;
        let state = agent(&self.ctx, &request.node_key)
            .await?
            .apply_cabinet(AgentApplyCabinet {
                version: request.version,
                files,
            })
            .await?;
        Ok(Response::new(CabinetStatus {
            version: state.version,
        }))
    }

    async fn remove_cabinet(
        &self,
        request: Request<CabinetNodeRequest>,
    ) -> Result<Response<CabinetStatus>, Status> {
        let node_key = request.into_inner().node_key;
        let state = agent(&self.ctx, &node_key).await?.remove_cabinet().await?;
        Ok(Response::new(CabinetStatus {
            version: state.version,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_the_build_and_nothing_outside_it() {
        let dir = std::env::temp_dir().join(format!("cabinet-driver-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("_app/immutable")).unwrap();
        assert_eq!(read(&dir).unwrap_err(), "cabinet_build_missing");
        fs::write(dir.join("index.html"), "page").unwrap();
        fs::write(dir.join("_app/immutable/a.js"), "a").unwrap();
        let files = read(&dir).unwrap();
        let names: Vec<_> = files.iter().map(|f| f.path.as_str()).collect();
        assert_eq!(names, ["_app/immutable/a.js", "index.html"]);
        std::os::unix::fs::symlink("/etc/hostname", dir.join("leak.txt")).unwrap();
        assert_eq!(read(&dir).unwrap_err(), "cabinet_files_invalid");
        assert_eq!(
            read(Path::new("relative")).unwrap_err(),
            "cabinet_build_missing"
        );
        fs::remove_dir_all(&dir).unwrap();
    }
}
