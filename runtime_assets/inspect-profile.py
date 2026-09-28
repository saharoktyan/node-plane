#!/usr/bin/env python3
"""Read disk and live protocol state under the profile mutation lock.

No config edits, container restarts, or API mutations. Errors mean unknown state.
Only booleans are returned; peer keys and Xray credentials stay on the node.
"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def xray(path, container, tags, name, expected_uuid):
    api = load('xray-user-api')
    with open(str(path) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = json.loads(Path(path).read_text())
        inbounds = {item['tag']: item for item in config['inbounds']}
        if len(set(tags)) != 2 or any(tag not in inbounds for tag in tags):
            raise ValueError('missing protocol inbound')
        disk, live = [], []
        matches = True
        for tag in tags:
            stored = [item for item in inbounds[tag]['settings']['clients']
                      if item.get('email', item.get('name')) == name]
            if len(stored) > 1:
                raise ValueError('duplicate profile')
            actual = api.users(container, tag).get(name)
            disk.append(bool(stored))
            live.append(actual is not None)
            expected_flow = 'xtls-rprx-vision' if tag == tags[0] else ''
            matches &= bool(stored) and actual == {'id': expected_uuid, 'flow': expected_flow} and stored[0].get('id') == expected_uuid and stored[0].get('flow', '') == expected_flow
        # Presence on only one inbound is drift, never a confirmed full state.
        return {'disk_present': any(disk), 'live_present': any(live),
                'identity_matches': bool(matches and all(disk) and all(live)),
                'config_available': bool(matches and all(disk) and all(live))}


def awg(path, clients, container, interface, name):
    peers = load('awg-peer-state')
    with open(str(path) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        text = Path(path).read_text()
        if sum(line.strip() == '# ' + name for line in text.splitlines()) > 1:
            raise ValueError('duplicate peer')
        _, block = peers.split_peer(text, name)
        data = peers.fields(block) if block else {}
        if not data:
            archive = Path(clients) / (name + '.revoked.json')
            if archive.exists():
                record = json.loads(archive.read_text())
                if record['server'] != peers.fingerprint(text):
                    raise ValueError('archived identity belongs to another server')
                data = peers.fields(record['peer'])
        if not data.get('PublicKey'):
            # Absence cannot be verified by name: live peers have public keys only.
            raise ValueError('peer identity unavailable')
        result = subprocess.run(['docker', 'exec', container, 'wg', 'show', interface, 'dump'],
                                check=True, capture_output=True, text=True, timeout=15)
        lines = result.stdout.splitlines()
        if not lines or len(lines[0].split('\t')) < 4:
            raise ValueError('invalid live interface response')
        rows = [line.split('\t') for line in lines[1:]]
        found = [row for row in rows if row[0] == data['PublicKey']]
        if len(found) > 1 or any(len(row) < 8 for row in rows):
            raise ValueError('invalid live peer response')
        actual = found[0] if found else None
        matches = bool(block and actual and actual[1] == data.get('PresharedKey')
                       and set(actual[3].split(',')) == set(data.get('AllowedIPs', '').split(',')))
        return {'disk_present': bool(block), 'live_present': actual is not None,
                'identity_matches': matches,
                'config_available': matches and (Path(clients) / (name + '.txt')).is_file()}


def main():
    protocol, name, *extra = sys.argv[1:]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name):
        raise ValueError('invalid profile name')
    if protocol == 'xray' and len(extra) == 1:
        value = xray(os.environ.get('XRAY_CONFIG', '/opt/node-plane-runtime/xray/config.json'),
            os.environ.get('XRAY_CONTAINER_NAME', 'xray'),
            (os.environ.get('XRAY_INBOUND_TCP_TAG', 'reality-tcp'), os.environ.get('XRAY_INBOUND_XHTTP_TAG', 'reality-xhttp')), name, extra[0])
    elif protocol == 'awg' and not extra:
        prefix = os.environ.get('SERVER_KEY', '')
        display = prefix + '-' + name if prefix else name
        value = awg(os.environ.get('AWG_CONFIG', '/opt/node-plane-runtime/amnezia-awg/data/wg0.conf'),
            os.environ.get('AWG_CLIENTS_DIR', '/opt/node-plane-runtime/awg-clients'),
            os.environ.get('AWG_CONTAINER_NAME', 'amnezia-awg'), os.environ.get('AWG_IFACE', 'wg0'), display)
    else:
        raise ValueError('invalid inspection arguments')
    print(json.dumps(value))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('profile inspection unavailable')
