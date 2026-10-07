"""Ensure the installed stack can load without a hidden PTB dependency."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RetiredRuntimeTests(unittest.TestCase):
    def test_backend_worker_telegram_and_update_helpers_do_not_import_ptb(self):
        with tempfile.TemporaryDirectory() as directory:
            program = '''
import importlib.abc
import sys
class RejectPTB(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'telegram' or fullname.startswith('telegram.'):
            raise AssertionError('Retired PTB runtime imported: ' + fullname)
sys.meta_path.insert(0, RejectPTB())
import backend.http_api
import backend.executor
import backend.admin_cli
import telegram_client.main
import services.updates
import services.backups
'''
            result = subprocess.run([sys.executable, '-c', program],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, 'NODE_PLANE_SHARED_DIR': directory,
                     'DB_BACKEND': 'postgres', 'POSTGRES_DSN': 'postgresql://unused/unused'},
                capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
