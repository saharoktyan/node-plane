//! Opt-in JSONL diagnostics; never write into the interactive terminal.
use std::{fs::OpenOptions, io::Write, path::PathBuf, time::Instant};
tokio::task_local! { static ACTION_ID: String; }

pub async fn action<F: std::future::Future>(name: &'static str, future: F) -> F::Output {
    ACTION_ID
        .scope(uuid::Uuid::new_v4().to_string(), async {
            let _timing = Stage::new(name);
            future.await
        })
        .await
}

pub struct Stage {
    started: Instant,
    path: Option<PathBuf>,
    name: &'static str,
    action_id: Option<String>,
    detail: String,
    request_id: Option<String>,
}

impl Stage {
    pub fn new(name: &'static str) -> Self {
        Self {
            started: Instant::now(),
            path: std::env::var_os("NODE_PLANE_TIMINGS_FILE")
                .filter(|s| !s.is_empty())
                .map(PathBuf::from),
            name,
            action_id: ACTION_ID.try_with(Clone::clone).ok(),
            detail: String::new(),
            request_id: None,
        }
    }

    pub fn endpoint(mut self, method: &str, path: &str) -> Self {
        let route = path
            .split('?')
            .next()
            .unwrap_or_default()
            .split('/')
            .map(|part| {
                if uuid::Uuid::parse_str(part).is_ok() {
                    ":id"
                } else {
                    part
                }
            })
            .collect::<Vec<_>>()
            .join("/");
        self.detail = format!("{method} {route}");
        self
    }

    pub fn request_id(mut self, value: Option<&str>) -> Self {
        self.request_id = value
            .and_then(|id| uuid::Uuid::parse_str(id).ok())
            .map(|id| id.to_string());
        self
    }
}

impl Drop for Stage {
    fn drop(&mut self) {
        let Some(path) = &self.path else { return };
        let value = serde_json::json!({
            "event": "NP_TIMING", "stage": self.name,
            "elapsed_ms": self.started.elapsed().as_secs_f64() * 1000.0,
            "pid": std::process::id(),
            "action_id": self.action_id, "detail": self.detail,
            "request_id": self.request_id,
            "timestamp_ms": std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_millis(),
        });
        // Timing failures must never interrupt an operation or expose its input.
        if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
            let line = format!("{value}\n");
            let _ = file.write_all(line.as_bytes());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn records_json_without_query_arguments_and_with_validated_request_id() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("timings.jsonl");
        let mut stage = Stage::new("backend_request")
            .endpoint(
                "GET",
                "/api/v1/profiles/12345678-1234-1234-1234-123456789abc?secret=private",
            )
            .request_id(Some("12345678-1234-1234-1234-123456789abc"));
        stage.path = Some(path.clone());
        drop(stage);
        let text = std::fs::read_to_string(path).unwrap();
        assert!(!text.contains("private"));
        let value: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(value["detail"], "GET /api/v1/profiles/:id");
        assert!(value["elapsed_ms"].as_f64().unwrap() >= 0.0);
        assert!(value["request_id"].is_string());
    }

    #[tokio::test]
    async fn concurrent_actions_keep_distinct_identifiers() {
        async fn id() -> Option<String> {
            tokio::task::yield_now().await;
            Stage::new("fixture").action_id.clone()
        }
        let (first, second) = tokio::join!(action("fixture", id()), action("fixture", id()));
        assert!(first.is_some());
        assert_ne!(first, second);
        assert!(Stage::new("outside").action_id.is_none());
    }
}
