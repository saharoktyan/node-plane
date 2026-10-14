"""Reconcile one failed stack update on an older controller, without replaying it.

Run from the installed controller with its Python environment and PYTHONPATH=app.
Only a collected unit with a durable successful rollback receipt is accepted.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import UUID, uuid4


class RecoveryRefused(ValueError):
    """A specific evidence check failed; safe to show without database details."""


def inspect_update(db, identity):
    identity = str(UUID(str(identity)))
    with db.connect() as conn:
        row = conn.execute('SELECT id,kind,status FROM backend_update_jobs WHERE id=?', (identity,)).fetchone()
        gate = conn.execute('SELECT job_id FROM backend_controller_update_gate WHERE id=1').fetchone()
    return {'requested_job_id':identity, 'found':row is not None,
            'kind':row['kind'] if row else None, 'status':row['status'] if row else None,
            'gate_job_id':gate['job_id'] if gate else None}


def recover(db, shared, identity, execute=None):
    execute = execute or subprocess.run
    identity = str(UUID(str(identity)))
    shared = Path(shared)
    fd = os.open(shared / 'data' / 'backend-worker.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with db.transaction() as conn:
            conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
            row = conn.execute('SELECT * FROM backend_update_jobs WHERE id=?', (identity,)).fetchone()
            if not row:
                raise RecoveryRefused('No backend update has this ID; use job_id from the workstation journal, not its local id')
            if row['kind'] != 'stack':
                raise RecoveryRefused('This backend update is not a stack update')
            if row['status'] not in {'running', 'blocked'}:
                raise RecoveryRefused('This backend update is not unfinished; inspect its recorded status and resume observation in Workstation')
            plan = json.loads(row['result_json'] or '{}')
            if not isinstance(plan, dict):
                raise RecoveryRefused('The stored update plan is invalid')
            unit = plan.get('unit_name', '')
            if plan.get('phase') != 'core' or not isinstance(unit, str) or not re.fullmatch(r'node-plane-update-[0-9]{8}-[0-9]{6}', unit):
                raise RecoveryRefused('The original controller update unit is not confirmed')
            gate = conn.execute('SELECT job_id FROM backend_controller_update_gate WHERE id=1').fetchone()
            metadata = conn.execute("SELECT value FROM schema_meta WHERE key='updates_last_run_unit'").fetchone()
            if not gate or gate['job_id'] != identity or not metadata or metadata['value'] != unit:
                raise RecoveryRefused('The maintenance gate or recorded unit belongs to another operation')
            if conn.execute("SELECT 1 FROM backend_system_cleanup_jobs WHERE status IN ('queued','running','awaiting_shutdown','blocked') LIMIT 1").fetchone() or conn.execute("SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running') LIMIT 1").fetchone():
                raise RecoveryRefused('Other controller maintenance is active')
            if conn.execute("SELECT 1 FROM backend_update_items WHERE job_id=? AND (status!='awaiting_executor' OR child_id IS NOT NULL) LIMIT 1", (identity,)).fetchone():
                raise RecoveryRefused('Node update work already started; use normal recovery')
            state = execute(['systemctl', 'show', unit + '.service', '--property=LoadState', '--value'],
                            capture_output=True, text=True, timeout=15)
            if state.returncode not in {0, 1} or state.stdout.strip() != 'not-found':
                raise RecoveryRefused('The update unit is still present or its absence is unconfirmed')
            path = shared / 'data' / 'updates' / (identity + '.json')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as stream:
                raw = stream.read(16385)
                if len(raw) > 16384:
                    raise RecoveryRefused('The saved update result exceeds the expected size')
                progress = json.loads(raw)
            if not isinstance(progress, dict) or progress.get('status') != 'failed' or progress.get('rollback_status') != 'succeeded':
                raise RecoveryRefused('A successful rollback of this exact update is not confirmed')
            plan.update(rollback_status='succeeded', error_code=progress.get('error_code', 'core_update_failed'))
            if isinstance(progress.get('components'), dict):
                plan['components'] = progress['components']
            conn.execute("UPDATE backend_update_jobs SET status='rolled_back',result_json=? WHERE id=?", (json.dumps(plan), identity))
            conn.execute("UPDATE backend_update_items SET status='skipped' WHERE job_id=? AND status='awaiting_executor'", (identity,))
            now = datetime.now(timezone.utc).isoformat()
            for key, value in (('updates_last_run_status', 'failed'), ('updates_last_run_finished_at', now)):
                conn.execute('INSERT INTO schema_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
            conn.execute('INSERT INTO backend_recovery_audit VALUES (?,?,?,?,?,?,?,?)',
                         (str(uuid4()), row['actor_id'], 'update', identity, 'recheck', 'confirmed', 'rolled_back', now))
            conn.execute('DELETE FROM backend_controller_update_gate WHERE id=1 AND job_id=?', (identity,))
    return {'id': identity, 'status': 'rolled_back', 'installation_replayed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation_id', type=UUID,
                        help='Backend task UUID (job_id in the Workstation journal, not its local id)')
    parser.add_argument('--shared-dir', type=Path, required=True,
                        help='Installed controller shared directory, e.g. /opt/node-plane/shared')
    parser.add_argument('--inspect', action='store_true', help='Only read backend task status and the current update gate')
    args = parser.parse_args()
    app_dir = Path.cwd()
    if not (app_dir / 'app/backend/updates.py').is_file():
        parser.exit(1, 'Run this command from the installed controller directory.\n')
    shared = args.shared_dir.resolve()
    if not (shared / '.env').is_file() or not (shared / 'data').is_dir():
        parser.exit(1, 'The selected shared directory lacks the controller environment or data directory.\n')
    # Interactive root shells do not inherit the environment of systemd services.
    # Set the paths before importing config so it loads the installed shared .env.
    os.environ['NODE_PLANE_APP_DIR'] = str(app_dir)
    os.environ['NODE_PLANE_SHARED_DIR'] = str(shared)
    from config import SHARED_ROOT
    from db import get_db
    try:
        result = inspect_update(get_db(), args.operation_id) if args.inspect else recover(get_db(), SHARED_ROOT, args.operation_id)
    except BlockingIOError:
        parser.exit(1, 'The backend worker is busy. Retry after its current check finishes.\n')
    except RecoveryRefused as exc:
        parser.exit(1, f'Recovery refused: {exc}. Nothing was replayed; no changes were committed.\n')
    except FileNotFoundError:
        parser.exit(1, 'A required recovery file is missing in the selected shared directory. No changes were committed.\n')
    except (OSError, ValueError, subprocess.SubprocessError):
        parser.exit(1, 'Recovery evidence is incomplete or does not match. Nothing was replayed; the transaction was rolled back.\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
