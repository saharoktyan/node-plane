#!/usr/bin/env python3
"""Read live Xray users for inspect-profile.py and traffic-snapshot.py.

Adding and removing users moved into the agent (`node-plane-agent
xray-user`, protocols/xray/user_sync.rs). This module goes away with its
two remaining readers.
"""

import functools
import json
import os
import subprocess


API = "127.0.0.1:10085"
AGENT = "/usr/local/bin/node-plane-agent"


def api_address():
    # Tests point helpers at a published API port; nodes use the default.
    return os.environ.get("NODE_PLANE_XRAY_API") or API


def agent():
    """The node agent binary when it serves `xray-api`, otherwise None.

    Agents from this release talk to the Xray gRPC API directly; older agents
    leave helpers on `docker exec <container> xray api`. Drop the fallback once
    no supported agent predates `xray-api` (VPN plan, section 10, item 5).
    """
    return _probe(os.environ.get("NODE_PLANE_AGENT_BIN") or AGENT)


def _probe(path):
    return path if _probe_version(path) >= 1 else None


@functools.lru_cache(maxsize=None)
def _probe_version(path):
    if not os.access(path, os.X_OK):
        return 0
    # An older agent ignores the arguments and starts its server. A missing
    # config and an unparsable listen address make it exit before binding.
    env = {**os.environ, "NODE_AGENT_CONFIG_PATH": "/nonexistent/node-plane-agent-probe.toml",
           "NODE_AGENT_LISTEN_ADDR": "probe"}
    try:
        result = subprocess.run([path, "xray-api", "capabilities"], env=env,
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return int(json.loads(result.stdout).get("xray_api", 0))
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        pass
    return 0


def agent_api(*args):
    result = subprocess.run([agent(), "xray-api", f"--server={api_address()}", *args],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "agent Xray API failed").strip())
    return json.loads(result.stdout)


def docker(*args):
    command = ["docker", *args]
    result = subprocess.run(command, capture_output=True, text=True)
    if (result.returncode and "permission denied while trying to connect to the docker api"
            in (result.stderr or "").lower() and shutil_which_sudo()):
        command = ["sudo", *command]
        result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "docker failed").strip())
    return result.stdout


def shutil_which_sudo():
    from shutil import which

    return which("sudo") is not None


def xray_api(container, *args):
    command, *parameters = args
    return docker("exec", container, "xray", "api", command, f"--server={API}", *parameters)


def users(container, tag):
    # Query the full list: a missing -email result is not represented reliably
    # across Xray versions.
    if agent():
        response = agent_api("inbound-users", f"--tag={tag}")
    else:
        response = json.loads(xray_api(container, "inbounduser", f"-tag={tag}"))
    return {
        user["email"]: {
            "id": (user.get("account") or {}).get("id"),
            "flow": (user.get("account") or {}).get("flow", ""),
        }
        for user in response.get("users", []) if user.get("email")
    }
