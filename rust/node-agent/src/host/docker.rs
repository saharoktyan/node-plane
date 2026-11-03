//! Docker availability and installation.
use std::process::Command;

use tonic::Status;

use crate::agent::v1::InstallDockerResponse;
use crate::state::AgentState;

const INSTALL_DOCKER_SCRIPT: &str = r#"set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt_wait() {
  local timeout="${1:-300}"
  local elapsed=0
  while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
    || fuser /var/lib/dpkg/lock >/dev/null 2>&1 \
    || fuser /var/lib/apt/lists/lock >/dev/null 2>&1 \
    || fuser /var/cache/apt/archives/lock >/dev/null 2>&1; do
    if (( elapsed >= timeout )); then
      echo "Timed out waiting for apt/dpkg lock." >&2
      return 1
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
}

apt_run() {
  apt_wait
  apt-get "$@"
}

if ! command -v apt-get >/dev/null 2>&1; then
  echo "apt-get is not available. Automatic Docker install is supported only on Debian/Ubuntu." >&2
  exit 1
fi

# Runtime initialization and profile helpers run Python on the node host.
if ! command -v python3 >/dev/null 2>&1; then
  apt_run update
  apt_run install -y python3
fi

# Speed limits shape with tc and nft and enter the AWG container with
# nsenter. Without them a node only reports that it cannot limit speed.
shaping=""
command -v tc >/dev/null 2>&1 || shaping="$shaping iproute2"
command -v nft >/dev/null 2>&1 || shaping="$shaping nftables"
command -v nsenter >/dev/null 2>&1 || shaping="$shaping util-linux"
if [ -n "$shaping" ]; then
  { apt_run update && apt_run install -y $shaping; } \
    || echo "Speed limits need these packages:$shaping" >&2
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "DOCKER_INSTALL_STATUS|ok|available"
  exit 0
fi
if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
  echo "DOCKER_INSTALL_STATUS|ok|available_via_sudo"
  exit 0
fi

echo "Updating package index..."
apt_run update

echo "Installing Docker..."
apt_run install -y docker.io
apt-cache show docker-compose-plugin >/dev/null 2>&1 && apt_run install -y docker-compose-plugin || true

echo "Starting Docker..."
systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "DOCKER_INSTALL_STATUS|ok|available"
  exit 0
fi
if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
  echo "DOCKER_INSTALL_STATUS|ok|available_via_sudo"
  exit 0
fi

echo "DOCKER_INSTALL_STATUS|error|unavailable"
exit 1
"#;

impl AgentState {
    pub(crate) fn container_running(name: &str) -> bool {
        Command::new("docker")
            .args(["inspect", "-f", "{{.State.Running}}", name])
            .output()
            .is_ok_and(|output| {
                output.status.success() && String::from_utf8_lossy(&output.stdout).trim() == "true"
            })
    }

    pub(crate) fn install_docker(&self) -> Result<InstallDockerResponse, Status> {
        let output = Command::new("bash")
            .arg("-c")
            .arg(INSTALL_DOCKER_SCRIPT)
            .output()
            .map_err(|err| {
                Status::internal(format!("failed to execute docker install script: {err}"))
            })?;
        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        let summary = if stderr.is_empty() {
            stdout
        } else if stdout.is_empty() {
            stderr
        } else {
            format!("{stdout}\n{stderr}")
        };
        if output.status.success() {
            return Ok(InstallDockerResponse { summary });
        }
        Err(Status::failed_precondition(if summary.is_empty() {
            "docker install failed".to_string()
        } else {
            summary
        }))
    }

    pub(crate) fn docker_available(&self) -> bool {
        Command::new("docker")
            .arg("info")
            .output()
            .map(|output| output.status.success())
            .unwrap_or(false)
    }

    pub(crate) fn docker_best_effort(&self, args: &[&str]) {
        let _ = Command::new("docker").args(args).output();
    }
}
