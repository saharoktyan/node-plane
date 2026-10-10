"""Best-effort installer observations. Never used to confirm or replay an action."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from uuid import UUID

STAGES = {
    'download_driver': 'Downloading driver',
    'download_agent': 'Downloading agent',
    'build_driver': 'Building driver',
    'build_agent': 'Building agent',
    'install_driver': 'Installing driver',
    'check_ssh': 'Checking SSH access',
    'archive_journals': 'Archiving previous installation journals',
    'certificates': 'Preparing connection certificates',
    'install_agent': 'Installing agent',
    'configure': 'Saving connection settings',
    'restart_driver': 'Restarting driver',
    'verify_agent': 'Verifying agent connection',
    'runtime_sync': 'Preparing runtime files',
    'docker_install': 'Installing Docker',
    'docker_verify': 'Verifying Docker',
    'protocol_settings': 'Preparing protocol settings',
    'awg_port': 'Selecting AWG port',
    'xray_init': 'Preparing Xray',
    'awg_init': 'Preparing AmneziaWG',
    'firewall': 'Opening protocol ports',
    'protocol_deploy': 'Starting VPN protocols',
    'protocol_verify': 'Verifying VPN protocols',
}
PREFIXES = (
    ('download driver binary', 'download_driver'),
    ('download agent binary', 'download_agent'),
    ('build node-driver binary', 'build_driver'),
    ('build node-agent binary', 'build_agent'),
    ('install local node-plane-driver', 'install_driver'),
    ('install node-plane-driver systemd', 'install_driver'),
    ('check SSH prerequisites', 'check_ssh'),
    ('check previous controller journals', 'archive_journals'),
    ('prepare mutual TLS', 'certificates'),
    ('prepare driver-agent mutual TLS', 'certificates'),
    ('install node-agent', 'install_agent'),
    ('restart node-agent', 'install_agent'),
    ('write driver/agent env', 'configure'),
    ('restart node-plane-driver', 'restart_driver'),
    ('verify backend agent route', 'verify_agent'),
)


def progress_path(identity):
    shared = os.environ.get('NODE_PLANE_SHARED_DIR')
    if not shared:
        return None
    return Path(shared) / 'data' / 'installation-progress' / (str(UUID(identity)) + '.json')


def read_progress(row):
    if row['status'] not in {'running', 'blocked'}:
        return None
    try:
        path = progress_path(row['id'])
        if path is None:
            return None
        with path.open() as stream:
            data = json.loads(stream.read(2049))
        if data.get('stage') not in STAGES or data.get('operation_id') != row['id']:
            return None
        return {'stage': data['stage'], 'label': STAGES[data['stage']]}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def write_progress(path, identity, step):
    stage = next((code for prefix, code in PREFIXES if step.startswith(prefix)), None)
    if stage is None:
        return
    write_stage(path, identity, stage)


def write_stage(path, identity, stage):
    if stage not in STAGES:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = stream.name
            json.dump({'operation_id': str(UUID(identity)), 'stage': stage}, stream)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def observe_node_job(row, driver):
    """Read-only remote polling while the executor owns the original mutation."""
    import threading
    path = progress_path(row['id'])
    getter = getattr(driver, 'node_action_progress', None)
    if (path is None or not callable(getter) or row['action'] not in
            {'bootstrap', 'install_docker', 'reinstall_keep', 'reinstall_clean', 'sync_env', 'sync_xray'}):
        yield
        return
    stop = threading.Event()
    def observe():
        while not stop.wait(1):
            try:
                data = getter(row['node_key'], row['id'])
                if (not stop.is_set() and isinstance(data, dict)
                        and data.get('operation_id') == row['id']):
                    write_stage(path, row['id'], data.get('stage'))
            except Exception:
                # Observability must never change execution or recovery.
                pass
    try:
        write_stage(path, row['id'], 'docker_install' if row['action'] == 'install_docker' else 'runtime_sync')
    except OSError:
        pass
    thread = threading.Thread(target=observe, name='node-installation-progress', daemon=True)
    try:
        thread.start()
    except RuntimeError:
        yield
        return
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=3)


if __name__ == '__main__':
    import sys
    write_progress(*sys.argv[1:])
