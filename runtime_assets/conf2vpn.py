#!/usr/bin/env python3
import json
import re
import subprocess
import sys
from pathlib import Path


def parse_conf(text: str):
    cur = None
    data = {"Interface": {}, "Peer": {}}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\\[(Interface|Peer)\\]$", line, re.I)
        if m:
            cur = m.group(1).capitalize()
            continue
        if cur and "=" in line:
            k, v = map(str.strip, line.split("=", 1))
            data[cur][k] = v
    return data


def _split_csv(value: str):
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def main(conf_path, template_path, out_json_path, decoder_py, container_name="amnezia-awg", description="awg"):
    conf_text = Path(conf_path).read_text(encoding="utf-8", errors="ignore").strip() + "\\n"
    tpl = json.loads(Path(template_path).read_text(encoding="utf-8"))

    cfg = parse_conf(conf_text)
    iface = cfg["Interface"]
    peer = cfg["Peer"]

    client_ip = iface.get("Address", "").split("/", 1)[0]
    endpoint = peer.get("Endpoint", "")
    endpoint_host, endpoint_port = endpoint.rsplit(":", 1)
    allowed = _split_csv(peer.get("AllowedIPs", "")) or ["0.0.0.0/0", "::/0"]
    dns_list = _split_csv(iface.get("DNS", ""))
    dns1 = dns_list[0] if len(dns_list) >= 1 else "1.1.1.1"
    dns2 = dns_list[1] if len(dns_list) >= 2 else "1.0.0.1"
    subnet_address = iface.get("Address", "10.8.1.0/24").split("/", 1)[0].rsplit(".", 1)[0] + ".0"

    awg_obj = {
        "H1": iface.get("H1", ""),
        "H2": iface.get("H2", ""),
        "H3": iface.get("H3", ""),
        "H4": iface.get("H4", ""),
        "I1": iface.get("I1", ""),
        "I2": iface.get("I2", ""),
        "I3": iface.get("I3", ""),
        "I4": iface.get("I4", ""),
        "I5": iface.get("I5", ""),
        "Jc": iface.get("Jc", ""),
        "Jmax": iface.get("Jmax", ""),
        "Jmin": iface.get("Jmin", ""),
        "S1": iface.get("S1", ""),
        "S2": iface.get("S2", ""),
        "S3": iface.get("S3", ""),
        "S4": iface.get("S4", ""),
        "allowed_ips": allowed,
        "clientId": iface.get("PublicKey", ""),
        "client_ip": client_ip,
        "client_priv_key": iface.get("PrivateKey", ""),
        "client_pub_key": iface.get("PublicKey", ""),
        "config": conf_text,
        "hostName": endpoint_host,
        "mtu": iface.get("MTU", "1280"),
        "persistent_keep_alive": peer.get("PersistentKeepalive", "25"),
        "port": int(endpoint_port),
        "psk_key": peer.get("PresharedKey", ""),
        "server_pub_key": peer.get("PublicKey", ""),
    }

    out = tpl
    out["hostName"] = endpoint_host
    out["description"] = description
    out["dns1"] = dns1
    out["dns2"] = dns2
    out["defaultContainer"] = container_name
    out["containers"][0]["container"] = container_name
    out["containers"][0]["awg"]["port"] = str(awg_obj["port"])
    out["containers"][0]["awg"]["transport_proto"] = "udp"
    out["containers"][0]["awg"]["protocol_version"] = "2"
    out["containers"][0]["awg"]["subnet_address"] = subnet_address

    for key in ["H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5", "Jc", "Jmax", "Jmin", "S1", "S2", "S3", "S4"]:
        out["containers"][0]["awg"][key] = str(awg_obj[key])

    out["containers"][0]["awg"]["last_config"] = json.dumps(
        awg_obj,
        ensure_ascii=False,
        indent=4,
    )

    Path(out_json_path).write_text(json.dumps(out, ensure_ascii=False, indent=4), encoding="utf-8")

    res = subprocess.run(
        ["python3", decoder_py, "-i", out_json_path],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print("=== amnezia-config-decoder stderr ===")
        print(res.stderr.strip())
        print("=== amnezia-config-decoder stdout ===")
        print(res.stdout.strip())
        raise SystemExit(res.returncode)

    print(res.stdout.strip())


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: conf2vpn.py <conf> <template.json> <out.json> <amnezia-config-decoder.py> [container_name] [description]")
        sys.exit(1)
    container_name = sys.argv[5] if len(sys.argv) >= 6 else "amnezia-awg"
    description = sys.argv[6] if len(sys.argv) >= 7 else "awg"
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], container_name, description)
