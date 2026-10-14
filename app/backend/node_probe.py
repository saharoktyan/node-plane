"""Public, read-only server observation without configuration secrets."""
from datetime import datetime, timezone
import math
from time import perf_counter


def probe(node, driver):
    started = perf_counter()
    facts = driver.inspect_node_services(node['key'])
    if not isinstance(facts, dict) or facts.get('inspection_available') is False:
        raise ValueError('service inspection unavailable')
    metrics = {}
    ranges = {'ram_used_percent': (0, 100), 'disk_free_percent': (0, 100),
              'load1': (0, None), 'load5': (0, None), 'load15': (0, None),
              'cpus': (1, None), 'uptime_seconds': (0, None),
              'ram_total_bytes': (0, None), 'ram_available_bytes': (0, None),
              'disk_total_bytes': (0, None), 'disk_free_bytes': (0, None)}
    raw = facts.get('host_metrics')
    from .host_resources import cpu_metrics
    metrics.update(cpu_metrics(raw) or {})
    for key, (low, high) in ranges.items():
        value = raw.get(key) if isinstance(raw, dict) else None
        if type(value) in {int, float} and math.isfinite(value) and value >= low and (high is None or value <= high):
            metrics[key] = value
    def flag(key):
        return facts.get(key) if type(facts.get(key)) is bool else None
    settings = node.get('settings') or {}
    protocols = []
    for protocol in node['protocols']:
        ports = ([{'transport': transport, 'network': 'tcp', 'port': settings.get('xray_' + transport + '_port')}
                  for transport in node.get('xray_transports', [])] if protocol == 'xray'
                 else [{'transport': 'awg', 'network': 'udp', 'port': settings.get('awg_port')}])
        protocols.append({'protocol': protocol, 'installed': flag(protocol + '_installed'),
                          'running': flag(protocol + '_running'), 'config_valid': flag(protocol + '_config_valid'),
                          'ports': ports})
    return {'node_key': node['key'], 'title': node['title'],
            'checked_at': datetime.now(timezone.utc).isoformat(),
            'response_ms': round((perf_counter() - started) * 1000),
            'agent_reachable': True, 'docker': flag('docker'),
            'agent_version': facts.get('agent_version') or '',
            'runtime_version': facts.get('runtime_version') or '',
            'desired_runtime_version': facts.get('desired_runtime_version') or '',
            'runtime_drift': flag('runtime_drift'), 'host_metrics': metrics,
            'protocols': protocols, 'desired_revision': node['desired_revision'],
            'applied_revision': node['applied_revision'],
            'settings_pending': bool(protocols) and node['desired_revision'] != node['applied_revision']}
