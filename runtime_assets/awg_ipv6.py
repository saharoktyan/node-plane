"""Stable tunnel IPv6 addresses and full-tunnel capture, independent of egress."""
import ipaddress
import os
from pathlib import Path
import re
import sys
import tempfile

NETWORK = ipaddress.ip_network('fd42:6e70:6177:2::/64')
SERVER = str(NETWORK.network_address + 1) + '/64'


def dual_addresses(value):
    addresses = [part.strip() for part in value.split(',') if part.strip()]
    parsed = [ipaddress.ip_interface(part) for part in addresses]
    ipv4 = next((item for item in parsed if item.version == 4), None)
    if ipv4 is not None:
        managed = NETWORK.network_address + int(ipv4.ip)
        if not any(item.ip == managed for item in parsed):
            addresses.append(str(managed) + '/128')
    return ', '.join(addresses)


def capture_routes(value):
    routes = [str(ipaddress.ip_network(part.strip())) for part in value.split(',') if part.strip()]
    if '0.0.0.0/0' in routes and '::/0' not in routes:
        routes.append('::/0')
    return ', '.join(routes)


def migrate(text):
    parts = text.split('[Peer]')
    def interface(match):
        addresses = [part.strip() for part in match[1].split(',')]
        if not any(ipaddress.ip_interface(part).ip == ipaddress.ip_interface(SERVER).ip for part in addresses):
            addresses.append(SERVER)
        return 'Address = ' + ', '.join(addresses)
    parts[0] = re.sub(r'(?m)^Address[ \t]*=[ \t]*([^\r\n]+)$', interface, parts[0])
    for index in range(1, len(parts)):
        parts[index] = re.sub(r'(?m)^AllowedIPs[ \t]*=[ \t]*([^\r\n]+)$',
            lambda match: 'AllowedIPs = ' + dual_addresses(match[1]), parts[index])
    return '[Peer]'.join(parts)


def migrate_file(path):
    path = Path(path)
    original = path.read_text()
    updated = migrate(original)
    if updated == original:
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.awg-ipv6-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    action, value = sys.argv[1:]
    if action == 'migrate':
        migrate_file(value)
    elif action == 'addresses':
        print(dual_addresses(value))
    elif action == 'routes':
        print(capture_routes(value))
    else:
        raise SystemExit('Unknown AWG IPv6 action')
