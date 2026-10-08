#!/usr/bin/env python3
"""Explicit bridge for an old verifier rejecting the alpha.52 journal helper.

Run with the installed controller venv and PYTHONPATH=app. No database records,
gates or installed package manifests are rewritten. The original failed unit is
reused only after proving its failure happened while downloading the package.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import UUID


def validate(job, items, journal, unit_state):
    plan = json.loads(job.get('result_json') or '{}')
    unit = plan.get('unit_name', '')
    if (job.get('kind') != 'stack' or job.get('status') != 'blocked' or
            plan.get('phase') != 'core' or plan.get('resolved_ref') != 'v0.4.3-alpha.52' or
            not re.fullmatch(r'[0-9a-f]{40}', plan.get('expected_commit') or '') or
            plan.get('error_code') != 'update_verification_unavailable' or
            not re.fullmatch(r'node-plane-update-[0-9]{8}-[0-9]{6}', unit)):
        raise ValueError('This bridge only supports the blocked alpha.52 archive verification failure')
    if any(item['status'] != 'awaiting_executor' or item.get('child_id') for item in items):
        raise ValueError('An agent update already crossed its execution boundary')
    if any(f'ActiveState={state}' in unit_state for state in
           ('active', 'activating', 'deactivating', 'reloading')):
        raise ValueError('The original update service is still active')
    error = ('Controller release unavailable: Unexpected or unsafe controller archive member: '
             'scripts/lib/archive_agent_journals.py')
    if error not in journal or 'status=1/FAILURE' not in journal or f'--stack-job {job["id"]}' not in journal:
        raise ValueError('Journal does not confirm this exact pre-installation failure')
    # Reject evidence that includes another attempt or any later installation step.
    if journal.count('Started ' + unit + '.service') != 1 or any(text in journal for text in
        ('Preparing new release:', 'Installing Python runtime for new release', 'Update complete.',
         'Applying database/schema init', 'Restarting node-plane')):
        raise ValueError('Execution history is not limited to one archive download failure')
    return unit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id', type=UUID)
    parser.add_argument('--apply', action='store_true', help='Explicitly prepare and launch the verified replacement updater')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('Run on the controller as root using its installed venv')
    from db import get_db
    db = get_db()
    with db.connect() as conn:
        row = conn.execute('SELECT * FROM backend_update_jobs WHERE id=?', (str(args.job_id),)).fetchone()
        if not row:
            raise ValueError('Update job not found')
        job = dict(row)
        items = [dict(row) for row in conn.execute(
            'SELECT status,child_id FROM backend_update_items WHERE job_id=?', (str(args.job_id),)).fetchall()]
    plan = json.loads(job.get('result_json') or '{}')
    unit = plan.get('unit_name', '')
    if not re.fullmatch(r'node-plane-update-[0-9]{8}-[0-9]{6}', unit):
        raise ValueError('Invalid recorded update unit')
    journal = subprocess.check_output(['journalctl', '-u', unit + '.service', '--no-pager', '-o', 'short'], text=True)
    state = subprocess.run(['systemctl', 'show', unit + '.service', '--property=ActiveState,SubState'],
                           text=True, capture_output=True, check=False).stdout
    validate(job, items, journal, state)
    root = Path(os.environ['NODE_PLANE_APP_DIR']).resolve()
    shared = Path(os.environ['NODE_PLANE_SHARED_DIR']).resolve()
    if (root / 'VERSION').read_text().strip() != '0.4.3-alpha.51':
        raise ValueError('The controller no longer runs alpha.51; reassess recovery first')
    print('Confirmed: alpha.51 updater failed before applying alpha.52. Existing gate and operation identity are retained.')
    if not args.apply:
        print('No changes made. Repeat with --apply to launch the verified replacement updater.')
        return
    spec = importlib.util.spec_from_file_location('installed_archive_verifier', root / 'scripts/controller_release.py')
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    # In-memory extension only; preserve all checksum, path, size and commit checks.
    verifier.SCRIPTS.add('lib/archive_agent_journals.py')
    parent = shared / 'data/update-recovery' / str(args.job_id)
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = parent / 'release'
    if stage.exists():
        raise ValueError('Recovery staging already exists; do not replay an uncertain launch')
    manifest = verifier.download('dev', 'v0.4.3-alpha.52', stage)
    if manifest['commit'] != plan['expected_commit']:
        raise ValueError('Downloaded release differs from the original immutable update target')
    subprocess.run([
        'systemd-run', '--unit', unit, '--property=Type=exec', '--working-directory', str(stage),
        '--setenv=NODE_PLANE_SOURCE_DIR=' + str(stage),
        '--setenv=NODE_PLANE_APP_DIR=' + str(shared.parent / 'current'),
        '--setenv=NODE_PLANE_SHARED_DIR=' + str(shared),
        '--setenv=NODE_PLANE_BASE_DIR=' + str(shared.parent),
        '--setenv=NODE_PLANE_INSTALL_MODE=simple',
        str(stage / 'scripts/update.sh'), '--mode', 'simple', '--branch', 'dev',
        '--to', 'v0.4.3-alpha.52', '--stack-job', str(args.job_id),
    ], check=True)
    print(f'Observe: journalctl -fu {unit}.service')
    print('After completion, resume the original workstation operation to recheck its result. No database gate was cleared.')


if __name__ == '__main__':
    main()
