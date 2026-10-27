import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

from backend.installation_identity import controller_identity
from tests.test_backend_identity import Database

spec = importlib.util.spec_from_file_location('archive_agent_journals',
    Path(__file__).resolve().parents[1] / 'scripts/lib/archive_agent_journals.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AgentJournalArchivalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = root / 'etc/agent.toml'
        self.config.parent.mkdir()
        self.state = root / 'state'
        self.state.mkdir()
        self.journal = root / 'etc/profile-intents.sqlite3'
        self.controller = str(uuid4())
        self.old = str(uuid4())
        self.marker = self.state / 'controller-installation-id'
        self.marker.write_text(self.old)
        self.config.write_text('node_key = "n1"\n')
        self.stops = []

    def archive(self, node_key='n1', *, controller=None, ca_digest=None, **kwargs):
        controller = self.controller if controller is None else controller
        ca_digest = hashlib.sha256(b'new-ca').hexdigest() if ca_digest is None else ca_digest
        options = {'config': self.config, 'state': self.state, 'journal': self.journal,
                   'stop': lambda: self.stops.append(True)}
        options.update(kwargs)
        return module.archive(controller, ca_digest, node_key, **options)

    def test_new_controller_archives_sqlite_and_all_sidecars_then_is_idempotent(self):
        contents = {}
        for suffix in ('', '-wal', '-shm', '-journal', '.lock', '.disabled'):
            path = Path(str(self.journal) + suffix)
            contents[path] = ('data' + suffix).encode()
            path.write_bytes(contents[path])
        archive = self.archive()
        self.assertTrue(archive.name.startswith('previous-controller-' + self.old + '-'))
        self.assertEqual(self.stops, [True])
        for path, body in contents.items():
            self.assertFalse(path.exists())
            self.assertEqual((archive / path.name).read_bytes(), body)
            self.assertEqual((archive / path.name).stat().st_mode & 0o777, 0o600)
        manifest = json.loads((archive / 'manifest.json').read_text())
        self.assertEqual(manifest['previous_controller'], self.old)
        self.assertEqual(manifest['new_controller'], self.controller)
        self.journal.write_bytes(b'new active journal')
        self.assertIsNone(self.archive())
        self.assertEqual(self.journal.read_bytes(), b'new active journal')
        self.assertEqual(self.stops, [True])

    def test_same_controller_different_node_refuses_without_modifications(self):
        self.marker.write_text(self.controller)
        marker_bytes = self.marker.read_bytes()
        config_bytes = self.config.read_bytes()
        sidecars = {}
        for suffix in ('', '-wal', '-shm', '-journal', '.lock', '.disabled'):
            path = Path(str(self.journal) + suffix)
            sidecars[path] = ('active' + suffix).encode()
            path.write_bytes(sidecars[path])

        with self.assertRaisesRegex(ValueError, 'different node'):
            self.archive('n2')

        self.assertEqual(self.stops, [])
        for path, body in sidecars.items():
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), body)
        self.assertEqual(self.marker.read_bytes(), marker_bytes)
        self.assertEqual(self.config.read_bytes(), config_bytes)
        self.assertFalse((self.state / 'journal-archives').exists())

    def test_same_controller_same_key_preserves_journal_and_sidecars_without_stopping(self):
        self.marker.write_text(self.controller)
        contents = {}
        for suffix in ('', '-wal', '-shm', '-journal', '.lock', '.disabled'):
            path = Path(str(self.journal) + suffix)
            contents[path] = ('preserve' + suffix).encode()
            path.write_bytes(contents[path])

        result = self.archive('n1')

        self.assertIsNone(result)
        self.assertEqual(self.stops, [])
        for path, body in contents.items():
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), body)
        self.assertEqual(self.marker.read_text().strip(), self.controller)
        self.assertFalse((self.state / 'journal-archives').exists())

    def test_legacy_same_ca_different_node_refuses_without_modifications(self):
        self.marker.unlink()
        ca = self.config.parent / 'tls/ca.crt'
        ca.parent.mkdir(parents=True, exist_ok=True)
        ca.write_bytes(b'new-ca')
        ca_bytes = ca.read_bytes()
        config_bytes = self.config.read_bytes()
        sidecars = {}
        for suffix in ('', '-wal', '-shm', '-journal', '.lock', '.disabled'):
            path = Path(str(self.journal) + suffix)
            sidecars[path] = ('legacy' + suffix).encode()
            path.write_bytes(sidecars[path])

        with self.assertRaisesRegex(ValueError, 'different node'):
            self.archive('n2')

        self.assertEqual(self.stops, [])
        self.assertFalse(self.marker.exists())
        self.assertEqual(ca.read_bytes(), ca_bytes)
        self.assertEqual(self.config.read_bytes(), config_bytes)
        for path, body in sidecars.items():
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), body)
        self.assertFalse((self.state / 'journal-archives').exists())

    def test_legacy_same_ca_adopts_identity_without_clearing_journal(self):
        self.marker.unlink()
        ca = self.config.parent / 'tls/ca.crt'
        ca.parent.mkdir()
        ca.write_bytes(b'new-ca')
        self.journal.write_bytes(b'active fences')
        self.assertIsNone(self.archive())
        self.assertEqual(self.journal.read_bytes(), b'active fences')
        self.assertEqual(self.stops, [])

    def test_legacy_changed_ca_archives_previous_installation(self):
        self.marker.unlink()
        ca = self.config.parent / 'tls/ca.crt'
        ca.parent.mkdir()
        ca.write_bytes(b'old-ca')
        self.journal.write_bytes(b'previous fences')
        self.assertIn('previous-controller-legacy-', self.archive().name)

    def test_symlink_and_unknown_legacy_owner_refuse_without_stopping_agent(self):
        self.marker.unlink()
        self.journal.write_bytes(b'unknown')
        with self.assertRaisesRegex(ValueError, 'identify legacy'):
            self.archive()
        self.marker.write_text(self.old)
        self.journal.unlink()
        self.journal.symlink_to(self.config)
        with self.assertRaisesRegex(ValueError, 'symlinked'):
            self.archive()
        self.assertEqual(self.stops, [])

    def test_interrupted_move_restores_existing_files_and_owner(self):
        self.journal.write_bytes(b'journal')
        wal = Path(str(self.journal) + '-wal')
        wal.write_bytes(b'wal')
        original = module.shutil.move
        calls = []
        def interrupted(source, target):
            calls.append(source)
            if len(calls) == 2:
                raise OSError('move failed')
            return original(source, target)
        with patch.object(module.shutil, 'move', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.archive()
        self.assertEqual(self.journal.read_bytes(), b'journal')
        self.assertEqual(wal.read_bytes(), b'wal')
        self.assertEqual(self.marker.read_text(), self.old)

    def test_controller_identity_is_stable_per_database(self):
        first, second = Database(), Database()
        self.addCleanup(first.connection.close)
        self.addCleanup(second.connection.close)
        identity = controller_identity(first)
        self.assertEqual(controller_identity(first), identity)
        self.assertNotEqual(controller_identity(second), identity)

    def test_retired_node_handover_archives_fences_before_rebinding(self):
        self.marker.write_text(self.controller)
        self.journal.write_bytes(b'old fences')
        disabled = Path(str(self.journal) + '.disabled')
        disabled.write_bytes(b'old disabled marker')
        result = self.archive('n2', retired_node_keys=['n1'])
        self.assertEqual(self.stops, [True])
        self.assertEqual((result / self.journal.name).read_bytes(), b'old fences')
        self.assertEqual((result / disabled.name).read_bytes(), b'old disabled marker')
        manifest = json.loads((result / 'manifest.json').read_text())
        self.assertEqual((manifest['previous_node'], manifest['new_node']), ('n1', 'n2'))
        self.assertTrue(manifest['previous_node_retired'])
        # The installer later installs the new agent config. Repeating its
        # archive boundary must preserve the new node's command journal.
        self.config.write_text('node_key = "n2"\n')
        self.journal.write_bytes(b'new fences')
        self.assertIsNone(self.archive('n2', retired_node_keys=['n1']))
        self.assertEqual(self.journal.read_bytes(), b'new fences')
        self.assertEqual(self.stops, [True])

    def test_ownership_probe_is_read_only_and_requires_exact_retirement(self):
        self.marker.write_text(self.controller)
        self.journal.write_bytes(b'active fences')
        before = {path: path.read_bytes() for path in (self.config, self.marker, self.journal)}
        result = self.archive('n2', retired_node_keys=['n1'], check_only=True)
        self.assertEqual(result, {'reassignable': True, 'previous_node': 'n1', 'controller': self.controller})
        self.assertEqual(self.stops, [])
        self.assertFalse((self.state / 'journal-archive.lock').exists())
        for path, contents in before.items():
            self.assertEqual(path.read_bytes(), contents)
        with self.assertRaises(module.JournalOwnerConflict):
            self.archive('n2', retired_node_keys=['other'], check_only=True)

    def test_probe_does_not_adopt_missing_or_foreign_installation(self):
        self.marker.write_text(self.old)
        self.assertFalse(self.archive('n2', retired_node_keys=['n1'], check_only=True)['reassignable'])
        self.marker.unlink()
        self.state.rmdir()
        self.assertFalse(self.archive('n2', retired_node_keys=['n1'], check_only=True)['reassignable'])
        self.assertFalse(self.state.exists())

    def test_invalid_retirement_proof_never_stops_or_archives(self):
        self.marker.write_text(self.controller)
        self.journal.write_bytes(b'active fences')
        with self.assertRaisesRegex(ValueError, 'invalid retired'):
            self.archive('n2', retired_node_keys=['n1;echo invalid'])
        self.assertEqual(self.stops, [])
        self.assertEqual(self.journal.read_bytes(), b'active fences')
