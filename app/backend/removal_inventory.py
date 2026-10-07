"""Read configuration as data; never source shell files during verification."""
import json
from pathlib import Path
import re
import shlex


def read_inventory(config_path, binary_path):
    fields = {'runtime_root': '/opt/node-plane-runtime',
              'state_dir': '/var/lib/node-plane-agent', 'log_dir': '/var/log/node-plane-agent',
              'node_env_path': '/etc/node-plane/node.env',
              'xray_config_path': '/opt/node-plane-runtime/xray/config.json',
              'awg_config_path': '/opt/node-plane-runtime/amnezia-awg/data/wg0.conf',
              'tls_certificate_path': '/etc/node-plane/tls/server.crt',
              'tls_key_path': '/etc/node-plane/tls/server.key',
              'tls_client_ca_path': '/etc/node-plane/tls/ca.crt'}
    # Agent rollout writes flat TOML string fields. Refuse unsupported syntax
    # instead of guessing paths (Python 3.10 nodes do not provide tomllib).
    for line in Path(config_path).read_text().splitlines():
        key, separator, value = line.partition('=')
        if separator and key.strip() in fields:
            fields[key.strip()] = json.loads(value.strip())
    values = {'XRAY_CONFIG': fields['xray_config_path'],
              'AWG_CONFIG': fields['awg_config_path'],
              'AWG_CLIENTS_DIR': fields['runtime_root'] + '/awg-clients',
              'XRAY_DOCKER_DIR': fields['runtime_root'] + '/xray',
              'AWG_DOCKER_DIR': fields['runtime_root'] + '/amnezia-awg',
              'XRAY_CONTAINER_NAME': 'xray', 'AWG_CONTAINER_NAME': 'amnezia-awg'}
    env_path = Path(fields['node_env_path'])
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            key, separator, value = line.partition('=')
            key = key.strip().removeprefix('export ')
            if separator and key in values:
                parts = shlex.split(value, comments=True)
                if len(parts) != 1 or any(c in parts[0] for c in '$`'):
                    raise ValueError('unsupported node environment value')
                values[key] = parts[0]
    paths = [config_path, binary_path, *fields.values(), values['AWG_CLIENTS_DIR'],
             values['XRAY_DOCKER_DIR'], values['AWG_DOCKER_DIR']]
    for name in ('XRAY_CONFIG', 'AWG_CONFIG'):
        path = values[name]
        paths.extend([path, path + '.lock', path + '.bak', path + '.node-plane-settings.bak',
                      path + '.node-plane-settings.bak.absent'])
        parent = Path(path).parent
        if parent.exists():
            prefixes = (Path(path).name + '.bak.', Path(path).name + '.dirbak.', '.awg-deploy-backup-',
                        '.awg-config-backup-', '.awg-regenerate-backup-', '.xray-config-', '.xray-user-')
            paths.extend(str(entry) for entry in parent.iterdir()
                         if entry.name.startswith(prefixes))
    return validate_inventory({'paths': sorted(set(paths)),
        'containers': [values['XRAY_CONTAINER_NAME'], values['AWG_CONTAINER_NAME']]})


def validate_inventory(value):
    if not isinstance(value, dict) or set(value) != {'paths', 'containers'}:
        raise ValueError('invalid removal inventory')
    paths, containers = value['paths'], value['containers']
    if not isinstance(paths, list) or not 1 <= len(paths) <= 64:
        raise ValueError('invalid removal paths')
    for path in paths:
        if (not isinstance(path, str) or len(path) > 4096 or not path.startswith('/')
                or any(c in path for c in '\n\r\0') or '..' in Path(path).parts
                or len(Path(path).parts) < 3):
            raise ValueError('invalid removal path')
    if (not isinstance(containers, list) or len(containers) != 2
            or any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name)
                   for name in containers) or len(set(containers)) != 2):
        raise ValueError('invalid removal containers')
    return {'paths': sorted(set(paths)), 'containers': containers}


def inventory_digest(value):
    import hashlib
    return hashlib.sha256(json.dumps(validate_inventory(value), sort_keys=True).encode()).hexdigest()
