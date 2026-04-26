from __future__ import annotations

import base64
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Dict, Tuple

from config import APP_COMMIT, APP_SEMVER
from services.server_registry import RegisteredServer, get_server, list_servers, update_server_fields
from services.server_runtime import is_running_in_container, run_server_command, write_server_file, write_server_files
from services.ssh_keys import get_public_key
from utils.security import shell_env_assignment


log = logging.getLogger("server_bootstrap")


_JSON_OBJECT_AT_END_RE = re.compile(r"(\{[\s\S]*\})\s*$")
AWG_RUNTIME_CONTAINER = "amnezia-awg"
RUNTIME_VERSION_PATH = "/opt/node-plane-runtime/VERSION"
RUNTIME_BUILD_COMMIT_PATH = "/opt/node-plane-runtime/BUILD_COMMIT"


RUNTIME_ASSETS_DIR = Path(__file__).resolve().parents[2] / "runtime_assets"
RUNTIME_MANIFEST_PATH = RUNTIME_ASSETS_DIR / "manifest.json"


def _runtime_asset_text(name: str) -> str:
    return (RUNTIME_ASSETS_DIR / name).read_text(encoding="utf-8")


def _load_runtime_manifest() -> list[dict[str, str]]:
    return json.loads(RUNTIME_MANIFEST_PATH.read_text(encoding="utf-8"))


def _shell_join_args(*args: object) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in args)


def _extract_last_json_object(text: str) -> dict:
    match = _JSON_OBJECT_AT_END_RE.search(text or "")
    if not match:
        raise ValueError("No JSON object found in output")
    return json.loads(match.group(1))


def _docker_status(server: RegisteredServer) -> tuple[bool, str]:
    rc, out = run_server_command(
        server,
        """#!/usr/bin/env bash
set -euo pipefail
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "available"
elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
  echo "available_via_sudo"
else
  echo "missing"
fi
""",
        timeout=30,
    )
    status = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else "missing"
    return (rc == 0 and status in {"available", "available_via_sudo"}, status)


def _docker_install_suggestion(status: str, details: str = "") -> str:
    parts = [
        "Docker недоступен на сервере.",
        "",
        "Bootstrap не может продолжиться без рабочего Docker.",
        "Установи и запусти Docker, затем повтори Probe или Bootstrap.",
        "",
        "Рекомендуемые команды:",
        "apt-get update",
        "apt-get install -y docker.io",
        "apt-cache show docker-compose-plugin >/dev/null 2>&1 && apt-get install -y docker-compose-plugin || true",
        "systemctl enable --now docker || service docker start",
    ]
    if status == "available_via_sudo":
        parts = [
            "Docker доступен только через sudo.",
            "",
            "Для bootstrap это обычно нормально, но если на сервере дальше возникают ошибки доступа к Docker, проверь права пользователя или группу docker.",
        ]
    if details.strip():
        parts.extend(["", "Технические детали:", details.strip()[-1200:]])
    return "\n".join(parts)


NODE_ENV_EXAMPLE = _runtime_asset_text('node.env.example')



def _runtime_metadata_files() -> Dict[str, Tuple[str, str]]:
    return {
        RUNTIME_VERSION_PATH: (f"{APP_SEMVER}\n", "0644"),
        RUNTIME_BUILD_COMMIT_PATH: (f"{APP_COMMIT}\n", "0644"),
    }


XRAY_ADD_SCRIPT = _runtime_asset_text('xray-add-user.sh')



XRAY_ADD_EXISTING_SCRIPT = _runtime_asset_text('xray-add-user-existing.sh')



XRAY_LIST_SCRIPT = _runtime_asset_text('xray-list-users.sh')



XRAY_DEL_SCRIPT = _runtime_asset_text('xray-del-user.sh')



XRAY_INIT_SCRIPT = _runtime_asset_text('init-xray.sh')



XRAY_ENABLE_STATS_SCRIPT = _runtime_asset_text('xray-enable-stats.sh')



XRAY_TRAFFIC_SCRIPT = _runtime_asset_text('xray-list-traffic.sh')



XRAY_SYNC_SCRIPT = _runtime_asset_text('sync-xray.sh')



XRAY_DEPLOY_SCRIPT = _runtime_asset_text('deploy-xray.sh')



AWG_SHOW_ENTROPY_SCRIPT = _runtime_asset_text('show-awg-entropy.sh')



AWG_TEMPLATE_JSON = _runtime_asset_text('awg-template.json')



AWG_CONF2VPN_PY = _runtime_asset_text('conf2vpn.py')



AMNEZIA_CONFIG_DECODER_PY = _runtime_asset_text('amnezia-config-decoder.py')



AWG_ADD_SCRIPT = _runtime_asset_text('awg-add-user.sh')



AWG_DEL_SCRIPT = _runtime_asset_text('awg-del-user.sh')



AWG_INIT_SCRIPT = _runtime_asset_text('init-awg.sh')



AWG_REGENERATE_ENTROPY_SCRIPT = _runtime_asset_text('regenerate-awg-entropy.sh')



AWG_START_SCRIPT = _runtime_asset_text('amnezia-awg/start.sh')



AWG_DOCKERFILE = _runtime_asset_text('amnezia-awg/Dockerfile')



AWG_DEPLOY_SCRIPT = _runtime_asset_text('deploy-awg.sh')



def _packages_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail
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

if command -v apt-get >/dev/null 2>&1; then
  apt_run update
  apt_run install -y ca-certificates curl jq qrencode wireguard-tools python3 iproute2 iptables
  if ! command -v docker >/dev/null 2>&1; then
    apt_run install -y docker.io
    apt-cache show docker-compose-plugin >/dev/null 2>&1 && apt_run install -y docker-compose-plugin || true
  fi
fi
systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true
mkdir -p /etc/node-plane /opt/node-plane-runtime /opt/node-plane-runtime/xray /opt/node-plane-runtime/amnezia-awg/data
touch /etc/node-plane/node.env
"""


def _install_docker_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail
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
  echo "На сервере нет apt-get. Автоустановка Docker поддерживается только для Debian/Ubuntu."
  exit 1
fi

echo "Обновляю индекс пакетов..."
apt_run update

echo "Устанавливаю Docker..."
apt_run install -y docker.io
apt-cache show docker-compose-plugin >/dev/null 2>&1 && apt_run install -y docker-compose-plugin || true

echo "Запускаю Docker..."
systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "Docker установлен и доступен."
  exit 0
fi

if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
  echo "Docker установлен и доступен через sudo."
  exit 0
fi

echo "Docker установлен, но всё ещё недоступен для текущего пользователя."
exit 1
"""


def _single_line_note(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parts = [part.strip() for part in raw.replace("\r", "\n").split("\n") if part.strip()]
    return " | ".join(parts)[:1500]


def _mark(server: RegisteredServer, state: str, notes: str = "") -> None:
    update_server_fields(server.key, bootstrap_state=state, notes=_single_line_note(notes))


def _remote_file_exists(server: RegisteredServer, path: str) -> bool:
    rc, _ = run_server_command(server, f"test -f {shlex.quote(path)}", timeout=30)
    return rc == 0


def _port_label(field: str) -> str:
    labels = {
        "xray_tcp_port": "Xray TCP",
        "xray_xhttp_port": "Xray XHTTP",
        "awg_port": "AWG",
    }
    return labels.get(field, field)


def _format_port_status_summary(text: str) -> str:
    port_rows: dict[str, dict[str, str]] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("PORT_STATUS|"):
            _, field, proto, port, status, suggestion = (line.split("|", 5) + [""])[:6]
            row = port_rows.setdefault(field, {})
            row.update({"proto": proto, "port": port, "port_status": status, "port_suggestion": suggestion.strip()})
        elif line.startswith("FIREWALL_STATUS|"):
            _, field, proto, port, status, suggestion = (line.split("|", 5) + [""])[:6]
            row = port_rows.setdefault(field, {})
            row.update({"proto": proto, "port": port, "firewall_status": status, "firewall_suggestion": suggestion.strip()})

    if not port_rows:
        return (text or "").strip()

    lines = ["Сводка по портам:"]
    for field in ("xray_tcp_port", "xray_xhttp_port", "awg_port"):
        row = port_rows.get(field)
        if not row:
            continue
        proto = row.get("proto", "?")
        port = row.get("port", "?")
        port_status = row.get("port_status", "unknown")
        firewall_status = row.get("firewall_status", "unknown")

        port_text_map = {
            "managed": "используется управляемым рантаймом",
            "free": "свободен",
            "busy": "занят",
        }
        firewall_text_map = {
            "open": "открыт в firewall",
            "closed": "закрыт в firewall",
        }
        line = f"- {_port_label(field)} {port}/{proto}: {port_text_map.get(port_status, port_status)}, {firewall_text_map.get(firewall_status, firewall_status)}"
        if port_status == "busy" and row.get("port_suggestion"):
            line += f" | рекомендуемый порт: {row['port_suggestion']}"
        if firewall_status == "closed" and row.get("firewall_suggestion"):
            line += f" | {row['firewall_suggestion']}"
        lines.append(line)
    return "\n".join(lines)


def _cleanup_server_runtime(server: RegisteredServer, preserve_config: bool) -> Tuple[int, str]:
    preserve = "1" if preserve_config else "0"
    script = f"""#!/usr/bin/env bash
set -euo pipefail

docker_cmd() {{
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker "$@"
    return $?
  fi
  if command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    sudo -n docker "$@"
    return $?
  fi
  return 1
}}

docker_rm() {{
  local name="$1"
  docker_cmd rm -f "$name" >/dev/null 2>&1 || true
}}

docker_rmi() {{
  local image="$1"
  if [[ -z "$image" ]]; then
    return
  fi
  docker_cmd rmi -f "$image" >/dev/null 2>&1 || true
}}

rm_path() {{
  local target="$1"
  if rm -rf "$target" >/dev/null 2>&1; then
    return
  fi
  if command -v sudo >/dev/null 2>&1; then
    sudo -n rm -rf "$target" >/dev/null 2>&1 || true
  fi
}}

XRAY_CONTAINER="xray"
AWG_CONTAINER="{AWG_RUNTIME_CONTAINER}"
XRAY_IMAGE_DEFAULT="ghcr.io/xtls/xray-core:25.12.8"
AWG_IMAGE_DEFAULT="node-plane-amnezia-awg:0.2.16"
if [[ -f /etc/node-plane/node.env ]]; then
  source /etc/node-plane/node.env
fi

docker_rm "${{XRAY_CONTAINER_NAME:-$XRAY_CONTAINER}}"
docker_rm "${{AWG_CONTAINER_NAME:-$AWG_CONTAINER}}"
docker_rmi "${{XRAY_DOCKER_IMAGE:-$XRAY_IMAGE_DEFAULT}}"
docker_rmi "${{AWG_DOCKER_IMAGE:-$AWG_IMAGE_DEFAULT}}"
docker_rmi "amneziavpn/amneziawg-go:0.2.16"
docker_cmd image prune -af >/dev/null 2>&1 || true

if [[ "{preserve}" != "1" ]]; then
  if rm -f /etc/node-plane/node.env >/dev/null 2>&1; then
    :
  elif command -v sudo >/dev/null 2>&1; then
    sudo -n rm -f /etc/node-plane/node.env >/dev/null 2>&1 || true
  fi
  rm_path /opt/node-plane-runtime
  echo "Управляемый рантайм удалён вместе с конфигами."
else
  echo "Управляемый рантайм удалён. Существующие конфиги сохранены."
fi

leftovers=()
docker_inspect() {{
  docker_cmd "$@" >/dev/null 2>&1
}}

if docker_inspect container inspect "${{XRAY_CONTAINER_NAME:-$XRAY_CONTAINER}}"; then
  leftovers+=("xray container still present")
fi
if docker_inspect container inspect "${{AWG_CONTAINER_NAME:-$AWG_CONTAINER}}"; then
  leftovers+=("awg container still present")
fi
if docker_inspect image inspect "${{XRAY_DOCKER_IMAGE:-$XRAY_IMAGE_DEFAULT}}"; then
  leftovers+=("xray image still present")
fi
if docker_inspect image inspect "${{AWG_DOCKER_IMAGE:-$AWG_IMAGE_DEFAULT}}"; then
  leftovers+=("awg image still present")
fi
if docker_inspect image inspect "amneziavpn/amneziawg-go:0.2.16"; then
  leftovers+=("amneziavpn/amneziawg-go:0.2.16 still present")
fi
if [[ "{preserve}" != "1" && -e /etc/node-plane/node.env ]]; then
  leftovers+=("/etc/node-plane/node.env still present")
fi
if [[ "{preserve}" != "1" && -e /opt/node-plane-runtime ]]; then
  leftovers+=("/opt/node-plane-runtime still present")
fi

if (( ${{#leftovers[@]}} > 0 )); then
  printf '%s\n' "${{leftovers[@]}}"
  exit 1
fi
"""
    return run_server_command(server, script, timeout=180)


def _remove_bot_ssh_key(server: RegisteredServer) -> Tuple[int, str]:
    ok, public_key = get_public_key()
    if not ok:
        return 1, public_key
    payload = base64.b64encode(public_key.encode("utf-8")).decode("ascii")
    script = f"""#!/usr/bin/env bash
set -euo pipefail

AUTH_KEYS="${{HOME}}/.ssh/authorized_keys"
if [[ ! -f "$AUTH_KEYS" ]]; then
  echo "authorized_keys not present"
  exit 0
fi

python3 - <<'PY'
import base64
from pathlib import Path

key = base64.b64decode({payload!r}).decode("utf-8").strip()
path = Path.home() / ".ssh" / "authorized_keys"
if not path.exists():
    print("authorized_keys not present")
    raise SystemExit(0)
lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
kept = [line for line in lines if line.strip() != key]
if len(kept) == len(lines):
    print("bot ssh key not present")
    raise SystemExit(0)
path.write_text(("\\n".join(kept) + ("\\n" if kept else "")), encoding="utf-8")
print("bot ssh key removed")
PY

chmod 600 "$AUTH_KEYS" >/dev/null 2>&1 || true
"""
    return run_server_command(server, script, timeout=60)


def render_server_node_env(server: RegisteredServer) -> str:
    lines = [
        shell_env_assignment("SERVER_KEY", server.key),
        shell_env_assignment("XRAY_CONFIG", server.xray_config_path),
        shell_env_assignment("XRAY_CONTAINER_NAME", server.xray_service_name),
        shell_env_assignment("XRAY_DOCKER_DIR", "/opt/node-plane-runtime/xray"),
        shell_env_assignment("XRAY_DOCKER_IMAGE", "ghcr.io/xtls/xray-core:25.12.8"),
        shell_env_assignment("XRAY_INBOUND_TCP_TAG", "reality-tcp"),
        shell_env_assignment("XRAY_INBOUND_XHTTP_TAG", "reality-xhttp"),
        shell_env_assignment("AWG_CONTAINER_NAME", AWG_RUNTIME_CONTAINER),
        shell_env_assignment("AWG_DOCKER_DIR", "/opt/node-plane-runtime/amnezia-awg"),
        shell_env_assignment("AWG_DOCKER_IMAGE", "node-plane-amnezia-awg:0.2.16"),
        shell_env_assignment("AWG_IFACE", server.awg_iface),
        shell_env_assignment("AWG_CONFIG", f"/opt/node-plane-runtime/amnezia-awg/data/{server.awg_iface}.conf"),
        shell_env_assignment("AWG_SERVER_ADDRESS", "10.8.1.0/24"),
        shell_env_assignment("AWG_NETWORK", "10.8.1.0/24"),
        shell_env_assignment("AWG_DNS", "1.1.1.1"),
        shell_env_assignment("AWG_MTU", "1280"),
        shell_env_assignment("AWG_ALLOWED_IPS", "0.0.0.0/0"),
        shell_env_assignment("AWG_KEEPALIVE", "25"),
        shell_env_assignment("AWG_I1_PRESET", server.awg_i1_preset),
        shell_env_assignment("AWG_SERVER_IP", server.awg_public_host or server.public_host),
        shell_env_assignment("AWG_SERVER_PORT", server.awg_port),
    ]
    return "\n".join(lines) + "\n"


def _runtime_files() -> Dict[str, Tuple[str, str]]:
    files = {
        entry["target_path"]: (_runtime_asset_text(entry["asset_path"]), entry["mode"])
        for entry in _load_runtime_manifest()
    }
    files.update(_runtime_metadata_files())
    return files


def sync_server_node_env(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"
    content = render_server_node_env(server)
    rc, out = write_server_file(server, "/etc/node-plane/node.env", content, mode="0600")
    if rc != 0:
        return rc, out
    update_server_fields(server.key, notes="node.env synced from bot")
    return 0, "Файл node.env записан в /etc/node-plane/node.env"


def _runtime_state_from_values(version: str, commit: str) -> str:
    version_value = str(version or "").strip()
    commit_value = str(commit or "").strip()
    if commit_value and commit_value != "unknown":
        if APP_COMMIT != "unknown":
            return "up_to_date" if commit_value == APP_COMMIT else "outdated"
        if version_value:
            return "up_to_date" if version_value == APP_SEMVER else "outdated"
        return "unknown"
    if version_value:
        return "up_to_date" if version_value == APP_SEMVER else "outdated"
    return "unknown"


def get_server_runtime_state(server_key: str) -> dict[str, str]:
    server = get_server(server_key)
    if not server:
        return {
            "state": "missing_server",
            "version": "",
            "commit": "",
            "expected_version": APP_SEMVER,
            "expected_commit": APP_COMMIT,
            "message": f"Сервер {server_key} не найден",
        }
    if server.bootstrap_state != "bootstrapped":
        return {
            "state": "not_bootstrapped",
            "version": "",
            "commit": "",
            "expected_version": APP_SEMVER,
            "expected_commit": APP_COMMIT,
            "message": "bootstrap required",
        }

    script = f"""#!/usr/bin/env bash
set -euo pipefail
if [[ -f {shlex.quote(RUNTIME_VERSION_PATH)} ]]; then
  printf 'version=%s\\n' "$(tr -d '\\r' < {shlex.quote(RUNTIME_VERSION_PATH)} | head -n1)"
fi
if [[ -f {shlex.quote(RUNTIME_BUILD_COMMIT_PATH)} ]]; then
  printf 'commit=%s\\n' "$(tr -d '\\r' < {shlex.quote(RUNTIME_BUILD_COMMIT_PATH)} | head -n1)"
fi
"""
    rc, out = run_server_command(server, script, timeout=30)
    if rc != 0:
        return {
            "state": "unknown",
            "version": "",
            "commit": "",
            "expected_version": APP_SEMVER,
            "expected_commit": APP_COMMIT,
            "message": (out or "").strip() or "failed to read runtime metadata",
        }

    payload: dict[str, str] = {}
    for raw_line in (out or "").splitlines():
        line = raw_line.strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        payload[key.strip()] = value.strip()
    version = str(payload.get("version") or "").strip()
    commit = str(payload.get("commit") or "").strip()
    return {
        "state": _runtime_state_from_values(version, commit),
        "version": version,
        "commit": commit,
        "expected_version": APP_SEMVER,
        "expected_commit": APP_COMMIT,
        "message": "",
    }


def get_servers_needing_runtime_sync() -> list[RegisteredServer]:
    targets: list[RegisteredServer] = []
    for server in list_servers(include_disabled=False):
        if server.bootstrap_state != "bootstrapped":
            continue
        state = str(get_server_runtime_state(server.key).get("state") or "")
        if state in {"outdated", "unknown"}:
            targets.append(server)
    return targets


def sync_server_runtime(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"
    if server.bootstrap_state != "bootstrapped":
        return 1, f"Сервер {server_key} ещё не bootstrap-нут"

    file_rc, file_out = write_server_files(server, _runtime_files(), timeout=180)
    if file_rc != 0:
        return file_rc, file_out

    node_env_rc, node_env_out = write_server_file(server, "/etc/node-plane/node.env", render_server_node_env(server), mode="0600")
    if node_env_rc != 0:
        return node_env_rc, node_env_out

    messages = [
        f"Runtime files synced for {server_key}.",
        "node.env updated.",
    ]
    if "xray" in server.protocol_kinds:
        rc, out = sync_xray_server_settings(server_key)
        if rc != 0:
            return rc, f"{messages[0]}\n\nXray sync failed:\n{out}"
        messages.append("Xray settings synced.")
    update_server_fields(server.key, notes=f"runtime synced to {APP_SEMVER} · {APP_COMMIT}")
    return 0, "\n".join(messages)


def _check_server_ports(server) -> Tuple[int, str]:
    checks: list[tuple[str, str, int]] = []
    if "xray" in server.protocol_kinds:
        checks.append(("xray_tcp_port", "tcp", int(server.xray_tcp_port)))
        checks.append(("xray_xhttp_port", "tcp", int(server.xray_xhttp_port)))
    if "awg" in server.protocol_kinds:
        checks.append(("awg_port", "udp", int(server.awg_port)))
    if not checks:
        return 0, "Проверка портов пропущена"

    payload = "\n".join(f"{field}|{proto}|{port}" for field, proto, port in checks)
    cmd = f"""#!/usr/bin/env bash
set -euo pipefail

cat <<'EOF' >/tmp/node-plane-port-check.txt
{payload}
EOF

docker_cmd() {{
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker "$@"
    return
  fi
  if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    sudo docker "$@"
    return
  fi
  return 1
}}

container_running() {{
  local name="$1"
  docker_cmd ps --format '{{{{.Names}}}}' 2>/dev/null | grep -q "^${{name}}$"
}}

is_busy_tcp() {{
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -H -ltn "( sport = :$port )" 2>/dev/null | grep -q .
    return $?
  fi
  netstat -ltn 2>/dev/null | awk '{{print $4}}' | grep -Eq '[:.]'"$port"'$'
}}

is_busy_udp() {{
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -H -lun "( sport = :$port )" 2>/dev/null | grep -q .
    return $?
  fi
  netstat -lun 2>/dev/null | awk '{{print $4}}' | grep -Eq '[:.]'"$port"'$'
}}

find_free_tcp() {{
  local start="$1"
  local end=$((start + 50))
  local port
  for ((port=start+1; port<=end; port++)); do
    if ! is_busy_tcp "$port"; then
      echo "$port"
      return 0
    fi
  done
  echo ""
}}

find_free_udp() {{
  local start="$1"
  local end=$((start + 50))
  local port
  for ((port=start+1; port<=end; port++)); do
    if ! is_busy_udp "$port"; then
      echo "$port"
      return 0
    fi
  done
  echo ""
}}

has_ufw_rules() {{
  command -v iptables >/dev/null 2>&1 && iptables -S ufw-user-input >/dev/null 2>&1
}}

firewall_allows() {{
  local proto="$1"
  local port="$2"
  if has_ufw_rules; then
    iptables -C ufw-user-input -p "$proto" --dport "$port" -j ACCEPT >/dev/null 2>&1
    return $?
  fi
  return 0
}}

is_managed_xray_port() {{
  local field="$1"
  local port="$2"
  local container="{server.xray_service_name}"
  local cfg="{server.xray_config_path}"
  container_running "$container" || return 1
  [[ -f "$cfg" ]] || return 1
  XRAY_FIELD="$field" XRAY_PORT="$port" XRAY_CFG="$cfg" python3 - <<'PY'
import json
import os
import sys

field = os.environ["XRAY_FIELD"]
port = int(os.environ["XRAY_PORT"])
cfg_path = os.environ["XRAY_CFG"]
tag_map = {{
    "xray_tcp_port": "reality-tcp",
    "xray_xhttp_port": "reality-xhttp",
}}
tag = tag_map.get(field)
if not tag:
    sys.exit(1)
try:
    with open(cfg_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(1)
for inbound in data.get("inbounds", []) or []:
    if str(inbound.get("tag") or "") != tag:
        continue
    try:
        inbound_port = int(inbound.get("port") or 0)
    except Exception:
        inbound_port = 0
    if inbound_port == port:
        sys.exit(0)
sys.exit(1)
PY
}}

is_managed_awg_port() {{
  local port="$1"
  local container="{AWG_RUNTIME_CONTAINER}"
  local cfg="{server.awg_config_path}"
  container_running "$container" || return 1
  [[ -f "$cfg" ]] || return 1
  AWG_PORT="$port" AWG_CFG="$cfg" python3 - <<'PY'
import os
import re
import sys

port = int(os.environ["AWG_PORT"])
cfg_path = os.environ["AWG_CFG"]
listen_port = None
try:
    with open(cfg_path, "r", encoding="utf-8", errors="ignore") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^ListenPort\\s*=\\s*(\\d+)\\s*$", line)
            if m:
                listen_port = int(m.group(1))
                break
except Exception:
    sys.exit(1)
sys.exit(0 if listen_port == port else 1)
PY
}}

while IFS='|' read -r field proto port; do
  [[ -n "$field" ]] || continue
  if [[ "$proto" == "tcp" ]]; then
    if is_busy_tcp "$port"; then
      if is_managed_xray_port "$field" "$port"; then
        echo "PORT_STATUS|$field|$proto|$port|managed|"
      else
        echo "PORT_STATUS|$field|$proto|$port|busy|$(find_free_tcp "$port")"
      fi
    else
      echo "PORT_STATUS|$field|$proto|$port|free|"
    fi
  else
    if is_busy_udp "$port"; then
      if [[ "$field" == "awg_port" ]] && is_managed_awg_port "$port"; then
        echo "PORT_STATUS|$field|$proto|$port|managed|"
      else
        echo "PORT_STATUS|$field|$proto|$port|busy|$(find_free_udp "$port")"
      fi
    else
      echo "PORT_STATUS|$field|$proto|$port|free|"
    fi
  fi
  if firewall_allows "$proto" "$port"; then
    echo "FIREWALL_STATUS|$field|$proto|$port|open|"
  else
    echo "FIREWALL_STATUS|$field|$proto|$port|closed|ufw allow $port/$proto"
  fi
done </tmp/node-plane-port-check.txt
rm -f /tmp/node-plane-port-check.txt
"""
    rc, out = run_server_command(server, cmd, timeout=30)
    if rc != 0:
        return rc, out

    conflicts: list[str] = []
    firewall_conflicts: list[str] = []
    suggestions: list[str] = []
    firewall_suggestions: list[str] = []
    for raw_line in (out or "").splitlines():
        line = raw_line.strip()
        if line.startswith("PORT_STATUS|"):
            _, field, proto, port, status, suggestion = (line.split("|", 5) + [""])[:6]
            if status == "managed":
                continue
            if status == "busy":
                conflicts.append(f"- {field}: {port}/{proto} занят")
                if suggestion.strip():
                    suggestions.append(f"/setserverfield {server.key} {field} {suggestion.strip()}")
            continue
        if line.startswith("FIREWALL_STATUS|"):
            _, field, proto, port, status, suggestion = (line.split("|", 5) + [""])[:6]
            if status == "closed":
                firewall_conflicts.append(f"- {field}: {port}/{proto} не открыт в firewall")
                if suggestion.strip():
                    firewall_suggestions.append(suggestion.strip())
    if conflicts or firewall_conflicts:
        lines = [
            f"Проверка портов для сервера {server.key} завершилась ошибкой.",
        ]
        if conflicts:
            lines.extend(["Занятые порты:", *conflicts])
        if firewall_conflicts:
            if conflicts:
                lines.append("")
            lines.extend(["Не хватает правил firewall:", *firewall_conflicts])
        if suggestions:
            lines.extend(["", "Рекомендуемые команды:", *[f"- {item}" for item in suggestions]])
        if firewall_suggestions:
            lines.extend(["", "Рекомендуемые команды для firewall:", *[f"- {item}" for item in firewall_suggestions]])
        return 1, "\n".join(lines)
    return 0, _format_port_status_summary(out)


def check_server_ports(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"
    return _check_server_ports(server)


def open_server_ports(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"

    checks: list[tuple[str, str, int]] = []
    if "xray" in server.protocol_kinds:
        checks.append(("xray_tcp_port", "tcp", int(server.xray_tcp_port)))
        checks.append(("xray_xhttp_port", "tcp", int(server.xray_xhttp_port)))
    if "awg" in server.protocol_kinds:
        checks.append(("awg_port", "udp", int(server.awg_port)))
    if not checks:
        return 0, "Для этого сервера нет управляемых портов"

    payload = "\n".join(f"{field}|{proto}|{port}" for field, proto, port in checks)
    cmd = f"""#!/usr/bin/env bash
set -euo pipefail

command -v ufw >/dev/null 2>&1 || {{
  echo "UFW не установлен на этой ноде."
  exit 1
}}

cat <<'EOF' >/tmp/node-plane-open-ports.txt
{payload}
EOF

while IFS='|' read -r field proto port; do
  [[ -n "$field" ]] || continue
  ufw allow "$port/$proto" >/dev/null
  echo "OPENED|$field|$proto|$port"
done </tmp/node-plane-open-ports.txt

rm -f /tmp/node-plane-open-ports.txt
ufw reload >/dev/null 2>&1 || true
"""
    rc, out = run_server_command(server, cmd, timeout=60)
    if rc != 0:
        return rc, out

    opened: list[str] = []
    for raw_line in (out or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("OPENED|"):
            continue
        _, field, proto, port = (line.split("|", 3) + [""])[:4]
        opened.append(f"- {field}: {port}/{proto}")
    if not opened:
        return 0, "Правила firewall обновлены."
    return 0, "Открыты правила firewall:\n" + "\n".join(opened)


def probe_server(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"
    if server.transport == "local" and is_running_in_container():
        return (
            1,
            "PROBE_UNSUPPORTED|local_in_container\n"
            "hostname: local-host\n"
            "пользователь: bot\n"
            "ядро: container\n"
            "reason: Local transport is unavailable while the bot runs inside a container.\n"
            "remediation: Register this node with transport=ssh or run the bot on the host via systemd.",
        )
    cmd = """#!/usr/bin/env bash
set -euo pipefail

echo "hostname: $(hostname)"
echo "пользователь: $(whoami)"
echo "ядро: $(uname -a)"

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "docker: доступен"
elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
  echo "docker: доступен через sudo"
else
  echo "docker: недоступен"
fi

if [[ -c /dev/net/tun ]]; then
  echo "tun: доступен"
else
  echo "tun: отсутствует"
fi

if [[ -c /dev/net/tun ]] && { (command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1) || (command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1); }; then
  echo "awg_userspace_ready: да"
else
  echo "awg_userspace_ready: нет"
fi
"""
    rc, out = run_server_command(server, cmd, timeout=20)
    if rc == 0:
        lines = [line.strip() for line in (out or "").splitlines() if line.strip()]
        summary = []
        docker_line = next((line for line in lines if line.startswith("docker:")), "")
        for prefix in ("docker:", "tun:", "awg_userspace_ready:"):
            hit = next((line for line in lines if line.startswith(prefix)), None)
            if hit:
                summary.append(hit)
        port_rc, port_out = _check_server_ports(server)
        if port_out.strip():
            out = (out.rstrip() + "\n\n" + port_out.strip()).strip()
        if docker_line == "docker: недоступен":
            out = (out.rstrip() + "\n\n" + _docker_install_suggestion("missing")).strip()
        if port_rc != 0:
            summary.append("ports: conflict")
        if summary:
            try:
                update_server_fields(server.key, notes="probe: " + " | ".join(summary))
            except Exception:
                pass
        if port_rc != 0:
            return 1, out
    return rc, out


def install_server_docker(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"

    docker_ok, docker_status = _docker_status(server)
    if docker_ok:
        if docker_status == "available_via_sudo":
            return 0, "Docker уже установлен и доступен через sudo."
        return 0, "Docker уже установлен и доступен."

    rc, out = run_server_command(server, _install_docker_script(), timeout=900)
    docker_ok, docker_status = _docker_status(server)
    if docker_ok:
        if docker_status == "available_via_sudo":
            return 0, "DOCKER_INSTALL_STATUS|ok|available_via_sudo"
        return 0, "DOCKER_INSTALL_STATUS|ok|available"

    if rc != 0:
        tail = (out or "").strip()[-2000:]
        return rc, f"DOCKER_INSTALL_STATUS|error|{docker_status}\n{tail}".strip()
    return 1, f"DOCKER_INSTALL_STATUS|error|{docker_status}"


def is_server_docker_available(server_key: str) -> bool:
    server = get_server(server_key)
    if not server:
        return False
    docker_ok, _ = _docker_status(server)
    return docker_ok


def sync_xray_server_settings(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"
    if "xray" not in server.protocol_kinds:
        return 1, f"На сервере {server_key} не включён Xray"

    rc, out = run_server_command(
        server,
        _shell_join_args(
            "/opt/node-plane-runtime/sync-xray.sh",
            server.xray_config_path,
            server.public_host,
            server.xray_flow,
            "ghcr.io/xtls/xray-core:25.12.8",
        ),
        timeout=120,
    )
    if rc != 0:
        return rc, out
    try:
        generated = _extract_last_json_object(out)
    except Exception:
        return 1, f"Не удалось разобрать синхронизированные настройки Xray:\n{out[-1500:]}"
    update_server_fields(server.key, **generated)
    return 0, json.dumps(generated, ensure_ascii=False, indent=2)


def show_awg_entropy(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"
    if "awg" not in server.protocol_kinds:
        return 1, f"На сервере {server_key} не включён AWG"
    return run_server_command(server, "/opt/node-plane-runtime/show-awg-entropy.sh", timeout=60)


def regenerate_awg_entropy(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"
    if "awg" not in server.protocol_kinds:
        return 1, f"На сервере {server_key} не включён AWG"
    return run_server_command(server, "/opt/node-plane-runtime/regenerate-awg-entropy.sh", timeout=180)


def show_server_metrics(server_key: str) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"
    script = _server_metrics_script()
    return run_server_command(server, script, timeout=60)


def _server_metrics_script() -> str:
    return r"""#!/usr/bin/env bash
set -euo pipefail

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
    return 0
  fi
  if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    sudo docker "$@"
    return 0
  fi
  return 1
}

echo "host: $(hostname)"
echo "kernel: $(uname -srmo)"
if command -v uptime >/dev/null 2>&1; then
  echo "uptime: $(uptime -p 2>/dev/null || uptime)"
fi
if [[ -r /proc/loadavg ]]; then
  echo "loadavg: $(cut -d' ' -f1-3 /proc/loadavg)"
fi
if [[ -r /proc/stat ]]; then
  cpu_usage="$(python3 - <<'PY'
import time

def read():
    with open("/proc/stat", "r", encoding="utf-8") as fh:
        parts = fh.readline().split()[1:]
    values = [int(part) for part in parts[:8]]
    total = sum(values)
    idle = values[3] + values[4]
    return total, idle

t1, i1 = read()
time.sleep(0.2)
t2, i2 = read()
total_delta = max(t2 - t1, 0)
idle_delta = max(i2 - i1, 0)
busy = 0.0 if total_delta <= 0 else (100.0 * (total_delta - idle_delta) / total_delta)
print(f"{busy:.1f}%")
PY
)"
  echo "cpu usage: $cpu_usage"
fi
if command -v nproc >/dev/null 2>&1; then
  echo "cpus: $(nproc)"
fi
if command -v free >/dev/null 2>&1; then
  free -h | awk 'NR==2 {print "memory: " $3 " / " $2 " used"}'
fi
if command -v df >/dev/null 2>&1; then
  df -h / | awk 'NR==2 {print "disk /: " $3 " / " $2 " used (" $5 ")"}'
fi

if docker_cmd ps --format '{{.Names}} {{.Status}}' >/tmp/node-plane-metrics-docker.$$ 2>/dev/null; then
  echo "docker: available"
  xray_status="$(grep '^xray ' /tmp/node-plane-metrics-docker.$$ | cut -d' ' -f2- | head -n1 || true)"
  awg_status="$(grep '^amnezia-awg ' /tmp/node-plane-metrics-docker.$$ | cut -d' ' -f2- | head -n1 || true)"
  echo "xray: ${xray_status:-not running}"
  echo "awg: ${awg_status:-not running}"
  rm -f /tmp/node-plane-metrics-docker.$$
else
  echo "docker: unavailable"
fi
"""


def bootstrap_server(server_key: str, preserve_config: bool = False) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"

    port_rc, port_out = _check_server_ports(server)
    if port_rc != 0:
        _mark(server, "bootstrap_failed", port_out[-1500:])
        return port_rc, port_out

    rc, out = run_server_command(server, _packages_script(), timeout=600)
    if rc != 0:
        docker_ok, docker_status = _docker_status(server)
        if not docker_ok:
            msg = _docker_install_suggestion(docker_status, out)
            _mark(server, "bootstrap_failed", msg[-1500:])
            return 1, msg
        _mark(server, "bootstrap_failed", out[-1500:])
        return rc, out

    docker_ok, docker_status = _docker_status(server)
    if not docker_ok:
        msg = _docker_install_suggestion(docker_status, out)
        _mark(server, "bootstrap_failed", msg[-1500:])
        return 1, msg

    file_rc, file_out = write_server_files(server, _runtime_files(), timeout=180)
    if file_rc != 0:
        _mark(server, "bootstrap_failed", file_out[-1500:])
        return file_rc, file_out

    node_env_rc, node_env_out = write_server_file(server, "/etc/node-plane/node.env", render_server_node_env(server), mode="0600")
    if node_env_rc != 0:
        _mark(server, "bootstrap_failed", node_env_out[-1500:])
        return node_env_rc, node_env_out

    reused_xray_config = False
    reused_awg_config = False

    if "xray" in server.protocol_kinds:
        sni_host = server.xray_sni or "www.cloudflare.com"
        if preserve_config and _remote_file_exists(server, server.xray_config_path):
            reused_xray_config = True
        else:
            rc, out = run_server_command(
                server,
                _shell_join_args(
                    "/opt/node-plane-runtime/init-xray.sh",
                    server.xray_config_path,
                    server.public_host,
                    sni_host,
                    server.xray_tcp_port,
                    server.xray_xhttp_port,
                    server.xray_xhttp_path_prefix,
                    server.xray_flow,
                    "ghcr.io/xtls/xray-core:25.12.8",
                ),
                timeout=180,
            )
            if rc != 0:
                _mark(server, "bootstrap_failed", out[-1500:])
                return rc, out
            try:
                generated = _extract_last_json_object(out)
            except Exception:
                _mark(server, "bootstrap_failed", out[-1500:])
                return 1, f"Не удалось разобрать сгенерированные настройки Xray:\n{out[-1500:]}"
            if not generated.get("xray_pbk"):
                _mark(server, "bootstrap_failed", out[-1500:])
                return 1, f"Сгенерированные настройки Xray неполные:\n{out[-1500:]}"
            update_server_fields(server.key, **generated)
        rc, out = run_server_command(server, "/opt/node-plane-runtime/deploy-xray.sh", timeout=300)
        if rc != 0:
            _mark(server, "bootstrap_failed", out[-1500:])
            return rc, out

    if "awg" in server.protocol_kinds:
        if preserve_config and _remote_file_exists(server, server.awg_config_path):
            reused_awg_config = True
        else:
            rc, out = run_server_command(server, "/opt/node-plane-runtime/init-awg.sh", timeout=120)
            if rc != 0:
                _mark(server, "bootstrap_failed", out[-1500:])
                return rc, out
        rc, out = run_server_command(server, "/opt/node-plane-runtime/deploy-awg.sh", timeout=900)
        if rc != 0:
            _mark(server, "bootstrap_failed", out[-1500:])
            return rc, out

    completed_parts = ["Base packages and helper scripts installed"]
    if "xray" in server.protocol_kinds:
        if reused_xray_config:
            completed_parts.append("Xray config preserved, runtime redeployed")
        else:
            completed_parts.append("Xray settings generated, runtime deployed")
    if "awg" in server.protocol_kinds:
        if reused_awg_config:
            completed_parts.append("AWG config preserved, runtime redeployed")
        else:
            completed_parts.append("AWG runtime deployed")
    summary = ". ".join(completed_parts) + "."

    _mark(
        server,
        "bootstrapped",
        summary,
    )
    return 0, (
        "Bootstrap завершён.\n"
        f"{summary}\n"
        "Рабочий node.env записан в /etc/node-plane/node.env."
    )


def reinstall_server(server_key: str, preserve_config: bool = True) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"

    prefix = "Режим переустановки: с сохранением существующего конфига.\n\n" if preserve_config else "Режим переустановки: чистая переустановка.\n\n"
    if not preserve_config:
        rc, out = _cleanup_server_runtime(server, preserve_config=False)
        if rc != 0:
            _mark(server, "bootstrap_failed", out[-1500:])
            return rc, out
    rc, out = bootstrap_server(server_key, preserve_config=preserve_config)
    return rc, prefix + out


def delete_server_runtime(server_key: str, preserve_config: bool = True) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"

    rc, out = _cleanup_server_runtime(server, preserve_config=preserve_config)
    if rc != 0:
        _mark(server, "bootstrap_failed", out[-1500:])
        return rc, out

    updates = {
        "bootstrap_state": "edited" if preserve_config else "new",
        "notes": "runtime removed; config preserved" if preserve_config else "runtime removed; config wiped",
    }
    if not preserve_config:
        updates.update(
            {
                "xray_pbk": "",
                "xray_short_id": "",
            }
        )
    update_server_fields(server.key, **updates)
    suffix = "Существующие конфиги сохранены." if preserve_config else "Конфиги и директории рантайма удалены."
    return 0, f"Рантайм удалён.\n{suffix}"


def full_cleanup_server(server_key: str, remove_ssh_key: bool = False) -> Tuple[int, str]:
    server = get_server(server_key)
    if not server:
        return 1, f"Сервер {server_key} не найден"

    rc, out = delete_server_runtime(server_key, preserve_config=False)
    if rc != 0:
        return rc, out

    cleanup_lines = [out.strip()]
    notes = ["full cleanup completed"]

    if remove_ssh_key and server.transport == "ssh":
        key_rc, key_out = _remove_bot_ssh_key(server)
        if key_rc == 0:
            if key_out.strip():
                cleanup_lines.append(key_out.strip())
            notes.append("ssh key removed")
        else:
            cleanup_lines.append(f"SSH key removal failed: {(key_out or '').strip()[:800]}")
            notes.append("ssh key removal failed")
        update_server_fields(server.key, notes="; ".join(notes))
        return 0, "\n".join(line for line in cleanup_lines if line)

    update_server_fields(server.key, notes="; ".join(notes))
    return 0, "\n".join(line for line in cleanup_lines if line)
