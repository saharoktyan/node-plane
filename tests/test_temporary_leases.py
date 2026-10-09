"""Node-local lease expiry: no Docker or controller needed for these tests."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

from tests.test_agent_profile_intents import MODULE


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

    def overdue(self, intent):
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE temporary_leases SET expires_at=? WHERE protocol=? AND profile=?',
                         ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                          intent['protocol'], intent['runtime_name']))

    def status(self, intent):
        with sqlite3.connect(self.path) as conn:
            return conn.execute('SELECT status FROM temporary_leases WHERE protocol=? AND profile=?',
                                (intent['protocol'], intent['runtime_name'])).fetchone()[0]

    def test_both_protocols_get_one_persisted_day_and_duplicate_does_not_extend_it(self):
        for protocol in ('awg', 'xray'):
            intent = self.intent(protocol)
            before = datetime.now(timezone.utc)
            receipt = MODULE.apply(intent, self.path, self.mutate)
            deadline = datetime.fromisoformat(json.loads(receipt['payload_json'])['expires_at'])
            self.assertGreaterEqual(deadline, before + timedelta(days=1))
            self.assertLessEqual(deadline, datetime.now(timezone.utc) + timedelta(days=1))
            self.assertEqual(MODULE.apply(intent, self.path, self.mutate), receipt)
            self.assertEqual(MODULE.lookup(intent, self.path), receipt)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(MODULE.expire_leases(self.path, self.mutate), {'expired': 0, 'pending': 0})

    def test_expiry_isolated_from_permanent_and_other_temporary_credentials(self):
        first, second = self.intent(), self.intent()
        permanent = {**self.intent(), 'runtime_name': 'permanent'}
        permanent.pop('lease_seconds')
        for intent in (first, second, permanent):
            MODULE.apply(intent, self.path, self.mutate)
        self.overdue(first)
        self.assertEqual(MODULE.expire_leases(self.path, self.mutate), {'expired': 1, 'pending': 0})
        self.assertEqual((self.calls[-1]['runtime_name'], self.calls[-1]['action']),
                         (first['runtime_name'], 'delete'))
        self.assertEqual(self.status(second), 'active')
        MODULE.lookup(second, self.path)
        MODULE.lookup(permanent, self.path)
        self.assertEqual(MODULE.expire_leases(self.path, self.mutate), {'expired': 0, 'pending': 0})

    def test_expired_credentials_cannot_be_read_replayed_renewed_or_made_permanent(self):
        intent = self.intent('xray')
        MODULE.apply(intent, self.path, self.mutate)
        self.overdue(intent)
        for function in (MODULE.lookup, lambda i, p: MODULE.apply(i, p, self.mutate)):
            with self.assertRaises(ValueError):
                function(intent, self.path)
        MODULE.expire_leases(self.path, self.mutate)
        for leased in (True, False):
            replacement = dict(intent, command_id=str(uuid4()), revision=3)
            if not leased:
                replacement.pop('lease_seconds')
            with self.assertRaises(ValueError):
                MODULE.apply(replacement, self.path, self.mutate)
        self.assertEqual([i['action'] for i in self.calls], ['ensure', 'delete'])

    def test_interrupted_ensure_is_revoked_without_being_replayed(self):
        intent = self.intent()
        def interrupted(i, fd):
            self.mutate(i, fd)
            raise TimeoutError('private diagnostics')
        with self.assertRaises(TimeoutError):
            MODULE.apply(intent, self.path, interrupted)
        self.assertEqual(self.status(intent), 'provisioning')
        self.overdue(intent)
        self.assertEqual(MODULE.expire_leases(self.path, self.mutate), {'expired': 1, 'pending': 0})
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT status FROM commands WHERE id=?',
                                          (intent['command_id'],)).fetchone()[0], 'superseded')
        # Expiry frees the shared command journal for unrelated profiles.
        MODULE.apply(self.intent(), self.path, self.mutate)

    def test_failed_delete_is_persisted_and_retried_without_renewing_any_peer(self):
        first, second = self.intent(), self.intent('xray')
        for intent in (first, second):
            MODULE.apply(intent, self.path, self.mutate)
            self.overdue(intent)
        def unavailable(i, fd):
            if i['protocol'] == 'awg':
                raise TimeoutError('offline runtime')
            return self.mutate(i, fd)
        self.assertEqual(MODULE.expire_leases(self.path, unavailable), {'expired': 1, 'pending': 1})
        self.assertEqual(self.status(first), 'revoking')
        self.assertEqual(MODULE.expire_leases(self.path, self.mutate), {'expired': 1, 'pending': 0})
        self.assertEqual(self.status(first), 'expired')

    def test_early_revoke_cancels_local_expiry_and_cannot_reactivate_identity(self):
        intent = self.intent()
        MODULE.apply(intent, self.path, self.mutate)
        revoke = dict(intent, command_id=str(uuid4()), revision=2, action='delete')
        revoke.pop('lease_seconds')
        MODULE.apply(revoke, self.path, self.mutate)
        self.assertEqual(self.status(intent), 'revoked')
        self.assertEqual(MODULE.expire_leases(self.path, self.mutate,
                            datetime.now(timezone.utc) + timedelta(days=2)), {'expired': 0, 'pending': 0})
        with self.assertRaises(ValueError):
            MODULE.apply(dict(intent, command_id=str(uuid4()), revision=3), self.path, self.mutate)

    def test_invalid_duration_or_nonisolated_identity_never_runs(self):
        for fields in ({'lease_seconds': True}, {'lease_seconds': 1}, {'lease_seconds': 172800},
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

    def test_concurrent_expiry_scans_delete_once(self):
        intent = self.intent()
        MODULE.apply(intent, self.path, self.mutate)
        self.overdue(intent)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: MODULE.expire_leases(self.path, self.mutate), range(2)))
        self.assertEqual(sum(result['expired'] for result in results), 1)
        self.assertEqual([i['action'] for i in self.calls], ['ensure', 'delete'])

    def test_finished_node_cleanup_never_retries_removed_runtime_scripts(self):
        intent = self.intent()
        MODULE.apply(intent, self.path, self.mutate)
        MODULE.prepare_decommission(self.path, str(uuid4()), self.mutate)
        self.overdue(intent)
        self.assertEqual(MODULE.expire_leases(self.path, self.mutate), {'expired': 0, 'pending': 0})
        self.assertEqual([i['action'] for i in self.calls], ['ensure', 'delete'])

    def test_old_or_absent_journal_is_not_created_by_expiry_scan(self):
        self.assertEqual(MODULE.expire_leases(self.path, self.mutate), {'expired': 0, 'pending': 0})
        self.assertFalse(self.path.exists())
        with sqlite3.connect(self.path) as conn:
            conn.execute('CREATE TABLE old_fixture (id TEXT)')
        MODULE.expire_leases(self.path, self.mutate)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('old_fixture',)])
