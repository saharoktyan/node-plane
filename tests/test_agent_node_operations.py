import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('node_operations', Path(__file__).resolve().parents[1] / 'runtime_assets/backend-node-operation.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AgentNodeOperationTests(unittest.TestCase):
    def test_host_metrics_use_available_memory_and_runtime_filesystem(self):
        with patch.object(MODULE.Path,'read_text',return_value='MemTotal: 1000 kB\nMemAvailable: 200 kB\n'), \
             patch.object(MODULE.os,'statvfs',return_value=SimpleNamespace(f_blocks=100,f_bavail=7)), \
             patch.object(MODULE.os,'getloadavg',return_value=(4.5,2,1)), \
             patch.object(MODULE.os,'cpu_count',return_value=2):
            self.assertEqual(MODULE.host_metrics(),{'ram_used_percent':80.0,'disk_free_percent':7.0,'load1':4.5,'cpus':2})

    def test_missing_host_measurements_are_not_reported_as_healthy_zero(self):
        with patch.object(MODULE.Path,'read_text',side_effect=OSError), \
             patch.object(MODULE.os,'statvfs',side_effect=OSError), \
             patch.object(MODULE.os,'getloadavg',side_effect=OSError):
            self.assertEqual(MODULE.host_metrics(),{})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'journal.sqlite3'
        self.intent = {'node_key': 'n1', 'command_id': 'job1', 'revision': 2,
            'protocols': ['awg'], 'settings': {'public_host': 'node.example', 'awg_port': 51820}}

    def execute(self, action='reinstall_clean', recover=False):
        return MODULE.execute(action, 'job1', self.intent, recover, self.path)

    def test_retries_and_recovery_do_not_repeat_clean_reinstall(self):
        with patch.object(MODULE, 'run', return_value={'verified': True}) as run:
            result = self.execute()
            self.assertEqual(self.execute(), result)
            self.assertEqual(self.execute(recover=True), result)
            run.assert_called_once()
            with self.assertRaises(ValueError):
                self.execute('reinstall_keep')

    def test_interrupted_command_requires_agent_restart_before_retirement(self):
        with patch.dict(os.environ, {'NODE_PLANE_AGENT_INSTANCE_ID': 'old'}), patch.object(MODULE, 'run', side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.execute()
            with self.assertRaises(ValueError):
                self.execute('resolve_reinstall_clean', True)
        with patch.dict(os.environ, {'NODE_PLANE_AGENT_INSTANCE_ID': 'new'}):
            self.assertTrue(self.execute('resolve_reinstall_clean', True)['retired'])
            with self.assertRaises(ValueError):
                self.execute()

    def test_retiring_not_started_command_fences_a_delayed_request(self):
        self.assertTrue(self.execute('resolve_reinstall_clean', True)['retired'])
        with patch.object(MODULE, 'run') as run:
            with self.assertRaises(ValueError):
                self.execute()
            run.assert_not_called()

    def test_cleanup_removes_client_cache_but_preserves_agent_journal_and_scripts(self):
        root = Path(self.temp.name)
        for folder in ('xray', 'amnezia-awg/data', 'awg-clients'):
            (root / folder).mkdir(parents=True)
            (root / folder / 'config').write_text('private config')
        (root / 'helper.py').write_text('runtime helper')
        self.path.write_text('journal')
        with patch.object(MODULE, 'ROOT', root), patch.object(MODULE, 'environment',
                return_value=[str(root/'xray/config.json'), str(root/'amnezia-awg/data/wg0.conf'), 'xray', 'amnezia-awg']), \
             patch.object(MODULE.subprocess, 'run') as inspect, patch.object(MODULE, 'command'):
            inspect.return_value.returncode = 1
            MODULE.remove_protocol_runtime(0)
        self.assertFalse((root / 'awg-clients').exists())
        self.assertFalse((root / 'xray').exists())
        self.assertFalse((root / 'amnezia-awg').exists())
        self.assertTrue(self.path.exists())
        self.assertTrue((root / 'helper.py').exists())
