import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
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
