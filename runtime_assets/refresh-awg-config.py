#!/usr/bin/env python3
"""Rebuild an existing AWG client config from the live server config, preserving its peer keys."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from conf2vpn import parse_conf
from awg_profile import ALL_FIELDS, interface_values, validate


def profile_description(server_key: str, profile_name: str) -> str:
    return ' · '.join(part for part in (server_key.strip(), profile_name.strip()) if part) or 'AmneziaWG'


def refresh(old_conf: str, server_conf: str, endpoint: str, port: str, server_pub: str) -> str:
    old = parse_conf(old_conf)
    current = parse_conf(server_conf)
    validate(interface_values(server_conf))
    iface, peer = old['Interface'], old['Peer']
    if not all((iface.get('PrivateKey'), iface.get('PublicKey'), iface.get('Address'), peer.get('PresharedKey'))):
        raise ValueError('Stored AWG config does not contain reusable peer credentials')
    if not endpoint or not server_pub:
        raise ValueError('AWG server endpoint or public key is missing')
    for key in ALL_FIELDS:
        value = current['Interface'].get(key)
        if key in ALL_FIELDS[:16] and not value:
            raise ValueError(f'AWG server config is missing {key}')
        if value:
            iface[key] = value
        else:
            iface.pop(key, None)
    validate(iface)
    peer['PublicKey'] = server_pub
    peer['Endpoint'] = f'{endpoint}:{port}'
    lines = ['[Interface]'] + [f'{key} = {value}' for key, value in iface.items()]
    lines += ['', '[Peer]'] + [f'{key} = {value}' for key, value in peer.items()]
    return '\n'.join(lines) + '\n'


def main() -> None:
    cfg = Path(os.environ.get('AWG_CONFIG', '/opt/node-plane-runtime/amnezia-awg/data/wg0.conf'))
    old_conf = sys.stdin.read()
    server_conf = cfg.read_text(encoding='utf-8')
    container = os.environ.get('AWG_CONTAINER_NAME', 'amnezia-awg')
    profile_name = sys.argv[1].strip() if len(sys.argv) > 1 else ''
    server_key = os.environ.get('SERVER_KEY', '').strip()
    description = profile_description(server_key, profile_name)
    iface = os.environ.get('AWG_IFACE', 'wg0')
    pub = ''
    for command in (['docker'], ['sudo', 'docker']):
        try:
            result = subprocess.run(command + ['exec', container, 'wg', 'show', iface, 'public-key'], capture_output=True, text=True, check=True)
        except (OSError, subprocess.CalledProcessError):
            continue
        pub = result.stdout.strip()
        if pub:
            break
    refreshed = refresh(old_conf, server_conf, os.environ.get('AWG_SERVER_IP', ''), os.environ.get('AWG_SERVER_PORT', '51820'), pub)
    client_pub = parse_conf(refreshed)['Interface']['PublicKey']
    if f'PublicKey = {client_pub}' not in server_conf:
        raise ValueError('Stored AWG peer no longer exists on server')
    with tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / 'client.conf'
        output = Path(tmp) / 'client.json'
        conf.write_text(refreshed, encoding='utf-8')
        result = subprocess.run([
            'python3', '/opt/node-plane-runtime/conf2vpn.py', str(conf),
            '/opt/node-plane-runtime/awg-template.json', str(output),
            '/opt/node-plane-runtime/amnezia-config-decoder.py', container, description,
        ], capture_output=True, text=True, check=True)
    key = next((line.strip() for line in result.stdout.splitlines() if line.strip().startswith('vpn://')), '')
    if not key:
        raise ValueError('AWG config converter did not return a vpn key')
    print(json.dumps({'wg_conf': refreshed, 'vpn_key': key}))


if __name__ == '__main__':
    main()
