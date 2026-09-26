#!/usr/bin/env python3
"""Keep Xray's on-disk VLESS users and live HandlerService users in sync."""

import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid as uuid_module


API = "127.0.0.1:10085"


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
    raw = xray_api(container, "inbounduser", f"-tag={tag}")
    response = json.loads(raw)
    return {
        user["email"]: {
            "id": (user.get("account") or {}).get("id"),
            "flow": (user.get("account") or {}).get("flow", ""),
        }
        for user in response.get("users", []) if user.get("email")
    }


def check_result(output, verb):
    if not re.search(rf"\b{verb} 1 user\(s\) in total\.", output):
        raise RuntimeError(f"Xray API did not confirm {verb.lower()}: {output.strip()}")


def write_config(path, config):
    fd, candidate = tempfile.mkstemp(prefix=".xray-config-", suffix=".json", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(candidate, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(candidate):
            os.unlink(candidate)


def ensure_api(config_path, config, container):
    services = config.setdefault("api", {}).setdefault("services", [])
    mounts = docker("inspect", "-f", "{{range .Mounts}}{{println .Destination}}{{end}}", container)
    needs_redeploy = "HandlerService" not in services or "/etc/xray/config.json" in mounts.splitlines()
    if not needs_redeploy:
        return config
    if "HandlerService" not in services:
        services.append("HandlerService")
        write_config(config_path, config)
    subprocess.run(["/opt/node-plane-runtime/deploy-xray.sh"], check=True)
    return config


def one_user_config(inbound, client):
    # `xray api adu` builds this inbound config and extracts its users.
    return {"inbounds": [{
        "tag": inbound["tag"],
        "port": inbound["port"],
        "protocol": "vless",
        "settings": {"decryption": "none", "clients": [client]},
    }]}


def add_live(container, inbound, client, config_dir):
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=".xray-user-", suffix=".json", dir=config_dir
    ) as stream:
        json.dump(one_user_config(inbound, client), stream)
        stream.flush()
        # The directory is mounted read-only into Xray; the temporary file is
        # visible there without exposing another host path.
        output = xray_api(container, "adu", f"/etc/xray/{Path(stream.name).name}")
    check_result(output, "Added")


def remove_live(container, tag, name):
    output = xray_api(container, "rmu", f"-tag={tag}", name)
    check_result(output, "Removed")


def run(action, config_path, container, tcp_tag, xhttp_tag, name, desired_uuid=None):
    if not name or name.startswith("-"):
        raise ValueError("invalid profile name")
    if action == "add":
        desired_uuid = str(uuid_module.UUID(desired_uuid))
    lock_path = config_path.with_name(config_path.name + ".lock")
    with open(lock_path, "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        inbounds = {item.get("tag"): item for item in config.get("inbounds", [])}
        if tcp_tag not in inbounds or xhttp_tag not in inbounds:
            raise RuntimeError("Xray TCP/XHTTP inbound is missing")
        config = ensure_api(config_path, config, container)
        targets = (tcp_tag, xhttp_tag)
        live = {tag: users(container, tag).get(name) for tag in targets}
        old = {
            tag: next((item for item in inbounds[tag]["settings"]["clients"]
                       if item.get("email", item.get("name")) == name), None)
            for tag in targets
        }
        if action == "add":
            for tag in targets:
                inbound = inbounds[tag]
                client = {"id": desired_uuid, "name": name, "email": name}
                if tag == tcp_tag:
                    client["flow"] = "xtls-rprx-vision"
                clients = inbound["settings"]["clients"]
                clients[:] = [item for item in clients
                              if item.get("email", item.get("name")) != name]
                clients.append(client)
            # Persist before enabling access. A retry can finish a partial API
            # update; a container restart loads the same desired users.
            if any(old[tag] != next(item for item in inbounds[tag]["settings"]["clients"]
                                    if item.get("email") == name) for tag in targets):
                write_config(config_path, config)
            for tag in targets:
                current = next(item for item in inbounds[tag]["settings"]["clients"]
                               if item.get("email") == name)
                desired_live = {"id": desired_uuid, "flow": current.get("flow", "")}
                if live[tag] == desired_live:
                    continue
                if live[tag]:
                    remove_live(container, tag, name)
                add_live(container, inbounds[tag], current, config_path.parent)
                if users(container, tag).get(name) != desired_live:
                    raise RuntimeError(f"Xray user mismatch after add on {tag}")
        else:
            # Persist the revocation first. If Xray restarts before the API
            # calls finish, it must not restore access from the config file.
            changed = False
            for tag in targets:
                clients = inbounds[tag]["settings"]["clients"]
                remaining = [item for item in clients
                             if item.get("email", item.get("name")) != name]
                changed |= len(remaining) != len(clients)
                clients[:] = remaining
            if changed:
                write_config(config_path, config)
            for tag in targets:
                if live[tag]:
                    remove_live(container, tag, name)
                if name in users(container, tag):
                    raise RuntimeError(f"Xray user still present on {tag}")
        print("OK")


if __name__ == "__main__":
    try:
        command, file_name, container_name, tcp, xhttp, profile, *extra = sys.argv[1:]
        if command not in ("add", "delete") or len(extra) != (command == "add"):
            raise ValueError("invalid xray-user-api.py arguments")
        run(command, Path(file_name), container_name, tcp, xhttp, profile, *extra)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"xray user sync failed: {error}")
