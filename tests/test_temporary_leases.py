"""Node-local lease expiry: no Docker or controller needed for these tests."""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
import subprocess
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from uuid import uuid4

SPEC = importlib.util.spec_from_file_location('agent_intents', Path(__file__).resolve().parents[1] / 'runtime_assets/apply-profile-intent.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
from tests.test_agent_node_operations import MODULE as OPERATIONS


class TemporaryLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'journal.sqlite3'
        self.calls = []

    def intent(self, protocol='awg', **fields):
        return {'command_id': str(uuid4()), 'protocol': protocol,
                'runtime_name': 'tmp_' + uuid4().hex, 'revision': 1,
                'action': 'ensure', 'uuid': str(uuid4()) if protocol == 'xray' else '',
                'short_id': '0123456789abcdef' if protocol == 'xray' else '',
                'lease_seconds': 86400, **fields}

    def mutate(self, intent, fd):
        self.calls.append(dict(intent))
        return {'summary': 'private config', 'payload_json': ''}

    def status(self, intent):
        with sqlite3.connect(self.path) as conn:
            return conn.execute('SELECT status FROM temporary_leases WHERE protocol=? AND profile=?',
                                (intent['protocol'], intent['runtime_name'])).fetchone()[0]

    def test_retry_cannot_change_selected_lifetime(self):
        intent = self.intent(lease_seconds=43200)
        MODULE.apply(intent, self.path, self.mutate)
        with self.assertRaisesRegex(ValueError, 'identity conflict'):
            MODULE.apply(dict(intent, lease_seconds=259200), self.path, self.mutate)
        self.assertEqual(len(self.calls), 1)

    def test_early_delete_recovers_failed_ensure_and_retries_failed_delete(self):
        intent = self.intent('xray')
        def fail(i, fd):
            raise TimeoutError('lost result')
        with self.assertRaises(TimeoutError):
            MODULE.apply(intent,self.path,fail)
        deletion = dict(intent,command_id=str(uuid4()),revision=2,action='delete',uuid='',short_id='')
        deletion.pop('lease_seconds')
        with self.assertRaises(TimeoutError):
            MODULE.apply(deletion,self.path,fail)
        MODULE.apply(deletion,self.path,self.mutate)
        self.assertEqual(self.status(intent),'revoked')
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT status FROM commands WHERE id=?',(intent['command_id'],)).fetchone()[0],'superseded')
        MODULE.apply(deletion,self.path,self.mutate)
        self.assertEqual(len(self.calls),1)

    def test_status_accepts_transport_node_key_and_never_mutates(self):
        intent = self.intent('xray')
        MODULE.apply(intent,self.path,self.mutate)
        with patch.object(OPERATIONS.intents,'apply',side_effect=AssertionError('mutation')):
            state = OPERATIONS.execute('temporary_status',intent['command_id'],dict(intent,node_key='n1'),True,self.path)
            self.assertEqual(state['status'],'active')
            with self.assertRaises(ValueError):
                OPERATIONS.execute('temporary_status',intent['command_id'],dict(intent,node_key='n1'),False,self.path)
        # As the expiry scan leaves it.
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE temporary_leases SET status='expired'")
        self.assertEqual(OPERATIONS.execute('temporary_status',intent['command_id'],intent,True,self.path)['status'],'expired')

    def test_early_delete_does_not_ignore_unrelated_interrupted_command(self):
        intent = self.intent()
        MODULE.apply(intent,self.path,self.mutate)
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO commands VALUES ('unrelated','fingerprint','running',NULL,'standalone')")
        deletion = dict(intent,command_id=str(uuid4()),revision=2,action='delete')
        deletion.pop('lease_seconds')
        with self.assertRaisesRegex(ValueError,'unfinished'):
            MODULE.apply(deletion,self.path,self.mutate)

    def test_invalid_duration_or_nonisolated_identity_never_runs(self):
        for fields in ({'lease_seconds': True}, {'lease_seconds': 1}, {'lease_seconds': 172800},
                       {'lease_seconds': 259201}, {'lease_seconds': 345600},
                       {'runtime_name': 'permanent'}, {'action': 'delete'}):
            with self.assertRaises(ValueError):
                MODULE.apply(self.intent(**fields), self.path, self.mutate)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.path.exists())

    def test_temporary_request_cannot_overwrite_an_existing_permanent_peer(self):
        intent = self.intent()
        permanent = dict(intent)
        permanent.pop('lease_seconds')
        MODULE.apply(permanent, self.path, self.mutate)
        with self.assertRaises(ValueError):
            MODULE.apply(dict(intent, command_id=str(uuid4()), revision=2), self.path, self.mutate)
        self.assertEqual(len(self.calls), 1)

    def test_scheduler_is_persistent_and_independent_of_agent_service(self):
        calls = []
        directory = Path(self.tmp.name) / 'units'
        directory.mkdir()
        def execute(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)
        MODULE.ensure_lease_scheduler(directory, execute)
        first = (directory / 'node-plane-lease-expiry.service').stat().st_mtime_ns
        MODULE.ensure_lease_scheduler(directory, execute)
        self.assertEqual((directory / 'node-plane-lease-expiry.service').stat().st_mtime_ns, first)
        self.assertEqual(calls.count(['systemctl', 'daemon-reload']), 1)
        unit = (directory / 'node-plane-lease-expiry.service').read_text()
        self.assertNotIn('node-plane-agent.service', unit)
        self.assertIn('ExecStart=/usr/local/bin/node-plane-agent lease monitor', unit)
        self.assertIn('Type=simple', unit)
        self.assertIn('StartLimitIntervalSec=0', unit)
        self.assertIn(['systemctl','reset-failed','node-plane-lease-expiry.service','node-plane-lease-expiry.timer'], calls)
        self.assertIn('KillMode=control-group', unit)
        self.assertIn('WantedBy=timers.target', (directory / 'node-plane-lease-expiry.timer').read_text())

    def test_first_scheduler_ignores_unloaded_reset_but_requires_start(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            def execute(args, **kwargs):
                calls.append((args, kwargs))
                if args[1] == 'reset-failed':
                    if kwargs.get('check'):
                        raise subprocess.CalledProcessError(1, args)
                    return subprocess.CompletedProcess(args, 1)
                return subprocess.CompletedProcess(args, 0)
            MODULE.ensure_lease_scheduler(directory, execute)
            self.assertTrue(any(args[1] == 'start' for args, _ in calls))
            self.assertFalse(next(kw['check'] for args, kw in calls if args[1] == 'reset-failed'))
            def failing_start(args, **kwargs):
                if args[1] == 'start':
                    raise subprocess.CalledProcessError(1, args)
                return execute(args, **kwargs)
            with self.assertRaises(subprocess.CalledProcessError):
                MODULE.ensure_lease_scheduler(directory, failing_start)

    def test_unused_temporary_resolution_fences_a_delayed_original_request(self):
        intent = self.intent()
        empty = dict.fromkeys(('disk_present','live_present','identity_matches','config_available'), False)
        with patch.object(OPERATIONS.intents, 'inspect_for_repair', return_value=empty):
            result = OPERATIONS.execute('temporary_resolve', intent['command_id'], intent, recover=True, path=self.path)
            self.assertTrue(result['unused_confirmed'])
            self.assertEqual(OPERATIONS.execute('temporary_resolve', intent['command_id'], intent, recover=True, path=self.path), result)
        with self.assertRaises(ValueError):
            MODULE.apply(intent, self.path, self.mutate)
        self.assertEqual(self.calls, [])
        with self.assertRaises(ValueError):
            OPERATIONS.execute('temporary_resolve', intent['command_id'], {**intent, 'lease_seconds':43200}, recover=True, path=self.path)

    def test_unused_resolution_requires_absence_and_old_agent_instance(self):
        intent = self.intent()
        empty = dict.fromkeys(('disk_present','live_present','identity_matches','config_available'), False)
        def crash(*args):
            raise RuntimeError('interrupted')
        with patch.dict('os.environ', {'NODE_PLANE_AGENT_INSTANCE_ID':'old'}):
            with self.assertRaises(RuntimeError):
                MODULE.apply(intent, self.path, crash)
            with patch.object(OPERATIONS.intents, 'inspect_for_repair', return_value=empty), self.assertRaises(ValueError):
                OPERATIONS.execute('temporary_resolve', intent['command_id'], intent, recover=True, path=self.path)
        # apply records a lease before invoking the mutator: never cancel that lease.
        with patch.dict('os.environ', {'NODE_PLANE_AGENT_INSTANCE_ID':'new'}), patch.object(OPERATIONS.intents, 'inspect_for_repair', return_value=empty), self.assertRaises(ValueError):
            OPERATIONS.execute('temporary_resolve', intent['command_id'], intent, recover=True, path=self.path)
        other = self.intent()
        for observation in ({**empty, 'live_present':True}, {**empty, 'disk_present':True}, {}, {**empty, 'live_present':0}):
            with patch.object(OPERATIONS.intents, 'inspect_for_repair', return_value=observation), self.assertRaises(ValueError):
                OPERATIONS.execute('temporary_resolve', other['command_id'], other, recover=True, path=self.path)

    def test_scheduler_failure_never_provisions_a_peer(self):
        intent = self.intent()
        with patch.object(OPERATIONS.intents, 'ensure_lease_scheduler', side_effect=RuntimeError('systemd unavailable')), \
             patch.object(OPERATIONS.intents, 'run') as mutate:
            with self.assertRaises(RuntimeError):
                OPERATIONS.execute('temporary_ensure', intent['command_id'], intent, path=self.path)
            mutate.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_authenticated_node_action_returns_exact_receipt_and_recovers_without_mutation(self):
        intent = self.intent()
        def mutate(i, fd):
            self.calls.append(i)
            return {'summary': '[Interface]\nPrivateKey = secret\n\n=========== key\nvpn://encoded\n====', 'payload_json': ''}
        with patch.object(OPERATIONS.intents, 'ensure_lease_scheduler'), \
             patch.object(OPERATIONS.intents, 'run', side_effect=mutate):
            first = OPERATIONS.execute('temporary_ensure', intent['command_id'], intent, path=self.path)
            recovered = OPERATIONS.execute('temporary_ensure', intent['command_id'], intent, recover=True, path=self.path)
            self.assertEqual(first, recovered)
            self.assertEqual(first['vpn_key'], 'vpn://encoded')
            self.assertNotIn('===========', first['wg_conf'])
            self.assertEqual(len(self.calls), 1)
            revoke = dict(intent, command_id=str(uuid4()), action='delete', revision=2)
            revoke.pop('lease_seconds')
            OPERATIONS.execute('temporary_revoke', revoke['command_id'], revoke, path=self.path)
            with self.assertRaises(ValueError):
                OPERATIONS.execute('temporary_ensure', intent['command_id'], intent, recover=True, path=self.path)

    def test_rpc_rejects_invalid_duration_and_conflicting_identity_before_scheduler(self):
        for intent in (self.intent(lease_seconds=345600), self.intent(command_id='conflicting')):
            with patch.object(OPERATIONS.intents, 'ensure_lease_scheduler') as scheduler:
                with self.assertRaises(ValueError):
                    command_id = intent['command_id'] if intent['lease_seconds'] == 345600 else 'request-id'
                    OPERATIONS.execute('temporary_ensure', command_id, intent, path=self.path)
                scheduler.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_vless_receipt_and_early_revocation_report_existing_connection_limit(self):
        intent = self.intent('xray', lease_seconds=259200)
        with patch.object(OPERATIONS.intents, 'ensure_lease_scheduler'), \
             patch.object(OPERATIONS.intents, 'run', return_value={'summary': 'OK', 'payload_json': ''}) as mutate:
            receipt = OPERATIONS.execute('temporary_ensure', intent['command_id'], intent, path=self.path)
            self.assertEqual(receipt['xray_uuid'], intent['uuid'])
            self.assertEqual(receipt['revocation_mode'], 'new_connections_only')
            self.assertNotIn('wg_conf', receipt)
            self.assertEqual(OPERATIONS.execute('temporary_ensure', intent['command_id'], intent,
                                               recover=True, path=self.path), receipt)
            self.assertEqual(mutate.call_count, 1)
            revoke = dict(intent, action='delete', revision=2, command_id=str(uuid4()), uuid='', short_id='')
            revoke.pop('lease_seconds')
            result = OPERATIONS.execute('temporary_revoke', revoke['command_id'], revoke, path=self.path)
            self.assertEqual(result['revocation_mode'], 'new_connections_only')
            self.assertEqual(self.status(intent), 'revoked')
            with self.assertRaises(ValueError):
                OPERATIONS.execute('temporary_ensure', intent['command_id'], intent, recover=True, path=self.path)

    def test_rpc_temporary_revoke_cannot_delete_permanent_peer(self):
        intent = self.intent()
        intent.pop('lease_seconds')
        MODULE.apply(intent, self.path, self.mutate)
        revoke = dict(intent, action='delete', revision=2, command_id=str(uuid4()))
        with patch.object(OPERATIONS.intents, 'run') as mutate:
            with self.assertRaises(ValueError):
                OPERATIONS.execute('temporary_revoke', revoke['command_id'], revoke, path=self.path)
            mutate.assert_not_called()
