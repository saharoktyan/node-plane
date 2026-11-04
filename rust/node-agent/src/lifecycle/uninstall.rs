//! Agent removal from the host.
use std::env;
use std::fs;
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::process::Command;

use tonic::Status;

use crate::agent::v1::RuntimeCommandResponse;
use crate::state::AgentState;

impl AgentState {
    pub(crate) fn uninstall_agent(&self) -> Result<RuntimeCommandResponse, Status> {
        let _guard = self.guard_fenced_maintenance()?;
        // The delayed systemd unit runs after this RPC. A durable tombstone
        // closes the gap between scheduling uninstall and stopping the agent.
        let disabled_path = "/etc/node-plane/profile-intents.sqlite3.disabled";
        let mut marker = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(disabled_path)
            .map_err(|_| Status::failed_precondition("cannot fence agent removal"))?;
        marker
            .write_all(b"agent removal scheduled\n")
            .and_then(|_| marker.sync_all())
            .and_then(|_| fs::File::open("/etc/node-plane")?.sync_all())
            .map_err(|_| Status::failed_precondition("cannot persist agent removal fence"))?;
        self.schedule_uninstall(None)
    }

    pub(crate) fn uninstall_agent_for_decommission(
        &self,
        command_id: &str,
        bot_public_key: &str,
    ) -> Result<RuntimeCommandResponse, Status> {
        let _guard = self.guard_prepared_decommission(command_id)?;
        // The backend must call this only after a verified runtime delete.
        // The agent independently checks that its owned runtime paths are gone.
        if Path::new(&self.config.runtime_root).exists()
            || Path::new(&self.config.node_env_path).exists()
        {
            return Err(Status::failed_precondition(
                "runtime cleanup is not complete",
            ));
        }
        // Local installations need no SSH key. Remote removal is verified by
        // the controller with an independent SSH identity and the original key.
        let key = (!bot_public_key.trim().is_empty()).then_some(bot_public_key);
        self.schedule_uninstall(key)
    }

    pub(crate) fn schedule_uninstall(
        &self,
        bot_public_key: Option<&str>,
    ) -> Result<RuntimeCommandResponse, Status> {
        let public_key = bot_public_key.unwrap_or("").trim();
        if public_key
            .chars()
            .any(|value| matches!(value, '\n' | '\r' | '\0'))
            || public_key.len() > 4096
        {
            return Err(Status::invalid_argument("invalid bot SSH public key"));
        }
        let authorized_keys = if bot_public_key.is_some() {
            self.authorized_keys_path()?.to_string_lossy().into_owned()
        } else {
            String::new()
        };
        // The transient unit survives termination of this service. Do not
        // remove the durable fence until the agent has actually stopped.
        let script = r#"set -eu
systemctl disable --now node-plane-lease-expiry.timer 2>/dev/null || true
systemctl stop node-plane-lease-expiry.service 2>/dev/null || true
systemctl disable --now node-plane-leased-awg.service node-plane-leased-xray.service 2>/dev/null || true
rm -f /etc/systemd/system/node-plane-leased-awg.service /etc/systemd/system/node-plane-leased-xray.service
rm -f /etc/systemd/system/node-plane-lease-expiry.timer /etc/systemd/system/node-plane-lease-expiry.service /etc/systemd/system/.node-plane-lease-expiry.lock
systemctl disable node-plane-agent.service
systemctl stop node-plane-agent.service
rm -f /etc/node-plane/profile-intents.sqlite3 /etc/node-plane/profile-intents.sqlite3.lock /etc/node-plane/profile-intents.sqlite3.disabled /etc/node-plane/profile-intents.sqlite3-journal /etc/node-plane/profile-intents.sqlite3-wal /etc/node-plane/profile-intents.sqlite3-shm
rm -rf /etc/systemd/system/node-plane-agent.service.d
rm -f /etc/systemd/system/node-plane-agent.service "$1" "$1.previous" "$2" "$3" "$4" "$5"
rm -rf "$6" "$7" /etc/node-plane/operation-progress /etc/node-plane/force-recovery
rmdir /etc/node-plane/tls /etc/node-plane 2>/dev/null || true
systemctl daemon-reload
systemctl reset-failed node-plane-lease-expiry.service node-plane-lease-expiry.timer node-plane-leased-awg.service node-plane-leased-xray.service node-plane-agent.service 2>/dev/null || true
if [ -n "$9" ]; then
    for file in "$8" /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys; do
        [ -f "$file" ] || continue
        [ -L "$file" ] && continue
        temporary="$(mktemp "${file}.node-plane-tmp.XXXXXX")"
        if grep -Fvx -- "$9" "$file" > "$temporary"; then
            :
        else
            status="$?"
            if [ "$status" -ne 1 ]; then
                rm -f -- "$temporary"
                exit "$status"
            fi
        fi
        chmod 600 "$temporary"
        mv -f -- "$temporary" "$file"
        chmod 600 "$file"
    done
fi
"#;
        let binary = env::current_exe()
            .map_err(|err| Status::internal(format!("cannot locate agent executable: {err}")))?;
        let config_path = env::var("NODE_AGENT_CONFIG_PATH")
            .unwrap_or_else(|_| "/etc/node-plane/agent.toml".to_string());
        let output = Command::new("systemd-run")
            .args([
                "--quiet",
                "--collect",
                "--on-active=2s",
                "/bin/sh",
                "-c",
                script,
                "node-plane-uninstall",
            ])
            .arg(binary)
            .args([
                &config_path,
                &self.config.tls_certificate_path,
                &self.config.tls_key_path,
                &self.config.tls_client_ca_path,
                &self.config.state_dir,
                &self.config.log_dir,
                &authorized_keys,
                public_key,
            ])
            .output()
            .map_err(|err| Status::internal(format!("failed to schedule agent removal: {err}")))?;
        if !output.status.success() {
            return Err(Status::internal(
                String::from_utf8_lossy(&output.stderr).to_string(),
            ));
        }
        Ok(RuntimeCommandResponse {
            summary: "agent uninstall scheduled".to_string(),
            payload_json: String::new(),
        })
    }
}
