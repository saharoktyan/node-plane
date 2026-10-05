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

    def archive(self, **kwargs):
        return module.archive(self.controller, hashlib.sha256(b'new-ca').hexdigest(), 'n1',
            config=self.config, state=self.state, journal=self.journal,
            stop=lambda: self.stops.append(True), **kwargs)

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
