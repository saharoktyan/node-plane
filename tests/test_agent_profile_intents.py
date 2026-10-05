import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

SPEC = importlib.util.spec_from_file_location('agent_intents', Path(__file__).resolve().parents[1] / 'runtime_assets/apply-profile-intent.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AgentProfileIntentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'journal.sqlite3'
        self.calls = []

    def intent(self, revision=1, command='task-1', **fields):
        return {'command_id': command, 'protocol': 'awg', 'runtime_name': 'p_test',
                'revision': revision, 'action': 'ensure', 'uuid': '', 'short_id': '', **fields}

    def mutate(self, intent, fd):
        self.calls.append(intent.copy())
        return {'summary': 'private result', 'payload_json': ''}

    def test_persisted_duplicate_returns_original_result_without_mutation(self):
        intent = self.intent()
        first = MODULE.apply(intent, self.path, self.mutate)
        second = MODULE.apply(intent, self.path, self.mutate)
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(MODULE.lookup(intent, self.path), first)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(ValueError):
            MODULE.lookup(self.intent(action='delete'), self.path)

    def test_revision_fence_rejects_delayed_old_command(self):
        MODULE.apply(self.intent(), self.path, self.mutate)
        MODULE.apply(self.intent(2, 'task-2', action='delete'), self.path, self.mutate)
        for stale in [self.intent(1, 'late-task'), self.intent(2, 'another-task')]:
            with self.assertRaises(ValueError):
                MODULE.apply(stale, self.path, self.mutate)
        # Even after a newer command, replaying the old ID returns its history,
        # never enabling the old user again.
        MODULE.apply(self.intent(), self.path, self.mutate)
        self.assertEqual([call['action'] for call in self.calls], ['ensure', 'delete'])

    def test_decommission_requires_successful_latest_delete_and_fences_replay(self):
        command = str(uuid4())
        MODULE.apply(self.intent(), self.path, self.mutate)
        with self.assertRaises(ValueError):
            MODULE.prepare_decommission(self.path, command)
        MODULE.apply(self.intent(2, 'task-2', action='delete'), self.path, self.mutate)
        expected = {'kind': 'decommission', 'command_id': command}
        self.assertEqual(MODULE.prepare_decommission(self.path, command), expected)
        self.assertEqual(MODULE.prepare_decommission(self.path, command), expected)
        self.assertEqual(MODULE.verify_decommission(self.path, command), expected)
        with self.assertRaises(ValueError):
            MODULE.prepare_decommission(self.path, str(uuid4()))
        with self.assertRaises(ValueError):
            MODULE.apply(self.intent(3, 'task-3'), self.path, self.mutate)
        with self.assertRaises(ValueError):
            MODULE.guard_legacy(self.path)

    def test_decommission_rejects_interrupted_or_old_unknown_action(self):
        command = str(uuid4())
        def fail(intent, fd):
            raise TimeoutError('unknown outcome')
        with self.assertRaises(TimeoutError):
            MODULE.apply(self.intent(action='delete'), self.path, fail)
        with self.assertRaises(ValueError):
            MODULE.prepare_decommission(self.path, command)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE commands SET status = 'succeeded' WHERE id = 'task-1'")
            conn.execute('UPDATE fences SET action = NULL')
        with self.assertRaises(ValueError):
            MODULE.prepare_decommission(self.path, command)

    def test_empty_node_can_be_fenced_for_decommission(self):
        command = str(uuid4())
        self.assertEqual(MODULE.prepare_decommission(self.path, command)['kind'], 'decommission')
        self.assertFalse(self.path.exists())

    def test_node_operation_journal_without_profiles_can_be_removed(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute('''CREATE TABLE commands (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                response TEXT, instance_id TEXT NOT NULL)''')
            conn.execute("INSERT INTO commands VALUES ('docker-install', 'hash', 'succeeded', '{}', 'agent')")
        self.assertEqual(MODULE.prepare_decommission(self.path, str(uuid4()))['kind'], 'decommission')

    def test_decommission_revokes_historical_profiles_not_known_to_backend(self):
        MODULE.apply(self.intent(), self.path, self.mutate)
        second = self.intent(1, 'second', runtime_name='old_profile', protocol='xray',
                             uuid=str(uuid4()), short_id='0123456789abcdef')
        MODULE.apply(second, self.path, self.mutate)
        command = str(uuid4())
        MODULE.prepare_decommission(self.path, command, self.mutate)
        self.assertEqual([(i['runtime_name'], i['action']) for i in self.calls[-2:]],
                         [('p_test', 'delete'), ('old_profile', 'delete')])
        MODULE.prepare_decommission(self.path, command, self.mutate)
        self.assertEqual(len(self.calls), 4)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fences WHERE action!='delete'").fetchone()[0], 0)
        with self.assertRaises(ValueError):
            MODULE.apply(self.intent(3, 'late'), self.path, self.mutate)

    def test_historical_revocation_failure_stays_uncertain_and_does_not_fence_cleanup(self):
        MODULE.apply(self.intent(), self.path, self.mutate)
        def failed(intent, fd):
            raise TimeoutError('uncertain remote delete')
        command = str(uuid4())
        with self.assertRaises(TimeoutError):
            MODULE.prepare_decommission(self.path, command, failed)
        self.assertFalse(Path(str(self.path) + '.disabled').exists())
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            MODULE.prepare_decommission(self.path, command, self.mutate)
        self.assertEqual(len(self.calls), 1)

    def test_unfinished_node_operation_blocks_removal_even_without_profiles(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute('''CREATE TABLE commands (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                response TEXT, instance_id TEXT NOT NULL)''')
            conn.execute("INSERT INTO commands VALUES ('bootstrap', 'hash', 'running', NULL, 'agent')")
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            MODULE.prepare_decommission(self.path, str(uuid4()))
        self.assertFalse(Path(str(self.path) + '.disabled').exists())

    def test_old_journal_requires_new_delete_before_decommission(self):
        command = str(uuid4())
        with sqlite3.connect(self.path) as conn:
            conn.execute('''CREATE TABLE commands (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                response TEXT, instance_id TEXT NOT NULL)''')
            conn.execute('''CREATE TABLE fences (
                protocol TEXT, profile TEXT, revision INTEGER NOT NULL,
                command_id TEXT NOT NULL, PRIMARY KEY(protocol, profile))''')
            conn.execute("INSERT INTO commands VALUES ('old', 'old', 'succeeded', '{}', 'old')")
            conn.execute("INSERT INTO fences VALUES ('awg', 'p_test', 1, 'old')")
        with self.assertRaises(ValueError):
            MODULE.prepare_decommission(self.path, command)
        MODULE.apply(self.intent(2, 'task-2', action='delete'), self.path, self.mutate)
        self.assertEqual(MODULE.prepare_decommission(self.path, command)['kind'], 'decommission')

    def test_payload_conflict_cannot_reuse_command_id(self):
        MODULE.apply(self.intent(), self.path, self.mutate)
        with self.assertRaises(ValueError):
            MODULE.apply(self.intent(action='delete'), self.path, self.mutate)
        self.assertEqual(len(self.calls), 1)

    def test_uncertain_failure_blocks_whole_node_after_restart(self):
        def fail(intent, fd):
            raise TimeoutError('unknown remote outcome')

        with self.assertRaises(TimeoutError):
            MODULE.apply(self.intent(), self.path, fail)
        for intent in [self.intent(), self.intent(2, 'new-task', action='delete'),
                       self.intent(1, 'other-profile', runtime_name='p_other')]:
            with self.assertRaises(ValueError):
                MODULE.apply(intent, self.path, self.mutate)
        self.assertEqual(self.calls, [])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT status FROM commands').fetchone()[0], 'running')
        with self.assertRaises(ValueError):
            MODULE.lookup(self.intent(), self.path)

    def test_newer_revision_waits_for_inflight_mutation(self):
        started, finish = threading.Event(), threading.Event()

        def delayed(intent, fd):
            started.set()
            if not finish.wait(5):
                raise TimeoutError('test did not release mutation')
            return self.mutate(intent, fd)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(MODULE.apply, self.intent(), self.path, delayed)
            self.assertTrue(started.wait(5))
            second = pool.submit(MODULE.apply, self.intent(2, 'task-2', action='delete'), self.path, self.mutate)
            self.assertFalse(second.done())
            finish.set()
            first.result(timeout=5)
            second.result(timeout=5)
        self.assertEqual([call['revision'] for call in self.calls], [1, 2])

    def test_legacy_profile_commands_obey_fence_and_other_profiles_continue(self):
        runtime = Path(self.tmp.name) / 'runtime'
        runtime.mkdir()
        script = runtime / 'xray-del-user.sh'
        output = Path(self.tmp.name) / 'legacy-invocations'
        script.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "' + str(output) + '"\nprintf "OK\\n"\n')
        script.chmod(0o700)
        with patch.object(MODULE, '__file__', str(runtime / 'apply-profile-intent.py')):
            self.assertEqual(MODULE.run_legacy(self.path, 'xray', 'old.name', script.name, ['old.name']), 'OK')
            MODULE.apply(self.intent(protocol='xray', uuid='11111111-1111-4111-8111-111111111111', short_id='0123456789abcdef'), self.path, self.mutate)
            with self.assertRaises(ValueError):
                MODULE.run_legacy(self.path, 'xray', 'p_test', script.name, ['p_test'])
            self.assertEqual(MODULE.run_legacy(self.path, 'xray', 'other.name', script.name, ['other.name']), 'OK')
            self.assertEqual(output.read_text().splitlines(), ['old.name', 'other.name'])

    def test_maintenance_and_uninstall_marker_fail_closed(self):
        MODULE.guard_legacy(self.path)
        MODULE.apply(self.intent(), self.path, self.mutate)
        with self.assertRaises(ValueError):
            MODULE.guard_legacy(self.path)
        with self.assertRaises(ValueError):
            MODULE.guard_legacy(self.path, 'awg', 'p_test')
        with self.assertRaises(ValueError):
            MODULE.run_legacy(self.path, 'all', '', 'deploy-awg.sh', [])
        Path(str(self.path) + '.disabled').write_text('agent removal scheduled')
        with self.assertRaises(ValueError):
            MODULE.apply(self.intent(2, 'later'), self.path, self.mutate)
        with self.assertRaises(ValueError):
            MODULE.guard_legacy(self.path, 'xray', 'other')

    def test_old_command_finishes_before_new_fence_is_committed(self):
        runtime = Path(self.tmp.name) / 'runtime'
        runtime.mkdir()
        entered = Path(self.tmp.name) / 'entered'
        release = Path(self.tmp.name) / 'release'
        script = runtime / 'awg-del-user.sh'
        script.write_text('#!/bin/sh\nprintf entered > "' + str(entered) + '"\n'
                          'while [ ! -f "' + str(release) + '" ]; do sleep 0.01; done\n'
                          'printf "OK\\n"\n')
        script.chmod(0o700)
        with patch.object(MODULE, '__file__', str(runtime / 'apply-profile-intent.py')):
            with ThreadPoolExecutor(max_workers=2) as pool:
                old = pool.submit(MODULE.run_legacy, self.path, 'awg', 'p_test', script.name, ['p_test'])
                deadline = time.monotonic() + 5
                while not entered.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(entered.exists())
                new = pool.submit(MODULE.apply, self.intent(), self.path, self.mutate)
                self.assertFalse(new.done())
                release.touch()
                self.assertEqual(old.result(timeout=5), 'OK')
                new.result(timeout=5)
        with self.assertRaises(ValueError):
            MODULE.run_legacy(self.path, 'awg', 'p_test', script.name, ['p_test'])

    def test_unreadable_journal_does_not_allow_legacy_mutation(self):
        self.path.write_text('corrupt')
        with self.assertRaises(sqlite3.DatabaseError):
            MODULE.guard_legacy(self.path, 'awg', 'p_test')

    def test_repair_requires_agent_restart_and_live_inspection(self):
        intent = self.intent()
        observation = {'disk_present': True, 'live_present': False,
                       'identity_matches': False, 'config_available': False}

        def uncertain(previous, fd):
            raise TimeoutError('remote outcome unknown')

        with patch.dict(os.environ, {'NODE_PLANE_AGENT_INSTANCE_ID': 'before-restart'}):
            with self.assertRaises(TimeoutError):
                MODULE.apply(intent, self.path, uncertain)
            with self.assertRaises(ValueError):
                MODULE.resolve_interrupted(intent, self.path, lambda current: observation)
        with patch.dict(os.environ, {'NODE_PLANE_AGENT_INSTANCE_ID': 'after-restart'}):
            with self.assertRaises(ValueError):
                MODULE.resolve_interrupted(intent, self.path, lambda current: {'available': False})
            resolved = MODULE.resolve_interrupted(intent, self.path, lambda current: observation)
            self.assertEqual(resolved['observation'], observation)
            self.assertEqual(MODULE.resolve_interrupted(intent, self.path, lambda current: None), resolved)
            with self.assertRaises(ValueError):
                MODULE.apply(intent, self.path, self.mutate)
            MODULE.apply(self.intent(2, 'replacement', action='delete'), self.path, self.mutate)
        self.assertEqual([call['action'] for call in self.calls], ['delete'])

    def test_repair_cannot_change_original_payload_or_resolve_success(self):
        intent = self.intent()
        MODULE.apply(intent, self.path, self.mutate)
        with patch.dict(os.environ, {'NODE_PLANE_AGENT_INSTANCE_ID': 'new-instance'}):
            with self.assertRaises(ValueError):
                MODULE.resolve_interrupted(intent, self.path, lambda current: {})
            with self.assertRaises(ValueError):
                MODULE.resolve_interrupted(self.intent(action='delete'), self.path, lambda current: {})
