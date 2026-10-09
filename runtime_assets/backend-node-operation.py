#!/usr/bin/env python3
"""Backend-owned maintenance, serialized with profile/settings mutations.

The journal survives runtime cleanup. Unknown outcomes are recovered by reading
the exact command, never by replaying a destructive operation.
"""
import fcntl
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location('profile_intents', ROOT / 'apply-profile-intent.py')
intents = importlib.util.module_from_spec(spec)
spec.loader.exec_module(intents)
ACTIONS = {'bootstrap', 'reinstall_keep', 'reinstall_clean', 'cleanup_runtime',
           'install_docker', 'check_ports', 'open_ports', 'sync_runtime',
           'sync_env', 'sync_xray', 'regenerate_entropy'}


def command(args, lock_fd=None):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=1200, pass_fds=() if lock_fd is None else (lock_fd,),
                          env=None if lock_fd is None else dict(os.environ,NODE_PLANE_PROFILE_LOCK_FD=str(lock_fd))).stdout.strip()


def environment():
    names = ('XRAY_CONFIG', 'AWG_CONFIG', 'XRAY_CONTAINER_NAME', 'AWG_CONTAINER_NAME')
    defaults = ('/opt/node-plane-runtime/xray/config.json',
                '/opt/node-plane-runtime/amnezia-awg/data/wg0.conf', 'xray', 'amnezia-awg')
    values = command(['bash', '-c', 'source /etc/node-plane/node.env; ' +
                      'printf "%s\\n" ' + ' '.join('"${' + name + ':-' + default + '}"'
                      for name, default in zip(names, defaults))]).splitlines()
    if len(values) != 4:
        raise ValueError('invalid node environment')
    return values


def inspect():
    xray, awg, xc, ac = environment()
    docker = False
    if shutil.which('docker'):
        docker = subprocess.run(['docker', 'info'], capture_output=True, timeout=30).returncode == 0
    xvalid = avalid = False
    try:
        data = json.loads(Path(xray).read_text())
        tags = {item['tag']: item for item in data['inbounds']}
        xvalid = all(tags[tag]['streamSettings']['realitySettings']['privateKey']
                     for tag in ('reality-tcp', 'reality-xhttp'))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    entropy = []
    try:
        text = Path(awg).read_text()
        avalid = bool(re.search(r'(?m)^PrivateKey\s*=\s*\S+', text)) and 'HeaderProtectionKey' in text
        entropy = [line for line in text.splitlines() if re.match(r'^(S[1-4]|H[1-4]|I[1-5]|RandomTrailers|DisableCookies)\s*=', line)]
    except OSError:
        pass
    def container(name):
        if not docker:
            return False, False
        result = subprocess.run(['docker', 'inspect', '-f', '{{.State.Running}}', name],
            capture_output=True, text=True, timeout=30)
        return result.returncode == 0, result.returncode == 0 and result.stdout.strip() == 'true'
    xinstalled, xrunning = container(xc)
    ainstalled, arunning = container(ac)
    return {'docker': docker, 'xray_config_valid': bool(xvalid), 'awg_config_valid': avalid,
            'xray_installed': xinstalled, 'awg_installed': ainstalled,
            'xray_running': xrunning, 'awg_running': arunning, 'entropy': entropy,
            'host_metrics': host_metrics(), 'inspection_available': True}


def host_metrics():
    """Read host resources on the agent; missing measurements remain unknown."""
    metrics = {}
    try:
        memory = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            name, value = line.split(':', 1)
            memory[name] = int(value.strip().split()[0])
        total, available = memory['MemTotal'], memory['MemAvailable']
        if total > 0 and 0 <= available <= total:
            metrics['ram_used_percent'] = round(100 * (total - available) / total, 1)
    except (OSError, ValueError, KeyError, IndexError):
        pass
    try:
        disk = os.statvfs(ROOT)
        if disk.f_blocks > 0:
            metrics['disk_free_percent'] = round(100 * disk.f_bavail / disk.f_blocks, 1)
    except OSError:
        pass
    try:
        metrics['load1'] = round(os.getloadavg()[0], 2)
        metrics['cpus'] = os.cpu_count() or 1
    except OSError:
        pass
    return metrics


def ports(intent):
    settings = intent['settings']
    return ([('tcp', settings['xray_tcp_port']), ('tcp', settings['xray_xhttp_port'])]
            if 'xray' in intent['protocols'] else []) + (
            [('udp', settings['awg_port'])] if 'awg' in intent['protocols'] else [])


def owned_protocol_containers(xray, awg, xc, ac):
    """Plan deletion by mount ownership and immutable ID, including crash leftovers."""
    ids = command(['docker', 'ps', '-aq']).splitlines()
    if not ids:
        return []
    records = json.loads(command(['docker', 'container', 'inspect', *ids]))
    owned = []
    for record in records:
        name = record['Name'].lstrip('/')
        for configured, source, destination in (
                (xc, Path(xray).parent, '/etc/xray'),
                (ac, Path(awg).parent, '/opt/amnezia/awg')):
            if name != configured and not re.fullmatch(re.escape(configured) + r'-previous-\d+', name):
                continue
            mounts = record.get('Mounts', [])
            if not any(mount.get('Type') == 'bind'
                       and mount.get('Source') == str(source)
                       and mount.get('Destination') == destination for mount in mounts):
                raise ValueError('container ownership could not be verified: ' + name)
            identity = record['Id']
            if not re.fullmatch(r'[a-f0-9]{64}', identity):
                raise ValueError('invalid container identity')
            owned.append(identity)
    return list(dict.fromkeys(owned))


def remove_protocol_runtime(lock_fd):
    xray, awg, xc, ac = environment()
    if any(path.is_symlink() for path in (ROOT, *ROOT.parents)):
        raise ValueError('runtime cleanup root must not contain symlinks')
    directories = (Path(xray).parent, Path(awg).parent.parent, ROOT / 'awg-clients')
    # Validate the whole deletion plan before stopping containers or removing
    # the first directory. A later invalid path must not leave partial cleanup.
    expected = (ROOT.resolve() / 'xray', ROOT.resolve() / 'amnezia-awg',
                ROOT.resolve() / 'awg-clients')
    for directory, owned in zip(directories, expected):
        if directory.is_symlink() or directory.resolve() != owned:
            raise ValueError('runtime cleanup path is outside managed directories')
    containers = owned_protocol_containers(xray, awg, xc, ac)
    intents.stop_leased_runtime_units()
    for identity in containers:
        command(['docker', 'rm', '-f', identity], lock_fd)
    # Only protocol-owned directories; never agent identity, journal or scripts.
    for directory in directories:
        if directory.exists():
            shutil.rmtree(directory)


def run(action, intent, lock_fd):
    if action == 'cleanup_runtime':
        remove_protocol_runtime(lock_fd)
        return {'cleaned': True}
    if action == 'check_ports':
        statuses = []
        for protocol, port in ports(intent):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM if protocol == 'tcp' else socket.SOCK_DGRAM) as probe:
                try:
                    probe.bind(('0.0.0.0', port))
                    status = 'free'
                except OSError:
                    # A listener can be our own container; do not call it an installation failure.
                    status = 'listening'
            statuses.append({'protocol': protocol, 'port': port, 'status': status})
        return {'ports': statuses}
    if action == 'open_ports':
        if not shutil.which('ufw'):
            return {'firewall': 'unmanaged'}
        for protocol, port in ports(intent):
            command(['ufw', 'allow', str(port) + '/' + protocol], lock_fd)
        command(['ufw', 'reload'], lock_fd)
        return {'firewall': 'opened'}
    if action == 'install_docker':
        if not inspect()['docker']:
            raise ValueError('Docker is unavailable')
        return {'docker': True}
    if action == 'sync_runtime':
        return {'synced': True}
    if action == 'regenerate_entropy':
        command([str(ROOT / 'regenerate-awg-entropy.sh')], lock_fd)
        return {'regenerated': True}
    if action == 'reinstall_keep':
        facts = inspect()
        if any(not facts[p + '_config_valid'] for p in intent['protocols']):
            raise ValueError('reusable protocol config is missing')
    if action == 'reinstall_clean':
        remove_protocol_runtime(lock_fd)
    # Bootstrap/reinstall/repair share the same settings verifier. Existing
    # identities are preserved unless clean reinstall explicitly removed them.
    settings_intent = {'kind': 'node_settings', **intent}
    intents.validate_node_settings(settings_intent)
    result = intents.run_node_settings(settings_intent, lock_fd)
    return json.loads(result['payload_json'])


def temporary_profile(action, command_id, intent, recover, path):
    """Explicit leased-peer commands over the existing authenticated node RPC.

    Unknown actions on old runtime bundles fail instead of losing a TTL field.
    """
    # node_key selects the authenticated agent in the driver; the agent also
    # checks its own node identity before executing this helper.
    intent = dict(intent)
    intent.pop('node_key', None)
    intents.validate(intent)
    if intent['command_id'] != command_id:
        raise ValueError('temporary operation is not supported')
    if action == 'temporary_status':
        if not recover:
            raise ValueError('temporary status is read-only')
        with open(str(path) + '.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
                row = conn.execute('SELECT command_id,expires_at,status FROM temporary_leases WHERE protocol=? AND profile=?',
                                   (intent['protocol'],intent['runtime_name'])).fetchone()
                if row is None or row[0] != command_id:
                    raise ValueError('temporary identity not found')
                return {'expires_at':row[1],'status':row[2],
                        'enforcement_ready':intents.lease_enforcement_ready(intent['protocol'])}
    if action == 'temporary_ensure':
        if intent.get('lease_seconds') not in intents.LEASE_DURATIONS or intent['action'] != 'ensure':
            raise ValueError('temporary lease is required')
    elif action == 'temporary_revoke':
        if intent['action'] != 'delete' or 'lease_seconds' in intent:
            raise ValueError('invalid temporary revocation')
        # This endpoint must never delete a permanent peer.
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            lease = conn.execute('SELECT status FROM temporary_leases WHERE protocol=? AND profile=?',
                                (intent['protocol'], intent['runtime_name'])).fetchone()
            if not lease:
                raise ValueError('temporary identity not found')
            if lease[0] in {'expired','revoked'}:
                return {'revocation_mode': 'new_connections_only' if intent['protocol']=='xray' else 'peer_removed'}
    else:
        raise ValueError('invalid temporary action')
    if Path(str(path) + '.disabled').exists():
        raise ValueError('agent removal is in progress')
    if recover:
        response = intents.lookup(intent, path)
    else:
        if action == 'temporary_ensure':
            intents.ensure_lease_scheduler(protocol=intent['protocol'])
        response = intents.apply(intent, path, intents.run)
    result = json.loads(response['payload_json']) if response['payload_json'] else {}
    if action == 'temporary_ensure':
        if not result.get('expires_at'):
            raise ValueError('temporary expiry receipt missing')
        if intent['protocol'] == 'awg':
            summary = response['summary']
            config = summary[summary.index('[Interface]'):].split('\n===========', 1)[0].strip()
            uri = next(line.strip() for line in summary.splitlines() if line.strip().startswith('vpn://'))
            result.update(wg_conf=config, vpn_key=uri)
        else:
            # REALITY uses the existing inbound's shortId/public metadata. Only
            # the independent VLESS client UUID is new; ports remain unchanged.
            result['xray_uuid'] = intent['uuid']
    else:
        result['revocation_mode'] = 'new_connections_only' if intent['protocol'] == 'xray' else 'peer_removed'
    return result


def execute(action, command_id, intent, recover=False, path='/etc/node-plane/profile-intents.sqlite3'):
    if action in {'temporary_ensure', 'temporary_revoke', 'temporary_status'}:
        return temporary_profile(action, command_id, intent, recover, path)
    resolving = action.startswith('resolve_')
    if resolving:
        action = action[len('resolve_'):]
    if action == 'traffic':
        if resolving or recover:
            raise ValueError('traffic observations cannot be recovered')
        # Do not wait behind a bootstrap, and never create a journal for a read.
        with open(str(path) + '.lock', 'r') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if Path(str(path) + '.disabled').exists():
                raise ValueError('agent removal is in progress')
            traffic_spec = importlib.util.spec_from_file_location('traffic_snapshot', ROOT / 'traffic-snapshot.py')
            traffic = importlib.util.module_from_spec(traffic_spec)
            traffic_spec.loader.exec_module(traffic)
            return traffic.read(intent)
    if action == 'inspect':
        return inspect()
    if action not in ACTIONS or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', command_id):
        raise ValueError('invalid node operation')
    if action not in {'install_docker', 'sync_runtime', 'cleanup_runtime'}:
        intents.validate_node_settings({'kind': 'node_settings', **intent})
    elif set(intent) != {'node_key', 'command_id', 'revision', 'protocols', 'settings'}:
        raise ValueError('invalid node operation intent')
    if intent['command_id'] != command_id:
        raise ValueError('command identity mismatch')
    fingerprint = hashlib.sha256(json.dumps({'action': action, 'intent': intent}, sort_keys=True).encode()).hexdigest()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if Path(str(path) + '.disabled').exists():
            raise ValueError('agent removal is in progress')
        with closing(sqlite3.connect(path)) as conn:
            os.chmod(path, 0o600)
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, response TEXT, instance_id TEXT NOT NULL)')
            prior = conn.execute('SELECT fingerprint, status, response FROM commands WHERE id = ?', (command_id,)).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise ValueError('command identity conflict')
                if resolving and prior[1] in {'running', 'retired'}:
                    instance = conn.execute('SELECT instance_id FROM commands WHERE id = ?', (command_id,)).fetchone()[0]
                    if prior[1] == 'running' and instance == os.environ.get('NODE_PLANE_AGENT_INSTANCE_ID', 'standalone'):
                        raise ValueError('restart the agent before retiring an interrupted operation')
                    conn.execute("UPDATE commands SET status = 'retired' WHERE id = ?", (command_id,))
                    conn.commit()
                    return {'node_key': intent['node_key'], 'action': action, 'revision': intent['revision'], 'retired': True}
                if prior[1] != 'succeeded':
                    raise ValueError('node operation outcome is uncertain')
                return json.loads(prior[2])
            if resolving:
                conn.execute("INSERT INTO commands VALUES (?, ?, 'retired', NULL, ?)",
                    (command_id, fingerprint, os.environ.get('NODE_PLANE_AGENT_INSTANCE_ID', 'standalone')))
                conn.commit()
                return {'node_key': intent['node_key'], 'action': action, 'revision': intent['revision'], 'retired': True}
            if recover:
                raise ValueError('node operation result not found')
            if conn.execute("SELECT 1 FROM commands WHERE status = 'running'").fetchone():
                raise ValueError('unfinished operation requires reconciliation')
            conn.execute("INSERT INTO commands VALUES (?, ?, 'running', NULL, ?)",
                (command_id, fingerprint, os.environ.get('NODE_PLANE_AGENT_INSTANCE_ID', 'standalone')))
            conn.commit()
            result = {'node_key': intent['node_key'], 'action': action,
                      'revision': intent['revision'], 'result': run(action, intent, lock.fileno())}
            conn.execute("UPDATE commands SET status = 'succeeded', response = ? WHERE id = ?",
                         (json.dumps(result), command_id))
            conn.commit()
            return result


if __name__ == '__main__':
    os.umask(0o077)
    try:
        action, identity, raw, recover = sys.argv[1:]
        print(json.dumps(execute(action, identity, json.loads(raw), recover == 'true')))
    except Exception:
        raise SystemExit('node operation requires reconciliation')
