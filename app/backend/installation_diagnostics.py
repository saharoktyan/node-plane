"""Bounded live fleet diagnostics; observations never repair or acknowledge jobs."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .authorization import AccessDenied, require_permission
from .node_probe import probe
from .host_resources import cpu_metrics, CPU_WARNING, CPU_CRITICAL
from .nodes import NodeService, agent_installed
from .node_overview import NodeOverviewService


def server_checks(observation, *, vpn_enabled=True):
    checks = []
    def add(key, status, detail):
        checks.append({'id': key, 'status': status, 'detail': detail})
    def flag(key, value):
        add(key, 'ok' if value is True else 'error' if value is False else 'warning',
            'Confirmed' if value is True else 'Check failed' if value is False else 'Measurement unavailable')
    add('agent', 'ok', 'Agent responded to the live inspection')
    expected, actual = observation.get('desired_runtime_version'), observation.get('agent_version')
    if expected and actual:
        add('agent_version', 'ok' if actual == expected else 'warning',
            f'Agent version: {actual}; expected: {expected}')
    if observation['protocols']:
        flag('docker', observation['docker'])
    for protocol in observation['protocols']:
        for key in ('installed', 'running', 'config_valid'):
            if key == 'running' and not vpn_enabled:
                add(protocol['protocol'] + ':running', 'ok', 'VPN is administratively disabled; running service is not required')
                continue
            flag(protocol['protocol'] + ':' + key, protocol[key])
    if observation['protocols']:
        if observation['settings_pending']:
            add('settings', 'warning', f"Settings awaiting application: {observation['applied_revision']} → {observation['desired_revision']}")
        else:
            add('settings', 'ok', 'Desired and applied settings revisions match')
        flag('runtime_version', None if observation['runtime_drift'] is None else not observation['runtime_drift'])
        if observation['runtime_drift'] is True:
            checks[-1].update(status='warning', detail='Runtime differs from the controller release; inspect Updates')
    metrics = observation['host_metrics']
    cpu = cpu_metrics(metrics)
    if cpu is None:
        add('cpu_used_percent', 'warning', 'Fresh CPU average unavailable; the agent needs one minute of uninterrupted samples')
    else:
        value = cpu['cpu_used_percent']
        add('cpu_used_percent', 'error' if value >= CPU_CRITICAL else 'warning' if value >= CPU_WARNING else 'ok',
            f'CPU used (1-minute average): {value:g}%; warning ≥{CPU_WARNING}%, error ≥{CPU_CRITICAL}%')
    for key in ('ram_used_percent', 'disk_free_percent', 'load1'):
        value = metrics.get(key)
        if value is None or key == 'load1' and 'cpus' not in metrics:
            add(key, 'warning', 'Host measurement unavailable')
            continue
        if key == 'ram_used_percent':
            status = 'error' if value >= 95 else 'warning' if value >= 85 else 'ok'
            detail = f'RAM used: {value:g}%; warning ≥85%, error ≥95%'
        elif key == 'disk_free_percent':
            status = 'error' if value < 5 else 'warning' if value < 10 else 'ok'
            if metrics.get('disk_free_bytes', 256 * 1024**2) < 256 * 1024**2:
                status = 'error'
            detail = f'Disk free: {value:g}%; warning <10%, error <5% or <256 MiB'
        else:
            ratio = value / metrics['cpus']
            status = 'warning' if ratio >= 2 else 'ok'
            detail = f"Load average (1 min): {value:g}; CPUs: {metrics['cpus']:g}; warning ≥2 per CPU"
        add(key, status, detail)
    return checks


class FleetDiagnostics:
    def __init__(self, db, driver):
        self.db, self.driver = db, driver

    def observe(self, actor, offset=0, limit=5, *, node_key=None):
        require_permission(actor, 'maintenance.manage')
        require_permission(actor, 'nodes.manage')
        with self.db.connect() as conn:
            total = conn.execute('SELECT COUNT(*) AS n FROM backend_nodes').fetchone()['n']
            offset = min(offset, max(0, (total - 1) // limit * limit))
            keys = [row['key'] for row in conn.execute(
                'SELECT key FROM backend_nodes ORDER BY region,title,key LIMIT ? OFFSET ?', (limit, offset)).fetchall()]
            if node_key is not None:
                NodeService(self.db).get(actor, node_key)
                keys, total, offset = [node_key], 1, 0
        nodes = []
        for key in keys:
            try:
                node = NodeService(self.db).get(actor, key)
                state = NodeOverviewService(self.db).get(actor, key)
                nodes.append((node, state))
            except AccessDenied as exc:
                if exc.status != 404:
                    raise
                nodes.append(({'key': key, 'title': key, 'region': ''}, None))
            except Exception:
                nodes.append(({'key': key, 'title': key, 'region': ''}, None))
        def inspect(item):
            node, state = item
            checks = []
            if state is None:
                checks.append({'id': 'registry', 'status': 'error',
                               'detail': 'Saved server metadata unavailable or changed during diagnostics; refresh the registry'})
            elif not agent_installed(node):
                checks.append({'id': 'installation', 'status': 'ok',
                               'detail': 'Server is not installed; configure it and run server setup'})
            else:
                try:
                    observation = probe(node, self.driver)
                    checks = server_checks(observation, vpn_enabled=node['enabled'])
                    if not node['enabled']:
                        checks.append({'id': 'disabled', 'status': 'ok', 'detail':
                            'VPN protocols are not installed; agent inspected separately' if not node['protocols']
                            else 'VPN access is disabled; agent inspected separately'})
                except Exception:
                    checks = [{'id': 'agent', 'status': 'error',
                               'detail': 'Live inspection unavailable; inspect agent setup and controller driver connectivity'}]
            if state and (state['state'] in {'needs_attention', 'deletion_blocked'} or state['failed']):
                checks.append({'id': 'operations', 'status': 'warning', 'detail': 'Saved operations need attention; inspect server recovery'})
            if state and state['attention']:
                checks.append({'id': 'access', 'status': 'warning', 'detail':
                    'Desired access is not confirmed; inspect profile access synchronization'})
            return {'node_key': node['key'], 'title': node['title'], 'region': node['region'],
                    'checks': checks, 'errors': sum(c['status'] == 'error' for c in checks),
                    'warnings': sum(c['status'] == 'warning' for c in checks)}
        with ThreadPoolExecutor(max_workers=5) as workers:
            items = list(workers.map(inspect, nodes))
        return {'scope': 'servers', 'checked_at': datetime.now(timezone.utc).isoformat(),
                'items': items, 'total': total, 'offset': offset, 'page_size': limit,
                'next_offset': offset + limit if offset + limit < total else None,
                'errors': sum(item['errors'] for item in items),
                'warnings': sum(item['warnings'] for item in items)}
