"""Container deployments through deploy-xray.sh and deploy-awg.sh, which
start the built agent, against a stateful fake Docker. In the Docker suite,
where CI builds the agent."""
import json
import os
import pathlib
import re
import secrets
import shlex
import subprocess
import tempfile
import unittest
import uuid

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = REPO_ROOT / "runtime_assets"
AGENT_BINARY = REPO_ROOT / "rust/node-agent/target/debug/node-plane-agent"
HEX64_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class FakeDocker:
    """Stateful fake Docker daemon emulator for isolated deployment testing."""

    def __init__(self, root: pathlib.Path):
        self.root = root
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = root / "docker_state.json"
        self.log_file = root / "docker_calls.log"
        self._init_state()
        self._install_tools()

    def _init_state(self):
        state = {
            "containers": {},
            "calls": [],
            "mutations": [],
            "fail_ps": False,
            "corrupt_ps": False,
            "fail_inspect": False,
            "corrupt_inspect": False,
            "fail_run": False,
            "fail_run_status": None,
            "fail_start": False,
            "on_rename_inject_collision": None,
            "on_run_wrong_token": False,
            "on_run_corrupt_labels": False,
        }
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

    def _load_state(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def _save_state(self, state: dict):
        self.state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _install_tools(self):
        # Fake docker executable implementation
        docker_code = f"""#!/usr/bin/env python3
import json
import os
import re
import secrets
import sys
from pathlib import Path

STATE_FILE = Path({repr(str(self.state_file))})
LOG_FILE = Path({repr(str(self.log_file))})
HEX64 = re.compile(r"^[0-9a-fA-F]{{64}}$")

def load_state():
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))

def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")

def log_call(args):
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(" ".join(args) + "\\n")

def main():
    args = sys.argv[1:]
    log_call(args)
    if not args:
        sys.exit(0)

    cmd = args[0]
    state = load_state()
    state["calls"].append(args)

    if cmd == "info":
        sys.exit(0)
    elif cmd == "pull":
        sys.exit(0)
    elif cmd == "build":
        sys.exit(0)
    elif cmd == "network":
        # Network inspect or create dummy responses
        if len(args) > 1 and args[1] == "inspect":
            sys.stdout.write('[{{'
                '"Name": "' + (args[2] if len(args) > 2 else "node-plane-awg") + '",'
                '"Driver": "bridge",'
                '"EnableIPv6": true,'
                '"Labels": {{"org.node-plane.network": "awg"}},'
                '"IPAM": {{"Config": [{{"Subnet": "fd42:6e70:6177:1::/64"}}]}}'
                '}}]\\n')
        sys.exit(0)
    elif cmd == "ps":
        if state.get("fail_ps"):
            sys.stderr.write("Simulated docker ps failure\\n")
            sys.exit(1)
        if state.get("corrupt_ps"):
            sys.stdout.write("invalid-not-64-hex-id xray\\n")
            sys.stdout.write("invalid-not-64-hex-id amnezia-awg\\n")
            sys.exit(0)
        # Format flag: docker ps -a --no-trunc --format '{{{{.ID}}}} {{{{Names}}}}'
        for c in state["containers"].values():
            cid = c["Id"]
            name = c["Name"].lstrip("/")
            sys.stdout.write(f"{{cid}} {{name}}\\n")
        sys.exit(0)
    elif cmd == "container" and len(args) > 1 and args[1] == "inspect":
        inspect_args = args[2:]
        _handle_inspect(inspect_args, state)
    elif cmd == "inspect":
        inspect_args = args[1:]
        _handle_inspect(inspect_args, state)
    elif cmd == "image" and len(args) > 1 and args[1] == "inspect":
        sys.stdout.write("image_os=linux image_arch=amd64\\n")
        sys.exit(0)
    elif cmd == "run":
        _handle_run(args[1:], state)
    elif cmd == "rename":
        _handle_rename(args[1:], state)
    elif cmd == "stop":
        _handle_stop(args[1:], state)
    elif cmd == "start":
        _handle_start(args[1:], state)
    elif cmd == "rm":
        _handle_rm(args[1:], state)
    elif cmd == "update":
        _handle_update(args[1:], state)
    elif cmd == "logs":
        _handle_logs(args[1:], state)
    else:
        sys.exit(0)

def _handle_inspect(args, state):
    if state.get("fail_inspect"):
        sys.stderr.write("Simulated inspect failure\\n")
        sys.exit(1)
    if state.get("corrupt_inspect"):
        sys.stdout.write("{{not-valid-json\\n")
        sys.exit(0)

    fmt = None
    target = None
    i = 0
    while i < len(args):
        if args[i] in ("-f", "--format") and i + 1 < len(args):
            fmt = args[i + 1]
            i += 2
        else:
            target = args[i]
            i += 1

    if not target or target not in state["containers"]:
        sys.stderr.write(f"Error: No such container: {{target}}\\n")
        sys.exit(1)

    c = state["containers"][target]
    if fmt:
        if ".State.Status" in fmt:
            sys.stdout.write(f"{{c.get('State', {{}}).get('Status', 'unknown')}}\\n")
        elif ".State.ExitCode" in fmt:
            sys.stdout.write("exit_code=0 error= oom_killed=false started_at= finished_at=\\n")
        else:
            sys.stdout.write(f"{{c.get('State', {{}}).get('Status', 'unknown')}}\\n")
        sys.exit(0)

    # Standard JSON array of records
    sys.stdout.write(json.dumps([c]) + "\\n")
    sys.exit(0)

def _handle_run(args, state):
    if "--rm" in args:
        sys.exit(0)

    if "-d" not in args:
        sys.exit(0)

    if state.get("on_run_write_foreign_cid_and_fail"):
        foreign_id = secrets.token_hex(32)
        c_name = "/foreign"
        foreign_mounts = []
        foreign_labels = {{}}
        for idx, a in enumerate(args):
            if a == "--label" and idx + 1 < len(args):
                key, value = args[idx + 1].split("=", 1)
                foreign_labels[key] = value
            if a == "--name" and idx + 1 < len(args):
                c_name = "/" + args[idx + 1].lstrip("/")
            if a == "--cidfile" and idx + 1 < len(args):
                Path(args[idx + 1]).write_text(foreign_id, encoding="utf-8")
            if a == "-v" and idx + 1 < len(args):
                p = args[idx + 1].split(":")
                foreign_mounts.append({{
                    "Type": "bind",
                    "Source": str(Path(p[0]).resolve()),
                    "Destination": p[1],
                    "RW": p[2] != "ro" if len(p) > 2 else True,
                }})
        foreign_labels["org.node-plane.deployment"] = "00000000-0000-0000-0000-000000000000"
        state["containers"][foreign_id] = {{
            "Id": foreign_id,
            "Name": c_name,
            "Mounts": foreign_mounts,
            "Config": {{"Labels": foreign_labels}},
            "State": {{"Status": "running", "ExitCode": 0}},
        }}
        save_state(state)
        sys.stderr.write("Simulated foreign container run failure\\n")
        sys.exit(1)

    if state.get("fail_run"):
        sys.stderr.write("Simulated docker run failure\\n")
        sys.exit(1)

    name = None
    cidfile = None
    labels = {{}}
    mounts = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--name" and i + 1 < len(args):
            name = args[i + 1]
            i += 2
        elif arg == "--cidfile" and i + 1 < len(args):
            cidfile = args[i + 1]
            i += 2
        elif arg == "--label" and i + 1 < len(args):
            raw = args[i + 1]
            if "=" in raw:
                k, v = raw.split("=", 1)
                labels[k] = v
            i += 2
        elif arg == "-v" and i + 1 < len(args):
            raw = args[i + 1]
            parts = raw.split(":")
            if len(parts) >= 2:
                src, dst = parts[0], parts[1]
                mode = parts[2] if len(parts) > 2 else "rw"
                mounts.append({{
                    "Type": "bind",
                    "Source": str(Path(src).resolve()),
                    "Destination": dst,
                    "RW": (mode != "ro"),
                }})
            i += 2
        else:
            i += 1

    if any(c["Name"] == "/" + (name or "unnamed").lstrip("/") for c in state["containers"].values()):
        sys.stderr.write("Container name is already in use\\n")
        save_state(state)
        sys.exit(1)

    new_id = secrets.token_hex(32)

    if state.get("on_run_wrong_token"):
        labels["org.node-plane.deployment"] = "00000000-0000-0000-0000-000000000000"

    if state.get("on_run_corrupt_labels"):
        labels["org.node-plane.installation"] = "wrong-installation"

    if cidfile:
        Path(cidfile).write_text(new_id, encoding="utf-8")

    status = state.get("fail_run_status") or "running"
    state["containers"][new_id] = {{
        "Id": new_id,
        "Name": "/" + (name or "unnamed").lstrip("/"),
        "Mounts": mounts,
        "Config": {{"Labels": labels}},
        "State": {{
            "Status": status,
            "ExitCode": 0 if status == "running" else 1,
            "Error": "" if status == "running" else "failed to start",
            "OOMKilled": False,
            "StartedAt": "2026-10-31T00:00:00Z",
            "FinishedAt": "",
        }},
    }}
    save_state(state)
    sys.stdout.write(f"{{new_id}}\\n")
    sys.exit(0)

def _handle_rename(args, state):
    target = args[0]
    new_name = args[1]
    is_valid_id = bool(HEX64.fullmatch(target))
    state["mutations"].append({{"cmd": "rename", "target": target, "new_name": new_name, "valid_id": is_valid_id}})

    if not is_valid_id:
        sys.stderr.write(f"Assertion Error: rename target '{{target}}' is not an immutable 64-hex ID\\n")
        save_state(state)
        sys.exit(2)

    if target not in state["containers"]:
        sys.stderr.write(f"Error: No such container: {{target}}\\n")
        save_state(state)
        sys.exit(1)

    # Check for name collision
    for c in state["containers"].values():
        if c["Name"] == "/" + new_name.lstrip("/") and c["Id"] != target:
            sys.stderr.write(f"Error response from daemon: Conflict. The container name \\"/{{new_name}}\\" is already in use by container \\"{{c['Id']}}\\".\\n")
            save_state(state)
            sys.exit(1)

    state["containers"][target]["Name"] = "/" + new_name.lstrip("/")

    # Inject name collision hook if configured
    if state.get("on_rename_inject_collision"):
        collision_name = state["on_rename_inject_collision"]
        cid = secrets.token_hex(32)
        state["containers"][cid] = {{
            "Id": cid,
            "Name": "/" + collision_name.lstrip("/"),
            "Mounts": [],
            "Config": {{"Labels": {{}}}},
            "State": {{"Status": "running", "ExitCode": 0}},
        }}

    save_state(state)
    sys.exit(0)

def _handle_stop(args, state):
    target = args[0]
    is_valid_id = bool(HEX64.fullmatch(target))
    state["mutations"].append({{"cmd": "stop", "target": target, "valid_id": is_valid_id}})

    if not is_valid_id:
        sys.stderr.write(f"Assertion Error: stop target '{{target}}' is not an immutable 64-hex ID\\n")
        save_state(state)
        sys.exit(2)

    if target not in state["containers"]:
        sys.stderr.write(f"Error: No such container: {{target}}\\n")
        save_state(state)
        sys.exit(1)

    state["containers"][target]["State"]["Status"] = "exited"
    save_state(state)
    sys.exit(0)

def _handle_start(args, state):
    target = args[0]
    is_valid_id = bool(HEX64.fullmatch(target))
    state["mutations"].append({{"cmd": "start", "target": target, "valid_id": is_valid_id}})

    if not is_valid_id:
        sys.stderr.write(f"Assertion Error: start target '{{target}}' is not an immutable 64-hex ID\\n")
        save_state(state)
        sys.exit(2)

    if target not in state["containers"]:
        sys.stderr.write(f"Error: No such container: {{target}}\\n")
        save_state(state)
        sys.exit(1)

    if state.get("fail_start"):
        sys.stderr.write("Simulated docker start failure\\n")
        save_state(state)
        sys.exit(1)

    state["containers"][target]["State"]["Status"] = "running"
    save_state(state)
    sys.exit(0)

def _handle_rm(args, state):
    target = args[-1]
    is_valid_id = bool(HEX64.fullmatch(target))
    state["mutations"].append({{"cmd": "rm", "target": target, "valid_id": is_valid_id}})

    if not is_valid_id:
        sys.stderr.write(f"Assertion Error: rm target '{{target}}' is not an immutable 64-hex ID\\n")
        save_state(state)
        sys.exit(2)

    if target in state["containers"]:
        del state["containers"][target]

    save_state(state)
    sys.exit(0)

def _handle_update(args, state):
    target = args[-1]
    is_valid_id = bool(HEX64.fullmatch(target))
    state["mutations"].append({{"cmd": "update", "target": target, "valid_id": is_valid_id}})

    if not is_valid_id:
        sys.stderr.write(f"Assertion Error: update target '{{target}}' is not an immutable 64-hex ID\\n")
        save_state(state)
        sys.exit(2)

    save_state(state)
    sys.exit(0)

def _handle_logs(args, state):
    target = args[-1]
    is_valid_id = bool(HEX64.fullmatch(target))
    state["mutations"].append({{"cmd": "logs", "target": target, "valid_id": is_valid_id}})
    sys.stderr.write("Simulated container log output\\n")
    sys.exit(0)

if __name__ == "__main__":
    main()
"""
        docker_bin = self.bin_dir / "docker"
        docker_bin.write_text(docker_code, encoding="utf-8")
        docker_bin.chmod(0o755)

        # Fake sleep (fast exit)
        sleep_bin = self.bin_dir / "sleep"
        sleep_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        sleep_bin.chmod(0o755)

        # Fake lsmod (no amneziawg loaded)
        lsmod_bin = self.bin_dir / "lsmod"
        lsmod_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        lsmod_bin.chmod(0o755)

        # Host service and sudo guard to fail fast on unauthorized attempts
        for tool in ("sudo", "systemctl", "service"):
            p = self.bin_dir / tool
            p.write_text(f"#!/bin/sh\necho 'Blocked host privileged call: {tool} $*' >&2\nexit 99\n", encoding="utf-8")
            p.chmod(0o755)

    def add_container(
        self,
        name: str,
        mount_source: str,
        mount_destination: str,
        rw: bool = True,
        labels: dict = None,
        container_id: str = None,
        status: str = "running",
    ) -> str:
        cid = container_id or secrets.token_hex(32)
        state = self._load_state()
        state["containers"][cid] = {
            "Id": cid,
            "Name": "/" + name.lstrip("/"),
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(pathlib.Path(mount_source).resolve()),
                    "Destination": mount_destination,
                    "RW": rw,
                }
            ],
            "Config": {"Labels": labels or {}},
            "State": {
                "Status": status,
                "ExitCode": 0 if status == "running" else 1,
                "Error": "" if status == "running" else "stopped",
                "OOMKilled": False,
                "StartedAt": "2026-10-31T00:00:00Z",
                "FinishedAt": "",
            },
        }
        self._save_state(state)
        return cid

    def get_container(self, container_id: str):
        state = self._load_state()
        return state["containers"].get(container_id)

    def get_containers(self):
        state = self._load_state()
        return state["containers"]

    def get_mutations(self):
        state = self._load_state()
        return state["mutations"]

    def set_fail_run(self, val: bool):
        s = self._load_state()
        s["fail_run"] = val
        self._save_state(s)

    def set_fail_run_status(self, status: str):
        s = self._load_state()
        s["fail_run_status"] = status
        self._save_state(s)

    def set_fail_start(self, val: bool):
        s = self._load_state()
        s["fail_start"] = val
        self._save_state(s)

    def set_fail_ps(self, val: bool):
        s = self._load_state()
        s["fail_ps"] = val
        self._save_state(s)

    def set_corrupt_ps(self, val: bool):
        s = self._load_state()
        s["corrupt_ps"] = val
        self._save_state(s)

    def set_fail_inspect(self, val: bool):
        s = self._load_state()
        s["fail_inspect"] = val
        self._save_state(s)

    def set_corrupt_inspect(self, val: bool):
        s = self._load_state()
        s["corrupt_inspect"] = val
        self._save_state(s)

    def set_on_rename_inject_collision(self, name: str):
        s = self._load_state()
        s["on_rename_inject_collision"] = name
        self._save_state(s)

    def set_on_run_wrong_token(self, val: bool):
        s = self._load_state()
        s["on_run_wrong_token"] = val
        self._save_state(s)

    def set_on_run_write_foreign_cid_and_fail(self, val: bool):
        s = self._load_state()
        s["on_run_write_foreign_cid_and_fail"] = val
        self._save_state(s)

    def set_on_run_corrupt_labels(self, val: bool):
        s = self._load_state()
        s["on_run_corrupt_labels"] = val
        self._save_state(s)


def prepare_deployment_env(root: pathlib.Path, protocol: str, fake_docker: FakeDocker) -> dict:
    """Prepare private tempdir deployment scripts, guard, node.env, and markers."""
    installation_id = str(uuid.uuid4())
    installation_file = root / "controller-installation-id"
    installation_file.write_text(f"{installation_id}\n", encoding="utf-8")

    server_key = "test-node-key"
    node_env = root / "node.env"
    # The agent finds the marker, the runtime and the TUN device here, and
    # does not wait for containers to settle.
    common = (
        f"NODE_PLANE_INSTALLATION_MARKER={shlex.quote(str(installation_file))}\n"
        f"NODE_PLANE_RUNTIME_ROOT={shlex.quote(str(root))}\n"
        "NODE_PLANE_DEPLOY_SETTLE_SECONDS=0\n"
        "AWG_TUN_DEVICE=/dev/null\n"
        f"AWG_PROFILE_TOOL={shlex.quote(str(ASSETS / 'awg_profile.py'))}\n"
    )

    if protocol == "xray":
        docker_dir = root / "xray"
        docker_dir.mkdir(parents=True, exist_ok=True)
        config_file = docker_dir / "config.json"
        config_file.write_text('{"log": {"loglevel": "warning"}}\n', encoding="utf-8")
        config_file.chmod(0o600)
        container_name = "xray"
        mount_source = str(docker_dir)
        mount_dest = "/etc/xray"
        node_env.write_text(
            common +
            f"SERVER_KEY={server_key}\n"
            f"XRAY_DOCKER_DIR={shlex.quote(str(docker_dir))}\n"
            f"XRAY_CONFIG={shlex.quote(str(config_file))}\n"
            f"XRAY_CONTAINER_NAME={container_name}\n"
            "XRAY_DOCKER_IMAGE=ghcr.io/xtls/xray-core:test\n"
            "IPV6_OUTBOUND=off\n",
            encoding="utf-8",
        )
    elif protocol == "awg":
        docker_dir = root / "amnezia-awg"
        data_dir = docker_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        config_file = data_dir / "wg0.conf"
        # Initialize valid AWG config using awg_profile.py
        profile_out = subprocess.check_output(
            ["python3", str(ASSETS / "awg_profile.py"), "init", str(config_file)], text=True, timeout=10
        )
        original_config = "[Interface]\nPrivateKey = oldserver\nAddress = 10.8.1.1/24\nListenPort = 51820\n" + profile_out + "\n"
        config_file.write_text(original_config, encoding="utf-8")
        config_file.chmod(0o600)
        container_name = "amnezia-awg"
        mount_source = str(data_dir)
        mount_dest = "/opt/amnezia/awg"
        node_env.write_text(
            common +
            f"SERVER_KEY={server_key}\n"
            f"AWG_DOCKER_DIR={shlex.quote(str(docker_dir))}\n"
            f"AWG_CONFIG={shlex.quote(str(config_file))}\n"
            f"AWG_CONTAINER_NAME={container_name}\n"
            "AWG_DOCKER_IMAGE=node-plane-amnezia-awg:3.1.20260828\n"
            "IPV6_OUTBOUND=off\n",
            encoding="utf-8",
        )
    else:
        raise ValueError(f"Unknown protocol: {protocol}")

    script_dst = root / f"deploy-{protocol}.sh"
    script_content = (ASSETS / script_dst.name).read_text(encoding="utf-8").replace(
        "source /etc/node-plane/node.env", f"source {shlex.quote(str(node_env))}")
    script_dst.write_text(script_content, encoding="utf-8")
    script_dst.chmod(0o755)

    return {
        "root": root,
        "protocol": protocol,
        "script": script_dst,
        "env_file": node_env,
        "installation_id": installation_id,
        "installation_file": installation_file,
        "server_key": server_key,
        "container_name": container_name,
        "mount_source": mount_source,
        "mount_dest": mount_dest,
        "config_file": config_file,
        "fake_docker": fake_docker,
        "bin_dir": fake_docker.bin_dir,
    }


def execute_deploy(dep_env: dict, timeout: int = 15) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PATH"] = f"{dep_env['bin_dir']}:{env['PATH']}"
    env["TMPDIR"] = str(dep_env["root"])
    env["NODE_PLANE_AGENT_BIN"] = str(AGENT_BINARY)
    return subprocess.run(
        ["bash", str(dep_env["script"])],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@unittest.skipUnless(os.environ.get("NODE_PLANE_TEST_DOCKER") == "1", "requires the Docker suite")
class ContainerDeployOwnershipTests(unittest.TestCase):
    def test_first_deployment_records_owned_container_without_previous_instance(self):
        for protocol in ('xray', 'awg'):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                docker = FakeDocker(root)
                dep = prepare_deployment_env(root, protocol, docker)
                result = execute_deploy(dep)
                self.assertEqual(result.returncode, 0, result.stderr)
                containers = list(docker.get_containers().values())
                self.assertEqual(len(containers), 1)
                self.assertEqual(containers[0]['State']['Status'], 'running')
                labels = containers[0]['Config']['Labels']
                self.assertEqual(labels['org.node-plane.installation'], dep['installation_id'])
                self.assertEqual(labels['org.node-plane.node'], dep['server_key'])
                self.assertEqual(labels['org.node-plane.service'], protocol)
                self.assertEqual(str(uuid.UUID(labels['org.node-plane.deployment'])), labels['org.node-plane.deployment'])
                self.assertFalse(any(item['cmd'] in {'rename', 'stop', 'rm'} for item in docker.get_mutations()))

    def test_missing_installation_marker_preserves_existing_container(self):
        for protocol in ('xray', 'awg'):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                docker = FakeDocker(root)
                dep = prepare_deployment_env(root, protocol, docker)
                identity = docker.add_container(dep['container_name'], dep['mount_source'],
                                                dep['mount_dest'], rw=protocol == 'awg')
                dep['installation_file'].unlink()
                result = execute_deploy(dep)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(docker.get_mutations(), [])
                self.assertEqual(docker.get_container(identity)['State']['Status'], 'running')

    def test_xray_deployment_success_removes_old_retains_new(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "xray", fake_docker)

            old_labels = {
                "org.node-plane.managed": "1",
                "org.node-plane.installation": dep["installation_id"],
                "org.node-plane.node": dep["server_key"],
                "org.node-plane.service": "xray",
            }
            old_id = fake_docker.add_container(
                dep["container_name"], dep["mount_source"], dep["mount_dest"], rw=False, labels=old_labels
            )

            res = execute_deploy(dep)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("Xray container deployed", res.stdout)

            # Old container removed, new container exists and is running
            containers = fake_docker.get_containers()
            self.assertNotIn(old_id, containers)
            self.assertEqual(len(containers), 1)
            new_id, new_record = list(containers.items())[0]
            self.assertNotEqual(new_id, old_id)
            self.assertEqual(new_record["Name"], "/" + dep["container_name"])
            self.assertEqual(new_record["State"]["Status"], "running")

            # Assert all mutations used immutable 64-hex IDs
            mutations = fake_docker.get_mutations()
            self.assertTrue(len(mutations) > 0)
            for m in mutations:
                self.assertTrue(m["valid_id"], f"Mutation argument was not 64-hex ID: {m}")

    def test_awg_deployment_success_removes_old_retains_new(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "awg", fake_docker)

            old_labels = {
                "org.node-plane.managed": "1",
                "org.node-plane.installation": dep["installation_id"],
                "org.node-plane.node": dep["server_key"],
                "org.node-plane.service": "awg",
            }
            old_id = fake_docker.add_container(
                dep["container_name"], dep["mount_source"], dep["mount_dest"], rw=True, labels=old_labels
            )

            res = execute_deploy(dep)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("AWG container deployed", res.stdout)

            containers = fake_docker.get_containers()
            self.assertNotIn(old_id, containers)
            self.assertEqual(len(containers), 1)
            new_id, new_record = list(containers.items())[0]
            self.assertNotEqual(new_id, old_id)
            self.assertEqual(new_record["Name"], "/" + dep["container_name"])
            self.assertEqual(new_record["State"]["Status"], "running")

            mutations = fake_docker.get_mutations()
            self.assertTrue(len(mutations) > 0)
            for m in mutations:
                self.assertTrue(m["valid_id"], f"Mutation argument was not 64-hex ID: {m}")

    def test_xray_legacy_unlabelled_exact_bind_mount_replaced_safely(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "xray", fake_docker)

            # Pre-label container: no org.node-plane labels, but exact ro bind mount
            old_id = fake_docker.add_container(
                dep["container_name"], dep["mount_source"], dep["mount_dest"], rw=False, labels={}
            )

            res = execute_deploy(dep)
            self.assertEqual(res.returncode, 0, res.stderr)

            containers = fake_docker.get_containers()
            self.assertNotIn(old_id, containers)
            self.assertEqual(len(containers), 1)
            new_id, new_record = list(containers.items())[0]
            self.assertEqual(new_record["Config"]["Labels"]["org.node-plane.managed"], "1")
            self.assertEqual(new_record["Config"]["Labels"]["org.node-plane.installation"], dep["installation_id"])

    def test_awg_legacy_unlabelled_exact_bind_mount_replaced_safely(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "awg", fake_docker)

            old_id = fake_docker.add_container(
                dep["container_name"], dep["mount_source"], dep["mount_dest"], rw=True, labels={}
            )

            res = execute_deploy(dep)
            self.assertEqual(res.returncode, 0, res.stderr)

            containers = fake_docker.get_containers()
            self.assertNotIn(old_id, containers)
            self.assertEqual(len(containers), 1)
            new_id, new_record = list(containers.items())[0]
            self.assertEqual(new_record["Config"]["Labels"]["org.node-plane.managed"], "1")

    def test_xray_foreign_mount_refuses_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "xray", fake_docker)

            foreign_dir = root / "foreign_data"
            foreign_dir.mkdir()
            old_labels = {
                "org.node-plane.managed": "1",
                "org.node-plane.installation": dep["installation_id"],
                "org.node-plane.node": dep["server_key"],
                "org.node-plane.service": "xray",
            }
            # Wrong mount source
            old_id = fake_docker.add_container(
                dep["container_name"], str(foreign_dir), dep["mount_dest"], rw=False, labels=old_labels
            )

            res = execute_deploy(dep)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Container mount ownership could not be verified", res.stderr)

            # Container must NOT be renamed, stopped, or removed
            mutations = fake_docker.get_mutations()
            self.assertEqual(mutations, [])
            c = fake_docker.get_container(old_id)
            self.assertEqual(c["Name"], "/" + dep["container_name"])
            self.assertEqual(c["State"]["Status"], "running")

    def test_xray_writable_mount_refuses_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "xray", fake_docker)

            old_labels = {
                "org.node-plane.managed": "1",
                "org.node-plane.installation": dep["installation_id"],
                "org.node-plane.node": dep["server_key"],
                "org.node-plane.service": "xray",
            }
            # Xray mount must be read-only (RW=False). If RW=True, guard refuses
            old_id = fake_docker.add_container(
                dep["container_name"], dep["mount_source"], dep["mount_dest"], rw=True, labels=old_labels
            )

            res = execute_deploy(dep)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Container mount ownership could not be verified", res.stderr)
            self.assertEqual(fake_docker.get_mutations(), [])

    def test_awg_foreign_mount_refuses_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "awg", fake_docker)

            foreign_dir = root / "foreign_awg"
            foreign_dir.mkdir()
            old_labels = {
                "org.node-plane.managed": "1",
                "org.node-plane.installation": dep["installation_id"],
                "org.node-plane.node": dep["server_key"],
                "org.node-plane.service": "awg",
            }
            old_id = fake_docker.add_container(
                dep["container_name"], str(foreign_dir), dep["mount_dest"], rw=True, labels=old_labels
            )

            res = execute_deploy(dep)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Container mount ownership could not be verified", res.stderr)
            self.assertEqual(fake_docker.get_mutations(), [])

    def test_xray_mismatched_and_partial_labels_refuse_before_mutation(self):
        # Partial labels (tagged with org.node-plane but missing installation)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "xray", fake_docker)

            fake_docker.add_container(
                dep["container_name"],
                dep["mount_source"],
                dep["mount_dest"],
                rw=False,
                labels={"org.node-plane.managed": "1"},
            )
            res = execute_deploy(dep)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Container installation ownership could not be verified", res.stderr)
            self.assertEqual(fake_docker.get_mutations(), [])

        # Mismatched installation UUID
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "xray", fake_docker)

            fake_docker.add_container(
                dep["container_name"],
                dep["mount_source"],
                dep["mount_dest"],
                rw=False,
                labels={
                    "org.node-plane.managed": "1",
                    "org.node-plane.installation": str(uuid.uuid4()),
                    "org.node-plane.node": dep["server_key"],
                    "org.node-plane.service": "xray",
                },
            )
            res = execute_deploy(dep)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Container installation ownership could not be verified", res.stderr)
            self.assertEqual(fake_docker.get_mutations(), [])

    def test_awg_mismatched_and_partial_labels_refuse_before_mutation(self):
        # Mismatched node key
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "awg", fake_docker)

            fake_docker.add_container(
                dep["container_name"],
                dep["mount_source"],
                dep["mount_dest"],
                rw=True,
                labels={
                    "org.node-plane.managed": "1",
                    "org.node-plane.installation": dep["installation_id"],
                    "org.node-plane.node": "different-node-key",
                    "org.node-plane.service": "awg",
                },
            )
            res = execute_deploy(dep)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Container installation ownership could not be verified", res.stderr)
            self.assertEqual(fake_docker.get_mutations(), [])

    def test_xray_failed_start_restores_old_config_and_container(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "xray", fake_docker)

            old_labels = {
                "org.node-plane.managed": "1",
                "org.node-plane.installation": dep["installation_id"],
                "org.node-plane.node": dep["server_key"],
                "org.node-plane.service": "xray",
            }
            old_id = fake_docker.add_container(
                dep["container_name"], dep["mount_source"], dep["mount_dest"], rw=False, labels=old_labels
            )

            # Trigger container failure
            fake_docker.set_fail_run_status("exited")

            res = execute_deploy(dep)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("rollback completed", res.stderr)

            # Old container restored and restarted by its 64-hex ID
            c = fake_docker.get_container(old_id)
            self.assertIsNotNone(c)
            self.assertEqual(c["Name"], "/" + dep["container_name"])
            self.assertEqual(c["State"]["Status"], "running")

            mutations = fake_docker.get_mutations()
            for m in mutations:
                self.assertTrue(m["valid_id"], f"Mutation target was not 64-hex ID: {m}")
            # Ensure rename back and start occurred
            self.assertTrue(any(m["cmd"] == "rename" and m["new_name"] == dep["container_name"] for m in mutations))
            self.assertTrue(any(m["cmd"] == "start" and m["target"] == old_id for m in mutations))

    def test_awg_failed_start_restores_old_config_and_container(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "awg", fake_docker)
            original_config = dep["config_file"].read_text(encoding="utf-8")

            old_labels = {
                "org.node-plane.managed": "1",
                "org.node-plane.installation": dep["installation_id"],
                "org.node-plane.node": dep["server_key"],
                "org.node-plane.service": "awg",
            }
            old_id = fake_docker.add_container(
                dep["container_name"], dep["mount_source"], dep["mount_dest"], rw=True, labels=old_labels
            )

            fake_docker.set_fail_run_status("exited")

            res = execute_deploy(dep)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("previous config and container restored", res.stderr)

            # Config file restored to original
            self.assertEqual(dep["config_file"].read_text(encoding="utf-8"), original_config)

            c = fake_docker.get_container(old_id)
            self.assertIsNotNone(c)
            self.assertEqual(c["Name"], "/" + dep["container_name"])
            self.assertEqual(c["State"]["Status"], "running")

            mutations = fake_docker.get_mutations()
            for m in mutations:
                self.assertTrue(m["valid_id"])
            self.assertTrue(any(m["cmd"] == "rename" and m["new_name"] == dep["container_name"] for m in mutations))
            self.assertTrue(any(m["cmd"] == "start" and m["target"] == old_id for m in mutations))

    def test_name_collision_after_preflight_blocks_rollback_without_touching_collision(self):
        # Protocol: xray
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "xray", fake_docker)

            old_labels = {
                "org.node-plane.managed": "1",
                "org.node-plane.installation": dep["installation_id"],
                "org.node-plane.node": dep["server_key"],
                "org.node-plane.service": "xray",
            }
            fake_docker.add_container(
                dep["container_name"], dep["mount_source"], dep["mount_dest"], rw=False, labels=old_labels
            )

            # Injected rogue container with name 'xray' when preflight rename happens
            fake_docker.set_on_rename_inject_collision(dep["container_name"])
            fake_docker.set_fail_run_status("exited")

            res = execute_deploy(dep)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("rollback could not be confirmed, recovery required", res.stderr)
            self.assertNotIn("rollback completed", res.stderr)

            # Confirm colliding container was NOT stopped or removed
            containers = fake_docker.get_containers()
            colliding = [c for c in containers.values() if c["Name"] == "/" + dep["container_name"]]
            self.assertEqual(len(colliding), 1)
            self.assertEqual(colliding[0]["State"]["Status"], "running")

            # Check mutations: rm/stop was not called on the colliding container ID
            colliding_id = colliding[0]["Id"]
            mutations = fake_docker.get_mutations()
            self.assertFalse(any(m["target"] == colliding_id for m in mutations))

        # Protocol: awg
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "awg", fake_docker)

            old_labels = {
                "org.node-plane.managed": "1",
                "org.node-plane.installation": dep["installation_id"],
                "org.node-plane.node": dep["server_key"],
                "org.node-plane.service": "awg",
            }
            fake_docker.add_container(
                dep["container_name"], dep["mount_source"], dep["mount_dest"], rw=True, labels=old_labels
            )

            fake_docker.set_on_rename_inject_collision(dep["container_name"])
            fake_docker.set_fail_run_status("exited")

            res = execute_deploy(dep)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("previous config restored, but the container needs attention", res.stderr)
            self.assertNotIn("previous config and container restored", res.stderr)

            containers = fake_docker.get_containers()
            colliding = [c for c in containers.values() if c["Name"] == "/" + dep["container_name"]]
            self.assertEqual(len(colliding), 1)
            self.assertEqual(colliding[0]["State"]["Status"], "running")

    def test_foreign_new_name_replacement_during_failure_never_removed(self):
        # Protocol: xray
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "xray", fake_docker)

            old_labels = {
                "org.node-plane.managed": "1",
                "org.node-plane.installation": dep["installation_id"],
                "org.node-plane.node": dep["server_key"],
                "org.node-plane.service": "xray",
            }
            fake_docker.add_container(
                dep["container_name"], dep["mount_source"], dep["mount_dest"], rw=False, labels=old_labels
            )

            # When container run is executed, simulate wrong deployment token written to cidfile and run failure
            fake_docker.set_on_run_write_foreign_cid_and_fail(True)

            res = execute_deploy(dep)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("rollback could not be confirmed, recovery required", res.stderr)

            # Foreign new container must NOT have been removed by docker rm
            mutations = fake_docker.get_mutations()
            # Find the foreign container ID
            containers = fake_docker.get_containers()
            foreign_containers = [c for c in containers.values() if c["Config"]["Labels"].get("org.node-plane.deployment") == "00000000-0000-0000-0000-000000000000"]
            self.assertEqual(len(foreign_containers), 1)
            foreign_id = foreign_containers[0]["Id"]
            self.assertFalse(any(m["cmd"] == "rm" and m["target"] == foreign_id for m in mutations))

        # Protocol: awg
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "awg", fake_docker)

            old_labels = {
                "org.node-plane.managed": "1",
                "org.node-plane.installation": dep["installation_id"],
                "org.node-plane.node": dep["server_key"],
                "org.node-plane.service": "awg",
            }
            fake_docker.add_container(
                dep["container_name"], dep["mount_source"], dep["mount_dest"], rw=True, labels=old_labels
            )

            fake_docker.set_on_run_write_foreign_cid_and_fail(True)

            res = execute_deploy(dep)
            self.assertNotEqual(res.returncode, 0)
            self.assertTrue(
                "Container belongs to another deployment" in res.stderr
                or "Container installation ownership could not be verified" in res.stderr
            )
            self.assertNotIn("previous config and container restored", res.stderr)

            # Foreign container must not be removed
            containers = fake_docker.get_containers()
            foreign_containers = [c for c in containers.values() if c["Config"]["Labels"].get("org.node-plane.deployment") == "00000000-0000-0000-0000-000000000000"]
            self.assertEqual(len(foreign_containers), 1)
            foreign_id = foreign_containers[0]["Id"]
            mutations = fake_docker.get_mutations()
            self.assertFalse(any(m["cmd"] == "rm" and m["target"] == foreign_id for m in mutations))

    def test_inventory_and_inspect_fail_closed(self):
        # docker ps failure fails closed
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "xray", fake_docker)
            fake_docker.set_fail_ps(True)
            res = execute_deploy(dep)
            self.assertNotEqual(res.returncode, 0)
            self.assertEqual(fake_docker.get_mutations(), [])

        # docker ps corrupt format fails closed
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "xray", fake_docker)
            fake_docker.set_corrupt_ps(True)
            res = execute_deploy(dep)
            self.assertNotEqual(res.returncode, 0)
            self.assertEqual(fake_docker.get_mutations(), [])

        # docker inspect failure fails closed
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "awg", fake_docker)
            fake_docker.add_container(
                dep["container_name"], dep["mount_source"], dep["mount_dest"], rw=True, labels={}
            )
            fake_docker.set_fail_inspect(True)
            res = execute_deploy(dep)
            self.assertNotEqual(res.returncode, 0)
            self.assertEqual(fake_docker.get_mutations(), [])

        # docker inspect corrupt json fails closed
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_docker = FakeDocker(root)
            dep = prepare_deployment_env(root, "awg", fake_docker)
            fake_docker.add_container(
                dep["container_name"], dep["mount_source"], dep["mount_dest"], rw=True, labels={}
            )
            fake_docker.set_corrupt_inspect(True)
            res = execute_deploy(dep)
            self.assertNotEqual(res.returncode, 0)
            self.assertEqual(fake_docker.get_mutations(), [])


if __name__ == "__main__":
    unittest.main()
