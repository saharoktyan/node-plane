#!/usr/bin/env python3
"""Bounded, secret-free controller observations and confirmed service recovery.

The workstation embeds this file so diagnostics work even when the API is down.
It uses the installed Python environment only for PostgreSQL inspection. Repair
never edits environment files, operation journals, grants or maintenance gates.
"""
import argparse
import json
import math
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

BASE = Path('/opt/node-plane')
UNITS = (
    'node-plane-backend.service', 'node-plane-telegram.service',
    'node-plane-driver.service', 'node-plane-backend-worker.service',
)
JOB_TABLES = (
    'backend_agent_rollouts', 'backend_node_jobs', 'backend_operation_tasks',
    'backend_update_jobs', 'backend_node_removals', 'backend_backup_jobs',
    'backend_node_settings_tasks', 'backend_node_bootstraps',
)


def command(args, timeout=8):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                check=False)
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return -1, ''


def unit_state(unit):
    code, output = command(['systemctl', 'show', unit, '--no-pager',
                            '--property=LoadState,ActiveState,SubState,Result,NRestarts'])
    return dict(line.split('=', 1) for line in output.splitlines() if '=' in line) if code == 0 else {}


def database(action=None):
    """Runs with installed dependencies; PostgreSQL errors never print DSNs."""
    from config import POSTGRES_DSN
    import psycopg
    with psycopg.connect(POSTGRES_DSN, connect_timeout=5) as conn:
        conn.execute("SET LOCAL statement_timeout = '5s'")
        if action:
            # Same admission guard as backend. Keep its row lock through dispatch.
            if not conn.execute('SELECT id FROM backend_account_guard WHERE id=1 FOR UPDATE').fetchone():
                return {'status': 'refused', 'reason': 'maintenance_guard_missing'}
        maintenance = bool(conn.execute('SELECT job_id FROM backend_controller_update_gate WHERE id=1').fetchone())
        maintenance = maintenance or bool(conn.execute("SELECT id FROM backend_system_cleanup_jobs WHERE status IN ('queued','running','awaiting_shutdown','blocked') LIMIT 1").fetchone())
        maintenance = maintenance or bool(conn.execute("SELECT id FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running') LIMIT 1").fetchone())
        if action:
            if action not in UNITS or maintenance:
                return {'status': 'refused', 'reason': 'maintenance_active_or_action_unavailable'}
            state = unit_state(action)
            if state.get('LoadState') != 'loaded' or state.get('ActiveState') not in ('inactive', 'failed'):
                return {'status': 'refused', 'reason': 'service_state_changed'}
            # Do not restart running components or reset failure counters/journals.
            code, _ = command(['systemctl', 'start', action], timeout=30)
            return {'status': 'dispatched' if code == 0 else 'failed', 'unit': action}
        blocked = {}
        for table in JOB_TABLES:
            count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE status='blocked'").fetchone()[0]
            if count:
                blocked[table.removeprefix('backend_')] = count
        try:
            from db import get_db
            from db.migrations import check_schema
            check_schema(get_db())
            schema_valid = True
        except Exception:
            schema_valid = False
        cpu_row = conn.execute("SELECT value FROM backend_system_settings WHERE key='controller_cpu_sample'").fetchone()
        return {'status': 'ok', 'maintenance': maintenance, 'blocked': blocked, 'schema_valid': schema_valid,
                'cpu_sample': json.loads(cpu_row[0]) if cpu_row else None}


def installed_database(action=None):
    python = BASE / 'current/.venv/bin/python'
    env = dict(os.environ, NODE_PLANE_APP_DIR=str(BASE / 'current'),
               NODE_PLANE_SHARED_DIR=str(BASE / 'shared'), PYTHONPATH=str(BASE / 'current/app'))
    source = Path(__file__).read_text()
    try:
        # The source is supplied on stdin, with no credentials in argv/output.
        args = [str(python), '-', '--database']
        if action:
            args += ['--repair', action]
        result = subprocess.run(args, input=source, capture_output=True, text=True,
                                timeout=45, check=False, env=env)
        return json.loads(result.stdout) if result.returncode == 0 else {'status': 'unavailable'}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'status': 'unavailable'}


def controller_cpu(sample):
    try:
        if sample['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
            return None
        metrics = sample['host_metrics']
        sample_age = metrics['cpu_sample_age_seconds']
        if type(sample_age) not in {int,float} or not math.isfinite(sample_age):
            return None
        age = (datetime.now(timezone.utc)-datetime.fromisoformat(sample['at'])).total_seconds() + sample_age
        value, window = metrics['cpu_used_percent'], metrics['cpu_sample_window_seconds']
        if any(type(v) not in {int, float} or not math.isfinite(v) for v in (value, window, age)):
            return None
        return value if 0 <= value <= 100 and 60 <= window <= 75 and 0 <= age <= 15 else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def observe():
    checks = []
    def add(key, status, detail, repair=None):
        check = {'id': key, 'status': status, 'detail': detail}
        if repair:
            check['repair'] = repair
        checks.append(check)
    for relative in ('current', 'current/.venv/bin/python', 'shared/.env'):
        path = BASE / relative
        add('file:' + relative, 'ok' if path.exists() else 'error',
            f'{path}: present' if path.exists() else f'{path}: missing')
    env_path = BASE / 'shared/.env'
    try:
        keys = {line.split('=', 1)[0].strip() for line in env_path.read_text().splitlines()
                if '=' in line and not line.lstrip().startswith('#') and line.split('=', 1)[1].strip()}
        missing = sorted({'POSTGRES_DSN', 'BOT_TOKEN', 'ADMIN_IDS'} - keys)
        add('environment', 'error' if missing else 'ok',
            'Missing configuration keys: ' + ', '.join(missing) if missing else 'Required configuration keys present (values hidden)')
    except OSError:
        add('environment', 'error', 'Shared environment cannot be read')
    for relative in ('current/VERSION', 'current/BUILD_COMMIT'):
        try:
            value = (BASE / relative).read_text().strip()[:100]
            # Only release metadata; no arbitrary file content or terminal controls.
            value = ''.join(c for c in value if c.isalnum() or c in '.-_')
            add('release:' + relative, 'ok', f'{relative}: {value}')
        except OSError:
            add('release:' + relative, 'warning', f'{relative}: unavailable')
    usage = shutil.disk_usage(BASE if BASE.exists() else '/')
    add('disk', 'error' if usage.free < 256 * 1024**2 else 'ok',
        f'Free disk: {usage.free / 1024**3:.2f} GiB ({usage.free * 100 / usage.total:.1f}%)')
    db = installed_database()
    cpu = controller_cpu(db.get('cpu_sample'))
    add('cpu_used_percent', 'warning' if cpu is None else 'error' if cpu >= 95 else 'warning' if cpu >= 85 else 'ok',
        'Fresh CPU average unavailable; the monitoring worker needs one minute of uninterrupted samples' if cpu is None else f'CPU used (1-minute average): {cpu:g}%; warning ≥85%, error ≥95%')
    try:
        load = os.getloadavg()
        cpus = os.cpu_count() or 1
        add('load1', 'warning' if load[0]/cpus >= 2 else 'ok', f'Load average (1/5/15 min): {load[0]:g} / {load[1]:g} / {load[2]:g}; CPUs: {cpus}; warning ≥2 per CPU')
    except OSError:
        add('load1', 'warning', 'Host load measurement unavailable')
    add('database', 'ok' if db['status'] == 'ok' else 'error',
        'PostgreSQL connection and maintenance schema verified' if db['status'] == 'ok' else 'PostgreSQL or installed maintenance schema unavailable; inspect configuration/dependencies')
    permitted = db.get('status') == 'ok' and not db.get('maintenance', True) and db.get('schema_valid', True)
    if 'schema_valid' in db:
        add('schema', 'ok' if db['schema_valid'] else 'error',
            'Published schema revisions and checksums verified' if db['schema_valid'] else 'Schema revisions or checksums do not match the installed release')
    if db.get('maintenance'):
        add('maintenance', 'warning', 'Controller maintenance is active or blocked; automatic service recovery is unavailable')
    for unit in UNITS:
        state = unit_state(unit)
        active = state.get('ActiveState', 'unknown')
        loaded = state.get('LoadState') == 'loaded'
        repair = unit if permitted and loaded and active in ('inactive', 'failed') else None
        add('unit:' + unit, 'ok' if loaded and active == 'active' else 'error',
            f'{unit}: {active}/{state.get("SubState", "unknown")}; restarts={state.get("NRestarts", "unknown")}', repair)
    worker = unit_state('node-plane-backend-worker.service')
    if (BASE / 'shared/data/telegram-polling-conflict.json').exists():
        add('telegram_polling', 'error',
            'Another process is polling this bot token. Stop the bot on the other installation, then restart node-plane-telegram.service.')
    add('worker', 'ok' if worker.get('ActiveState') == 'active' else 'warning',
        'Worker: ' + worker.get('ActiveState', 'unknown'))
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=5) as response:
            ready = response.status == 200
    except Exception:
        ready = False
    add('api', 'ok' if ready else 'error', 'Backend readiness: ' + ('ready' if ready else 'unavailable'))
    for table, count in db.get('blocked', {}).items():
        add('blocked:' + table, 'warning', f'{table}: {count} blocked operations; preserve identity and use operation-specific recovery')
    errors = sum(c['status'] == 'error' for c in checks)
    warnings = sum(c['status'] == 'warning' for c in checks)
    return {'version': 1, 'scope': 'controller', 'checks': checks, 'errors': errors, 'warnings': warnings}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', action='store_true')
    parser.add_argument('--repair', choices=UNITS)
    args = parser.parse_args()
    try:
        result = database(args.repair) if args.database else installed_database(args.repair) if args.repair else observe()
    except Exception:
        result = {'status': 'unavailable'}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
