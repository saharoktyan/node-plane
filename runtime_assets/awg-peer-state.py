#!/usr/bin/env python3
"""Durable revoked peers; caller holds wg0.conf.lock for all peer mutations."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def atomic_write(path, text):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.peer-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def values(text):
    return dict(line.strip().split('=', 1) for line in text.splitlines() if '=' in line)


def fields(text):
    return {key.strip(): value.strip() for key, value in values(text).items()}


def fingerprint(text):
    interface = text.split('[Peer]', 1)[0]
    private = fields(interface).get('PrivateKey', '')
    if not private:
        raise ValueError('Server private key is missing')
    return hashlib.sha256(private.encode()).hexdigest()


def split_peer(text, name):
    lines = text.splitlines(keepends=True)
    begin = next((i for i, line in enumerate(lines) if line.strip() == '# ' + name), None)
    if begin is None:
        return text, ''
    end = begin + 1
    while end < len(lines) and lines[end].strip():
        end += 1
    return ''.join(lines[:begin] + lines[end:]), ''.join(lines[begin:end])


def docker(*args, **options):
    return subprocess.run(['docker', *args], check=True, **options)


def mutate(action, config, clients, name, container, interface):
    config, clients = Path(config), Path(clients)
    text = config.read_text()
    clients.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive = clients / (name + '.revoked.json')
    current, peer = split_peer(text, name)
    if action == 'revoke':
        if peer:
            data = fields(peer)
            atomic_write(archive, json.dumps({'server': fingerprint(text), 'peer': peer}))
            # Persist revocation before changing the running interface.
            atomic_write(config, current)
        elif archive.exists():
            data = fields(json.loads(archive.read_text())['peer'])
        else:
            return 0
        docker('exec', '-i', container, 'wg', 'set', interface, 'peer', data['PublicKey'], 'remove')
        return 0
    if action == 'restore':
        if not archive.exists() or not (clients / (name + '.txt')).exists():
            return 2
        record = json.loads(archive.read_text())
        if record['server'] != fingerprint(text):
            archive.unlink()
            return 2
        data = fields(record['peer'])
        # Protect against an older runtime having reused this revoked peer IP.
        addresses = [fields(block).get('AllowedIPs', '') for block in current.split('[Peer]')[1:]]
        if data['AllowedIPs'] in addresses and not peer:
            archive.unlink()
            return 2
        if not peer:
            atomic_write(config, current.rstrip() + '\n\n' + record['peer'].rstrip() + '\n')
        docker('exec', '-i', container, 'sh', '-c',
               'umask 077; p=$(mktemp); trap \'rm -f "$p"\' EXIT; cat > "$p"; wg set "$1" peer "$2" preshared-key "$p" allowed-ips "$3"',
               'awg-peer', interface, data['PublicKey'], data['AllowedIPs'], input=data['PresharedKey'] + '\n', text=True)
        # Keep the archive until the next revoke: retries after a failed live update are safe.
        return 0
    raise ValueError('Unknown action')


def reserved(config, clients):
    current = Path(config).read_text()
    server = fingerprint(current)
    for block in current.split('[Peer]'):
        data = fields(block)
        for address in (data.get('AllowedIPs') or data.get('Address') or '').split(','):
            if address.strip():
                print(ipaddress.ip_interface(address.strip()).ip)
    for archive in Path(clients).glob('*.revoked.json'):
        record = json.loads(archive.read_text())
        if record['server'] == server:
            for address in fields(record['peer'])['AllowedIPs'].split(','):
                print(ipaddress.ip_interface(address.strip()).ip)


if __name__ == '__main__':
    if sys.argv[1] == 'reserved':
        reserved(*sys.argv[2:])
    else:
        raise SystemExit(mutate(*sys.argv[1:]))
