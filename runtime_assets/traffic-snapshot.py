"""Read one consenting profile's cumulative counters; never reset counters.

The caller holds the node-wide shared mutation lock. Docker/protocol outputs
stay local; only the requested identity's counters and an opaque epoch leave
the agent. A container restart during the read makes the observation unknown.
"""

import base64
import hashlib
import json
import re
import subprocess
from pathlib import Path
from uuid import UUID

MAX_COUNTER = 2**63 - 1


def command(args):
    return subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=4
    ).stdout.strip()


def environment():
    defaults = {
        "XRAY_CONFIG": "/opt/node-plane-runtime/xray/config.json",
        "AWG_CONFIG": "/opt/node-plane-runtime/amnezia-awg/data/wg0.conf",
        "XRAY_CONTAINER_NAME": "xray",
        "AWG_CONTAINER_NAME": "amnezia-awg",
        "AWG_IFACE": "wg0",
        "SERVER_KEY": "",
        "XRAY_INBOUND_TCP_TAG": "reality-tcp",
        "XRAY_INBOUND_XHTTP_TAG": "reality-xhttp",
    }
    values = command(
        [
            "bash",
            "-c",
            'source /etc/node-plane/node.env; printf "%s\\n" '
            + " ".join(
                '"${' + key + ":-" + value + '}"' for key, value in defaults.items()
            ),
        ]
    ).split("\n")
    if len(values) != len(defaults):
        raise ValueError("invalid node environment")
    return dict(zip(defaults, values))


def counter(value):
    # Protobuf JSON represents int64 as decimal strings; booleans are invalid.
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise ValueError("invalid traffic counter")
    return value


def container_epoch(container):
    record = json.loads(command(["docker", "inspect", container]))
    if len(record) != 1 or record[0]["State"]["Running"] is not True:
        raise ValueError("protocol container unavailable")
    state = record[0]
    return hashlib.sha256(
        json.dumps(
            [
                Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                state["Id"],
                state["State"]["StartedAt"],
                state["State"]["Pid"],
            ]
        ).encode()
    ).hexdigest()


def fields(text):
    return {
        key.strip(): value.strip()
        for key, value in (
            line.split("=", 1) for line in text.splitlines() if "=" in line
        )
    }


def xray(env, name, identity):
    cfg = json.loads(Path(env["XRAY_CONFIG"]).read_text())
    if not isinstance(cfg.get("stats"), dict) or "StatsService" not in cfg.get(
        "api", {}
    ).get("services", []):
        raise ValueError("Xray statistics unavailable")
    tags = (env["XRAY_INBOUND_TCP_TAG"], env["XRAY_INBOUND_XHTTP_TAG"])
    if len(set(tags)) != 2:
        raise ValueError("invalid Xray inbounds")
    for tag in tags:
        inbounds = [item for item in cfg["inbounds"] if item.get("tag") == tag]
        if len(inbounds) != 1:
            raise ValueError("missing Xray inbound")
        users = [
            item
            for item in inbounds[0]["settings"]["clients"]
            if item.get("email") == name
        ]
        if len(users) != 1 or users[0].get("id") != identity:
            raise ValueError("Xray identity changed")
        level = str(users[0].get("level", 0))
        policy = cfg.get("policy", {}).get("levels", {}).get(level, {})
        if any(
            policy.get(key) is not True
            for key in ("statsUserUplink", "statsUserDownlink")
        ):
            raise ValueError("Xray user statistics unavailable")
    raw = None
    for binary in ("xray", "/usr/local/bin/xray", "/usr/bin/xray"):
        try:
            raw = command(
                [
                    "docker",
                    "exec",
                    env["XRAY_CONTAINER_NAME"],
                    binary,
                    "api",
                    "statsquery",
                    "--server=127.0.0.1:10085",
                    "-timeout=2",
                    "-reset=false",
                    "-pattern=user>>>" + name + ">>>",
                ]
            )
            break
        except subprocess.CalledProcessError:
            continue
    if raw is None:
        raise ValueError("Xray statistics unavailable")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or set(payload) - {"stat"}:
        raise ValueError("invalid Xray statistics response")
    statistics = payload.get("stat", [])
    if not isinstance(statistics, list):
        raise TypeError("invalid Xray statistics response")
    result = {"uplink": 0, "downlink": 0}
    seen = set()
    for stat in statistics:
        match = re.fullmatch(
            "user>>>" + re.escape(name) + r">>>traffic>>>(uplink|downlink)",
            stat["name"],
        )
        if not match or match[1] in seen:
            raise ValueError("unexpected Xray statistics identity")
        seen.add(match[1])
        result[match[1]] = counter(stat.get("value", 0))
    return result


def awg(env, name, identity):
    text = Path(env["AWG_CONFIG"]).read_text()
    display = (env["SERVER_KEY"] + "-" if env["SERVER_KEY"] else "") + name
    lines = text.splitlines()
    indices = [i for i, line in enumerate(lines) if line.strip() == "# " + display]
    if len(indices) != 1:
        raise ValueError("AWG peer unavailable")
    start = indices[0] + 1
    end = start
    while end < len(lines) and lines[end].strip():
        end += 1
    if fields("\n".join(lines[start:end])).get("PublicKey") != identity:
        raise ValueError("AWG identity changed")
    interface = env["AWG_IFACE"]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,14}", interface):
        raise ValueError("invalid AWG interface")
    output = command(
        [
            "docker",
            "exec",
            env["AWG_CONTAINER_NAME"],
            "wg",
            "show",
            interface,
            "transfer",
        ]
    )
    matches = [
        line.split()
        for line in output.splitlines()
        if line.split() and line.split()[0] == identity
    ]
    if len(matches) != 1 or len(matches[0]) != 3:
        raise ValueError("AWG peer counters unavailable")
    # Server receive = client upload; server transmit = client download.
    return {"uplink": counter(matches[0][1]), "downlink": counter(matches[0][2])}


def read(intent):
    if (
        not isinstance(intent, dict)
        or set(intent) != {"node_key", "runtime_name", "protocol", "identity"}
        or not isinstance(intent["runtime_name"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", intent["runtime_name"])
        or not isinstance(intent["node_key"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", intent["node_key"])
    ):
        raise ValueError("invalid traffic query")
    protocol, identity = intent["protocol"], intent["identity"]
    if protocol == "xray":
        if str(UUID(identity)) != identity:
            raise ValueError("invalid Xray identity")
    elif protocol == "awg":
        if (
            not isinstance(identity, str)
            or len(base64.b64decode(identity, validate=True)) != 32
        ):
            raise ValueError("invalid AWG identity")
    else:
        raise ValueError("unsupported traffic protocol")
    env = environment()
    container = env[
        "XRAY_CONTAINER_NAME" if protocol == "xray" else "AWG_CONTAINER_NAME"
    ]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
        raise ValueError("invalid protocol container")
    epoch = container_epoch(container)
    counters = (xray if protocol == "xray" else awg)(
        env, intent["runtime_name"], identity
    )
    if container_epoch(container) != epoch:
        raise ValueError("protocol restarted during traffic observation")
    return {
        "node_key": intent["node_key"],
        "runtime_name": intent["runtime_name"],
        "protocol": protocol,
        "identity": identity,
        "epoch": epoch,
        "uplink_bytes": counters["uplink"],
        "downlink_bytes": counters["downlink"],
    }
