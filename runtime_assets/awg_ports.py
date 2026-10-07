"""Bounded preset-aware UDP port selection on the target host."""
import errno
import json
from pathlib import Path
import socket
import subprocess

FALLBACKS = {'quic': (443, 8443, 4433, 4443), 'dns': (53, 5353, 5300, 8053)}


def candidates(preset, preferred):
    if preset not in {'quic', 'dns', 'chaos'} or type(preferred) is not int or not 1 <= preferred <= 65535:
        raise ValueError('invalid AWG port selection')
    if preset == 'chaos':
        if not 1024 <= preferred <= 9999:
            raise ValueError('Chaos port must be between 1024 and 9999')
        return [1024 + (preferred - 1024 + offset) % 8976 for offset in range(128)]
    return list(dict.fromkeys((preferred, *FALLBACKS[preset])))


def docker_bindings(config_path, container_name):
    ids = subprocess.run(['docker', 'ps', '-aq'], check=True, capture_output=True,
        text=True, timeout=30).stdout.split()
    if not ids:
        return {}, set()
    records = json.loads(subprocess.run(['docker', 'container', 'inspect', *ids],
        check=True, capture_output=True, text=True, timeout=30).stdout)
    reserved, owned = {}, set()
    source = str(Path(config_path).parent)
    for record in records:
        is_owned = record['Name'].lstrip('/') == container_name and any(
            m.get('Type') == 'bind' and m.get('Source') == source
            and m.get('Destination') == '/opt/amnezia/awg' for m in record.get('Mounts', []))
        mappings = dict(record.get('HostConfig', {}).get('PortBindings') or {})
        mappings.update({protocol: bindings for protocol, bindings in
            (record.get('NetworkSettings', {}).get('Ports') or {}).items() if bindings})
        for protocol, bindings in mappings.items():
            if not protocol.endswith('/udp'):
                continue
            for binding in bindings or []:
                if not binding.get('HostPort'):
                    continue
                port = int(binding['HostPort'])
                reserved.setdefault(port, []).append(is_owned)
                if is_owned and record.get('State', {}).get('Running'):
                    owned.add(port)
    return reserved, owned


def can_bind(port):
    # Docker without a userland proxy may publish a port without a listener;
    # the Docker inventory must also be consulted by select_port.
    for family, address in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as probe:
                if family == socket.AF_INET6:
                    probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                probe.bind((address, port))
        except OSError as error:
            if family == socket.AF_INET6 and error.errno in {errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL}:
                continue
            return False
    return True


def select_port(preset, preferred, config_path, container_name):
    reserved, owned = docker_bindings(config_path, container_name)
    for port in candidates(preset, preferred):
        owners = reserved.get(port, [])
        if owners and not all(owners):
            continue
        if port in owned or can_bind(port):
            return port
    raise ValueError('no available UDP port for AWG preset')
