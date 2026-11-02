//! Shared agent state passed to every module.
use std::fs;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use chrono::Utc;
use serde_json::Value;

use crate::config::AgentConfig;
use crate::host::metrics as host_metrics;

#[derive(Clone)]
pub(crate) struct AgentState {
    pub(crate) config: AgentConfig,
    pub(crate) last_seen_at: Arc<Mutex<String>>,
    pub(crate) instance_id: String,
    pub(crate) cpu: Arc<Mutex<host_metrics::CpuSampler>>,
}

impl AgentState {
    pub(crate) fn new(config: AgentConfig) -> Self {
        Self {
            config,
            cpu: Arc::new(Mutex::new(host_metrics::CpuSampler::default())),
            last_seen_at: Arc::new(Mutex::new(Utc::now().to_rfc3339())),
            instance_id: fs::read_to_string("/proc/sys/kernel/random/uuid")
                .map(|value| value.trim().to_string())
                .unwrap_or_else(|_| {
                    format!("{}-{}", std::process::id(), Utc::now().timestamp_micros())
                }),
        }
    }

    pub(crate) fn cpu_metrics(&self) -> Value {
        self.cpu
            .lock()
            .map(|sampler| sampler.metrics(Instant::now()))
            .unwrap_or_else(|_| serde_json::json!({}))
    }

    pub(crate) fn mark_heartbeat(&self) {
        if let Ok(mut guard) = self.last_seen_at.lock() {
            *guard = Utc::now().to_rfc3339();
        }
    }

    pub(crate) fn last_seen_at(&self) -> String {
        self.last_seen_at
            .lock()
            .map(|guard| guard.clone())
            .unwrap_or_default()
    }
}
