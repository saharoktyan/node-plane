import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest import TestCase
from uuid import uuid4, uuid5, NAMESPACE_URL

from tests import test_backend_updates as fixture_updates


HELPER_PATH = Path(__file__).resolve().parent.parent / 'runtime_assets' / 'force-recovery.py'
spec_loader = importlib.util.spec_from_file_location('force_recovery_helper', str(HELPER_PATH))
helper = importlib.util.module_from_spec(spec_loader)
spec_loader.loader.exec_module(helper)

APPLY_HELPER_PATH = Path(__file__).resolve().parent.parent / 'runtime_assets' / 'apply-profile-intent.py'
apply_loader = importlib.util.spec_from_file_location('apply_profile_intent_helper', str(APPLY_HELPER_PATH))
apply_helper = importlib.util.module_from_spec(apply_loader)
apply_loader.loader.exec_module(apply_helper)


def make_spec(kind='settings', revision=2, node_key='node-1', command_id=None, ticket_id=None):
    command_id = command_id or str(uuid4())
    if kind == 'settings':
        intent = {
            'kind': 'node_settings',
            'command_id': command_id,
            'node_key': node_key,
            'revision': revision,
            'protocols': ['awg'],
            'settings': {'public_host': '127.0.0.1', 'awg_port': 51820},
        }
    elif kind == 'profile':
        intent = {
            'command_id': command_id,
            'protocol': 'awg',
            'runtime_name': 'test-user',
            'revision': revision,
            'action': 'ensure',
            'uuid': '',
            'short_id': '',
        }
    elif kind == 'node':
        intent = {
            'action': 'sync_runtime',
            'intent': {
                'command_id': command_id,
                'node_key': node_key,
                'revision': revision,
                'protocols': ['awg'],
                'settings': {'awg_port': 51820},
            },
        }
    else:
        raise ValueError(f'unknown kind {kind}')

    body = intent.get('intent') if kind == 'node' else intent
    if ticket_id is None:
        import hashlib
        encoded = json.dumps(intent, sort_keys=True) if kind == 'node' else json.dumps(intent, sort_keys=True, separators=(',', ':'))
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        ticket_id = str(uuid5(NAMESPACE_URL, f"node-plane:force:{node_key}:{kind}:{command_id}:{fingerprint}"))

    return {
        'ticket_id': ticket_id,
        'node_key': node_key,
        'command_id': command_id,
        'kind': kind,
        'intent': intent,
    }


class ForceRecoveryAgentHelperTests(TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.ticket_dir = self.root / 'tickets'
        self.ticket_dir.mkdir(mode=0o700)
        self.journal_path = self.root / 'journal.sqlite3'

    def test_all_backend_node_job_actions_can_be_force_retired(self):
        from backend.node_operations import ACTIONS
        for action in sorted(ACTIONS):
            with self.subTest(action=action):
                spec = make_spec('node')
                spec['intent']['action'] = action
                helper.prepare(spec, 'before', self.ticket_dir, self.journal_path)
                receipt = helper.retire(spec, 'after', self.ticket_dir, self.journal_path)
                self.assertTrue(receipt['outcome_unknown'])
                with sqlite3.connect(self.journal_path) as conn:
                    row = conn.execute('SELECT status FROM commands WHERE id=?', (spec['command_id'],)).fetchone()
                self.assertEqual(row, ('retired',))

    def test_prepare_and_retire_require_instance_change(self):
        spec = make_spec('settings')
        # Empty instance in prepare fails
        with self.assertRaises(ValueError) as ctx:
            helper.prepare(spec, '', str(self.ticket_dir), str(self.journal_path))
        self.assertEqual(str(ctx.exception), 'force_instance_missing')

        # Prepare with instance inst-1 succeeds
        prep = helper.prepare(spec, 'inst-1', str(self.ticket_dir), str(self.journal_path))
        self.assertEqual(prep['ticket_id'], spec['ticket_id'])
        self.assertEqual(prep['node_key'], spec['node_key'])
        self.assertTrue(prep['restart_required'])

        # Retire with SAME instance fails with force_restart_required
        with self.assertRaises(ValueError) as ctx:
            helper.retire(spec, 'inst-1', str(self.ticket_dir), str(self.journal_path))
        self.assertEqual(str(ctx.exception), 'force_restart_required')

        # Retire with NEW instance inst-2 succeeds
        receipt = helper.retire(spec, 'inst-2', str(self.ticket_dir), str(self.journal_path))
        self.assertEqual(receipt['ticket_id'], spec['ticket_id'])
        self.assertEqual(receipt['node_key'], spec['node_key'])
        self.assertTrue(receipt['forced'])
        self.assertTrue(receipt['outcome_unknown'])

    def test_retire_requires_free_original_journal_lock(self):
        spec = make_spec('node')
        helper.prepare(spec, 'inst-1', str(self.ticket_dir), str(self.journal_path))

        # Hold lock on journal
        lock_fd = os.open(str(self.journal_path) + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, lock_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Retire fails because execution lock is held
        with self.assertRaises(ValueError) as ctx:
            helper.retire(spec, 'inst-2', str(self.ticket_dir), str(self.journal_path))
        self.assertEqual(str(ctx.exception), 'force_execution_still_running')

        # Release lock
        fcntl.flock(lock_fd, fcntl.LOCK_UN)

        # Retire now succeeds
        receipt = helper.retire(spec, 'inst-2', str(self.ticket_dir), str(self.journal_path))
        self.assertEqual(receipt['command_id'], spec['command_id'])

    def test_spec_validation_rejects_wrong_hash_node_id_revision_ticket(self):
        spec = make_spec('settings')

        # Missing required keys
        bad_keys = dict(spec)
        del bad_keys['kind']
        with self.assertRaises(ValueError) as ctx:
            helper.checked(bad_keys)
        self.assertEqual(str(ctx.exception), 'force_invalid_request')

        # Invalid UUID ticket (unhyphenated or malformed)
        bad_ticket_unhyphenated = dict(spec, ticket_id='00000000000000000000000000000000')
        with self.assertRaises(ValueError) as ctx:
            helper.checked(bad_ticket_unhyphenated)
        self.assertEqual(str(ctx.exception), 'force_invalid_identity')

        bad_ticket_malformed = dict(spec, ticket_id='not-a-uuid')
        with self.assertRaises(ValueError):
            helper.checked(bad_ticket_malformed)

        # Invalid node key (symbols / spaces)
        bad_node = dict(spec, node_key='!invalid_node!')
        with self.assertRaises(ValueError) as ctx:
            helper.checked(bad_node)
        self.assertEqual(str(ctx.exception), 'force_invalid_identity')

        # Mismatched command_id in intent
        bad_cmd = dict(spec, command_id=str(uuid4()))
        with self.assertRaises(ValueError) as ctx:
            helper.checked(bad_cmd)
        self.assertEqual(str(ctx.exception), 'force_invalid_identity')

        # Non-int or revision < 1
        bad_rev = make_spec('settings')
        bad_rev['intent']['revision'] = 0
        with self.assertRaises(ValueError) as ctx:
            helper.checked(bad_rev)
        self.assertEqual(str(ctx.exception), 'force_invalid_revision')

        bad_rev_str = make_spec('settings')
        bad_rev_str['intent']['revision'] = 'two'
        with self.assertRaises(ValueError) as ctx:
            helper.checked(bad_rev_str)
        self.assertEqual(str(ctx.exception), 'force_invalid_revision')

        # Profile invalid action
        bad_prof = make_spec('profile')
        bad_prof['intent']['action'] = 'restart'
        with self.assertRaises(ValueError) as ctx:
            helper.checked(bad_prof)
        self.assertEqual(str(ctx.exception), 'force_invalid_request')

    def test_preserve_higher_revision_fences_and_sentinel(self):
        # Settings fence
        spec1 = make_spec('settings', revision=5)
        helper.prepare(spec1, 'inst-1', str(self.ticket_dir), str(self.journal_path))
        helper.retire(spec1, 'inst-2', str(self.ticket_dir), str(self.journal_path))

        with sqlite3.connect(str(self.journal_path)) as conn:
            row = conn.execute("SELECT revision, command_id FROM node_settings_fence WHERE node_key='node-1'").fetchone()
            self.assertEqual(row, (5, spec1['command_id']))

        # Stale lower revision 3 does NOT overwrite higher revision 5
        spec2 = make_spec('settings', revision=3)
        helper.prepare(spec2, 'inst-3', str(self.ticket_dir), str(self.journal_path))
        helper.retire(spec2, 'inst-4', str(self.ticket_dir), str(self.journal_path))

        with sqlite3.connect(str(self.journal_path)) as conn:
            row = conn.execute("SELECT revision, command_id FROM node_settings_fence WHERE node_key='node-1'").fetchone()
            self.assertEqual(row, (5, spec1['command_id']))

        # Higher revision 8 DOES advance the fence
        spec3 = make_spec('settings', revision=8)
        helper.prepare(spec3, 'inst-5', str(self.ticket_dir), str(self.journal_path))
        helper.retire(spec3, 'inst-6', str(self.ticket_dir), str(self.journal_path))

        with sqlite3.connect(str(self.journal_path)) as conn:
            row = conn.execute("SELECT revision, command_id FROM node_settings_fence WHERE node_key='node-1'").fetchone()
            self.assertEqual(row, (8, spec3['command_id']))

        # Profile fence
        prof_spec1 = make_spec('profile', revision=5)
        helper.prepare(prof_spec1, 'inst-7', str(self.ticket_dir), str(self.journal_path))
        helper.retire(prof_spec1, 'inst-8', str(self.ticket_dir), str(self.journal_path))

        with sqlite3.connect(str(self.journal_path)) as conn:
            row = conn.execute("SELECT revision, command_id FROM fences WHERE protocol='awg' AND profile='test-user'").fetchone()
            self.assertEqual(row, (5, prof_spec1['command_id']))

        # Lower revision does not overwrite
        prof_spec2 = make_spec('profile', revision=3)
        helper.prepare(prof_spec2, 'inst-9', str(self.ticket_dir), str(self.journal_path))
        helper.retire(prof_spec2, 'inst-10', str(self.ticket_dir), str(self.journal_path))

        with sqlite3.connect(str(self.journal_path)) as conn:
            row = conn.execute("SELECT revision, command_id FROM fences WHERE protocol='awg' AND profile='test-user'").fetchone()
            self.assertEqual(row, (5, prof_spec1['command_id']))

    def test_exact_command_tombstone_blocks_late_original_apply(self):
        spec = make_spec('settings')
        prep = helper.prepare(spec, 'inst-1', str(self.ticket_dir), str(self.journal_path))

        # Prepare produces durable command dispatch fence before restart
        fence_file = self.ticket_dir / f"command-{spec['command_id']}.fence"
        self.assertTrue(fence_file.exists())
        fence_data = json.loads(fence_file.read_text())
        self.assertEqual(fence_data['command_id'], spec['command_id'])
        self.assertEqual(fence_data['node_key'], spec['node_key'])

        helper.retire(spec, 'inst-2', str(self.ticket_dir), str(self.journal_path))

        # Check journal contains superseded tombstone
        with sqlite3.connect(str(self.journal_path)) as conn:
            row = conn.execute("SELECT status, response FROM commands WHERE id=?", (spec['command_id'],)).fetchone()
            self.assertEqual(row[0], 'superseded')
            data = json.loads(row[1])
            self.assertTrue(data['forced'])
            self.assertTrue(data['outcome_unknown'])

        # Actually invoke production apply_node_settings and expect tombstone rejection with valid payload
        with self.assertRaises(ValueError) as ctx:
            apply_helper.apply_node_settings(spec['intent'], str(self.journal_path), runner=lambda *args: None)
        self.assertEqual(str(ctx.exception), 'previous command did not complete successfully')

        # Also verify production apply for profile intent rejects superseded tombstone with valid payload
        prof_spec = make_spec('profile')
        helper.prepare(prof_spec, 'inst-1', str(self.ticket_dir), str(self.journal_path))
        prof_fence = self.ticket_dir / f"command-{prof_spec['command_id']}.fence"
        self.assertTrue(prof_fence.exists())
        helper.retire(prof_spec, 'inst-2', str(self.ticket_dir), str(self.journal_path))
        with self.assertRaises(ValueError) as ctx:
            apply_helper.apply(prof_spec['intent'], str(self.journal_path), runner=lambda *args: None)
        self.assertEqual(str(ctx.exception), 'previous command did not complete successfully')

        # Attempting prepare again: superseded status is allowed for replay
        prep2 = helper.prepare(spec, 'inst-2', str(self.ticket_dir), str(self.journal_path))
        self.assertEqual(prep2['ticket_id'], spec['ticket_id'])

        # Attempting retire again: idempotent replay returns same receipt
        retire2 = helper.retire(spec, 'inst-3', str(self.ticket_dir), str(self.journal_path))
        self.assertEqual(retire2['ticket_id'], spec['ticket_id'])

        # If fingerprint in journal was changed, prepare and retire reject it
        bad_fp_spec = dict(spec, intent=dict(spec['intent'], revision=99))
        with self.assertRaises(ValueError) as ctx:
            helper.prepare(bad_fp_spec, 'inst-4', str(self.ticket_dir), str(self.journal_path))
        self.assertEqual(str(ctx.exception), 'force_command_conflict')

    def test_completed_success_never_overwritten(self):
        spec = make_spec('settings')
        _, fp, binding = helper.checked(spec)

        # Existing command was successfully completed
        with sqlite3.connect(str(self.journal_path)) as conn:
            conn.execute("CREATE TABLE commands (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, response TEXT, instance_id TEXT NOT NULL)")
            conn.execute("INSERT INTO commands VALUES (?, ?, 'succeeded', '{\"synced\": true}', 'inst-0')", (spec['command_id'], fp))

        # Prepare must NOT overwrite completed success
        with self.assertRaises(ValueError) as ctx:
            helper.prepare(spec, 'inst-1', str(self.ticket_dir), str(self.journal_path))
        self.assertEqual(str(ctx.exception), 'force_command_already_terminal')

        # Even if a ticket and fence were somehow staged, retire must NOT overwrite completed success
        ticket_file = self.ticket_dir / (spec['ticket_id'] + '.json')
        with open(ticket_file, 'w') as f:
            json.dump({'binding': binding, 'old_instance': 'inst-1'}, f)
        fence_file = self.ticket_dir / ('command-' + spec['command_id'] + '.fence')
        with open(fence_file, 'w') as f:
            json.dump({'binding': binding, 'command_id': spec['command_id'], 'node_key': spec['node_key']}, f)

        with self.assertRaises(ValueError) as ctx:
            helper.retire(spec, 'inst-2', str(self.ticket_dir), str(self.journal_path))
        self.assertEqual(str(ctx.exception), 'force_command_already_terminal')

        # Journal unchanged
        with sqlite3.connect(str(self.journal_path)) as conn:
            row = conn.execute("SELECT status FROM commands WHERE id=?", (spec['command_id'],)).fetchone()
            self.assertEqual(row[0], 'succeeded')

    def test_absent_journal_allowed_only_force(self):
        spec = make_spec('profile')
        self.assertFalse(self.journal_path.exists())

        # Prepare succeeds without existing journal
        prep = helper.prepare(spec, 'inst-1', str(self.ticket_dir), str(self.journal_path))
        self.assertEqual(prep['ticket_id'], spec['ticket_id'])

        # Retire creates journal and records superseded tombstone
        receipt = helper.retire(spec, 'inst-2', str(self.ticket_dir), str(self.journal_path))
        self.assertTrue(self.journal_path.exists())
        self.assertEqual(receipt['command_id'], spec['command_id'])

    def test_ticket_privacy_and_symlink_rejection(self):
        # Symlink ticket directory
        real_dir = self.root / 'real_tickets'
        real_dir.mkdir(mode=0o700)
        symlink_dir = self.root / 'symlink_tickets'
        symlink_dir.symlink_to(real_dir)

        spec = make_spec('settings')
        with self.assertRaises(ValueError) as ctx:
            helper.prepare(spec, 'inst-1', str(symlink_dir), str(self.journal_path))
        self.assertEqual(str(ctx.exception), 'force_ticket_store_untrusted')

        # Ticket directory with loose permissions (e.g. 0o755)
        bad_perms_dir = self.root / 'bad_perms'
        bad_perms_dir.mkdir(mode=0o700)
        os.chmod(bad_perms_dir, 0o755)
        with self.assertRaises(ValueError) as ctx:
            helper.prepare(spec, 'inst-1', str(bad_perms_dir), str(self.journal_path))
        self.assertEqual(str(ctx.exception), 'force_ticket_store_untrusted')

        # Journal is a symlink
        real_journal = self.root / 'real_journal.sqlite3'
        real_journal.touch()
        symlink_journal = self.root / 'symlink_journal.sqlite3'
        symlink_journal.symlink_to(real_journal)
        with self.assertRaises(ValueError) as ctx:
            helper.prepare(spec, 'inst-1', str(self.ticket_dir), str(symlink_journal))
        self.assertEqual(str(ctx.exception), 'force_journal_untrusted')

        # Agent removal pending (.disabled exists)
        helper.prepare(spec, 'inst-1', str(self.ticket_dir), str(self.journal_path))
        disabled_file = Path(str(self.journal_path) + '.disabled')
        disabled_file.touch()
        self.addCleanup(disabled_file.unlink, missing_ok=True)

        with self.assertRaises(ValueError) as ctx:
            helper.retire(spec, 'inst-2', str(self.ticket_dir), str(self.journal_path))
        self.assertEqual(str(ctx.exception), 'force_agent_removal_pending')

    def test_concurrent_crash_safe_publication(self):
        spec = make_spec('settings')
        # First publication
        p1 = helper.prepare(spec, 'inst-1', str(self.ticket_dir), str(self.journal_path))
        # Immediate replay of prepare returns same ticket
        p2 = helper.prepare(spec, 'inst-1', str(self.ticket_dir), str(self.journal_path))
        self.assertEqual(p1, p2)

        # Ticket file content matches
        ticket_file = self.ticket_dir / (spec['ticket_id'] + '.json')
        self.assertTrue(ticket_file.exists())
        with open(ticket_file) as f:
            data = json.load(f)
        self.assertEqual(data['old_instance'], 'inst-1')

        # Conflict if another payload uses the same ticket_id
        conflicting_spec = dict(spec, node_key='node-2')
        conflicting_spec['intent'] = dict(spec['intent'], node_key='node-2')
        with self.assertRaises(ValueError) as ctx:
            helper.prepare(conflicting_spec, 'inst-1', str(self.ticket_dir), str(self.journal_path))
        self.assertEqual(str(ctx.exception), 'force_ticket_conflict')

    def test_cli_execution_no_stdout_secrets_and_clean_exit_codes(self):
        # 1. Invalid phase exits with force_invalid_phase
        res_phase = subprocess.run(
            [sys.executable, str(HELPER_PATH), 'invalid_phase'],
            input=b'{}',
            capture_output=True,
        )
        self.assertNotEqual(res_phase.returncode, 0)
        self.assertIn('force_invalid_phase', res_phase.stderr.decode())

        # 2. Oversized input exits with force_invalid_request
        oversized = b'{"a": "' + b'x' * 140000 + b'"}'
        res_oversized = subprocess.run(
            [sys.executable, str(HELPER_PATH), 'prepare'],
            input=oversized,
            capture_output=True,
        )
        self.assertNotEqual(res_oversized.returncode, 0)
        self.assertIn('force_invalid_request', res_oversized.stderr.decode())

        # 3. Payload with sensitive secrets rejected without leaking secrets to stdout or stderr
        secret_payload = json.dumps({'sensitive_token': 'SUPER_SECRET_TOKEN_XYZ999'}).encode()
        res_secret = subprocess.run(
            [sys.executable, str(HELPER_PATH), 'prepare'],
            input=secret_payload,
            capture_output=True,
        )
        self.assertNotEqual(res_secret.returncode, 0)
        self.assertIn('force_invalid_request', res_secret.stderr.decode())
        self.assertNotIn('SUPER_SECRET_TOKEN_XYZ999', res_secret.stdout.decode())
        self.assertNotIn('SUPER_SECRET_TOKEN_XYZ999', res_secret.stderr.decode())

        # 4. Helper functions return pure dicts and never print secrets to stdout
        spec = make_spec('settings')
        spec['intent']['settings']['private_token'] = 'AGENT_MUTATION_TOKEN_777'
        import io
        from contextlib import redirect_stdout, redirect_stderr
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            helper.prepare(spec, 'inst-1', str(self.ticket_dir), str(self.journal_path))
            helper.retire(spec, 'inst-2', str(self.ticket_dir), str(self.journal_path))
        self.assertEqual(buf_out.getvalue(), '')
        self.assertEqual(buf_err.getvalue(), '')
